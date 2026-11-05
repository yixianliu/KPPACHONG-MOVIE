#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""资源全量更新模式 命令行入口（独立于 run.py 的新增采集，互不影响）。

示例：
  # 全量更新全部库内影视资源（更新所有关联元数据字段）
  python run_update.py

  # 增量更新：仅刷新 update_time 超过 24 小时的旧资源
  python run_update.py --mode incremental --since-hours 24

  # 仅更新指定 ba_film.id 列表（精准定向更新）
  python run_update.py --ids 1001,1002,1003

  # 仅更新指定 source_url 列表（按去重主键定向）
  python run_update.py --source-urls https://www.mofaguo.cn/gangju/news/dt/1.html

  # 仅更新状态为正常(status=1)、类型为电影/电视剧 的资源，最多 50 条
  python run_update.py --status 1 --types movie,tv --limit 50

  # 指定并发与重试上限
  python run_update.py --workers 4 --retry 3

  # 试跑（dry-run）：只抓取/解析/比对，不写库、不下载图片、不写断点游标
  python run_update.py --mode full --limit 5 --dry-run
"""
from __future__ import annotations

import argparse
import sys

from mofaspider.config import Config
from mofaspider.logger import get_logger
from mofaspider.db import DB
from mofaspider.updater import Updater, UPDATE_BATCH_SIZE, UPDATE_MAX_RETRY


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mofa_updater",
        description="魔法国(mofaguo.cn)影视资源全量更新模式（更新已有资源关联元数据）")
    p.add_argument("--env", default=None, help=".env 路径（默认 <root>/.env）")
    # 更新范围控制
    p.add_argument("--mode", default="full", choices=["full", "incremental"],
                   help="更新子模式：full=全量更新；incremental=增量更新（默认 full）")
    p.add_argument("--since-hours", type=int, default=24,
                   help="增量窗口小时数（仅 --mode incremental 生效，默认 24）")
    p.add_argument("--ids", default="",
                   help="指定 ba_film.id 列表，逗号分隔（空=全部）")
    p.add_argument("--source-urls", default="",
                   help="指定 source_url 列表，逗号分隔（空=全部）")
    p.add_argument("--status", default="",
                   help="资源状态筛选：1=正常 0=下架（空=全部）")
    p.add_argument("--types", default="",
                   help="类型筛选，逗号分隔：movie,tv,variety,anime,documentary（空=全部）")
    p.add_argument("--limit", type=int, default=0,
                   help="最多处理条数（0=不限制，避免对库造成过大写入压力）")
    # 执行参数
    p.add_argument("--workers", type=int, default=0,
                   help="并发线程数（0=取 .env SPIDER_CONCURRENCY）")
    p.add_argument("--retry", type=int, default=UPDATE_MAX_RETRY,
                   help="单资源最大重试次数（默认 3）")
    p.add_argument("--batch-size", type=int, default=UPDATE_BATCH_SIZE,
                   help="每批处理条数（默认 200，控制内存峰值；0=使用默认值）")
    p.add_argument("--no-resume", action="store_true",
                   help="禁用断点续传（默认启用：中断后重跑从断点继续）")
    p.add_argument("--dry-run", action="store_true",
                   help="试跑模式：只抓取/解析/比对，不写库、不下载图片、不写断点游标，"
                        "仅统计将变更的字段（默认关闭）")
    p.add_argument("--no-images", action="store_true", help="不下载图片")
    p.add_argument("--log-level", default="", help="日志级别 DEBUG/INFO/WARNING/ERROR")
    return p


def main(argv=None) -> int:
    args = build_argparser().parse_args(argv)

    try:
        cfg = Config.load(args.env)
    except Exception as e:
        print(f"[FATAL] 配置加载失败: {e}", file=sys.stderr)
        return 2

    log_level = args.log_level or cfg.spider.log_level
    logger = get_logger(level=log_level, log_dir=cfg.spider.log_dir, root=cfg.root)
    logger.info("资源更新模式启动 | DB=%s | 站点=%s | mode=%s",
                cfg.db.dsn, cfg.mofa.base_url, args.mode)

    if args.no_images:
        cfg.spider.image_download = False

    # 解析 id / source_url 列表
    ids = [int(x.strip()) for x in args.ids.split(",") if x.strip()] or None
    urls = [x.strip() for x in args.source_urls.split(",") if x.strip()] or None
    types = [t.strip() for t in args.types.split(",") if t.strip()] or None
    status = args.status.strip() or None

    db = DB.from_config(cfg)
    try:
        updater = Updater(cfg, db)
        report = updater.run(
            mode=args.mode,
            since_hours=args.since_hours,
            resource_ids=ids,
            source_urls=urls,
            status_filter=status,
            types_filter=types,
            limit=args.limit,
            workers=args.workers or None,
            retry_max=args.retry,
            batch_size=args.batch_size or UPDATE_BATCH_SIZE,
            resume=not args.no_resume,
            dry_run=args.dry_run,
        )
    except KeyboardInterrupt:
        logger.warning("用户中断")
        return 130
    except Exception:
        logger.exception("资源更新异常终止")
        return 1

    d = report.to_dict()
    print("\n===== 资源更新报告 =====")
    print(f"  更新模式     : {d['mode']}{'（DRY-RUN 只比对不写库）' if d['dry_run'] else ''}")
    print(f"  待处理总数   : {d['total']}")
    print(f"  成功更新数   : {d['updated']}")
    print(f"  未变更(跳过) : {d['unchanged']}")
    print(f"  失败数       : {d['failed']}")
    print(f"  累计重试数   : {d['retried']}")
    if d["dry_run"]:
        print(f"  将变更字段数 : {d['changed_fields_total']}")
    print(f"  总耗时       : {d['elapsed_sec']}s")
    if d["dry_run"] and d["preview_items"]:
        print("  ---- 将变更明细（前 10 条）----")
        for it in d["preview_items"][:10]:
            print(f"    id={it['resource_id']} 标题={it['title']} 字段={it['changed_fields']}")
            for df in it["diffs"][:8]:
                print(f"        {df['field']}: {df['old']!r} -> {df['new']!r}")
    if d["failed_items"]:
        print("  ---- 失败清单（前 10 条）----")
        for it in d["failed_items"][:10]:
            print(f"    id={it['resource_id']} 源={it['source_url']} 错误={it['error']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
