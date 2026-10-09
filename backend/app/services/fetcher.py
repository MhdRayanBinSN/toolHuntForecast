import httpx
import urllib.robotparser
from urllib.parse import urljoin, urlsplit
from .safety import canonicalize, assert_public_host
from .ratelimit import limiter
from ..config import get_settings

MAX_BYTES=2*1024*1024
_robots: dict[str, urllib.robotparser.RobotFileParser] = {}

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
            host, current=canonicalize(current); assert_public_host(host); await limiter.wait(host)
            if not await _robots_allowed(client,current,host): return 0,"","robots"
            try: response=await client.get(current)
            except httpx.HTTPError: return 0,"","httpx"
            if response.status_code in {301,302,303,307,308}:
                if hop==5 or not response.headers.get("location"): return response.status_code,"","httpx"
                current=urljoin(current,response.headers["location"]); continue
            if "text/html" not in response.headers.get("content-type","").lower(): return response.status_code,"","httpx"
            body=bytes(response.content[:MAX_BYTES]).decode(response.encoding or "utf-8",errors="replace")
            return response.status_code,body,"httpx"
    return 0,"","httpx"

async def resolve_url(url: str) -> str:
    """Resolve a public redirect URL without downloading the destination page."""
    current=url
    async with httpx.AsyncClient(timeout=15, follow_redirects=False, headers={"User-Agent":get_settings().user_agent}) as client:
        for hop in range(6):
            try:
                host,current=canonicalize(current)
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
