import asyncio, json, logging, random, time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit
from sqlmodel import Session, select
from .db import engine, Run, StageLog, Candidate, Page, Fact, Screenshot, Comparison, Category, utcnow
from .sources.producthunt import fetch as fetch_producthunt
from .services.fetcher import fetch
from .services.safety import canonicalize
from .pipeline.extract import extract
from .pipeline.scoring import candidate_score
from .config import get_settings

log=logging.getLogger(__name__)
STAGES=["collect","score","select","research_a","research_b","screenshots","compare","write","render"]
CAPTURES_PER_PRODUCT=2

async def _capture_logo(page, path, home_url):
    """Save a visible site logo, falling back to the site's own favicon."""
    selectors=(
        'header img[alt*="logo" i], nav img[alt*="logo" i], img[alt*="logo" i]',
        'header img[src*="logo" i], nav img[src*="logo" i], img[src*="logo" i]',
        'header img[alt*="brand" i], nav img[alt*="brand" i], img[alt*="brand" i]',
        'header a[href="/"] img, nav a[href="/"] img, header img, nav img',
        'header a[href="/"] svg, nav a[href="/"] svg, header svg[aria-label*="logo" i], nav svg[aria-label*="logo" i]',
    )
    for selector in selectors:
        elements=page.locator(selector)
        try: count=min(await elements.count(),8)
        except Exception: continue
        for index in range(count):
            element=elements.nth(index)
            try:
                if not await element.is_visible(): continue
                bounds=await element.evaluate("e => { const r=e.getBoundingClientRect(); return {w:r.width,h:r.height,nw:e.naturalWidth||0,nh:e.naturalHeight||0}; }")
                if bounds["w"]<20 or bounds["h"]<16 or bounds["w"]>700 or bounds["h"]>300: continue
                if bounds["nw"] and bounds["nw"]<16: continue
                await element.screenshot(path=str(path),timeout=4000)
                return {"source":"page_logo","width":round(bounds["w"]),"height":round(bounds["h"])}
            except Exception: continue

    # Product sites commonly expose only a favicon. Keep fallback assets on the
    # same host as the homepage and render them large enough to inspect.
    try:
        icon=await page.locator('link[rel~="icon" i], link[rel="shortcut icon" i]').first.get_attribute("href",timeout=1500)
        if icon:
            icon_url=urljoin(home_url,icon)
            home=urlsplit(home_url); candidate=urlsplit(icon_url)
            if candidate.scheme in ("http","https") and candidate.netloc==home.netloc:
                import html
                preview=await page.context.new_page()
                await preview.set_content(f'<html><body style="margin:0;width:320px;height:240px;display:grid;place-items:center;background:white"><img id="product-icon" src="{html.escape(icon_url,quote=True)}" style="width:160px;height:160px;object-fit:contain"></body></html>',wait_until="domcontentloaded",timeout=4000)
                icon_img=preview.locator("#product-icon")
                await preview.wait_for_function("() => { const i=document.querySelector('#product-icon'); return i && i.complete && i.naturalWidth>0; }",timeout=4000)
                await icon_img.screenshot(path=str(path),timeout=4000)
                await preview.close()
                return {"source":"favicon","url":icon_url,"width":160,"height":160}
    except Exception: pass
    return None

def _stage(run_id, stage, state, duration=0, output=None, error=""):
    with Session(engine) as s:
        s.add(StageLog(run_id=run_id,stage=stage,status=state,duration_ms=duration,output_json=json.dumps(output or {}),error=error)); s.commit()

def _load(run_id):
    with Session(engine) as s: return s.get(Run,run_id)

