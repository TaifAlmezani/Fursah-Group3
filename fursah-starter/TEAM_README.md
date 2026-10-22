# FURSAH — Team plan and progress

Our team's working document for the FURSAH exercise: the design we chose, what is
built so far, how to run it, and what comes next. The course guides (`README.md`
files in each folder) describe the exercise itself; this file describes **our**
implementation. Update it whenever a step changes.

_Last updated: 22 October 2026. Day 4 evaluation complete: 9 → 23 of 24 shared cases pass; one
live search traced in Phoenix (1 trace, 16 spans, 3 model calls). Optional web search (Tavily) done,
with a loading spinner. Nothing is committed yet (team decision for now). Step 6 not started._

## Progress

| # | Step (from the course guide) | Status |
|---|---|---|
| 1 | Start the backend and frontend | ✅ Done |
| 2 | Data notebook: understand the announcements | ✅ Done |
| 3 | Prepare and store all 1,000 announcements | ✅ Done |
| 4 | Details endpoint and storage readiness | ✅ Done |
| 5 | Search: LangChain agent, tools and validation | ✅ Done: live-tested with the real model (12 problems found and fixed); offline checks pass |
| + | Optional web search (Tavily), team addition | ✅ Done: button with loading spinner on a no-match result; Tavily key added; works live |
| 6 | Full corpus: group related announcements, handle conflicts | ⬜ Not started |
| D4 | Day 4 evaluation: 2.1 audit · 2.2 tracing · 2.3 graders · 2.4 diagnose | ✅ All four parts done: **9 → 23 of 24 cases pass** ([2.1](day4/2.1-audit/audit.md), [2.2](day4/2.2-instrument/results.md), [2.3](day4/2.3-graders/results.md), [2.4](day4/2.4-diagnose/results.md)); Phoenix trace checked live. DEV-07 open: contradicts DEV-02 (question for instructors) |

## Our design

