import asyncio, json, logging, random, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from sqlmodel import Session, select
from .db import engine, Run, StageLog, Candidate, CategoryCandidate, Page, Fact, Screenshot, Comparison, Category, utcnow
from .sources.producthunt import fetch as fetch_producthunt
from .sources.directories import fetch_directories
from .services.fetcher import fetch
from .services.llm import write_report
from .services.safety import canonicalize
from .pipeline.extract import extract
from .pipeline.scoring import candidate_score, category_verification_rules, verify_category_fit
from .pipeline.topic_matching import allowed_fact_fields, category_config_value, classify_page_topics, required_topics, select_topic_links, topic_coverage, topic_definitions
from .config import get_settings

log=logging.getLogger(__name__)
STAGES=["collect","score","select","research_a","research_b","screenshots","compare","write","render"]
PROJECT_ROOT=Path(__file__).resolve().parents[2]
SCREENSHOT_ROOT=PROJECT_ROOT/"data"/"screenshots"

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

def _image_quality(path):
    try:
        from PIL import Image, ImageStat
        with Image.open(path) as image:
            stat=ImageStat.Stat(image.convert("L"))
            return {"pixel_stdev":round(float(stat.stddev[0]),2)}
    except Exception:
        return {"pixel_stdev":None}

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

def _store_research_page(run_id, candidate_id, url, topic, status, method, page, score, matched_topics=None):
    saved=[]
    with Session(engine) as s:
        record=s.exec(select(Page).where(Page.run_id==run_id,Page.url==url)).first()
        if record:
            old_facts=s.exec(select(Fact).where(Fact.run_id==run_id,Fact.source_url==url)).all()
            for old_fact in old_facts: s.delete(old_fact)
            record.candidate_id=candidate_id; record.topic=topic; record.score=score; record.status_code=status; record.method=method; record.matched_topics_json=json.dumps(matched_topics or []); record.text_hash=page["hash"]; record.text=page["text"]
        else:
            record=Page(run_id=run_id,candidate_id=candidate_id,url=url,topic=topic,score=score,status_code=status,method=method,matched_topics_json=json.dumps(matched_topics or []),text_hash=page["hash"],text=page["text"])
        s.add(record); s.commit(); s.refresh(record)
        for fact in page["facts"]:
            if fact["quote"] not in page["text"]: continue
            row=Fact(run_id=run_id,candidate_id=candidate_id,field=fact["field"],value=fact["value"],source_url=fact["source_url"],quote=fact["quote"],kind=fact["kind"],confidence=.55)
            s.add(row); s.commit(); s.refresh(row)
            saved.append({"id":row.id,"field":row.field,"value":row.value,"source_url":row.source_url,"quote":row.quote,"kind":row.kind,"confidence":row.confidence,"topic":topic})
    return saved

def _load(run_id):
    with Session(engine) as s: return s.get(Run,run_id)

def _latest_stage(run_id, stage):
    with Session(engine) as s:
        return s.exec(select(StageLog).where(StageLog.run_id==run_id,StageLog.stage==stage).order_by(StageLog.id.desc())).first()

def _stage_done(run_id, stage):
    row=_latest_stage(run_id,stage)
    return bool(row and row.status in {"ok","done"})

def _stage_output(run_id, stage):
    row=_latest_stage(run_id,stage)
    if not row or not row.output_json: return {}
    try: return json.loads(row.output_json)
    except (TypeError,ValueError): return {}