async def run_pipeline(run_id: int):
    start=time.monotonic()
    try:
        with Session(engine) as s:
            run=s.get(Run,run_id); run.status="running"; run.started_at=utcnow(); run.message="Collecting product candidates"; category=run.category; s.add(run); s.commit()
        _stage(run_id,"collect","running")
        raw=await fetch_producthunt(category)
        candidates=[]
        with Session(engine) as s:
            for item in raw:
                try: domain,url=canonicalize(item["url"])
                except ValueError: continue
                existing=s.exec(select(Candidate).where(Candidate.domain==domain)).first()
                c=existing or Candidate(domain=domain,name=item["name"] or domain,url=url)
                if c.id is not None and c.id in candidates: continue
                c.name=item["name"] or domain; c.url=url; c.tagline=item.get("tagline",""); c.description=item.get("description",""); c.ph_votes=item.get("votes",0); c.sources_json=json.dumps([item.get("source","Product Hunt")]); c.status="new"
                s.add(c); s.commit(); s.refresh(c); candidates.append(c.id)
        _stage(run_id,"collect","ok",int((time.monotonic()-start)*1000),{"count":len(candidates)})
        if len(candidates)<2: raise RuntimeError("Fewer than two candidates were discovered. Add a Product Hunt API token or configure another discovery source, then start a new run.")
        start=time.monotonic(); scored=[]
        with Session(engine) as s:
            for cid in candidates:
                c=s.get(Candidate,cid)
                status,_,_=await fetch(c.url)
                scores=candidate_score({"name":c.name,"tagline":c.tagline,"description":c.description,"votes":c.ph_votes,"source":c.sources_json},category,liveness=1.0 if status==200 else 0.0)
                c.scores_json=json.dumps(scores); c.status="new" if status==200 and scores["category_fit"]>=.30 else "rejected"; s.add(c)
                if status==200 and scores["category_fit"]>=.30: scored.append((scores["total"],cid))
            s.commit()
        _stage(run_id,"score","ok",int((time.monotonic()-start)*1000),{"scored":len(scored)})
        if len(scored)<2: raise RuntimeError("Fewer than two live, category-matched candidates were found. Try a broader category or configure more discovery sources.")
        scored.sort(reverse=True); chosen=scored[:2]
        with Session(engine) as s:
            run=s.get(Run,run_id); run.decision_json=json.dumps({"chosen":chosen,"backups":[x[1] for x in scored[2:]]}); s.add(run); s.commit()
        _stage(run_id,"select","ok",output={"chosen":chosen,"backups":[x[1] for x in scored[2:]]})
        researched=[]
        for label,(_,cid) in zip(("A","B"),chosen):
            with Session(engine) as s:
                c=s.get(Candidate,cid)
                if not c: raise RuntimeError(f"Selected product {cid} is no longer available.")
                candidate={"id":c.id,"name":c.name,"url":c.url,"domain":c.domain}
                run=s.get(Run,run_id); run.message=f"Researching Product {label}"; s.add(run); s.commit()
            t=time.monotonic(); status,html,method=await fetch(candidate["url"])
            if status!=200 or not html: raise RuntimeError(f"Product {label} site could not be fetched (HTTP {status}).")
            page=extract(html,candidate["url"])
            with Session(engine) as s:
                p=Page(candidate_id=cid,url=candidate["url"],topic="home",score=1,status_code=status,method=method,text_hash=page["hash"],text=page["text"]); s.add(p); s.commit(); s.refresh(p)
                for fact in page["facts"]:
                    if fact["quote"] in page["text"]: s.add(Fact(candidate_id=cid,field=fact["field"],value=fact["value"],source_url=fact["source_url"],quote=fact["quote"],kind=fact["kind"],confidence=.55))
                s.commit()
            links=[]
            from selectolax.lexbor import LexborHTMLParser as HTMLParser
            tree=HTMLParser(html)
            for a in tree.css("a[href]"):
                href=urljoin(candidate["url"],a.attributes.get("href",""))
                try: d,norm=canonicalize(href)
                except ValueError: continue
                if d==candidate["domain"] and norm not in links: links.append(norm)
                if len(links)>=get_settings().max_pages_per_product: break
            researched.append({"id":cid,"candidate":candidate,"home":page,"links":links or [candidate["url"]]})
            _stage(run_id,f"research_{label.lower()}","ok",int((time.monotonic()-t)*1000),{"url":candidate["url"],"links":len(links),"facts":len(page["facts"])})
        t=time.monotonic(); shot_count=0
        try:
            from playwright.async_api import async_playwright
            shots_root=__import__('pathlib').Path("data/screenshots"); shots_root.mkdir(parents=True,exist_ok=True)
            async with async_playwright() as pw:
                browser=await pw.chromium.launch(headless=True)
                for ix,item in enumerate(researched):
                    ctx=await browser.new_context(viewport={"width":1440,"height":900},locale="en-US",color_scheme="light",reduced_motion="reduce")
                    product_label='A' if ix==0 else 'B'
                    home_url=item["candidate"]["url"]
                    pg=await ctx.new_page()
                    try:
                        resp=await pg.goto(home_url,wait_until="domcontentloaded",timeout=20000)
                        if not resp or resp.status!=200: raise RuntimeError(f"homepage returned HTTP {resp.status if resp else 'no response'}")
                        await pg.wait_for_timeout(500)
                        title=await pg.title()
                        txt=await pg.locator("body").inner_text(timeout=4000)
                        if len(txt)<100 or any(x in (title+txt[:300]).lower() for x in ("captcha","access denied","not found")):
                            raise RuntimeError("homepage did not pass content checks")
                        homepage_path=shots_root/f"run-{run_id}-product-{ix+1}-homepage.png"
                        await pg.screenshot(path=str(homepage_path),full_page=False)
                        with Session(engine) as s:
                            s.add(Screenshot(candidate_id=item["id"],path=str(homepage_path),width=1440,height=900,quality_json=json.dumps({"kind":"homepage","status":resp.status,"text_length":len(txt)}),caption=f"Product {product_label} · Homepage")); s.commit()
                        shot_count+=1

                        logo_path=shots_root/f"run-{run_id}-product-{ix+1}-logo.png"
                        logo_info=await _capture_logo(pg,logo_path,home_url)
                        if logo_info:
                            with Session(engine) as s:
                                s.add(Screenshot(candidate_id=item["id"],path=str(logo_path),width=logo_info["width"],height=logo_info["height"],quality_json=json.dumps({"kind":"logo",**logo_info}),caption=f"Product {product_label} · Logo / icon")); s.commit()
                            shot_count+=1
                        else:
                            log.info("Logo/icon screenshot unavailable for %s",home_url)
                    except Exception as exc: log.info("Homepage/logo screenshots skipped for %s: %s",home_url,exc)
                    await pg.close()
                    await ctx.close()
                await browser.close()
        except Exception as exc: log.info("Browser screenshot stage unavailable: %s",exc)
        required_shots=CAPTURES_PER_PRODUCT*len(researched)
        screenshots_complete=shot_count==required_shots
        _stage(run_id,"screenshots","ok" if screenshots_complete else "partial",int((time.monotonic()-t)*1000),{"count":shot_count,"required":required_shots,"per_product":["homepage","logo/icon"]})
        a,b=researched[0],researched[1]
        with Session(engine) as s:
            af=s.exec(select(Fact).where(Fact.candidate_id==a["id"])).all(); bf=s.exec(select(Fact).where(Fact.candidate_id==b["id"])).all()
            rows=[]
            for field in ["pricing","features"]:
                av=next((f.value for f in af if f.field==field),"not publicly found"); bv=next((f.value for f in bf if f.field==field),"not publicly found")
                rows.append({"dimension":field,"a":av,"b":bv,"verdict":"unknown" if av=="not publicly found" or bv=="not publicly found" else "not_comparable","facts_a":[f"F{f.id}" for f in af if f.field==field],"facts_b":[f"F{f.id}" for f in bf if f.field==field]})
            matrix={"category":category,"a":{"id":a["id"],"name":a["candidate"]["name"],"url":a["candidate"]["url"],"description":a["home"]["description"]},"b":{"id":b["id"],"name":b["candidate"]["name"],"url":b["candidate"]["url"],"description":b["home"]["description"]},"rows":rows,"coverage":{"a":min(1,len(af)/4),"b":min(1,len(bf)/4)},"facts":[{"id":f"F{f.id}","product":"A" if f.candidate_id==a["id"] else "B","field":f.field,"value":f.value,"source_url":f.source_url,"quote":f.quote,"kind":f.kind} for f in af+bf]}
            _stage(run_id,"compare","ok",output={"rows":len(rows),"facts":len(af)+len(bf)})
            report=render_markdown(matrix)
            _stage(run_id,"write","ok",output={"method":"deterministic_template"})
            html_report=render_html_report(matrix,report)
            comp=Comparison(run_id=run_id,a_id=a["id"],b_id=b["id"],matrix_json=json.dumps(matrix),markdown=report,html=html_report,coverage_json=json.dumps(matrix["coverage"]),status="partial" if not screenshots_complete else "completed")
            s.add(comp)
            for item in (a,b):
                candidate=s.get(Candidate,item["id"])
                if candidate: candidate.status="covered"; s.add(candidate)
            s.commit(); s.refresh(comp); report_id=comp.id
        _stage(run_id,"render","ok",output={"report_id":report_id})
        with Session(engine) as s:
            run=s.get(Run,run_id); run.status="partial" if not screenshots_complete else "completed"; run.finished_at=utcnow(); run.message="Report ready"; s.add(run); s.commit()
    except Exception as exc:
        log.exception("Run %s failed",run_id)
        with Session(engine) as s:
            run=s.get(Run,run_id)
            if run: run.status="failed"; run.error=str(exc); run.message=str(exc); run.finished_at=utcnow(); s.add(run); s.commit()
        _stage(run_id,"pipeline","failed",error=str(exc))

