# Learner guidance: Return interpreted_filters and relevant missing_explanations. For
# incompatible query/profile constraints, return needs_info with constraint_conflict=True
# and the affected constraints.* missing field. Honor constraint_resolution only for that
# request; never overwrite the saved profile. Enforce
# deps.settings.workflow_timeout_seconds and return 1–3 grounded recommendations only for
# ok.
#
# Team design: understand (model) -> resolve constraints (code) -> agent searches and
# picks (model + tools) -> validate (code, one retry with feedback) -> public response.
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

from opentelemetry import trace as otel

from .agent import ModelCallBudget, QueryPlan, check_plan, ground_plan, interpret_query, run_agent
from .infrastructure.dependencies import AppDependencies
from .infrastructure.errors import (
    EvidenceValidationError, FursahError, InvalidModelOutputError, WorkflowBudgetExceededError,
)
from .infrastructure.observability import start_span
from .internal import AgentContext, AgentOutcome, CheckedResult, ValidationContext
from .repository import SearchCriteria, local_date
from .schemas import DiscoveryRequest, DiscoveryResponse, DiscoveryStatus, Recommendation
from .validation import validate_result

TYPE_LABELS = {"workshop": "ورشة", "course": "دورة", "internship": "تدريب",
               "competition": "مسابقة", "volunteering": "تطوع", "event": "فعالية"}
MODE_LABELS = {"in_person": "حضوري", "online": "عن بعد", "hybrid": "مدمج"}


def application_is_ready(deps: AppDependencies) -> bool:
    """Ready when a model is configured and the agent framework imports.

    No model call here: readiness is probed often and must not cost money.
    """
    if deps.model_config is None:
        return False
    try:
        import langchain.agents  # noqa: F401
        import langchain_openai  # noqa: F401
    except ImportError:
        return False
    return True


async def discover(
    request: DiscoveryRequest,
    deps: AppDependencies,
) -> DiscoveryResponse:
    """API integration entry point: request + dependencies -> DiscoveryResponse.

    Use deps.evaluation_time, captured once for this request, for time-dependent
    decisions. You own the orchestration; other learner modules are optional.
    """
    # Day 4: the root span, so one search is one Phoenix trace.
    with start_span("workflow.discover", request_id=request.request_id,
                    evaluation_time=deps.evaluation_time.isoformat(),
                    **{"openinference.span.kind": "CHAIN", "input.value": request.query}) as span:
        try:
            response = await asyncio.wait_for(_discover(request, deps),
                                              timeout=deps.settings.workflow_timeout_seconds)
        except TimeoutError as exc:
            raise WorkflowBudgetExceededError("discovery exceeded its time budget") from exc
        span.set_attribute("output.value", response.message)
        span.set_attribute("response.status", response.status.value)
        span.set_attribute("response.opportunity_ids",
                           [r.opportunity_id for r in response.recommendations])
        span.set_attribute("response.source_ids",
                           [s for r in response.recommendations for s in r.source_ids])
        return response


async def _discover(request: DiscoveryRequest, deps: AppDependencies) -> DiscoveryResponse:
    trace = _Trace(deps, request.request_id)
    today = local_date(deps.evaluation_time)
    # One budget for every model call in this request (plan, agent steps, final pick).
    budget = ModelCallBudget(deps.settings.max_model_calls)
    try:
        return await _run(request, deps, trace, today, budget)
    except FursahError as exc:
        # The API returns only the public code; log the real reason (no secrets) for debugging.
        trace.emit("failed", "workflow", f"{type(exc).__name__}: {exc}"[:300])
        raise
    finally:
        trace.emit("model_calls", "cost", f"{budget.used}/{budget.limit}")
        otel.get_current_span().set_attribute("model_calls.used", budget.used)


