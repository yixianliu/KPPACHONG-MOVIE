"""时间戳单位归一（秒级/毫秒级混写容错）。

背景：本项目统一写入 13 位毫秒时间戳，但库内存在外部写入方（如 CMS 后台）
写入的 10 位秒级 `update_time`。若直接按毫秒比较，秒级值会被误判为「极旧」，
导致增量模式反复选中同一批资源。

单位判定规则（唯一口径，集中于此）：
  - 数值 < 10^12  → 视为**秒级**，乘 1000 归一为毫秒；
  - 数值 >= 10^12 → 视为**毫秒级**，原样返回；
  - None / 非法 / <= 0 → 返回 0（未知时间）。

注意：本模块只做「比较侧」归一，不回写库内数据、不做全表修复。
"""
from __future__ import annotations

# 秒/毫秒分界阈值：10^12（1 万亿）
#   10 位秒级最大值 9999999999 < 10^12；13 位毫秒级约为 1.7×10^12 >= 10^12
SECONDS_THRESHOLD = 10 ** 12


def normalize_timestamp_ms(value) -> int:
    """把任意来源的 update_time 归一为毫秒时间戳。

    >>> normalize_timestamp_ms(1793764022)       # 秒级 -> 毫秒
    1793764022000
    >>> normalize_timestamp_ms(1793764022000)    # 毫秒级原样
    1793764022000
    >>> normalize_timestamp_ms(None)
    0
    """
    if value is None:
        return 0
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return 0
    if ts <= 0:
        return 0
    return ts * 1000 if ts < SECONDS_THRESHOLD else ts


def timestamp_ms_sql_expr(column: str = "update_time", alias: str = "") -> str:
    """生成 SQL 侧同口径归一表达式，供增量时间窗筛选使用。

    为什么用 SQL 表达式而不是「SQL 粗筛 + Python 二次过滤」：
    本模块的游标分批会先 `LIMIT batch` 再在 Python 中过滤，若被过滤掉的行
    占满一批，会导致本批为空、游标提前结束（漏条目）。把单位归一放到 SQL
    WHERE 中与 `ORDER BY id LIMIT` 同时生效，可保证**不漏、不误纳**，
    且规则与 normalize_timestamp_ms 共享同一阈值常量。

    生成形如：
      (CASE WHEN `update_time` < 1000000000000
            THEN `update_time` * 1000 ELSE `update_time` END)
    """
    col = f"{alias}.`{column}`" if alias else f"`{column}`"
    return (f"(CASE WHEN {col} < {SECONDS_THRESHOLD} "
            f"THEN {col} * 1000 ELSE {col} END)")
