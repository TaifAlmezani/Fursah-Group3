# Age and study stage are not collected by this profile. Keep eligibility uncertain
# when these requirements cannot be established; do not request nonexistent form fields.
# Learner guidance: Keep null skills distinct from an explicit empty list. Validate
# mandatory rules and skill proficiency, preserve source conflicts, and separate calendar
# duration from participation hours. Do not mark unknown requirements as confirmed.
#
# Team design: deterministic checks on every recommendation the agent proposes. The
# agent chooses and explains; this module decides what may reach the user.
from __future__ import annotations

import re

from .internal import (
    AgentOutcome, AgentRecommendation, AgentResult, CheckedResult, ValidationContext,
)
from .repository import SearchCriteria, local_date, normalize_arabic
from .schemas import EligibilityStatus, UserProfile
from .tools import deadline_state

MAX_RECOMMENDATIONS = 3

# Fields the user can edit in the frontend form; never ask for anything else.
EDITABLE_FIELDS = {
    "profile.city", "profile.skills", "profile.field_of_study", "profile.education_level",
    "profile.interests", "constraints.location_preference", "constraints.opportunity_types",
    "constraints.delivery_modes", "constraints.available_from", "constraints.available_to",
    "constraints.max_duration_days", "constraints.max_budget_sar",
}
INDEXED_SKILL_FIELD = re.compile(r"^profile\.skills\[\d+\]\.(name|level)$")


def validate_result(
    result: AgentResult,
    context: ValidationContext,
) -> CheckedResult:
    """Proposed result + source context -> checked internal outcome.

    Verify selected IDs and supporting evidence; do not invent missing rules.
    Ask for skill level only when a sourced requirement needs it. A changed start
    date alone does not establish a new end or duration. Retain unresolved source
    conflicts; source similarity alone does not authorize merging or deletion.
    Verify candidate IDs and source ownership; reject invented supporting quotes.
    A past opening date does not prove registration is now open. Keep the source
    statement and expose uncertainty when the current status is unverified.
    """
    if result.outcome is AgentOutcome.NEEDS_INFO:
        return _check_needs_info(result)
    if result.outcome is AgentOutcome.NO_MATCH:
        return CheckedResult(outcome=AgentOutcome.NO_MATCH)

    records: dict = context.evidence.get("records", {})
    criteria: SearchCriteria = context.evidence.get("criteria") or SearchCriteria()
    profile = context.request.profile

    accepted: list[AgentRecommendation] = []
    rejections: list[str] = []
    warnings: list[str] = []
    skills_needed: dict[str, list[str]] = {}

    for rec in result.recommendations:
        oid = rec.opportunity_id
        if any(a.opportunity_id == oid for a in accepted):
            warnings.append(f"{oid}: duplicate recommendation dropped")
            continue
        record = records.get(oid)
        if record is None:
            rejections.append(f"{oid} was not returned by the search tool; recommend only search results.")
            continue

        problems = _check_sources(rec, record) + check_constraints(record, criteria)
        problems += _check_availability(record, context)
        problems += _check_quotes(rec, record)
        status, eligibility_notes, missing_skills, lacks = assess_eligibility(record, profile)
        if lacks:
            problems.append(f"the profile does not list required skill(s): {', '.join(lacks)}")
        if problems:
            rejections.extend(f"{oid}: {p}" for p in problems)
            continue

        if rec.eligibility_status is not status:
            warnings.append(f"{oid}: eligibility set to {status.value} by validation")
        caveats = _merge(rec.caveats, record.get("caveats", []), eligibility_notes)
        if deadline_state(record.get("deadline"), context.evaluation_time) == "uncertain":
            caveats = _merge(caveats, ["اليوم هو آخر يوم للتقديم ولم يُذكر وقت الإغلاق؛ قد يكون التقديم أُغلق."])
        if missing_skills:
            skills_needed[oid] = missing_skills
        accepted.append(rec.model_copy(update={
            "eligibility_status": status,
            "caveats": caveats,
            "source_ids": record["source_ids"],
        }))

    accepted = accepted[:MAX_RECOMMENDATIONS]
    if not accepted:
        return CheckedResult(outcome=AgentOutcome.NO_MATCH, rejections=rejections,
                             warnings=warnings)

    # A required skill the user has not answered does not hide results: the pick stays
    # `uncertain` with a caveat naming the skill (added by assess_eligibility), and the
    # user can add skills to confirm. Asking first only blocked useful answers.
    if skills_needed:
        warnings.append("skills unknown for: " + ", ".join(sorted(skills_needed)))
    return CheckedResult(outcome=AgentOutcome.PROPOSED, checked_recommendations=accepted,
                         rejections=rejections, warnings=warnings)


