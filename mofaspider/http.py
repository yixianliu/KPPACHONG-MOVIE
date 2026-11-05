"""HTTP 客户端：生产级防封与重试。

特性：
  - 随机 User-Agent 轮换（内置较新 UA 池，避免单一 UA 被风控）
  - 代理池轮换（SPIDER_PROXY 单代理 + SPIDER_PROXY_POOL 代理池，逐请求随机选路）
  - 自适应限速：依据 429/5xx 触发频率自动放宽间隔，成功后缓慢收敛（避免长期过慢）
  - 限速 + 随机抖动（SPIDER_REQUEST_INTERVAL ± 50%），降低请求规律性
  - 指数退避重试，并尊重服务端 `Retry-After` 头（秒数或 HTTP 日期）
  - 反爬挑战检测：识别 403/验证码/人机校验等，触发可插拔 on_challenge 回调后放弃该请求
  - 仅对幂等 GET 重试；对 429/500/502/503/504 及网络异常自动退避
  - 线程安全：每个线程独立 Session（requests.Session 非线程安全）；限速/自适应状态加锁
"""
from __future__ import annotations

import email.utils
import random
import re
import threading
import time
from typing import Callable, List, Optional

import urllib3

import requests

# 抑制 InsecureRequestWarning：目标站点证书链不完整/自签名时通过 verify_ssl=False 跳过校验。
# 仅屏蔽此警告类型，不影响其他警告输出。
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from .config import SpiderConfig
from .logger import get_logger

logger = get_logger()

# 较新的 UA 池（覆盖 Chrome / Edge / Firefox / Safari / 移动端），降低被指纹识别的概率
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
]

_RETRY_STATUS = {429, 500, 502, 503, 504}
# 反爬挑战特征（命中即视为被拦截，交由 on_challenge 处理并放弃该请求）
_CHALLENGE_RE = re.compile(
    r"captcha|验证码|人机|robot|access denied|cloudflare|请完成安全验证",
    re.IGNORECASE,
)

_LOCK = threading.Lock()          # 限速 / 自适应状态全局锁
_LAST_REQ = 0.0
_INTERVAL_MULT = 1.0              # 自适应倍率（>=1 表示被限流后放宽）
_MIN_MULT = 0.5
_MAX_MULT = 8.0


