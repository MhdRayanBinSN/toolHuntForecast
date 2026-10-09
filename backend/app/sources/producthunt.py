import httpx
import asyncio
import re
from datetime import datetime, timedelta, timezone
from sqlmodel import Session, select
from ..config import get_settings
from ..db import Candidate, engine
from ..services.fetcher import resolve_url
from ..services.safety import canonicalize

QUERY='''query($first:Int!, $after:String, $postedAfter:DateTime, $topic:String) { posts(first:$first, after:$after, order:NEWEST, postedAfter:$postedAfter, topic:$topic) { pageInfo { endCursor hasNextPage } edges { node { name tagline description website url votesCount createdAt } } } }'''

def _matches_category(post, category, keywords):
    text=" ".join((post.get(key) or "") for key in ("name","tagline","description")).casefold()
    phrases=[category,*keywords]
    if any(phrase.casefold().strip() in text for phrase in phrases if phrase.strip()): return True
    words=set(re.findall(r"[a-z0-9]+",text))
    return any(word in words for phrase in phrases for word in re.findall(r"[a-z0-9]+",phrase.casefold()) if len(word)>2)

async def fetch(category: str, keywords=None, topic_slugs=None, since_days: int=365):
    token=get_settings().producthunt_token
    if not token: return []
    posts=[]; posted=(datetime.now(timezone.utc)-timedelta(days=since_days)).isoformat()
    terms=list(dict.fromkeys([category.strip(),*(str(x).strip() for x in (keywords or []) if str(x).strip())]))
    topics=list(dict.fromkeys(str(x).strip() for x in (topic_slugs or []) if str(x).strip())) or [None]
    async with httpx.AsyncClient(timeout=20) as c:
        async def collect(topic):
            found=[]; after=None
            for _ in range(4):
                r=await c.post("https://api.producthunt.com/v2/api/graphql",headers={"Authorization":f"Bearer {token}"},json={"query":QUERY,"variables":{"first":50,"after":after,"postedAfter":posted,"topic":topic}})
                r.raise_for_status(); payload=r.json(); conn=payload.get("data",{}).get("posts",{})
                for edge in conn.get("edges",[]):
                    p=edge.get("node",{})
                    if p.get("website") and _matches_category(p,category,terms): found.append(p)
                info=conn.get("pageInfo",{}); after=info.get("endCursor")
                if not info.get("hasNextPage") or not after: break
            return found
        for topic in topics:
            try: posts.extend(await collect(topic))
            except httpx.HTTPStatusError:
                if topic is None: raise
                posts.extend(await collect(None))
                break
        if topics[0] is not None and len(posts)<2:
            posts.extend(await collect(None))
    with Session(engine) as s:
        cached={c.name.strip().casefold():c.url for c in s.exec(select(Candidate)).all() if c.name and c.url}
    urls=[cached.get(p.get("name","").strip().casefold()) for p in posts]
    pending=[i for i,url in enumerate(urls) if not url]
    resolved=await asyncio.gather(*(resolve_url(posts[i]["website"]) for i in pending))
    for i,url in zip(pending,resolved): urls[i]=url
    out=[]; seen_domains=set()
    for p,url in zip(posts,urls):
        if not url: continue
        try: domain,url=canonicalize(url)
        except ValueError: continue
        if domain=="producthunt.com" or domain in seen_domains: continue
        seen_domains.add(domain)
        out.append({"name":p.get("name",""),"url":url,"tagline":p.get("tagline",""),"description":p.get("description",""),"votes":p.get("votesCount",0),"source":"Product Hunt"})
    return out
