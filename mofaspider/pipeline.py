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

新增：save_changed（资源全量更新模式）
  - 按「变更字段」增量更新：仅 UPDATE 发生变化的字段，保留未变更字段原值；
  - 空值/异常脏数据不覆盖：新爬取值为空时保留库内已有非空值；
  - CMS 管理字段（status/is_recommend/is_hot/is_top/author/weigh）不参与比对与更新；
  - 播放源/分集/属性关联按内容签名判断，签名变化才重建，否则保留；
  - 全程单事务 + SELECT ... FOR UPDATE 行锁，保证原子性。

只读预览：preview_changed（dry-run 专用）
  - 与 save_changed 共用 _diff_fields / _sources_signature 比对规则；
  - 不执行任何写语句、不加行锁，仅返回「将会变更」的字段清单与新旧值预览。
"""
from __future__ import annotations

import json
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


def _preview_val(val, limit: int = 50) -> str:
    """dry-run 预览值：转字符串并截断，避免日志/报告被长文本撑爆。"""
    if val is None:
        return ""
    s = str(val)
    return s if len(s) <= limit else s[:limit] + "…"


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

    # ---------------- 资源全量更新模式：仅更新变更字段 ----------------
    # CMS 管理字段与时间戳（后台手工维护 / 总是刷新，爬虫不参与变更比对）
    _CMS_ONLY_FIELDS = {"author", "status", "is_recommend", "is_hot", "is_top",
                        "weigh", "create_time", "update_time"}

    def save_changed(self, film: Film) -> Tuple[int, str]:
        """资源全量更新：比对库内既有数据，仅对变更字段执行 UPDATE，保留未变更字段原值。

        规则（对应需求「仅对发生变更的字段执行数据库更新，保留未变更字段」）：
          1. 仅比较可爬取字段（排除 CMS 管理字段与主键）；
          2. 新爬取值为空（'' / None / 0）时视为无效脏数据，不覆盖库内已有非空值；
          3. 播放源/分集/属性关联按内容签名比对：签名变化才重建，否则保留（避免无谓写放大）；
          4. 若整片无任何变更（含子表），仅刷新 update_time 并返回 ('unchanged', film_id)。

        返回 (film_id, action)，action ∈ {'update','unchanged'}。
        """
        film.normalize(self._cid_map)
        film.build_seo()
        now = _NOW()
        conn = self._db.connection()
        try:
            with conn.cursor() as cur:
                # 行锁：事务内对目标行加锁，保证「读取旧值 -> 比对 -> 更新」原子性
                cur.execute(
                    f"SELECT * FROM `{T_FILM}` WHERE `source_url`=%s FOR UPDATE",
                    (film.source_url,),
                )
                old = cur.fetchone()
                if old is None:
                    # 库内不存在：按标准新增（更新模式兜底，一般不会走到）
                    film_id = self._upsert_film(cur, film, now)[0]
                    conn.commit()
                    return film_id, "update"

                film_id = old["id"]
                # 1) 主字段变更比对（排除 CMS 管理字段与主键）
                changed = self._diff_fields(old, film)

                # 2) 子表内容签名：播放源/分集/属性关联（签名变化才重建）
                src_sig = self._sources_signature(film)
                old_src_sig = self._read_source_signature(cur, film_id, old)
                src_changed = (old_src_sig != src_sig)

                # 3) 若整片无变更，仅刷新 update_time
                if not changed and not src_changed:
                    cur.execute(
                        f"UPDATE `{T_FILM}` SET `update_time`=%s WHERE `id`=%s",
                        (now, film_id),
                    )
                    conn.commit()
                    return film_id, "unchanged"

                # 4) 仅更新变更字段（保留未变更字段原值）；create_time 一律保留
                if changed:
                    changed["update_time"] = now
                    set_sql = ", ".join(f"`{k}`=%s" for k in changed.keys())
                    cur.execute(f"UPDATE `{T_FILM}` SET {set_sql} WHERE `id`=%s",
                                tuple(changed.values()) + (film_id,))
                else:
                    cur.execute(
                        f"UPDATE `{T_FILM}` SET `update_time`=%s WHERE `id`=%s",
                        (now, film_id),
                    )

                # 5) 子表变更：重建播放源/分集/属性关联（先删后插，与 save 一致）
                if src_changed:
                    cur.execute(f"DELETE FROM `{T_FILM_PLAY_SOURCE}` WHERE `film_id`=%s", (film_id,))
                    cur.execute(f"DELETE FROM `{T_FILM_EPISODE}` WHERE `film_id`=%s", (film_id,))
                    cur.execute(f"DELETE FROM `{T_FILM_ATTR_REL}` WHERE `film_id`=%s", (film_id,))
                    self._write_sources(cur, film, film_id)
                    self._write_attributes(cur, film, film_id)
                    self._write_stars(cur, film)

            conn.commit()
            logger.info("影片变更更新 id=%s 标题=%s 源=%s 变更字段=%d 子表重建=%s",
                        film_id, film.title, film.source_url, len(changed), src_changed)
            return film_id, "update"
        except Exception:
            conn.rollback()
            logger.exception("影片变更更新失败 标题=%s 源=%s", film.title, film.source_url)
            raise

    def preview_changed(self, film: Film) -> Tuple[int, str, List[Dict], bool]:
        """只读变更预览（dry-run 专用）：比对库内数据，返回「将会变更」的字段清单。

        与 save_changed 使用**同一套比对规则**（_diff_fields / _sources_signature），
        但不含任何写操作（无 UPDATE/DELETE/INSERT，也不加 FOR UPDATE 行锁），
        用于真实环境试跑时核验「字段是否匹配、是否全字段误判为变更」。

        返回 (film_id, action, diffs, sources_changed)：
          action ∈ {'update', 'unchanged', 'not_found'}；
          diffs  为 [{'field','old','new'}]（value 已截断，避免日志过长）。
        """
        film.normalize(self._cid_map)
        film.build_seo()
        conn = self._db.connection()
        with conn.cursor() as cur:
            # 只读查询：刻意不使用 FOR UPDATE，避免 dry-run 对生产库加锁
            cur.execute(
                f"SELECT * FROM `{T_FILM}` WHERE `source_url`=%s",
                (film.source_url,),
            )
            old = cur.fetchone()
            if old is None:
                return 0, "not_found", [], False
            changed = self._diff_fields(old, film)
            src_changed = (self._read_source_signature(cur, old["id"], old)
                           != self._sources_signature(film))
            diffs = [
                {"field": col, "old": _preview_val(old.get(col)),
                 "new": _preview_val(val)}
                for col, val in changed.items()
            ]
            action = "update" if (changed or src_changed) else "unchanged"
            return old["id"], action, diffs, src_changed

    def _diff_fields(self, old: Dict, film: Film) -> Dict[str, object]:
        """比对旧行与爬取结果，返回需更新的字段（save_changed / preview_changed 共用）。

        规则：排除 CMS 管理字段与主键；空值脏数据不覆盖库内已有非空值。
        """
        data = film.to_film_row(_NOW())
        changed: Dict[str, object] = {}
        for col, val in data.items():
            if col in self._CMS_ONLY_FIELDS or col == "id":
                continue
            cur_val = old.get(col)
            if not self._value_changed(cur_val, val):
                continue
            # 空值/异常脏数据：不覆盖库内已有非空值（需求「过滤无效空值脏数据」）
            if self._is_blank(val) and not self._is_blank(cur_val):
                continue
            changed[col] = val
        return changed

    # ---- 变更比对辅助 ----
    @staticmethod
    def _value_changed(cur_val, new_val) -> bool:
        """判断新旧值是否实质不同（数字/字符串统一比较，None 视为空串）。"""
        if cur_val is None and new_val in (None, "", 0, 0.0):
            return False
        if new_val in (None, "", 0, 0.0) and cur_val in (None, "", 0, 0.0):
            return False
        return str(cur_val or "") != str(new_val or "")

    @staticmethod
    def _is_blank(val) -> bool:
        return val is None or (isinstance(val, str) and not val.strip())

    @staticmethod
    def _sources_signature(film: Film) -> str:
        """播放源+分集+属性的内容签名：签名一致则视为未变更，跳过重建。

        序列化时剔除 play_url_backup（备份地址）差异，避免仅备份地址变化触发整表重建。
        注意：is_free 统一转 int、year 统一转 str，保证与库内读取侧（
        _read_source_signature）序列化口径一致，否则会产生「永远不相等」的假变更。
        """
        parts = []
        for src in film.sources:
            eps = [(ep.sort, ep.title, ep.play_url, ep.duration,
                    int(ep.is_free or 0))
                   for ep in src.episodes]
            parts.append((src.name, src.from_flag, src.play_type, src.remark, eps))
        body = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
        # 附加维度属性（region/language/year/tags/stars），避免属性变化被漏判
        attrs = json.dumps({
            "region": film.region or "",
            "language": film.language or "",
            "year": str(film.year or ""),
            "tags": list(film.tags or []),
            "stars": list(film.stars or []),
        }, ensure_ascii=False, sort_keys=True, default=str)
        return body + "|" + attrs

    @staticmethod
    def _split_multi(val) -> List[str]:
        """库内「，」分隔的多值字段（tags/stars）拆分为列表，与写侧口径一致。"""
        if not val:
            return []
        return [x.strip() for x in re.split(r"[，,]", str(val)) if x.strip()]

    @staticmethod
    def _read_source_signature(cur, film_id: int, film_row: Dict | None = None) -> str:
        """读取库内既有播放源+分集签名（用于与爬取结果比对）。

        film_row 传入时（save_changed / preview_changed 均已持有旧行），
        同时拼接与 _sources_signature 完全一致的属性段，避免因「写侧带属性、
        读侧不带属性」而恒判定为变更（历史上导致每次更新都重建子表）。
        """
        cur.execute(
            f"SELECT `name`,`sort`,`play_type`,`play_from`,`remark`,`id` "
            f"FROM `{T_FILM_PLAY_SOURCE}` WHERE `film_id`=%s ORDER BY `sort`,`id`",
            (film_id,),
        )
        srcs = cur.fetchall()
        parts = []
        for s in srcs:
            cur.execute(
                f"SELECT `title`,`sort`,`play_url`,`duration`,`is_free` "
                f"FROM `{T_FILM_EPISODE}` WHERE `source_id`=%s ORDER BY `sort`,`id`",
                (s["id"],),
            )
            eps = [(e["sort"], e["title"], e["play_url"], e["duration"],
                    int(e["is_free"] or 0))
                   for e in cur.fetchall()]
            parts.append((s["name"], s["play_from"], s["play_type"], s["remark"], eps))
        body = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
        if film_row is None:
            return body
        attrs = json.dumps({
            "region": film_row.get("region") or "",
            "language": film_row.get("language") or "",
            "year": str(film_row.get("year") or ""),
            "tags": Pipeline._split_multi(film_row.get("tags")),
            "stars": Pipeline._split_multi(film_row.get("stars")),
        }, ensure_ascii=False, sort_keys=True, default=str)
        return body + "|" + attrs

    # ---------------- 内部 ----------------
    def _upsert_film(self, cur, film: Film, now: int) -> Tuple[int, str]:
        # FOR UPDATE：在事务内对目标行（或间隙）加锁，保证“查重 -> 写库”的原子性，
        # 杜绝并发场景下“两个事务同时判定不存在 -> 双插/覆盖错乱”的数据不一致。
        # 注：若 ba_film.source_url 无唯一索引，InnoDB 会退化为表级间隙锁（仍安全，
        # 仅并发度下降）；生产建议对 source_url 建唯一索引以启用行级锁，迁移 SQL 见 README。
        cur.execute(
            f"SELECT `id`,`create_time` FROM `{T_FILM}` "
            f"WHERE `source_url`=%s FOR UPDATE",
            (film.source_url,),
        )
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
