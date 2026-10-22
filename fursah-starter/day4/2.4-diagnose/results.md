# 2.4 Diagnose, fix, rerun: team Fursah-Group3

Four full runs of the 24 shared cases (`gpt-4o-mini`): `baseline`, `after-fix-1`, `after-fix-2`,
`after-fix-3` (results in `backend/evals/results/`). Phoenix was not running during these runs, so
failing cases were diagnosed by rerunning them with the same spans kept in memory and printing
`plan.interpret`, `repo.precheck`, `repo.search` and `validation.check`.

## Before/after table

| Metric | Before | After | What changed |
|---|---|---|---|
| `search.no_excluded` | 0.45 | **1.00** | Ended activities, not-yet-open registration and unknown fees under a budget are filtered out; explicit updates applied |
| `e2e.no_excluded` | 0.60 | **1.00** | Same fixes; validation rejects the same cases as a second line |
| `e2e.recommendations` | 0.50 | **0.94** | All of the above, plus subject matching and the agent's relevance rule |
| `e2e.status` | 0.83 | **0.96** | No-match cases now answer `no_match` (DEV-20, 21, 24); DEV-13 finds its answer |
| `search.recall` | 0.89 | **0.94** | DEV-13: the agent can no longer add a type filter; subjects matched exactly |
| `agent.choice` | 0.94 | **1.00** | DEV-05's wrong candidates no longer reach the agent |
| `agent.no_rejections` | 1.00 | 1.00 | — |
| `agent.tool_called` | 1.00 | 1.00 | — |
| `resolve.constraints_applied` | 1.00 | 1.00 | — |
| `team.details_consistent` | 1.00 | 1.00 | — |
| **Cases passing** | **9 / 24** | **23 / 24** | after-fix-1: 21, after-fix-2: 22, after-fix-3: 23 |

New failures: none. No case that passed in an earlier run failed in a later one.

## Diagnosis log

| Case | What failed | What the trace shows | Hypothesis | Fix | Result |
|---|---|---|---|---|---|
| DEV-03, 04, 05, 12, 14, 15, 17, 21 | `search.no_excluded` / `e2e.no_excluded`: `activity_ended` sources recommended | `repo.search.candidate_source_ids` include sources whose `end_date` is before 18 Oct | The availability rule checked only the application deadline, not whether the activity is over | `repository.search()` and `validation` exclude `end_date` before the evaluation date | Fixed in all 8 |
| DEV-05 (SRC-0613/0614), DEV-07 (SRC-0980) | `deadline_passed` / `status_closed` sources still candidates | The candidates have no deadline; the texts are an original and its **update** ("تحديث … ومُدد آخر يوم للتقديم من 12 أكتوبر إلى 16 أكتوبر"; another update says registration closed) | Extraction missed deadlines written "من … إلى …", and an explicit update's changes never reached the original it names | `build_store.py`: read the update's new deadline; apply **explicit** updates (same link, same title, naming the original's publication date) to the original: deadline, status, start date. 20 updates applied | 0 time/status leaks in all cases |
| DEV-08, DEV-10 | `not_yet_open` sources recommended | Candidates with status `not_yet_open` and an opening date after 18 Oct | We kept them, treating "not open yet" as informational | Exclude registration not yet open at the evaluation date (an opening date that has passed stays, as needing verification) | Fixed |
| DEV-10, 15, 17, 20 | `fee_unknown` sources with a budget set | Candidates with `cost_sar` null under `max_budget_sar` | Design choice: unknown fees were kept with a caveat; the cases expect them excluded ("unknown is not free") | With a budget set, an unknown fee is excluded (search and validation) | Fixed |
| DEV-13 | `search.recall` 0, `no_match` | `plan.final` had no type (code removed the model's "event"), `repo.precheck.found=true`, but the agent's `search_opportunities` call passed `opportunity_types=["event"]` and got 0 candidates | The tool let the agent **add** a type filter the user never set | The agent may only narrow within types/modes the user set | Fixed |
| DEV-24 | `e2e.status`: `ok` instead of `no_match` | `plan.model` topics include "البيئة"; `repo.search` returned green-space and water-saving internships; the agent picked them | Broad model synonyms widened the subject, and the agent accepted neighbouring subjects | Prompt: recommend only the requested subject; code: the query's own subject wins over model topics | Fixed |
| DEV-20 | `e2e.status`: `ok` instead of `no_match` (varied between runs) | `plan.model` had no topics and an invented "no jobs" note; the fallback split the subject into single words ("قصه", "انتاج") that matched writing and reading activities | Topic fallback checked only **open** opportunities, so a real subject with nothing under 50 SAR was split into loose words; model output varies | Topic checks use all announcements; the job note is dropped when the query names no job; price words are not subject words | Fixed: the pre-check answers `no_match` in 1 model call |
| DEV-06, 02, 18 (risk) | DEV-06 once picked "خدمة العملاء" for "المحادثة الإنجليزية في العمل" | The topic "عمل" also matches "العملاء"; connecting words ("في", "تعلم") split subjects such as "مقدمة في Python" | The query's subject should be matched as a whole | `repository.known_subjects()`: the 31 activity subjects of the data (186 titles minus standard suffixes); a subject named in the query is searched exactly | All 24 cases resolve to their exact subject |
| DEV-07 | Still fails: `no_match` instead of `ok` | The request is for an **event** (`opportunity_types: ["event"]`); the acceptable sources are SRC-0870 ("هذا تدريب عملي داخل مقر العمل": internship) and SRC-0104/0105 ("فرصة للتطوع": volunteering), while other internships on the same topic are excluded as `type_not_requested` | The labels appear inconsistent with the announcements' text; nothing in the text calls these events. Every announcement sharing those activity links was checked: SRC-0870 says "تدريب عملي", SRC-0104 "فرصة للتطوع", SRC-0105 "نشاط تطوعي" | Not fixed: matching the label would mean ignoring the evidence. **Question for the instructors** | Open |

**Why DEV-07 is not fixed: it contradicts DEV-02.** Both cases ask for the same type:

| Case | Asks for | Acceptable sources (type in the text) | Excluded as `type_not_requested` |
|---|---|---|---|
| DEV-02 | `event` | 5 sources, all events | competition, course, **internship**, **volunteering**, workshop |
| DEV-07 | `event` | 1 **internship** + 2 **volunteering** | competition, course, **internship**, workshop |

DEV-02 treats internships and volunteering as the wrong type for an event request; DEV-07 accepts
exactly those types, while excluding other internships on the same topic for being the wrong type.
Passing DEV-07 would mean recommending an internship to someone who asked for an event, which
would break DEV-02 and contradict the announcements.

A score of 0.94–1.00 means "passes the cases we have", not "always right": the shared cases use one
minimal profile and fixed wordings. Next steps: add team cases for other phrasings and profiles,
and save traces in Phoenix for each run.
