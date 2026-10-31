"""配置中心：统一从项目根目录 .env 读取，禁止在代码中硬编码任何连接信息。

.env 采用标准 INI 分段格式，由 configparser 解析：
  [DATABASE]  -> 数据库连接参数（host/port/user/pass/db/charset）
  [SPIDER]    -> 网络限速/重试/图片下载/日志 参数
  [MOFAGUO]   -> 目标站点（魔法国）特定抓取策略
"""
from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List
from urllib.parse import urljoin


class ConfigError(Exception):
    """配置缺失或非法时抛出。"""


def project_root() -> Path:
    # 本文件位于 <root>/mofaspider/config.py，root 即上级目录
    return Path(__file__).resolve().parent.parent


def find_env_path(explicit: str | None = None) -> Path:
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_absolute():
            p = project_root() / p
        return p
    # 依次尝试 root/.env
    cand = project_root() / ".env"
    if cand.is_file():
        return cand
    raise ConfigError("未找到 .env，请通过 --env 指定路径")


class _Section:
    """对 configparser 某一段的安全取值封装。"""

    def __init__(self, parser: configparser.ConfigParser, section: str):
        if not parser.has_section(section):
            raise ConfigError(f".env 缺少段落 [{section}]")
        self._p = parser
        self._s = section

    def get(self, key: str, default: str | None = None) -> str:
        if self._p.has_option(self._s, key):
            return self._p.get(self._s, key).strip()
        if default is not None:
            return default
        raise ConfigError(f"[{self._s}] 缺少必需配置项 {key}")

    def get_int(self, key: str, default: int | None = None) -> int:
        return int(self.get(key, "" if default is None else str(default)))

    def get_float(self, key: str, default: float | None = None) -> float:
        return float(self.get(key, "" if default is None else str(default)))

    def get_bool(self, key: str, default: bool | None = None) -> bool:
        raw = self.get(key, "" if default is None else ("true" if default else "false"))
        return raw.lower() in ("1", "true", "yes", "on", "y")

    def get_list(self, key: str, default: str = "") -> List[str]:
        return [x.strip() for x in self.get(key, default).split(",") if x.strip()]


@dataclass
class DBConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    charset: str = "utf8mb4"
    debug: bool = False
    prefix: str = ""  # 仅记录，实际表名由代码常量给出

    @property
    def dsn(self) -> str:
        return f"mysql://{self.user}:***@{self.host}:{self.port}/{self.database}"


@dataclass
class SpiderConfig:
    page_size: int
    max_pages: int
    concurrency: int
    timeout: int
    retry_times: int
    retry_backoff: float
    request_interval: float
    user_agent: str
    proxy: str
    # image
    image_download: bool
    image_base_dir: str
    image_public_prefix: str
    image_timeout: int
    image_concurrency: int
    image_max_size_mb: int
    image_allowed_types: List[str]
    # log/state
    log_level: str
    log_dir: str
    state_dir: str


@dataclass
class MofaguoConfig:
    base_url: str
    path_prefix: str
    categories: Dict[str, int]          # type枚举 -> 站点 type 编号
    list_url_tpl: str
    list_url_page_tpl: str
    detail_url_tpl: str
    view_url_tpl: str
    extract_play_url: bool
    max_episodes_per_film: int
    detail_retry_times: int
    dedup_key: str
    since_hours: int
    category_id_map: Dict[str, int]      # type枚举 -> ba_film_category.id

    def resolve(self, path: str) -> str:
        """将站点相对地址（如 /gangju/news/1.html）解析为绝对地址。

        使用 urljoin，避免字符串拼接导致出现双 /gangju。
        """
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return urljoin(self.base_url, path)


