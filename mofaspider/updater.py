"""资源全量更新模式：更新库内已有影视资源的关联元数据。

与 crawler.py（新增采集）完全隔离，本模块只做「更新」：
  1. 从数据库批量拉取待更新资源列表，按 resource_id / source_url 分组去重生成任务队列，
     支持按 update_time、资源状态(status)等条件筛选，避免重复处理；
  2. 对每条任务调用站点解析规则精准爬取该资源最新全量信息（复用 HttpClient/parser/ImageDownloader）；
  3. 数据合法性校验：过滤无效/空值/异常脏数据，仅对发生变更的字段执行 UPDATE，
     未变更字段保留原值（由 Pipeline.save_changed 原子完成）；
  4. 异常容错：爬取失败自动进入重试队列，最多 3 次；重试失败记录错误日志并标记失败状态；
  5. 增量/全量两种子模式：incremental 仅更新 update_time 超过阈值(如 24h)的旧数据；
     full 更新全部；
  6. 全程记录执行日志与统计，输出完整更新报告。

设计要点：
  - 复用已有 HttpClient（限速/UA轮换/退避重试/反爬检测）、parser（详情/播放解析）、
    ImageDownloader（图片落盘）、Pipeline.save_changed（变更字段更新）；
  - 数据库写入串行加锁（PyMySQL 非线程安全），网络抓取并发（线程池）；
  - 失败资源记录到失败清单，最终写错误日志并统计；
  - 独立调用入口 run_update.py，不影响 run.py 的新增采集逻辑。

覆盖全部资源（专项修复）：
  - 主键 id 升序**游标分批遍历**（WHERE id>last_id ORDER BY id ASC LIMIT batch），
    id 自增不变，保证无遗漏、无重复、可断点续传；每批处理完推进游标并落盘，
    内存峰值受 batch_size 限制（不再一次性 fetch_all 全量 + 全量提交）；
  - 断点续传：游标/进度存 STATE_DIR/update_state.json，按 mode 分键（full/incremental
    互不干扰），中断后重跑从断点继续；遍历完成自动清空游标；
  - limit 控制游标推进，保证每次运行处理的是「不同」资源而非固定头部。

dry-run（试跑，默认关闭）：
  - 只抓取/解析/比对，**跳过全部数据库写操作**（主表字段与播放源/分集/属性子表均不写），
    仅统计「将变更的字段」；
  - 同时跳过图片下载（避免落盘副作用）、不写断点游标、不写失败清单、忽略既有游标
    （从 id=0 完整比对），保证对生产库零副作用；
  - 比对复用 Pipeline.preview_changed，与 save_changed 共享同一套变更规则。
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from .config import Config
from .db import DB, T_FILM
from .http import HttpClient
from .images import ImageDownloader
from .logger import get_logger
from .models import FILM_TYPES, Film
from .parser import parse_detail, parse_view
from .pipeline import Pipeline
from .timeutil import normalize_timestamp_ms, timestamp_ms_sql_expr

logger = get_logger()

# 更新模式最大重试次数（需求：最多支持 3 次重试）
UPDATE_MAX_RETRY = 3
# 单资源更新失败重试的退避基数（秒）
UPDATE_RETRY_BACKOFF = 3.0
# 分批游标遍历：每批处理条数（控制内存峰值，避免全量加载 O(N) 内存）
UPDATE_BATCH_SIZE = 200
# 断点续传游标状态文件名（存于 STATE_DIR 下）
UPDATE_STATE_FILE = "update_state.json"


@dataclass
class UpdateTask:
    """单条待更新资源任务。"""

    resource_id: int          # ba_film.id（自增主键）
    source_url: str           # 去重主键（详情页地址）
    title: str = ""           # 库内已有标题（爬取失败时用于日志）
    film_type: str = "movie"  # 库内已有类型（详情解析用，保证字段精准匹配）
    status: str = "1"         # 库内资源状态
    update_time: int = 0      # 库内 update_time（毫秒时间戳），用于增量筛选
    retry: int = 0            # 已重试次数


@dataclass
class UpdateResult:
    """单条资源更新结果。"""

    task: UpdateTask
    action: str = "failed"    # updated / unchanged / failed
    changed_fields: int = 0
    error: str = ""
    # dry-run 专用：将变更字段清单与新旧值预览（正式模式为空，避免内存占用）
    changed_field_names: List[str] = field(default_factory=list)
    preview: List[Dict] = field(default_factory=list)
    dry_run: bool = False     # 标记该结果来自 dry-run（用于报告累计口径）


@dataclass
class UpdateReport:
    """更新执行报告（需求：完整统计信息）。"""

    total: int = 0
    success: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    retried: int = 0
    elapsed_ms: int = 0
    mode: str = "full"
    # dry-run：仅比对不写库；统计「将变更字段」总量并保留少量明细供核验
    dry_run: bool = False
    changed_fields_total: int = 0
    preview_items: List[Dict] = field(default_factory=list)
    failed_items: List[Dict] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "mode": self.mode,
            "dry_run": self.dry_run,
            "total": self.total,
            "success": self.success,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "failed": self.failed,
            "retried": self.retried,
            "changed_fields_total": self.changed_fields_total,
            "elapsed_ms": self.elapsed_ms,
            "elapsed_sec": round(self.elapsed_ms / 1000, 2),
            "preview_items": self.preview_items[:100],
            "failed_items": self.failed_items[:100],
        }


class Updater:
    """资源全量更新引擎。"""

    def __init__(self, cfg: Config, db: DB):
        self._cfg = cfg
        self._db = db
        self._http = HttpClient(cfg.spider)
        self._img = ImageDownloader(cfg.spider, self._http, cfg.root,
                                    enabled=cfg.spider.image_download)
        self._pipeline = Pipeline(db, cfg.mofa.category_id_map)
        self._db_lock = threading.Lock()

    # ---------------- 对外入口 ----------------
    def run(self, *, mode: str = "full", since_hours: int = 0,
            resource_ids: Optional[List[int]] = None,
            source_urls: Optional[List[str]] = None,
            status_filter: Optional[str] = None,
            types_filter: Optional[List[str]] = None,
            limit: int = 0, workers: Optional[int] = None,
            retry_max: int = UPDATE_MAX_RETRY,
            batch_size: int = UPDATE_BATCH_SIZE,
            resume: bool = True,
            dry_run: bool = False) -> UpdateReport:
        """执行资源全量更新。

        修复要点（全量更新无法覆盖全部资源）：
          - 采用主键 id 升序**游标分批遍历**（WHERE id>last_id ORDER BY id ASC LIMIT batch），
            id 自增且不变，天然保证「无遗漏、无重复」，彻底替代原先一次性 LIMIT 拉取；
          - 批内并发抓取 + 串行落库，批后即释放任务/结果内存，避免 O(N) 内存溢出；
          - 断点续传：游标与进度落盘到 STATE_DIR/update_state.json，中断后重跑可续传；
          - 支持 limit 控制，处理完即停止（游标推进保证不同资源被处理，非固定头部）。

        参数：
          mode          'full'=全量更新；'incremental'=增量更新（仅更新 since_hours 内变更的资源）
          since_hours   增量窗口（小时），仅 mode='incremental' 时生效
          resource_ids  指定更新 ba_film.id 列表（None=全部，按筛选条件）
          source_urls   指定更新 source_url 列表（None=全部）
          status_filter 资源状态筛选（'1'=正常，'0'=下架等）
          types_filter  类型筛选（movie/tv/...）
          limit         最多处理条数（0=不限制）
          workers       并发线程数（0=取配置）
          retry_max     单资源最大重试次数（默认 3）
          batch_size    单批处理条数（默认 UPDATE_BATCH_SIZE，控制内存峰值）
          resume        是否启用断点续传（默认 True；resume=False 从头开始）
          dry_run       试跑模式（默认 False）：只抓取/解析/比对，**跳过全部数据库写操作**，
                        仅统计「将变更的字段」；同时不下载图片、不写断点游标、不写失败清单
        """
        workers = workers or self._cfg.spider.concurrency
        start = time.monotonic()

        # dry-run 必须是纯只读：忽略既有断点游标（从 0 开始完整比对），且全程不落盘
        if dry_run:
            resume = False
            logger.info("DRY-RUN 模式：仅抓取/解析/比对，不写库、不下载图片、不写断点游标")

        # 1) 断点续传：读取上一轮已推进的主键游标
        cursor_state = self._load_cursor(mode) if resume else {}
        last_id = cursor_state.get("last_id", 0) if resume else 0
        logger.info("资源更新启动 | mode=%s | dry_run=%s | 断点游标 last_id=%d | workers=%d | batch=%d",
                    mode, dry_run, last_id, workers, batch_size)

        report = UpdateReport(total=0, mode=mode, dry_run=dry_run)
        processed = 0          # 已处理条数（含成功/失败），用于 limit 控制
        total_retried = 0
        done = False           # 遍历完成标记（批为空即结束）

        # 2) 主键游标分批遍历：保证完整覆盖全部资源，无遗漏、无重复、可断点续传
        while not done and (limit <= 0 or processed < limit):
            # 本批最多处理条数（受 limit 剩余额度约束）
            batch_limit = batch_size
            if limit > 0:
                batch_limit = min(batch_size, limit - processed)

            tasks = self._build_tasks(
                mode=mode, since_hours=since_hours,
                resource_ids=resource_ids, source_urls=source_urls,
                status_filter=status_filter, types_filter=types_filter,
                limit=batch_limit, after_id=last_id,
            )
            if not tasks:
                done = True
                break

            report.total += len(tasks)
            # 处理本批（并发抓取 + 串行落库），返回 (本批结果, 本批最大id, 本批重试数)
            results, batch_max_id, batch_retried = self._process_batch(
                tasks, workers, retry_max, dry_run=dry_run)
            total_retried += batch_retried
            self._accumulate(report, results)

            # 推进游标：以本批最大 id 为下一批起点（id 升序，天然不重复）
            last_id = batch_max_id
            processed += len(tasks)
            # 进度落盘（断点续传关键：每批处理完即持久化游标与统计；dry-run 不落盘）
            if resume:
                self._save_cursor(mode, last_id, processed, report)
            logger.info("批次完成 | 本批=%d | 游标推进 id=%d | 已处理=%d/%s",
                        len(tasks), last_id, processed,
                        "全部" if limit <= 0 else str(limit))

        report.elapsed_ms = int((time.monotonic() - start) * 1000)
        report.retried = total_retried

        logger.info("【资源更新完成】dry_run=%s total=%d updated=%d unchanged=%d failed=%d "
                    "retried=%d changed_fields=%d 耗时=%.2fs",
                    dry_run, report.total, report.updated, report.unchanged,
                    report.failed, report.retried, report.changed_fields_total,
                    report.elapsed_ms / 1000)

        # 3) 失败清单落盘（供排障；dry-run 不落盘，避免污染正式失败清单）
        if not dry_run:
            self._dump_failed(report)
        # 4) 完成清理：遍历结束清空断点游标（dry-run 不做任何落盘/清理）
        if done and not dry_run:
            self._clear_cursor(mode)
        self._db.close()
        return report

    # ---------------- 批处理 ----------------
    def _process_batch(self, tasks: List[UpdateTask], workers: int,
                       retry_max: int, dry_run: bool = False
                       ) -> tuple[List[UpdateResult], int, int]:
        """并发抓取 + 串行落库单批任务。

        返回 (本批结果列表, 本批最大 resource_id, 本批累计重试数)。
        批内结果列表在批结束后即被丢弃（外层只累计统计），避免内存堆积。
        """
        results: List[UpdateResult] = []
        batch_max_id = 0
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futs = {ex.submit(self._update_one, t, retry_max, dry_run): t for t in tasks}
            for f in as_completed(futs):
                task = futs[f]
                try:
                    r = f.result()
                    results.append(r)
                except Exception:
                    logger.exception("更新任务异常 %s", task.source_url)
                    r = UpdateResult(task=task, action="failed", error="内部异常")
                    results.append(r)
                batch_max_id = max(batch_max_id, task.resource_id)
                # 已用尽重试且失败 -> 记录错误日志（需求：重试失败记录错误日志并标记失败）
                if r.action == "failed":
                    logger.error("资源更新最终失败 id=%s 源=%s 重试=%d 错误=%s",
                                 r.task.resource_id, r.task.source_url,
                                 r.task.retry, r.error)
        total_retried = sum(r.task.retry for r in results)
        return results, batch_max_id, total_retried

    def _accumulate(self, report: UpdateReport, results: List[UpdateResult]):
        """将单批结果累计到报告（仅统计，不保留批内明细，控制内存）。"""
        for r in results:
            if r.action == "updated":
                report.success += 1
                report.updated += 1
            elif r.action == "unchanged":
                report.success += 1
                report.unchanged += 1
            else:
                report.failed += 1
                report.failed_items.append({
                    "resource_id": r.task.resource_id,
                    "source_url": r.task.source_url,
                    "title": r.task.title,
                    "error": r.error,
                })
            # dry-run：累计「将变更字段」数量，并保留少量明细（最多 100 条）
            if r.dry_run:
                report.changed_fields_total += r.changed_fields
                if r.action == "updated" and len(report.preview_items) < 100:
                    report.preview_items.append({
                        "resource_id": r.task.resource_id,
                        "source_url": r.task.source_url,
                        "title": r.task.title,
                        "changed_fields": r.changed_field_names,
                        "diffs": r.preview,
                    })

    # ---------------- 断点续传：游标状态持久化 ----------------
    def _state_path(self) -> Path:
        """断点游标状态文件路径（STATE_DIR/update_state.json）。"""
        state_dir = (self._cfg.root / self._cfg.spider.state_dir).resolve() \
            if not Path(self._cfg.spider.state_dir).is_absolute() \
            else Path(self._cfg.spider.state_dir)
        return state_dir / UPDATE_STATE_FILE

    def _load_cursor(self, mode: str) -> Dict:
        """读取断点游标。返回 {} 表示无有效断点（从头开始）。

        按 mode 分键存储，full / incremental 互不干扰（两种模式可独立运行）。
        """
        try:
            path = self._state_path()
            if not path.is_file():
                return {}
            data = json.loads(path.read_text(encoding="utf-8"))
            cursor = data.get(mode) or {}
            return cursor if isinstance(cursor, dict) else {}
        except (OSError, json.JSONDecodeError):
            logger.warning("断点游标文件读取失败，从头开始")
            return {}

    def _save_cursor(self, mode: str, last_id: int, processed: int,
                     report: UpdateReport):
        """持久化断点游标（每批处理完调用），供中断后续传。"""
        try:
            path = self._state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            if path.is_file():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    data = {}
            data.setdefault("mode", mode)
            data["mode"] = mode
            data[mode] = {
                "last_id": last_id,               # 下一批起点（id > last_id）
                "processed": processed,           # 已处理条数
                "updated": report.updated,
                "unchanged": report.unchanged,
                "failed": report.failed,
                "at": int(time.time()),           # 最近一次进度时间戳
            }
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            logger.info("断点游标已落盘 %s (mode=%s last_id=%d)", path, mode, last_id)
        except OSError as e:
            logger.error("断点游标落盘失败: %s", e)

    def _clear_cursor(self, mode: str):
        """遍历完成后清理断点游标，避免下次重跑重复/空跑。"""
        try:
            path = self._state_path()
            if not path.is_file():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            if mode in data:
                data.pop(mode, None)
            if not data or (set(data.keys()) == {"mode"}):
                # 无剩余有效游标，删除状态文件
                path.unlink(missing_ok=True)
                logger.info("断点游标已清空（全部遍历完成）")
            else:
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("断点游标清理失败: %s", e)

    # ---------------- 任务队列构建 ----------------
    def _build_tasks(self, *, mode: str, since_hours: int,
                     resource_ids: Optional[List[int]], source_urls: Optional[List[str]],
                     status_filter: Optional[str], types_filter: Optional[List[str]],
                     limit: int, after_id: int = 0) -> List[UpdateTask]:
        """从数据库分批拉取待更新资源，按 resource_id/source_url 分组去重，避免重复处理。

        修复要点（全量覆盖全部资源）：
          - 以主键 id 为游标（WHERE id > after_id ORDER BY id ASC LIMIT limit），
            id 自增且不变，保证遍历**无遗漏、无重复**、可断点续传；
          - 每批仅拉取 limit 条（UPDATE_BATCH_SIZE 由调用方控制），内存受限。

        返回按 id 升序的任务队列（每批最多 limit 条）。
        """
        where = []
        params: List = []
        if resource_ids:
            ids = [int(x) for x in resource_ids]
            if not ids:
                return []
            where.append(f"`id` IN ({','.join(['%s'] * len(ids))})")
            params += ids
        if source_urls:
            urls = [u for u in source_urls if u]
            if not urls:
                return []
            where.append(f"`source_url` IN ({','.join(['%s'] * len(urls))})")
            params += urls
        if status_filter is not None:
            where.append("`status`=%s")
            params.append(status_filter)
        if types_filter:
            where.append(f"`type` IN ({','.join(['%s'] * len(types_filter))})")
            params += types_filter
        # 主键游标：仅取 id > after_id 的资源（本批起点，配合 ORDER BY id ASC 无遗漏/无重复）
        if after_id:
            where.append("`id` > %s")
            params.append(int(after_id))

        # 增量：仅更新 update_time 早于截止时刻的资源（旧数据）
        # 单位容错：库内存在外部写入的 10 位秒级 update_time，SQL 侧统一归一为
        # 毫秒后比较（见 timeutil.timestamp_ms_sql_expr），避免秒级值被误判为极旧。
        if mode == "incremental" and since_hours > 0:
            cutoff_ms = int((datetime.now() - timedelta(hours=since_hours)).timestamp() * 1000)
            where.append(f"{timestamp_ms_sql_expr('update_time')} < %s")
            params.append(cutoff_ms)

        sql = (f"SELECT `id`,`source_url`,`title`,`type`,`status`,`update_time` "
               f"FROM `{T_FILM}` "
               f"WHERE `source_url` IS NOT NULL AND `source_url`<>''")
        if where:
            sql += " AND " + " AND ".join(where)
        # 以主键 id 升序作为遍历基准（保证完整覆盖、可续传），非 update_time
        sql += " ORDER BY `id` ASC"
        if limit:
            sql += f" LIMIT {int(limit)}"

        rows = self._db.fetch_all(sql, tuple(params))
        # 按 resource_id + source_url 去重（需求：分组整理，避免重复处理）
        tasks: List[UpdateTask] = []
        seen: set = set()
        for r in rows:
            key = (r["id"], r["source_url"])
            if key in seen:
                continue
            seen.add(key)
            tasks.append(UpdateTask(
                resource_id=r["id"],
                source_url=r["source_url"],
                title=r["title"] or "",
                film_type=r.get("type") or "movie",
                status=str(r.get("status") or "1"),
                # 库内 update_time 可能为秒级（外部写入），统一归一为毫秒后再放入任务
                update_time=normalize_timestamp_ms(r.get("update_time")),
            ))
        logger.info("任务队列构建完成：候选=%d 去重后=%d 游标>%d",
                    len(rows), len(tasks), after_id)
        return tasks

    # ---------------- 单资源更新 ----------------
    def _update_one(self, task: UpdateTask, retry_max: int,
                    dry_run: bool = False) -> UpdateResult:
        """单条资源：爬取 -> 校验 -> 变更更新；失败自动重试（最多 retry_max 次）。

        dry_run=True 时只调用 Pipeline.preview_changed 做只读比对，
        不执行 save_changed（因此不会产生任何库写入）。
        """
        last_err = ""
        for attempt in range(retry_max + 1):
            task.retry = attempt
            try:
                film = self._fetch_full(task.source_url, task.film_type)
                if film is None:
                    raise ValueError("详情解析失败/空结果")

                # 图片下载（封面/缩略/头图；失败置空，不写原图 URL）
                # dry-run 跳过：图片落盘属于副作用，且会改变 cover 字段影响比对结果
                if not dry_run:
                    rel = self._img.download(
                        film.cover_original, title=film.title,
                        site_id=film.site_id, source_url=film.source_url,
                    )
                    if rel:
                        film.cover = film.thumb = film.banner = rel
                    else:
                        film.cover = film.thumb = film.banner = ""

                # 提取播放直链（复用 crawler 逻辑；仅网络读取，无写库）
                if self._cfg.mofa.extract_play_url:
                    self._enrich_play(film)
                else:
                    for src in film.sources:
                        for ep in src.episodes:
                            ep.play_url = ep.view_url

                if dry_run:
                    # 只读比对：不写库，仅统计「将会变更」的字段。
                    # 必须加锁：DB 为单连接（PyMySQL 非线程安全），并发线程共用
                    # 同一连接会导致协议错乱（典型报错 MySQL server has gone away）。
                    with self._db_lock:
                        _, action, diffs, src_changed = self._pipeline.preview_changed(film)
                    if action == "not_found":
                        raise ValueError("库内不存在该 source_url")
                    names = [d["field"] for d in diffs]
                    if src_changed:
                        names.append("<play_sources>")
                    return UpdateResult(
                        task=task,
                        action="updated" if action == "update" else "unchanged",
                        changed_fields=len(names),
                        changed_field_names=names,
                        preview=diffs,
                        dry_run=True,
                    )

                # 变更字段更新（原子：单事务 + 行锁，仅更新变更字段）
                with self._db_lock:
                    _, action = self._pipeline.save_changed(film)
                # 统一动作枚举：pipeline 返回 'update'，报告口径使用 'updated'
                action = "updated" if action == "update" else action
                return UpdateResult(task=task, action=action,
                                    changed_fields=0 if action == "unchanged" else -1)
            except Exception as e:
                last_err = str(e)
                logger.warning("资源更新失败 尝试 %d/%d id=%s 源=%s: %s",
                               attempt + 1, retry_max + 1, task.resource_id,
                               task.source_url, e)
                if attempt < retry_max:
                    # 退避后重试
                    time.sleep(min(UPDATE_RETRY_BACKOFF * (2 ** attempt), 30))
        return UpdateResult(task=task, action="failed", error=last_err,
                            dry_run=dry_run)

    def _fetch_full(self, source_url: str, default_type: str = "movie") -> Optional[Film]:
        """按 source_url 精准爬取该资源最新全量信息（详情页解析）。

        从 URL 提取站点数字 id，保证命中目标资源，避免爬取到无关内容。
        default_type 用库内已有类型，保证详情字段精准匹配。
        """
        # 解析 site_id（/dt/{id}.html）
        m = re.search(r"/dt/(\d+)\.html", source_url)
        site_id = m.group(1) if m else source_url

        # 详情页重试（复用站点配置的详情重试次数）
        detail_url = self._cfg.mofa.resolve(f"/gangju/news/dt/{site_id}.html")
        film = None
        for attempt in range(1, self._cfg.mofa.detail_retry_times + 1):
            ok, html, status, _ = self._http.get(detail_url, referer=self._cfg.mofa.base_url)
            if ok:
                film = parse_detail(html, self._cfg.mofa.base_url, site_id, default_type)
                if film:
                    film.source_url = source_url  # 强制使用库内去重主键
                    break
            logger.warning("详情解析失败/空 %s (尝试 %d)", detail_url, attempt)
        return film

    def _enrich_play(self, film: Film):
        """提取播放直链（复用 crawler._enrich_play 的逻辑）。"""
        max_ep = self._cfg.mofa.max_episodes_per_film
        ep_count = 0
        for src in film.sources:
            for ep in src.episodes:
                if max_ep and ep_count >= max_ep:
                    break
                ep_count += 1
                ep.play_url = ep.view_url
                ok, html, status, _ = self._http.get(ep.view_url, referer=self._cfg.mofa.base_url)
                if not ok:
                    continue
                url, from_flag = parse_view(html)
                if from_flag and not src.from_flag:
                    src.from_flag = from_flag
                if url:
                    ep.play_url = url
                    ep.play_url_backup = ep.view_url

    # ---------------- 失败清单落盘 ----------------
    def _dump_failed(self, report: UpdateReport):
        if not report.failed_items:
            return
        try:
            state_dir = (self._cfg.root / self._cfg.spider.state_dir).resolve()
            state_dir.mkdir(parents=True, exist_ok=True)
            path = state_dir / "update_failed.json"
            path.write_text(json.dumps(report.failed_items, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            logger.warning("更新失败清单已落盘 %s（%d 条）", path, len(report.failed_items))
        except OSError as e:
            logger.error("失败清单落盘失败: %s", e)
