"""图片下载器：把源站封面/海报下载到本地，写入字段为可公开访问的相对路径。

  - 落盘目录：配置 IMAGE_BASE_DIR（相对项目根或绝对）
  - 对外路径：IMAGE_PUBLIC_PREFIX + /<filename>
  - 校验 Content-Type 在白名单、体积 <= IMAGE_MAX_SIZE MB，否则跳过并返回原 URL
  - 失败不抛异常，返回空串（调用方决定回退为源站地址或留空）
"""
from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

from .config import SpiderConfig
from .http import HttpClient
from .logger import get_logger

logger = get_logger()

_EXT_BY_CT = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/gif": ".gif",
}
_DEFAULT_EXT = ".jpg"


class ImageDownloader:
    def __init__(self, spider_cfg: SpiderConfig, http: HttpClient,
                 root: Path, enabled: bool = True):
        self._cfg = spider_cfg
        self._http = http
        self._enabled = enabled
        self._root = root
        self._lock = threading.Lock()
        self._base_dir = (root / spider_cfg.image_base_dir).resolve() \
            if not os.path.isabs(spider_cfg.image_base_dir) \
            else Path(spider_cfg.image_base_dir)
        self._prefix = spider_cfg.image_public_prefix.rstrip("/")

    def _ensure_dir(self):
        with self._lock:
            self._base_dir.mkdir(parents=True, exist_ok=True)

    def download(self, url: str) -> str:
        """返回相对公开路径（如 /uploads/film/ab12cd.webp），失败返回 ''。"""
        if not self._enabled or not url:
            return ""
        ok, data, status, _ = self._http.get(url, is_text=False,
                                             timeout=self._cfg.image_timeout)
        if not ok or not data:
            logger.warning("图片下载失败 %s status=%s", url, status)
            return ""
        # 类型/大小校验
        ctype = self._guess_ctype(data)
        if ctype not in self._cfg.image_allowed_types:
            logger.warning("图片类型不在白名单 %s ctype=%s", url, ctype)
            return ""
        max_bytes = self._cfg.image_max_size_mb * 1024 * 1024
        if max_bytes and len(data) > max_bytes:
            logger.warning("图片过大 %s %dMB", url, len(data) // (1024 * 1024))
            return ""
        ext = _EXT_BY_CT.get(ctype, _DEFAULT_EXT)
        name = hashlib.md5(url.encode("utf-8")).hexdigest() + ext
        self._ensure_dir()
        path = self._base_dir / name
        try:
            with open(path, "wb") as f:
                f.write(data)
        except OSError as e:
            logger.error("图片写盘失败 %s: %s", path, e)
            return ""
        rel = f"{self._prefix}/{name}"
        logger.debug("图片已落盘 %s -> %s", url, rel)
        return rel

    @staticmethod
    def _guess_ctype(data: bytes) -> str:
        if data[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        if data[:8] == b"\x89\x50\x4e\x47":
            return "image/png"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return "image/gif"
        return _DEFAULT_EXT and "image/jpeg"
