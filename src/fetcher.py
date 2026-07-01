"""Fetch layer: a single choke point for all network access.

Centralizing fetches here is what makes the politeness + resilience story real:
every request passes through one rate limiter, one retry policy, and one robots
check. Agents never touch the network directly — they ask the Fetcher.

Strategy (recon-driven): Safco's listing and detail pages are server-rendered
(products + JSON-LD present in raw HTML), so the default path is httpx — fast and
browser-free. Playwright is launched lazily and only when a page needs JS
(config render_mode=always, or auto-escalation when httpx HTML looks too small).
The production scale path (async pool / distributed workers) is in the README;
this interface would not change.
"""

from __future__ import annotations

import time
import urllib.robotparser as robotparser
from dataclasses import dataclass
from typing import Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .logging_setup import get_logger

log = get_logger("fetcher")


class FetchError(Exception):
    """Retryable fetch failure."""


@dataclass
class FetchResult:
    url: str
    html: str
    status: int = 200
    rendered: bool = False   # True if a browser was used


class RateLimiter:
    """Simple monotonic-clock spacing between requests to the same host."""

    def __init__(self, min_delay_seconds: float):
        self.min_delay = min_delay_seconds
        self._last = 0.0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last
        if elapsed < self.min_delay:
            time.sleep(self.min_delay - elapsed)
        self._last = time.monotonic()


class Fetcher:
    def __init__(self, cfg):
        self.cfg = cfg
        self.limiter = RateLimiter(cfg.crawl.min_delay_seconds)
        self._robots: Optional[robotparser.RobotFileParser] = None
        self._client: Optional[httpx.Client] = None
        # Playwright handles are created lazily on first render() call.
        self._pw = None
        self._browser = None
        self._context = None

    # --- lifecycle --------------------------------------------------------
    def __enter__(self) -> "Fetcher":
        self._client = httpx.Client(
            timeout=self.cfg.crawl.timeout_seconds,
            headers={"User-Agent": self.cfg.crawl.user_agent},
            follow_redirects=True,
        )
        if self.cfg.crawl.respect_robots:
            self._load_robots()
        return self

    def __exit__(self, *exc) -> None:
        if self._client:
            self._client.close()
        for closer in (self._context, self._browser):
            try:
                if closer:
                    closer.close()
            except Exception:
                pass
        if self._pw:
            self._pw.stop()

    def _ensure_browser(self) -> None:
        """Lazily start Playwright the first time JS rendering is needed."""
        if self._context is not None:
            return
        from playwright.sync_api import sync_playwright  # local import: optional dep

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._context = self._browser.new_context(
            user_agent=self.cfg.crawl.user_agent
        )
        self._context.set_default_timeout(self.cfg.crawl.timeout_seconds * 1000)
        log.info("browser_started")

    # --- robots -----------------------------------------------------------
    def _load_robots(self) -> None:
        robots_url = self.cfg.abs_url("/robots.txt")
        rp = robotparser.RobotFileParser()
        try:
            txt = httpx.get(robots_url, timeout=10).text
            rp.parse(txt.splitlines())
            self._robots = rp
            log.info("robots_loaded", url=robots_url)
        except Exception as e:
            log.warning("robots_load_failed", url=robots_url, error=str(e))
            self._robots = None

    def allowed(self, url: str) -> bool:
        if not self.cfg.crawl.respect_robots or self._robots is None:
            return True
        return self._robots.can_fetch(self.cfg.crawl.user_agent, url)

    # --- public fetch -----------------------------------------------------
    def get(self, url: str, render: Optional[bool] = None,
            wait_selector: Optional[str] = None) -> FetchResult:
        """Fetch a page. Chooses httpx vs browser per config/heuristics.

        render=None  -> honor config.render_mode (auto | never | always)
        render=True  -> force browser
        render=False -> force httpx
        """
        if not self.allowed(url):
            raise FetchError(f"blocked by robots.txt: {url}")

        mode = self.cfg.crawl.render_mode
        force_browser = render is True or mode == "always"
        force_http = render is False or mode == "never"

        if force_browser and not force_http:
            return self._get_rendered(url, wait_selector)

        result = self._get_http(url)
        # auto-escalation: server returned suspiciously little HTML -> try JS
        if (not force_http and mode == "auto"
                and len(result.html) < self.cfg.crawl.render_min_bytes):
            log.info("auto_escalate_render", url=url, bytes=len(result.html))
            return self._get_rendered(url, wait_selector)
        return result

    # --- httpx path -------------------------------------------------------
    def _get_http(self, url: str) -> FetchResult:
        @retry(
            retry=retry_if_exception_type(FetchError),
            stop=stop_after_attempt(self.cfg.crawl.max_retries),
            wait=wait_exponential(multiplier=self.cfg.crawl.backoff_base_seconds),
            reraise=True,
        )
        def _do() -> FetchResult:
            self.limiter.wait()
            try:
                r = self._client.get(url)
                if r.status_code >= 500:
                    raise FetchError(f"server {r.status_code} for {url}")
                r.raise_for_status()
                log.info("fetched", url=url, status=r.status_code,
                         bytes=len(r.text), mode="http")
                return FetchResult(url=url, html=r.text, status=r.status_code)
            except FetchError:
                raise
            except Exception as e:
                raise FetchError(f"{type(e).__name__}: {e}") from e

        return _do()

    # --- Playwright path --------------------------------------------------
    def _get_rendered(self, url: str, wait_selector: Optional[str]) -> FetchResult:
        self._ensure_browser()

        @retry(
            retry=retry_if_exception_type(FetchError),
            stop=stop_after_attempt(self.cfg.crawl.max_retries),
            wait=wait_exponential(multiplier=self.cfg.crawl.backoff_base_seconds),
            reraise=True,
        )
        def _do() -> FetchResult:
            self.limiter.wait()
            page = self._context.new_page()
            try:
                resp = page.goto(url, wait_until="domcontentloaded")
                if wait_selector:
                    try:
                        page.wait_for_selector(wait_selector, timeout=8000)
                    except Exception:
                        pass  # selector may legitimately be absent
                html = page.content()
                status = resp.status if resp else 0
                if status >= 500 or not html:
                    raise FetchError(f"bad response {status} for {url}")
                log.info("fetched", url=url, status=status,
                         bytes=len(html), mode="browser")
                return FetchResult(url=url, html=html, status=status, rendered=True)
            except FetchError:
                raise
            except Exception as e:
                raise FetchError(f"{type(e).__name__}: {e}") from e
            finally:
                page.close()

        return _do()

    # --- lightweight fetch (sitemaps, robots) ----------------------------
    def get_text(self, url: str) -> str:
        self.limiter.wait()
        r = self._client.get(url)
        r.raise_for_status()
        return r.text
