"""数据库连接层：连接参数全部来自 Config，使用 PyMySQL + 轻量连接池。

提供：
  - DB.connect() 返回 PyMySQL 连接（调用方负责关闭）
  - DB.execute_upsert / execute_insert / fetch_one 等便捷封装
  - 表名常量（数据库真实表名，非连接机密，放代码常量无可厚非）
"""
from __future__ import annotations

import pymysql
from pymysql.cursors import DictCursor

from .config import DBConfig, Config
from .logger import get_logger

logger = get_logger()

# 真实表名前缀为 ba_（与 .env 的 PREFIX=kp_ 不同，以数据库实际为准）
T_FILM = "ba_film"
T_FILM_CATEGORY = "ba_film_category"
T_FILM_PLAY_SOURCE = "ba_film_play_source"
T_FILM_EPISODE = "ba_film_episode"
T_FILM_ATTRIBUTE = "ba_film_attribute"
T_FILM_ATTR_REL = "ba_film_attribute_relation"
T_FILM_STAR = "ba_film_star"


class DB:
    def __init__(self, cfg: DBConfig):
        self._cfg = cfg
        self._pool = None
        # 简单单连接缓存（脚本型爬虫足够；如需更高并发可换 PooledDB）
        self._conn = None

    @classmethod
    def from_config(cls, cfg: Config) -> "DB":
        return cls(cfg.db)

    def _connect(self) -> pymysql.connections.Connection:
        return pymysql.connect(
            host=self._cfg.host,
            port=self._cfg.port,
            user=self._cfg.user,
            password=self._cfg.password,
            database=self._cfg.database,
            charset=self._cfg.charset,
            cursorclass=DictCursor,
            autocommit=False,
            connect_timeout=15,
            read_timeout=60,
            write_timeout=60,
        )

    def connection(self) -> pymysql.connections.Connection:
        """返回可用连接，断线自动重连。"""
        if self._conn is None or not self._conn.open:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
            self._conn = self._connect()
            logger.debug("MySQL 连接已建立 -> %s", self._cfg.dsn)
        return self._conn

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    # ---------- 便捷封装 ----------

    def fetch_one(self, sql: str, params: tuple | None = None):
        conn = self.connection()
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchone()

    def fetch_all(self, sql: str, params: tuple | None = None):
        conn = self.connection()
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchall()

    def exists(self, sql: str, params: tuple | None = None) -> bool:
        return self.fetch_one(sql, params) is not None

    def insert(self, table: str, row: dict) -> int:
        """插入一行，返回自增 id。"""
        cols = list(row.keys())
        placeholders = ", ".join(["%s"] * len(cols))
        col_sql = ", ".join(f"`{c}`" for c in cols)
        sql = f"INSERT INTO `{table}` ({col_sql}) VALUES ({placeholders})"
        conn = self.connection()
        with conn.cursor() as cur:
            cur.execute(sql, tuple(row.values()))
            last_id = cur.lastrowid
        conn.commit()
        return last_id

    def update(self, table: str, row: dict, where: dict) -> int:
        set_cols = [f"`{c}`=%s" for c in row.keys()]
        where_cols = [f"`{c}`=%s" for c in where.keys()]
        sql = f"UPDATE `{table}` SET {', '.join(set_cols)} WHERE {' AND '.join(where_cols)}"
        params = tuple(row.values()) + tuple(where.values())
        conn = self.connection()
        with conn.cursor() as cur:
            cur.execute(sql, params)
            affected = cur.rowcount
        conn.commit()
        return affected

    def upsert(self, table: str, row: dict, unique_keys: list) -> tuple[int, str]:
        """按 unique_keys 做存在即更新（INSERT ... ON DUPLICATE KEY UPDATE）。

        返回 (row_id, 'insert'|'update')。要求表对 unique_keys 有唯一索引。
        若没有唯一索引，则由调用方先 exists 判断。
        """
        cols = list(row.keys())
        col_sql = ", ".join(f"`{c}`" for c in cols)
        placeholders = ", ".join(["%s"] * len(cols))
        update_sql = ", ".join(f"`{c}`=VALUES(`{c}`)" for c in cols if c not in unique_keys)
        sql = (f"INSERT INTO `{table}` ({col_sql}) VALUES ({placeholders}) "
               f"ON DUPLICATE KEY UPDATE {update_sql}")
        conn = self.connection()
        with conn.cursor() as cur:
            cur.execute(sql, tuple(row.values()))
            if cur.lastrowid:
                rid = cur.lastrowid
                action = "insert"
            else:
                # 命中重复键，取现有行的 unique 值
                rid = self._select_id(conn, table, unique_keys, row)
                action = "update"
        conn.commit()
        return rid, action

    @staticmethod
    def _select_id(conn, table, unique_keys, row):
        where = " AND ".join(f"`{k}`=%s" for k in unique_keys)
        cols = ", ".join(f"`{k}`" for k in unique_keys)
        with conn.cursor() as cur:
            cur.execute(f"SELECT `id` FROM `{table}` WHERE {where}",
                        tuple(row[k] for k in unique_keys))
            r = cur.fetchone()
        return r["id"] if r else 0

    def execute_raw(self, sql: str, params: tuple | None = None):
        conn = self.connection()
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
        conn.commit()
