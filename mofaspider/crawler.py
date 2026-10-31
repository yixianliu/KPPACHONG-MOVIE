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
from .db import DB
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
            with ThreadPoolExecutor(max_workers=workers) as ex:
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
            if self._state.is_unchanged(film.source_url, film.update_date):
                logger.debug("跳过未变更 %s", film.source_url)
                self._stats.inc("skipped")
                self._stats.inc("processed")
                return
            if self._state.is_too_old(film.update_date):
                logger.debug("跳过超时间窗 %s 更新=%s", film.source_url, film.update_date)
                self._stats.inc("skipped")
                self._stats.inc("processed")
                return

        # 图片下载（封面/缩略/头图同源）
        if self._img._enabled and film.cover_original:
            rel = self._img.download(film.cover_original)
            if rel:
                film.cover = film.thumb = film.banner = rel
            else:
                film.cover = film.thumb = film.banner = film.cover_original
        else:
            film.cover = film.thumb = film.banner = film.cover_original

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
