"""Day 4 · 2.3: code graders, one per component from your 2.1 audit.

Every grader has the same signature:

    grader(case, response, spans) -> Grade | None

case      one line of dev_cases.jsonl. Its "expected" block holds:
            status                 "ok" or "no_match"
            acceptable_source_ids  recommending ANY source of these opportunities is correct
            excluded_source_ids    {source_id: reason}: recommending these is wrong, even
                                   next to an acceptable source in the same recommendation
            uncertain_source_ids   {source_id: reason}: availability unknown; a recommendation
                                   made only of these is not graded (always empty in no_match cases)
response  the DiscoveryResponse your workflow.discover() returned
spans     {span name: attributes} for this case's trace; the last span wins when a name
          repeats. spans.all(name) returns every span with that name, in order.

Return None when the case has nothing to check for this metric (e.g. no acceptable IDs).
Put the expected and the actual value in `reason`: it is printed under every failing case.
"""
from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass
class Grade:
    metric: str
    score: float  # 0..1
    reason: str = ""  # "expected X, got Y"


def tool_inputs(spans, tool_name: str) -> list[dict]:
    """Arguments of every call to `tool_name`, for any framework that records tool spans.

    LangChain, LangGraph and PydanticAI all record `tool.name` and the arguments as JSON in
    `input.value`. Without a framework, record them yourself on a custom span with the same keys.
    Exception: LangChain records a tool with ONE argument as the bare value, not JSON; that
    call comes back as {"raw": value}. Print tool_inputs(...) once to see what yours returns.
    """
    calls = []
    for attributes in spans.all_spans():
        if attributes.get("tool.name") == tool_name:
            value = attributes.get("input.value") or "{}"
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = None
            calls.append(parsed if isinstance(parsed, dict) else {"raw": value})
    return calls


# --- Example (given) ---------------------------------------------------------------

def grade_status(case, response, spans):
    expected = case["expected"]["status"]
    got = response.status.value
    return Grade("e2e.status", float(got == expected), f"expected {expected!r}, got {got!r}")


# --- Your graders: one per deterministic or verifiable component in your 2.1 table ---
# Team Fursah-Group3. Each grader names the component it measures (day4/2.1-audit/audit.md).
# Shared cases have one minimal profile, so these graders cover search, constraints,
# availability and the final answer; grade_details_consistency is our team-defined check.


def _expected(case):
    e = case["expected"]
    return set(e["acceptable_source_ids"]), dict(e["excluded_source_ids"]), dict(e["uncertain_source_ids"])


def _verdict(source_ids, acceptable, excluded, uncertain):
    """The assignment's rule for one recommendation: right / wrong / ungraded."""
    if any(s in excluded for s in source_ids):
        return "wrong"
    if any(s in acceptable for s in source_ids):
        return "right"
    if source_ids and all(s in uncertain for s in source_ids):
        return "ungraded"
    return "wrong"  # off topic


def _all_values(spans, span_name, key):
    """Every value of a list attribute across all spans with that name."""
    values = []
    for attributes in spans.all(span_name):
        values.extend(attributes.get(key) or [])
    return values


def grade_recommendations(case, response, spans):
    """Component: POST /api/v1/discover (final answer). Verifiable.
    1 when, in an ok case, no recommendation is wrong and at least one is right."""
    if case["expected"]["status"] != "ok":
        return None  # no_match cases are covered by e2e.status
    acceptable, excluded, uncertain = _expected(case)
    got = [list(r.source_ids) for r in response.recommendations]
    verdicts = [_verdict(s, acceptable, excluded, uncertain) for s in got]
    wrong = [s for s, v in zip(got, verdicts) if v == "wrong"]
    score = float(not wrong and "right" in verdicts)
    leaked = [f"{s}:{excluded[s]}" for r in got for s in r if s in excluded]
    return Grade("e2e.recommendations", score,
                 f"expected any of {sorted(acceptable)}, got {got}; wrong: {wrong}; excluded: {leaked}")


def grade_no_excluded(case, response, spans):
    """Component: final answer, safety. Deterministic: no recommended source may be excluded
    (deadline passed, closed, cancelled, wrong city, over budget, ...)."""
    if not response.recommendations:
        return None
    _, excluded, _ = _expected(case)
    leaked = [f"{s}:{excluded[s]}" for r in response.recommendations for s in r.source_ids if s in excluded]
    return Grade("e2e.no_excluded", float(not leaked), f"expected no excluded source, got {leaked}")


def grade_constraints_applied(case, response, spans):
    """Component: workflow.resolve_constraints(). Deterministic: the saved form constraints of
    the case must be in the resolved criteria the search used (constraints.resolve span)."""
    resolve = spans.get("constraints.resolve")
    if resolve is None or not resolve.get("criteria"):
        return None  # stopped before resolving (or a conflict)
    criteria = json.loads(resolve["criteria"])
    form = case.get("constraints") or {}
    missing = []
    cities = set(criteria.get("cities") or [])
    if form.get("location_preference") and not cities:
        missing.append(f"cities {form['location_preference']}, got none")
    elif form.get("location_preference") and not cities <= set(form["location_preference"]):
        missing.append(f"cities within {form['location_preference']}, got {sorted(cities)}")
    for key, field in (("opportunity_types", "opportunity_types"), ("delivery_modes", "delivery_modes")):
        if form.get(key) and not set(criteria.get(field) or []) <= set(form[key]):
            missing.append(f"{field} within {form[key]}, got {criteria.get(field)}")
        if form.get(key) and not criteria.get(field):
            missing.append(f"{field} {form[key]}, got none")
    if form.get("max_budget_sar") is not None:
        got = criteria.get("max_budget_sar")
        if got is None or got > form["max_budget_sar"]:
            missing.append(f"max_budget_sar <= {form['max_budget_sar']}, got {got}")
    if not form:
        return None
    return Grade("resolve.constraints_applied", float(not missing),
                 "expected the case constraints in the criteria; " + ("; ".join(missing) or "all present"))