def _load_research(run_id, candidate_id, label):
    with Session(engine) as s:
        c=s.get(Candidate,candidate_id)
        if not c: raise RuntimeError(f"Selected product {candidate_id} is no longer available.")
        pages=s.exec(select(Page).where(Page.run_id==run_id,Page.candidate_id==candidate_id).order_by(Page.id)).all()
        facts=s.exec(select(Fact).where(Fact.run_id==run_id,Fact.candidate_id==candidate_id).order_by(Fact.id)).all()
    if not pages:
        return None
    home_page=next((p for p in reversed(pages) if p.topic=="home"),None)
    topic_by_url={p.url:p.topic for p in pages}
    candidate={"id":c.id,"name":c.name,"url":c.url,"domain":c.domain}
    page_items=[{"url":p.url,"topic":p.topic,"matched_topics":json.loads(p.matched_topics_json or "[]")} for p in pages]
    return {"id":candidate_id,"candidate":candidate,"home":{"description":c.description or (home_page.text[:240] if home_page else ""),"text":home_page.text if home_page else ""},"facts":[{"id":row.id,"field":row.field,"value":row.value,"source_url":row.source_url,"quote":row.quote,"kind":row.kind,"confidence":row.confidence,"topic":topic_by_url.get(row.source_url,"home")} for row in facts],"pages":page_items,"topic_coverage":topic_coverage(page_items,required_topics(get_settings().topics,topic_definitions(get_settings().topics)))}

