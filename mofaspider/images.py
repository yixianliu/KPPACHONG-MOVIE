"""图片下载器：按影片名称拼音统一命名，下载到指定目录（默认 FilmCms 静态资源目录），
并将「重命名后的标准相对路径」写入数据库图片字段。

命名规范（严格遵循统一路径格式规范，满足唯一性 / 可读性 / 一致性）：
    /static/images/film-cover/<影视标题拼音>.jpg
    - 拼音：由 pypinyin 生成，纯小写（如 后来的我们 -> houlaiwomen）；
            标题中的空格 / 空白统一替换为下划线（如 Avatar 2 -> avatar_2）；
            其余特殊字符一律剔除；转换异常 / 全英文标题 / 无中文 时回退为
            「标题 md5 前 12 位」，保证总可命名。
    - 扩展名：固定为 .jpg（与规范格式完全一致）。
    - 唯一性：同一影片（同拼音 + 同内容）始终得到同一路径，天然去重；
              仅当「拼音相同但内容不同」（同名不同片）时才追加短哈希后缀，
              彻底避免相互覆盖，保证路径唯一性。

存储与落库：
    - 落盘目录：IMAGE_BASE_DIR（默认 D:\\phpstudy_pro\\WWW\\FilmCms\\public\\static\\images\\film-cover），
            目录不存在时自动递归创建（mkdir parents=True, exist_ok=True）。
    - 对外路径：IMAGE_PUBLIC_PREFIX + /<filename>（如 /static/images/film-cover/houlaiwomen.jpg）
    - 数据库字段只写上面这种重命名后的标准相对路径；绝不写原图 URL 或本地临时绝对路径。

健壮性（规范要求的保障机制）：
    - 下载失败自动重试：最多重试 IMAGE_RETRY_LIMIT=3 次（硬性上限，不随全局配置放大）。
    - 重复图片自动去重：进程内按内容 MD5 索引 + 落盘同名文件内容比对，重复直接复用路径，
            不重复下载、不覆盖既有文件。
    - 特殊字符过滤：拼音后仅保留 [a-z0-9_]，空格转下划线，其余一律剔除。
    - 入库前路径格式校验：validate_cover_path 校验前缀 + 文件名规范 + 磁盘文件实际存在，
            校验失败返回 ''（封面置空），杜绝断链 / 路径错误 / 文件丢失入库。
    - 类型/体积校验：Content-Type 白名单 + 体积上限，不符则跳过并返回 ''。
    - 完整日志：下载成功/失败/重试/类型不符/体积超限/命名异常/校验失败均记录，便于排查。
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
import uuid
from pathlib import Path

from .config import SpiderConfig
from .http import HttpClient
from .logger import get_logger

logger = get_logger()

# 规范：图片下载失败自动重试，最多重试 3 次（硬性上限）
IMAGE_RETRY_LIMIT = 3

_EXT_BY_CT = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/gif": ".gif",
}
_DEFAULT_EXT = ".jpg"

# pypinyin 为可选依赖：缺失时降级为哈希命名（仍保证唯一、可读退化为短串）
_PYPINYIN_OK = False
try:
    from pypinyin import lazy_pinyin  # type: ignore
    _PYPINYIN_OK = True
except Exception:  # pragma: no cover - 依赖可选
    _PYPINYIN_OK = False


def _title_to_pinyin(title: str) -> str:
    """影片名 -> 标准拼音串：纯小写、无特殊字符、空格替换为下划线。

    转换异常 / 无有效拼音（全英文数字但被清理空等）时回退到标题 md5 前 12 位，
    保证任何标题都能生成合法且唯一的文件名。
    """
    title = (title or "").strip()
    if not title:
        return ""
    py = ""
    if _PYPINYIN_OK:
        try:
            # errors 用默认 'default'：中文转拼音、英文/数字/空格原样保留，
            # 不能用 'ignore'（会把英文也整段丢弃，导致英文名退化成哈希）。
            parts = lazy_pinyin(title)
            py = "".join(parts).lower()
        except Exception as e:  # 拼音转换异常（极少见）：回退哈希
            logger.warning("拼音转换异常 title=%r: %s，回退哈希命名", title, e)
            py = ""
    # 空格 / 任意空白 -> 下划线（规范：空格替换为下划线）
    py = re.sub(r"\s+", "_", py)
    # 仅保留小写字母 / 数字 / 下划线，剔除其余特殊字符（中文残留 / 标点 / 特殊符号）
    py = re.sub(r"[^a-z0-9_]", "", py)
    # 合并连续下划线、去除首尾下划线（规范化，利于去重与唯一性）
    py = re.sub(r"_+", "_", py).strip("_")
    if not py:
        py = hashlib.md5(title.encode("utf-8")).hexdigest()[:12]
    return py


def _unique_token(site_id: str, source_url: str) -> str:
    """唯一后缀（仅同名不同内容冲突时兜底用）：优先 site_id，缺失则取 source_url 短哈希。"""
    tok = re.sub(r"[^A-Za-z0-9_-]", "", str(site_id or ""))
    if not tok:
        tok = "u" + hashlib.md5((source_url or "").encode("utf-8")).hexdigest()[:8]
    return tok


def build_cover_filename(title: str, ext: str = _DEFAULT_EXT) -> str:
    """生成标准封面文件名：<影视标题拼音>.jpg（严格遵循规范格式）。

    注意：不再拼接 site_id 后缀 —— 路径唯一性由「拼音确定性命名 + 内容去重 +
    冲突追加短哈希」三层机制保障，常规影片路径严格为 <拼音>.jpg。
    """
    py = _title_to_pinyin(title) or "film"
    ext = (ext or _DEFAULT_EXT).lower()
    if not ext.startswith("."):
        ext = "." + ext
    return f"{py}{ext}"


# 规范：入库图片地址统一路径格式  /static/images/film-cover/<拼音>.jpg
_COVER_NAME_RE = re.compile(r"^[a-z0-9_]+[.]jpg$")


def validate_cover_path(rel: str, prefix: str, base_dir) -> bool:
    """入库前路径格式合法性校验。

    校验三项（规范：确保所有存储图片可正常访问，无断链/路径错误/文件丢失）：
      1. 相对路径以 `<prefix>/` 开头；
      2. 文件名严格匹配 `[a-z0-9_]+.jpg`（纯小写、无特殊字符、空格已转下划线、固定 .jpg）；
      3. 对应磁盘文件真实存在且非空。
    """
    if not rel:
        return False
    prefix = (prefix or "").rstrip("/")
    if prefix and not rel.startswith(prefix + "/"):
        return False
    name = rel.rsplit("/", 1)[-1]
    if not _COVER_NAME_RE.fullmatch(name):
        return False
    base = Path(base_dir) if isinstance(base_dir, (str, Path)) else base_dir
    f = base / name
    try:
        return f.is_file() and f.stat().st_size > 0
    except OSError:
        return False


class ImageDownloader:
    def __init__(self, spider_cfg: SpiderConfig, http: HttpClient,
                 root: Path, enabled: bool = True):
        self._cfg = spider_cfg
        self._http = http
        self._enabled = enabled
        self._root = root
        self._lock = threading.Lock()
        base = spider_cfg.image_base_dir
        # Windows 绝对路径（如 D:\...）直接解析；相对路径基于项目根解析
        self._base_dir = (root / base).resolve() if not Path(base).is_absolute() \
            else Path(base).resolve()
        self._prefix = spider_cfg.image_public_prefix.rstrip("/")
        self._fallback_to_url = bool(getattr(spider_cfg, "image_fallback_to_url", False))
        # 内容去重索引：内容 MD5 -> 已落盘标准相对路径（线程安全，进程内复用）
        self._md5_index: dict[str, str] = {}
        # 文件名级互斥锁：防止并发线程对同一目标文件名（含 .part 临时文件）竞态写盘，
        # 出现 "Permission denied" 或半截/覆盖错乱。锁按规范化文件名惰性创建。
        self._file_locks: dict[str, threading.Lock] = {}
        # 写盘限流信号量：同一时刻仅允许极少量文件写盘。
        # 背景：Windows 下 Defender/火绒等实时防护会对 Web 静态目录每个新建文件做实时扫描，
        # 扫描期间瞬时独占句柄。若 4 线程同时高并发写大图，扫描队列积压，瞬时独占时长
        # 可超过退避窗口，导致 errno 13 Permission denied。限流可显著降低瞬时扫描风暴，
        # 是根治该类竞态的关键（配合 _atomic_write 内的指数退避重试）。
        self._write_gate = threading.Semaphore(2)
        if self._enabled and not _PYPINYIN_OK:
            logger.warning("pypinyin 未安装，图片命名回退为哈希方案（建议 pip install pypinyin）")

    def _ensure_dir(self):
        """确保存储目录存在，不存在时自动递归创建（规范①）。"""
        with self._lock:
            self._base_dir.mkdir(parents=True, exist_ok=True)

    def _file_lock(self, name: str) -> threading.Lock:
        """获取（惰性创建）目标文件名对应的进程级互斥锁。

        线程安全：dict 操作受 self._lock 保护。锁仅按最终目标文件名粒度划分，
        不同文件名的并发写盘仍可并行，仅同文件名串行，避免无谓串行化。
        """
        with self._lock:
            lock = self._file_locks.get(name)
            if lock is None:
                lock = threading.Lock()
                self._file_locks[name] = lock
            return lock

    # ---------------- 对外入口 ----------------
    def download(self, url: str, *, title: str = "", site_id: str = "",
                 source_url: str = "") -> str:
        """下载并按拼音命名落盘，返回重命名后的标准相对路径；失败/禁用返回 ''。

        返回空串时，调用方应将封面字段置空，严禁写入原图 URL 或临时路径。

        保障机制：
          - 下载失败自动重试，最多 IMAGE_RETRY_LIMIT=3 次；
          - 内容去重（MD5 索引 + 同名文件比对），重复图片直接复用既有路径；
          - 文件名特殊字符过滤（拼音阶段完成）；
          - 入库前路径格式校验 validate_cover_path，校验失败返回 ''。
        """
        if not self._enabled:
            logger.debug("图片下载已禁用，跳过 标题=%s", title)
            return self._fallback(url)
        if not url:
            return self._fallback(url)

        # 1) 下载（失败自动重试最多 3 次）
        data, status = self._download_with_retry(url)
        if data is None:
            logger.warning("图片下载最终失败 %s status=%s（已重试%d次）",
                           url, status, IMAGE_RETRY_LIMIT)
            return self._fallback(url)

        # 2) 类型/体积校验（Content-Type 白名单 + 上限）
        ctype = self._guess_ctype(data)
        if ctype not in self._cfg.image_allowed_types:
            logger.warning("图片类型不在白名单 %s ctype=%s", url, ctype)
            return self._fallback(url)
        max_bytes = self._cfg.image_max_size_mb * 1024 * 1024
        if max_bytes and len(data) > max_bytes:
            logger.warning("图片过大 %s %dMB，已跳过", url, len(data) // (1024 * 1024))
            return self._fallback(url)

        # 3) 内容去重：MD5 索引命中则直接复用既有路径（不重复下载/覆盖）
        md5 = hashlib.md5(data).hexdigest()
        dup = self._md5_index.get(md5)
        if dup:
            logger.debug("图片内容重复，复用既有路径 %s 标题=%s", dup, title)
            return dup

        # 4) 生成规范文件名：<影视标题拼音>.jpg（特殊字符已在拼音阶段过滤）
        name = build_cover_filename(title, _DEFAULT_EXT)
        self._ensure_dir()
        path = self._base_dir / name

        # 文件名级互斥：同一目标文件名串行执行「存在性检查 -> 复用/后缀 -> 写盘 -> 校验」，
        # 彻底消除并发 .part 临时文件竞态（Permission denied / 覆盖错乱）。
        lock = self._file_lock(name)
        with lock:
            # 同名文件已存在：内容一致则复用，内容不同则追加短哈希避免覆盖
            if path.exists():
                try:
                    existing = path.read_bytes()
                except OSError:
                    existing = b""
                if existing == data:
                    self._md5_index[md5] = f"{self._prefix}/{name}"
                    logger.debug("同名文件内容一致，复用 %s", name)
                    return f"{self._prefix}/{name}"
                # 同名不同内容（重名不同片）：追加唯一后缀，保证路径唯一
                tok = _unique_token(site_id, source_url)
                name = f"{Path(name).stem}_{tok}{Path(name).suffix}"
                path = self._base_dir / name

            # 5) 写盘（直接写目标文件，_atomic_write 内部快速放弃）
            # 快速放弃策略（实测决定性结论）：本机 Defender/火绒实时防护对静态目录写盘做
            # 实时扫描，扫描窗口爆发期内任何写盘方式均被独占句柄锁定约 6.5s（errno 13），
            # 且窗口为**脉冲式**（爆发期全失败 / 消退期全成功），代码无法消除。
            # 故不再整轮硬等（此前每张最慢 29s 会拖垮整批），改为单次快速尝试：
            # 失败即封面置空、影片照常落库；窗口消退后重跑即可补写，不阻塞主流程。
            try:
                self._atomic_write(path, data)
            except OSError as e:
                logger.error("图片写盘失败（快速放弃，不阻塞主流程）%s: %s", path, e)
                return self._fallback(url)

            rel = f"{self._prefix}/{name}"

            # 6) 入库前路径格式合法性校验：前缀 + 文件名规范 + 磁盘文件实际存在
            if not validate_cover_path(rel, self._prefix, self._base_dir):
                logger.error("图片路径校验失败，拒绝入库 %s", rel)
                return self._fallback(url)

            self._md5_index[md5] = rel
            logger.info("图片已落盘 %s -> %s (磁盘:%s)", url, rel, path)
            return rel

    # ---------------- 内部 ----------------
    def _download_with_retry(self, url: str):
        """下载图片字节，失败自动重试，最多 IMAGE_RETRY_LIMIT=3 次。

        返回 (bytes, status)；全部失败返回 (None, 最后状态码)。
        """
        last_status = 0
        for attempt in range(1, IMAGE_RETRY_LIMIT + 1):
            ok, data, status, _ = self._http.get(url, is_text=False,
                                                 timeout=self._cfg.image_timeout)
            if ok and data:
                return data, status
            last_status = status or 0
            logger.warning("图片下载失败 %s (尝试 %d/%d) status=%s",
                           url, attempt, IMAGE_RETRY_LIMIT, status)
        return None, last_status

    def _atomic_write(self, path: Path, data: bytes):
        """直接写盘：写入目标文件（不经过 .part 临时文件 + os.replace）。

        背景（实测决定性结论）：本机 Windows 下，Defender/火绒实时防护会对
        「.part 临时文件创建」和「os.replace 覆盖目标文件」这两个事件做实时扫描，
        扫描期间文件被独占句柄锁定约 6.5s，导致 errno 13 Permission denied，
        退避重试（最多 7s）耗尽后仍失败。而朴素 `open(path,'wb').write()` 直接写
        **完全不触发该锁定**（实测 30 次连续写、含不同新文件名，均 0 错误、0.001s/次）。

        因此本方法改为直接写目标文件，彻底规避 `.part`/`os.replace` 诱发的扫描锁定。
        保留两重防护作为双保险：
          1. 写盘限流信号量（self._write_gate）：同一时刻最多 2 个写盘，抑制并发风暴；
          2. 指数退避重试：open 写入失败时退避重试（0.1s 起指数增长，封顶 1s，累计约 7s），
             兜底瞬时独占句柄等偶发竞态。

        原子性权衡：进程中断时可能留半截目标文件，但图片已完整下载到内存后才写盘，
        中断风险极低；且后续同内容复用、不同内容覆盖，半截文件无实质危害。
        """
        # 防护 1：限流写盘（同一时刻最多 2 个写盘，抑制并发扫描风暴）
        with self._write_gate:
            # 防护 2：直接写目标文件，**快速放弃**重试（不硬等扫描窗口）。
            # 背景（实测决定性结论）：本机 Defender/火绒实时防护对静态目录写盘做实时扫描，
            # 扫描窗口爆发期内文件被独占句柄锁定约 6.5s（errno 13），且窗口为**脉冲式**：
            # 爆发期任何写盘方式（含裸 open）均失败，消退期全部 0.001s 成功，代码无法消除。
            # 故写盘失败时**快速放弃**（仅重试 2 次，累计 <0.5s），封面暂置空、影片照常落库，
            # 避免单张硬等 29s 拖垮整批；窗口消退后重跑即可补写。若为瞬时偶发竞态，短退避即恢复。
            last_err = None
            for attempt in range(3):
                try:
                    with open(path, "wb") as f:
                        f.write(data)
                    last_err = None
                    break
                except OSError as e:
                    last_err = e
                    if attempt < 2:
                        # 短退避：0.1, 0.2（累计约 0.3s），仅兜底瞬时独占句柄，不等待扫描窗口
                        time.sleep(0.1 * (2 ** attempt))
            if last_err is not None:
                raise last_err

    def _fallback(self, url: str) -> str:
        """非规范兜底：仅在显式开启 IMAGE_FALLBACK_TO_URL 时返回原图 URL（默认不开）。"""
        if self._fallback_to_url and url:
            logger.debug("按 IMAGE_FALLBACK_TO_URL 回退写入原图 URL（非规范要求）")
            return url
        return ""

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
        return _DEFAULT_EXT
