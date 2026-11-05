"""pipeline 模块单元测试：以假游标验证「查重 -> 新增/全字段覆盖更新」逻辑与原子锁。"""
from __future__ import annotations

from mofaspider.models import Film
from mofaspider.pipeline import Pipeline


class FakeCursor:
    """记录执行的 SQL，并模拟 SELECT FOR UPDATE 的命中/未命中。"""

    def __init__(self, existing=None):
        self._existing = existing          # dict | None
        self.executed = []
        self.lastrowid = 0

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._last_sql = sql

    def fetchone(self):
        return self._existing


def _make_film():
    f = Film(site_id="1", title="测试影片", source_url="http://x/1", type="movie")
    f.normalize({})
    return f


def test_upsert_insert_when_absent():
    cur = FakeCursor(existing=None)
    cur.lastrowid = 42
    p = Pipeline(db=None)
    film_id, action = p._upsert_film(cur, _make_film(), 999)
    assert action == "insert"
    assert film_id == 42
    # 首条应为 SELECT ... FOR UPDATE；随后为 INSERT
    assert "FOR UPDATE" in cur.executed[0][0]
    assert cur.executed[1][0].startswith("INSERT INTO")


def test_upsert_update_when_exists():
    cur = FakeCursor(existing={"id": 7, "create_time": 111})
    p = Pipeline(db=None)
    film_id, action = p._upsert_film(cur, _make_film(), 999)
    assert action == "update"
    assert film_id == 7
    # 命中已存在：应执行 UPDATE 且带 WHERE 与全部字段
    update_sql = cur.executed[1][0]
    assert update_sql.startswith("UPDATE")
    assert "WHERE" in update_sql
    assert update_sql.count("=%s") >= 10      # 全字段覆盖更新


def test_upsert_preserves_create_time_on_update():
    cur = FakeCursor(existing={"id": 7, "create_time": 111})
    p = Pipeline(db=None)
    p._upsert_film(cur, _make_film(), 999)
    # UPDATE 绑定的参数中应保留原 create_time（更新操作不刷新创建时间）
    params = cur.executed[1][1]
    assert 111 in params                       # 原 create_time 被保留
    assert params[-1] == 7                     # 末尾 WHERE id=7