class HttpClient:
    def __init__(self, cfg: SpiderConfig, on_challenge: Optional[Callable[[str, int], None]] = None):
        self._cfg = cfg
        self._verify = bool(cfg.verify_ssl)
        self._on_challenge = on_challenge
        # 合并单代理与代理池，去重保序
        pool: List[str] = []
        if cfg.proxy:
            pool.append(cfg.proxy)
        pool += list(cfg.proxy_pool or [])
        self._proxies = list(dict.fromkeys(p for p in pool if p))  # 去重
        self._local = threading.local()
        self._default_headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        }
        if self._proxies:
            logger.info("HTTP 客户端启用代理池，共 %d 个代理（逐请求轮换）", len(self._proxies))
        if not self._verify:
            logger.debug("TLS 证书校验已关闭（源站自签/证书链不完整）")

    # ---- 线程局部 Session ----
    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update(self._default_headers)
            self._local.session = s
        return s

    def _pick_proxy(self) -> Optional[dict]:
        if not self._proxies:
            return None
        p = random.choice(self._proxies)
        return {"http": p, "https": p}

    # ---- 限速 ----
    def _rate_limit(self):
        """全局限速：保证相邻请求间隔 >= interval（含自适应倍率与随机抖动）。"""
        interval = self._cfg.request_interval
        if interval <= 0:
            return
        if self._cfg.adaptive_interval:
            interval *= _INTERVAL_MULT
        jitter = interval * 0.5 * random.random()          # 0 ~ 0.5*interval
        wait = interval * 0.5 + jitter                     # 0.5~1.0 * interval
        with _LOCK:
            global _LAST_REQ
            now = time.monotonic()
            sleep_for = (_LAST_REQ + wait) - now
            if sleep_for > 0:
                time.sleep(sleep_for)
            _LAST_REQ = time.monotonic()

    @staticmethod
    def _bump_interval():
        """被限流/报错时放宽后续间隔。"""
        global _INTERVAL_MULT
        with _LOCK:
            _INTERVAL_MULT = min(_MAX_MULT, _INTERVAL_MULT * 1.5)

    @staticmethod
    def _relax_interval():
        """成功后缓慢收敛间隔（向 1.0 靠拢，不低于下限）。"""
        global _INTERVAL_MULT
        with _LOCK:
            if _INTERVAL_MULT > 1.0:
                _INTERVAL_MULT = max(1.0, _INTERVAL_MULT * 0.9)

    @staticmethod
    def _parse_retry_after(resp) -> Optional[float]:
        ra = resp.headers.get("Retry-After")
        if not ra:
            return None
        try:
            return max(0.0, float(ra))
        except ValueError:
            dt = email.utils.parsedate_to_datetime(ra)
            if dt is not None:
                return max(0.0, (dt.timestamp() - time.time()))
            return None

    def _rotate_ua(self) -> str:
        ua = random.choice(_USER_AGENTS)
        self._session().headers["User-Agent"] = ua
        return ua

    def get(self, url: str, *, referer: Optional[str] = None,
            retries: Optional[int] = None, timeout: Optional[int] = None,
            is_text: bool = True):
        """发起 GET，返回 (ok, content, status, final_url)。

        content: 成功且 is_text=True 时为 str；is_text=False 时为 bytes。
        ok=False 时 content 为 None，status 为实际状态码（或 0 表示网络失败）。
        """
        retries = self._cfg.retry_times if retries is None else retries
        timeout = self._cfg.timeout if timeout is None else timeout
        proxies = self._pick_proxy()
        last_err = None
        for attempt in range(1, retries + 1):
            self._rate_limit()
            ua = self._rotate_ua()
            headers = {}
            if referer:
                headers["Referer"] = referer
            try:
                resp = self._session().get(
                    url, headers=headers, proxies=proxies,
                    timeout=timeout, verify=self._verify,
                )
            except requests.exceptions.RequestException as e:
                last_err = e
                logger.warning("请求异常 %s (尝试 %d/%d, UA=%s): %s",
                               url, attempt, retries, ua[:24], e)
                self._bump_interval()
                self._backoff(attempt)
                continue

            # 反爬挑战检测（验证码/人机校验/拦截页）
            if resp.status_code in (401, 403) or resp.status_code == 429:
                snippet = resp.text[:2000] if is_text else ""
                if _CHALLENGE_RE.search(snippet):
                    logger.error("疑似触发反爬挑战 %s status=%s", url, resp.status_code)
                    if self._on_challenge:
                        try:
                            self._on_challenge(url, resp.status_code)
                        except Exception:
                            logger.exception("on_challenge 回调异常")
                    resp.close()
                    return False, None, resp.status_code, resp.url

            if resp.status_code in _RETRY_STATUS:
                retry_after = self._parse_retry_after(resp)
                last_err = f"HTTP {resp.status_code}"
                logger.warning("服务端限流/错误 %s (尝试 %d/%d, Retry-After=%s): %s",
                               url, attempt, retries, retry_after, resp.status_code)
                resp.close()
                self._bump_interval()
                self._backoff(attempt, retry_after)
                continue

            if resp.status_code != 200:
                logger.error("非预期状态码 %s: %s", url, resp.status_code)
                resp.close()
                return False, None, resp.status_code, resp.url

            # 成功：苹果CMS 站点统一 UTF-8，直接置编码避免误判
            self._relax_interval()
            if is_text:
                resp.encoding = "utf-8"
                return True, resp.text, 200, resp.url
            return True, resp.content, 200, resp.url

        logger.error("请求最终失败 %s: %s", url, last_err)
        return False, None, 0, url

    def _backoff(self, attempt: int, retry_after: Optional[float] = None):
        base = self._cfg.retry_backoff
        wait = base ** attempt + random.uniform(0, 1.0)
        if retry_after:
            wait = max(wait, retry_after)
        time.sleep(min(wait, 60))