| Decision | Choice | Why |
|---|---|---|
| Storage | **SQLite** (`data/prepared/fursah.db`) | The agent's search tool filters by city, type, budget and dates; SQL does this directly. |
| Fact extraction | **Rule-based** (regex), no model | The announcements reuse a fixed set of sentences, so rules cover the data fully, cost nothing and give the same result every run. |
| Search and recommendation | **One LangChain agent** on an **OpenRouter** model | Understands free Arabic queries, calls the search tools and picks the best 1–3. |
| Checking the agent | **Plain-Python validation** around the agent | The model cannot reliably check itself; rules such as "deadline passed means expired" must never be wrong. |
| Web search | **Optional, only when the user asks**, after a no-match result; kept apart from recommendations | Recommendations must cite source IDs from our dataset, so web results are shown separately as unverified external links (see [Web search](#web-search-optional)). |

Defaults we are using until the team decides otherwise:

- Opportunities with an **unknown fee** are excluded from budget searches (changed on Day 4; they
  used to stay with a caveat).
- Topic search starts with **keywords plus Arabic spelling normalization**; embeddings can come later.
- **Grouping related announcements** (original, update, repost) happens in step 6. Until then the
  same opportunity ID is never recommended twice, and the agent is told to prefer different
  opportunities, but two announcements of one activity can still appear as separate results.

## How a search works

This is the full path of one search, from the user pressing "اكتشف الفرص" to the cards on screen.

![How a search works: frontend → discover endpoint → understand, resolve, pre-check, agent, validate, respond; optional web search after no match](docs/search-workflow.png)

In practice `needs_info` leaves at ② and the pre-check's `no_match` at ②b, before the agent. The text
version below adds the details the picture leaves out: errors, the held-back final call, and web-search cost.

```
Frontend: query + profile + saved constraints
   │  POST /api/v1/discover   (FastAPI checks the request shape; bad input → 422)
   ▼
┌─ workflow.discover() ──────── at most 6 model calls, must finish within 90 s ──────┐
│                                                                                    │
│ ① UNDERSTAND   model call → QueryPlan (cities, types, modes, budget, dates, topics) │
│                the model also sees the data's 31 subjects and picks those the      │
│                request is about ("تقنية" → Python, SQL, Power BI, Excel)           │
│                code then reads the query itself and corrects the plan:             │
│                cities, exclusions, price, unasked types/modes, subjects, topics    │
│                plan still wrong → re-ask the model once                            │
│                                                                                    │
│ ② RESOLVE      code merges the plan with the saved form constraints                │
│                clash (Jeddah vs. saved Riyadh) → stop: needs_info, ask the user    │
│                                                                                    │
│ ②b PRE-CHECK   database search with all filters and topics; nothing found →        │
│                stop: explained no_match, no agent (1 model call in total)          │
│                                                                                    │
│ ③ AGENT        LangChain agent loop, up to 8 steps                                 │
│      ├─ search_opportunities ──► repository.search() ──► SQLite (fursah.db)        │
│      │     hard filters applied in SQL; agent may narrow, never widen              │
│      ├─ (search results carry every fact: no other tools)                          │
│      └─ returns 1–3 picks with Arabic reason, caveats, next action                 │
│                                                                                    │
│ ④ VALIDATE     code checks every pick against the database records                 │
│                invalid picks dropped; all rejected → stop with an error (no re-run)│
│                                                                                    │
│ ⑤ RESPOND      facts taken from the database, not the model                        │
│                ok · needs_info · no_match  (+ readable interpreted_filters)         │
└────────────────────────────────────────────────────────────────────────────────────┘
   │
   └─ no_match → button "ابحث في مواقع خارجية موثوقة" → only if the user clicks:
      POST /api/v1/web-search → web agent + Tavily (trusted sites) → unverified links
      (see Web search)
```

**Who does what:**

| Stage | Done by | Why |
|---|---|---|
| ① Understand the query | Model (1 call), then code corrections and a check | Free Arabic text needs a language model; code reads what it can read reliably (cities, negations, price) |
| ② Resolve constraints | Code | Strict rules that must never be wrong |
| ②b Pre-check | Code (database) | No model call is spent when the data has nothing |
| ③ Search the database | Code, called by the agent | Filters must hold whatever the model does |
| ③ Choose and explain | Model (agent) | Judging relevance and writing reasons |
| ④ Validate | Code | A model cannot reliably check its own output |
| ⑤ Build the response | Code | Must match the API contract and the details endpoint exactly |
| Web search (optional) | Model (web agent) + Tavily, then code link checks | Only on the user's request; links must come from Tavily and trusted sites |

**Model calls per search:**

| Case | Calls |
|---|---|
| Normal search (understand → search → answer) | **3** (live tests: 3) |
| Conflict with saved preferences, or nothing in the database | **1** |
| Agent searches again with other words | 4–5 |
| Maximum, enforced by code | **6** (see [Cost control](#cost-control)) |
| Web search, if the user clicks | up to 4 more, plus up to 2 Tavily credits |

### ① Understand the query

`agent.interpret_query()` sends the query to the model with a fixed instruction and asks for a
`QueryPlan` back, a structured object rather than free text. The instruction includes the list of
the data's 31 subjects, so the model compares the request with what we actually have instead of
guessing keywords:

| Field | Example from "ورشة تصميم في جدة أو الرياض مو أونلاين بأقل من 100 ريال" |
|---|---|
| `cities` | جدة، الرياض (alternatives keep every option) |
| `exclude_cities` | — ("مو في الرياض" would go here) |
| `opportunity_types` | workshop |
| `delivery_modes` / `exclude_delivery_modes` | — / online ("مو أونلاين" is an exclusion) |
| `max_budget_sar` | 100 ("مجاني" gives 0) |
| `available_from`, `available_to`, `max_duration_days` | only if stated |
| `topics` | تصميم plus close synonyms, at most 8 |
| `subjects` | The data's subjects the request is about, copied from the list of 31 the model is given: here تصميم الملصق الثقافي, تصميم هوية مبسطة… (code drops any name not on the list) |
| `unsupported` | Notes on what the data lacks, e.g. jobs |

**Code corrects the plan first** (`ground_plan()`), because `gpt-4o-mini` has made mistakes in live
tests (excluding the city the user asked for, dropping the topic, excluding online for "أو عن بعد").
For things code can read reliably from the query, the query wins over the model:

| What | How code reads it |
|---|---|
| Cities (all 24 frontend cities) | A named city is included; a city right after a negation ("مو في الرياض") is excluded. Prefixes such as "بالرياض" are handled; "مكة" → "مكة المكرمة"; "المدينة" alone is not a city |
| Excluded modes | Only when a **whole** negation word comes right before the mode ("مو أونلاين", "بدون حضور"); the model's exclusions are ignored |
| "[City] أو عن بعد" | Means either: no online-only filter (the city filter already admits online) |
| Price | "بأقل من 100 ريال" → 100, "مجاني" → 0, if the model left it empty |
| Topics | (1) One of the data's 31 known subjects named in the query ("مقدمة في Python"); else (2) the data's subjects **the model related the request to** (the model sees the list of 31 subjects and picks those the request is about: "تقنية" → Python, SQL, Power BI, Excel; "ريادة الأعمال" → إدارة مشروع صغير; code keeps only names really on the list) **plus** the query's own phrases found in any announcement; else (3) the model's topics with synonyms; else (4) the query's leftover subject words, even if not in the data ("علم الفلك" → explained no match). Request words such as "احتاج" never become topics |

Then `check_plan()` checks the **meaning** of the corrected plan. It rejects the plan if a named city
is missing, a negated "online" is not excluded, online is both requested and excluded, or a riyal
price was dropped. Since code fixes these first, a re-ask is rare: the model is asked again once,
and if it still fails the search stops with `FURSAH_INVALID_MODEL_OUTPUT` rather than searching with
the wrong filters.

**The opposite mistake: filters the user never asked for.** `ground_plan()` keeps a type or
delivery-mode filter only if the query contains a word for it:

| Filter | Kept only if the query contains |
|---|---|
| workshop | ورشة / ورش |
| course | دورة / دورات / كورس / برنامج تعليمي |
| internship | تدريب / متدرب |
| competition | مسابقة / هاكاثون / تحدي / منافسة |
| volunteering | تطوع / متطوع |
| event | فعالية / لقاء / ملتقى / أمسية / محاضرة / ندوة |
| in person / online / hybrid | حضوري·حضور / أونلاين·عن بعد / مدمج·هجين |

"فرصة", "فرص", "نشاط" and "وظيفة" support no type. A removed filter is reported on screen
("ملاحظة: لم تحدد نوع فرصة بعينه، فبحثنا في جميع الأنواع."). This is code, not a model call.

**Things the data does not offer.** The plan has an `unsupported` field: the model notes
anything the user asked for that the data lacks. The data has no jobs, so "وظيفة" produces
"لا تتضمن البيانات وظائف؛ نعرض فرصاً تدريبية مرتبطة بطلبك." Code adds the same note if the model
forgets. Notes appear under the interpreted filters, and the agent's caveats say a pick is a
training opportunity, not a job.

### ② Resolve constraints

`workflow.resolve_constraints()` combines the query plan with the constraints saved in the form.
The result is **one set of constraints, used for both searching and checking**.

| Constraint | Rule |
|---|---|
| City, type, delivery mode | Only one side set → use it. Both set and overlapping → keep the overlap. Both set and different → **conflict** |
| Excluded cities / modes | Removed from the allowed lists and passed to the search |
| Budget, latest end date, maximum duration | The stricter value wins |
| Earliest start date | The later date wins |
| Topics | From the query; if it has none, the profile's interests |

On a conflict the search stops and returns `needs_info` with `constraint_conflict: true`, the field
to resolve (for example `constraints.location_preference`) and an Arabic explanation. The frontend
then resends the search with `constraint_resolution: "query"` or `"profile"`, which applies to that
search only. **The saved profile is never changed.**

The resolved constraints are also turned into readable Arabic `interpreted_filters`
("المدينة: جدة (أو عن بعد)", "استبعاد: عن بعد", "الحد الأقصى للرسوم: 100 ريال", …) so the user can see
what was searched.

### ③ How the database is searched

The agent never reads the database directly. It calls the `search_opportunities` tool, which calls
`repository.search()`. That function opens `fursah.db` **read-only** and runs one SQL query.

**Always removed**, whatever the request:

- application status `cancelled` or `closed`;
- deadline before today (today = the evaluation date in Riyadh time). A deadline of **today** is kept
  with a caveat, because no closing time is stated;
- activity already ended (end date before today);
- registration not yet open (status "لم يبدأ التسجيل" with an opening date after today). An opening
  date that has passed stays, with status "requires verification".

Explicit update posts have already been applied to the announcement they name when the store was
built (new deadline, status, start date), so an original never shows outdated facts.

**Hard filters** from the resolved constraints:

| Filter | Rule in SQL |
|---|---|
| Type, delivery mode | Must be one of the allowed values; excluded modes removed |
| City | Must be an allowed city, **or** fully online (no travel needed) |
| Excluded city | Removed |
| Budget | Cost **known** and at or under the limit (an unknown fee is excluded: unknown is not free) |
| Available from / to | Starts on or after / ends on or before, **or dates unknown** |
| Maximum duration | Duration at or under the limit, **or unknown** |

Unknown dates and duration do not exclude an opportunity; the candidate carries an Arabic caveat
("مواعيد النشاط غير مذكورة؛ …"). An unknown **fee** with a budget set is excluded (Day 4 change: it
cannot be shown to fit the budget, and the evaluation treats it as wrong).

**Ranking by topic.** Text is normalised first (أ/إ/آ → ا, ة → ه, ى → ي, diacritics removed) so
spelling variants match. A topic must **start a word**, optionally after Arabic prefixes (و ف ب ل ك،
ال، لل), with any ending: "صحي" matches "الصحية", but "طب" does not match "تطبيق". Each topic scores
**3** if found in the title, **2** in the activity description, **1** anywhere else in the
announcement. When the user named a city, a match located there gets **+1**, so it ranks above the
same match online. When topics are given, candidates scoring 0 are dropped. Results are sorted by
score, then by earliest start date, and the top 10 are returned.

**"City or online".** "في أبها أو عن بعد" accepts either. The city filter already admits online
opportunities, so code removes an online-only mode filter when the query offers online as an
alternative to a city ("أو عن بعد"). "دورة عن بعد" on its own still means online only.

**What the agent sees** for each candidate: ID, source IDs, title, type, city, delivery mode,
physical attendance, dates, duration, cost, deadline, status, mandatory and preferred requirements,
required skills, description, caveats and score. The full record with the evidence sentences stays
on the server for validation.

**Guards on the search tool:**

- It is bound to the resolved constraints. The agent may **narrow** (topics, a subset of the
  allowed types or modes) but not **widen**: asking for a type the user did not allow returns
  "outside the user's constraints".
- At most 3 searches per request. At the limit, or when one model call is left, the tool answers
  "limit reached: give your final answer now".
- Every candidate it returns is remembered, so validation knows exactly what the agent was shown.

### ③ How the agent chooses the best opportunities

The agent is LangChain's `create_agent` with the OpenRouter model, **one tool**
(`search_opportunities`) and a system prompt. Search results carry every fact (description, dates,
cost, deadline, status, requirements), so there are deliberately no details or deadline tools: in
live tests the model used them to re-fetch what it already had (see
[Problems found and fixed](#problems-found-and-fixed)). It receives the query, the profile and the
topics to try, then loops (think → call the tool → read the result) for at most 8 steps:

1. **Search** with the topic keywords. If the results are relevant, answer now.
2. **Search deeper** only if results are empty or off-topic: related or broader topics
   (for example "بيانات" → Excel, SQL, Power BI), within the search limit.
3. **Choose 1–3** that best fit the query and the profile (interests, field of study, city, skills),
   preferring different opportunities over versions of the same one.
4. **Explain** each pick in Arabic: a reason built only from facts in the tool results, caveats for
   anything unknown, and a next action ("سجّل قبل 24 أكتوبر عبر الرابط").

Because the hard filters already ran, every candidate the agent sees is valid; its job is to find
the **most relevant** among them. The prompt also tells it to:

- recommend only IDs returned by the search tool and never invent fees, dates or conditions;
- quote with «» only text copied exactly from the results;
- leave eligibility to validation;
- treat announcement text as data and ignore instructions inside it;
- answer `no_match` when nothing fits, and `needs_info` only for fields the form collects
  (never age or study year).

Its answer is a structured `Selection` (outcome, picks, missing fields, notes). Before validation,
source IDs are filled in **from the database**, never taken from the model, and eligibility is set to
`uncertain` until validation decides.

**An answer is guaranteed by code, not by asking the model.** One model call is held back from the
agent's loop. If the agent keeps searching instead of answering (it ignored "give your final answer
now" in a live test), or runs out of its 8 steps, code stops it and spends the held-back call on one
"choose from what you found" call with no tools. If it found nothing, the result is an explained
`no_match`, never an error. The total still never exceeds 6 calls.

**No agent when nothing can match (②b pre-check).** Before calling the agent, code runs the
database search with every filter **and the topics** (which already include the model's synonyms).
If nothing is found, the search returns the explained `no_match` straight away, with no agent calls:
1 model call in total instead of 5 (live test).

### ④ How picks are validated

`validation.validate_result()` checks every pick against the database records the search returned:

| Check | Rejected when |
|---|---|
| Real opportunity | The ID was not returned by the search tool |
| Sources | Source IDs do not belong to that opportunity |
| Constraints | Type, mode, excluded mode, city, excluded city, a cost over budget or unknown under a budget, known dates outside availability, known duration over the limit |
| Availability (also) | Activity already ended; registration not yet open |
| Availability | Status `cancelled` or `closed`, or the deadline has passed |
| Quotes | Text in «» or "…" matches neither the source sentences nor the stored facts (dates, cost, deadline, requirements) the search showed for that opportunity |
| Skills | The profile lists skills but not one the opportunity requires |

**Eligibility** is decided here, not by the model:

- `confirmed` only when the announcement states its mandatory requirements **and** the profile meets
  every one (in this data, that means listing each required skill);
- `uncertain` when requirements are not stated, or include age or final-year study (the form does
  not collect them), or depend on skills the user has not entered. The reason is added to the caveats.

**Unknown skills never hide results.** If a pick requires a skill and the profile's skills are
unknown (`null`), the pick is still shown, `uncertain`, with a caveat naming the skill
("يشترط الإعلان معرفة Excel، ولم تحدد مهاراتك بعد."); adding skills to the profile confirms it. A pick
is rejected only when the profile lists skills and the required one is not among them. Skills `[]`
(the user said none) is different from `null` (not answered).

**Other rules:** duplicate IDs are dropped, at most 3 picks are kept, and a `needs_info` from the
agent may only name fields the form collects.

**No re-run.** The agent runs once. Picks that fail validation are dropped and the rest are kept.
If every pick fails, the search stops with `FURSAH_VALIDATION_ERROR` instead of running the agent
again, so a failing search cannot keep spending model calls. Bad output is never accepted just to
avoid an error.

### ⑤ The response

Each recommendation is built from the **same database record** the details endpoint serves, so a
card and its details panel always agree:

- From the database: title, type, location (or "عن بعد"), delivery mode, deadline, current
  registration status, source IDs, evidence version.
- From the model: reason, next action, relevance score.
- From validation: eligibility and the merged caveats.

| Outcome | When |
|---|---|
| `ok` | 1–3 recommendations passed validation |
| `needs_info` | A conflict between the query and the saved constraints |
| `no_match` | Nothing in the data fits, even after a deeper search; the message says why (below) |
| error | See below; no fake results are ever returned |

**Explaining an empty result.** `workflow.explain_no_match()` writes an Arabic message that says
why nothing was found and what to change, using **database counts only** (no model call):

| Situation | Message (example) |
|---|---|
| The topic is not in the open data at all | "لا توجد في البيانات فرص مفتوحة حالياً عن «علم الفلك». جرّب موضوعاً قريباً." |
| A combination of filters empties it | The search is re-counted with one filter removed at a time: "لا توجد فرص تجمع كل شروط بحثك. بدون شرط نوع الفرصة توجد 7 فرص؛ بدون شرط المدينة توجد فرصة واحدة. جرّب تعديل هذا الشرط." |
| Candidates existed but the agent judged none a fit | The agent's own one-to-two-sentence reason |
| The user asked for a job | Adds "لا تتضمن البيانات وظائف، بل فرصاً تدريبية وتطوعية وفعاليات." |

| Error code | Cause |
|---|---|
| `FURSAH_MODEL_ERROR` | No key configured, or the OpenRouter call failed |
| `FURSAH_INVALID_MODEL_OUTPUT` | The query plan stayed invalid after its one re-ask, or the agent gave no answer |
| `FURSAH_VALIDATION_ERROR` | The agent proposed picks, but none passed validation |
| `FURSAH_INTERNAL_ERROR` | The search reached its model-call limit, its step limit, or 90 seconds |

### Cost control

Every model call costs money, so each search has hard limits enforced in code:

| Limit | Default | Setting in `backend/.env` |
|---|---|---|
| **Model calls per search** (all stages together) | **6** | `FURSAH_MAX_MODEL_CALLS` |
| Agent steps | 8 | `FURSAH_MAX_WORKFLOW_STEPS` |
| Searches by the agent (its only tool) | 3 | fixed in `agent.py` |
| Calls held back for the final answer | 1 | fixed in `agent.py` |
| Re-asking the model when the query plan drops a constraint | 1 | `FURSAH_MODEL_OUTPUT_RETRIES` |
| Re-running the agent after rejected picks | **never** | — |
| Total time | 90 s | `FURSAH_WORKFLOW_TIMEOUT_SECONDS` |

**How the model-call limit works.** `agent.ModelCallBudget` is a LangChain callback attached to
every model call in the request (query plan, each agent step, final pick). It counts calls and
blocks the next one **before it is sent**, so a blocked call is never billed. When one call is
left, the tools tell the agent to give its final answer, and code holds one call back so an answer
is always possible (see [③](#-how-the-agent-chooses-the-best-opportunities)). A search ends with
`FURSAH_INTERNAL_ERROR` only if the limit is hit outside the agent. The number of calls used is
logged at the end of every search (`model_calls`, for example `4/6`) and recorded on the trace
(`model_calls.used`).

`FURSAH_MODEL_MAX_RETRIES` is different: it retries a **dropped connection**, which is not billed
because no answer was produced. It does not count toward the 6.

The frontend's "إعادة المحاولة" button sends a new search; that is the user's choice, with its own
budget.

### Web search (optional)

When a search ends in "no match", the panel offers **"🔎 ابحث في مواقع خارجية موثوقة"**. Nothing
runs unless the user clicks it.

```
no_match panel → user clicks → POST /api/v1/web-search (new endpoint, same request body)
   web agent (LangChain, web mode) with one tool: web_search → Tavily
     ├─ rewrites the request into a web query (Arabic or English, + "دورة"، "تسجيل"، city)
     ├─ searches at most 2 times, trusted sites only
     └─ keeps up to 3 real opportunities, each with a 1–2 sentence Arabic summary
   code checks every pick → shown under "نتائج من مواقع خارجية" with a disclaimer
```

| Rule | How |
|---|---|
| Trusted sites only | `.gov.sa`, `.edu.sa`, `.org.sa`, `doroob.sa`, `linkedin.com` (edit `ALLOWED_SUFFIXES` / `ALLOWED_DOMAINS` in `web_search.py`). Commercial `.com.sa` sites are excluded. Checked **in code** on every link, because Tavily does not document suffix filters; https only |
| No invented links | A pick is kept only if its URL was returned by Tavily; the title, site and date come from Tavily, never the model |
| Kept apart | Web results are not recommendations: no source IDs, no eligibility, and the existing discovery/details API is unchanged |
| Labelled | "نتائج من مواقع خارجية، وليست من بيانات فرصة: لم نتحقق من تفاصيلها أو من أهليتك. تحقق من الموقع قبل التقديم." |
| Privacy | Only the query text (and the profile city, if set) is sent; not the rest of the profile |
| Page text is data | The prompt says to ignore instructions inside pages; links are validated by code anyway |
| Cost | At most 2 Tavily searches (1 credit each, `basic`) and 4 model calls per click; if the budget runs out, code shows the top results without a model summary |

LinkedIn: Tavily only sees public pages, so results are mostly public job/internship posts and company
pages.

Settings in `backend/.env`: `FURSAH_TAVILY_API_KEY` (required for this feature),
`FURSAH_WEB_SEARCH_MAX_QUERIES` (2), `FURSAH_WEB_SEARCH_MAX_MODEL_CALLS` (4). Without a key the
button returns a clear "web search not configured" error and everything else works as before.

Files: `backend/app/web_search.py` (agent, Tavily client, link checks), `backend/app/web_routes.py`
(endpoint), one line in `backend/app/main.py`; frontend `opportunities/WebSearch.tsx`, plus small
additions to `api/client.ts`, `types.ts`, `SearchResults.tsx` and `App.tsx`. This changes the
supplied frontend; mention it to the trainer.

### Readiness and logging
- **Readiness:** `/ready` returns 200 only when the database opens with the expected columns and
  records **and** a model is configured. It never calls the model, so checking it costs nothing.
- **Logging:** each stage (query interpreted, constraints resolved, agent completed, validation
  completed) is logged with its duration, outcome and the opportunity IDs involved, so slow or failing
  stages are easy to find. The last line of every search reports the model calls used.
- **Tracing (optional):** with Phoenix switched on, every search becomes one trace with each model
  call, tool call and stage as a span (see [2.2 Tracing](#22-tracing-phoenix)).

## What each step did

### Step 1 — Start the backend and frontend

Backend: FastAPI on port 8000. Frontend: React on port 5173. Both start with no learner code;
`/health` returns 200 and searches return `FURSAH_NOT_IMPLEMENTED` until steps 4–5.

### Step 2 — Data notebook

Completed all nine exercises in `data/data_exploration_and_preparation.ipynb`. It writes
`data/prepared/source_preparation.json`: 1,000 source rows with their original text plus
title, fee, deadline and link.

Three decisions the notebook asks us to explain:

1. **A missing fee is not zero.** No stated price means the cost is unknown, not free.
2. **A shared link does not allow deleting a source.** Similar posts may describe one
   opportunity, but that is evidence to review; both source IDs are kept.
3. **A future deadline does not confirm eligibility.** It says nothing about whether
   registration is open or whether the user meets the requirements.

### Step 3 — Prepare and store

`backend/scripts/build_store.py` reads the notebook output and writes `data/prepared/fursah.db`,
one row per announcement. It adds the facts the application filters on:

| Fact | Found in | Remaining sources |
|---|---|---|
| Title, opportunity type, delivery mode | 1,000 / 1,000 | |
| City | 741 / 1,000 | the other 259 are fully online |
| Start and end dates | 966 / 1,000 | 34 state no dates |
| Cost | 804 / 1,000 | not stated, so unknown |
| Deadline | 653 / 1,000 | not stated (642 before Day 4 added the "مُدد … من A إلى B" wording) |
| Application status | 696 / 1,000 | not stated |

It also extracts sessions and hours, mandatory and preferred requirements (kept separate),
required skills, and whether a post updates, reminds about or reposts an earlier one.

Since Day 4 it also **applies explicit updates** to the announcement they name (same activity link,
same title, and the update names the original's publication date): new deadline, status (with the
registration opening date) and moved start date. 20 updates are applied; each changed fact records
which update and sentence changed it (`updated_by`).

Rules we follow:

- Every fact stores the **exact sentence** it came from; all of them were checked against the source text.
- Unknown stays **null**. Required skills are `[]` only when the text says no experience is needed.
- Nothing is merged or deleted. "مكة" is stored as "مكة المكرمة" to match the frontend's city list.
- 49 sources carry review warnings (44 "fee not yet set", 6 "skill required but experience not required").

### Step 4 — Details and readiness

`backend/app/repository.py` reads `fursah.db` (read-only):

- `is_ready()` — checks the database exists, has the expected columns and has records.
- `get_details(id)` — maps one record to the API's details format. Registration status is
  checked against the evaluation date: a passed deadline gives `closed`, and an opening date
  that has passed gives `requires_verification`. Warnings are in Arabic for the user.
- `search(criteria)` — the agent's search tool for step 5. Removes cancelled, closed and expired
  opportunities; applies type, mode, city, budget, date and duration filters; online
  opportunities match any city; unknown dates and duration are returned as caveats, not excluded.
  (Day 4 added: ended activities, registration not yet open, and an unknown fee under a budget
  are excluded.)

`backend/app/infrastructure/dependencies.py` passes the evaluation time into the repository.

Result: `/api/v1/opportunities/OPP-0193` returns real data, an unknown ID returns 404, and
`/ready` shows `storage: true` (`application` stays false until step 5).

Dates are compared in Riyadh time (UTC+3): source deadlines are Saudi calendar dates.

### Step 5 — Search (done)

For how the finished search works end to end, see [How a search works](#how-a-search-works).
This section records how it was built.

We build the parts that need no model first and test them, then add the agent. The
validation layer is the safety net that catches model mistakes from the first run.

**Tools** (`backend/app/tools.py`) — what the agent may call:

- `search_opportunities(criteria)` — candidates from `repository.search()`; the agent sees a
  compact view, and the full records (with evidence) are kept for validation.
- `get_opportunity_details(id)` — the public details record. No longer given to the agent.
- `check_deadline(id)` — `passed`, `not_passed`, `uncertain` (deadline is today; no closing
  time is stated) or `unknown`. Used by validation; no longer given to the agent.

**Validation** (`backend/app/validation.py`) — checks every recommendation the agent proposes:

| Check | Rule |
|---|---|
| Real opportunity | The ID must come from the search tool's results |
| Sources | Source IDs must belong to that opportunity |
| Constraints | Type, mode, excluded modes, city (online matches any city), budget, dates and duration must hold; unknown dates and duration pass with a caveat; an unknown fee under a budget is rejected |
| Availability | Cancelled, closed, deadline-passed, ended or not-yet-open opportunities are rejected; a deadline of today adds a caveat |
| Quotes | Text the agent quotes («…») must appear in the source or the stored facts shown for it |
| Eligibility | `confirmed` only when the source states its requirements and the profile meets all of them; age and study year are not in the form, so they keep it `uncertain`. The agent's own eligibility claim is replaced by this rule. |
| Skills | Required skill and skills unknown → shown as `uncertain` with a caveat naming the skill; user lists skills without it → rejected |
| Missing information | The agent may only ask for fields the form collects (never age) |
| Count | Duplicates dropped; at most 3 |

Rejection reasons are kept for the logs and the error message. (They were first used to re-run
the agent once; that re-run was removed for cost control.)
14 hand-written scenarios (invented ID, invented quote, over budget, expired deadline,
skills unknown/present/missing, asking for age, …) all give the expected result.

`backend/app/internal.py` gained two fields on `CheckedResult`: `rejections` and
`missing_explanations`.

**Agent** (`backend/app/agent.py`) — LangChain `create_agent` on the OpenRouter model:

- `interpret_query()` — one structured model call turns the Arabic query into a `QueryPlan`
  (cities, excluded cities, types, modes, excluded modes, budget, dates, duration, topics).
- `check_plan()` — code checks the plan kept what the query states: every city named, a
  negated "online" ("مو أونلاين"), a price limit. A plan that drops one is retried with the
  reason, then rejected with `FURSAH_INVALID_MODEL_OUTPUT`.
- `run_agent()` — the agent gets one tool (search); the deadline and details tools were removed
  after live testing (see [Problems found and fixed](#problems-found-and-fixed)). The search tool is
  bound to the resolved constraints: the agent can narrow (type, mode, topics) but never widen,
  and has call limits. It returns 1–3 picks with Arabic reasons, caveats and a next action,
  or `no_match` / `needs_info`. Source IDs come from the store, never from the model;
  eligibility is decided by validation.

**Workflow** (`backend/app/workflow.py`) — what `POST /api/v1/discover` runs:

1. Interpret the query and check the plan.
2. `resolve_constraints()` merges the query with the saved form constraints: overlapping lists
   keep the overlap, the stricter budget/dates/duration win, a "not online" removes online, and
   disjoint lists (Jeddah vs. saved Riyadh) return `needs_info` with `constraint_conflict: true`.
   `constraint_resolution` ("query" or "profile") applies to that request only; nothing is saved.
   With no topics in the query, the profile's interests are used.
3. Run the agent once and validate. Invalid picks are dropped; if none survive, the search stops
   with `FURSAH_VALIDATION_ERROR` (no re-run, for cost control).
4. Build the response from the same records the details endpoint serves (so recommendation and
   details agree), with readable Arabic `interpreted_filters`.

Limits come from `.env`: at most 6 model calls per search (`FURSAH_MAX_MODEL_CALLS`), total time
(`FURSAH_WORKFLOW_TIMEOUT_SECONDS`, 90 s), agent steps (`FURSAH_MAX_WORKFLOW_STEPS`, 8). See
[Cost control](#cost-control). Each stage is logged with its time.
`application_is_ready()` is true when a model is configured; it makes no model call.

Tested without the model: plan checks, constraint resolution, tool limits, and the full
request through FastAPI with stand-in model calls (including a retry after an invented ID).

**Agent test scenarios** — expected outcomes to measure the agent against once it runs:

| Scenario | Expected |
|---|---|
| "ورشة في جدة" while the saved city is الرياض | `needs_info`, `constraint_conflict: true`, `constraints.location_preference` |
| Same request with `constraint_resolution: "query"` | Results in جدة (or online); saved profile unchanged |
| "مو أونلاين" / "ليس عن بعد" | No `online` results |
| "الرياض أو جدة" | Both cities allowed |
| "أقل من 100 ريال" | No result above 100 and no result with an unknown fee |
| A Python opportunity, profile skills unknown | Shown, `uncertain`, caveat "يشترط الإعلان معرفة Python، ولم تحدد مهاراتك بعد." |
| Profile lists Python, opportunity requires only Python | `confirmed` |
| An opportunity whose deadline has passed | Never recommended |
| A topic that does not exist in the data | `no_match`, no invented results |
| Announcement text containing instructions | Ignored; treated as data |

## Day 4: evaluation

### 2.1 Audit

[`day4/2.1-audit/audit.md`](day4/2.1-audit/audit.md): 11 components, each with its output, kind of
check (deterministic / verifiable / subjective) and missing signal. The agent's choice of
opportunities is **verifiable** against the dev cases' `acceptable_source_ids`; only the wording of
its reasons is subjective.

### 2.2 Tracing (Phoenix)

`FURSAH_PHOENIX_INSTRUMENT=langchain`: our model and tool calls go through LangChain, so its
instrumentor records the agent graph, each model call (with tokens) and each tool call
automatically. `setup_observability()` is called once in `main.py`.

Spans we added (from the audit's missing signals):

| Span | Where | Records |
|---|---|---|
| `workflow.discover` (root) | `workflow.discover()` | `request_id`, `evaluation_time`, query, response status, recommended opportunity and source IDs, `model_calls.used` |
| `plan.interpret` | `workflow._run()` | The model's raw plan, the final plan, **which fields code corrected** (`plan.corrected_by_code`), attempts, problems, notes |
| `constraints.resolve` | `workflow._run()` | Resolved criteria, conflicts, interpreted filters |
| `repo.precheck` | `workflow._run()` | Whether the database had any candidate |
| `repo.search` | `tools.search_opportunities()` | Search topics and filters, candidate count, **candidate opportunity and source IDs** (nested in LangChain's tool span) |
| `validation.check` | `workflow._run()` | Candidate, proposed and accepted IDs, **rejection reasons**, warnings, eligibility per pick |
| `no_match.explain` | `workflow._explain()` | The explanation shown |

Checked offline with an in-memory exporter: one trace per search, all spans nested under the root.

**Installing Phoenix on this machine.** The Phoenix server (`arize-phoenix`) needs
`pydantic-monty-client`, which has no prebuilt package for Python 3.14 on Windows. So:

- The backend's Python 3.14 has only the tracing **client**: `arize-phoenix-otel`,
  `openinference-instrumentation-langchain`, `openinference-instrumentation-openai`.
- The Phoenix **server** runs from its own Python 3.12 environment, outside the project:
  `C:\Users\mohme\.venvs\phoenix`. Start it with
  `/c/Users/mohme/.venvs/phoenix/Scripts/phoenix serve` (Git Bash) and keep that terminal open;
  the UI is at <http://localhost:6006>.

**Version pin (important).** Installing the tracing client upgraded OpenTelemetry to 1.45.1, and
`arize-phoenix-otel` 0.17.1 then **crashes the backend at startup** when tracing is on
(`'HTTPSpanExporter' object has no attribute '_headers'`). Keep OpenTelemetry at **1.44.0**, the
version `requirements.txt` pins:

```bash
python -m pip install opentelemetry-api==1.44.0 opentelemetry-sdk==1.44.0 \
  opentelemetry-exporter-otlp==1.44.0 opentelemetry-exporter-otlp-proto-http==1.44.0 \
  opentelemetry-exporter-otlp-proto-grpc==1.44.0 opentelemetry-exporter-otlp-proto-common==1.44.0 \
  opentelemetry-proto==1.44.0
```

**Live check (22 Oct):** one search sent to Phoenix produced **one** trace rooted at
`workflow.discover` with 16 spans: 3 LLM spans (= 3 model calls, tokens in and out on each), the
`search_opportunities` tool span with its arguments, and every custom span nested correctly. The
whole 2.2 checklist passes.

To switch tracing on, add to `backend/.env` and restart the backend:

```dotenv
FURSAH_PHOENIX_ENABLED=true
FURSAH_PHOENIX_INSTRUMENT=langchain
```

### 2.3 Graders and 2.4 diagnosis

Ten graders in `backend/evals/graders.py` (one per checkable audit row, plus the team-defined
details-consistency check). Run: `python -m evals.run_eval --label <name>` from `backend/`.

| | Baseline | After fixes |
|---|---|---|
| Cases passing | 9 / 24 | **23 / 24** |
| `search.no_excluded` | 0.45 | 1.00 |
| `e2e.no_excluded` | 0.60 | 1.00 |
| `e2e.recommendations` | 0.50 | 0.94 |
| `e2e.status` | 0.83 | 0.96 |

Most failures were availability rules, not the model: ended activities, registration not yet
open, unknown fees under a budget, and update posts whose changes never reached the original.
Then: the agent adding a type filter, broad model synonyms, and loose topic fallbacks. Full
before/after table and diagnosis log: [`day4/2.4-diagnose/results.md`](day4/2.4-diagnose/results.md).
The one open case (DEV-07) looks like a label inconsistency; it is a question for the instructors.

**Design changes from Day 4** (behaviour, not just tests):

- Unknown fee with a budget set → **excluded** (was: kept with a caveat).
- Always excluded: activities already ended; registration not yet open.
- Explicit update posts are applied to the announcement they name (deadline, status, start date).
- The search subject: a known subject of the data named in the query wins (31 subjects); else
  the query's own phrase found in the data; else the model's topics.
- The agent can no longer add a type or mode filter the user did not set.
- Prompt: recommend only the requested subject, not neighbouring ones.

## Problems found and fixed

### 1. The agent looped on deadline checks and never answered (first live test)

**Symptom.** "فرصة في تحليل البيانات في مكة" in the frontend failed with
`FURSAH_INTERNAL_ERROR` after about 15 seconds.

**Cause.** A step-by-step recording of the same search showed the first search worked (7 results),
then the agent spent every remaining step calling `check_deadline`, one opportunity at a time and
repeating the same IDs (one of them three times), until the 8-step budget ran out. The guard then
stopped it, and everything it had found was discarded. The tool was redundant: search results
already include each deadline, and search and validation already enforce it. Offering a tool to
re-check known facts invited the model to loop.

**Fix** (`backend/app/agent.py`):

1. Removed `check_deadline` from the agent's tools (validation still uses it).
2. Capped tool use: 3 searches, 3 detail lookups; a repeated detail request answers "already
   provided"; at a limit the tool says "give your final answer now".
3. The prompt now says results already contain deadline, status, cost and requirements, so the
   agent should search and then answer.
4. If the agent still runs out of steps, one final model call chooses from the candidates already
   found instead of failing.

**Result.** The same search finished in 7 steps (search → details for 3 candidates → answer).

### 2. Some opportunities were wrongly marked "confirmed"

**Symptom.** A result requiring a final-year student in "خدمة المجتمع" (OPP-0386) came back
`confirmed`, although the form does not collect study year.

**Cause.** The extraction rule for the required field of study captured only one word, so the 16
announcements naming a two-word field ("خدمة المجتمع") had no `required_study_field`. Validation
then treated the requirement as checkable.

**Fix.**

1. `backend/scripts/build_store.py`: the rule now captures multi-word fields; the store was rebuilt.
   All 166 final-year-study requirements are extracted (150 before).
2. `backend/app/validation.py`: eligibility also reads the requirement text itself, so any
   requirement mentioning age or final-year study keeps eligibility `uncertain` even if extraction
   misses a field. Two independent checks instead of one.

### 3. A failing search could keep spending model calls

**Concern.** Several paths could make extra paid calls on one search: re-asking for the query
plan, a long agent loop, re-running the whole agent when its picks were rejected, and a final
pick after running out of steps. A bad search could cost several times a good one.

**Fix** (`agent.py`, `workflow.py`, `infrastructure/config.py`):

1. One hard limit per search: **at most 6 model calls**, counted by a LangChain callback on every
   call and enforced before the call is sent (`FURSAH_MAX_MODEL_CALLS`).
2. The agent is never re-run. Invalid picks are dropped; if all fail, the search stops with
   `FURSAH_VALIDATION_ERROR`.
3. The plan re-ask and the final-pick fallback stay, but inside the same limit.

**Checked offline with a fake model (no credit used):** the 7th call is blocked; an agent stuck in
a loop is stopped at 6 calls; rejected picks run the agent once, not twice; a reached limit returns
a controlled 503. The 28 earlier checks still pass.

### 4. Generic words became a filter, hiding every match (second live test)

**Symptom.** "فرصة في تحليل البيانات في مكة" and "وظيفة في تحليل البيانات في مكة" both returned
"لم نجد فرصاً", and the screen showed "نوع الفرصة: تدريب", although the user never asked for an
internship. Without that filter the database has 7 matching opportunities (all online).

**Cause.** The model mapped the generic "فرصة" (opportunity) and "وظيفة" (job; the data has no
jobs) to the type "internship". No data-analysis internship exists, so the search came back empty,
and the "no match" message did not say why.

**Fix** (`agent.py`, `workflow.py`), no extra model calls:

1. `ground_plan()`: code keeps a type or mode filter only if the query names it, and reports a
   removed filter on screen.
2. The query plan has an `unsupported` field; the model flags a job request, and code adds the
   flag if the model forgets. The prompt now says "فرصة / فرص / نشاط" are not a type and a city
   alone does not mean in person.
3. `explain_no_match()`: an empty result names the filter that emptied it, using database counts,
   or the agent's own reason when it rejected the candidates.

**Checked offline (17 checks):** both queries drop the wrong type and find the 3 best of the 7;
"ورشة", "تدريب صيفي", "دورة تدريبية", "مسابقة", "تطوعية" and "حضوري" keep their filters; the
no-match messages above are produced. All earlier checks still pass.

### 5. The model excluded the city the user asked for, and dropped the topic (third live test)

**Symptom.** Live samples failed in two ways: three hit the 6-call limit before answering, and the
others (and the same query in the frontend) failed with `FURSAH_MODEL_ERROR`. A single plan call for
"وظيفة في تحليل البيانات في مكة" then returned `exclude_cities: [مكة المكرمة]` and no topics.

**Causes.**

1. `gpt-4o-mini` is unreliable on details: it put the requested city in `exclude_cities` and dropped
   the topic. The plan check accepted it because it only checked the city appeared somewhere.
2. The 6-call limit could block the agent's **final answer**: plan + several searches used it up.
3. `FURSAH_MODEL_ERROR` was a dropped connection, with only 1 connection retry configured.
4. Failures logged only the public error code, so the real reason was invisible.

**Fix:**

1. `ground_plan()` now reads from the query **by code**, overriding the model: known cities (a city
   right after a negation is excluded, otherwise included; "بالرياض"-style prefixes handled),
   "مو أونلاين", a riyal price or "مجاني", and a fallback topic made of the query's subject words when
   the model returns none. Grounding runs **before** the plan check, so the model is re-asked only
   when code cannot fix the plan.
2. The tools answer "limit reached: give your final answer now" when one model call is left, so the
   last call is kept for the answer.
3. Connection retries default to 3 (`FURSAH_MODEL_MAX_RETRIES`); failed connections are not billed.
4. A failed search logs its real reason (`failed` trace event, e.g.
   `ModelExecutionError: query interpretation failed: APIConnectionError`).

**Checked offline (26 checks):** the live bad plan is corrected (Mecca included, topic recovered);
cities in 9 phrasings, "مو أونلاين", prices and fallback topics; the last call is reserved. All 51
earlier checks still pass.

**Testing note.** Running several searches through FastAPI's test client can reuse the model's
connection across event loops and fail; test scripts now use one event loop, like the real server.

### 6. The agent kept searching instead of answering (fourth live test)

**Symptom.** "وظيفة في تحليل البيانات في مكة" still failed in the frontend. A recorded run showed
the first search found 7 good candidates, then the agent fetched details for 3, searched again,
fetched 3 more details, ignored "limit reached: give your final answer now", and searched again
until the 6-call limit stopped it with no answer.

**Cause.** Telling a model to stop is not a guarantee; it ignored the instruction. The details tool
also wasted half the calls, since search results already contain every fact.

**Fix** (`agent.py`):

1. One model call is **held back by code** from the agent's loop. If the agent has not answered
   when its share runs out, code stops it and uses that call for one "choose from what you found"
   call with no tools.
2. Removed the details tool; the agent has only the search tool. The prompt says to answer as
   soon as a search returns relevant candidates.

**Checked offline:** a fake agent that only ever searches still ends in a final answer, with the
held-back call and all 7 candidates, within 6 calls.

### 7. Validation rejected real dates as invented quotes

**Symptom.** The next live run picked 3 correct opportunities in 3 calls, but validation rejected
all of them: `quoted text «2026-11-12» is not in the source`.

**Cause.** The agent wrote dates from the search results in «». The quote check compared only
against the announcement's sentences, which write the same date as "12 نوفمبر 2026".

**Fix** (`validation.py`, `agent.py`): quotes may match anything the search showed for that
opportunity (source sentences plus stored dates, cost, deadline, requirements); invented text is
still rejected. The prompt says to use «» only for phrases from the announcement, not dates or
numbers.

**Result, live:** the query returns `ok` with 3 online data-analysis opportunities (volunteering,
course, competition) in **3 model calls**, with the note that the data has no jobs, and the course
card noting that Excel is required while the user's skills are unknown.

### 8. Results hidden behind a skills question

**Symptom.** "وظيفة في تحليل البيانات في مكة" (with an empty profile) answered "نحتاج إلى معلومات
إضافية: أضف مهاراتك" instead of showing the opportunities it found.

**Cause.** A validation rule turned the result into `needs_info` for `profile.skills` whenever every
pick required a skill the user had not entered (here, Excel). It blocked useful answers; the
contract only asks for missing information when it is needed to decide.

**Fix** (`validation.py`): the rule is removed. Such picks are shown as `uncertain` with a caveat
naming the skill, and adding skills to the profile confirms eligibility. Picks are still rejected
when the profile lists skills and the required one is missing.

### 9. "[City] or online" returned online only; topic matching was too loose

**Symptom.** "فرص صحية في أبها أو عن بعد" showed the filter "طريقة الحضور: عن بعد" and only online
results; the city's in-person and hybrid opportunities were never considered.

**Causes.**

1. The model added an online-only mode filter for "أو عن بعد", which overrode the city.
2. Topic matching used substrings, so "طب" matched "تطبيق": some "health" results were finance and
   design opportunities.

**Fix** (general: every city, every online wording):

1. `agent.ground_plan()`: when the query offers online as an alternative to any named city
   ("في جدة أو عن بعد", "أونلاين أو في الرياض", "بالدمام أو اون لاين", …), the online-only filter is
   removed; the city filter already admits online. "عن بعد في جدة" still means online only.
2. `repository._topic_pattern()`: topics must start a word (prefixes such as "ال" allowed).
3. `repository.search()`: a match in the named city gets +1, so it ranks above online matches.

**Checked offline:** all 24 cities × 5 phrasings (120 cases) behave correctly; whole-word topic
matching. Example: "تحليل البيانات في أبها أو عن بعد" ranks Abha's hybrid workshop first, then online
ones. For the health query the answer stays online-only, which is correct: none of Abha's 16 open
opportunities is about health.

### 10. An empty search ended in an error instead of an explanation

**Symptom.** "ابي فرصة تدريب حضورية في تحليل البيانات" failed with `FURSAH_INTERNAL_ERROR`.

**Cause.** The query names no city, so the saved cities (الرياض، جدة) applied, and no in-person
data-analysis internship exists there (the only one is in Tabuk). The agent searched repeatedly with
other words, used its calls, and with nothing found the code raised an error.

**Fix:**

1. `agent.run_agent()`: an agent that runs out of calls having found nothing returns `no_match`;
   the workflow then explains it ("بدون شرط المدينة توجد فرصة واحدة").
2. `workflow`: a database pre-check skips the agent entirely when nothing matches the hard
   constraints, saving model calls.

**Checked offline (6 checks):** this exact case (saved Riyadh/Jeddah) returns HTTP 200 `no_match`
pointing at the city filter; a hopeless search makes no agent calls; all earlier checks pass.

### 11. A request word became the topic ("احتاج")

**Symptom.** "احتاج فرصة تدريب حضورية" showed "المواضيع: احتاج" and then "لا توجد في البيانات
فرص مفتوحة حالياً عن «احتاج»".

**Cause.** The query has no subject, so the model correctly returned no topic. The code fallback
then used the leftover words as a topic, and "احتاج" ("I need") was missing from the list of
request words.

**Fix** (`agent.ground_plan()`, `workflow`): a fallback topic is kept only if it **appears in the
opportunity data**; if the whole phrase does not, its individual words that do are used. Request
words are therefore dropped whatever wording the user picks. The list of request words was also
extended. Topics the model itself returns are unchanged, so "دورة في علم الفلك" still gets the
"no opportunities about «علم الفلك»" explanation.

**Checked offline:** "احتاج…", "محتاج شي جديد", "عطني دورة" give no topic; "تحليل البيانات", "Python"
and "الأعمال" are kept; all suites pass (97 checks).

(Since problem 14, a subject phrase that is not in the data is still searched when the model
returns nothing for it, so "دورة في علم الفلك" gets its explained no match. Request words are
removed before that, so "احتاج فرصة تدريب حضورية" still has no topic.)

**Not changed, by decision:** saved profile preferences (for example the saved cities) still act as
filters, and the "(أو عن بعد)" label is unchanged.

### 12. "In Abha or online" excluded online; "no match" cost 5 calls (five-case live test)

**Symptoms.** Of five live cases, "تحليل البيانات في أبها أو عن بعد" showed "استبعاد: عن بعد" and only the
Abha result; three no-match cases (workshop in Riyadh, free online Python course, astronomy) were
correct but used 5 model calls each.

**Causes.** The model put "online" in the excluded modes; and the agent kept searching with other
words although the plan's topics already included synonyms and the database had nothing.

**Fix** (`agent.py`, `workflow.py`):

1. Excluded modes are read by code: a mode is excluded only when a whole negation word comes right
   before it ("مو أونلاين", "بدون حضور"); the model's exclusions are ignored. Negation words now match
   whole words only ("لا" inside "الأعمال" is not a negation).
2. The pre-check runs **with the topics**: if the database has nothing, the explained no-match is
   returned without calling the agent (1 model call instead of 5).

### 13. Unstable connection to OpenRouter (environment, not code)

From this computer, 2 of 3 connections to OpenRouter failed during the TLS handshake (the same
drops seen earlier with `pip`). A dropped connection makes a search fail with `FURSAH_MODEL_ERROR`.
Mitigation: connection retries now default to 3 (`FURSAH_MODEL_MAX_RETRIES`); see problem 5.

### 14. A broad request ("تقنية") found nothing, although the data has tech opportunities

**Symptom.** "فرص في الرياض تقنية" answered "لم نجد فرصاً"; the screen showed "المواضيع: تقنية".
The data has a SQL event in Riyadh and several online Python, SQL, Power BI and Excel opportunities.

**Cause.** The Day 4 rule "the query's own words win over the model's topics" searched for the
literal word "تقنية". It appears only in 11 in-person internships in other cities, so the pre-check
found nothing in Riyadh. The rule suits a specific subject, not a broad area the announcements
rarely name.

**Fix** (general, any request; no extra model calls):

1. `repository.subject_catalog()`: the data's 31 subjects in their original spelling.
2. `agent.PLAN_PROMPT` / `QueryPlan.subjects`: the plan call now sees that list and compares the
   request with it, picking every subject the request is about. A broad area covers the subjects
   inside it; a merely neighbouring subject is not picked.
3. `agent.ground_plan()`: the picked subjects are searched together with the query's own words
   found in the data, but only names really on the list are kept, so the model cannot invent a
   subject. An exact subject named in the query still wins first, so the Day 4 cases are unchanged.
4. Agent prompt: a broad request covers every subject in `topics_to_try`.

**The first live tests found four more problems**, fixed the same day:

- `gpt-4o-mini` left `subjects` empty when the instruction was one rule among many → the prompt
  now has a "Subjects (important)" section with worked examples (including one that must stay empty).
- The model once sent `unsupported` as text instead of a list, which failed the whole search →
  `QueryPlan` now accepts a single value as a one-item list.
- With no subject on the list, the model also left `topics` empty, so "دورة في علم الفلك"
  searched everything → code keeps the query's own subject words, giving the explained no match.
- For "صحية" the model picked only one of the two health subjects → the query's own words are
  searched as well as the model's subjects, not instead of them.

**Checked (plan-only live calls, 1 model call each):** "فرص تقنية في الرياض" and
"فرص في الرياض تقنية" find 18 (Riyadh's SQL event and Power BI first); "ابي اتعلم برمجة" → Python;
"ريادة الأعمال" → إدارة مشروع صغير; "فرص صحية في أبها أو عن بعد" finds the health opportunities;
"فرز النفايات" stays exact. Offline: an invented subject is dropped, astronomy gives no match,
"احتاج…" gives no topic, all 24 Day 4 cases keep their exact subject, and all suites pass.

### Testing note

Sending Arabic text with `curl` from the Windows command line can corrupt it before it reaches the
backend. Test searches from the frontend, `/docs`, or Python, which send proper UTF-8.

### Offline check (no model calls)

28 checks covering the store, repository, tools, validation and the full request through FastAPI
(with the model replaced by a stand-in) all pass after these fixes, including:

- all 166 final-year-study and 65 age requirements extracted;
- no opportunity with a study, age or unknown requirement can be `confirmed` (72, 26 and 310
  checked); the 8 `confirmed` cases are all skill-only requirements the user meets;
- the agent has no deadline tool, cannot widen constraints, and its call limits hold.

## How to run

From Git Bash, with Windows Python 3.14 (the backend currently runs without a venv):

```bash
# Backend
cd fursah-starter/backend
python scripts/build_store.py        # only when the notebook output or the rules change
python -m uvicorn app.main:app --reload --port 8000

# Frontend (second terminal)
cd fursah-starter/frontend
npm ci
npm run dev
```

Open http://127.0.0.1:5173 (frontend) and http://localhost:8000/docs (API).

On this machine the frontend runs inside WSL, which does not notice edits made to the files from
Windows: after changing frontend code, stop `npm run dev` (Ctrl+C) and start it again.

**Backend in Docker** (instead of the `uvicorn` command; the frontend runs as usual). Docker Desktop
must be running. From `fursah-starter/`:

```bash
docker build -t fursah-api .      # again after every backend or data change
docker run --rm -p 8000:8000 --env-file backend/.env -e FURSAH_PHOENIX_ENABLED=false fursah-api
```

`-e FURSAH_PHOENIX_ENABLED=false` is needed: the image installs only `requirements.txt` (no Phoenix
packages), so with tracing on the backend stops at startup, and Phoenix on your PC is not
`localhost` inside the container. Keys are passed at start with `--env-file`, never built into the
image. Checked 22 Oct: image builds on Python 3.12; `/health` and `/ready` (storage and application
true) and the details endpoint work. If Git Bash says `docker: command not found`, open a new
terminal (Docker Desktop installs per user, in `AppData\Local\Programs\DockerDesktop`).

**Evaluation (Day 4).** From `backend/`, uses the real model (about 24 searches, paid):

```bash
python -m evals.run_eval --label <name>   # results go to backend/evals/results/
```

**Tracing (optional).** Start Phoenix in a third terminal and keep it open, then switch tracing on
in `backend/.env` (lines below) and restart the backend:

```bash
/c/Users/mohme/.venvs/phoenix/Scripts/phoenix serve
```

Send a search, then open http://localhost:6006 → Projects → **fursah** → Traces; click a trace to
see its spans. Teammates need their own Phoenix install (see [2.2](#22-tracing-phoenix)).

`backend/.env` must contain the classroom evaluation time and, for step 5, the model settings:

```dotenv
FURSAH_EVALUATION_TIME=2026-10-18T12:00:00+03:00
FURSAH_MODEL_PROVIDER=openrouter
FURSAH_MODEL_NAME=openai/gpt-4o-mini
FURSAH_MODEL_API_KEY=<your OpenRouter key>
FURSAH_MODEL_BASE_URL=https://openrouter.ai/api/v1

# Optional
FURSAH_TAVILY_API_KEY=<your Tavily key>     # web search button
FURSAH_PHOENIX_ENABLED=true                 # tracing; needs Phoenix running
FURSAH_PHOENIX_INSTRUMENT=langchain
```

Never commit `.env`; it is ignored by git. `.env.example` is the safe template.

## Files we added or changed

| File | What |
|---|---|
| `backend/scripts/build_store.py` | Builds the SQLite store with rule-based extraction (step 3) |
| `backend/app/repository.py` | Readiness, details and search over SQLite (step 4) |
| `backend/app/infrastructure/dependencies.py` | Passes the evaluation time to the repository (one line) |
| `backend/app/tools.py` | Search tool for the agent (details and deadline helpers used by validation); `repo.search` span (step 5, Day 4) |
| `backend/app/validation.py` | Checks on every proposed recommendation (step 5, Day 4 availability rules) |
| `backend/app/internal.py` | Two extra fields on `CheckedResult` (step 5) |
| `backend/app/agent.py` | Query interpretation, plan grounding, the LangChain agent and the model-call budget (step 5) |
| `backend/app/workflow.py` | Discovery workflow, constraint resolution, pre-check, no-match explanations, tracing spans (step 5, Day 4) |
| `backend/app/infrastructure/config.py` | `max_model_calls` setting: model-call limit per search; Tavily and web-search settings |
| `backend/app/web_search.py`, `backend/app/web_routes.py` | Optional web search: agent, Tavily client, link checks, endpoint |
| `backend/app/main.py` | Adds the web-search route; calls `setup_observability()` at startup (tracing) |
| `backend/requirements.txt` | LangChain packages |
| `backend/evals/graders.py` | The ten Day 4 graders |
| `backend/evals/results/` | Eval run results: baseline and three after-fix runs |
| `day4/2.1-audit/audit.md`, `day4/2.2-instrument/results.md`, `day4/2.3-graders/results.md`, `day4/2.4-diagnose/results.md` | Day 4 write-ups |
| `frontend/src/opportunities/WebSearch.tsx` + `api/client.ts`, `types.ts`, `SearchResults.tsx`, `App.tsx` | Web-search button and results in the no-match panel |
| `frontend/src/styles.css` | Spinning loading icon while the web search runs (still when the user prefers reduced motion) |
| `docs/search-workflow.png` | Workflow diagram shown in [How a search works](#how-a-search-works) |
| `data/data_exploration_and_preparation.ipynb` | Notebook exercises completed (step 2) |
| `backend/.env.example`, `frontend/.env.example` | Templates; the originals were lost in the upload |
| `../.gitignore` | Keeps `.env`, virtual environments, caches and `node_modules` out of git |

## Open items

- [x] **Evaluation time:** set to 18 October 2026, 12:00 Riyadh in `backend/.env`.
- [x] **OpenRouter key:** added; `/ready` returns 200.
- [x] **Model retries:** connection retries default to 3 in `config.py`.
- [x] **Live re-test:** "فرصة في تحليل البيانات في مكة" returns 3 results in 3 model calls; the 24
      Day 4 cases ran live four times.
- [x] **`requirements.txt`:** lists `langchain==1.4.6` and `langchain-openai==1.7.2`.
- [x] **Tavily key:** added to `backend/.env`.
- [x] **Web search live run:** works from the frontend.
- [ ] **Tracing on by default?** Add the two `FURSAH_PHOENIX_*` lines to `backend/.env` when you
      want traces; leave them out when Phoenix is not running.
- [ ] **DEV-07:** ask the instructors about the label (contradicts DEV-02; see
      [2.4](day4/2.4-diagnose/results.md)).
- [ ] **Commit the work** to a team branch. Nothing is committed yet; the team will decide when.
- [ ] **Move the offline checks** into `backend/tests/` so the team can run them.
- [ ] **Tell the trainer** the frontend was extended with the optional web-search button.
- [ ] **Commit generated data?** `data/prepared/` (JSON and `fursah.db`) is untracked. Decide whether
      to commit it or have each teammate regenerate it.
- [ ] **Duplicate titles:** some announcements share a title (for example OPP-0384 and OPP-0385);
      review and group them in step 6.

### Before deployment

Not ready yet. To do before deploying:

- [ ] Commit the code, and decide how `data/prepared/fursah.db` reaches the server (commit it, or
      build it during deployment with `scripts/build_store.py`).
- [ ] Put the keys in the hosting platform's secret settings, never in files.
- [ ] Set `VITE_API_BASE_URL` to the deployed backend, and allow the deployed frontend's address
      in the backend's CORS setting (`FURSAH_ALLOWED_ORIGINS`).
- [ ] Decide the evaluation time in production: blank means the real current date.
- [ ] Turn tracing off (`FURSAH_PHOENIX_ENABLED=false`) unless a Phoenix server is reachable.
- [x] Run the backend without `--reload`; the Dockerfile's Python 3.12 supports our packages (image
      built and checked locally, 22 Oct).
- [ ] Move the offline checks into `backend/tests/` and run them before each deploy.

## Next

1. ~~Add the OpenRouter key and the evaluation time to `backend/.env`; `/ready` returns 200.~~ ✅
2. ~~Run live test cases against the real model; fix what fails.~~ ✅ (problems 1–14 above)
3. ~~Day 4: audit, tracing, graders, diagnose and fix.~~ ✅ (23 / 24)
4. ~~Optional web search (Tavily) with a loading spinner.~~ ✅
5. Step 6: group related announcements (original, update, repost, reminder), show the latest facts
   with all source IDs, and surface unexplained conflicts. 100 groups share an activity link;
   20 explicit updates are already applied (Day 4); reposts and reminders are not grouped yet.
6. When the team agrees: commit to a team branch; move the offline checks into `backend/tests/`;
   work through [Before deployment](#before-deployment).
