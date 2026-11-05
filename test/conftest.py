"""pytest 公共 fixture：构造各类配置与桩对象，供各测试模块复用。

request_interval=0 以避免测试中触发真实限速睡眠。
"""
from __future__ import annotations

import pytest

from mofaspider.config import SpiderConfig


@pytest.fixture
def spider_cfg() -> SpiderConfig:
    return SpiderConfig(
        page_size=20,
        max_pages=0,
        concurrency=4,
        timeout=30,
        retry_times=3,
        retry_backoff=2.0,
        request_interval=0.0,          # 测试关闭限速
        user_agent="test-ua",
        proxy="",
        proxy_pool=[],
        verify_ssl=False,
        adaptive_interval=False,
        image_download=True,
        image_base_dir="tmp_film_images",
        image_public_prefix="/uploads/film",
        image_timeout=30,
        image_concurrency=4,
        image_max_size_mb=10,
        image_allowed_types=["image/jpeg", "image/png", "image/webp", "image/gif"],
        image_fallback_to_url=False,
        log_level="DEBUG",
        log_dir="runtime/spider/log",
        state_dir="runtime/spider/state",
    )
