"""models 模块单元测试：字段截断、枚举校正、拼音首字母、SEO 与落库行生成。"""
from __future__ import annotations

from mofaspider.models import Film, PlaySource, Episode, first_letter, clip


def test_clip_truncates():
    assert clip("ab" * 100, 3) == "aba"
    assert clip(None, 5) == ""
    assert clip("  hi  ", 10) == "hi"


def test_first_letter():
    assert first_letter("后来的我们") == "#"
    assert first_letter("Avatar") == "A"
    assert first_letter("") == "#"


def test_film_normalize_truncation_and_letter():
    f = Film(site_id="1", title="影片" * 60, cover="x" * 300,
             source_url="http://x/" + "a" * 300, type="movie")
    f.normalize({})
    assert len(f.title) == 120
    assert len(f.cover) == 255
    assert len(f.source_url) == 255
    assert f.letter == "#"


def test_film_normalize_type_and_score_bounds():
    f = Film(site_id="1", title="T", type="unknown", score=99.9, year=1800)
    f.normalize({})
    assert f.type == "movie"                       # 非法枚举回退
    assert 0.0 <= f.score <= 9.9                  # 评分上限校正
    assert f.year == 0                            # 年份越界回退


def test_film_normalize_episode_total_and_category():
    f = Film(site_id="1", title="T", type="tv")
    f.sources = [PlaySource(name="默认", episodes=[Episode(sort=1, title="E1", view_url="u1"),
                                                   Episode(sort=2, title="E2", view_url="u2")])]
    f.normalize({"tv": 2})
    assert f.episode_total == 2
    assert f.category_id == 2
    assert f.category_path == "0,2"


def test_film_build_seo_and_row():
    f = Film(site_id="1", title="测试影片", region="中国大陆",
             tags=["动作", "科幻"], intro="这是 一段\n简介 " * 10, type="movie")
    f.normalize({})
    f.build_seo()
    assert f.seo_title == "测试影片"
    assert "中国大陆" in f.seo_keywords
    row = f.to_film_row(123456)
    assert isinstance(row, dict)
    assert row["source_url"] == f.source_url
    assert row["update_time"] == 123456
    assert "title" in row and "cover" in row
