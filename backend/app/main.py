import json, asyncio
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlmodel import Session, select
from .config import get_settings
from .db import engine, init_db, Category, Run, StageLog, Comparison, Candidate, Screenshot
from .orchestrator import run_pipeline, STAGES

BACKEND_ROOT=Path(__file__).resolve().parents[1]
PROJECT_ROOT=BACKEND_ROOT.parent
app=FastAPI(title="Compare · Product research",version="1.0.0")
app.mount("/screenshots",StaticFiles(directory=PROJECT_ROOT/"data/screenshots",check_dir=False),name="screenshots")
app.mount("/assets",StaticFiles(directory=PROJECT_ROOT/"frontend/dist/assets",check_dir=False),name="assets")

@app.on_event("startup")
def startup(): init_db()

def _json(row): return json.loads(row) if row else {}

def _run_dict(r, logs=None):
    logs=logs or []
    return {"id":r.id,"category":r.category,"status":r.status,"mode":r.mode,"started_at":r.started_at,"finished_at":r.finished_at,"message":r.message,"error":r.error,"decision":_json(r.decision_json),"stages":[{"stage":x.stage,"status":x.status,"duration_ms":x.duration_ms,"output":_json(x.output_json),"error":x.error,"started_at":x.started_at} for x in logs]}

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

@app.post("/runs",status_code=202)
def create_run(data:RunInput,background_tasks:BackgroundTasks):
    with Session(engine) as s:
        cat=data.category
        if not cat:
            active=s.exec(select(Category).where(Category.active==True)).first(); cat=active.name if active else get_settings().category
        r=Run(category=cat,mode=data.mode,status="pending",message="Queued"); s.add(r); s.commit(); s.refresh(r)
        background_tasks.add_task(run_pipeline,r.id)
        return _run_dict(r)

@app.get("/runs")
def runs(limit:int=50):
    with Session(engine) as s: return [_run_dict(r,s.exec(select(StageLog).where(StageLog.run_id==r.id)).all()) for r in s.exec(select(Run).order_by(Run.id.desc()).limit(limit)).all()]

@app.get("/runs/{run_id}")
def run_detail(run_id:int):
    with Session(engine) as s:
        r=s.get(Run,run_id)
        if not r: raise HTTPException(404,"Run not found")
        logs=s.exec(select(StageLog).where(StageLog.run_id==run_id).order_by(StageLog.id)).all()
        return _run_dict(r,logs)

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
            return {"id":r.id,"run_id":r.run_id,"status":r.status,"created_at":r.created_at,"matrix":matrix,"markdown":r.markdown,"screenshots":[{"url":"/screenshots/"+Path(x.path).name,"caption":x.caption,"product":"A" if x.candidate_id==matrix["a"]["id"] else "B"} for x in shots]}
        if format=="md": return PlainTextResponse(r.markdown,media_type="text/markdown; charset=utf-8")
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
