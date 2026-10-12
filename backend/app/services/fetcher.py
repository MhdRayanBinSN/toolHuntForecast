import httpx
import urllib.robotparser
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
from .safety import canonicalize, assert_public_host
from .ratelimit import limiter
from ..config import get_settings

MAX_BYTES=2*1024*1024
CACHE_SECONDS=24*60*60
_robots: dict[str, urllib.robotparser.RobotFileParser] = {}
_cache: dict[str, tuple[float,int,str,str]] = {}


def _canonicalize_fetch_url(url: str) -> tuple[str, str]:
    host, normalized = canonicalize(url)
    original_path = urlsplit(url).path
    if original_path.endswith("/") and original_path not in {"", "/"}:
        parts = urlsplit(normalized)
        normalized = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") + "/", parts.query, ""))
    return host, normalized

async def _robots_allowed(client, url: str, host: str) -> bool:
    if host not in _robots:
        robots_url=f"{urlsplit(url).scheme}://{urlsplit(url).netloc}/robots.txt"
        parser=urllib.robotparser.RobotFileParser(robots_url)
        try:
            response=await client.get(robots_url)
            parser.parse(response.text.splitlines() if response.status_code==200 else [])
        except httpx.HTTPError:
            parser.parse([])
        _robots[host]=parser
    return _robots[host].can_fetch(get_settings().user_agent,url)

async def fetch(url: str) -> tuple[int,str,str]:
    current=url
    async with httpx.AsyncClient(timeout=15, follow_redirects=False, headers={"User-Agent":get_settings().user_agent}) as client:
        for hop in range(6):
            host, current=_canonicalize_fetch_url(current); assert_public_host(host); await limiter.wait(host)
            if not await _robots_allowed(client,current,host): return 0,"","robots"
            cached=_cache.get(current)
            if cached and time.time()-cached[0]<CACHE_SECONDS:
                _,status,body,method=cached
                return status,body,method
            for attempt in range(3):
                try:
                    async with client.stream("GET",current) as response:
                        if response.status_code in {301,302,303,307,308}:
                            if hop==5 or not response.headers.get("location"): return response.status_code,"","httpx"
                            current=urljoin(current,response.headers["location"])
                            break
                        content_type=response.headers.get("content-type","").lower()
                        if not any(kind in content_type for kind in ("text/html","application/xhtml+xml","text/xml","application/xml","application/rss+xml")):
                            return response.status_code,"","httpx"
                        chunks=[]; size=0
                        async for chunk in response.aiter_bytes():
                            size+=len(chunk)
                            if size>MAX_BYTES:
                                return response.status_code,"","too_large"
                            chunks.append(chunk)
                        body=b"".join(chunks).decode(response.encoding or "utf-8",errors="replace")
                        _cache[current]=(time.time(),response.status_code,body,"httpx")
                        return response.status_code,body,"httpx"
                except httpx.HTTPError:
                    if attempt==2: return 0,"","httpx"
                    await limiter.wait(host)
            else:
                continue
    return 0,"","httpx"

async def resolve_url(url: str) -> str:
    """Resolve a public redirect URL without downloading the destination page."""
    current=url
    async with httpx.AsyncClient(timeout=15, follow_redirects=False, headers={"User-Agent":get_settings().user_agent}) as client:
        for hop in range(6):
            try:
                host,current=_canonicalize_fetch_url(current)
                assert_public_host(host)
                await limiter.wait(host)
                if not await _robots_allowed(client,current,host): return ""
                async with client.stream("GET",current) as response:
                    if response.status_code in {301,302,303,307,308}:
                        location=response.headers.get("location")
                        if hop==5 or not location: return ""
                        current=urljoin(current,location)
                        continue
                    if response.status_code>=400: return ""
                    return current
            except (ValueError,httpx.HTTPError):
                return ""
    return ""
