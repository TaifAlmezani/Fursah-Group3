# Learner guidance: Return source IDs with evidence. Compare timezone-aware deadlines
# using a timezone-aware evaluation_time >= deadline for expiry. Never assume hybrid means physical
# attendance is optional.
#
# Team design: plain functions over the repository. agent.py wraps them as LangChain
# tools; they stay framework-free so validation and tests can call them directly.
from __future__ import annotations

from datetime import datetime
from typing import Any

from .infrastructure.observability import start_span
from .repository import OpportunityRepository, SearchCriteria, local_date

# Fields the agent sees for each candidate; the full record stays in the repository.
CANDIDATE_FIELDS = (
    "opportunity_id", "source_ids", "title", "opportunity_type", "city", "delivery_mode",
    "physical_attendance_required", "start_date", "end_date", "duration_days", "cost_sar",
    "deadline", "application_status", "mandatory_requirements", "preferred_requirements",
    "required_skills", "description", "caveats", "relevance",
)


def search_opportunities(criteria: SearchCriteria, repo: OpportunityRepository) -> dict[str, Any]:
    """Return candidate evidence, not final rankings.

    `candidates` is what the agent reads; `records` keeps the full rows (with evidence
    sentences) so validation can check the agent's picks against them.
    """
    # Day 4: which candidates came back (the tool span only shows the arguments).
    with start_span("repo.search", topics=criteria.topics, cities=criteria.cities,
                    opportunity_types=criteria.opportunity_types,
                    delivery_modes=criteria.delivery_modes) as span:
        found = repo.search(criteria)
        span.set_attribute("candidate_count", len(found))
        span.set_attribute("candidate_ids", [c["opportunity_id"] for c in found])
        span.set_attribute("candidate_source_ids", [s for c in found for s in c["source_ids"]])
    return {
        "count": len(found),
        "candidates": [{k: c[k] for k in CANDIDATE_FIELDS} for c in found],
        "records": {c["opportunity_id"]: c for c in found},
    }


def get_opportunity_details(opportunity_id: str, repo: OpportunityRepository) -> dict[str, Any]:
    """Return source-grounded details for one opportunity."""
    details = repo.get_details(opportunity_id)
    if details is None:
        return {"found": False, "opportunity_id": opportunity_id}
    return {"found": True, **details.model_dump(mode="json")}


def check_deadline(opportunity_id: str, repo: OpportunityRepository,
                   evaluation_time: datetime) -> dict[str, Any]:
    """Check the opportunity deadline using the supplied comparison time."""
    details = repo.get_details(opportunity_id)
    if details is None:
        return {"found": False, "opportunity_id": opportunity_id}
    return {
        "found": True,
        "opportunity_id": opportunity_id,
        "deadline": details.deadline,
        "evaluated_on": local_date(evaluation_time).isoformat(),
        "state": deadline_state(details.deadline, evaluation_time),
    }


def deadline_state(deadline: str | None, evaluation_time: datetime) -> str:
    """passed / not_passed / uncertain / unknown for a date-only deadline.

    Sources give a date without a closing time, so the deadline day itself is
    uncertain rather than open: the cutoff may already have been reached.
    """
    if not deadline:
        return "unknown"
    today = local_date(evaluation_time).isoformat()
    if deadline < today:
        return "passed"
    if deadline == today:
        return "uncertain"
    return "not_passed"
