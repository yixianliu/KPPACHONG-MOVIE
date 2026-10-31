"""日志模块：同时输出到控制台与滚动文件，级别由配置决定。"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

_DEFAULT_FMT = "%(asctime)s [%(levelname)s] %(name)s %(filename)s:%(lineno)d | %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"


def get_logger(name: str = "mofa_spider", *, level: str = "INFO",
               log_dir: str | None = None, root: Path | None = None) -> logging.Logger:
    logger = logging.getLogger(name)
    if getattr(logger, "_mofa_inited", False):
        return logger

    lvl = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(lvl)
    formatter = logging.Formatter(_DEFAULT_FMT, _DATE_FMT)

    # 控制台
    ch = logging.StreamHandler()
    ch.setLevel(lvl)
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    # 文件（滚动，单文件 10MB，保留 5 个）
    if log_dir:
        base = root if root else Path.cwd()
        fpath = (base / log_dir).resolve()
        fpath.mkdir(parents=True, exist_ok=True)
        log_file = fpath / f"{name}.log"
        fh = RotatingFileHandler(
            str(log_file), maxBytes=10 * 1024 * 1024, backupCount=5,
            encoding="utf-8",
        )
        fh.setLevel(lvl)
        fh.setFormatter(formatter)
        logger.addHandler(fh)

    logger._mofa_inited = True  # type: ignore
    logger.propagate = False
    return logger