async def _run(request: DiscoveryRequest, deps: AppDependencies, trace: "_Trace",
               today, budget: ModelCallBudget) -> DiscoveryResponse:
    retries = deps.settings.model_output_retries

    # 1. Understand the query. Code first corrects what it can read from the query itself
    # (cities, "not online", price, ungrounded types, empty topics) at no cost; the model
    # is re-asked only if a check still fails.
    plan, notes, feedback = None, [], None
    with start_span("plan.interpret", **{"openinference.span.kind": "CHAIN",
                                          "input.value": request.query}) as span:
        for attempt in range(1, retries + 2):
            raw = await interpret_query(deps, request.query, today, feedback, budget=budget,
                                        subjects=deps.repository.subject_catalog())
            plan, notes = ground_plan(raw, request.query,
                                      topic_exists=deps.repository.topic_in_data,
                                      subjects=deps.repository.known_subjects())
            feedback = check_plan(plan, request.query)
            if not feedback:
                break
        # What the model said, what code changed, and what was used (audit: missing signals).
        span.set_attribute("plan.model", raw.model_dump_json(exclude_defaults=True))
        span.set_attribute("plan.final", plan.model_dump_json(exclude_defaults=True))
        span.set_attribute("plan.corrected_by_code", _changed_fields(raw, plan))
        span.set_attribute("plan.attempts", attempt)
        span.set_attribute("plan.problems", feedback or [])
        span.set_attribute("plan.notes", notes)
        span.set_attribute("output.value", plan.model_dump_json(exclude_defaults=True))
    if feedback:
        trace.emit("query_interpreted", "model", "invalid")
        raise InvalidModelOutputError("query plan dropped explicit constraints: " + "; ".join(feedback))
    trace.emit("query_interpreted", "model", "success")

    # 2. Resolve query, form constraints and profile into one set of constraints.
    with start_span("constraints.resolve", **{"openinference.span.kind": "CHAIN"}) as span:
        resolved = resolve_constraints(plan, request)
        filters = interpreted_filters(resolved.criteria) + [f"ملاحظة: {n}" for n in notes]
        span.set_attribute("criteria", _criteria_json(resolved.criteria))
        span.set_attribute("conflicts", [c.field for c in resolved.conflicts])
        span.set_attribute("interpreted_filters", filters)
    if resolved.conflicts:
        trace.emit("constraints_resolved", "workflow", "conflict")
        return DiscoveryResponse(
            request_id=request.request_id, status=DiscoveryStatus.NEEDS_INFO,
            message="طلبك يختلف عن تفضيلاتك المحفوظة. اختر أيهما نستخدم لهذا البحث.",
            missing_fields=[c.field for c in resolved.conflicts],
            missing_explanations=[c.explanation for c in resolved.conflicts],
            interpreted_filters=filters, constraint_conflict=True,
        )
    trace.emit("constraints_resolved", "workflow", "success")

    # 3-4. Agent searches and picks once; validation checks. No re-run on rejection:
    # invalid picks are dropped, and if none survive the request ends with an error,
    # so a failing search cannot keep spending model calls.
    # Nothing matches the constraints and topics (which already include the model's
    # synonyms): the agent's extra searches would cost calls and find nothing (live
    # tests: 5 calls each), so answer from the database now.
    with start_span("repo.precheck", criteria=_criteria_json(resolved.criteria)) as span:
        found = bool(deps.repository.search(SearchCriteria(**{**resolved.criteria.__dict__, "limit": 1})))
        span.set_attribute("found", found)
    if not found:
        trace.emit("precheck", "repository", "no_candidates")
        message = _explain(resolved.criteria, {}, [], notes, deps)
        return DiscoveryResponse(request_id=request.request_id, status=DiscoveryStatus.NO_MATCH,
                                 message=message, interpreted_filters=filters)

    context = AgentContext(request_id=request.request_id, query=request.query,
                           profile=request.profile, constraints=request.constraints)
    records: dict = {}
    result = await run_agent(context, deps, resolved.criteria, records, budget=budget)
    trace.emit("agent_completed", "agent", result.outcome.value,
               evidence_ids=[r.opportunity_id for r in result.recommendations])
    with start_span("validation.check", **{"openinference.span.kind": "CHAIN"}) as span:
        checked: CheckedResult = validate_result(result, ValidationContext(
            request_id=request.request_id, request=request,
            evidence={"records": records, "criteria": resolved.criteria},
            evaluation_time=deps.evaluation_time,
        ))
        span.set_attribute("candidate_ids", list(records))
        span.set_attribute("proposed_ids", [r.opportunity_id for r in result.recommendations])
        span.set_attribute("accepted_ids", [r.opportunity_id for r in checked.checked_recommendations])
        span.set_attribute("rejections", checked.rejections)
        span.set_attribute("warnings", checked.warnings)
        span.set_attribute("eligibility", [f"{r.opportunity_id}:{r.eligibility_status.value}"
                                           for r in checked.checked_recommendations])
        span.set_attribute("outcome", checked.outcome.value)
    trace.emit("validation_completed", "validation", checked.outcome.value,
               evidence_ids=[r.opportunity_id for r in checked.checked_recommendations])
    if checked.outcome is AgentOutcome.NO_MATCH and checked.rejections:
        # The agent proposed something, but nothing it proposed held up.
        raise EvidenceValidationError("; ".join(checked.rejections))

    no_match_message = None
    if checked.outcome is AgentOutcome.NO_MATCH:
        no_match_message = _explain(resolved.criteria, records, result.notes, notes, deps)
    return to_response(request, checked, filters, deps, no_match_message)


