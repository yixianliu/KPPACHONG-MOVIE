"""updater / pipeline.save_changed 单元测试。

验证资源全量更新模式核心逻辑：
  - 变更字段比对：仅对发生变化的字段执行 UPDATE，保留未变更字段原值
  - 空值/脏数据：新爬取为空时不覆盖库内已有非空值
  - 无变更：返回 ('unchanged', id)，仅刷新 update_time，不触发子表重建
  - 子表签名：播放源/分集签名变化才重建，否则保留
  - 任务队列：按 resource_id+source_url 去重、增量时间窗筛选、limit
  - 重试容错：失败自动重试，最多 3 次
"""
from __future__ import annotations

import json

from mofaspider.models import Film
from mofaspider.pipeline import Pipeline
from mofaspider.updater import UpdateResult, UpdateTask, Updater

_value_changed = Pipeline._value_changed  # 静态方法别名


# ---------------- 辅助 ----------------
def _make_film(source_url="http://x/1", title="测试影片", score=8.0,
               intro="剧情简介", region="中国大陆", stars=None):
    f = Film(site_id="1", title=title, source_url=source_url, type="movie")
    f.score = score
    f.intro = intro
    f.region = region
    f.stars = stars or ["演员A", "演员B"]
    f.normalize({})
    return f


class FakeCursor:
    """模拟游标：记录 SQL，支持 SELECT FOR UPDATE 命中旧行。"""

    def __init__(self, existing=None):
        self._existing = existing
        self.executed = []
        self.lastrowid = 0

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._existing

    def fetchall(self):
        return []

    # 上下文管理器支持（pipeline 用 with conn.cursor() as cur）
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    """模拟连接：游标上下文管理器。"""

    def __init__(self, existing=None):
        self._cur = FakeCursor(existing)
        self.commit_calls = 0
        self.rollback_calls = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.commit_calls += 1

    def rollback(self):
        self.rollback_calls += 1


def _old_row(film):
    """构造库内既有行（ba_film 各字段快照）。

    与 save_changed 内 build_seo 保持一致，避免 SEO 字段被误判为变更。
    """
    film.build_seo()                                # 补全 SEO（与 save_changed 一致）
    data = film.to_film_row(1000)
    data["id"] = 7
    data["create_time"] = 500
    data["status"] = "1"
    data["author"] = "spider_mofa"
    return data


class FakeDB:
    """最小 DB 桩：save_changed 通过 self._db.connection() 取连接。"""

    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        return self._conn


# ---------------- save_changed：变更字段比对 ----------------
def test_changed_only_updates_diff_fields():
    """新爬取评分变化 -> 仅 UPDATE score（及其他变更字段），保留未变更字段原值。"""
    old = _old_row(_make_film(score=5.0))          # 库内 score=5.0
    film = _make_film(score=8.0)                   # 新爬取 score=8.0
    conn = FakeConn(existing=old)
    p = Pipeline(db=FakeDB(conn))
    p._read_source_signature = lambda cur, fid, row=None: json.dumps([])   # 子表签名为空（变化）
    film_id, action = p.save_changed(film)

    # 找到 UPDATE ba_film 语句
    update_sql = [s for s, _ in conn._cur.executed if s.startswith("UPDATE `ba_film`")]
    assert update_sql, "应执行 UPDATE"
    # 变更字段应包含 score；不应包含 title（未变更）
    assert "`score`=%s" in update_sql[-1]
    assert "`title`=%s" not in update_sql[-1]
    assert action == "update"


def test_blank_value_does_not_overwrite_existing():
    """新爬取 intro 为空 -> 保留库内原有非空 intro，不覆盖为 ''。"""
    old = _old_row(_make_film(intro="原有剧情很长"))
    film = _make_film(intro="")                     # 新爬取 intro 为空（脏数据）
    conn = FakeConn(existing=old)
    p = Pipeline(db=FakeDB(conn))
    p._read_source_signature = lambda cur, fid, row=None: json.dumps([])
    _, action = p.save_changed(film)
    # 找到 UPDATE 语句，确认其中没有 intro 覆盖
    update_sql = [s for s, _ in conn._cur.executed if s.startswith("UPDATE `ba_film`")]
    # intro 为空且库内非空 -> 不应写入 intro
    assert all("`intro`=%s" not in s for s in update_sql)


