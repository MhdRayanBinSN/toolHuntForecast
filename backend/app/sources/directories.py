import gzip
import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser
from sqlmodel import Session, select

from ..config import get_settings
from ..db import URLSnapshot, engine, utcnow
from ..services.fetcher import fetch
from ..services.safety import canonicalize


def _xml_locations(body: str) -> list[str]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    locations = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == "loc" and element.text:
            locations.append(element.text.strip())
    return locations


def _matches(url: str, directory: dict) -> bool:
    pattern = str(directory.get("tool_page_pattern") or directory.get("url_pattern") or "").strip()
    if pattern:
        return bool(re.search(pattern, url))
    prefix = str(directory.get("path_prefix") or "").strip()
    if prefix:
        return urlsplit(url).path.startswith(prefix)
    return True


def _candidate_from_listing(html: str, listing_url: str, directory: dict) -> dict | None:
    tree = HTMLParser(html)
    title = tree.css_first("title")
    meta_description = ""
    for node in tree.css("meta"):
        key = (node.attributes.get("property") or node.attributes.get("name") or "").lower()
        if key in {"description", "og:description"} and node.attributes.get("content"):
            meta_description = node.attributes["content"].strip()
            break
    listing_host = urlsplit(listing_url).hostname or ""
    for link in tree.css("a[href]"):
        href = urljoin(listing_url, link.attributes.get("href", ""))
        try:
            domain, normalized = canonicalize(href)
        except ValueError:
            continue
        if domain == listing_host.removeprefix("www."):
            continue
        text = link.text(strip=True)
        if not text and not directory.get("allow_unlabelled_outbound"):
            continue
        return {
            "name": text or (title.text(strip=True) if title else domain),
            "url": normalized,
            "tagline": meta_description,
            "description": meta_description,
            "votes": 0,
            "source": str(directory.get("name") or "directory"),
            "source_url": listing_url,
        }
    return None


async def fetch_directories(category: str, keywords=None, limit: int = 30) -> list[dict]:
    del category, keywords
    directories = get_settings().topics.get("directories") or []
    if not isinstance(directories, list):
        return []
    out = []
    for directory in directories:
        if not isinstance(directory, dict) or not directory.get("enabled", True):
            continue
        source = str(directory.get("name") or directory.get("base_url") or "directory")
        sitemap_urls = list(directory.get("sitemaps") or [])
        if not sitemap_urls and directory.get("base_url"):
            base = str(directory["base_url"]).rstrip("/")
            sitemap_urls = [f"{base}/sitemap.xml"]
        discovered = []
        seen_sitemaps = set()
        while sitemap_urls and len(seen_sitemaps) < 50:
            sitemap_url = sitemap_urls.pop(0)
            if sitemap_url in seen_sitemaps:
                continue
            seen_sitemaps.add(sitemap_url)
            status, body, _ = await fetch(sitemap_url)
            if status != 200 or not body:
                continue
            if sitemap_url.endswith(".gz"):
                try:
                    body = gzip.decompress(body.encode("latin1")).decode("utf-8", "replace")
                except OSError:
                    continue
            for location in _xml_locations(body):
                if location.endswith(".xml") or location.endswith(".xml.gz"):
                    sitemap_urls.append(location)
                elif _matches(location, directory):
                    discovered.append(location)
        if not discovered:
            continue
        with Session(engine) as session:
            existing = {
                row.url for row in session.exec(select(URLSnapshot).where(URLSnapshot.source == source)).all()
            }
            first_seen = not existing
            new_urls = [url for url in discovered if url not in existing]
            for url in discovered:
                row = session.exec(select(URLSnapshot).where(URLSnapshot.source == source, URLSnapshot.url == url)).first()
                if row:
                    row.last_seen = utcnow()
                else:
                    row = URLSnapshot(source=source, url=url)
                session.add(row)
            session.commit()
        if first_seen:
            continue
        for listing_url in new_urls[: int(directory.get("limit", limit))]:
            status, html, _ = await fetch(listing_url)
            if status != 200 or not html:
                continue
            candidate = _candidate_from_listing(html, listing_url, directory)
            if candidate:
                out.append(candidate)
    return out
