"""数据模型：爬虫解析出的结构化对象，以及写入前的规范化/校验。

规范化严格遵循 ba_film / ba_film_play_source / ba_film_episode 表结构约束：
  - 字符串按 varchar 长度截断（避免 'Data too long'）
  - 枚举值落在允许集合
  - 数值范围校正（score<=9.9, year 4位等）
  - 计算派生字段（letter、category_id、episode_total 等）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

# ba_film 关键字段最大长度（与 SHOW COLUMNS 一致）
FILM_STRLEN = {
    "title": 120, "sub_title": 120, "en_title": 120, "letter": 1,
    "category_path": 100, "cover": 255, "thumb": 255, "banner": 255,
    "director": 120, "stars": 500, "tags": 300, "region": 60, "language": 60,
    "release_date": 30, "duration": 30, "update_remark": 60, "intro": 20000,
    "play_url": 500, "source_url": 255, "author": 30,
    "seo_title": 120, "seo_keywords": 200, "seo_description": 500,
}
FILM_TYPES = {"movie", "tv", "anime", "documentary", "variety"}
EPISODE_STATUS = {"serial", "finish"}
PLAY_TYPES = {"url", "iframe"}

SRC_STRLEN = {"name": 50, "play_from": 50, "remark": 255}
EP_STRLEN = {"title": 80, "play_url": 500, "play_url_backup": 500, "duration": 30}


def clip(text: str, maxlen: int) -> str:
    if text is None:
        return ""
    text = str(text).strip()
    if len(text) > maxlen:
        return text[:maxlen]
    return text


def first_letter(title: str) -> str:
    if not title:
        return "#"
    ch = title[0]
    if ch.isascii() and ch.isalpha():
        return ch.upper()
    return "#"


@dataclass
class Episode:
    sort: int
    title: str
    view_url: str                 # /gangju/view/{id}-{src}-{ep}.html
    play_url: str = ""            # 直链（m3u8/mp4），未提取则为 ''
    play_url_backup: str = ""
    duration: str = ""
    is_free: int = 1

    def normalize(self):
        self.title = clip(self.title, EP_STRLEN["title"])
        self.play_url = clip(self.play_url, EP_STRLEN["play_url"])
        self.play_url_backup = clip(self.play_url_backup, EP_STRLEN["play_url_backup"])
        self.duration = clip(self.duration, EP_STRLEN["duration"])
        self.is_free = 1 if self.is_free else 0
        self.sort = max(0, int(self.sort or 0))


@dataclass
class PlaySource:
    name: str
    from_flag: str = ""           # player_aaaa.from，线路内部标识
    play_type: str = "url"
    sort: int = 0
    remark: str = ""
    episodes: List[Episode] = field(default_factory=list)

    def normalize(self):
        self.name = clip(self.name, SRC_STRLEN["name"]) or "默认线路"
        self.from_flag = clip(self.from_flag, SRC_STRLEN["play_from"])
        self.play_type = self.play_type if self.play_type in PLAY_TYPES else "url"
        self.remark = clip(self.remark, SRC_STRLEN["remark"])
        self.sort = max(0, int(self.sort or 0))
        for ep in self.episodes:
            ep.normalize()


@dataclass
class FilmSummary:
    """列表页提取的轻量信息。"""
    site_id: str
    title: str
    detail_path: str              # /gangju/news/dt/{id}.html
    cover: str = ""               # 缩略图绝对/相对地址
    raw_type: str = ""            # 列表页未知，详情页判定

    @property
    def detail_url(self) -> str:
        return self.detail_path


@dataclass
class Film:
    site_id: str
    title: str
    type: str = "movie"
    sub_title: str = ""
    en_title: str = ""
    director: str = ""
    stars: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    region: str = ""
    language: str = ""
    year: int = 0
    release_date: str = ""
    duration: str = ""
    score: float = 0.0
    score_num: int = 0
    episode_status: str = "serial"
    update_date: str = ""         # 详情页“更新：YYYY-MM-DD”
    intro: str = ""
    category_id: int = 0
    category_path: str = ""
    # 图片（封面/缩略/头图），存最终落盘相对路径
    cover: str = ""
    thumb: str = ""
    banner: str = ""
    cover_original: str = ""      # 源站图片 URL（用于下载）
    thumb_original: str = ""
    banner_original: str = ""
    sources: List[PlaySource] = field(default_factory=list)
    source_url: str = ""          # 详情页地址（去重键）
    author: str = "spider_mofa"
    status: str = "1"
    is_recommend: str = "0"
    is_hot: str = "0"
    is_top: str = "0"
    # SEO 字段（build_seo 自动生成，可被覆盖）
    seo_title: str = ""
    seo_keywords: str = ""
    seo_description: str = ""

    # ---------- 规范化 ----------
    def normalize(self, category_id_map: dict):
        self.title = clip(self.title, FILM_STRLEN["title"])
        self.sub_title = clip(self.sub_title, FILM_STRLEN["sub_title"])
        self.en_title = clip(self.en_title, FILM_STRLEN["en_title"])
        self.director = clip(self.director, FILM_STRLEN["director"])
        self.region = clip(self.region, FILM_STRLEN["region"])
        self.language = clip(self.language, FILM_STRLEN["language"])
        self.release_date = clip(self.release_date, FILM_STRLEN["release_date"])
        self.duration = clip(self.duration, FILM_STRLEN["duration"])
        self.update_remark = clip(self.update_date, FILM_STRLEN["update_remark"])
        self.intro = clip(self.intro, FILM_STRLEN["intro"])
        self.source_url = clip(self.source_url, FILM_STRLEN["source_url"])
        self.author = clip(self.author, FILM_STRLEN["author"])

        # 枚举校正
        if self.type not in FILM_TYPES:
            self.type = "movie"
        self.episode_status = self.episode_status if self.episode_status in EPISODE_STATUS else "serial"
        self.status = "1" if str(self.status) in ("1", "true", "yes") else "0"

        # 数值校正
        try:
            self.score = max(0.0, min(9.9, round(float(self.score or 0), 1)))
        except (TypeError, ValueError):
            self.score = 0.0
        try:
            self.score_num = max(0, int(self.score_num or 0))
        except (TypeError, ValueError):
            self.score_num = 0
        try:
            self.year = int(self.year or 0)
            if not (1900 <= self.year <= 2100):
                self.year = 0
        except (TypeError, ValueError):
            self.year = 0

        # 主演/标签去重、裁剪（stars 总串<=500）
        self.stars = self._dedup_trim(self.stars, FILM_STRLEN["stars"], 8)
        self.tags = self._dedup_trim(self.tags, FILM_STRLEN["tags"], 12)

        # 分类映射
        self.category_id = int(category_id_map.get(self.type, 0))
        self.category_path = clip(f"0,{self.category_id}", FILM_STRLEN["category_path"])

        # 图片字段长度
        self.cover = clip(self.cover, FILM_STRLEN["cover"])
        self.thumb = clip(self.thumb, FILM_STRLEN["thumb"])
        self.banner = clip(self.banner, FILM_STRLEN["banner"])

        # 播放源
        ep_total = 0
        for i, src in enumerate(self.sources):
            src.sort = i
            src.normalize()
            ep_total += len(src.episodes)
        self.episode_total = ep_total

        self.letter = first_letter(self.title)

    @staticmethod
    def _dedup_trim(items, maxlen, per_item):
        out, buf = [], 0
        for it in items:
            it = clip(it, per_item)
            if not it or it in out:
                continue
            if buf + len(it) + (1 if buf else 0) > maxlen:
                break
            out.append(it)
            buf += len(it) + 1
        return out

    # 自动生成 SEO 字段（CMS 前台依赖）
    def build_seo(self):
        if not self.seo_title:
            self.seo_title = clip(self.title, FILM_STRLEN["seo_title"])
        kw = self.tags[:5]
        if self.region:
            kw = [self.region] + kw
        self.seo_keywords = clip("，".join(kw), FILM_STRLEN["seo_keywords"])
        desc = re.sub(r"\s+", " ", self.intro or "").strip()
        self.seo_description = clip(desc[: FILM_STRLEN["seo_description"]], FILM_STRLEN["seo_description"])

    def to_film_row(self, now_ms: int) -> dict:
        """生成 ba_film 插入/更新行（create_time 由调用方在新增时填充）。"""
        return {
            "title": self.title,
            "sub_title": self.sub_title,
            "en_title": self.en_title,
            "letter": self.letter,
            "category_id": self.category_id,
            "category_path": self.category_path,
            "type": self.type,
            "cover": self.cover,
            "thumb": self.thumb,
            "banner": self.banner,
            "director": self.director,
            "stars": "，".join(self.stars),
            "tags": "，".join(self.tags),
            "region": self.region,
            "language": self.language,
            "year": self.year,
            "release_date": self.release_date,
            "duration": self.duration,
            "score": self.score,
            "score_num": self.score_num,
            "episode_total": self.episode_total,
            "episode_status": self.episode_status,
            "update_remark": self.update_remark,
            "intro": self.intro,
            "play_url": self.sources[0].episodes[0].play_url if self.sources and self.sources[0].episodes else "",
            "is_recommend": self.is_recommend,
            "is_hot": self.is_hot,
            "is_top": self.is_top,
            "seo_title": self.seo_title,
            "seo_keywords": self.seo_keywords,
            "seo_description": self.seo_description,
            "source_url": self.source_url,
            "author": self.author,
            "status": self.status,
            "weigh": 0,
            "update_time": now_ms,
        }
