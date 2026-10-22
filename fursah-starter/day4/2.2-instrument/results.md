# 2.2 Instrument the agent: team Fursah-Group3

**Instrumentor:** `FURSAH_PHOENIX_INSTRUMENT=langchain`. Our model and tool calls go through
LangChain (`create_agent` and `ChatOpenAI` on OpenRouter), so its instrumentor records the agent
graph, each model call (with tokens) and each tool call. `setup_observability()` is called once in
`backend/app/main.py`, at startup.

## Spans we added

From the **Missing signal** column of the [2.1 audit](../2.1-audit/audit.md):

| Span | Where | Records |
|---|---|---|
| `workflow.discover` (root) | `workflow.discover()` | `request_id`, `evaluation_time`, query (`input.value`), message (`output.value`), `response.status`, recommended opportunity and source IDs, `model_calls.used` |
| `plan.interpret` | `workflow._run()` | `plan.model` (the model's raw plan), `plan.final`, **`plan.corrected_by_code`** (fields code changed), `plan.attempts`, `plan.problems`, `plan.notes` |
| `constraints.resolve` | `workflow._run()` | `criteria`, `conflicts`, `interpreted_filters` |
| `repo.precheck` | `workflow._run()` | `found`: whether the database had any candidate |
| `repo.search` | `tools.search_opportunities()` | Search topics and filters, `candidate_count`, **`candidate_ids`**, `candidate_source_ids` (nested inside LangChain's tool span) |
| `validation.check` | `workflow._run()` | `candidate_ids`, `proposed_ids`, `accepted_ids`, **`rejections`**, `warnings`, `eligibility` per pick, `outcome` |
| `no_match.explain` | `workflow._explain()` | `candidates_found`, the explanation shown |

## Live trace (22 October 2026)

Search: "وظيفة في تحليل البيانات في مكة" → `ok`, 3 recommendations.

```
workflow.discover  [CHAIN] 13.7 s   model_calls.used = 3
├─ plan.interpret  [CHAIN] 4.5 s    plan.corrected_by_code = [cities, exclude_cities, topics]
│   └─ RunnableSequence
│       ├─ ChatOpenAI  [LLM] 1.5 s   tokens 705 in / 33 out
│       └─ PydanticToolsParser
├─ constraints.resolve
├─ repo.precheck                    found = true
├─ LangGraph  [CHAIN] 9.0 s          (the agent)
│   ├─ model → ChatOpenAI  [LLM] 1.5 s   tokens 827 / 18
│   ├─ tools → search_opportunities  [TOOL]   args {"topics": ["تحليل البيانات"]}
│   │            └─ repo.search      candidate_count = 7
│   └─ model → ChatOpenAI  [LLM] 5.4 s   tokens 2213 / 285
└─ validation.check                 accepted_ids = OPP-0707, OPP-0132, OPP-0504
```

## Checklist

- [x] One trace per search, rooted at `workflow.discover` (1 trace, 16 spans)
- [x] Input (`input.value` on the root)
- [x] Output (`output.value`, `response.status`, recommended IDs on the root)
- [x] Tool calls with their arguments (`search_opportunities`, `input.value`)
- [x] Latency (every span)
- [x] One LLM span per model call: 3 LLM spans = `model_calls.used` 3, none twice
- [x] Tokens in and out on those LLM spans
- [x] Custom spans nested correctly, not separate traces (`repo.search` sits inside the tool span)

## What the trace taught us

- **Code corrects the model's plan in practice**: `plan.corrected_by_code` showed the model
  excluding the city the user asked for, fixed by code before the search.
- **Where the time goes**: of 13.7 s, the agent's final answer is the slowest call (5.4 s, 2,213
  tokens in, because the candidates are in its context). The database work takes milliseconds.

## Setup notes

- The Phoenix **server** runs from its own Python 3.12 environment (`C:\Users\mohme\.venvs\phoenix`):
  it cannot install on Python 3.14 on Windows. The backend's Python 3.14 has only the tracing client.
- Keep OpenTelemetry at **1.44.0**: with 1.45.1, `arize-phoenix-otel` 0.17.1 crashes the backend at
  startup (`'HTTPSpanExporter' object has no attribute '_headers'`). Install command in
  [TEAM_README.md](../../TEAM_README.md).
- Switch tracing on in `backend/.env`: `FURSAH_PHOENIX_ENABLED=true`,
  `FURSAH_PHOENIX_INSTRUMENT=langchain`; then restart the backend.
