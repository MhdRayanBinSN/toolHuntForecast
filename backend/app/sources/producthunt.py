import httpx
import asyncio
from datetime import datetime, timedelta, timezone
from sqlmodel import Session, select
from ..config import get_settings
from ..db import Candidate, engine
from ..services.fetcher import resolve_url
from ..services.safety import canonicalize

QUERY='''query($first:Int!, $after:String, $postedAfter:DateTime) { posts(first:$first, after:$after, order:NEWEST, postedAfter:$postedAfter) { pageInfo { endCursor hasNextPage } edges { node { name tagline description website url votesCount createdAt } } } }'''

async def fetch(category: str, since_days: int=30):
    token=get_settings().producthunt_token
    if not token: return []
    posts=[]; after=None; posted=(datetime.now(timezone.utc)-timedelta(days=since_days)).isoformat()
    async with httpx.AsyncClient(timeout=20) as c:
        for _ in range(4):
            r=await c.post("https://api.producthunt.com/v2/api/graphql",headers={"Authorization":f"Bearer {token}"},json={"query":QUERY,"variables":{"first":50,"after":after,"postedAfter":posted}})
            r.raise_for_status(); payload=r.json(); conn=payload.get("data",{}).get("posts",{})
            for edge in conn.get("edges",[]):
                p=edge.get("node",{})
                if p.get("website"): posts.append(p)
            info=conn.get("pageInfo",{}); after=info.get("endCursor")
            if not info.get("hasNextPage") or not after: break
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
