import asyncio, json, logging, random, time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit
from sqlmodel import Session, select
from .db import engine, Run, StageLog, Candidate, CategoryCandidate, Page, Fact, Screenshot, Comparison, Category, utcnow
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
        row=s.exec(select(StageLog).where(StageLog.run_id==run_id,StageLog.stage==stage).order_by(StageLog.id.desc())).first()
        if row and row.status=="running":
            row.status=state; row.duration_ms=duration; row.output_json=json.dumps(output or {}); row.error=error; s.add(row)
        else:
            s.add(StageLog(run_id=run_id,stage=stage,status=state,duration_ms=duration,output_json=json.dumps(output or {}),error=error))
        s.commit()

def _begin_stage(run_id, stage, message):
    _stage(run_id,stage,"running")
    with Session(engine) as s:
        run=s.get(Run,run_id)
        if run: run.message=message; s.add(run); s.commit()
    return time.monotonic()

def _store_research_page(candidate_id, url, topic, status, method, page, score):
    saved=[]
    with Session(engine) as s:
        record=Page(candidate_id=candidate_id,url=url,topic=topic,score=score,status_code=status,method=method,text_hash=page["hash"],text=page["text"])
        s.add(record); s.commit(); s.refresh(record)
        for fact in page["facts"]:
            if fact["quote"] not in page["text"]: continue
            row=Fact(candidate_id=candidate_id,field=fact["field"],value=fact["value"],source_url=fact["source_url"],quote=fact["quote"],kind=fact["kind"],confidence=.55)
            s.add(row); s.commit(); s.refresh(row)
            saved.append({"id":row.id,"field":row.field,"value":row.value,"source_url":row.source_url,"quote":row.quote,"kind":row.kind,"confidence":row.confidence,"topic":topic})
    return saved

_LINK_TOPICS={
    "pricing":("pricing","price","plans","billing","cost"),
    "features":("features","capabilities","product"),
    "integrations":("integration","integrations","api","webhook","connect"),
    "security":("security","privacy","trust","compliance"),
    "workflow":("how-it-works","how it works","workflow","getting-started","demo","docs")
}
def _link_topic(url, anchor_text=""):
    text=(url+" "+anchor_text).lower()
    for topic,terms in _LINK_TOPICS.items():
        if any(term in text for term in terms): return topic
    return "page"

def _load(run_id):
    with Session(engine) as s: return s.get(Run,run_id)