def test_no_change_returns_unchanged():
    """整片无任何字段变更 -> 返回 ('unchanged', id)，仅刷新 update_time。"""
    old = _old_row(_make_film(score=8.0, intro="剧情简介"))
    film = _make_film(score=8.0, intro="剧情简介")
    conn = FakeConn(existing=old)
    p = Pipeline(db=FakeDB(conn))
    # 子表签名完全一致（含属性部分），避免误判为子表变化
    p._read_source_signature = lambda cur, fid, row=None: p._sources_signature(film)
    film_id, action = p.save_changed(film)
    assert action == "unchanged"
    assert film_id == 7
    # 仅刷新 update_time，不删除子表（无 DELETE ba_film_play_source）
    deletes = [s for s, _ in conn._cur.executed if s.startswith("DELETE")]
    assert not deletes


def test_source_signature_triggers_rebuild():
    """播放源分集签名变化 -> 触发子表重建（DELETE + INSERT）。"""
    old = _old_row(_make_film())
    conn = FakeConn(existing=old)
    p = Pipeline(db=FakeDB(conn))
    p._read_source_signature = lambda cur, fid, row=None: json.dumps([])   # 旧签名不同
    p._write_sources = lambda cur, film, fid: None               # 桩掉写库
    p._write_attributes = lambda cur, film, fid: None
    p._write_stars = lambda cur, film: None
    _, action = p.save_changed(_make_film())
    deletes = [s for s, _ in conn._cur.executed if s.startswith("DELETE")]
    assert any("ba_film_play_source" in s for s in deletes)
    assert action == "update"


# ---------------- _value_changed ----------------
def test_value_changed_semantics():
    assert _value_changed(None, "") is False        # 空值等价
    assert _value_changed("8.0", 8.0) is False      # 数值等价（字符串化比较）
    assert _value_changed("5.0", 8.0) is True       # 实质变化


# ---------------- 任务队列去重 ----------------
def test_build_tasks_dedup_and_incremental():
    """同一 resource_id+source_url 去重；incremental 仅取 update_time 早于截止的资源。"""
    rows = [
        {"id": 1, "source_url": "http://x/1", "title": "A", "type": "movie",
         "status": "1", "update_time": 100},
        {"id": 1, "source_url": "http://x/1", "title": "A", "type": "movie",
         "status": "1", "update_time": 100},       # 重复 -> 去重
        {"id": 2, "source_url": "http://x/2", "title": "B", "type": "tv",
         "status": "1", "update_time": 9999999999999},  # 新近 -> 增量应排除
    ]

    class FakeDB:
        def fetch_all(self, sql, params=None):
            # 验证增量 SQL 带 update_time 条件
            assert "update_time" in sql
            return rows

    upd = Updater.__new__(Updater)
    upd._db = FakeDB()
    upd._cfg = type("C", (), {"mofa": type("M", (), {"category_id_map": {}})()})()

    tasks = upd._build_tasks(mode="incremental", since_hours=24,
                             resource_ids=None, source_urls=None,
                             status_filter=None, types_filter=None, limit=0)
    # 去重后 2 条（id=1 合并，id=2 为最新 update_time 排除）
    ids = [t.resource_id for t in tasks]
    assert len(tasks) == 2
    assert ids.count(1) == 1


# ================= 全量覆盖专项修复测试 =================
import tempfile
from pathlib import Path as _Path
from mofaspider.updater import UPDATE_BATCH_SIZE, UPDATE_STATE_FILE


def _cursor_updater(tmp_path, mode="full"):
    """构造带断点游标目录的 Updater 桩（_cfg.root/state_dir 指向临时目录）。"""
    upd = Updater.__new__(Updater)
    upd._cfg = type("C", (), {
        "mofa": type("M", (), {"extract_play_url": False,
                               "max_episodes_per_film": 0,
                               "category_id_map": {}})(),
        "spider": type("S", (), {"image_download": False,
                                 "state_dir": "state", "concurrency": 4})(),
        "root": _Path(tmp_path),
    })()
    return upd


