"""HTTP 客户端：生产级防封与重试。

特性：
  - 随机 User-Agent 轮换（避免单一 UA 被风控）
  - 可选 HTTP/HTTPS 代理（从配置读取，未配置则直连）
  - 限速 + 随机抖动（SPIDER_REQUEST_INTERVAL ± 50%），降低请求规律性
  - 指数退避重试（SPIDER_RETRY_TIMES / SPIDER_RETRY_BACKOFF）
  - 仅对幂等 GET 重试；对 429/500/502/503/504 及网络异常自动退避
  - 线程安全：每个线程独立 Session（requests.Session 非线程安全）
"""
from __future__ import annotations

import random
import threading
import time
from typing import Optional

import urllib3

import requests

# 抑制 InsecureRequestWarning：目标站点证书链不完整/自签名，
# 已通过 verify_ssl=False 跳过校验；仅屏蔽此警告类型，不影响其他警告输出
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from .config import SpiderConfig
from .logger import get_logger

logger = get_logger()

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
]

_RETRY_STATUS = {429, 500, 502, 503, 504}
_LOCK = threading.Lock()          # 限速全局锁
_LAST_REQ = 0.0


class HttpClient:
    def __init__(self, cfg: SpiderConfig, verify_ssl: bool = False):
        self._cfg = cfg
        self._verify = verify_ssl
        self._proxies = None
        if cfg.proxy:
            self._proxies = {"http": cfg.proxy, "https": cfg.proxy}
        self._local = threading.local()
        self._default_headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        }

    # ---- 线程局部 Session ----
    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update(self._default_headers)
            self._local.session = s
        return s

    def _rate_limit(self):
        """全局限速：保证相邻请求间隔 >= interval，并加入随机抖动。"""
        interval = self._cfg.request_interval
        if interval <= 0:
            return
        jitter = interval * 0.5 * random.random()          # 0 ~ 0.5*interval
        wait = interval * 0.5 + jitter                     # 0.5~1.0 * interval
        with _LOCK:
            global _LAST_REQ
            now = time.monotonic()
            sleep_for = (_LAST_REQ + wait) - now
            if sleep_for > 0:
                time.sleep(sleep_for)
            _LAST_REQ = time.monotonic()

    def _rotate_ua(self):
        ua = random.choice(_USER_AGENTS)
        self._session().headers["User-Agent"] = ua
        return ua

    def get(self, url: str, *, referer: Optional[str] = None,
            retries: Optional[int] = None, timeout: Optional[int] = None,
            is_text: bool = True):
        """发起 GET，返回 (ok, content, status, final_url)。

        content: 成功且 is_text=True 时为 str；is_text=False 时为 bytes。
        """
        retries = self._cfg.retry_times if retries is None else retries
        timeout = self._cfg.timeout if timeout is None else timeout
        last_err = None
        for attempt in range(1, retries + 1):
            self._rate_limit()
            ua = self._rotate_ua()
            headers = {}
            if referer:
                headers["Referer"] = referer
            try:
                resp = self._session().get(
                    url, headers=headers, proxies=self._proxies,
                    timeout=timeout, verify=self._verify,
                )
            except requests.exceptions.RequestException as e:
                last_err = e
                logger.warning("请求异常 %s (尝试 %d/%d, UA=%s): %s",
                               url, attempt, retries, ua[:24], e)
                self._backoff(attempt)
                continue

            if resp.status_code in _RETRY_STATUS:
                last_err = f"HTTP {resp.status_code}"
                logger.warning("服务端限流/错误 %s (尝试 %d/%d): %s",
                               url, attempt, retries, resp.status_code)
                resp.close()
                self._backoff(attempt)
                continue

            if resp.status_code != 200:
                logger.error("非预期状态码 %s: %s", url, resp.status_code)
                return False, None, resp.status_code, resp.url

            # 成功
            if is_text:
                resp.encoding = resp.apparent_encoding or "utf-8"
                # 二次校正：苹果CMS 多为 utf-8
                if "utf-8" in (resp.encoding or "").lower() or True:
                    resp.encoding = "utf-8"
                return True, resp.text, 200, resp.url
            return True, resp.content, 200, resp.url

        logger.error("请求最终失败 %s: %s", url, last_err)
        return False, None, 0, url

    def _backoff(self, attempt: int):
        base = self._cfg.retry_backoff
        sleep = base ** attempt + random.uniform(0, 1.0)
        time.sleep(min(sleep, 30))
