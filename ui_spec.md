# ui_spec.md
## UI Specification for the Product Comparison Pipeline

Implementation: React + Vite in `frontend/`, backed by FastAPI in `backend/`. The React application uses the REST API endpoints from `architecture.md` (section 18), works on phones, and supports dark mode. The live interface uses only black, white, and neutral gray. Product A uses near-black and Product B uses charcoal, so they remain distinct without hue. Statuses use gray shades with text labels and borders. The Jinja2 template examples and phase prompts below are legacy references; the design, screens, components, writing rules, and accessibility checklist remain product requirements for the React UI.

---

## 1. Design plan

**Subject:** a research tool that finds two new software products each day and compares them. People use it to start a run, watch it work, and read the result.

**Design idea: a survey bench.** The product is a careful side-by-side comparison, so the UI is built around two things:
1. **The pipeline strip** (the one memorable element): a horizontal row of stages that fills in as a run progresses. It appears on the Dashboard and the Run page.
2. **The comparison spine**: the report places Product A and Product B in two columns that meet at a shared center column of dimension names. Everything else stays quiet.

**Palette (named, all defined as tokens below)**

| Name | Light | Dark | Role |
|---|---|---|---|
| Ink | `#111111` | `#F2F2F2` | Text |
| Fog | `#F7F7F7` | `#111111` | Page background |
| Panel | `#FFFFFF` | `#1B1B1B` | Cards |
| Product A | `#111111` | `#FFFFFF` | Product A accent |
| Product B | `#555555` | `#BDBDBD` | Product B accent |
| Neutral state | `#EEEEEE` | `#292929` | Running, partial and error backgrounds |

Statuses use only grayscale: pending = muted gray, running = medium gray, completed = black, partial = outlined gray, failed = black on light gray. Every status includes a text label.

**Type:** two families, clearly different.
- Headings: *Bricolage Grotesque* (fallback: system sans), semi-bold, slightly tight.
- Body and data: *Instrument Sans* (fallback: system sans), regular and medium.
- Scale: 13, 15, 17, 22, 32 px. Body line height 1.55. Line length under 70 characters in reports.
- No all-caps labels, no accenting a single word in headings, no decorative numbering (numbers appear only where the content is a real sequence, like pipeline stages).

**Principles**
- One bold element (the pipeline strip). The rest is flat, quiet and consistent.
- Cards are used only for items that are separate things (a report, a run). Sections inside a page use spacing and a thin divider, not more cards.
- Two radii only: 10px for containers, 6px for controls.
- Motion only where it shows a change: a stage filling in, a button loading. Respect `prefers-reduced-motion`.
- Structure carries meaning: product A is near-black and product B is charcoal everywhere (table headers, screenshot frames, strengths lists).

## 2. Screens and wireframes

### 2.1 Dashboard
```
+----------------------------------------------------------+
| Compare       Reports   Runs   Categories        [theme] |
+----------------------------------------------------------+
| Category  [ cold email generation  v ]   [ Start run ]   |
|                                                          |
| Pipeline strip (live)                                    |
| (1 Collect)-(2 Score)-(3 Select)-(4 Research)-(5 Shots)  |
|        -(6 Compare)-(7 Write)-(8 Publish)                |
|   Running: Research. 2 of 5 pages done for Product A     |
|                                                          |
| Latest report                                            |
| +---------------------------+  Recent runs               |
| | A  vs  B   thumbnails     |  Today      completed 3m   |
| | one-line summary          |  Yesterday  partial   5m   |
| | [Read report]             |  ...                       |
| +---------------------------+                            |
+----------------------------------------------------------+
```
When no run is active, the strip shows the last run, greyed.

### 2.2 Run detail
Pipeline strip on top, then a table of stages: name, status badge, duration, and a "Details" expander showing the decision (scores, chosen pair, pages picked, errors). A "Slowest steps" list sits under the table. Polls every 3 seconds while the run is active.

### 2.3 Reports list
Grid of report cards (1 column on phones, 2 on tablets, 3 on desktop): date, "A vs B", two small thumbnails, status badge (completed or partial), category.

