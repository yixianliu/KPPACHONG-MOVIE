"""http 模块单元测试：Retry-After 解析、代理池轮换、反爬挑战检测、成功/失败路径。"""
from __future__ import annotations

import email.utils
import time

from mofaspider.http import HttpClient


class _StubResp:
    def __init__(self, status, text="", headers=None, url="http://x/1"):
        self.status_code = status
        self.text = text
        self.headers = headers or {}
        self.url = url

    def close(self):
        pass


class _StubSession:
    def __init__(self, resp):
        self.headers: dict = {}
        self._resp = resp

    def get(self, *args, **kwargs):
        return self._resp


def test_parse_retry_after_seconds():
    assert HttpClient._parse_retry_after(_StubResp(429, headers={"Retry-After": "5"})) == 5.0


def test_parse_retry_after_httpdate():
    future = time.time() + 10
    hdr = email.utils.formatdate(future, usegmt=True)
    val = HttpClient._parse_retry_after(_StubResp(429, headers={"Retry-After": hdr}))
    assert 5 < val < 15


def test_parse_retry_after_missing():
    assert HttpClient._parse_retry_after(_StubResp(429)) is None


def test_proxy_pool_merge_and_pick(spider_cfg):
    spider_cfg.proxy = "http://single"
    spider_cfg.proxy_pool = ["http://a", "http://b"]
    client = HttpClient(spider_cfg)
    assert set(client._proxies) == {"http://single", "http://a", "http://b"}
    picked = client._pick_proxy()
    assert picked["http"] in client._proxies
    assert picked["https"] == picked["http"]


def test_proxy_pool_empty(spider_cfg):
    client = HttpClient(spider_cfg)
    assert client._pick_proxy() is None


def test_challenge_detection_invokes_callback(spider_cfg):
    called = {}

    def on_ch(url, status):
        called["url"] = url
        called["status"] = status

    client = HttpClient(spider_cfg, on_challenge=on_ch)
    client._session = lambda: _StubSession(
        _StubResp(403, text="<html>验证码 captcha</html>", url="http://x/blocked"))
    ok, content, status, final = client.get("http://x/blocked")
    assert ok is False
    assert status == 403
    assert called.get("status") == 403
    assert final == "http://x/blocked"


def test_get_success_text(spider_cfg):
    client = HttpClient(spider_cfg)
    client._session = lambda: _StubSession(_StubResp(200, text="<html>ok</html>"))
    ok, content, status, final = client.get("http://x/ok", is_text=True)
    assert ok is True and status == 200 and content == "<html>ok</html>"


def test_get_non_200_no_challenge(spider_cfg):
    client = HttpClient(spider_cfg)
    client._session = lambda: _StubSession(_StubResp(404, text="not found"))
    ok, content, status, _ = client.get("http://x/missing")
    assert ok is False and status == 404
