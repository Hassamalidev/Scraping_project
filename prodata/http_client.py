"""The one HTTP client every connector uses (spec section 7).

Enforces, for every request:
- a descriptive User-Agent with a contact email
- robots.txt (checked per host, cached for the session)
- a per-domain rate limit (default 1 request/second)
- retries with exponential backoff on 429 / 5xx / network errors (tenacity)
- an on-disk response cache, so nothing is refetched inside its TTL

It never rotates proxies, solves CAPTCHAs or retries past a block: a 401/403 or a
robots.txt disallow raises immediately so the caller can stop and report.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from prodata.logs import log


class FetchBlocked(Exception):
    """The site refused us (robots.txt, 401/403). Stop and report — never work around."""


class RetryableStatus(Exception):
    def __init__(self, status: int, url: str):
        super().__init__(f"HTTP {status} for {url}")
        self.status = status


@dataclass
class CachedResponse:
    url: str
    status: int
    path: Path
    fetched_at: datetime
    from_cache: bool
    content_type: str = ""

    def read_bytes(self) -> bytes:
        return self.path.read_bytes()

    def text(self, encoding: str = "utf-8") -> str:
        return self.path.read_bytes().decode(encoding, errors="replace")

    def json(self):
        return json.loads(self.read_bytes())


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, (RetryableStatus, httpx.TransportError))


class RateLimiter:
    """Minimum interval between requests to the same host."""

    def __init__(
        self,
        max_rps: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.interval = 1.0 / max_rps if max_rps > 0 else 0.0
        self._clock = clock
        self._sleep = sleep
        self._next_slot: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def wait(self, host: str) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = self._clock()
            slot = self._next_slot.get(host, now)
            if slot > now:
                await self._sleep(slot - now)
                now = slot
            self._next_slot[host] = now + self.interval


class HttpClient:
    def __init__(
        self,
        user_agent: str,
        cache_dir: Path,
        max_rps_per_domain: float = 1.0,
        respect_robots_txt: bool = True,
        cache_ttl_seconds: float = 20 * 3600,
        max_attempts: int = 5,
        transport: httpx.AsyncBaseTransport | None = None,
        rate_limiter: RateLimiter | None = None,
        backoff_base_seconds: float = 1,
        backoff_max_seconds: float = 60,
    ):
        if "@" not in user_agent:
            raise ValueError("User-Agent must include a contact email (set CONTACT_EMAIL in .env)")
        self.user_agent = user_agent
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.respect_robots_txt = respect_robots_txt
        self.cache_ttl_seconds = cache_ttl_seconds
        self.max_attempts = max_attempts
        self.backoff_base_seconds = backoff_base_seconds
        self.backoff_max_seconds = backoff_max_seconds
        self.limiter = rate_limiter or RateLimiter(max_rps_per_domain)
        self._robots: dict[str, RobotFileParser] = {}
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            follow_redirects=True,
            timeout=httpx.Timeout(60.0, connect=20.0),
            transport=transport,
        )

    async def __aenter__(self) -> HttpClient:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- cache ----------------------------------------------------------------

    def _cache_paths(self, url: str, headers: dict | None) -> tuple[Path, Path]:
        key = hashlib.sha256((url + json.dumps(headers or {}, sort_keys=True)).encode()).hexdigest()
        return self.cache_dir / f"{key}.body", self.cache_dir / f"{key}.meta.json"

    def _read_cache(self, url: str, headers: dict | None, ttl: float) -> CachedResponse | None:
        body, meta = self._cache_paths(url, headers)
        if not (body.exists() and meta.exists()):
            return None
        info = json.loads(meta.read_text())
        fetched_at = datetime.fromisoformat(info["fetched_at"])
        if (datetime.now(timezone.utc) - fetched_at).total_seconds() > ttl:
            return None
        return CachedResponse(url, info["status"], body, fetched_at, True, info.get("content_type", ""))

    # ---- robots.txt -----------------------------------------------------------

    async def allowed(self, url: str) -> bool:
        if not self.respect_robots_txt:
            return True
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        parser = self._robots.get(origin)
        if parser is None:
            parser = RobotFileParser()
            robots_url = origin + "/robots.txt"
            await self.limiter.wait(parts.netloc)
            try:
                resp = await self._client.get(robots_url)
                if resp.status_code in (401, 403):
                    parser.disallow_all = True
                elif resp.status_code >= 400:
                    parser.allow_all = True
                else:
                    parser.parse(resp.text.splitlines())
            except httpx.TransportError:
                # Can't read robots.txt: be conservative and treat the site as off-limits.
                parser.disallow_all = True
            self._robots[origin] = parser
        return parser.can_fetch(self.user_agent, url)

    # ---- fetch ----------------------------------------------------------------

    async def get(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        ttl_seconds: float | None = None,
    ) -> CachedResponse:
        if params:
            url = f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"
        ttl = self.cache_ttl_seconds if ttl_seconds is None else ttl_seconds
        cached = self._read_cache(url, headers, ttl)
        if cached:
            return cached

        if not await self.allowed(url):
            raise FetchBlocked(f"robots.txt disallows {url}")

        host = urlsplit(url).netloc
        body_path, meta_path = self._cache_paths(url, headers)

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self.max_attempts),
            wait=wait_exponential(
                multiplier=self.backoff_base_seconds, min=self.backoff_base_seconds, max=self.backoff_max_seconds
            ),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        ):
            with attempt:
                await self.limiter.wait(host)
                tmp = body_path.with_suffix(".part")
                async with self._client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code in (401, 403):
                        raise FetchBlocked(f"HTTP {resp.status_code} for {url} — access refused")
                    if resp.status_code == 429 or resp.status_code >= 500:
                        raise RetryableStatus(resp.status_code, url)
                    with tmp.open("wb") as fh:
                        async for chunk in resp.aiter_bytes():
                            fh.write(chunk)
                    status = resp.status_code
                    content_type = resp.headers.get("content-type", "")
                tmp.replace(body_path)

        fetched_at = datetime.now(timezone.utc)
        if status < 400:  # only successful responses are served from cache later
            meta_path.write_text(
                json.dumps({"url": url, "status": status, "fetched_at": fetched_at.isoformat(), "content_type": content_type})
            )
        log.debug("fetched", url=url, status=status)
        return CachedResponse(url, status, body_path, fetched_at, False, content_type)


def build_client(market_config=None, settings=None, **overrides) -> HttpClient:
    """Client wired from config/market.yaml and .env."""
    from prodata.config import get_settings, load_market_config

    market_config = market_config or load_market_config()
    settings = settings or get_settings()
    crawl = market_config.crawl
    kwargs = dict(
        user_agent=market_config.user_agent(settings.contact_email),
        cache_dir=settings.prodata_cache_dir,
        max_rps_per_domain=crawl.max_rps_per_domain,
        respect_robots_txt=crawl.respect_robots_txt,
        cache_ttl_seconds=crawl.cache_ttl_hours * 3600,
    )
    kwargs.update(overrides)
    return HttpClient(**kwargs)
