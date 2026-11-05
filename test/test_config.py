"""config 模块单元测试：.env 加载、分段取值、代理池/开关解析、默认图片路径、缺失文件报错。"""
from __future__ import annotations

from pathlib import Path

import pytest

from mofaspider.config import Config, ConfigError

_ENV = """[DATABASE]
HOSTNAME=127.0.0.1
DATABASE=film_cms
USERNAME=root
PASSWORD=root
HOSTPORT=3306

[SPIDER]
SPIDER_PROXY_POOL=http://a:1, http://b:2
VERIFY_SSL=true
ADAPTIVE_INTERVAL=false

[MOFAGUO]
BASE_URL=https://www.mofaguo.cn
CATEGORIES=movie:1,tv:2
CATEGORY_ID_MAP=movie:1,tv:2
"""


def _write(path: Path, text: str):
    path.write_text(text, encoding="utf-8")


def test_load_full(tmp_path):
    p = tmp_path / ".env"
    _write(p, _ENV)
    cfg = Config.load(str(p))
    assert cfg.db.host == "127.0.0.1"
    assert cfg.db.database == "film_cms"
    assert cfg.spider.proxy_pool == ["http://a:1", "http://b:2"]
    assert cfg.spider.verify_ssl is True
    assert cfg.spider.adaptive_interval is False
    assert cfg.mofa.categories == {"movie": 1, "tv": 2}
    assert cfg.mofa.category_id_map == {"movie": 1, "tv": 2}


def test_missing_env_raises(tmp_path):
    with pytest.raises(ConfigError):
        Config.load(str(tmp_path / "nope.env"))


def test_image_storage_defaults(tmp_path):
    p = tmp_path / ".env"
    _write(p, """[DATABASE]
HOSTNAME=127.0.0.1
DATABASE=film_cms
USERNAME=root
PASSWORD=root
HOSTPORT=3306

[SPIDER]

[MOFAGUO]
BASE_URL=https://www.mofaguo.cn
""")
    cfg = Config.load(str(p))
    # 默认落盘目录指向 FilmCms 静态资源目录，公开前缀为 Web 可访问路径
    assert cfg.spider.image_base_dir.endswith("film-cover")
    assert cfg.spider.image_public_prefix == "/static/images/film-cover"
    assert cfg.spider.image_fallback_to_url is False