def check_constraints(record: dict, criteria: SearchCriteria) -> list[str]:
    """Hard constraints must hold for every recommendation; unknown facts pass with a caveat."""
    problems = []
    if criteria.opportunity_types and record["opportunity_type"] not in criteria.opportunity_types:
        problems.append(f"type {record['opportunity_type']} is not one of {criteria.opportunity_types}")
    mode = record["delivery_mode"]
    if criteria.delivery_modes and mode not in criteria.delivery_modes:
        problems.append(f"delivery mode {mode} is not one of {criteria.delivery_modes}")
    if mode in criteria.exclude_delivery_modes:
        problems.append(f"delivery mode {mode} was excluded by the user")
    if criteria.cities and mode != "online" and record["city"] not in criteria.cities:
        problems.append(f"city {record['city']} is not one of {criteria.cities}")
    if record["city"] and record["city"] in criteria.exclude_cities:
        problems.append(f"city {record['city']} was excluded by the user")
    cost = record["cost_sar"]
    if criteria.max_budget_sar is not None and cost is None:
        problems.append(f"the fee is unknown, so it cannot be shown to fit the budget of {criteria.max_budget_sar} SAR")
    elif criteria.max_budget_sar is not None and cost > criteria.max_budget_sar:
        problems.append(f"cost {cost} SAR exceeds the budget of {criteria.max_budget_sar} SAR")
    if criteria.available_from and record["start_date"] and record["start_date"] < criteria.available_from.isoformat():
        problems.append(f"starts {record['start_date']}, before the user is available")
    if criteria.available_to and record["end_date"] and record["end_date"] > criteria.available_to.isoformat():
        problems.append(f"ends {record['end_date']}, after the user's availability")
    duration = record["duration_days"]
    if criteria.max_duration_days is not None and duration is not None and duration > criteria.max_duration_days:
        problems.append(f"lasts {duration} days, over the limit of {criteria.max_duration_days}")
    return problems


def assess_eligibility(
    record: dict, profile: UserProfile,
) -> tuple[EligibilityStatus, list[str], list[str], list[str]]:
    """-> (status, Arabic notes, skills the user has not answered, skills the user lacks).

    Confirmed only when the source states its mandatory requirements and the profile
    meets every one. Unknown requirements, or ones the form cannot collect (age,
    study year), keep eligibility uncertain.
    """
    mandatory = record.get("mandatory_requirements")
    if mandatory is None:
        return (EligibilityStatus.UNCERTAIN,
                ["لم يذكر الإعلان شروط الالتحاق؛ الأهلية غير مؤكدة."], [], [])

    notes: list[str] = []
    unanswered: list[str] = []
    lacks: list[str] = []
    confirmed = True
    user_skills = {normalize_arabic(s.name) for s in profile.skills or []}

    # Read the statements themselves too, so a missed extraction cannot confirm eligibility.
    if record.get("min_age") or any("عمر" in s for s in mandatory):
        confirmed = False
        age = f" {record['min_age']} سنة فأكثر" if record.get("min_age") else " محدداً"
        notes.append(f"يشترط الإعلان عمراً{age}، والملف لا يتضمن العمر.")
    if record.get("required_study_field") or any("طالباً" in s for s in mandatory):
        confirmed = False
        notes.append("يشترط الإعلان أن يكون المتقدم طالباً في السنة الأخيرة من تخصص ذي صلة، "
                     "والملف لا يتضمن السنة الدراسية.")
    for skill in record.get("required_skills") or []:
        if profile.skills is None:
            confirmed = False
            unanswered.append(skill)
            notes.append(f"يشترط الإعلان معرفة {skill}، ولم تحدد مهاراتك بعد.")
        elif normalize_arabic(skill) not in user_skills:
            lacks.append(skill)

    known = sum(1 for s in mandatory if "عمر" in s or "طالباً" in s or "معرفة" in s)
    if known < len(mandatory):
        confirmed = False
        notes.append("بعض شروط الإعلان لا يمكن التحقق منها من ملفك.")

    status = EligibilityStatus.CONFIRMED if confirmed and not lacks else EligibilityStatus.UNCERTAIN
    return status, notes, unanswered, lacks