@dataclass
class Config:
    db: DBConfig
    spider: SpiderConfig
    mofa: MofaguoConfig
    root: Path = field(default_factory=project_root)

    @classmethod
    def load(cls, env_path: str | None = None) -> "Config":
        path = find_env_path(env_path)
        parser = configparser.ConfigParser()
        # 保留键的原始大小写
        parser.optionxform = str  # type: ignore
        read = parser.read(str(path), encoding="utf-8")
        if not read:
            raise ConfigError(f"无法读取 .env: {path}")

        db_s = _Section(parser, "DATABASE")
        db = DBConfig(
            host=db_s.get("HOSTNAME"),
            port=db_s.get_int("HOSTPORT"),
            user=db_s.get("USERNAME"),
            password=db_s.get("PASSWORD"),
            database=db_s.get("DATABASE"),
            charset=db_s.get("CHARSET", "utf8mb4"),
            debug=db_s.get_bool("DEBUG", False),
            prefix=db_s.get("PREFIX", ""),
        )

        sp_s = _Section(parser, "SPIDER")
        spider = SpiderConfig(
            page_size=sp_s.get_int("SPIDER_PAGE_SIZE", 20),
            max_pages=sp_s.get_int("SPIDER_MAX_PAGES", 0),
            concurrency=sp_s.get_int("SPIDER_CONCURRENCY", 4),
            timeout=sp_s.get_int("SPIDER_REQUEST_TIMEOUT", 30),
            retry_times=sp_s.get_int("SPIDER_RETRY_TIMES", 3),
            retry_backoff=sp_s.get_float("SPIDER_RETRY_BACKOFF", 2.0),
            request_interval=sp_s.get_float("SPIDER_REQUEST_INTERVAL", 1.0),
            user_agent=sp_s.get("SPIDER_USER_AGENT",
                                 "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                 "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
            proxy=sp_s.get("SPIDER_PROXY", ""),
            image_download=sp_s.get_bool("IMAGE_DOWNLOAD", True),
            image_base_dir=sp_s.get("IMAGE_BASE_DIR", "storage/film_images"),
            image_public_prefix=sp_s.get("IMAGE_PUBLIC_PREFIX", "/uploads/film"),
            image_timeout=sp_s.get_int("IMAGE_TIMEOUT", 30),
            image_concurrency=sp_s.get_int("IMAGE_CONCURRENCY", 4),
            image_max_size_mb=sp_s.get_int("IMAGE_MAX_SIZE", 10),
            image_allowed_types=sp_s.get_list("IMAGE_ALLOWED_TYPES",
                                              "image/jpeg,image/png,image/webp,image/gif"),
            log_level=sp_s.get("LOG_LEVEL", "INFO"),
            log_dir=sp_s.get("LOG_DIR", "runtime/spider/log"),
            state_dir=sp_s.get("STATE_DIR", "runtime/spider/state"),
        )

        mf_s = _Section(parser, "MOFAGUO")
        categories = {}
        for pair in mf_s.get_list("CATEGORIES"):
            if ":" in pair:
                k, v = pair.split(":", 1)
                categories[k.strip()] = int(v.strip())
        if not categories:
            categories = {"movie": 1, "tv": 2, "variety": 3, "anime": 4}

        cid_map = {}
        for pair in mf_s.get_list("CATEGORY_ID_MAP"):
            if ":" in pair:
                k, v = pair.split(":", 1)
                cid_map[k.strip()] = int(v.strip())
        if not cid_map:
            cid_map = {"movie": 1, "tv": 2, "anime": 3, "documentary": 4, "variety": 5}

        mofa = MofaguoConfig(
            base_url=mf_s.get("BASE_URL").rstrip("/"),
            path_prefix=mf_s.get("PATH_PREFIX", "/gangju"),
            categories=categories,
            list_url_tpl=mf_s.get("LIST_URL_TPL", "/gangju/news/{type}.html"),
            list_url_page_tpl=mf_s.get("LIST_URL_PAGE_TPL", "/gangju/news/{type}-{page}.html"),
            detail_url_tpl=mf_s.get("DETAIL_URL_TPL", "/gangju/news/dt/{id}.html"),
            view_url_tpl=mf_s.get("VIEW_URL_TPL", "/gangju/view/{id}-{src}-{ep}.html"),
            extract_play_url=mf_s.get_bool("EXTRACT_PLAY_URL", True),
            max_episodes_per_film=mf_s.get_int("MAX_EPISODES_PER_FILM", 0),
            detail_retry_times=mf_s.get_int("DETAIL_RETRY_TIMES", 3),
            dedup_key=mf_s.get("DEDUP_KEY", "source_url"),
            since_hours=mf_s.get_int("SINCE_HOURS", 0),
            category_id_map=cid_map,
        )

        return cls(db=db, spider=spider, mofa=mofa)
