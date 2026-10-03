"""Cached HTTP with retries and per-host rate limiting.

download(url, dest)        -> streams a file to data/raw/... once (idempotent; --refresh re-downloads)
get_json / post_json       -> response bodies cached under data/raw/http_cache/<ns>/<sha1>.json
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from .config import RAW

log = logging.getLogger(__name__)
UA = "RareDiseaseAtlas-pipeline/0.1 (hackathon; contact via repo)"
REFRESH = os.environ.get("ATLAS_REFRESH") == "1"

# minimum seconds between requests per host (NCBI: 3/s without key, 10/s with key)
_MIN_INTERVAL = {
    "eutils.ncbi.nlm.nih.gov": 0.11 if os.environ.get("NCBI_API_KEY") else 0.35,
    "api.reporter.nih.gov": 1.0,
    "clinicaltrials.gov": 0.2,
    "api.brightdata.com": 0.5,
}
_last: dict[str, float] = {}
_lock = threading.Lock()


def _throttle(url: str) -> None:
    host = urlparse(url).hostname or ""
    gap = _MIN_INTERVAL.get(host, 0.0)
    if not gap:
        return
    with _lock:
        wait = _last.get(host, 0) + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last[host] = time.monotonic()


_SECRET_RE = re.compile(r"((?:api_key|apikey|access_token|token|key|email)=)[^&\s'\"]+", re.I)


def redact(text: object) -> str:
    """Mask secret query parameters (api_key=…, email=…) in a URL or exception message before logging."""
    return _SECRET_RE.sub(r"\1***", str(text))


def _safe_url(url: httpx.URL | str) -> str:
    return str(httpx.URL(str(url)).copy_with(query=None))


def _client() -> httpx.Client:
    return httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60, read=300),
                        headers={"User-Agent": UA})


def request(method: str, url: str, *, retries: int = 5, **kw: Any) -> httpx.Response:
    delay = 2.0
    for attempt in range(retries):
        _throttle(url)
        try:
            with _client() as c:
                r = c.request(method, url, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError(f"retryable {r.status_code}", request=r.request, response=r)
            r.raise_for_status()
            return r
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            resp = getattr(e, "response", None)
            if resp is not None and (resp.status_code not in (429, 500, 502, 503, 504) or attempt == retries - 1):
                # httpx's message embeds the full URL incl. the query string (NCBI api_key): rebuild it
                # without the query; `from None` keeps the original message out of tracebacks too
                raise httpx.HTTPStatusError(f"HTTP {resp.status_code} for {method} {_safe_url(resp.request.url)}",
                                            request=resp.request, response=resp) from None
            if attempt == retries - 1:
                raise
            ra = resp.headers.get("Retry-After") if resp is not None else None
            sleep = float(ra) if ra and ra.isdigit() else delay
            log.warning("%s %s failed (%s); retry in %.0fs", method, _safe_url(url), redact(e), sleep)
            time.sleep(sleep)
            delay *= 2
    raise RuntimeError("unreachable")


def download(url: str, dest: Path | str, refresh: bool = False) -> Path:
    """Stream url to dest (relative paths are under data/raw). Cached: skipped if dest exists."""
    dest = Path(dest)
    if not dest.is_absolute():
        dest = RAW / dest
    if dest.exists() and dest.stat().st_size > 0 and not (refresh or REFRESH):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    log.info("downloading %s -> %s", url, dest)
    for attempt in range(4):
        try:
            with _client() as c, c.stream("GET", url) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
            break
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            if attempt == 3:
                raise
            log.warning("download failed (%s), retrying", redact(e))
            time.sleep(5 * (attempt + 1))
    tmp.replace(dest)
    meta = dest.with_suffix(dest.suffix + ".meta.json")
    meta.write_text(json.dumps({"url": url, "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}))
    return dest


def retrieved_at(path: Path) -> str | None:
    meta = path.with_suffix(path.suffix + ".meta.json")
    if meta.exists():
        return json.loads(meta.read_text()).get("retrieved_at")
    return None


def _cache_path(ns: str, key: str, ext: str = "json") -> Path:
    return RAW / "http_cache" / ns / f"{hashlib.sha1(key.encode()).hexdigest()}.{ext}"


# credentials / contact params that do not change the answer: left out of the cache key so keyed and
# keyless runs (and different machines) share one cache. Without them the key is exactly as before.
_UNKEYED_PARAMS = frozenset({"api_key", "email"})


def _cache_key(url: str, params: dict | None) -> str:
    return url + "?" + json.dumps({k: v for k, v in (params or {}).items() if k not in _UNKEYED_PARAMS},
                                  sort_keys=True)


def get_text(url: str, params: dict | None = None, ns: str = "misc", headers: dict | None = None,
             refresh: bool = False, validate: Callable[[str], object] | None = None) -> str:
    """validate(body) runs before the body is cached; if it raises, nothing is cached (and it propagates).
    It also runs on a cache hit: a cached body that fails it (one cached before the check existed) is
    refetched instead of being served forever."""
    p = _cache_path(ns, _cache_key(url, params), "txt")
    if p.exists() and not (refresh or REFRESH):
        body = p.read_text()
        try:
            if validate is not None:
                validate(body)
            return body
        except Exception as e:  # noqa: BLE001 - any validation failure means "not a usable cached body"
            log.warning("cached %s response %s is invalid (%s); refetching", ns, p.name, type(e).__name__)
    r = request("GET", url, params=params, headers=headers)
    if validate is not None:
        validate(r.text)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(r.text)
    return r.text


def get_json(url: str, params: dict | None = None, ns: str = "misc", headers: dict | None = None,
             refresh: bool = False, validate: Callable[[Any], object] | None = None) -> Any:
    """Only a body that parses as JSON (and passes validate(data), e.g. "no API error object") is cached,
    so an HTML outage page or an error served with HTTP 200 is retried next run instead of kept for good."""
    parsed: list[Any] = []

    def check(body: str) -> None:
        data = json.loads(body)
        if validate is not None:
            validate(data)
        parsed.append(data)

    get_text(url, params, ns, headers, refresh, validate=check)
    return parsed[-1]


def post_json(url: str, body: Any, ns: str = "misc", headers: dict | None = None,
              refresh: bool = False, cache_key: str | None = None) -> Any:
    key = url + "#" + (cache_key or json.dumps(body, sort_keys=True))
    p = _cache_path(ns, key)
    if p.exists() and not (refresh or REFRESH):
        return json.loads(p.read_text())
    r = request("POST", url, json=body, headers=headers)
    data = r.json()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data))
    return data