async def run_pipeline(run_id: int):
    start=time.monotonic(); current_stage="collect"; stage_started=start
    try:
        with Session(engine) as s:
            run=s.get(Run,run_id); run.status="running"; run.started_at=run.started_at or utcnow(); run.finished_at=None; run.error=""; run.message="Collecting product candidates"; category=run.category; category_config=s.get(Category,run.category_id) if run.category_id else s.exec(select(Category).where(Category.name==category)).first(); category_id=category_config.id if category_config else None; db_keywords=json.loads(category_config.keywords_json or "[]") if category_config else []; category_profile=json.loads(category_config.profile_json or "{}") if category_config else {}; profile_keywords=category_profile.get("keywords",[]) if isinstance(category_profile,dict) else []; topic_slugs=json.loads(category_config.ph_topic_slugs_json or "[]") if category_config else []; s.add(run); s.commit()
        with Session(engine) as s:
            existing_comparison=s.exec(select(Comparison).where(Comparison.run_id==run_id).order_by(Comparison.id.desc())).first()
            existing_status=existing_comparison.status if existing_comparison else ""
        if _stage_done(run_id,"render") and existing_status!="partial":
            with Session(engine) as s:
                run=s.get(Run,run_id)
                if run:
                    run.status="completed"; run.finished_at=run.finished_at or utcnow(); run.message="Report ready"; s.add(run); s.commit()
            return
        topic_config=get_settings().topics
        configured=topic_config.get("category_keywords",{}) or {}
        configured_keywords=category_config_value(category, configured)
        category_keywords=list(dict.fromkeys([*db_keywords,*configured_keywords,*profile_keywords]))
        capability_aliases=category_config_value(category,topic_config.get("category_capability_aliases",{}) or {})
        verification_rules=category_verification_rules(category,category_profile,category_keywords,capability_aliases)
        if not verification_rules["ready"]:
            raise RuntimeError("Strict product matching is unavailable for this category. Add and confirm at least two required capabilities or configure at least two specific category phrases, then retry.")
        configured_topics=topic_config.get("category_topic_slugs",{}) or {}
        mapped_topics=category_config_value(category, configured_topics)
        topic_slugs=list(dict.fromkeys([*topic_slugs,*mapped_topics]))
        page_topic_definitions=topic_definitions(topic_config)
        required_research_topics=required_topics(topic_config,page_topic_definitions)
        try: page_topic_min_score=max(0.0,min(1.0,float(topic_config.get("page_topic_min_score",0.55))))
        except (TypeError,ValueError): page_topic_min_score=0.55
        if _stage_done(run_id,"collect"):
            candidates=[int(x) for x in _stage_output(run_id,"collect").get("candidate_ids",[]) if str(x).isdigit()]
        else:
            stage_started=_begin_stage(run_id,"collect","Collecting product candidates")
            raw=[]
            raw.extend(await fetch_producthunt(category,keywords=category_keywords,topic_slugs=topic_slugs))
            raw.extend(await fetch_directories(category,keywords=category_keywords))
            candidates=[]
            with Session(engine) as s:
                for item in raw:
                    try: domain,url=canonicalize(item["url"])
                    except ValueError: continue
                    existing=s.exec(select(Candidate).where(Candidate.domain==domain)).first()
                    c=existing or Candidate(domain=domain,name=item["name"] or domain,url=url)
                    existing_scores=json.loads(c.scores_json or "{}") if c.scores_json else {}
                    if item.get("topics"): existing_scores["ph_topics"]=item.get("topics",[])
                    c.name=item["name"] or domain; c.url=url; c.tagline=item.get("tagline",""); c.description=item.get("description",""); c.ph_votes=item.get("votes",0); c.sources_json=json.dumps([item.get("source","Product Hunt")]); c.scores_json=json.dumps(existing_scores); c.status="new"
                    s.add(c); s.commit(); s.refresh(c); candidates.append(c.id)
                    if category_id and not s.exec(select(CategoryCandidate).where(CategoryCandidate.category_id==category_id,CategoryCandidate.candidate_id==c.id)).first():
                        s.add(CategoryCandidate(category_id=category_id,candidate_id=c.id)); s.commit()
            _stage(run_id,"collect","ok",int((time.monotonic()-stage_started)*1000),{"count":len(candidates),"candidate_ids":candidates})
        if len(candidates)<2:
            reason="No configured discovery source returned at least two candidates. Add Product Hunt credentials or enable a directory source in config, then retry."
            raise RuntimeError(reason)
        homepage_cache={}
        if _stage_done(run_id,"score"):
            scored=[(float(item.get("total",0)),int(item["id"]),float(item.get("category_fit",0))) for item in _stage_output(run_id,"score").get("verified_scores",[]) if item.get("id") is not None]
            shortlist=[]
        else:
            current_stage="score"; stage_started=_begin_stage(run_id,"score","Checking product homepages against category requirements"); start=stage_started; scored=[]; ranked_candidates=[]
            with Session(engine) as s:
                for cid in candidates:
                    c=s.get(Candidate,cid)
                    cand_topics = json.loads(c.scores_json or "{}").get("ph_topics", []) if c.scores_json else []
                    details={"name":c.name,"tagline":c.tagline,"description":c.description,"votes":c.ph_votes,"source":c.sources_json,"topics":cand_topics}
                    discovery_check=verify_category_fit({**details,"homepage":{"title":c.name,"description":c.tagline,"text":c.description}},verification_rules)
                    ranked_candidates.append((len(discovery_check["matched_capabilities"]),c.ph_votes,cid,{"url":c.url,"details":details}))
            ranked_candidates.sort(key=lambda item:(item[0],item[1],item[2]),reverse=True)
            try: candidate_check_limit=max(2,int(get_settings().topics.get("candidate_check_limit",100)))
            except (TypeError,ValueError): candidate_check_limit=100
            shortlist=ranked_candidates[:candidate_check_limit]
            probe_semaphore=asyncio.Semaphore(5)
            async def probe_candidate(item):
                _,_,cid,candidate_info=item
                try:
                    async with probe_semaphore:
                        status,html,method=await fetch(candidate_info["url"])
                    page=extract(html,candidate_info["url"]) if status==200 and html else {"title":"","description":"","headings":[],"text":""}
                    details=candidate_info["details"]
                    verification=verify_category_fit({**details,"homepage":page},verification_rules)
                    if status!=200:
                        verification["accepted"]=False
                        verification["reasons"].insert(0,f"Homepage could not be retrieved (HTTP {status}).")
                    elif len(page.get("text", "").strip())<100:
                        verification["accepted"]=False
                        verification["reasons"].insert(0,"Homepage did not provide enough readable product text to verify its category.")
                    for evidence in verification.get("matched_capabilities",[]): evidence["source_url"]=candidate_info["url"]
                    for evidence in verification.get("adjacent_matches",[]): evidence["source_url"]=candidate_info["url"]
                    for evidence in verification.get("negative_matches",[]): evidence["source_url"]=candidate_info["url"]
                    if status==200 and html:
                        homepage_cache[cid]=(status,html,method,page)
                    return status,html,method,page,verification
                except Exception as exc:
                    log.info("Candidate site check failed for %s: %s",candidate_info["url"],exc)
                    return 0,"","unavailable",{"title":"","description":"","headings":[],"text":""},{"accepted":False,"category_fit":0.0,"profile_source":verification_rules["profile_source"],"matched_capabilities":[],"missing_capabilities":verification_rules["required_phrases"],"adjacent_matches":[],"negative_matches":[],"reasons":[f"Homepage check failed ({type(exc).__name__})."]}
            site_results=await asyncio.gather(*(probe_candidate(item) for item in shortlist))
            rejection_reasons={}; rejected_examples=[]; live_candidates=0
            for (_,_,cid,item),(status,_,_,page,verification) in zip(shortlist,site_results):
                live=bool(status==200 and len(page.get("text", "").strip())>=100)
                if live: live_candidates+=1
                scores=candidate_score(item["details"],category,liveness=1.0 if live else 0.0,keywords=category_keywords,category_fit=verification["category_fit"])
                scores["verification"]={**verification,"category_name":category}
                if item["details"].get("topics"): scores["ph_topics"]=item["details"]["topics"]
                with Session(engine) as s:
                    c=s.get(Candidate,cid)
                    if c:
                        c.scores_json=json.dumps(scores,ensure_ascii=False); c.status="new" if live and verification["accepted"] else "rejected"; s.add(c); s.commit()
                if live and verification["accepted"]: scored.append((scores["total"],cid,scores["category_fit"]))
                else:
                    reason=verification["reasons"][0] if verification.get("reasons") else "Homepage did not pass the strict category check."
                    rejection_reasons[reason]=rejection_reasons.get(reason,0)+1
                    if len(rejected_examples)<20:
                        rejected_examples.append({"candidate":item["details"]["name"],"url":item["url"],"reasons":verification.get("reasons",[]),"matched_capabilities":verification.get("matched_capabilities",[]),"missing_capabilities":verification.get("missing_capabilities",[])})
            score_output={"candidates_found":len(candidates),"sites_checked":len(shortlist),"site_check_limit":candidate_check_limit,"live_candidates":live_candidates,"verified_candidates":len(scored),"verified_scores":[{"id":cid,"total":score,"category_fit":fit} for score,cid,fit in scored],"rejected_candidates":len(shortlist)-len(scored),"rejection_reasons":rejection_reasons,"rejected_examples":rejected_examples}
            _stage(run_id,"score","ok",int((time.monotonic()-start)*1000),score_output)
        if len(scored)<2: raise RuntimeError(f"Strict homepage verification found only {len(scored)} category-matched product(s) after checking {len(shortlist)} sites. No comparison was created; review the score-stage rejection reasons, then refine or confirm the category profile.")
        if _stage_done(run_id,"select"):
            output=_stage_output(run_id,"select")
            chosen=[(float(item[0]),int(item[1]),0.0) for item in output.get("chosen",[]) if isinstance(item,list) and len(item)>=2]
        else:
            current_stage="select"; stage_started=_begin_stage(run_id,"select","Selecting products for comparison")
            # Relevance is the primary ordering signal; popularity and the remaining
            # quality signals break ties only among candidates that passed the gate.
            scored.sort(key=lambda item:(item[2],item[0]),reverse=True); chosen=scored[:2]
            with Session(engine) as s:
                chosen_for_storage=[[score,cid] for score,cid,_ in chosen]
                run=s.get(Run,run_id); run.decision_json=json.dumps({"chosen":chosen_for_storage,"backups":[x[1] for x in scored[2:]]}); s.add(run); s.commit()
            _stage(run_id,"select","ok",output={"chosen":chosen_for_storage,"backups":[x[1] for x in scored[2:]]})
        researched=[]
        for label,(_,cid,_) in zip(("A","B"),chosen):
            current_stage=f"research_{label.lower()}"
            if _stage_done(run_id,current_stage):
                cached_research=_load_research(run_id,cid,label)
                if cached_research:
                    researched.append(cached_research)
                    continue
            stage_started=_begin_stage(run_id,current_stage,f"Researching Product {label}")
            with Session(engine) as s:
                c=s.get(Candidate,cid)
                if not c: raise RuntimeError(f"Selected product {cid} is no longer available.")
                candidate={"id":c.id,"name":c.name,"url":c.url,"domain":c.domain}
                run=s.get(Run,run_id); run.message=f"Researching Product {label}"; s.add(run); s.commit()
            t=time.monotonic()
            cached_homepage=homepage_cache.get(cid)
            if cached_homepage:
                status,html,method=cached_homepage[:3]
            else:
                status,html,method=await fetch(candidate["url"])
            if status!=200 or not html: raise RuntimeError(f"Product {label} site could not be fetched (HTTP {status}).")
            page=cached_homepage[3] if cached_homepage else extract(html,candidate["url"])
            homepage_topics=classify_page_topics(page,page_topic_definitions,page_topic_min_score)
            homepage_item={"url":candidate["url"],"topic":"home","matched_topics":[match["topic"] for match in homepage_topics]}
            research_facts=_store_research_page(run_id,cid,candidate["url"],"home",status,method,page,1.0,[match["topic"] for match in homepage_topics])
            links=[]; seen_urls={candidate["url"]}
            from selectolax.lexbor import LexborHTMLParser as HTMLParser
            tree=HTMLParser(html)
            for a in tree.css("a[href]"):
                href=urljoin(candidate["url"],a.attributes.get("href",""))
                try: d,norm=canonicalize(href)
                except ValueError: continue
                if d!=candidate["domain"] or norm in seen_urls: continue
                candidate_path=urlsplit(candidate["url"]).path.rstrip("/")
                candidate_segments=[part for part in candidate_path.split("/") if part]
                page_path=urlsplit(norm).path.rstrip("/")
                if len(candidate_segments)>=2 and page_path!=candidate_path and not page_path.startswith(candidate_path+"/"):
                    continue
                seen_urls.add(norm); links.append({"url":norm,"anchor":a.text(strip=True)})
            pages=[homepage_item]
            max_extra=max(max(0,get_settings().max_pages_per_product-1),len(required_research_topics))
            targets=select_topic_links(links,page_topic_definitions,required_research_topics,max_extra,page_topic_min_score)
            topic_errors=[]
            for target in targets:
                page_status,sub_html,sub_method=await fetch(target["url"])
                if page_status!=200 or not sub_html:
                    topic_errors.append({"topic":target["topic"],"url":target["url"],"reason":f"Page fetch returned HTTP {page_status}."})
                    continue
                sub_page=extract(sub_html,target["url"])
                content_matches=classify_page_topics(sub_page,page_topic_definitions,page_topic_min_score)
                if not content_matches:
                    topic_errors.append({"topic":target["topic"],"url":target["url"],"reason":"Fetched page content did not match a configured topic."})
                    continue
                actual_topic=content_matches[0]["topic"]
                allowed_fields=allowed_fact_fields(actual_topic,topic_config)
                if allowed_fields is not None:
                    sub_page["facts"]=[fact for fact in sub_page["facts"] if fact["field"] in allowed_fields]
                research_facts.extend(_store_research_page(run_id,cid,target["url"],actual_topic,page_status,sub_method,sub_page,.8,[match["topic"] for match in content_matches]))
                pages.append({"url":target["url"],"topic":actual_topic,"matched_topics":[match["topic"] for match in content_matches]})
            coverage=topic_coverage(pages,required_research_topics)
            coverage["errors"]=topic_errors
            researched.append({"id":cid,"candidate":candidate,"home":page,"facts":research_facts,"pages":pages,"topic_coverage":coverage})
            _stage(run_id,f"research_{label.lower()}","ok" if not coverage["missing"] else "partial",int((time.monotonic()-t)*1000),{"url":candidate["url"],"pages_reviewed":pages,"facts":len(research_facts),"topic_coverage":coverage})
        current_stage="screenshots"; stage_started=_begin_stage(run_id,"screenshots","Capturing product page screenshots"); t=stage_started; shot_count=0
        product_counts={"A":0,"B":0}; screenshot_errors=[]
        try:
            from playwright.async_api import async_playwright
            SCREENSHOT_ROOT.mkdir(parents=True,exist_ok=True)
            min_screenshots=max(1,get_settings().min_screenshots)
            async with async_playwright() as pw:
                browser=await pw.chromium.launch(headless=True)
                for ix,item in enumerate(researched):
                    ctx=await browser.new_context(viewport={"width":1440,"height":900},locale="en-US",color_scheme="light",reduced_motion="reduce")
                    product_label='A' if ix==0 else 'B'
                    product_shots=0
                    page_targets=[]
                    for page_target in item["pages"]:
                        if page_target["url"] not in {target["url"] for target in page_targets}:
                            page_targets.append(page_target)
                    for page_target in page_targets:
                        if product_shots>=min_screenshots: break
                        pg=await ctx.new_page()
                        try:
                            resp=await pg.goto(page_target["url"],wait_until="domcontentloaded",timeout=20000)
                            if not resp or resp.status!=200: raise RuntimeError(f"page returned HTTP {resp.status if resp else 'no response'}")
                            try: await pg.wait_for_load_state("networkidle",timeout=3000)
                            except Exception: pass
                            await pg.wait_for_timeout(500)
                            title=await pg.title()
                            txt=await pg.locator("body").inner_text(timeout=4000)
                            title_check=(title+" "+txt[:300]).lower()
                            if len(txt)<200 or any(x in title_check for x in ("captcha","just a moment","access denied","not found","404")):
                                raise RuntimeError("page did not pass content checks")
                            topic=page_target["topic"].replace("/","-")
                            shot_path=SCREENSHOT_ROOT/f"run-{run_id}-product-{ix+1}-{product_shots+1}-{topic}.png"
                            await pg.screenshot(path=str(shot_path),full_page=False)
                            quality=_image_quality(shot_path)
                            if quality.get("pixel_stdev") is not None and quality["pixel_stdev"]<3:
                                try: shot_path.unlink()
                                except OSError: pass
                                raise RuntimeError("screenshot appears blank")
                            with Session(engine) as s:
                                s.add(Screenshot(candidate_id=item["id"],path=str(shot_path),width=1440,height=900,quality_json=json.dumps({"kind":"page","topic":page_target["topic"],"status":resp.status,"text_length":len(txt),**quality}),caption=f"Product {product_label} · {page_target['topic'].replace('_',' ').title()}")); s.commit()
                            product_shots+=1; shot_count+=1; product_counts[product_label]=product_shots
                        except Exception as exc:
                            log.info("Screenshot skipped for %s: %s",page_target["url"],exc)
                            if len(screenshot_errors)<20:
                                screenshot_errors.append({"product":product_label,"url":page_target["url"],"reason":str(exc)[:240] or exc.__class__.__name__})
                        finally:
                            await pg.close()
                    await ctx.close()
                await browser.close()
        except Exception as exc:
            log.exception("Browser screenshot stage failed for run %s",run_id)
            screenshot_errors.append({"reason":f"Browser capture unavailable: {str(exc)[:240] or exc.__class__.__name__}"})
        required_shots=max(1,get_settings().min_screenshots)*len(researched)
        screenshots_complete=all(product_counts[label]>=max(1,get_settings().min_screenshots) for label in ("A","B"))
        _stage(run_id,"screenshots","ok" if screenshots_complete else "partial",int((time.monotonic()-t)*1000),{"count":shot_count,"required":required_shots,"per_product_minimum":get_settings().min_screenshots,"per_product":product_counts,"errors":screenshot_errors})
        current_stage="compare"; stage_started=_begin_stage(run_id,"compare","Comparing verified product details")
        a,b=researched[0],researched[1]
        research_topic_coverage={label:item.get("topic_coverage",topic_coverage(item["pages"],required_research_topics)) for label,item in (("A",a),("B",b))}
        topics_complete=all(not item["missing"] for item in research_topic_coverage.values())
        with Session(engine) as s:
            af=a["facts"]; bf=b["facts"]
            field_topics={"pricing":"pricing","limits":"pricing","features":"features","integrations":"integrations","security":"trust","limitations":"features"}
            def best_fact(items,field):
                matches=[fact for fact in items if fact["field"]==field]
                preferred=field_topics.get(field)
                return next((fact for fact in matches if fact.get("topic")==preferred),next((fact for fact in matches if fact.get("topic")=="home"),matches[0] if matches else None))
            rows=[]
            for field in ["pricing","features","integrations","security","limits","limitations"]:
                afield=best_fact(af,field); bfield=best_fact(bf,field)
                av=afield["value"] if afield else "not publicly found"; bv=bfield["value"] if bfield else "not publicly found"
                rows.append({"dimension":field,"a":av,"b":bv,"verdict":"unknown" if not afield or not bfield else "not_comparable","facts_a":[f"F{afield['id']}"] if afield else [],"facts_b":[f"F{bfield['id']}"] if bfield else []})
            matrix={"category":category,"a":{"id":a["id"],"name":a["candidate"]["name"],"url":a["candidate"]["url"],"description":a["home"]["description"]},"b":{"id":b["id"],"name":b["candidate"]["name"],"url":b["candidate"]["url"],"description":b["home"]["description"]},"rows":rows,"coverage":{"a":min(1,len(af)/6),"b":min(1,len(bf)/6)},"topic_coverage":research_topic_coverage,"sources":[{"product":"A","url":p["url"],"topic":p["topic"]} for p in a["pages"]]+[{"product":"B","url":p["url"],"topic":p["topic"]} for p in b["pages"]],"facts":[{"id":f"F{f['id']}","product":label,"field":f["field"],"value":f["value"],"source_url":f["source_url"],"quote":f["quote"],"topic":f.get("topic",""),"kind":f["kind"]} for label,items in (("A",af),("B",bf)) for f in items]}
            _stage(run_id,"compare","ok",output={"rows":len(rows),"facts":len(af)+len(bf)})
            current_stage="write"; stage_started=_begin_stage(run_id,"write","Writing comparison report")
            report=await write_report(run_id,matrix)
            method="llm_write_report" if report else "deterministic_template"
            if not report: report=render_markdown(matrix)
            _stage(run_id,"write","ok",output={"method":method})
            current_stage="render"; stage_started=_begin_stage(run_id,"render","Rendering comparison report")
            html_report=render_html_report(matrix,report)
            comp=s.exec(select(Comparison).where(Comparison.run_id==run_id).order_by(Comparison.id.desc())).first()
            if comp:
                comp.a_id=a["id"]; comp.b_id=b["id"]; comp.matrix_json=json.dumps(matrix); comp.markdown=report; comp.html=html_report; comp.coverage_json=json.dumps(matrix["coverage"]); comp.status="partial" if not screenshots_complete or not topics_complete else "completed"
            else:
                comp=Comparison(run_id=run_id,a_id=a["id"],b_id=b["id"],matrix_json=json.dumps(matrix),markdown=report,html=html_report,coverage_json=json.dumps(matrix["coverage"]),status="partial" if not screenshots_complete or not topics_complete else "completed")
            s.add(comp)
            for item in (a,b):
                candidate=s.get(Candidate,item["id"])
                if candidate: candidate.status="covered"; s.add(candidate)
            s.commit(); s.refresh(comp); report_id=comp.id
        _stage(run_id,"render","ok",output={"report_id":report_id})
        with Session(engine) as s:
            run=s.get(Run,run_id); incomplete=not screenshots_complete or not topics_complete; run.status="partial" if incomplete else "completed"; run.finished_at=utcnow(); run.message="Report ready with incomplete coverage" if incomplete else "Report ready"; s.add(run); s.commit()
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
    topic_coverage_data=m.get("topic_coverage",{})
    if topic_coverage_data:
        lines += ["","## Required topic coverage"]
        for label,product in (("A",a),("B",b)):
            coverage=topic_coverage_data.get(label,{})
            covered=", ".join(coverage.get("covered",[])) or "none found"
            missing=", ".join(coverage.get("missing",[])) or "none"
            lines.append(f"- **{product['name']}:** matched {covered}; not found {missing}.")
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