### 2.4 Report view (the main page)
```
Title: A vs B for <category>        date, status, coverage
TL;DR   (3 lines)
+---------------------------------------------------------+
|        Product A (black)  | dimension |  Product B (gray)  |
|        $19 / month        | Price     |  per credit       |
|        yes                | Free plan |  no               |
+---------------------------------------------------------+
Screenshots: two rows, A on the left in a black frame, B on the right in gray
Strengths and limits: two columns
Who may prefer which
Sources and method (collapsed)
```
On phones the comparison table keeps three columns and scrolls sideways inside its own container. Screenshots open full size in a dialog.

### 2.5 Categories
A simple list with a name, keyword chips, and an active toggle. "Add category" opens an inline form (name, optional keywords).

### 2.6 Candidates (optional)
Table of the pool: name, domain, sources, novelty score, status. Sort by score.

### 2.7 `/style-guide`
One page listing every component in every state. Build it first and check it before building screens.

## 3. Components (build once, reuse)

| Component | Class | Notes |
|---|---|---|
| Button | `.btn`, `.btn-primary`, `.btn-quiet`, `.btn-danger` | Loading state shows a spinner and disables the button |
| Card | `.card` | Only for separate items |
| Status badge | `.badge[data-status]` | Text plus color, never color alone |
| Pipeline strip | `.strip`, `.strip-step[data-state]` | States: pending, running, done, failed |
| Table | `.table-wrap > table` | Wrap scrolls horizontally |
| Compare table | `.compare` | A and B header colors, center dimension column |
| Screenshot figure | `.shot` | Frame color by product, caption, click to enlarge |
| Chip | `.chip` | Keywords and sources |
| Empty / loading / error | `.state-empty`, `.skeleton`, `.state-error` | Every list needs all three |
| Toast | `.toast` | Same verb as the action ("Run started") |

## 4. Writing rules for UI text
- Sentence case, active voice, plain words: "Start run", "Read report", "Add category".
- The same word for the same thing everywhere: Run, Report, Category, Product.
- Empty states tell the next step: "No reports yet. Start a run to create the first one."
- Errors say what happened and what to do: "The run stopped at Research because Product B blocked the crawler. A backup product was used instead."
- Never apologize in errors, and never say "something went wrong" without details.

## 5. Legacy stylesheet example (not used by the React app)

The CSS below is retained as historical reference only. The live React theme is implemented in `frontend/src/styles.css` and uses grayscale tokens.

