prompts (do in single pass with high thinking)

### Phase 0: Scaffold
```
Read architecture.md and AGENTS.md. Create the repo layout from section 5,
pyproject/requirements (fastapi, uvicorn, httpx, selectolax, trafilatura,
sqlmodel, pydantic-settings, jinja2, playwright, sentence-transformers,
pillow, numpy, tldextract, pytest, pytest-asyncio, respx, google-genai, pyyaml).
Implement config.py (env + config/topics.yaml), db.py with all tables from
section 7, and GET /health. Add .env.example and README setup steps.
Tests: config loads, tables create. Stop and show results.
```

### Phase 1: Safety, fetcher, cache, rate limit
```
Implement services/safety.py (URL canonicalization to registered domain, SSRF
guard with DNS resolution and redirect re-checks), robots.py, ratelimit.py
(per-domain token bucket), cache.py, and fetcher.py with the ladder in
section 12.1 (httpx -> retry -> Playwright). Unit tests with respx for:
redirect loop, 403, size cap, private IP rejection, robots disallow.
Stop and show results.
```

### Phase 2: Sources
```
Implement sources/base.py, producthunt.py (GraphQL query from section 8.1,
pagination, caching, rate-limit handling), saashub.py (section 8.2, store alt_edge),
and sitemap_diff.py (section 8.3, driven by a directory config list, first run
seeds snapshot only). Save real response fixtures. Add a CLI:
python -m app.sources.run_all. Tests with fixtures. Stop and show results.
```

### Phase 3: Pool, scoring, selection
```
Implement services/embed.py (lazy-loaded sentence-transformers, cached),
pipeline/pool.py (section 9.1), pipeline/scoring.py (9.2-9.3, store all
signals in scores_json, RDAP domain age and Wayback first capture with
graceful failure), and pipeline/select.py (section 10 including backups and
widening the time window). The LLM tie-break is a stub interface for now.
Unit tests for every formula with known inputs. Stop and show results.
```

### Phase 4: Site analysis and extraction
```
Implement pipeline/site.py (section 11: robots, sitemaps incl. gzip/index,
homepage crawl fallback, Playwright link extraction, normalization, page
scoring driven by config/topics.yaml) and pipeline/extract.py (section 12.2,
12.3). Every fact needs source_url and a verified exact quote; drop others.
Test on saved HTML fixtures and on tests/testsite. Stop and show results.
```

### Phase 5: Screenshots
```
Implement pipeline/screenshots.py per section 13: single visit gives screenshot
and DOM text, overlay dismissal by accessible name, quality gates, one retry,
fallback to next-best pages until MIN_SCREENSHOTS. Concurrency 2-3 pages,
close contexts. Test against tests/testsite (cookie banner, blank page, 404).
Stop and show results.
```

### Phase 6: Compare, write, render
```
Implement pipeline/compare.py (section 14 incl. feature clustering),
services/llm.py (section 16: cache, budget, rate limiter, retry, fallback,
Pydantic validation), prompts/ files from section 17, pipeline/write.py with
the code verification rules and template-report fallback, and render.py with
a Jinja2 report template. Test with a mocked LLM and with no LLM key
(template report must still render). Stop and show results.
```

### Phase 7: Orchestrator and API
```
Implement orchestrator.py per section 19 (resumable stages, retries, backup
swap, deadline, partial reports, timers) and all routes in section 18.
Test: kill mid-run and resume; product failure swaps in a backup.
Stop and show results.
```

### Phase 8: UI
```
Implement the UI from section 20: templates/layout, styles.css with design
tokens, Dashboard, Run detail with HTMX polling, Reports list and view,
Categories. Include loading, empty and error states and a /style-guide page.
Stop and show results.
```

### Phase 9: Automation and hardening
```
Add cron instructions and scheduler.py, the collect job, stage timing table
on the run page, cache pruning, and the webhook on failed/partial. Run the
acceptance checklist in section 26 and report which items pass or fail.
```