async def run_pipeline(run_id: int):
    start=time.monotonic(); current_stage="collect"; stage_started=start
    try:
        with Session(engine) as s:
            run=s.get(Run,run_id); run.status="running"; run.started_at=utcnow(); run.finished_at=None; run.error=""; run.message="Collecting product candidates"; category=run.category; category_config=s.get(Category,run.category_id) if run.category_id else s.exec(select(Category).where(Category.name==category)).first(); category_id=category_config.id if category_config else None; db_keywords=json.loads(category_config.keywords_json or "[]") if category_config else []; topic_slugs=json.loads(category_config.ph_topic_slugs_json or "[]") if category_config else []; s.add(run); s.commit()
        configured=get_settings().topics.get("category_keywords",{}) or {}
        configured_keywords=next((values for name,values in configured.items() if " ".join(name.casefold().split())==" ".join(category.casefold().split())),[])
        category_keywords=list(dict.fromkeys([*db_keywords,*configured_keywords]))
        configured_topics=get_settings().topics.get("category_topic_slugs",{}) or {}
        mapped_topics=next((values for name,values in configured_topics.items() if " ".join(name.casefold().split())==" ".join(category.casefold().split())),[])
        topic_slugs=list(dict.fromkeys([*topic_slugs,*mapped_topics]))
        stage_started=_begin_stage(run_id,"collect","Collecting product candidates")
        raw=await fetch_producthunt(category,keywords=category_keywords,topic_slugs=topic_slugs)
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
                if category_id and not s.exec(select(CategoryCandidate).where(CategoryCandidate.category_id==category_id,CategoryCandidate.candidate_id==c.id)).first():
                    s.add(CategoryCandidate(category_id=category_id,candidate_id=c.id)); s.commit()
        _stage(run_id,"collect","ok",int((time.monotonic()-start)*1000),{"count":len(candidates),"candidate_ids":candidates})
        if len(candidates)<2:
            reason="Product Hunt token is missing." if not get_settings().producthunt_token else "Fewer than two relevant products matched this category. Add category keywords or broaden the topic, then retry."
            raise RuntimeError(reason)
        current_stage="score"; stage_started=_begin_stage(run_id,"score","Checking and scoring candidates"); start=stage_started; scored=[]
        with Session(engine) as s:
            for cid in candidates:
                c=s.get(Candidate,cid)
                status,_,_=await fetch(c.url)
                scores=candidate_score({"name":c.name,"tagline":c.tagline,"description":c.description,"votes":c.ph_votes,"source":c.sources_json},category,liveness=1.0 if status==200 else 0.0,keywords=category_keywords)
                c.scores_json=json.dumps(scores); c.status="new" if status==200 and scores["category_fit"]>=.30 else "rejected"; s.add(c)
                if status==200 and scores["category_fit"]>=.30: scored.append((scores["total"],cid))
            s.commit()
        _stage(run_id,"score","ok",int((time.monotonic()-start)*1000),{"scored":len(scored)})
        if len(scored)<2: raise RuntimeError("Fewer than two live, category-matched candidates were found. Try a broader category or configure more discovery sources.")
        current_stage="select"; stage_started=_begin_stage(run_id,"select","Selecting products for comparison")
        scored.sort(reverse=True); chosen=scored[:2]
        with Session(engine) as s:
            run=s.get(Run,run_id); run.decision_json=json.dumps({"chosen":chosen,"backups":[x[1] for x in scored[2:]]}); s.add(run); s.commit()
        _stage(run_id,"select","ok",output={"chosen":chosen,"backups":[x[1] for x in scored[2:]]})
        researched=[]
        for label,(_,cid) in zip(("A","B"),chosen):
            current_stage=f"research_{label.lower()}"; stage_started=_begin_stage(run_id,current_stage,f"Researching Product {label}")
            with Session(engine) as s:
                c=s.get(Candidate,cid)
                if not c: raise RuntimeError(f"Selected product {cid} is no longer available.")
                candidate={"id":c.id,"name":c.name,"url":c.url,"domain":c.domain}
                run=s.get(Run,run_id); run.message=f"Researching Product {label}"; s.add(run); s.commit()
            t=time.monotonic(); status,html,method=await fetch(candidate["url"])
            if status!=200 or not html: raise RuntimeError(f"Product {label} site could not be fetched (HTTP {status}).")
            page=extract(html,candidate["url"])
            research_facts=_store_research_page(cid,candidate["url"],"home",status,method,page,1.0)
            links=[]; seen_urls={candidate["url"]}
            from selectolax.lexbor import LexborHTMLParser as HTMLParser
            tree=HTMLParser(html)
            for a in tree.css("a[href]"):
                href=urljoin(candidate["url"],a.attributes.get("href",""))
                try: d,norm=canonicalize(href)
                except ValueError: continue
                if d!=candidate["domain"] or norm in seen_urls: continue
                seen_urls.add(norm); links.append({"url":norm,"topic":_link_topic(norm,a.text(strip=True))})
            topic_order={name:index for index,name in enumerate(("pricing","features","integrations","security","workflow","page"))}
            links.sort(key=lambda item:(topic_order.get(item["topic"],99),len(item["url"])))
            pages=[{"url":candidate["url"],"topic":"home"}]
            max_extra=max(0,get_settings().max_pages_per_product-1)
            for target in links[:max_extra]:
                page_status,sub_html,sub_method=await fetch(target["url"])
                if page_status!=200 or not sub_html: continue
                sub_page=extract(sub_html,target["url"])
                research_facts.extend(_store_research_page(cid,target["url"],target["topic"],page_status,sub_method,sub_page,.8))
                pages.append({"url":target["url"],"topic":target["topic"]})
            researched.append({"id":cid,"candidate":candidate,"home":page,"facts":research_facts,"pages":pages})
            _stage(run_id,f"research_{label.lower()}","ok",int((time.monotonic()-t)*1000),{"url":candidate["url"],"pages_reviewed":pages,"facts":len(research_facts)})
        current_stage="screenshots"; stage_started=_begin_stage(run_id,"screenshots","Capturing homepage and logo screenshots"); t=stage_started; shot_count=0
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
        current_stage="compare"; stage_started=_begin_stage(run_id,"compare","Comparing verified product details")
        a,b=researched[0],researched[1]
        with Session(engine) as s:
            af=a["facts"]; bf=b["facts"]
            rows=[]
            for field in ["pricing","features","integrations","security","limits","limitations"]:
                av=next((f["value"] for f in af if f["field"]==field),"not publicly found"); bv=next((f["value"] for f in bf if f["field"]==field),"not publicly found")
                rows.append({"dimension":field,"a":av,"b":bv,"verdict":"unknown" if av=="not publicly found" or bv=="not publicly found" else "not_comparable","facts_a":[f"F{f['id']}" for f in af if f["field"]==field],"facts_b":[f"F{f['id']}" for f in bf if f["field"]==field]})
            matrix={"category":category,"a":{"id":a["id"],"name":a["candidate"]["name"],"url":a["candidate"]["url"],"description":a["home"]["description"]},"b":{"id":b["id"],"name":b["candidate"]["name"],"url":b["candidate"]["url"],"description":b["home"]["description"]},"rows":rows,"coverage":{"a":min(1,len(af)/6),"b":min(1,len(bf)/6)},"sources":[{"product":"A","url":p["url"],"topic":p["topic"]} for p in a["pages"]]+[{"product":"B","url":p["url"],"topic":p["topic"]} for p in b["pages"]],"facts":[{"id":f"F{f['id']}","product":label,"field":f["field"],"value":f["value"],"source_url":f["source_url"],"quote":f["quote"],"kind":f["kind"]} for label,items in (("A",af),("B",bf)) for f in items]}
            _stage(run_id,"compare","ok",output={"rows":len(rows),"facts":len(af)+len(bf)})
            current_stage="write"; stage_started=_begin_stage(run_id,"write","Writing comparison report")
            report=render_markdown(matrix)
            _stage(run_id,"write","ok",output={"method":"deterministic_template"})
            current_stage="render"; stage_started=_begin_stage(run_id,"render","Rendering comparison report")
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
        reason=str(exc) or exc.__class__.__name__
        _stage(run_id,current_stage,"failed",int((time.monotonic()-stage_started)*1000),error=reason)
        with Session(engine) as s:
            run=s.get(Run,run_id)
            if run: run.status="failed"; run.error=reason; run.message=f"Failed during {current_stage.replace('_',' ')}: {reason}"; run.finished_at=utcnow(); s.add(run); s.commit()