def _explain(criteria: SearchCriteria, records: dict, agent_notes: list[str],
             plan_notes: list[str], deps: AppDependencies) -> str:
    with start_span("no_match.explain", **{"openinference.span.kind": "CHAIN"}) as span:
        message = explain_no_match(criteria, records, agent_notes, plan_notes, deps.repository)
        span.set_attribute("candidates_found", len(records))
        span.set_attribute("output.value", message)
        return message


def _changed_fields(raw: QueryPlan, final: QueryPlan) -> list[str]:
    """Plan fields that ground_plan() changed from what the model returned."""
    before, after = raw.model_dump(mode="json"), final.model_dump(mode="json")
    return [name for name in after if before.get(name) != after[name]]


def _criteria_json(criteria: SearchCriteria) -> str:
    """Short JSON of the criteria that are set, for span attributes."""
    return json.dumps({k: v for k, v in criteria.__dict__.items() if v not in (None, [])},
                      ensure_ascii=False, default=str)


# --- constraint resolution ---------------------------------------------------------

@dataclass
class Conflict:
    field: str
    explanation: str


@dataclass
class Resolved:
    criteria: SearchCriteria
    conflicts: list[Conflict] = field(default_factory=list)


def resolve_constraints(plan: QueryPlan, request: DiscoveryRequest) -> Resolved:
    """Query text + saved form constraints -> the one set used for search AND ranking.

    Overlapping lists keep the overlap; disjoint lists are a conflict the user resolves.
    `constraint_resolution` applies to this request only; nothing is saved.
    """
    form = request.constraints
    choice = request.constraint_resolution
    conflicts: list[Conflict] = []

    def merge(query_values, saved_values, field_name, label):
        query_values, saved_values = list(query_values or []), list(saved_values or [])
        if not query_values or not saved_values:
            return query_values or saved_values
        overlap = [v for v in query_values if v in saved_values]
        if overlap:
            return overlap
        if choice == "query":
            return query_values
        if choice == "profile":
            return saved_values
        conflicts.append(Conflict(
            field=field_name,
            explanation=f"طلبت {label(query_values)} في البحث، بينما تفضيلاتك المحفوظة "
                        f"{label(saved_values)}. اختر أيهما نستخدم لهذا البحث.",
        ))
        return query_values

    def names(values):
        return "، ".join(values)

    def types(values):
        return "، ".join(TYPE_LABELS.get(v, v) for v in values)

    def modes(values):
        return "، ".join(MODE_LABELS.get(v, v) for v in values)

    cities = merge(plan.cities, form.location_preference, "constraints.location_preference", names)
    kinds = merge(plan.opportunity_types, form.opportunity_types, "constraints.opportunity_types", types)
    delivery = merge(plan.delivery_modes, form.delivery_modes, "constraints.delivery_modes", modes)
    delivery = [m for m in delivery if m not in plan.exclude_delivery_modes]

    criteria = SearchCriteria(
        cities=[c for c in cities if c not in plan.exclude_cities],
        exclude_cities=plan.exclude_cities,
        opportunity_types=kinds,
        delivery_modes=delivery,
        exclude_delivery_modes=plan.exclude_delivery_modes,
        max_budget_sar=_min(plan.max_budget_sar, form.max_budget_sar),
        available_from=_max(plan.available_from, form.available_from),
        available_to=_min(plan.available_to, form.available_to),
        max_duration_days=_min(plan.max_duration_days, form.max_duration_days),
        topics=plan.topics or list(request.profile.interests),
    )
    return Resolved(criteria=criteria, conflicts=conflicts)