```css
/* Tokens ---------------------------------------------------------- */
:root {
  --ink: #16222B;  --ink-soft: #52616C;  --fog: #F4F6F6;  --panel: #FFFFFF;
  --line: #D9E0E3;
  --teal: #0E6B64; --teal-soft: #DDF0EE;
  --plum: #7A3E8E; --plum-soft: #F0E4F4;
  --amber: #B26B00; --amber-soft: #FBEBD0;
  --red: #B3261E;  --red-soft: #F9DEDC;
  --r-box: 10px;   --r-ctl: 6px;
  --s1: 4px; --s2: 8px; --s3: 12px; --s4: 16px; --s5: 24px; --s6: 32px; --s7: 48px;
  --font-head: "Bricolage Grotesque", ui-sans-serif, system-ui, sans-serif;
  --font-body: "Instrument Sans", ui-sans-serif, system-ui, sans-serif;
  --t-s: 13px; --t-m: 15px; --t-l: 17px; --t-xl: 22px; --t-2xl: 32px;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ink: #E6ECEF; --ink-soft: #9FB0BA; --fog: #10171C; --panel: #18222A; --line: #2A3A45;
    --teal: #4FC3B8; --teal-soft: #14332F;
    --plum: #C79AD6; --plum-soft: #2C1D33;
    --amber: #F0B24A; --amber-soft: #382A10;
    --red: #F2867F; --red-soft: #3A1B19;
  }
}
:root[data-theme="dark"] {
  --ink: #E6ECEF; --ink-soft: #9FB0BA; --fog: #10171C; --panel: #18222A; --line: #2A3A45;
  --teal: #4FC3B8; --teal-soft: #14332F; --plum: #C79AD6; --plum-soft: #2C1D33;
  --amber: #F0B24A; --amber-soft: #382A10; --red: #F2867F; --red-soft: #3A1B19;
}

/* Base ------------------------------------------------------------ */
*, *::before, *::after { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--fog); color: var(--ink);
  font: 400 var(--t-m)/1.55 var(--font-body); }
h1, h2, h3 { font-family: var(--font-head); font-weight: 600; letter-spacing: -0.01em;
  line-height: 1.2; margin: 0 0 var(--s3); }
h1 { font-size: var(--t-2xl); } h2 { font-size: var(--t-xl); } h3 { font-size: var(--t-l); }
p { margin: 0 0 var(--s3); max-width: 70ch; }
a { color: var(--teal); text-underline-offset: 3px; }
:focus-visible { outline: 3px solid var(--teal); outline-offset: 2px; }
img { max-width: 100%; height: auto; display: block; }

/* Layout ---------------------------------------------------------- */
.topbar { display: flex; align-items: center; gap: var(--s5); padding: var(--s3) var(--s5);
  background: var(--panel); border-bottom: 1px solid var(--line); position: sticky; top: 0; z-index: 10; }
.brand { font: 600 var(--t-l) var(--font-head); color: var(--ink); text-decoration: none; }
.nav { display: flex; gap: var(--s4); margin-left: auto; flex-wrap: wrap; }
.nav a { color: var(--ink-soft); text-decoration: none; padding: var(--s1) 0; }
.nav a[aria-current="page"] { color: var(--ink); border-bottom: 2px solid var(--teal); }
.page { max-width: 1080px; margin: 0 auto; padding: var(--s6) var(--s4) var(--s7); }
.section + .section { margin-top: var(--s6); padding-top: var(--s6); border-top: 1px solid var(--line); }
.grid { display: grid; gap: var(--s4); grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); }
.row { display: flex; gap: var(--s3); align-items: center; flex-wrap: wrap; }

/* Card / buttons / badges / chips -------------------------------- */
.card { background: var(--panel); border: 1px solid var(--line); border-radius: var(--r-box);
  padding: var(--s4); }
.btn { font: 500 var(--t-m) var(--font-body); padding: 10px var(--s4); min-height: 44px;
  border-radius: var(--r-ctl); border: 1px solid var(--line); background: var(--panel);
  color: var(--ink); cursor: pointer; }
.btn-primary { background: var(--teal); border-color: var(--teal); color: #fff; }
:root[data-theme="dark"] .btn-primary { color: #06201D; }
.btn-quiet { background: transparent; border-color: transparent; color: var(--teal); }
.btn-danger { background: var(--red); border-color: var(--red); color: #fff; }
.btn[disabled], .btn[aria-busy="true"] { opacity: .6; cursor: progress; }
.badge { display: inline-block; padding: 2px var(--s2); border-radius: 999px;
  font-size: var(--t-s); font-weight: 500; border: 1px solid currentColor; }
.badge[data-status="pending"]   { color: var(--ink-soft); }
.badge[data-status="running"]   { color: var(--amber); background: var(--amber-soft); }
.badge[data-status="completed"] { color: var(--teal);  background: var(--teal-soft); }
.badge[data-status="partial"]   { color: var(--amber); }
.badge[data-status="failed"]    { color: var(--red);   background: var(--red-soft); }
.chip { display: inline-block; padding: 2px var(--s2); border-radius: var(--r-ctl);
  background: var(--fog); border: 1px solid var(--line); font-size: var(--t-s); }

/* Pipeline strip (the one bold element) -------------------------- */
.strip { display: flex; gap: 0; overflow-x: auto; padding: var(--s3) 0; list-style: none; margin: 0; }
.strip-step { flex: 1 0 96px; position: relative; text-align: center; font-size: var(--t-s);
  color: var(--ink-soft); padding-top: 28px; }
.strip-step::before { content: ""; position: absolute; top: 8px; left: 50%; width: 14px; height: 14px;
  margin-left: -7px; border-radius: 50%; background: var(--panel); border: 2px solid var(--line); z-index: 1; }
.strip-step::after { content: ""; position: absolute; top: 14px; left: -50%; width: 100%; height: 2px;
  background: var(--line); }
.strip-step:first-child::after { display: none; }
.strip-step[data-state="done"]    { color: var(--ink); }
.strip-step[data-state="done"]::before    { background: var(--teal); border-color: var(--teal); }
.strip-step[data-state="done"]::after     { background: var(--teal); }
.strip-step[data-state="running"] { color: var(--ink); font-weight: 600; }
.strip-step[data-state="running"]::before { background: var(--amber); border-color: var(--amber);
  box-shadow: 0 0 0 4px var(--amber-soft); }
.strip-step[data-state="failed"]::before  { background: var(--red); border-color: var(--red); }

/* Tables ---------------------------------------------------------- */
.table-wrap { overflow-x: auto; border: 1px solid var(--line); border-radius: var(--r-box);
  background: var(--panel); }
table { border-collapse: collapse; width: 100%; font-size: var(--t-m); }
th, td { text-align: left; padding: var(--s3) var(--s4); border-bottom: 1px solid var(--line);
  vertical-align: top; }
tr:last-child td { border-bottom: 0; }
th { font-weight: 600; color: var(--ink-soft); font-size: var(--t-s); }

/* Compare table: A | dimension | B ------------------------------- */
.compare th.a, .compare td.a { border-left: 4px solid var(--teal); }
.compare th.b, .compare td.b { border-right: 4px solid var(--plum); }
.compare th.a { color: var(--teal); } .compare th.b { color: var(--plum); }
.compare td.dim { text-align: center; color: var(--ink-soft); background: var(--fog); font-weight: 500; }
.compare td.win-a { background: var(--teal-soft); } .compare td.win-b { background: var(--plum-soft); }
.compare td[data-verdict="unknown"], .compare td[data-verdict="not_comparable"] { color: var(--ink-soft); }

/* Screenshots ----------------------------------------------------- */
.shots { display: grid; gap: var(--s4); grid-template-columns: 1fr 1fr; }
.shot { margin: 0; border: 3px solid var(--line); border-radius: var(--r-box); overflow: hidden;
  background: var(--panel); }
.shot.a { border-color: var(--teal); } .shot.b { border-color: var(--plum); }
.shot figcaption { padding: var(--s2) var(--s3); font-size: var(--t-s); color: var(--ink-soft); }
.shot img { cursor: zoom-in; }

/* States ---------------------------------------------------------- */
.state-empty, .state-error { padding: var(--s5); border: 1px dashed var(--line);
  border-radius: var(--r-box); color: var(--ink-soft); }
.state-error { border-style: solid; border-color: var(--red); background: var(--red-soft); color: var(--ink); }
.skeleton { background: linear-gradient(90deg, var(--line), var(--fog), var(--line));
  background-size: 200% 100%; border-radius: var(--r-ctl); min-height: 1em; animation: sk 1.4s linear infinite; }
@keyframes sk { to { background-position: -200% 0; } }
.toast { position: fixed; right: var(--s4); bottom: var(--s4); background: var(--ink); color: var(--fog);
  padding: var(--s3) var(--s4); border-radius: var(--r-ctl); }

/* Responsive ------------------------------------------------------ */
@media (max-width: 640px) {
  .topbar { padding: var(--s3) var(--s4); gap: var(--s3); }
  .page { padding-top: var(--s5); }
  h1 { font-size: var(--t-xl); }
  .shots { grid-template-columns: 1fr; }
}
@media (prefers-reduced-motion: reduce) {
  * { animation: none !important; transition: none !important; }
}
```

