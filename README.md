# Compare: automated product research

React + Vite frontend and FastAPI backend for discovering software candidates, researching public product pages, and publishing evidence-aware comparison reports.

## Project structure

```text
frontend/                 React UI, Vite config, browser entry point
backend/app/              FastAPI routes, pipeline, services and database
backend/config/            Runtime research topics
backend/requirements.txt   Python dependencies
data/                      Local database, page cache and screenshots
```

## Setup

1. Use Python 3.10 or newer and create a virtual environment.
2. Install the API dependencies with `pip install -r backend/requirements.txt`.
3. Copy `.env.example` to `.env`. Set `PRODUCTHUNT_TOKEN` to enable Product Hunt discovery.
4. Install the Playwright browser with `playwright install chromium` if screenshot capture is needed.
5. In `frontend/`, run `npm install` and `npm run build`.
6. Start the app from the repository root with `uvicorn backend.app.main:app --reload` and open `http://127.0.0.1:8000`.

For React development with hot reload, run `npm run dev` in `frontend/` and `uvicorn backend.app.main:app --reload` from the repository root in a second terminal. Vite proxies API requests to FastAPI. React lives under `/app/` for views that would otherwise overlap with the documented REST API paths; the overview is `/`.

The database and screenshots are stored in `data/`. Without the Product Hunt token, discovery returns no products and a run explains that it needs a discovery source. Reports are generated from retrieved page evidence; the deterministic report path works without an LLM key.

### Environment values

- **Required for product discovery:** `PRODUCTHUNT_TOKEN` from your Product Hunt developer account.
- **Optional:** `CATEGORY` chooses the default category (defaults to `software tools`); `DATABASE_URL` changes the database location; `MAX_PAGES_PER_PRODUCT`, `MIN_SCREENSHOTS`, and `USER_AGENT` tune crawling.
- **Not needed by this build:** Gemini and SaaSHub keys. Those integrations are not currently wired into the pipeline.

Start with `cp .env.example .env`, then add your Product Hunt token. Keep `.env` private and do not commit it. If you leave `PRODUCTHUNT_TOKEN` blank, the API still starts, but discovery runs cannot find candidates.

### Getting API credentials

- **Product Hunt (currently used):** Sign in and create an application from [My Apps](https://www.producthunt.com/v2/oauth/applications). Open the app and copy its developer token into `PRODUCTHUNT_TOKEN`. The API docs say developer tokens are for quick scripts and are linked to your account. Product Hunt says its API is not for commercial use by default; contact them for business use. See the [official API docs](https://api.producthunt.com/v2/docs).
- **SaaSHub (not wired into this version):** Sign in to SaaSHub and open [your API key page](https://www.saashub.com/profile/api_key). Copy the key into `SAASHUB_API_KEY` if/when the alternatives adapter is implemented. SaaSHub labels the API beta and asks API users to disclose that use on their site; see its [API docs](https://www.saashub.com/site/api).
- **Gemini (not wired into this version):** Open [Google AI Studio](https://aistudio.google.com/), go to API Keys, then create or copy a key into `GEMINI_API_KEY`. Keep it server-side. Google’s current docs say new AI Studio keys are authorization keys by default; see [Gemini API key setup](https://ai.google.dev/gemini-api/docs/api-key). Gemini’s free tier has limits; billing applies if you move to a paid tier.

## API

- `POST /runs` with `{"category":"...","mode":"fast"}` starts a background run.
- `GET /runs`, `GET /runs/{id}` provide progress and stage details.
- `GET /reports`, `GET /reports/{id}?format=md` provide report data and Markdown.
- `GET/POST /categories`, `PATCH /categories/{id}` manage categories.
- `GET /candidates` lists collected candidates and scores.

For scheduled runs, configure cron to POST to `/runs`.
# toolHuntForecast
