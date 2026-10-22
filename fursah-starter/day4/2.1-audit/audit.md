# Evaluability audit: team Fursah-Group3

Framework or SDK used: LangChain 1.4 (`create_agent`, running on LangGraph) with an OpenRouter model
(`openai/gpt-4o-mini`) through `langchain-openai`; plain-Python validation; SQLite store.

What is logged today: the `Tracer` events printed to stdout (`query_interpreted`, `constraints_resolved`,
`precheck`, `agent_completed`, `validation_completed`, `model_calls`, `failed`, plus the route's
`request_received` / `response_ready`), each with an outcome, a duration and sometimes opportunity IDs.
`FURSAH_TRACE_JSONL_PATH` is not set, so nothing is written to a file. The store (`fursah.db`) keeps
every extracted fact with its evidence sentence. There is no checkpoint store.

| Component | Output | Kind of check | Missing signal |
|---|---|---|---|
| `agent.interpret_query()` → `ground_plan()` → `check_plan()` (understand the query) | `QueryPlan`: `cities`, `exclude_cities`, `opportunity_types`, `delivery_modes`, `exclude_delivery_modes`, `max_budget_sar`, `available_from/to`, `max_duration_days`, `topics`, `unsupported`; plus Arabic `notes` | **Deterministic** for explicit constraints: compare cities, exclusions, types, modes and budget with labels derived from the query text. Topics are **verifiable** (below), not subjective | The plan itself is not logged, only `success`/`invalid`. Not logged: the raw model plan **before** `ground_plan()` corrected it, which fields code overrode, and whether a re-ask happened |
| `workflow.resolve_constraints(plan, request)` | `SearchCriteria` (cities, types, modes, exclusions, budget, dates, duration, topics) and `conflicts` (`field`, `explanation`) | **Deterministic**: a pure function; given a plan and saved constraints the merge result and conflict are fixed by rule | The resolved criteria are not logged (only `success`/`conflict`); they reach the user only as Arabic `interpreted_filters` |
| Topics in the plan (`QueryPlan.topics`, used by `repository._relevance`) | Topic keywords, normalized | **Verifiable**: a topic is good if searching with it returns the case's `acceptable_source_ids` (topic recall) | Which topics were searched, and how many candidates each returned |
| Pre-check: `repository.search(criteria, limit=1)` in `workflow` | Empty or not → explained `no_match` without the agent | **Verifiable**: when `acceptable_source_ids` is non-empty, the pre-check must not stop (false-negative rate) | Logged only as `precheck: no_candidates`; no candidate count |
| Tool `search_opportunities` → `tools.search_opportunities()` → `repository.search(criteria)` | Up to 10 candidates: `opportunity_id`, `source_ids`, `city`, `delivery_mode`, `cost_sar`, `deadline`, `application_status`, requirements, `caveats`, `relevance` | **Deterministic** for exclusion: no candidate may be in `excluded_source_ids` (deadline passed, closed, cancelled, location not preferred). **Verifiable** for recall: acceptable sources should appear among the candidates | The tool's arguments and the candidate IDs it returned are not logged; nor how many searches the agent made |
| `agent.run_agent()` (LangChain agent; fallback `choose_from_records()`) | `AgentResult`: 1–3 picks (`opportunity_id`, `reason`, `caveats`, `next_action`, `relevance_score`) or `no_match` / `needs_info` | **Verifiable** for the choice: picked sources ⊆ `acceptable_source_ids` (precision), at least one acceptable picked (hit). **Subjective** for the Arabic `reason` (LLM judge) | Agent step count, which tool calls it made, whether the fallback pick ran, tokens and latency per model call; only the picked IDs are logged |
| `validation.validate_result()` (with `assess_eligibility()`) | `CheckedResult`: `checked_recommendations` (with `eligibility_status`, merged `caveats`), `rejections`, `warnings` | **Deterministic**: rule-based; eligibility is `confirmed` only when every stated requirement is met; rejected IDs match a rule | Rejection reasons are logged only when **all** picks fail; the reason for each eligibility decision is not logged |
| `workflow.explain_no_match()` | Arabic `message`: which filter emptied the result and how many opportunities each relaxation finds | **Verifiable**: the counts must equal database counts. Wording is subjective | The relaxation counts are not logged, only the final text |
| `POST /api/v1/discover` → `workflow.discover()` | `DiscoveryResponse`: `status`, `recommendations[].source_ids`, `eligibility_status`, `interpreted_filters`, `missing_fields` | **Deterministic** end to end: `status` equals the expected status; no recommended source is excluded | Tokens and cost per request (only the model-call count is logged) |
| `web_search.web_discover()` (optional, user-approved; not in the dev cases) | `WebSearchResponse`: `results[]` (`url`, `title`, `site`, `summary`), `queries` | **Deterministic** for safety: every URL came from Tavily and is on an allowed site. Relevance and summary quality are **subjective** | The raw Tavily results and how many were dropped by the site check |
| Offline: `scripts/build_store.extract()` (store preparation) | Per source `facts_json` with `evidence` sentences | **Verifiable**: each fact against a labelled sample; each evidence sentence must appear in the source (already checked: 0 mismatches) | Nothing missing: facts and evidence are stored in `fursah.db` |

**Subjective rows:** for each, what would an LLM judge need to see to grade it?

- **Agent `reason` / `caveats` / `next_action`:** the user's query and profile, the full candidate
  record the agent picked (all fields the search returned, including requirements and caveats), and the
  generated Arabic text. Criteria: grounded in the record (no invented facts), tied to the user's
  request and profile, clear Arabic, caveats cover the unknowns.
- **`explain_no_match` wording:** the query, the interpreted filters, the relaxation counts and the
  message. Criterion: the user understands what to change.
- **Web search relevance and summaries:** the query, each Tavily snippet and the summary written from
  it. Criteria: the result is a real opportunity, the summary says nothing the snippet does not.

**One label we changed after review, and why:**

- **The agent's choice of opportunities** was first labelled *subjective* ("is it the best fit?").
  The dev cases list `acceptable_source_ids` and `excluded_source_ids` for each query, so the choice is
  **verifiable**: precision (no picked source outside the acceptable set) and hit rate (at least one
  acceptable source picked). Only the wording of the reason stays subjective.