## 6. Key templates (Jinja2 + HTMX)

### `templates/layout.html`
```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}Compare{% endblock %}</title>
  <link rel="stylesheet" href="/static/styles.css">
  <script src="/static/htmx.min.js" defer></script>   <!-- download once, self-host -->
</head>
<body>
  <header class="topbar">
    <a class="brand" href="/">Compare</a>
    <nav class="nav" aria-label="Main">
      <a href="/" {% if page=='home' %}aria-current="page"{% endif %}>Dashboard</a>
      <a href="/ui/reports" {% if page=='reports' %}aria-current="page"{% endif %}>Reports</a>
      <a href="/ui/runs" {% if page=='runs' %}aria-current="page"{% endif %}>Runs</a>
      <a href="/ui/categories" {% if page=='cats' %}aria-current="page"{% endif %}>Categories</a>
    </nav>
  </header>
  <main class="page">{% block content %}{% endblock %}</main>
</body>
</html>
```

### Pipeline strip partial (`templates/_strip.html`), polled by HTMX
```html
<div hx-get="/ui/runs/{{ run.id }}/strip" hx-trigger="every 3s [{{ 'true' if run.status in ('pending','running') else 'false' }}]" hx-swap="outerHTML">
  <ol class="strip" aria-label="Run progress">
    {% for s in stages %}
      <li class="strip-step" data-state="{{ s.state }}">{{ s.label }}</li>
    {% endfor %}
  </ol>
  <p>{{ run.message }}</p>
</div>
```
`s.state` is one of `pending|running|done|failed`. Stop polling when the run is finished.