def _check_needs_info(result: AgentResult) -> CheckedResult:
    allowed = [f for f in result.missing_fields
               if f in EDITABLE_FIELDS or INDEXED_SKILL_FIELD.match(f)]
    rejected = [f for f in result.missing_fields if f not in allowed]
    if not allowed:
        return CheckedResult(
            outcome=AgentOutcome.NO_MATCH,
            rejections=[f"cannot ask the user for {f}: the form does not collect it" for f in rejected],
        )
    return CheckedResult(
        outcome=AgentOutcome.NEEDS_INFO, missing_fields=allowed,
        missing_explanations=[n for n in result.notes if n.strip()],
        warnings=[f"dropped request for uncollected field {f}" for f in rejected],
    )


def _check_sources(rec: AgentRecommendation, record: dict) -> list[str]:
    unknown = set(rec.source_ids) - set(record["source_ids"])
    if unknown:
        return [f"source IDs {sorted(unknown)} do not belong to this opportunity"]
    return []


def _check_availability(record: dict, context: ValidationContext) -> list[str]:
    problems = []
    if record.get("application_status") in ("cancelled", "closed"):
        problems.append(f"the source says it is {record['application_status']}")
    if deadline_state(record.get("deadline"), context.evaluation_time) == "passed":
        problems.append(f"the deadline {record['deadline']} has passed")
    today = local_date(context.evaluation_time).isoformat()
    if record.get("end_date") and record["end_date"] < today:
        problems.append(f"the activity ended on {record['end_date']}")
    if record.get("application_status") == "not_yet_open" and (
            not record.get("registration_opens_on") or record["registration_opens_on"] > today):
        problems.append(f"registration has not opened yet (opens {record.get('registration_opens_on')})")
    return problems


def _check_quotes(rec: AgentRecommendation, record: dict) -> list[str]:
    """Quoted text in the reason or caveats must come from what the search tool showed the
    agent for this opportunity: its source sentences and its stored facts (dates, cost,
    deadline, requirements). Anything else is an invented quote."""
    facts = [record.get(k) for k in ("title", "description", "city", "delivery_mode", "start_date",
                                     "end_date", "deadline", "cost_sar", "duration_days",
                                     "opportunity_type", "application_status")]
    for key in ("mandatory_requirements", "preferred_requirements", "required_skills", "caveats"):
        facts += record.get(key) or []
    if isinstance(record.get("cost_sar"), float) and record["cost_sar"].is_integer():
        facts.append(int(record["cost_sar"]))  # "120" as well as "120.0"
    source_text = normalize_arabic(" ".join(
        [str(f) for f in facts if f is not None] + list((record.get("evidence") or {}).values())
    ))
    problems = []
    for text in [rec.reason, *rec.caveats]:
        for groups in re.findall(r"«([^»]{4,})»|\"([^\"]{4,})\"", text or ""):
            quote = next(g for g in groups if g)
            if normalize_arabic(quote) not in source_text:
                problems.append(f"quoted text «{quote}» is not in the source")
    return problems


def _merge(*lists: list[str]) -> list[str]:
    return list(dict.fromkeys(item for items in lists for item in items if item))
