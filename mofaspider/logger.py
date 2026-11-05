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
    # 模块级 get_logger()（无 log_dir）可能先初始化并置位标记，
    # 此处若再传入 log_dir 且尚未挂文件 handler，应补挂而非短路返回，
    # 否则 run.py 的日志文件配置会被静默忽略，导致日志从不落盘。
    if getattr(logger, "_mofa_inited", False):
        if log_dir and not getattr(logger, "_mofa_has_file", False):
            lvl = logger.level
            formatter = logging.Formatter(_DEFAULT_FMT, _DATE_FMT)
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
            logger._mofa_has_file = True  # type: ignore
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
        logger._mofa_has_file = True  # type: ignore

    logger._mofa_inited = True  # type: ignore
    logger.propagate = False
    return logger