def render_markdown(m):
    a,b=m["a"],m["b"]
    rows=m.get("rows",[]); facts=m.get("facts",[])
    by_product={"A":[f for f in facts if f.get("product")=="A"],"B":[f for f in facts if f.get("product")=="B"]}
    source_pages=m.get("sources") or [{"product":"A","topic":"home","url":a["url"]},{"product":"B","topic":"home","url":b["url"]}]
    def clean(value): return value if value and "not publicly found" not in value.lower() else "Not found in the pages reviewed"
    def ref_id(f):
        value=str(f.get("id", ""))
        return value if value.startswith("F") else f"F{value}"
    def row_value(name,key):
        row=next((x for x in rows if x.get("dimension")==name),None)
        return clean(row.get(key)) if row else "Not found in the pages reviewed"
    lines=[f"# {a['name']} vs {b['name']} for {m['category']}","",f"Generated from public pages retrieved for this run.","","## Quick decision verdict","| Decision category | Evidence-based finding |","|---|---|",
      f"| Pricing evidence | {a['name']}: {row_value('pricing','a')}; {b['name']}: {row_value('pricing','b')} |",
      f"| Feature evidence | {a['name']}: {row_value('features','a')}; {b['name']}: {row_value('features','b')} |",
      "| Overall recommendation | No winner is assigned because the available public evidence is not enough to verify a complete side-by-side fit. |",
      "","## At a glance","| Attribute | Product A | Product B |","|---|---|---|",
      f"| Product | {a['name']} | {b['name']} |",f"| Primary purpose | {a.get('description') or 'Not found in the pages reviewed'} | {b.get('description') or 'Not found in the pages reviewed'} |",
      f"| Starting price evidence | {row_value('pricing','a')} | {row_value('pricing','b')} |",f"| Feature evidence | {row_value('features','a')} | {row_value('features','b')} |",
      "","## Feature-by-feature matrix","| Capability | Why it matters | Product A | Product B | Finding |","|---|---|---|---|---|"]
    for row in rows:
        why="Budget and plan limits" if row["dimension"]=="pricing" else "Publicly described product capability"
        finding="Not enough matched evidence" if row.get("verdict")=="unknown" else "See evidence"
        av=clean(row.get("a")); bv=clean(row.get("b"))
        if row.get("facts_a"): av += " " + " ".join(f"[{x}]" for x in row["facts_a"])
        if row.get("facts_b"): bv += " " + " ".join(f"[{x}]" for x in row["facts_b"])
        lines.append(f"| {row['dimension'].replace('_',' ').title()} | {why} | {av} | {bv} | {finding} |")
    lines += ["","## Technical deep dives"]
    for label,product in (("A",a),("B",b)):
        lines += [f"### {product['name']}",product.get("description") or "No product summary was found in the pages reviewed."]
        product_facts=by_product[label]
        if product_facts:
            lines.append("Verified details:")
            lines.extend(f"- **{f.get('field','Detail').replace('_',' ').title()}:** {f.get('quote') or f.get('value','')} [{ref_id(f)}]" for f in product_facts)
        else: lines.append("No additional technical facts were verified in the pages reviewed.")
    lines += ["","## Pricing and total cost of ownership",f"- **{a['name']}:** {row_value('pricing','a')}",f"- **{b['name']}:** {row_value('pricing','b')}","","Comparable total cost is unavailable because equivalent plans, user counts, usage levels, and add-on costs were not established by the retrieved evidence.","","## Which product fits your use case?","The retrieved pages provide product summaries and limited fact excerpts, but do not establish enough requirements or matched plan limits to rank either product for a specific team. Confirm use-case fit with the official sources.","","## Where each product falls short"]
    for label,product in (("A",a),("B",b)):
        limits=[f for f in by_product[label] if f.get("field") in ("limitations","cons")]
        lines.append(f"- **{product['name']}:** " + ("; ".join(f.get("quote") or f.get("value","") for f in limits) if limits else "No independently verified limitation was extracted in this run; this does not mean the product has no limitations."))
    lines += ["","## The switching playbook","Before switching, confirm data export, migration support, plan limits, integrations, and contract terms directly with each vendor. Migration documentation was not established by this run.","","## Frequently asked questions",
      f"### Which product is cheaper?\nThe run did not find comparable pricing for both products. Check equivalent billing periods, included limits, and add-on costs.",
      f"### Which product has more features?\n{a['name']}: {row_value('features','a')}. {b['name']}: {row_value('features','b')}. These excerpts are not a complete feature inventory.",
      "### Which one should I choose?\nThere is not enough verified evidence in this report to name an overall winner. Compare the cited sources against your requirements and confirm missing details with both vendors.",
      "","## Final verdict and recommendation","No overall winner is assigned. Review the verified evidence, confirm plan and workflow details with both vendors, and make the decision against your team's requirements.",
      "","## Data coverage and confidence",f"Coverage: Product A {m['coverage']['a']:.0%}; Product B {m['coverage']['b']:.0%}. ‘Not found’ means the crawler did not locate the information in its reviewed pages.","","## Sources and methodology",
      *[f"- Product {source['product']} · {source['topic']}: [{source['url']}]({source['url']})" for source in source_pages],
      "","Claims in this report come from public pages retrieved during this run. Missing evidence is stated as unknown rather than inferred. Verify current pricing, plan limits, and product claims with the official sources before purchase."]
    return "\n".join(lines)

