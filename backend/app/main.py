import json, asyncio, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlmodel import Session, select
from .config import get_settings
from .db import engine, init_db, Category, CategoryCandidate, Run, StageLog, Comparison, Candidate, Page, Fact, Screenshot, LLMCall, AltEdge
from .orchestrator import run_pipeline, render_markdown, STAGES

BACKEND_ROOT=Path(__file__).resolve().parents[1]
PROJECT_ROOT=BACKEND_ROOT.parent
app=FastAPI(title="Compare · Product research",version="1.0.0")
logger=logging.getLogger(__name__)
_scheduler_task=None
app.mount("/screenshots",StaticFiles(directory=PROJECT_ROOT/"data/screenshots",check_dir=False),name="screenshots")
app.mount("/assets",StaticFiles(directory=PROJECT_ROOT/"frontend/dist/assets",check_dir=False),name="assets")

@app.on_event("startup")
def startup():
    global _scheduler_task
    init_db()
    settings=get_settings()
    if settings.schedule_enabled:
        try:
            _parse_cron(settings.schedule_cron)
            _scheduler_task=asyncio.get_running_loop().create_task(_daily_scheduler())
            logger.info("Daily category runs enabled: %s (UTC)",settings.schedule_cron)
        except ValueError as exc:
            logger.error("Daily category scheduler disabled: %s",exc)

@app.on_event("shutdown")
async def shutdown():
    global _scheduler_task
    if _scheduler_task:
        _scheduler_task.cancel()
        try: await _scheduler_task
        except asyncio.CancelledError: pass
        _scheduler_task=None

def _cron_field_matches(field,value,minimum,maximum):
    for part in field.split(","):
        base,*step_part=part.split("/")
        step=int(step_part[0]) if step_part else 1
        if step<1: raise ValueError("Cron steps must be positive")
        if base=="*": start,end=minimum,maximum
        elif "-" in base:
            start,end=(int(x) for x in base.split("-",1))
        else:
            start=end=int(base)
        if start<minimum or end>maximum or start>end: raise ValueError("Cron field value out of range")
        if start<=value<=end and (value-start)%step==0: return True
    return False

def _parse_cron(expression):
    fields=expression.split()
    if len(fields)!=5: raise ValueError("SCHEDULE_CRON must use five cron fields (minute hour day month weekday)")
    ranges=((0,59),(0,23),(1,31),(1,12),(0,6))
    for field,(minimum,maximum) in zip(fields,ranges):
        for part in field.split(","):
            base,*step=part.split("/")
            if len(step)>1: raise ValueError("Invalid cron expression")
            if step and int(step[0])<1: raise ValueError("Cron steps must be positive")
            values=(minimum,maximum) if base=="*" else tuple(int(x) for x in base.split("-")) if "-" in base else (int(base),int(base))
            if len(values)!=2 or values[0]<minimum or values[1]>maximum or values[0]>values[1]: raise ValueError("Cron field value out of range")
    return fields

def _cron_matches(fields,instant):
    values=(instant.minute,instant.hour,instant.day,instant.month,(instant.weekday()+1)%7)
    ranges=((0,59),(0,23),(1,31),(1,12),(0,6))
    return all(_cron_field_matches(field,value,*bounds) for field,value,bounds in zip(fields,values,ranges))

