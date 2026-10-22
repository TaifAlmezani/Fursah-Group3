# 2.3 Graders and baseline: team Fursah-Group3

Graders: [`backend/evals/graders.py`](../../backend/evals/graders.py), one per deterministic or
verifiable row of the [2.1 audit](../2.1-audit/audit.md).

| Metric | Component (2.1 row) | Kind | What it checks |
|---|---|---|---|
| `e2e.status` | `POST /api/v1/discover` | Deterministic | Status equals the expected `ok` / `no_match` (given example) |
| `e2e.recommendations` | Final answer | Verifiable | In `ok` cases, no recommendation is wrong and at least one is right (the assignment's per-recommendation rule) |
| `e2e.no_excluded` | Final answer, safety | Deterministic | No recommended source is in `excluded_source_ids` |
| `resolve.constraints_applied` | `workflow.resolve_constraints()` | Deterministic | The case's saved constraints (cities, types, modes, budget) are in the criteria the search used (`constraints.resolve` span) |
| `search.recall` | `repository.search()` + pre-check | Verifiable | In `ok` cases, some search returned an acceptable source (`repo.search` / `repo.precheck` spans) |
| `search.no_excluded` | `repository.search()` hard filters | Deterministic | No candidate is an excluded source |
| `agent.choice` | `agent.run_agent()` selection | Verifiable | When the search found an acceptable source, the agent proposed one (separates search failures from choice failures) |
| `agent.no_rejections` | `validation.validate_result()` | Deterministic | The agent proposed nothing validation had to reject |
| `agent.tool_called` | The agent's tool call | Deterministic | When the agent ran, it called `search_opportunities` (`tool_inputs()`) |
| `team.details_consistent` | **Team-defined check: details consistency** | Deterministic | For every recommendation, `get_details()` agrees on title, deadline, status and `source_ids` |

Graders return `None` where a metric does not apply (for example `search.recall` on `no_match`
cases, or `agent.*` when the pre-check answered without the agent).

## Baseline (shared cases)

`python -m evals.run_eval --label baseline` (24 cases, `gpt-4o-mini`; Phoenix was not running, so
traces were kept in memory for grading only):

| Metric | Score | n | Failing cases |
|---|---|---|---|
| `agent.choice` | 0.94 | 16 | DEV-05 |
| `agent.no_rejections` | 1.00 | 20 | |
| `agent.tool_called` | 1.00 | 21 | |
| `e2e.no_excluded` | 0.60 | 20 | DEV-05, 08, 10, 12, 14, 17, 20, 21 |
| `e2e.recommendations` | 0.50 | 18 | DEV-05, 06, 07, 08, 10, 12, 13, 14, 17 |
| `e2e.status` | 0.83 | 24 | DEV-13, 20, 21, 24 |
| `resolve.constraints_applied` | 1.00 | 24 | |
| `search.no_excluded` | 0.45 | 20 | DEV-03, 04, 05, 08, 10, 12, 14, 15, 17, 20, 21 |
| `search.recall` | 0.89 | 18 | DEV-07, 13 |
| `team.details_consistent` | 1.00 | 20 | |

**24 cases: 9 PASS, 15 FAIL.**

## Which results test what

- **Shared cases** (`dev_cases.jsonl`): every metric above except the last.
- **Our own design** (team-defined check): `team.details_consistent`, the "details consistency"
  check from the assignment, run as a grader on every recommendation. Result: **1.00** (20 of 20
  cases with recommendations): a recommendation card and its details panel always agree, because
  both are built from the same database record.