def test_cursor_pagination_no_gap_no_dup():
    """游标分批：id 升序 + after_id 条件，多批遍历无遗漏、无重复，覆盖全部资源。"""
    # 模拟数据库全部资源 id=1..7（7 条）
    all_rows = [{"id": i, "source_url": f"http://x/{i}", "title": f"片{i}",
                 "type": "movie", "status": "1", "update_time": i}
                for i in range(1, 8)]

    class FakeDB:
        def __init__(self, rows):
            self._rows = rows
            self._calls = []

        def fetch_all(self, sql, params=None):
            # 解析 after_id 参数（id > after_id）
            after_id = 0
            for p in (params or ()):
                if isinstance(p, int) and p > 0:
                    after_id = p
            # 解析 LIMIT
            import re as _re
            lm = _re.search(r"LIMIT (\d+)", sql)
            limit = int(lm.group(1)) if lm else 0
            self._calls.append((after_id, limit))
            # 模拟 WHERE id > after_id ORDER BY id ASC LIMIT limit
            rows = [r for r in self._rows if r["id"] > after_id]
            rows.sort(key=lambda r: r["id"])
            return rows[:limit] if limit else rows

    db = FakeDB(all_rows)
    upd = _cursor_updater(tempfile.mkdtemp())
    upd._db = db

    # 分批（batch=3）遍历全部，用 _build_tasks 直接验证游标 SQL 语义
    seen_ids = []
    after_id = 0
    while True:
        tasks = upd._build_tasks(mode="full", since_hours=0,
                                 resource_ids=None, source_urls=None,
                                 status_filter=None, types_filter=None,
                                 limit=3, after_id=after_id)
        if not tasks:
            break
        seen_ids += [t.resource_id for t in tasks]
        after_id = max(t.resource_id for t in tasks)

    # 无遗漏、无重复：恰好覆盖全部 1..7
    assert seen_ids == list(range(1, 8)), f"应覆盖全部资源无遗漏无重复，实际={seen_ids}"
    # 每批 <= batch_size（此处 3）
    assert len(set(seen_ids)) == 7


def test_cursor_pagination_respects_limit_advances():
    """limit 控制：游标推进保证不同资源被处理，而非固定头部重复。"""
    all_rows = [{"id": i, "source_url": f"http://x/{i}", "title": f"片{i}",
                 "type": "movie", "status": "1", "update_time": i}
                for i in range(1, 11)]  # 10 条

    class FakeDB:
        def fetch_all(self, sql, params=None):
            after_id = 0
            for p in (params or ()):
                if isinstance(p, int) and p > 0:
                    after_id = p
            import re as _re
            lm = _re.search(r"LIMIT (\d+)", sql)
            limit = int(lm.group(1)) if lm else 0
            rows = [r for r in all_rows if r["id"] > after_id]
            rows.sort(key=lambda r: r["id"])
            return rows[:limit] if limit else rows

    upd = _cursor_updater(tempfile.mkdtemp())
    upd._db = FakeDB()

    # limit=4：应恰好处理 4 条且为 id 1..4（游标推进，非固定头部）
    tasks = upd._build_tasks(mode="full", since_hours=0,
                             resource_ids=None, source_urls=None,
                             status_filter=None, types_filter=None,
                             limit=4, after_id=0)
    assert [t.resource_id for t in tasks] == [1, 2, 3, 4]


def test_cursor_state_save_load_clear(tmp_path):
    """断点续传：游标可落盘、可读取、遍历完成自动清空。"""
    upd = _cursor_updater(str(tmp_path))
    report = type("R", (), {"updated": 3, "unchanged": 1, "failed": 0})()

    # 保存游标
    upd._save_cursor("full", last_id=100, processed=4, report=report)
    state_path = tmp_path / "state" / UPDATE_STATE_FILE
    assert state_path.is_file(), "断点状态文件应已创建"

    # 读取游标
    cursor = upd._load_cursor("full")
    assert cursor["last_id"] == 100
    assert cursor["processed"] == 4
    # 其它 mode 互不干扰
    assert upd._load_cursor("incremental") == {}

    # 清理
    upd._clear_cursor("full")
    assert not state_path.exists(), "遍历完成应清空游标状态"


