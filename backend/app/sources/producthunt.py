import httpx
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from sqlmodel import Session, select
from ..config import get_settings
from ..db import Candidate, engine
from ..services.fetcher import resolve_url
from ..services.safety import canonicalize

log = logging.getLogger(__name__)

TOPICS_QUERY = '''query($query:String!) { topics(query:$query, first:5) { edges { node { slug postsCount } } } }'''
QUERY='''query($first:Int!, $after:String, $postedAfter:DateTime, $topic:String) { posts(first:$first, after:$after, order:NEWEST, postedAfter:$postedAfter, topic:$topic) { pageInfo { endCursor hasNextPage } edges { node { name tagline description website url votesCount createdAt topics(first:5) { edges { node { name slug } } } } } } }'''

_GENERIC_WORDS = {"software", "tool", "tools", "app", "apps", "platform", "platforms", "service", "services", "system", "systems", "solution", "solutions", "free", "web", "online", "new", "product", "products", "ai", "best", "pro", "plus", "hub"}

def _matches_category(post, category, keywords):
    topic_names = [t.get("node", {}).get("name", "") for t in post.get("topics", {}).get("edges", [])]
    text=" ".join([(post.get(key) or "") for key in ("name","tagline","description")] + topic_names).casefold()
    phrases=[category.strip(),*(str(k).strip() for k in keywords if str(k).strip())]
    # 1. Exact phrase match in post text (e.g. "cold email", "sales outreach")
    for phrase in phrases:
        p = phrase.casefold().strip()
        if p and len(p) >= 3 and p in text:
            return True
    # 2. Distinctive words matching
    def normalize_word(word):
        if word.endswith("ies") and len(word)>4: return word[:-3]+"y"
        if word.endswith("s") and not word.endswith(("ss","us")) and len(word)>4: return word[:-1]
        return word
    text_words={normalize_word(word) for word in re.findall(r"[a-z0-9]+", text)}
    for phrase in phrases:
        phrase_words=[normalize_word(w) for w in re.findall(r"[a-z0-9]+", phrase.casefold()) if len(w) > 2 and w not in _GENERIC_WORDS]
        if not phrase_words:
            continue
        if len(phrase_words) >= 2:
            matched_count = sum(w in text_words for w in phrase_words)
            if matched_count >= min(2, len(phrase_words)):
                return True
        elif phrase_words[0] in text_words and len(phrase_words[0]) >= 5:
            return True
    return False

async def _discover_topics(client, token, category, terms):
    found = []
    for term in [category, *terms][:4]:
        clean = " ".join(term.split())
        if not clean or len(clean) < 3: continue
        try:
            r = await client.post("https://api.producthunt.com/v2/api/graphql",
                                  headers={"Authorization": f"Bearer {token}"},
                                  json={"query": TOPICS_QUERY, "variables": {"query": clean}})
            if r.status_code == 200:
                edges = r.json().get("data", {}).get("topics", {}).get("edges", [])
                for edge in edges:
                    slug = edge.get("node", {}).get("slug")
                    if slug and slug not in found:
                        found.append(slug)
        except Exception:
            pass
    return found

async def fetch(category: str, keywords=None, topic_slugs=None, since_days: int=365):
    token=get_settings().producthunt_token
    if not token:
        log.warning("[ProductHunt] No PRODUCTHUNT_TOKEN configured — skipping discovery")
        return []
    log.info("[ProductHunt] Starting fetch for category=%r keywords=%r topic_slugs=%r since_days=%d", category, keywords, topic_slugs, since_days)
    posts=[]; posted=(datetime.now(timezone.utc)-timedelta(days=since_days)).isoformat()
    terms=list(dict.fromkeys([category.strip(),*(str(x).strip() for x in (keywords or []) if str(x).strip())]))
    topics=list(dict.fromkeys(str(x).strip() for x in (topic_slugs or []) if str(x).strip()))
    async with httpx.AsyncClient(timeout=20) as c:
        if not topics:
            discovered = await _discover_topics(c, token, category, terms)
            if discovered:
                log.info("[ProductHunt] Discovered topic slugs dynamically from API: %r", discovered)
                topics = discovered
        if not topics:
            topics = [None]
        async def collect(topic):
            found=[]; after=None
            for page_num in range(4):
                r=await c.post("https://api.producthunt.com/v2/api/graphql",headers={"Authorization":f"Bearer {token}"},json={"query":QUERY,"variables":{"first":50,"after":after,"postedAfter":posted,"topic":topic}})
                r.raise_for_status(); payload=r.json(); conn=payload.get("data",{}).get("posts",{})
                edges=conn.get("edges",[])
                matched_on_page=0
                for edge in edges:
                    p=edge.get("node",{})
                    if p.get("website") and _matches_category(p,category,terms):
                        found.append(p)
                        matched_on_page+=1
                log.info("[ProductHunt]   topic=%r page=%d  raw_posts=%d  category_matched=%d", topic, page_num+1, len(edges), matched_on_page)
                info=conn.get("pageInfo",{}); after=info.get("endCursor")
                if not info.get("hasNextPage") or not after: break
            log.info("[ProductHunt]   topic=%r total matched: %d posts", topic, len(found))
            return found
        for topic in topics:
            try: posts.extend(await collect(topic))
            except httpx.HTTPStatusError:
                if topic is None: raise
                log.warning("[ProductHunt] Topic %r returned HTTP error, falling back to general query", topic)
                posts.extend(await collect(None))
                break
        if topics[0] is not None and len(posts)<2:
            log.info("[ProductHunt] Too few results from topic queries (%d), running general fallback", len(posts))
            posts.extend(await collect(None))
    log.info("[ProductHunt] Raw matched posts (before dedup): %d", len(posts))
    for i, p in enumerate(posts):
        log.info("[ProductHunt]   [%d] name=%r  tagline=%r  votes=%s  website=%s", i+1, p.get("name",""), p.get("tagline","")[:80], p.get("votesCount",0), p.get("website",""))
    with Session(engine) as s:
        cached={c.name.strip().casefold():c.url for c in s.exec(select(Candidate)).all() if c.name and c.url}
    urls=[cached.get(p.get("name","").strip().casefold()) for p in posts]
    pending=[i for i,url in enumerate(urls) if not url]
    log.info("[ProductHunt] Resolving %d URLs (%d cached)", len(pending), len(posts)-len(pending))
    resolved=await asyncio.gather(*(resolve_url(posts[i]["website"]) for i in pending))
    for i,url in zip(pending,resolved): urls[i]=url
    out=[]; seen_domains=set()
    for p,url in zip(posts,urls):
        if not url: continue
        try: domain,url=canonicalize(url)
        except ValueError: continue
        if domain=="producthunt.com" or domain in seen_domains: continue
        seen_domains.add(domain)
        topics=[t.get("node",{}).get("name","") for t in p.get("topics",{}).get("edges",[]) if t.get("node",{}).get("name")]
        out.append({"name":p.get("name",""),"url":url,"tagline":p.get("tagline",""),"description":p.get("description",""),"votes":p.get("votesCount",0),"ph_url":p.get("url",""),"topics":topics,"created_at":p.get("createdAt",""),"source":"Product Hunt"})
    log.info("[ProductHunt] Final output: %d unique products", len(out))
    for i, product in enumerate(out):
        log.info("[ProductHunt]   [%d] %s  (%s)  votes=%d  tagline=%r", i+1, product["name"], product["url"], product["votes"], product["tagline"][:80])
    return out
