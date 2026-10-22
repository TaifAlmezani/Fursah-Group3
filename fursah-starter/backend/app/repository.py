# Learner guidance: prepare data/raw/raw_opportunities.csv using data/README.md.
# Choose your own storage and internal schema. The bridge is an optional CSV/JSON
# helper. Map your storage into the public API contract; rename/split this module freely.
#
# Team design: scripts/build_store.py writes data/prepared/fursah.db (SQLite, one row
# per source announcement with its extracted facts and evidence). This module reads
# it for readiness, details and the agent's search tool.
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from .infrastructure.data_package import DatasetLoader
from .schemas import EligibilityRequirements, OpportunityDetailsResponse

STORE_FILE = ("prepared", "fursah.db")
# Standard endings added to activity titles in the announcements.
TITLE_SUFFIXES = ("خطوة بخطوة", "للمجتمع", "للمهتمين", "في الممارسة", "من الفكرة إلى التطبيق")
# Source dates are Saudi calendar dates; Asia/Riyadh is UTC+3 with no daylight saving.
RIYADH = timezone(timedelta(hours=3))
REQUIRED_COLUMNS = {
    "source_id", "opportunity_id", "content", "title", "opportunity_type", "city",
    "delivery_mode", "start_date", "end_date", "duration_days", "cost_sar",
    "deadline_date", "application_status", "facts_json", "warnings_json",
}

# Review warnings written by build_store.py, shown to users in Arabic.
WARNING_LABELS = {
    "fee explicitly not yet set": "ذكر الإعلان أن رسوم المشاركة لم تُحدد بعد.",
    "states a required skill and that prior experience is not required":
        "يذكر الإعلان مهارة مطلوبة ويذكر أيضاً أن الخبرة السابقة ليست شرطاً؛ راجع النص.",
    "title not identified; needs review": "لم يُحدد عنوان النشاط بوضوح في الإعلان.",
    "delivery mode not stated": "لم يذكر الإعلان طريقة الحضور.",
    "end date before start date": "تاريخ النهاية في الإعلان يسبق تاريخ البداية.",
}


@dataclass
class SearchCriteria:
    """Resolved constraints for one discovery request; empty means no restriction."""

    cities: list[str] = field(default_factory=list)
    exclude_cities: list[str] = field(default_factory=list)
    opportunity_types: list[str] = field(default_factory=list)
    delivery_modes: list[str] = field(default_factory=list)
    exclude_delivery_modes: list[str] = field(default_factory=list)
    max_budget_sar: float | None = None
    available_from: date | None = None
    available_to: date | None = None
    max_duration_days: int | None = None
    topics: list[str] = field(default_factory=list)
    limit: int = 15


def not_yet_open(row, today: date) -> bool:
    """The source says registration opens later than `today` (or gives no opening date)."""
    if row["application_status"] != "not_yet_open":
        return False
    opens = json.loads(row["facts_json"]).get("registration_opens_on")
    return opens is None or opens > today.isoformat()


def local_date(moment: datetime) -> date:
    """Calendar date in Riyadh for comparing with date-only source dates."""
    return moment.astimezone(RIYADH).date()


def normalize_arabic(text: str) -> str:
    """Fold common spelling variants so topic words match their source text."""
    text = re.sub(r"[ً-ْـ]", "", text or "")  # diacritics, tatweel
    text = re.sub(r"[إأآ]", "ا", text)
    text = text.replace("ة", "ه").replace("ى", "ي")
    return text.lower()