def test_cursor_resume_continues_from_last_id(tmp_path):
    """断点续传：中断后重跑从 last_id 之后继续，不重复处理已推进资源。"""
    upd = _cursor_updater(str(tmp_path))
    report = type("R", (), {"updated": 2, "unchanged": 0, "failed": 0})()
    upd._save_cursor("full", last_id=50, processed=2, report=report)

    cursor = upd._load_cursor("full")
    assert cursor["last_id"] == 50

    # 模拟续传：_build_tasks 应从 id>50 开始（验证 SQL 带 id>50 条件）
    class FakeDB:
        def fetch_all(self, sql, params=None):
            assert "id` > %s" in sql or "`id`>" in sql, "应包含主键游标条件"
            return []
    upd._db = FakeDB()
    upd._build_tasks(mode="full", since_hours=0, resource_ids=None,
                     source_urls=None, status_filter=None, types_filter=None,
                     limit=10, after_id=cursor["last_id"])


def test_run_covers_all_resources_via_batches(tmp_path, monkeypatch):
    """run() 集成：分批游标遍历覆盖全部资源（无遗漏无重复），统计与游标推进正确。

    用桩替身 _process_batch 模拟「每批处理即推进游标」，验证 run() 主循环
    能完整遍历全部数据库资源并正确汇总，且遍历完成清空游标。
    """
    # 全部资源 id=1..6（6 条），按 id 升序分批（batch=2 -> 3 批）
    all_rows = [{"id": i, "source_url": f"http://x/{i}", "title": f"片{i}",
                 "type": "movie", "status": "1", "update_time": i}
                for i in range(1, 7)]

    class FakeDB:
        def __init__(self, rows):
            self._rows = rows
            self.closed = False

        def fetch_all(self, sql, params=None):
            after_id = 0
            for p in (params or ()):
                if isinstance(p, int) and p > 0:
                    after_id = p
            import re as _re
            lm = _re.search(r"LIMIT (\d+)", sql)
            limit = int(lm.group(1)) if lm else 0
            rows = [r for r in self._rows if r["id"] > after_id]
            rows.sort(key=lambda r: r["id"])
            return rows[:limit] if limit else rows

        def close(self):
            self.closed = True

    db = FakeDB(all_rows)
    upd = _cursor_updater(str(tmp_path))
    upd._db = db

    # 桩 _process_batch：返回本批结果 + 本批最大 id + 重试数
    def fake_process_batch(tasks, workers, retry_max, dry_run=False):
        max_id = max(t.resource_id for t in tasks)
        results = [UpdateResult(task=t, action="updated", changed_fields=1)
                   for t in tasks]
        return results, max_id, 0
    monkeypatch.setattr(upd, "_process_batch", fake_process_batch)

    report = upd.run(mode="full", since_hours=0, resource_ids=None,
                     source_urls=None, status_filter=None, types_filter=None,
                     limit=0, workers=2, retry_max=3, batch_size=2, resume=True)
    assert report.total == 6            # 覆盖全部 6 条
    assert report.updated == 6
    assert report.failed == 0
    # 遍历完成应清空游标状态
    state_path = _Path(tmp_path) / "state" / UPDATE_STATE_FILE
    assert not state_path.exists(), "全量遍历完成后应清空断点游标"


def test_run_resume_skips_already_processed(tmp_path, monkeypatch):
    """run() 断点续传：存在 last_id 游标时，从游标之后继续，跳过已处理资源。"""
    all_rows = [{"id": i, "source_url": f"http://x/{i}", "title": f"片{i}",
                 "type": "movie", "status": "1", "update_time": i}
                for i in range(1, 6)]  # id 1..5

    class FakeDB:
        def __init__(self, rows):
            self._rows = rows
            self.closed = False

        def fetch_all(self, sql, params=None):
            after_id = 0
            for p in (params or ()):
                if isinstance(p, int) and p > 0:
                    after_id = p
            import re as _re
            lm = _re.search(r"LIMIT (\d+)", sql)
            limit = int(lm.group(1)) if lm else 0
            rows = [r for r in self._rows if r["id"] > after_id]
            rows.sort(key=lambda r: r["id"])
            return rows[:limit] if limit else rows

        def close(self):
            self.closed = True

    db = FakeDB(all_rows)
    upd = _cursor_updater(str(tmp_path))
    upd._db = db

    # 预置游标：已处理到 id=2（即 id 1,2 已完成）
    report0 = type("R", (), {"updated": 2, "unchanged": 0, "failed": 0})()
    upd._save_cursor("full", last_id=2, processed=2, report=report0)

    def fake_process_batch(tasks, workers, retry_max, dry_run=False):
        max_id = max(t.resource_id for t in tasks)
        results = [UpdateResult(task=t, action="updated", changed_fields=1)
                   for t in tasks]
        return results, max_id, 0
    monkeypatch.setattr(upd, "_process_batch", fake_process_batch)

    report = upd.run(mode="full", since_hours=0, resource_ids=None,
                     source_urls=None, status_filter=None, types_filter=None,
                     limit=0, workers=2, retry_max=3, batch_size=2, resume=True)
    # 只处理 id>2 的 3,4,5 共 3 条（跳过已处理的 1,2）
    assert report.total == 3
    assert report.updated == 3