def render_markdown(m):
    a,b=m["a"],m["b"]
    lines=[f"# {a['name']} vs {b['name']} for {m['category']}","","## TL;DR","- The report compares only details extracted from the public pages linked in Sources.","- Values not found during this run are labeled explicitly.","- Coverage is shown below for each product.","","## At a glance","| Dimension | Product A | Product B |","|---|---|---|"]
    for r in m["rows"]:
        av=r["a"]+((" "+" ".join(f"[{fid}]" for fid in r.get("facts_a",[]))) if r.get("facts_a") else "")
        bv=r["b"]+((" "+" ".join(f"[{fid}]" for fid in r.get("facts_b",[]))) if r.get("facts_b") else "")
        lines.append(f"| {r['dimension'].replace('_',' ').title()} | {av} | {bv} |")
    lines += ["","## Features","Public page feature evidence is summarized in the table above.","","## Pricing","Pricing was not publicly found unless a value is shown above.","","## Integrations","Integration data was not publicly found in the pages reviewed.","","## Strengths and limitations",f"- **{a['name']}:** No verified strengths or limitations were extracted from the pages reviewed.",f"- **{b['name']}:** No verified strengths or limitations were extracted from the pages reviewed.","","## Who may prefer which","The available public evidence is not sufficient to recommend one product over the other.","","## Data coverage and confidence",f"Coverage: Product A {m['coverage']['a']:.0%}; Product B {m['coverage']['b']:.0%}. Findings are limited to retrieved public pages.","","## Sources",f"- [{a['name']}]({a['url']})",f"- [{b['name']}]({b['url']})"]
    return "\n".join(lines)

