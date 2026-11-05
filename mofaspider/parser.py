"""HTML 解析器（针对魔法国/苹果CMS-STUI 模板）。

三种页面：
  - 列表页  /gangju/news/{type}.html            -> [FilmSummary]
  - 详情页  /gangju/news/dt/{id}.html           -> Film（含播放源与分集元信息，直链待补）
  - 播放页  /gangju/view/{id}-{src}-{ep}.html   -> 直链(m3u8/mp4) + from 线路标识

所有选择器均围绕已探测的真实 DOM 结构，解析失败不抛异常，仅返回空/默认值，
由上层决定重试或跳过，保证单页异常不影响整体。
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .models import Episode, Film, FilmSummary, PlaySource

# 解析器优选：lxml 更快且容错更强；不可用时回退内置 html.parser
try:  # pragma: no cover - 依赖环境相关
    import lxml  # noqa: F401
    _PARSER = "lxml"
except Exception:  # pragma: no cover
    _PARSER = "html.parser"

_SITE_ID_RE = re.compile(r"/dt/(\d+)\.html")


def _abs(url: str, base: str) -> str:
    if not url:
        return ""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return urljoin(base, url)


def parse_listing(html: str, base_url: str) -> List[FilmSummary]:
    """从列表页提取影片摘要列表。"""
    soup = BeautifulSoup(html, _PARSER)
    out: List[FilmSummary] = []
    seen = set()
    # 主网格：带 lazyload 的缩略图锚点
    for a in soup.find_all("a", class_=re.compile(r"stui-vodlist__thumb")):
        href = a.get("href", "")
        m = _SITE_ID_RE.search(href)
        if not m:
            continue
        site_id = m.group(1)
        if site_id in seen:
            continue
        seen.add(site_id)
        title = a.get("title") or (a.get_text(strip=True))
        cover = a.get("data-original") or ""
        cover = _abs(cover, base_url)
        out.append(FilmSummary(
            site_id=site_id,
            title=title or f"未知-{site_id}",
            detail_path=f"/gangju/news/dt/{site_id}.html",
            cover=cover,
        ))
    return out


def _parse_data_lines(soup: BeautifulSoup):
    """解析 <p class="data"> 中的 类型/地区/年份/语言/主演/导演/更新。

    注意：一个 <p> 内常并列多个标签（如“类型：动作 地区：中国大陆 年份：2026”），
    因此按 span.text-muted(标签) 与 a(值) 顺序扫描，而非整段正则。
    """
    tags, stars, region, language, year, director, update_date = (
        [], [], "", "", 0, "", "")
    for p in soup.select("p.data"):
        label = None
        vals = []
        collected = []
        for node in p.find_all(["span", "a"]):
            cls = node.get("class") or []
            if "text-muted" in cls:
                if label is not None:
                    collected.append((label, vals))
                label = node.get_text(strip=True).rstrip("：:").strip()
                vals = []
            elif node.name == "a":
                if label is not None:
                    t = node.get_text(strip=True)
                    if t:
                        vals.append(t)
        if label is not None:
            collected.append((label, vals))

        full_text = p.get_text(" ", strip=True)
        date_m = re.search(r"(\d{4}-\d{2}-\d{2})", full_text)
        for lab, vs in collected:
            if "类型" in lab:
                tags = vs
            elif "地区" in lab:
                region = vs[0] if vs else ""
            elif "语言" in lab:
                language = vs[0] if vs else ""
            elif "年份" in lab:
                for a in vs:
                    mm = re.search(r"(\d{4})", a)
                    if mm:
                        year = int(mm.group(1))
                        break
            elif "主演" in lab:
                stars = vs
            elif "导演" in lab:
                director = vs[0] if vs else ""
            elif "更新" in lab and date_m:
                update_date = date_m.group(1)
        # 兜底：更新日期是纯文本（非 <a>）时，从整段文本取
        if "更新" in full_text and not update_date and date_m:
            update_date = date_m.group(1)
    return tags, stars, region, language, year, director, update_date


def _parse_intro(soup: BeautifulSoup) -> str:
    for panel in soup.select("div.stui-pannel"):
        h3 = panel.find("h3")
        if h3 and "剧情简介" in h3.get_text():
            p = panel.find("p", class_="col-pd")
            if p:
                return p.get_text(" ", strip=True)
    # 兜底：desc 区块
    desc = soup.find("p", class_="desc")
    return desc.get_text(" ", strip=True) if desc else ""


def _parse_images(soup: BeautifulSoup, base_url: str) -> Tuple[str, str, str]:
    thumb = soup.select_one(".stui-content__thumb img.lazyload")
    cover = ""
    if thumb:
        cover = _abs(thumb.get("data-original") or thumb.get("src") or "", base_url)
    return cover, cover, cover  # 站仅提供一张海报，thumb/banner 复用


def _parse_play(soup: BeautifulSoup, base_url: str, site_id: str):
    tabs = soup.select("#play-tablist div.tab-item")
    bodies = soup.select("#play-body ul.stui-content__playlist")
    sources: List[PlaySource] = []
    # 以 tab 数量为准；body 按序对应
    for idx, tab in enumerate(tabs):
        name = tab.get_text(strip=True) or f"线路{idx + 1}"
        ul = bodies[idx] if idx < len(bodies) else None
        eps: List[Episode] = []
        if ul:
            for j, li in enumerate(ul.find_all("li")):
                a = li.find("a")
                if not a:
                    continue
                ep_title = a.get_text(strip=True)
                view_path = a.get("href", "")
                view_url = _abs(view_path, base_url)
                eps.append(Episode(sort=j + 1, title=ep_title or f"第{j + 1}集",
                                   view_url=view_url))
        sources.append(PlaySource(name=name, episodes=eps, sort=idx))
    # 兼容：无 tab 但存在 ul（极少数模板）
    if not sources and bodies:
        for j, li in enumerate(bodies[0].find_all("li")):
            a = li.find("a")
            if not a:
                continue
            sources.append(PlaySource(name="默认线路", sort=0, episodes=[
                Episode(sort=1, title=a.get_text(strip=True) or "正片",
                        view_url=_abs(a.get("href", ""), base_url))]))
    return sources


def parse_detail(html: str, base_url: str, site_id: str,
                 default_type: str = "movie") -> Optional[Film]:
    soup = BeautifulSoup(html, _PARSER)

    h1 = soup.find("h1", class_="title")
    if not h1:
        return None
    score = 0.0
    score_span = h1.find("span", class_="score")
    if score_span:
        try:
            score = float(score_span.get_text(strip=True))
        except ValueError:
            score = 0.0
    title = h1.get_text(" ", strip=True)
    if score_span:
        title = title.replace(score_span.get_text(strip=True), "").strip()
    title = title.strip()

    tags, stars, region, language, year, director, update_date = _parse_data_lines(soup)
    intro = _parse_intro(soup)
    cover, thumb, banner = _parse_images(soup, base_url)
    sources = _parse_play(soup, base_url, site_id)

    film = Film(
        site_id=site_id,
        title=title,
        type=default_type,
        director=director,
        stars=stars,
        tags=tags,
        region=region,
        language=language,
        year=year,
        update_date=update_date,
        intro=intro,
        cover_original=cover,
        thumb_original=thumb,
        banner_original=banner,
        sources=sources,
        source_url=_abs(f"/gangju/news/dt/{site_id}.html", base_url),
        score=score,
    )
    return film


def parse_view(html: str) -> Tuple[Optional[str], str]:
    """从播放页提取直链 url 与 from 线路标识。

    返回 (url, from_flag)。未找到返回 (None, '')。

    注意：player_aaaa 是嵌套 JSON 对象，用整体捕获再取字段易因内部 `}` 截断而失败，
    故直接在整页文本上按键名提取，更稳健。
    """
    from_flag = ""
    fm = re.search(r'"from"\s*:\s*"([^"]*)"', html)
    if fm:
        from_flag = fm.group(1)
    # 仅匹配以 http(s):// 开头的真实直链，排除页面中 var maccms 的 "url":"域名"
    um = re.search(r'"url"\s*:\s*"(https?:\\?/+[^"]*)"', html)
    url = um.group(1).replace("\\/", "/") if um else None
    return (url, from_flag)