def interpreted_filters(criteria: SearchCriteria) -> list[str]:
    """Readable Arabic summary of what was searched, for the user to check."""
    filters = []
    if criteria.cities:
        filters.append("المدينة: " + "، ".join(criteria.cities) + " (أو عن بعد)")
    if criteria.exclude_cities:
        filters.append("استبعاد المدن: " + "، ".join(criteria.exclude_cities))
    if criteria.opportunity_types:
        filters.append("نوع الفرصة: " + "، ".join(TYPE_LABELS[t] for t in criteria.opportunity_types))
    if criteria.delivery_modes:
        filters.append("طريقة الحضور: " + "، ".join(MODE_LABELS[m] for m in criteria.delivery_modes))
    if criteria.exclude_delivery_modes:
        filters.append("استبعاد: " + "، ".join(MODE_LABELS[m] for m in criteria.exclude_delivery_modes))
    if criteria.max_budget_sar is not None:
        filters.append("مجانية فقط" if criteria.max_budget_sar == 0
                       else f"الحد الأقصى للرسوم: {criteria.max_budget_sar:g} ريال")
    if criteria.available_from:
        filters.append(f"تبدأ من {criteria.available_from.isoformat()}")
    if criteria.available_to:
        filters.append(f"تنتهي قبل {criteria.available_to.isoformat()}")
    if criteria.max_duration_days:
        filters.append(f"المدة القصوى: {criteria.max_duration_days} يوم")
    if criteria.topics:
        filters.append("المواضيع: " + "، ".join(criteria.topics))
    return filters


# --- explaining an empty result ------------------------------------------------------

NO_MATCH = "لم نجد فرصة تدعم الأدلة مطابقتها لطلبك."

# Each hard constraint, its Arabic name, and how to remove it for a "what if" count.
RELAXATIONS = [
    ("المدينة", lambda c: {"cities": [], "exclude_cities": []}),
    ("نوع الفرصة", lambda c: {"opportunity_types": []}),
    ("طريقة الحضور", lambda c: {"delivery_modes": [], "exclude_delivery_modes": []}),
    ("الحد الأقصى للرسوم", lambda c: {"max_budget_sar": None}),
    ("التواريخ", lambda c: {"available_from": None, "available_to": None}),
    ("المدة القصوى", lambda c: {"max_duration_days": None}),
]


def explain_no_match(criteria: SearchCriteria, records: dict, agent_notes: list[str],
                     plan_notes: list[str], repo) -> str:
    """Arabic explanation of an empty result and what the user could change.

    Uses database counts only (no model calls): the search is re-counted with one
    constraint removed at a time to find which constraint emptied the result.
    """
    parts = [NO_MATCH]
    if any("وظائف" in n for n in plan_notes):
        parts.append("لا تتضمن البيانات وظائف، بل فرصاً تدريبية وتطوعية وفعاليات.")
    if records:
        # Candidates existed but the agent judged none a fit; use its reason.
        parts += agent_notes or ["وجدنا فرصاً قريبة، لكنها لا تناسب طلبك بما يكفي."]
        return " ".join(parts)

    def count(**changes) -> int:
        return len(repo.search(SearchCriteria(**{**criteria.__dict__, **changes, "limit": 5000})))

    # The topic alone, with every other constraint removed.
    if criteria.topics and not repo.search(SearchCriteria(topics=criteria.topics, limit=1)):
        topic = "، ".join(criteria.topics[:3])
        parts.append(f"لا توجد في البيانات فرص مفتوحة حالياً عن «{topic}». جرّب موضوعاً قريباً.")
        return " ".join(parts)

    unlocks = []
    for label, relax in RELAXATIONS:
        changes = relax(criteria)
        if all(getattr(criteria, k) in (None, []) for k in changes):
            continue  # this constraint was not set
        found = count(**changes)
        if found:
            unlocks.append((found, label))
    if unlocks:
        unlocks.sort(reverse=True)
        hints = "؛ ".join(f"بدون شرط {label} توجد {_count_ar(n)}" for n, label in unlocks[:2])
        parts.append(f"لا توجد فرص تجمع كل شروط بحثك. {hints}. جرّب تعديل هذا الشرط.")
    else:
        parts.append("جرّب كلمات أخرى أو خفّف شروط البحث.")
    return " ".join(parts)