class OpportunityRepository:
    """Read access to the team's SQLite opportunity store."""

    def __init__(
        self,
        bridge: DatasetLoader,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.bridge = bridge
        self.path = bridge.data_dir.joinpath(*STORE_FILE)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _connect(self) -> sqlite3.Connection:
        # Read-only: the application never changes prepared evidence.
        conn = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def known_subjects(self) -> list[str]:
        """The data's activity subjects (normalized): titles without their standard suffixes.

        186 titles reduce to 31 subjects such as "مقدمه في python"; code can find these
        inside a query reliably, whatever connecting words they contain.
        """
        if getattr(self, "_subjects", None) is None:
            catalog = {}
            with self._connect() as conn:
                for (title,) in conn.execute("SELECT DISTINCT title FROM sources WHERE title IS NOT NULL ORDER BY title"):
                    subject = title
                    for suffix in TITLE_SUFFIXES:
                        subject = subject.removesuffix(" " + suffix)
                    catalog.setdefault(normalize_arabic(subject), subject)
            self._subjects = sorted(catalog, key=len, reverse=True)
            self._catalog = sorted(catalog.values())
        return self._subjects

    def subject_catalog(self) -> list[str]:
        """The same 31 subjects in their original spelling, for the model to choose from."""
        self.known_subjects()
        return self._catalog

    def topic_in_data(self, topic: str) -> bool:
        """Does any announcement (open or not) have this topic in its title or description?

        Used to tell a real subject from a request word. Availability is not checked here:
        a subject whose opportunities are all closed or over budget is still a subject, and
        the search must then answer no_match rather than widen to unrelated words.
        """
        pattern = _topic_pattern(normalize_arabic(topic))
        with self._connect() as conn:
            for title, facts_json in conn.execute("SELECT title, facts_json FROM sources"):
                text = normalize_arabic(" ".join(filter(None, [title, json.loads(facts_json).get("description")])))
                if pattern.search(text):
                    return True
        return False

    def is_ready(self) -> bool:
        if not self.path.is_file():
            return False
        try:
            with self._connect() as conn:
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(sources)")}
                if not REQUIRED_COLUMNS <= columns:
                    return False
                count = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        except sqlite3.Error:
            return False
        return count > 0

    def search(self, criteria: SearchCriteria) -> list[dict[str, Any]]:
        """Criteria -> ranked candidates with source evidence.

        Hard constraints are applied here so callers cannot bypass them. Unknown dates and
        duration do not exclude a candidate (they are returned as caveats); an unknown fee
        does when a budget is set, since it cannot be shown to be within it.
        Always removed: cancelled, closed, deadline passed, activity already ended, and
        registration that has not opened yet.
        """
        today = self._today()
        where = ["(application_status IS NULL OR application_status NOT IN ('cancelled', 'closed'))",
                 "(deadline_date IS NULL OR deadline_date >= ?)",
                 "(end_date IS NULL OR end_date >= ?)"]
        params: list[Any] = [today.isoformat(), today.isoformat()]

        if criteria.opportunity_types:
            where.append(f"opportunity_type IN ({_marks(criteria.opportunity_types)})")
            params += criteria.opportunity_types
        if criteria.delivery_modes:
            where.append(f"delivery_mode IN ({_marks(criteria.delivery_modes)})")
            params += criteria.delivery_modes
        if criteria.exclude_delivery_modes:
            where.append(f"delivery_mode NOT IN ({_marks(criteria.exclude_delivery_modes)})")
            params += criteria.exclude_delivery_modes
        if criteria.cities:
            # Fully online activities need no travel, so they match any city.
            where.append(f"(city IN ({_marks(criteria.cities)}) OR delivery_mode = 'online')")
            params += criteria.cities
        if criteria.exclude_cities:
            where.append(f"(city IS NULL OR city NOT IN ({_marks(criteria.exclude_cities)}))")
            params += criteria.exclude_cities
        if criteria.max_budget_sar is not None:
            # Unknown is not free: an unknown fee cannot be shown to fit the budget.
            where.append("(cost_sar IS NOT NULL AND cost_sar <= ?)")
            params.append(criteria.max_budget_sar)
        if criteria.available_from is not None:
            where.append("(start_date IS NULL OR start_date >= ?)")
            params.append(criteria.available_from.isoformat())
        if criteria.available_to is not None:
            where.append("(end_date IS NULL OR end_date <= ?)")
            params.append(criteria.available_to.isoformat())
        if criteria.max_duration_days is not None:
            where.append("(duration_days IS NULL OR duration_days <= ?)")
            params.append(criteria.max_duration_days)

        sql = f"SELECT * FROM sources WHERE {' AND '.join(where)}"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        # Registration not open yet at the evaluation date (an opening date that has passed
        # stays: its current status needs verification, which is shown as a caveat).
        rows = [row for row in rows if not not_yet_open(row, today)]

        topics = [normalize_arabic(t) for t in criteria.topics if t.strip()]
        candidates = [self._candidate(row, topics, criteria, today) for row in rows]
        if topics:
            candidates = [c for c in candidates if c["relevance"] > 0]
        if criteria.cities:
            # The user named a city: a match there ranks above the same match online.
            for c in candidates:
                if c["city"] in criteria.cities:
                    c["relevance"] += 1
        candidates.sort(key=lambda c: (-c["relevance"], c["start_date"] or "9999"))
        return candidates[: criteria.limit]

    def get_details(self, opportunity_id: str) -> OpportunityDetailsResponse | None:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sources WHERE opportunity_id = ? ORDER BY source_id",
                (opportunity_id,),
            ).fetchall()
        if not rows:
            return None
        return self._details(rows[0], [r["source_id"] for r in rows])

    # --- mapping -------------------------------------------------------------

    def _today(self) -> date:
        return local_date(self.clock())

    def _details(self, row: sqlite3.Row, source_ids: list[str]) -> OpportunityDetailsResponse:
        facts = json.loads(row["facts_json"])
        status, status_warnings = self._status(row, facts)
        mandatory = facts.get("mandatory_requirements")
        preferred = facts.get("preferred_requirements")
        return OpportunityDetailsResponse(
            evidence_version=evidence_version(row),
            opportunity_id=row["opportunity_id"],
            title=row["title"] or facts.get("title") or row["source_id"],
            opportunity_type=row["opportunity_type"],
            description=facts.get("description"),
            location=_location(row["city"], facts.get("venue")),
            delivery_mode=row["delivery_mode"],
            start_date=row["start_date"],
            end_date=row["end_date"],
            remote_participation_allowed=facts.get("remote_participation_allowed"),
            physical_attendance_required=facts.get("physical_attendance_required"),
            duration=row["duration_days"],
            session_count=facts.get("session_count"),
            hours_per_session=facts.get("hours_per_session"),
            total_hours=facts.get("total_hours"),
            application_url=row["activity_url"],
            deadline=row["deadline_date"],
            application_status=status,
            topics=[],
            eligibility_requirements=(
                EligibilityRequirements(mandatory=mandatory, preferred=preferred)
                if mandatory is not None or preferred is not None else None
            ),
            required_skills=facts.get("required_skills"),
            cost=row["cost_sar"],
            source_ids=source_ids,
            last_verified_at=None,  # publication time is not verification time
            data_warnings=self._warnings(row, facts) + status_warnings,
        )

    def _status(self, row: sqlite3.Row, facts: dict) -> tuple[str | None, list[str]]:
        """Source statement about applications, checked against the evaluation date."""
        stated = row["application_status"]
        today = self._today()
        deadline = row["deadline_date"]
        if stated in ("cancelled", "closed"):
            return stated, []
        if deadline and deadline < today.isoformat():
            return "closed", [f"انتهى آخر يوم للتقديم ({deadline})."]
        if stated == "not_yet_open":
            opens = facts.get("registration_opens_on")
            if opens and opens <= today.isoformat():
                return "requires_verification", [
                    f"ذكر الإعلان أن التسجيل يفتح يوم {opens}؛ الحالة الحالية تحتاج إلى تحقق."
                ]
            return "not_yet_open", []
        if stated == "open":
            return "open", ["حالة التسجيل كما ذُكرت وقت نشر الإعلان."]
        return None, []

    def _warnings(self, row: sqlite3.Row, facts: dict) -> list[str]:
        warnings = [WARNING_LABELS.get(w, w) for w in json.loads(row["warnings_json"])]
        if row["cost_sar"] is None and "لم تُحدد رسوم" not in " ".join(warnings):
            warnings.append("لم يذكر الإعلان رسوم المشاركة؛ التكلفة غير معروفة.")
        if row["deadline_date"] is None:
            warnings.append("لم يذكر الإعلان آخر يوم للتقديم.")
        if row["deadline_date"] and row["deadline_date"] == self._today().isoformat():
            warnings.append("اليوم هو آخر يوم للتقديم ولم يُذكر وقت الإغلاق.")
        update = facts.get("update") or {}
        if update.get("kind") == "update":
            changes = []
            if moved := update.get("date_changed"):
                changes.append(f"الموعد من {moved['from']} إلى {moved['to']}")
            if extended := update.get("deadline_changed"):
                changes.append(f"آخر يوم للتقديم من {extended['from']} إلى {extended['to']}")
            if changes:
                warnings.append("هذا الإعلان تحديث لإعلان سابق؛ تغيّر " + " و".join(changes) + ".")
        elif update.get("kind") in ("reminder", "repost"):
            warnings.append("هذا الإعلان إعادة نشر أو تذكير بإعلان سابق.")
        return warnings

    def _candidate(self, row: sqlite3.Row, topics: list[str], criteria: SearchCriteria,
                   today: date) -> dict[str, Any]:
        facts = json.loads(row["facts_json"])
        caveats = []
        if (criteria.available_from or criteria.available_to) and not row["start_date"]:
            caveats.append("مواعيد النشاط غير مذكورة؛ لا يمكن التأكد من توافقها مع وقتك.")
        if criteria.max_duration_days is not None and row["duration_days"] is None:
            caveats.append("مدة النشاط غير مذكورة.")
        if row["deadline_date"] == today.isoformat():
            caveats.append("اليوم هو آخر يوم للتقديم ولم يُذكر وقت الإغلاق؛ قد يكون التقديم أُغلق.")
        if row["deadline_date"] is None:
            caveats.append("لم يُذكر آخر يوم للتقديم.")
        return {
            "opportunity_id": row["opportunity_id"],
            "source_ids": [row["source_id"]],
            "title": row["title"],
            "opportunity_type": row["opportunity_type"],
            "city": row["city"],
            "delivery_mode": row["delivery_mode"],
            "physical_attendance_required": facts.get("physical_attendance_required"),
            "start_date": row["start_date"],
            "end_date": row["end_date"],
            "duration_days": row["duration_days"],
            "cost_sar": row["cost_sar"],
            "deadline": row["deadline_date"],
            "application_status": row["application_status"],
            "registration_opens_on": facts.get("registration_opens_on"),
            "mandatory_requirements": facts.get("mandatory_requirements"),
            "preferred_requirements": facts.get("preferred_requirements"),
            "required_skills": facts.get("required_skills"),
            "min_age": facts.get("min_age"),
            "required_study_field": facts.get("required_study_field"),
            "description": facts.get("description"),
            "evidence": facts.get("evidence", {}),
            "caveats": caveats,
            "evidence_version": evidence_version(row),
            "relevance": _relevance(row, facts, topics),
        }