### Status badge macro
```html
{% macro badge(status) %}<span class="badge" data-status="{{ status }}">{{ status|capitalize }}</span>{% endmacro %}
```

### Screenshot dialog (no JS framework)
```html
<dialog id="viewer"><img id="viewer-img" alt=""><form method="dialog"><button class="btn">Close</button></form></dialog>
<script>
document.addEventListener('click', e => {
  const img = e.target.closest('.shot img'); if (!img) return;
  document.getElementById('viewer-img').src = img.src;
  document.getElementById('viewer-img').alt = img.alt;
  document.getElementById('viewer').showModal();
});
</script>
```

## 7. Accessibility and quality checklist
- [ ] Every status shows text, not only color
- [ ] Contrast at least 4.5:1 for text in light and dark modes
- [ ] Keyboard: all controls reachable, visible focus ring, dialog closes with Escape
- [ ] Alt text on every screenshot (the caption works)
- [ ] Tap targets at least 44px
- [ ] No horizontal scroll on the page body at 360px width
- [ ] Reduced motion respected
- [ ] Each list has loading, empty and error states
- [ ] Report page prints cleanly (add a small `@media print` block hiding the nav)

## 8. Prompts for the coding agent (UI phases)

Use these after the backend API works. Paste one at a time.

### UI-1: Foundation (legacy Jinja workflow; do not use for the React app)
```
Read ui_spec.md. Create static/styles.css exactly from section 5, templates/layout.html
from section 6, and a /style-guide page that shows every component in section 3
in light and dark mode (buttons incl. loading, badges for all statuses, strip with
all states, table, compare table, screenshot figures, empty/loading/error states).
Serve static files and template routes from FastAPI. Do not use any CSS framework
or build step. Stop and tell me what to check in the browser.
```

### UI-2: Dashboard and live strip (legacy Jinja workflow)
```
Build the Dashboard from section 2.1: category select, "Start run" button that
POSTs /runs (show toast "Run started"), the pipeline strip partial polled with
HTMX every 3 seconds only while a run is active, latest report card, recent runs list.
Use the existing API endpoints. Include empty states. Stop and show how to test it.
```

### UI-3: Run detail (legacy Jinja workflow)
```
Build /ui/runs and /ui/runs/{id} from section 2.2: stage table with status badges,
durations, an expandable details row showing the decision/scores/errors, and a
"Slowest steps" list. Poll while active. Stop and show how to test it.
```

### UI-4: Reports (legacy Jinja workflow)
```
Build /ui/reports (card grid) and /ui/reports/{id} from sections 2.3 and 2.4:
TL;DR, compare table with A teal and B plum and verdict highlighting, screenshots
in colored frames with a click-to-enlarge dialog, strengths/limits in two columns,
who-may-prefer-which, collapsible sources and method. Render stored Markdown
safely (escape HTML). Add @media print rules. Stop and show how to test it.
```

### UI-5: Categories and polish (legacy Jinja workflow)
```
Build /ui/categories from section 2.5 with add and activate/deactivate through
the existing API. Then run through the checklist in section 7, fix failures,
and report which items pass. Add a light/dark theme toggle that sets
data-theme on <html> and remembers the choice in localStorage.
```

## 9. Working on a free coding plan
- I could not verify the limits of the plan or model you named ("Luna 5/6"), so assume small context and tight quotas.
- Keep each prompt to one screen. If the agent runs out of room, split the screen further (for example, "table only", then "details row only").
- Put `architecture.md`, `ui_spec.md` and `AGENTS.md` in the repo. Tell the agent to read only the sections each prompt names, not the whole files.
- Commit after every prompt. If a session degrades, start a new one and point it at the last commit.
- Test in the browser after each step and paste back only the error text or a short description.