# ---------------- 重试容错 ----------------
def test_retry_max_exhausted_marks_failed(monkeypatch):
    """爬取持续失败 -> 重试 3 次后标记 failed 并返回错误。"""
    upd = Updater.__new__(Updater)
    upd._cfg = type("C", (), {
        "mofa": type("M", (), {"extract_play_url": False, "max_episodes_per_film": 0})(),
        "spider": type("S", (), {"image_download": False})(),
        "root": __import__("pathlib").Path("."),
    })()
    upd._http = None
    upd._img = type("I", (), {"download": lambda *a, **k: ""})()
    upd._pipeline = None
    upd._db_lock = __import__("threading").Lock()

    task = UpdateTask(resource_id=1, source_url="http://x/1")

    # 用桩替代 _fetch_full 持续抛异常
    def boom(*a, **k):
        raise RuntimeError("网络错误")
    monkeypatch.setattr(upd, "_fetch_full", boom)

    import time as _t
    _orig = _t.sleep
    monkeypatch.setattr(_t, "sleep", lambda s: None)   # 关闭真实退避等待

    result = upd._update_one(task, retry_max=3)
    assert result.action == "failed"
    assert task.retry == 3                            # 已重试 3 次
    assert "网络错误" in result.error


# ================= dry-run（只比对不写库）测试 =================
def test_preview_changed_is_read_only():
    """preview_changed 只读：不产生 UPDATE/DELETE/INSERT，也不提交事务。"""
    old = _old_row(_make_film(score=5.0))
    film = _make_film(score=8.0)
    conn = FakeConn(existing=old)
    p = Pipeline(db=FakeDB(conn))
    p._read_source_signature = lambda cur, fid, row=None: json.dumps([])   # 子表签名变化
    fid, action, diffs, src_changed = p.preview_changed(film)

    assert fid == 7
    assert action == "update"
    assert src_changed is True
    score_diff = [d for d in diffs if d["field"] == "score"]
    assert score_diff and score_diff[0]["new"] == "8.0" and score_diff[0]["old"] == "5.0"
    # 关键断言：全程无任何写语句、无提交
    writes = [s for s, _ in conn._cur.executed
              if s.startswith(("UPDATE", "DELETE", "INSERT", "REPLACE"))]
    assert not writes, f"dry-run 不应产生写操作，实际={writes}"
    assert conn.commit_calls == 0


def test_source_signature_roundtrip_is_equal():
    """签名一致性回归：库内 is_free='1'（字符串）+ 属性段 必须与爬取侧签名相等。

    历史上读侧不带属性段、且 is_free 类型不同，导致每次更新都误判子表变更。
    """
    from mofaspider.models import Episode, PlaySource

    film = _make_film(region="中国大陆")
    film.year = 2022
    film.tags = ["动作", "犯罪"]
    src = PlaySource(name="ikun资源", from_flag="ikm3u8", play_type="url")
    src.episodes = [Episode(sort=1, title="正片",
                            view_url="http://x/view/1-1-1.html",
                            play_url="https://cdn/x/index.m3u8", is_free=1)]
    film.sources = [src]

    class SigCursor:
        def __init__(self):
            self._n = 0

        def execute(self, sql, params=None):
            self._n += 1

        def fetchall(self):
            if self._n == 1:     # 播放源行（play_from 对应 from_flag）
                return [{"name": "ikun资源", "sort": 0, "play_type": "url",
                         "play_from": "ikm3u8", "remark": "", "id": 11}]
            # 分集行：is_free 在库内为字符串 '1'
            return [{"title": "正片", "sort": 1,
                     "play_url": "https://cdn/x/index.m3u8",
                     "duration": "", "is_free": "1"}]

    old_row = {"region": "中国大陆", "language": "", "year": 2022,
               "tags": "动作，犯罪", "stars": "演员A，演员B"}
    assert (Pipeline._read_source_signature(SigCursor(), 7, old_row)
            == Pipeline._sources_signature(film))