def _next_schedule_time(expression,now=None):
    fields=_parse_cron(expression)
    candidate=(now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(second=0,microsecond=0)+timedelta(minutes=1)
    for _ in range(60*24*366):
        if _cron_matches(fields,candidate): return candidate
        candidate+=timedelta(minutes=1)
    raise ValueError("SCHEDULE_CRON has no upcoming execution time")

async def _run_all_active_categories():
    with Session(engine) as s:
        categories=s.exec(select(Category).where(Category.active==True).order_by(Category.id)).all()
        run_ids=[]
        for category in categories:
            run=Run(category_id=category.id,category=category.name,mode="scheduled",status="pending",message="Queued by daily schedule")
            s.add(run); s.commit(); s.refresh(run); run_ids.append(run.id)
    logger.info("Daily schedule queued %d active categories",len(run_ids))
    for run_id in run_ids:
        await run_pipeline(run_id)

async def _daily_scheduler():
    settings=get_settings(); fields=_parse_cron(settings.schedule_cron); last_slot=None
    while True:
        now=datetime.now(timezone.utc)
        slot=now.strftime("%Y-%m-%d %H:%M")
        if _cron_matches(fields,now) and slot!=last_slot:
            last_slot=slot
            try: await _run_all_active_categories()
            except asyncio.CancelledError: raise
            except Exception: logger.exception("Daily category run batch failed")
        await asyncio.sleep(max(1,60-datetime.now(timezone.utc).second))

def _json(row): return json.loads(row) if row else {}

def _runtime_ms(run):
    if not run.started_at: return 0
    start=run.started_at
    end=run.finished_at or datetime.now(timezone.utc)
    if start.tzinfo is None: start=start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None: end=end.replace(tzinfo=timezone.utc)
    return max(0,int((end-start).total_seconds()*1000))

def _run_dict(r, logs=None, llm_calls=None):
    logs=logs or []
    llm_calls=llm_calls or []
    failed_stage=next((x for x in reversed(logs) if x.status=="failed"),None)
    tokens_in=sum(call.tokens_in for call in llm_calls)
    tokens_out=sum(call.tokens_out for call in llm_calls)
    failure={"stage":failed_stage.stage,"reason":failed_stage.error or r.error} if r.status=="failed" and failed_stage else None
    return {"id":r.id,"category":r.category,"status":r.status,"mode":r.mode,"started_at":r.started_at,"finished_at":r.finished_at,"runtime_ms":_runtime_ms(r),"message":r.message,"error":r.error if r.status=="failed" else "","failure":failure,"token_usage":{"input_tokens":tokens_in,"output_tokens":tokens_out,"total_tokens":tokens_in+tokens_out,"calls":len(llm_calls)},"decision":_json(r.decision_json),"stages":[{"stage":x.stage,"status":x.status,"duration_ms":x.duration_ms,"output":_json(x.output_json),"error":x.error,"started_at":x.started_at} for x in logs]}

class RunInput(BaseModel):
    category: str|None=None
    mode: str="fast"
class CategoryInput(BaseModel):
    name: str=Field(min_length=2,max_length=120)
    keywords: list[str]=[]
    active: bool=True
class CategoryPatch(BaseModel): active: bool|None=None; keywords: list[str]|None=None

@app.get("/health")
def health(): return {"status":"ok","service":"product-research","time":datetime.now(timezone.utc).isoformat()}

@app.get("/schedule")
def schedule_status():
    settings=get_settings()
    with Session(engine) as s: active_count=len(s.exec(select(Category).where(Category.active==True)).all())
    next_run=_next_schedule_time(settings.schedule_cron).isoformat() if settings.schedule_enabled else None
    return {"enabled":settings.schedule_enabled,"cron":settings.schedule_cron,"timezone":"UTC","active_categories":active_count,"next_run":next_run}

@app.post("/runs",status_code=202)
def create_run(data:RunInput,background_tasks:BackgroundTasks):
    with Session(engine) as s:
        cat=data.category; category_id=None
        if not cat:
            active=s.exec(select(Category).where(Category.active==True)).first(); cat=active.name if active else get_settings().category; category_id=active.id if active else None
        else:
            configured=s.exec(select(Category).where(Category.name==cat)).first(); category_id=configured.id if configured else None
        r=Run(category_id=category_id,category=cat,mode=data.mode,status="pending",message="Queued"); s.add(r); s.commit(); s.refresh(r)
        background_tasks.add_task(run_pipeline,r.id)
        return _run_dict(r)

@app.get("/runs")
def runs(limit:int=50):
    with Session(engine) as s:
        result=[]
        for r in s.exec(select(Run).order_by(Run.id.desc()).limit(limit)).all():
            logs=s.exec(select(StageLog).where(StageLog.run_id==r.id).order_by(StageLog.id)).all()
            calls=s.exec(select(LLMCall).where(LLMCall.run_id==r.id)).all()
            result.append(_run_dict(r,logs,calls))
        return result

@app.get("/runs/{run_id}")
def run_detail(run_id:int):
    with Session(engine) as s:
        r=s.get(Run,run_id)
        if not r: raise HTTPException(404,"Run not found")
        logs=s.exec(select(StageLog).where(StageLog.run_id==run_id).order_by(StageLog.id)).all()
        calls=s.exec(select(LLMCall).where(LLMCall.run_id==run_id)).all()
        return _run_dict(r,logs,calls)

@app.get("/reports")
def reports(limit:int=50):
    with Session(engine) as s:
        return [{"id":x.id,"run_id":x.run_id,"status":x.status,"created_at":x.created_at,"matrix":_json(x.matrix_json)} for x in s.exec(select(Comparison).order_by(Comparison.id.desc()).limit(limit)).all()]

@app.get("/reports/{report_id}")
def report(report_id:int,format:str="html"):
    with Session(engine) as s:
        r=s.get(Comparison,report_id)
        if not r: raise HTTPException(404,"Report not found")
        if format=="json":
            matrix=_json(r.matrix_json); ids={matrix.get("a",{}).get("id"),matrix.get("b",{}).get("id")}
            shots=s.exec(select(Screenshot).where(Screenshot.candidate_id.in_(ids),Screenshot.path.contains(f"run-{r.run_id}-"))).all() if None not in ids else []
            return {"id":r.id,"run_id":r.run_id,"status":r.status,"created_at":r.created_at,"matrix":matrix,"markdown":render_markdown(matrix),"screenshots":[{"url":"/screenshots/"+Path(x.path).name,"caption":x.caption,"product":"A" if x.candidate_id==matrix["a"]["id"] else "B"} for x in shots]}
        if format=="md": return PlainTextResponse(render_markdown(_json(r.matrix_json)),media_type="text/markdown; charset=utf-8")
        return HTMLResponse(r.html)

@app.get("/candidates")
def candidates():
    with Session(engine) as s: return [{"id":c.id,"name":c.name,"domain":c.domain,"url":c.url,"sources":_json(c.sources_json),"scores":_json(c.scores_json),"status":c.status} for c in s.exec(select(Candidate).order_by(Candidate.id.desc())).all()]

@app.get("/categories")
def categories():
    with Session(engine) as s: return [{"id":c.id,"name":c.name,"keywords":_json(c.keywords_json),"active":c.active} for c in s.exec(select(Category).order_by(Category.id)).all()]

@app.post("/categories",status_code=201)
def add_category(data:CategoryInput):
    with Session(engine) as s:
        c=Category(name=data.name,keywords_json=json.dumps(data.keywords),active=data.active); s.add(c); s.commit(); s.refresh(c); return {"id":c.id,"name":c.name,"keywords":data.keywords,"active":c.active}

@app.patch("/categories/{category_id}")
def patch_category(category_id:int,data:CategoryPatch):
    with Session(engine) as s:
        c=s.get(Category,category_id)
        if not c: raise HTTPException(404,"Category not found")
        if data.active is not None: c.active=data.active
        if data.keywords is not None: c.keywords_json=json.dumps(data.keywords)
        s.add(c); s.commit(); return {"id":c.id,"name":c.name,"keywords":_json(c.keywords_json),"active":c.active}

def _decision_candidate_ids(raw):
    try:
        decision=json.loads(raw) if isinstance(raw,str) else (raw or {})
    except (TypeError,ValueError):
        return set()
    ids=set()
    for item in decision.get("chosen",[]) or []:
        value=item[-1] if isinstance(item,(list,tuple)) and item else item.get("id") if isinstance(item,dict) else item
        try: ids.add(int(value))
        except (TypeError,ValueError): pass
    for item in decision.get("backups",[]) or []:
        value=item.get("id") if isinstance(item,dict) else item
        try: ids.add(int(value))
        except (TypeError,ValueError): pass
    return ids

def _logged_candidate_ids(logs):
    ids=set()
    for log in logs:
        try: found=json.loads(log.output_json or "{}").get("candidate_ids",[])
        except (TypeError,ValueError,AttributeError): found=[]
        for value in found or []:
            try: ids.add(int(value))
            except (TypeError,ValueError): pass
    return ids

@app.delete("/categories/{category_id}")
def delete_category(category_id:int):
    with Session(engine) as s:
        category=s.get(Category,category_id)
        if not category: raise HTTPException(404,"Category not found")
        # Include legacy runs that stored only the category name and not its ID.
        same_name_categories=s.exec(select(Category).where(Category.name==category.name)).all()
        active_runs=s.exec(select(Run).where(Run.category_id==category_id,Run.status.in_(["pending","running"]))).all()
        if len(same_name_categories)==1:
            active_runs.extend(s.exec(select(Run).where(Run.category==category.name,Run.category_id==None,Run.status.in_(["pending","running"]))).all())
        if active_runs:
            raise HTTPException(409,"This category has an active run. Wait for it to finish, then delete the category.")
        runs_by_id=s.exec(select(Run).where(Run.category_id==category_id)).all()
        runs_by_name=s.exec(select(Run).where(Run.category==category.name)).all() if len(same_name_categories)==1 else []
        runs={run.id:run for run in [*runs_by_id,*runs_by_name]}
        run_ids=set(runs)
        logs=[]
        if run_ids:
            logs=s.exec(select(StageLog).where(StageLog.run_id.in_(run_ids))).all()
        reports=s.exec(select(Comparison).where(Comparison.run_id.in_(run_ids))).all() if run_ids else []

        category_links=s.exec(select(CategoryCandidate).where(CategoryCandidate.category_id==category_id)).all()
        owned_candidate_ids={link.candidate_id for link in category_links}
        for run in runs.values(): owned_candidate_ids.update(_decision_candidate_ids(run.decision_json))
        owned_candidate_ids.update(_logged_candidate_ids(logs))
        for report in reports: owned_candidate_ids.update((report.a_id,report.b_id))

        for report in reports: s.delete(report)
        for log in logs: s.delete(log)
        llm_calls=s.exec(select(LLMCall).where(LLMCall.run_id.in_(run_ids))).all() if run_ids else []
        for call in llm_calls: s.delete(call)
        for run in runs.values(): s.delete(run)
        for link in category_links: s.delete(link)
        s.delete(category)
        s.flush()

        # Preserve candidates referenced by another category or any remaining research.
        shared_ids={link.candidate_id for link in s.exec(select(CategoryCandidate)).all()}
        for report in s.exec(select(Comparison)).all(): shared_ids.update((report.a_id,report.b_id))
        remaining_runs=s.exec(select(Run)).all()
        for run in remaining_runs: shared_ids.update(_decision_candidate_ids(run.decision_json))
        remaining_logs=s.exec(select(StageLog)).all()
        shared_ids.update(_logged_candidate_ids(remaining_logs))
        orphan_ids=owned_candidate_ids-shared_ids
        orphan_candidates=[candidate for candidate_id in orphan_ids if (candidate:=s.get(Candidate,candidate_id))]
        screenshot_root=(PROJECT_ROOT/"data"/"screenshots").resolve()
        screenshot_paths=[]
        if orphan_ids:
            screenshots=s.exec(select(Screenshot).where(Screenshot.candidate_id.in_(orphan_ids))).all()
            for shot in screenshots:
                path=Path(shot.path)
                if not path.is_absolute(): path=PROJECT_ROOT/path
                try:
                    resolved=path.resolve()
                    if resolved.is_relative_to(screenshot_root): screenshot_paths.append(resolved)
                except (OSError,ValueError): pass
                s.delete(shot)
            for model in (Fact,Page,CategoryCandidate):
                rows=s.exec(select(model).where(model.candidate_id.in_(orphan_ids))).all()
                for row in rows: s.delete(row)
            domains={candidate.domain for candidate in orphan_candidates}
            if domains:
                edges=s.exec(select(AltEdge).where(AltEdge.seed_domain.in_(domains)|AltEdge.alt_domain.in_(domains))).all()
                for edge in edges: s.delete(edge)
            for candidate in orphan_candidates: s.delete(candidate)
        s.commit()
        removed_screenshot_files=0
        for path in screenshot_paths:
            try:
                if path.is_file(): path.unlink(); removed_screenshot_files+=1
            except OSError: pass
        return {"deleted_category":category_id,"deleted_runs":len(runs),"deleted_reports":len(reports),"deleted_products":len(orphan_candidates),"preserved_shared_products":len(owned_candidate_ids & shared_ids),"deleted_screenshots":removed_screenshot_files}

@app.get("/",response_class=HTMLResponse)
def dashboard(request:Request):
    return _spa()

@app.get("/ui/runs",response_class=HTMLResponse)
def run_list(): return RedirectResponse("/app/runs",status_code=307)

@app.get("/ui/runs/{run_id}",response_class=HTMLResponse)
def run_page(run_id:int): return RedirectResponse(f"/app/runs/{run_id}",status_code=307)

@app.get("/ui/reports",response_class=HTMLResponse)
def report_list(): return RedirectResponse("/app/reports",status_code=307)

@app.get("/ui/reports/{report_id}",response_class=HTMLResponse)
def report_page(report_id:int): return RedirectResponse(f"/app/reports/{report_id}",status_code=307)

@app.get("/ui/categories",response_class=HTMLResponse)
def category_page(): return RedirectResponse("/app/categories",status_code=307)

@app.get("/style-guide",response_class=HTMLResponse)
def style_guide(): return RedirectResponse("/app/style-guide",status_code=307)

def _spa():
    index=PROJECT_ROOT/"frontend/dist/index.html"
    if index.exists(): return FileResponse(index)
    return HTMLResponse("<h1>React UI is not built yet</h1><p>Run <code>cd frontend &amp;&amp; npm install &amp;&amp; npm run build</code>, or use <code>npm run dev</code> while developing.</p>",status_code=503)

@app.get("/{full_path:path}",include_in_schema=False)
def spa_fallback(full_path:str): return _spa()

def _stage_states(logs):
    found={x.stage:x.status for x in logs}; done={"ok","done","partial"}
    out=[]
    for i,name in enumerate(STAGES):
        status=found.get(name)
        if status in done: state="done"
        elif status=="running": state="running"
        elif status=="failed": state="failed"
        elif any(found.get(prev) not in done for prev in STAGES[:i]): state="pending"
        else: state="pending"
        out.append({"label":name.replace("_"," ").title(),"state":state})
    return out