def evidence_version(row: sqlite3.Row) -> str:
    """Deterministic fingerprint of the facts shown; changes when evidence changes."""
    payload = json.dumps(
        {"source_id": row["source_id"], "content": row["content"],
         "facts": json.loads(row["facts_json"]), "cost": row["cost_sar"],
         "deadline": row["deadline_date"]},
        ensure_ascii=False, sort_keys=True,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _topic_pattern(topic: str) -> re.Pattern:
    """A topic must start a word, after optional Arabic prefixes (و ف ب ل ك، ال، لل).

    Endings stay free ("صحي" matches "الصحيه"), but a topic cannot start mid-word:
    "طب" must not match "تطبيق".
    """
    return re.compile(rf"(?:^|[^\w])(?:[وفبلك])?(?:ال|لل)?{re.escape(topic)}")


def _relevance(row: sqlite3.Row, facts: dict, topics: list[str]) -> int:
    if not topics:
        return 0
    title = normalize_arabic(row["title"] or "")
    description = normalize_arabic(facts.get("description") or "")
    content = normalize_arabic(row["content"])
    score = 0
    for topic in topics:
        pattern = _topic_pattern(topic)
        if pattern.search(title):
            score += 3
        elif pattern.search(description):
            score += 2
        elif pattern.search(content):
            score += 1
    return score


def _location(city: str | None, venue: str | None) -> str | None:
    if city and venue:
        return f"{city} — {venue}"
    return city


def _marks(values: list) -> str:
    return ", ".join("?" for _ in values)
