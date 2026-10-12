import hashlib
import json
import re
from datetime import datetime, timezone

import httpx
from sqlmodel import Session, select

from ..config import get_settings
from ..db import LLMCall, engine
from .prompts import REPORT_WRITER_SYSTEM, report_writer_prompt


def _fact_ids(matrix: dict) -> set[str]:
    return {str(fact.get("id")) for fact in matrix.get("facts", []) if fact.get("id")}


def verify_report(matrix: dict, markdown: str) -> list[str]:
    errors = []
    known = _fact_ids(matrix)
    facts_by_id = {str(fact.get("id")): fact for fact in matrix.get("facts", []) if fact.get("id")}
    product_names = {label: str(matrix.get(key, {}).get("name") or "").casefold() for label, key in (("A", "a"), ("B", "b"))}
    facts_by_id = {str(fact.get("id")): fact for fact in matrix.get("facts", []) if fact.get("id")}
    product_names = {label: str(matrix.get(key, {}).get("name") or "").casefold() for label, key in (("A", "a"), ("B", "b"))}
    citation_pattern = r"\[(F\d+)\]"
    for citation in re.findall(citation_pattern, markdown or ""):
        if citation not in known:
            errors.append(f"Unknown citation [{citation}]")
    in_table = False
    for line in (markdown or "").splitlines():
        content = line.strip()
        if not content:
            in_table = False
            continue
        if content.startswith("#") or re.fullmatch(r"\|?[\s:|-]+\|?", content):
            continue
        if content.startswith("|"):
            if not in_table:
                in_table = True
                continue
            if not re.search(citation_pattern, content):
                errors.append("Comparison table row lacks a fact citation")
            continue
        in_table = False
        is_bullet = content.startswith(("- ", "* ", "> "))
        word_count = len(re.findall(r"\b\w+\b", content))
        if (is_bullet or word_count >= 8) and not re.search(citation_pattern, content):
            errors.append("Factual prose lacks a fact citation")
        citation_ids = re.findall(citation_pattern, content)
        cited_products = {facts_by_id[citation].get("product") for citation in citation_ids if citation in facts_by_id}
        for label, name in product_names.items():
            if name and name in content.casefold() and cited_products and label not in cited_products:
                errors.append(f"Product {label} is cited only with the other product's facts")
        citation_ids = re.findall(citation_pattern, content)
        cited_products = {facts_by_id[citation].get("product") for citation in citation_ids if citation in facts_by_id}
        for label, name in product_names.items():
            if name and name in content.casefold() and cited_products and label not in cited_products:
                errors.append(f"Product {label} is cited only with the other product's facts")
    if re.search(r"\b(best|leading|unmatched|#1|number one)\b", markdown or "", re.I):
        errors.append("Unsupported superlative found")
    price_like = re.findall(r"(?:[$€£₹]\s?\d[\d,.]*|\b\d+\s?(?:users|seats|credits|emails)\b)", markdown or "", re.I)
    matrix_text = json.dumps(matrix, ensure_ascii=False)
    for value in price_like:
        if value not in matrix_text:
            errors.append(f"Value not present in matrix: {value}")
    return list(dict.fromkeys(errors))


async def write_report(run_id: int, matrix: dict) -> str:
    settings = get_settings()
    if not settings.gemini_api_key:
        return ""
    with Session(engine) as session:
        used = len(session.exec(select(LLMCall).where(LLMCall.run_id == run_id)).all())
        if used >= settings.llm_max_calls_per_run:
            return ""
    model = settings.llm_model_fast or settings.category_validation_model or "gemini-3.6-flash"
    compact = {
        "category": matrix.get("category"),
        "products": {"A": matrix.get("a"), "B": matrix.get("b")},
        "rows": matrix.get("rows", []),
        "coverage": matrix.get("coverage", {}),
        "topic_coverage": matrix.get("topic_coverage", {}),
        "sources": matrix.get("sources", []),
        "facts": matrix.get("facts", [])[:80],
    }
    system = REPORT_WRITER_SYSTEM
    user = report_writer_prompt(matrix.get("category", "software products"), json.dumps(compact, ensure_ascii=False))
    prompt_hash = hashlib.sha256((model + system + user).encode()).hexdigest()
    with Session(engine) as session:
        cached = session.exec(select(LLMCall).where(LLMCall.prompt_hash == prompt_hash)).first()
        if cached and cached.response:
            return cached.response if not verify_report(matrix, cached.response) else ""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(
                url,
                headers={"x-goog-api-key": settings.gemini_api_key},
                json={
                    "systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": user}]}],
                    "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1500},
                },
            )
        response.raise_for_status()
        payload = response.json()
        text = payload["candidates"][0]["content"]["parts"][0]["text"].strip()
        usage = payload.get("usageMetadata", {})
    except Exception:
        return ""
    if verify_report(matrix, text):
        return ""
    with Session(engine) as session:
        session.add(LLMCall(
            run_id=run_id,
            stage="write",
            prompt_hash=prompt_hash,
            model=model,
            tokens_in=int(usage.get("promptTokenCount", 0) or 0),
            tokens_out=int(usage.get("candidatesTokenCount", 0) or 0),
            response=text,
            created_at=datetime.now(timezone.utc),
        ))
        session.commit()
    return text