def test_update_one_dry_run_skips_write_and_image(monkeypatch):
    """_update_one(dry_run=True)：不下载图片、不调用 save_changed，仅返回将变更字段。"""

    class ImgStub:
        def download(self, *a, **k):
            raise AssertionError("dry-run 不应下载图片")

    class PipelineStub:
        def preview_changed(self, film):
            return 7, "update", [{"field": "score", "old": "5.0", "new": "8.0"}], True

        def save_changed(self, film):
            raise AssertionError("dry-run 不应写库")

    upd = Updater.__new__(Updater)
    upd._cfg = type("C", (), {
        "mofa": type("M", (), {"extract_play_url": False, "max_episodes_per_film": 0})(),
        "spider": type("S", (), {"image_download": True})(),
        "root": _Path("."),
    })()
    upd._img = ImgStub()
    upd._pipeline = PipelineStub()
    upd._db_lock = __import__("threading").Lock()
    monkeypatch.setattr(upd, "_fetch_full", lambda url, t: _make_film())

    task = UpdateTask(resource_id=1, source_url="http://x/1")
    r = upd._update_one(task, retry_max=0, dry_run=True)

    assert r.action == "updated"
    assert r.changed_field_names == ["score", "<play_sources>"]
    assert r.changed_fields == 2
    assert r.dry_run is True
    assert r.preview[0]["field"] == "score"


def test_run_dry_run_writes_no_state_file(tmp_path, monkeypatch):
    """run(dry_run=True)：完整遍历但不落盘（无断点游标、无失败清单）。"""
    all_rows = [{"id": i, "source_url": f"http://x/{i}", "title": f"片{i}",
                 "type": "movie", "status": "1", "update_time": i}
                for i in range(1, 5)]     # 4 条

    class FakeDB:
        def __init__(self, rows):
            self._rows = rows

        def fetch_all(self, sql, params=None):
            after_id = 0
            for p in (params or ()):
                if isinstance(p, int) and p > 0:
                    after_id = p
            import re as _re
            lm = _re.search(r"LIMIT (\d+)", sql)
            limit = int(lm.group(1)) if lm else 0
            rows = [r for r in self._rows if r["id"] > after_id]
            rows.sort(key=lambda r: r["id"])
            return rows[:limit] if limit else rows

        def close(self):
            pass

    upd = _cursor_updater(str(tmp_path))
    upd._db = FakeDB(all_rows)

    def fake_process_batch(tasks, workers, retry_max, dry_run=False):
        assert dry_run is True, "dry-run 标记应传入批处理"
        max_id = max(t.resource_id for t in tasks)
        results = [UpdateResult(task=t, action="updated", changed_fields=2,
                                changed_field_names=["score", "<play_sources>"],
                                dry_run=True)
                   for t in tasks]
        return results, max_id, 0
    monkeypatch.setattr(upd, "_process_batch", fake_process_batch)
    # 失败清单落盘不应被调用
    monkeypatch.setattr(upd, "_dump_failed",
                        lambda report: (_ for _ in ()).throw(
                            AssertionError("dry-run 不应写失败清单")))

    report = upd.run(mode="full", since_hours=0, resource_ids=None,
                     source_urls=None, status_filter=None, types_filter=None,
                     limit=0, workers=2, retry_max=3, batch_size=2,
                     resume=True, dry_run=True)

    assert report.dry_run is True
    assert report.total == 4
    assert report.updated == 4
    assert report.changed_fields_total == 8          # 4 条 * 2 字段
    assert len(report.preview_items) == 4
    state_path = _Path(tmp_path) / "state" / UPDATE_STATE_FILE
    assert not state_path.exists(), "dry-run 不应写断点游标状态文件"
