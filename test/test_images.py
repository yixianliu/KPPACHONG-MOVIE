"""images 模块单元测试：拼音命名、特殊字符、去重、重试、路径校验与回退。"""
from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

from mofaspider.images import (
    IMAGE_RETRY_LIMIT,
    ImageDownloader,
    build_cover_filename,
    validate_cover_path,
    _title_to_pinyin,
    _unique_token,
)


# ---------------- 拼音 / 命名规范 ----------------
def test_pinyin_chinese():
    assert _title_to_pinyin("后来的我们") == "houlaidewomen"


def test_pinyin_english_kept():
    # 英文应原样保留，而非被忽略退化成哈希
    assert _title_to_pinyin("Avatar") == "avatar"


def test_pinyin_space_to_underscore():
    # 规范：空格替换为下划线（纯小写）
    assert _title_to_pinyin("Avatar 2") == "avatar_2"


def test_pinyin_special_chars_stripped():
    assert _title_to_pinyin("【测试】特殊#字符*名称") == "ceshiteshuzifumingcheng"


def test_pinyin_multiple_spaces_collapsed():
    assert _title_to_pinyin("  A   B  ") == "a_b"


def test_pinyin_empty_returns_empty():
    assert _title_to_pinyin("") == ""


def test_filename_standard_format():
    # 规范格式：<影视标题拼音>.jpg（无 site_id 后缀）
    assert build_cover_filename("后来的我们") == "houlaidewomen.jpg"
    assert build_cover_filename("Avatar 2") == "avatar_2.jpg"


def test_filename_ext_normalized():
    assert build_cover_filename("x", "png") == "x.png"
    assert build_cover_filename("x", ".WEBP") == "x.webp"


def test_filename_empty_title_falls_back_to_film():
    assert build_cover_filename("") == "film.jpg"


def test_filename_deterministic_unique():
    # 同标题确定性，天然去重（重复图片同名同路径）
    assert build_cover_filename("后来的我们") == build_cover_filename("后来的我们")
    # 不同标题不同路径
    assert build_cover_filename("a") != build_cover_filename("b")


def test_unique_token_site_id():
    assert _unique_token("12345", "http://x/1") == "12345"


def test_unique_token_missing_site_id():
    tok = _unique_token("", "http://example.com/film/9")
    assert tok.startswith("u") and len(tok) == 9  # 'u' + 8 hex


# ---------------- 路径格式校验 ----------------
def test_validate_cover_path_ok(tmp_path):
    f = tmp_path / "houlaidewomen.jpg"
    f.write_bytes(b"img")
    assert validate_cover_path("/static/images/film-cover/houlaidewomen.jpg",
                               "/static/images/film-cover", tmp_path) is True


def test_validate_cover_path_wrong_prefix(tmp_path):
    f = tmp_path / "a.jpg"
    f.write_bytes(b"img")
    assert validate_cover_path("/other/a.jpg", "/static/images/film-cover", tmp_path) is False


def test_validate_cover_path_bad_name(tmp_path):
    f = tmp_path / "A_Up.jpg"  # 大写不符合规范
    f.write_bytes(b"img")
    assert validate_cover_path("/static/images/film-cover/A_Up.jpg",
                               "/static/images/film-cover", tmp_path) is False


def test_validate_cover_path_non_jpg(tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"img")
    assert validate_cover_path("/static/images/film-cover/a.png",
                               "/static/images/film-cover", tmp_path) is False


def test_validate_cover_path_missing_file(tmp_path):
    assert validate_cover_path("/static/images/film-cover/nofile.jpg",
                               "/static/images/film-cover", tmp_path) is False


def test_validate_cover_path_empty():
    assert validate_cover_path("", "/p", Path(".")) is False


# ---------------- 下载器 ----------------
def _mk_dl(tmp_path, enabled=True, retries=IMAGE_RETRY_LIMIT):
    cfg = mock.Mock()
    cfg.image_base_dir = str(tmp_path)
    cfg.image_public_prefix = "/static/images/film-cover"
    cfg.image_allowed_types = ["image/jpeg", "image/png", "image/webp", "image/gif"]
    cfg.image_max_size_mb = 10
    cfg.image_timeout = 1
    cfg.image_fallback_to_url = False
    http = mock.Mock()
    http.get.return_value = (True, b"\xff\xd8\xff\xe0" + b"x" * 20, 200, "")
    dl = ImageDownloader(cfg, http, Path(tmp_path), enabled=enabled)
    return dl, http


def test_download_success_and_dir_created(tmp_path):
    dl, _ = _mk_dl(tmp_path)
    # 目录不存在时自动递归创建
    rel = dl.download("http://x/a.jpg", title="后来的我们")
    assert rel == "/static/images/film-cover/houlaidewomen.jpg"
    assert (tmp_path / "houlaidewomen.jpg").is_file()
    assert validate_cover_path(rel, "/static/images/film-cover", tmp_path)


def test_download_empty_url_returns_empty(tmp_path):
    dl, _ = _mk_dl(tmp_path)
    assert dl.download("", title="T") == ""


def test_download_disabled_returns_empty(tmp_path):
    dl, _ = _mk_dl(tmp_path, enabled=False)
    assert dl.download("http://x/a.jpg", title="T") == ""


def test_download_retry_three_times(tmp_path):
    dl, http = _mk_dl(tmp_path)
    # 前两次失败，第三次成功 -> 应恰好请求 3 次
    http.get.side_effect = [
        (False, None, 0, ""),
        (False, None, 503, ""),
        (True, b"\xff\xd8\xff" + b"y" * 20, 200, ""),
    ]
    rel = dl.download("http://x/a.jpg", title="测试重试")
    assert rel == "/static/images/film-cover/ceshizhongshi.jpg"  # 重 -> zhong
    assert http.get.call_count == 3


def test_download_all_fail_returns_empty(tmp_path):
    dl, http = _mk_dl(tmp_path)
    http.get.return_value = (False, None, 0, "")
    assert dl.download("http://x/a.jpg", title="T") == ""
    assert http.get.call_count == IMAGE_RETRY_LIMIT  # 严格 3 次


def test_download_dedup_same_content(tmp_path):
    dl, _ = _mk_dl(tmp_path)
    r1 = dl.download("http://x/a.jpg", title="同片")
    r2 = dl.download("http://x/a.jpg", title="同片")
    assert r1 == r2 == "/static/images/film-cover/tongpian.jpg"
    assert dl._md5_index  # 内容索引已记录


def test_download_same_name_diff_content_gets_token(tmp_path):
    dl, _ = _mk_dl(tmp_path)
    # 两次调用内容不同（b 数据与 a 不同），才能触发同名不同内容的冲突分支
    # 手动设置 http 返回值序列：第一次成功，第二次返回不同内容
    dl._http.get.side_effect = [
        (True, b"\xff\xd8\xff" + b"a" * 20, 200, ""),
        (True, b"\xff\xd8\xff" + b"b" * 20, 200, ""),
    ]
    r1 = dl.download("http://x/a.jpg", title="同名", site_id="1")
    # 同名但内容不同 -> 追加唯一后缀，不覆盖
    r2 = dl.download("http://x/b.jpg", title="同名", site_id="2")
    assert r1 == "/static/images/film-cover/tongming.jpg"
    assert r2.startswith("/static/images/film-cover/tongming_2.jpg")
    assert r1 != r2


def test_download_wrong_type_returns_empty(tmp_path):
    dl, http = _mk_dl(tmp_path)
    http.get.return_value = (True, b"not an image at all", 200, "")
    assert dl.download("http://x/a.txt", title="T") == ""