def render_html_report(matrix, markdown):
    import html
    a,b=matrix["a"],matrix["b"]
    rows="".join(f"<tr><td class='a'>{html.escape(r['a'])}</td><td class='dim'>{html.escape(r['dimension'].replace('_',' ').title())}</td><td class='b'>{html.escape(r['b'])}</td></tr>" for r in matrix["rows"])
    return f"<article class='page'><p class='eyebrow'>Research report · {html.escape(matrix['category'])}</p><h1>{html.escape(a['name'])} vs {html.escape(b['name'])}</h1><p>Public-source comparison with explicit coverage notes.</p><section class='section'><h2>At a glance</h2><div class='table-wrap'><table class='compare'><thead><tr><th class='a'>{html.escape(a['name'])}</th><th>Dimension</th><th class='b'>{html.escape(b['name'])}</th></tr></thead><tbody>{rows}</tbody></table></div></section><section class='section'><h2>Report</h2><pre class='report-md'>{html.escape(markdown)}</pre></section><section class='section'><h2>Sources</h2><p><a href='{html.escape(a['url'],quote=True)}' rel='noreferrer'>{html.escape(a['url'])}</a><br><a href='{html.escape(b['url'],quote=True)}' rel='noreferrer'>{html.escape(b['url'])}</a></p></section></article>"
