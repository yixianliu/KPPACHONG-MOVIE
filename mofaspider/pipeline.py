"""数据管线：清洗校验后的落库（去重 + 写多表 + 维度维护）。

写入顺序（单事务）：
  ba_film                 (按 source_url 去重 upsert)
  ba_film_play_source     (先删后插，挂 film_id)
  ba_film_episode         (引用 play_source.id)
  ba_film_attribute       (region/language/year/tag/star 字典，get-or-create)
  ba_film_attribute_relation (film<->attribute)
  ba_film_star            (主演维度，确保存在)

设计要点：
  - ba_film 无 source_url 唯一索引，故显式 SELECT 判定存在，再 INSERT/UPDATE
  - 影片每次写入都重建其播放源/分集/属性关联（增量跳过由 crawler 决定）
  - 所有字符串长度/枚举已在 Film.normalize 中按表约束校正
"""
from __future__ import annotations

import re
import time
from typing import Dict, List, Tuple

from .db import DB, T_FILM, T_FILM_PLAY_SOURCE, T_FILM_EPISODE, T_FILM_ATTRIBUTE, \
    T_FILM_ATTR_REL, T_FILM_STAR
from .logger import get_logger
from .models import Film

logger = get_logger()

_NOW = lambda: int(time.time() * 1000)
_SLUG_STRIP = re.compile(r"[^\w一-鿿]+")


def _slug(name: str) -> str:
    s = _SLUG_STRIP.sub("", name)
    return s[:60] if s else "na"


class Pipeline:
    def __init__(self, db: DB, category_id_map: Dict[str, int] | None = None):
        self._db = db
        self._cid_map = category_id_map or {}

    # ---------------- 对外入口 ----------------
    def save(self, film: Film) -> Tuple[int, str]:
        film.normalize(self._cid_map)
        film.build_seo()
        now = _NOW()
        conn = self._db.connection()
        try:
            with conn.cursor() as cur:
                film_id, action = self._upsert_film(cur, film, now)
                # 清理该影片旧的播放源/分集/属性关联
                cur.execute(f"DELETE FROM `{T_FILM_PLAY_SOURCE}` WHERE `film_id`=%s", (film_id,))
                cur.execute(f"DELETE FROM `{T_FILM_EPISODE}` WHERE `film_id`=%s", (film_id,))
                cur.execute(f"DELETE FROM `{T_FILM_ATTR_REL}` WHERE `film_id`=%s", (film_id,))
                # 写播放源 + 分集
                self._write_sources(cur, film, film_id)
                # 写维度属性 + 关联
                self._write_attributes(cur, film, film_id)
                # 写主演维度
                self._write_stars(cur, film)
            conn.commit()
            logger.info("影片落库 %s id=%s 标题=%s 源=%s 分集=%d",
                        action, film_id, film.title, film.source_url, film.episode_total)
            return film_id, action
        except Exception:
            conn.rollback()
            logger.exception("影片落库失败 标题=%s 源=%s", film.title, film.source_url)
            raise

    # ---------------- 内部 ----------------
    def _upsert_film(self, cur, film: Film, now: int) -> Tuple[int, str]:
        cur.execute(f"SELECT `id`,`create_time` FROM `{T_FILM}` WHERE `source_url`=%s",
                    (film.source_url,))
        row = cur.fetchone()
        if row:
            film_id = row["id"]
            create_time = row["create_time"] or now
            data = film.to_film_row(now)
            data["create_time"] = create_time
            set_sql = ", ".join(f"`{k}`=%s" for k in data.keys())
            cur.execute(f"UPDATE `{T_FILM}` SET {set_sql} WHERE `id`=%s",
                        tuple(data.values()) + (film_id,))
            return film_id, "update"
        # insert
        data = film.to_film_row(now)
        data["create_time"] = now
        cols = list(data.keys())
        col_sql = ", ".join(f"`{c}`" for c in cols)
        placeholders = ", ".join(["%s"] * len(cols))
        cur.execute(f"INSERT INTO `{T_FILM}` ({col_sql}) VALUES ({placeholders})",
                    tuple(data.values()))
        return cur.lastrowid, "insert"

    def _write_sources(self, cur, film: Film, film_id: int):
        for src in film.sources:
            cur.execute(
                f"INSERT INTO `{T_FILM_PLAY_SOURCE}` "
                f"(`film_id`,`name`,`sort`,`play_type`,`play_from`,`remark`,`status`,`weigh`,`create_time`,`update_time`) "
                f"VALUES (%s,%s,%s,%s,%s,%s,'1',0,%s,%s)",
                (film_id, src.name, src.sort, src.play_type, src.from_flag,
                 src.remark, _NOW(), _NOW()),
            )
            src_id = cur.lastrowid
            for ep in src.episodes:
                cur.execute(
                    f"INSERT INTO `{T_FILM_EPISODE}` "
                    f"(`film_id`,`source_id`,`title`,`sort`,`play_url`,`play_url_backup`,`duration`,`is_free`,`status`,`create_time`,`update_time`) "
                    f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'1',%s,%s)",
                    (film_id, src_id, ep.title, ep.sort, ep.play_url,
                     ep.play_url_backup, ep.duration, str(ep.is_free), _NOW(), _NOW()),
                )

    def _get_or_create_attr(self, cur, attr_type: str, name: str) -> int:
        name = name.strip()
        if not name:
            return 0
        en_name = _slug(name)
        unique_key = f"{attr_type}:{en_name}"
        cur.execute(f"SELECT `id` FROM `{T_FILM_ATTRIBUTE}` WHERE `unique_key`=%s",
                    (unique_key,))
        r = cur.fetchone()
        if r:
            return r["id"]
        cur.execute(
            f"INSERT INTO `{T_FILM_ATTRIBUTE}` "
            f"(`type`,`name`,`en_name`,`unique_key`,`remark`,`status`,`weigh`,`create_time`,`update_time`) "
            f"VALUES (%s,%s,%s,%s,'', '1',0,%s,%s)",
            (attr_type, name, en_name, unique_key, _NOW(), _NOW()),
        )
        return cur.lastrowid

    def _write_attributes(self, cur, film: Film, film_id: int):
        def rel(attr_id, attr_type):
            if attr_id:
                cur.execute(
                    f"INSERT IGNORE INTO `{T_FILM_ATTR_REL}` "
                    f"(`film_id`,`attribute_id`,`type`,`create_time`,`update_time`) "
                    f"VALUES (%s,%s,%s,%s,%s)",
                    (film_id, attr_id, attr_type, _NOW(), _NOW()),
                )

        if film.region:
            rel(self._get_or_create_attr(cur, "region", film.region), "region")
        if film.language:
            rel(self._get_or_create_attr(cur, "language", film.language), "language")
        if film.year:
            rel(self._get_or_create_attr(cur, "year", str(film.year)), "year")
        for t in film.tags:
            rel(self._get_or_create_attr(cur, "tag", t), "tag")
        for s in film.stars:
            rel(self._get_or_create_attr(cur, "star", s), "star")

    @staticmethod
    def _write_stars(cur, film: Film):
        for s in film.stars:
            s = s.strip()
            if not s:
                continue
            cur.execute(
                f"INSERT IGNORE INTO `{T_FILM_STAR}` (`name`,`film_count`,`update_time`) "
                f"VALUES (%s,0,%s)",
                (s, _NOW()),
            )