def render_html_report(matrix, markdown):
    import html
    a,b=matrix["a"],matrix["b"]
    rows="".join(f"<tr><td class='a'>{html.escape(r['a'])}</td><td class='dim'>{html.escape(r['dimension'].replace('_',' ').title())}</td><td class='b'>{html.escape(r['b'])}</td></tr>" for r in matrix["rows"])
    return f"<article class='page'><p class='eyebrow'>Research report · {html.escape(matrix['category'])}</p><h1>{html.escape(a['name'])} vs {html.escape(b['name'])}</h1><p>Public-source comparison with explicit coverage notes.</p><section class='section'><h2>At a glance</h2><div class='table-wrap'><table class='compare'><thead><tr><th class='a'>{html.escape(a['name'])}</th><th>Dimension</th><th class='b'>{html.escape(b['name'])}</th></tr></thead><tbody>{rows}</tbody></table></div></section><section class='section'><h2>Report</h2><pre class='report-md'>{html.escape(markdown)}</pre></section><section class='section'><h2>Sources</h2><p><a href='{html.escape(a['url'],quote=True)}' rel='noreferrer'>{html.escape(a['url'])}</a><br><a href='{html.escape(b['url'],quote=True)}' rel='noreferrer'>{html.escape(b['url'])}</a></p></section></article>"
