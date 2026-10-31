#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""魔法国影视爬虫 命令行入口。

示例：
  # 全量抓取电影（前 2 页，每片最多提取 3 条分集直链，不下载图片）
  python run.py --types movie --pages 2 --max-episodes 3 --no-images

  # 抓取全部分类，启用图片与直链提取
  python run.py

  # 强制重抓（忽略增量幂等）
  python run.py --force

  # 仅采集最近 48 小时更新的影片
  python run.py --since-hours 48
"""
from __future__ import annotations

import argparse
import sys

from mofaspider.config import Config
from mofaspider.logger import get_logger
from mofaspider.db import DB
from mofaspider.crawler import Crawler


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mofa_spider", description="魔法国(mofaguo.cn)影视数据爬虫")
    p.add_argument("--env", default=None,
                   help=".env 路径（默认 <root>/.env）")
    p.add_argument("--types", default="",
                   help="仅抓取的分类，逗号分隔：movie,tv,variety,anime")
    p.add_argument("--pages", type=int, default=0,
                   help="每分类最大页数（0=全部）")
    p.add_argument("--workers", type=int, default=0,
                   help="并发线程数（0=取 .env SPIDER_CONCURRENCY）")
    p.add_argument("--max-episodes", type=int, default=0,
                   help="单片最多提取的分集直链数（0=全部）")
    p.add_argument("--limit", type=int, default=0,
                   help="每分类最多处理的影片数（0=不限制；便于测试与生产限流）")
    p.add_argument("--no-images", action="store_true", help="不下载图片")
    p.add_argument("--no-extract", action="store_true",
                   help="不深入 /view/ 提取直链，仅存播放页地址")
    p.add_argument("--force", action="store_true",
                   help="忽略增量幂等，强制重抓已存在且未变更的影片")
    p.add_argument("--since-hours", type=int, default=0,
                   help="仅处理更新时间在此小时数内的影片（0=不限制）")
    p.add_argument("--log-level", default="",
                   help="日志级别 DEBUG/INFO/WARNING/ERROR")
    return p


def main(argv=None) -> int:
    args = build_argparser().parse_args(argv)

    # 1) 配置（连接信息全部来自 .env）
    try:
        cfg = Config.load(args.env)
    except Exception as e:
        print(f"[FATAL] 配置加载失败: {e}", file=sys.stderr)
        return 2

    # 2) 日志
    log_level = args.log_level or cfg.spider.log_level
    logger = get_logger(level=log_level, log_dir=cfg.spider.log_dir, root=cfg.root)
    logger.info("爬虫启动 | DB=%s | 站点=%s", cfg.db.dsn, cfg.mofa.base_url)

    # 3) 命令行覆盖
    if args.no_images:
        cfg.spider.image_download = False
    if args.no_extract:
        cfg.mofa.extract_play_url = False
    if args.since_hours:
        cfg.mofa.since_hours = args.since_hours

    types_filter = [t.strip() for t in args.types.split(",") if t.strip()] or None

    # 4) 数据库 + 爬虫
    db = DB.from_config(cfg)
    try:
        crawler = Crawler(cfg, db)
        stats = crawler.run(
            types_filter=types_filter,
            max_pages=args.pages,
            workers=args.workers or None,
            force=args.force,
            max_episodes=args.max_episodes,
            limit=args.limit,
        )
    except KeyboardInterrupt:
        logger.warning("用户中断")
        return 130
    except Exception:
        logger.exception("爬虫运行异常终止")
        return 1

    print("\n===== 运行统计 =====")
    for k, v in stats.items():
        print(f"  {k:12s}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