def grade_search_recall(case, response, spans):
    """Component: repository.search() via the search tool (and the pre-check). Verifiable:
    in an ok case, did any search return an acceptable source?"""
    acceptable, _, _ = _expected(case)
    if case["expected"]["status"] != "ok" or not acceptable:
        return None
    candidates = set(_all_values(spans, "repo.search", "candidate_source_ids"))
    precheck = spans.get("repo.precheck")
    if not candidates and precheck is not None and precheck.get("found") is False:
        return Grade("search.recall", 0.0, f"expected any of {sorted(acceptable)}; the pre-check found nothing")
    hit = sorted(acceptable & candidates)
    return Grade("search.recall", float(bool(hit)),
                 f"expected any of {sorted(acceptable)} among {len(candidates)} candidates, got {hit}")


def grade_search_no_excluded(case, response, spans):
    """Component: repository.search() hard filters. Deterministic: candidates must not include
    sources the case excludes (passed deadline, closed, wrong city, over budget, ...)."""
    candidates = _all_values(spans, "repo.search", "candidate_source_ids")
    if not candidates:
        return None
    _, excluded, _ = _expected(case)
    leaked = sorted({f"{s}:{excluded[s]}" for s in candidates if s in excluded})
    return Grade("search.no_excluded", float(not leaked),
                 f"expected no excluded candidate among {len(set(candidates))}, got {leaked}")


def grade_agent_choice(case, response, spans):
    """Component: agent.run_agent() selection. Verifiable: when the search did return an
    acceptable source, did the agent propose one? (Separates search from choice failures.)"""
    acceptable, _, _ = _expected(case)
    candidates = set(_all_values(spans, "repo.search", "candidate_source_ids"))
    check = spans.get("validation.check")
    if case["expected"]["status"] != "ok" or not (acceptable & candidates) or check is None:
        return None
    by_opportunity = {}
    for attributes in spans.all("repo.search"):
        ids = list(attributes.get("candidate_ids") or [])
        sources = list(attributes.get("candidate_source_ids") or [])
        # One source per opportunity in our store, in the same order.
        by_opportunity.update(zip(ids, sources))
    proposed = list(check.get("proposed_ids") or [])
    proposed_sources = [by_opportunity.get(o) for o in proposed]
    hit = [s for s in proposed_sources if s in acceptable]
    return Grade("agent.choice", float(bool(hit)),
                 f"expected a pick from {sorted(acceptable & candidates)}, proposed {proposed_sources}")


def grade_validation_clean(case, response, spans):
    """Component: validation.validate_result() as a check on the agent. Deterministic: how often
    the agent proposed something validation had to reject (invented ID, bad quote, ...)."""
    check = spans.get("validation.check")
    if check is None or not check.get("proposed_ids"):
        return None
    rejections = list(check.get("rejections") or [])
    return Grade("agent.no_rejections", float(not rejections), f"expected no rejections, got {rejections}")


def grade_tool_called(case, response, spans):
    """Component: the agent's tool call. Deterministic: when the agent ran, it searched."""
    if spans.get("validation.check") is None:
        return None  # stopped before the agent (pre-check, conflict)
    calls = tool_inputs(spans, "search_opportunities")
    return Grade("agent.tool_called", float(bool(calls)), f"expected >=1 search call, got {len(calls)}: {calls}")


def grade_details_consistency(case, response, spans):
    """Team-defined check (details consistency): for each recommendation, GET details must
    agree on title, deadline and source_ids. Reads our store directly; no model call."""
    if not response.recommendations:
        return None
    from app.infrastructure.dependencies import get_app_dependencies
    repo = get_app_dependencies().repository
    mismatches = []
    for r in response.recommendations:
        details = repo.get_details(r.opportunity_id)
        if details is None:
            mismatches.append(f"{r.opportunity_id}: no details")
            continue
        for field in ("title", "deadline", "application_status"):
            if getattr(details, field) != getattr(r, field):
                mismatches.append(f"{r.opportunity_id}.{field}: {getattr(r, field)!r} vs {getattr(details, field)!r}")
        if list(details.source_ids) != list(r.source_ids):
            mismatches.append(f"{r.opportunity_id}.source_ids differ")
    return Grade("team.details_consistent", float(not mismatches), f"expected none, got {mismatches}")


GRADERS = [
    grade_status,
    grade_recommendations,
    grade_no_excluded,
    grade_constraints_applied,
    grade_search_recall,
    grade_search_no_excluded,
    grade_agent_choice,
    grade_validation_clean,
    grade_tool_called,
    grade_details_consistency,
]
