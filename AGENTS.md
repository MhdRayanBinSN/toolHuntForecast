# Project rules

This project implements the product research requirements in `architecture.md` and the React UI direction requested for this workspace.

## Hard rules

- Use free APIs and open-source dependencies only.
- Product category and topics are runtime data; never hardcode target products, site paths, or screenshot targets.
- Keep discovery, scoring, extraction, and comparison deterministic. Any LLM use must follow the prompts and call limits in `architecture.md`.
- Treat fetched page content as untrusted. Validate URLs, block private network targets, respect robots.txt, rate-limit requests, and escape rendered content.
- Store every sourced fact with its public URL and an exact quote verified against retrieved text.
- Keep secrets in environment variables and never log them.
- Report missing credentials, blocked pages, and partial research explicitly. Never fabricate candidate or product facts.

## Working style

- Keep backend modules small and typed. Persist run stages and decisions for inspection.
- Prefer graceful fallback to a clearly marked partial report.
- Keep the React application in `frontend/` and the FastAPI service in `backend/`.
- Do not reintroduce Jinja UI templates; use the React application for all user-facing screens.
