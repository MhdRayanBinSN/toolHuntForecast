import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from sqlmodel import SQLModel, Field, Session, create_engine, select
from sqlalchemy import UniqueConstraint, inspect
from .config import get_settings

def utcnow(): return datetime.now(timezone.utc)

class Category(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    keywords_json: str = "[]"
    ph_topic_slugs_json: str = "[]"
    active: bool = True
    profile_json: str = "{}"
    confirmed: bool = False
    validation_status: str = "pending_review"

class CategoryValidationCache(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("normalized_name", name="uq_category_validation_name"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    normalized_name: str = Field(index=True)
    response_json: str = "{}"
    created_at: datetime = Field(default_factory=utcnow)

class Candidate(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    domain: str = Field(index=True)
    name: str
    url: str
    tagline: str = ""
    description: str = ""
    sources_json: str = "[]"
    ph_votes: int = 0
    first_seen: datetime = Field(default_factory=utcnow)
    scores_json: str = "{}"
    status: str = "new"

class CategoryCandidate(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("category_id", "candidate_id", name="uq_category_candidate"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    category_id: int = Field(index=True, foreign_key="category.id")
    candidate_id: int = Field(index=True, foreign_key="candidate.id")

class URLSnapshot(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    source: str
    url: str
    first_seen: datetime = Field(default_factory=utcnow)
    last_seen: datetime = Field(default_factory=utcnow)

class AltEdge(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    seed_domain: str
    alt_domain: str
    source: str
    rank: int = 0

class Run(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    category_id: Optional[int] = Field(default=None, foreign_key="category.id")
    category: str = ""
    status: str = "pending"
    mode: str = "fast"
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error: str = ""
    message: str = "Queued"
    decision_json: str = "{}"

class StageLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(index=True, foreign_key="run.id")
    stage: str
    status: str
    output_json: str = "{}"
    started_at: datetime = Field(default_factory=utcnow)
    duration_ms: int = 0
    attempts: int = 1
    error: str = ""

class Page(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: Optional[int] = Field(default=None, index=True, foreign_key="run.id")
    candidate_id: int = Field(index=True, foreign_key="candidate.id")
    url: str
    topic: str = "home"
    score: float = 0
    status_code: int = 0
    method: str = "httpx"
    matched_topics_json: str = "[]"
    text_hash: str = ""
    text: str = ""
    fetched_at: datetime = Field(default_factory=utcnow)

class Fact(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: Optional[int] = Field(default=None, index=True, foreign_key="run.id")
    candidate_id: int = Field(index=True, foreign_key="candidate.id")
    field: str
    value: str
    source_url: str
    quote: str
    kind: str = "fact"
    confidence: float = 0.5

class Screenshot(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    candidate_id: int = Field(index=True, foreign_key="candidate.id")
    page_id: Optional[int] = Field(default=None, foreign_key="page.id")
    path: str
    width: int = 0
    height: int = 0
    quality_json: str = "{}"
    caption: str = ""

class Comparison(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(index=True, foreign_key="run.id")
    a_id: int = Field(foreign_key="candidate.id")
    b_id: int = Field(foreign_key="candidate.id")
    matrix_json: str = "{}"
    markdown: str = ""
    html: str = ""
    coverage_json: str = "{}"
    status: str = "completed"
    created_at: datetime = Field(default_factory=utcnow)

class LLMCall(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(index=True, foreign_key="run.id")
    stage: str
    prompt_hash: str = Field(index=True)
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    response: str = ""
    created_at: datetime = Field(default_factory=utcnow)

def _url():
    settings = get_settings(); url = settings.database_url
    if url.startswith("sqlite:///"):
        p = Path(url.removeprefix("sqlite:///")); (Path.cwd() / p.parent).mkdir(parents=True, exist_ok=True)
        return create_engine(url, connect_args={"check_same_thread": False})
    return create_engine(url)

engine = _url()
def init_db():
    SQLModel.metadata.create_all(engine)
    columns={column["name"] for column in inspect(engine).get_columns("category")}
    with engine.begin() as connection:
        if "profile_json" not in columns: connection.exec_driver_sql("ALTER TABLE category ADD COLUMN profile_json TEXT NOT NULL DEFAULT '{}'" )
        if "confirmed" not in columns: connection.exec_driver_sql("ALTER TABLE category ADD COLUMN confirmed BOOLEAN NOT NULL DEFAULT 1")
        if "validation_status" not in columns: connection.exec_driver_sql("ALTER TABLE category ADD COLUMN validation_status VARCHAR NOT NULL DEFAULT 'confirmed'")
        for table in ("page", "fact"):
            table_columns={column["name"] for column in inspect(engine).get_columns(table)}
            if "run_id" not in table_columns:
                connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN run_id INTEGER REFERENCES run(id)")
            connection.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS ix_{table}_run_id ON {table}(run_id)")
        page_columns={column["name"] for column in inspect(engine).get_columns("page")}
        if "matched_topics_json" not in page_columns:
            connection.exec_driver_sql("ALTER TABLE page ADD COLUMN matched_topics_json TEXT NOT NULL DEFAULT '[]'")
    with Session(engine) as s:
        if not s.exec(select(Category)).first():
            s.add(Category(name=get_settings().category, keywords_json="[]", active=False, confirmed=False, validation_status="pending_review")); s.commit()
        stale=s.exec(select(Category).where(Category.confirmed==True, Category.profile_json=="{}")).all()
        for category in stale:
            category.confirmed=False
            category.active=False
            category.validation_status="pending_review"
            s.add(category)
        if stale:
            s.commit()
def session(): return Session(engine)
def dump(obj): return json.loads(obj) if obj else {}