def _count_ar(n: int) -> str:
    """Arabic number agreement for "opportunity"."""
    if n == 1:
        return "فرصة واحدة"
    if n == 2:
        return "فرصتان"
    if 3 <= n <= 10:
        return f"{n} فرص"
    return f"{n} فرصة"


# --- response ------------------------------------------------------------------

def to_response(request: DiscoveryRequest, checked: CheckedResult, filters: list[str],
                deps: AppDependencies, no_match_message: str | None = None) -> DiscoveryResponse:
    base = {"request_id": request.request_id, "interpreted_filters": filters}
    if checked.outcome is AgentOutcome.NEEDS_INFO:
        return DiscoveryResponse(
            **base, status=DiscoveryStatus.NEEDS_INFO,
            message="نحتاج معلومة إضافية قبل أن نرشح لك فرصاً.",
            missing_fields=checked.missing_fields,
            missing_explanations=checked.missing_explanations,
        )
    if checked.outcome is AgentOutcome.NO_MATCH:
        return DiscoveryResponse(
            **base, status=DiscoveryStatus.NO_MATCH, message=no_match_message or NO_MATCH,
        )

    recommendations = []
    for rec in checked.checked_recommendations:
        # Facts come from the same record the details endpoint serves, so both agree.
        details = deps.repository.get_details(rec.opportunity_id)
        if details is None:
            continue
        recommendations.append(Recommendation(
            evidence_version=details.evidence_version,
            opportunity_id=details.opportunity_id,
            title=details.title,
            opportunity_type=details.opportunity_type,
            location=details.location or ("عن بعد" if details.delivery_mode == "online" else None),
            delivery_mode=details.delivery_mode,
            deadline=details.deadline,
            application_status=details.application_status,
            eligibility_status=rec.eligibility_status,
            reason=rec.reason,
            caveats=rec.caveats,
            source_ids=details.source_ids,
            relevance_score=rec.relevance_score,
            next_action=rec.next_action,
        ))
    if not recommendations:
        return DiscoveryResponse(**base, status=DiscoveryStatus.NO_MATCH, message=NO_MATCH)
    return DiscoveryResponse(
        **base, status=DiscoveryStatus.OK,
        message=f"وجدنا {len(recommendations)} من الفرص المناسبة لطلبك.",
        recommendations=recommendations,
    )


class _Trace:
    """Stage timings for the request trace (stdout and optional JSONL file)."""

    def __init__(self, deps: AppDependencies, request_id: str) -> None:
        self.deps, self.request_id = deps, request_id
        self.last = time.perf_counter()

    def emit(self, step: str, component: str, outcome: str, evidence_ids: list[str] | None = None):
        now = time.perf_counter()
        self.deps.tracer.emit(request_id=self.request_id, step=step, component=component,
                              outcome=outcome, elapsed_ms=(now - self.last) * 1000,
                              evidence_ids=evidence_ids)
        self.last = now


def _min(*values):
    present = [v for v in values if v is not None]
    return min(present) if present else None


def _max(*values):
    present = [v for v in values if v is not None]
    return max(present) if present else None
