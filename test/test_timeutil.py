"""timeutil 时间戳单位归一测试。

覆盖：秒级/毫秒级/边界值、非法值、往返幂等、增量窗口「不漏不误纳」，
以及 updater 构建增量任务时的 SQL 归一与任务字段归一。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from mofaspider.timeutil import (SECONDS_THRESHOLD, normalize_timestamp_ms,
                                 timestamp_ms_sql_expr)
from mofaspider.updater import Updater


# ---------------- 归一函数 ----------------
def test_normalize_seconds_to_ms():
    """10 位秒级 -> 13 位毫秒（×1000）。"""
    assert normalize_timestamp_ms(1793764022) == 1793764022000
    assert normalize_timestamp_ms("1793764022") == 1793764022000


def test_normalize_ms_unchanged():
    """13 位毫秒级保持原样。"""
    assert normalize_timestamp_ms(1793764022000) == 1793764022000
    assert normalize_timestamp_ms("1793764022000") == 1793764022000


def test_normalize_boundary():
    """边界：9999999999 视为秒；1000000000000（=10^12）视为毫秒。"""
    assert normalize_timestamp_ms(9999999999) == 9999999999000
    assert normalize_timestamp_ms(SECONDS_THRESHOLD) == SECONDS_THRESHOLD


def test_normalize_invalid_values():
    """None/空/非数字/非正数 -> 0（未知时间）。"""
    assert normalize_timestamp_ms(None) == 0
    assert normalize_timestamp_ms("") == 0
    assert normalize_timestamp_ms("abc") == 0
    assert normalize_timestamp_ms(-1) == 0
    assert normalize_timestamp_ms(0) == 0


def test_normalize_idempotent():
    """往返一致：已是毫秒的归一结果再次归一回相同值。"""
    ms = 1793764022000
    assert normalize_timestamp_ms(normalize_timestamp_ms(ms)) == ms
    assert normalize_timestamp_ms(normalize_timestamp_ms(1793764022)) == 1793764022000


def test_sql_expr_shares_threshold():
    """SQL 表达式与 Python 函数同口径（同一阈值常量）。"""
    expr = timestamp_ms_sql_expr("update_time")
    assert "CASE WHEN" in expr and str(SECONDS_THRESHOLD) in expr
    assert "* 1000" in expr
    assert "`update_time`" in expr


# ---------------- 增量窗口语义 ----------------
def test_incremental_window_no_miss_no_false():
    """窗口筛选：秒级旧数据要入选；秒级新数据不得误纳；毫秒旧/新分别入选/排除。"""
    now_ms = int(datetime.now().timestamp() * 1000)
    cutoff = int((datetime.now() - timedelta(hours=24)).timestamp() * 1000)
    ms_recent = now_ms                      # 毫秒-新 -> 排除
    ms_old = cutoff - 3600 * 1000           # 毫秒-旧 -> 入选
    sec_recent = now_ms // 1000             # 秒-新 -> 归一后排除（不得误纳）
    sec_old = cutoff // 1000 - 3600         # 秒-旧 -> 归一后入选（不得漏）
    unknown = 0                             # 未知时间 -> 入选（不漏）

    rows = [ms_recent, ms_old, sec_recent, sec_old, unknown]
    selected = [v for v in rows if normalize_timestamp_ms(v) < cutoff]

    assert ms_old in selected
    assert sec_old in selected
    assert unknown in selected
    assert ms_recent not in selected
    assert sec_recent not in selected      # 关键：秒级噪声不再被误判为极旧


# ---------------- updater 集成（SQL 与任务字段） ----------------
def test_build_tasks_incremental_uses_normalized_sql_and_task():
    """_build_tasks 增量模式：SQL 使用归一表达式；任务 update_time 归一为毫秒。"""
    captured = {}

    class FakeDB:
        def fetch_all(self, sql, params=None):
            captured["sql"] = sql
            captured["params"] = params
            return [{"id": 1, "source_url": "http://x/1", "title": "A",
                     "type": "movie", "status": "1", "update_time": 1793764022}]

    upd = Updater.__new__(Updater)
    upd._db = FakeDB()
    upd._cfg = type("C", (), {"mofa": type("M", (), {"category_id_map": {}})()})()

    tasks = upd._build_tasks(mode="incremental", since_hours=24,
                             resource_ids=None, source_urls=None,
                             status_filter=None, types_filter=None, limit=0)

    assert "CASE WHEN" in captured["sql"] and "* 1000" in captured["sql"]
    assert len(tasks) == 1
    assert tasks[0].update_time == 1793764022000     # 秒级已归一为毫秒
