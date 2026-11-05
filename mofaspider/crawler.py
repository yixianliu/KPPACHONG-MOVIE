"""爬虫编排：分类 -> 列表分页 -> 详情 -> 图片 -> 播放直链 -> 落库。

生产级特性：
  - 防封：HttpClient 统一处理 UA 轮换/代理/限速抖动/退避重试
  - 并发：线程池处理单影片（网络并行），数据库写入串行加锁（PyMySQL 非线程安全）
  - 去重：按 source_url + 更新日期幂等；增量按 SINCE_HOURS 时间窗跳过
  - 异常隔离：单影片失败仅记日志并计数，不中断整体
  - 资源释放：结束时关闭 DB 连接
"""
from __future__ import annotations

import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

from .config import Config
from .db import DB, T_FILM
from .http import HttpClient
from .images import ImageDownloader
from .logger import get_logger
from .models import Film, FilmSummary
from .parser import parse_detail, parse_listing, parse_view
from .pipeline import Pipeline
from .state import State

logger = get_logger()


class Stats:
    def __init__(self):
        self._lock = threading.Lock()
        self.vals = defaultdict(int)

    def inc(self, key: str, n: int = 1):
        with self._lock:
            self.vals[key] += n

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return dict(self.vals)


class Crawler:
    def __init__(self, cfg: Config, db: DB):
        self._cfg = cfg
        self._db = db
        self._http = HttpClient(cfg.spider)
        self._img = ImageDownloader(cfg.spider, self._http, cfg.root,
                                    enabled=cfg.spider.image_download)
        self._pipeline = Pipeline(db, cfg.mofa.category_id_map)
        self._state = State(cfg.spider.state_dir, cfg.root, cfg.mofa.since_hours)
        self._db_lock = threading.Lock()
        self._stats = Stats()
        self._limit = 0
        self._processed_total = 0
        # 数据库中真实存在的 source_url 集合（非空），用于幂等真实性校验，
        # 防止状态文件与库脱节（状态有记录但库无数据）导致永久跳过。
        self._db_seen = set()
        # 封面缺失（cover 为空）的 source_url 集合：上次封面写盘失败（如杀软扫描锁定
        # errno13）时封面被置空，但影片已落库。增量跳过仅按 source_url+更新日期判断，
        # 不检查封面，会导致这些影片被永久跳过、封面永远补不上。故加载该集合，
        # 跳过判断时命中即视为"需补封面"，继续处理而非跳过。
        self._db_cover_missing: set[str] = set()

    # ---------------- 入口 ----------------
    def run(self, types_filter: Optional[List[str]] = None,
            max_pages: int = 0, workers: Optional[int] = None,
            force: bool = False, max_episodes: int = 0,
            limit: int = 0) -> Dict[str, int]:
        workers = workers or self._cfg.spider.concurrency
        max_pages = max_pages or self._cfg.spider.max_pages
        max_ep = max_episodes or self._cfg.mofa.max_episodes_per_film
        extract = self._cfg.mofa.extract_play_url
        self._limit = limit

        # 载入数据库真实存在的 source_url（非空）集合，用于幂等真实性校验，
        # 避免状态文件残留（库中无对应记录）导致影片被永久误跳过。
        try:
            rows = self._db.fetch_all(
                f"SELECT `source_url` FROM `{T_FILM}` "
                f"WHERE `source_url` IS NOT NULL AND `source_url`<>''")
            self._db_seen = {r["source_url"] for r in rows}
            logger.info("已载入库内 source_url 基线 %d 条（幂等真实性校验）", len(self._db_seen))
        except Exception:
            logger.warning("载入 source_url 基线失败，跳过库真实性校验", exc_info=True)
            self._db_seen = set()

        # 载入封面缺失（cover 为空或空串）的 source_url 集合，用于补封面。
        # 上次封面写盘失败（杀软扫描锁定 errno13）时封面置空但影片已落库，
        # 增量跳过不检查封面会永久跳过这些影片，故加载并命中时继续处理。
        try:
            rows = self._db.fetch_all(
                f"SELECT `source_url` FROM `{T_FILM}` "
                f"WHERE `source_url` IS NOT NULL AND `source_url`<>'' "
                f"AND (`cover` IS NULL OR `cover`='')")
            self._db_cover_missing = {r["source_url"] for r in rows}
            logger.info("已载入库内封面缺失基线 %d 条（补封面重抓）", len(self._db_cover_missing))
        except Exception:
            logger.warning("载入封面缺失基线失败，补封面机制停用", exc_info=True)
            self._db_cover_missing = set()

        cats = self._cfg.mofa.categories
        if types_filter:
            cats = {t: cats[t] for t in types_filter if t in cats}

        global_seen = set()
        try:
            for ltype, stype in cats.items():
                self._crawl_category(ltype, stype, max_pages, workers,
                                     force, max_ep, extract, global_seen)
                if self._limit and self._processed_total >= self._limit:
                    break
        finally:
            self._state.flush()
            self._db.close()

        snap = self._stats.snapshot()
        logger.info("【本轮完成】类别=%d 处理=%d 新增=%d 更新=%d 跳过=%d 失败=%d",
                    len(cats), snap.get("processed", 0), snap.get("insert", 0),
                    snap.get("update", 0), snap.get("skipped", 0),
                    snap.get("failed", 0))
        return snap

    # ---------------- 分类 ----------------
    def _crawl_category(self, ltype, stype, max_pages, workers, force,
                        max_ep, extract, global_seen):
        logger.info("=== 开始分类 type=%s site_type=%s ===", ltype, stype)
        page = 1
        # 线程池在分类内复用（而非每页重建），降低线程创建/销毁开销
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            while True:
                if self._limit and self._processed_total >= self._limit:
                    break
                if max_pages and page > max_pages:
                    break
                url = (self._cfg.mofa.list_url_tpl.format(type=stype)
                       if page == 1 else
                       self._cfg.mofa.list_url_page_tpl.format(type=stype, page=page))
                full = self._cfg.mofa.resolve(url)
                ok, html, status, _ = self._http.get(full, referer=self._cfg.mofa.base_url)
                if not ok:
                    logger.error("列表页失败 %s status=%s，停止该分类", full, status)
                    break
                summaries = parse_listing(html, self._cfg.mofa.base_url)
                new = [s for s in summaries if s.site_id not in global_seen]
                if not new:
                    logger.info("分类 %s 第 %d 页无新影片，结束翻页", ltype, page)
                    break
                # 全局 limit 截断
                if self._limit:
                    remaining = self._limit - self._processed_total
                    if remaining <= 0:
                        break
                    new = new[:remaining]
                for s in new:
                    global_seen.add(s.site_id)
                self._processed_total += len(new)

                self._stats.inc("pages")
                self._stats.inc("listed", len(new))
                futs = [ex.submit(self._process_film, s, ltype, force, max_ep, extract)
                        for s in new]
                for f in as_completed(futs):
                    try:
                        f.result()
                    except Exception:
                        logger.exception("影片处理任务异常")
                        self._stats.inc("failed")
                        self._stats.inc("processed")
                page += 1

    # ---------------- 单影片 ----------------
    def _process_film(self, s: FilmSummary, ltype: str, force: bool,
                      max_ep: int, extract: bool):
        detail_url = self._cfg.mofa.resolve(s.detail_path)
        film = None
        for attempt in range(1, self._cfg.mofa.detail_retry_times + 1):
            ok, html, status, _ = self._http.get(detail_url, referer=self._cfg.mofa.base_url)
            if ok:
                film = parse_detail(html, self._cfg.mofa.base_url, s.site_id, ltype)
                if film:
                    break
            logger.warning("详情解析失败/空 %s (尝试 %d)", detail_url, attempt)
        if not film:
            logger.error("详情最终失败 %s", detail_url)
            self._stats.inc("failed")
            self._stats.inc("processed")
            return

        # 增量/幂等跳过
        if not force:
            # 库真实性校验：状态文件记录了该影片，但库中无对应 source_url 记录，
            # 说明状态与库脱节（例如库被清空/重建），必须重新落库而非误跳过。
            in_db = film.source_url in self._db_seen
            if not in_db:
                logger.info("状态记录但库中不存在，重抓 %s", film.source_url)
            # 封面缺失补抓：上次封面写盘失败（杀软扫描锁定 errno13）时封面置空，
            # 但影片已落库；若按 is_unchanged 直接跳过，封面将永远补不上。
            # 命中封面缺失集合时视为"需补封面"，继续处理（不跳过），落库时重写封面。
            elif film.source_url in self._db_cover_missing:
                logger.info("封面缺失，重抓补封面 %s", film.source_url)
            elif self._state.is_unchanged(film.source_url, film.update_date):
                logger.debug("跳过未变更 %s", film.source_url)
                self._stats.inc("skipped")
                self._stats.inc("processed")
                return
            elif self._state.is_too_old(film.update_date):
                logger.debug("跳过超时间窗 %s 更新=%s", film.source_url, film.update_date)
                self._stats.inc("skipped")
                self._stats.inc("processed")
                return

        # 图片下载（封面/缩略/头图同源），按影片名称拼音统一命名落盘指定目录
        rel = self._img.download(
            film.cover_original,
            title=film.title,
            site_id=film.site_id,
            source_url=film.source_url,
        )
        if rel:
            film.cover = film.thumb = film.banner = rel
        else:
            # 下载失败 / 禁用：封面字段置空，绝不写入原图 URL 或临时路径（满足存储规范）
            film.cover = film.thumb = film.banner = ""
            if film.cover_original:
                logger.warning("封面未落盘，字段置空 标题=%s 源=%s",
                               film.title, film.source_url)

        # 提取播放直链（未提取时以播放页地址兜底）
        if extract:
            self._enrich_play(film, max_ep)
        else:
            for src in film.sources:
                for ep in src.episodes:
                    ep.play_url = ep.view_url

        # 落库（串行）
        try:
            with self._db_lock:
                _, action = self._pipeline.save(film)
            self._state.mark(film.source_url, film.update_date)
            self._db_seen.add(film.source_url)
            # 维护封面缺失集合：封面成功则移除（后续不再重抓）；失败则保留，
            # 下次运行仍命中"封面缺失"继续补抓，直到封面真正落盘。
            if film.cover:
                self._db_cover_missing.discard(film.source_url)
            else:
                self._db_cover_missing.add(film.source_url)
            self._stats.inc(action)        # insert / update
            self._stats.inc("processed")
        except Exception:
            logger.exception("落库异常 %s", film.source_url)
            self._stats.inc("failed")
            self._stats.inc("processed")

    def _enrich_play(self, film: Film, max_ep: int):
        ep_count = 0
        for src in film.sources:
            for ep in src.episodes:
                if max_ep and ep_count >= max_ep:
                    break
                ep_count += 1
                ep.play_url = ep.view_url            # 兜底：至少保留播放页地址
                ok, html, status, _ = self._http.get(ep.view_url, referer=self._cfg.mofa.base_url)
                if not ok:
                    continue
                url, from_flag = parse_view(html)
                if from_flag and not src.from_flag:
                    src.from_flag = from_flag
                if url:
                    ep.play_url = url
                    ep.play_url_backup = ep.view_url
