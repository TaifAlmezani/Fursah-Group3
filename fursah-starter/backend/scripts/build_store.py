"""Build the FURSAH SQLite store from the notebook's prepared sources.

Reads data/prepared/source_preparation.json (the notebook output) and writes
data/prepared/fursah.db. Each source keeps its ID, original text and the
notebook's facts (fee, deadline, link). Rule-based extraction then adds the
facts the application filters on: type, city, delivery mode, dates, sessions,
application status, requirements and skills.

Every extracted fact stores the exact sentence it came from as evidence. Facts
the text does not state stay null; nothing is guessed. The rules match the
fixed sentence patterns found in the corpus; review the printed coverage after
changing the data.

Run from fursah-starter/backend/:
    python scripts/build_store.py
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.infrastructure.config import get_settings  # noqa: E402

PREPARED_FILE = Path("prepared") / "source_preparation.json"
STORE_FILE = Path("prepared") / "fursah.db"
EXTRACTION_METHOD = "rules-v1"

MONTHS = {
    "يناير": 1, "فبراير": 2, "مارس": 3, "أبريل": 4, "مايو": 5, "يونيو": 6,
    "يوليو": 7, "أغسطس": 8, "سبتمبر": 9, "أكتوبر": 10, "نوفمبر": 11, "ديسمبر": 12,
}
DATE = r"(\d{1,2}) (\S+) (\d{4})"

# Every announcement names its activity with exactly one of these phrases.
TYPE_PHRASES = {
    "ورشة عمل": "workshop", "ورشة تطبيقية": "workshop", "جلسة تدريبية عملية": "workshop",
    "دورة من عدة لقاءات": "course", "دورة تدريبية": "course",
    "برنامج تعليمي متسلسل": "course",
    "تدريب عملي داخل مقر العمل": "internship", "تدريب مهني داخل المؤسسة": "internship",
    "فرصة تدريب ميداني بإشراف الفريق": "internship",
    "منافسة لتطوير فكرة": "competition", "تحدي تنافسي": "competition",
    "مسابقة فرق": "competition",
    "نشاط تطوعي": "volunteering", "مهمة تطوعية مجتمعية": "volunteering",
    "فرصة للتطوع": "volunteering",
    "فعالية حوارية": "event", "لقاء مفتوح": "event", "أمسية معرفية": "event",
}

# Source spelling -> frontend choice (frontend/src/profile/choices.ts).
CITY_ALIASES = {"مكة": "مكة المكرمة"}

STATUS_PHRASES = [
    (r"تم إلغاء النشاط ولن يُعقد[^.]*\.|أُلغيت هذه الفرصة[^.]*\.|اعتذر المنظمون عن إقامة البرنامج وألغوه\.", "cancelled"),
    (r"انتهى استقبال الطلبات[^.]*\.|أُغلق التسجيل لاكتمال المقاعد\.|باب التقديم مغلق حالياً\.", "closed"),
    (rf"لم يبدأ التسجيل بعد؛ يفتح يوم {DATE}\.", "not_yet_open"),
    (r"تُستقبل الطلبات الآن\.|باب المشاركة مفتوح وقت نشر الإعلان\.|التسجيل مفتوح حالياً\.", "open"),
]

# Lead-ins of the sentence that describes what participants actually do.
ACTIVITY_LEADS = (
    "يساعد المتطوعون", "تتنافس الفرق", "يعمل المشاركون", "يعمل المتدرب",
    "سيتناول المشاركون", "يركز المحتوى", "تدور الأنشطة", "يمكن للحضور متابعة",
    "يستعرض المنظمون", "سنبدأ بحالة صغيرة", "الهدف هو", "يتضمن البرنامج",
)

SCHEMA = """
DROP TABLE IF EXISTS sources;
CREATE TABLE sources (
    source_id             TEXT PRIMARY KEY,
    opportunity_id        TEXT NOT NULL,
    source_type           TEXT,
    source_name           TEXT,
    published_at          TEXT,
    content               TEXT NOT NULL,
    activity_url          TEXT,
    title                 TEXT,
    opportunity_type      TEXT,
    city                  TEXT,
    delivery_mode         TEXT,
    start_date            TEXT,
    end_date              TEXT,
    duration_days         INTEGER,
    cost_sar              REAL,
    fee_evidence          TEXT,
    deadline_date         TEXT,
    application_status    TEXT,
    facts_json            TEXT NOT NULL,
    warnings_json         TEXT NOT NULL,
    extraction_method     TEXT NOT NULL
);
CREATE INDEX idx_sources_opportunity ON sources(opportunity_id);
CREATE INDEX idx_sources_filters ON sources(opportunity_type, city, delivery_mode);
"""


def sentences(text: str) -> list[str]:
    """Split on line breaks and sentence ends; posts may be one paragraph."""
    parts = re.split(r"[\r\n]+|(?<=[.؟!])\s+", text)
    return [p.strip() for p in parts if p.strip()]


def parse_date(day: str, month: str, year: str) -> str | None:
    number = MONTHS.get(month)
    if number is None:
        return None
    try:
        return date(int(year), number, int(day)).isoformat()
    except ValueError:
        return None


def find_sentence(text: str, pattern: str) -> tuple[re.Match | None, str | None]:
    for sentence in sentences(text):
        match = re.search(pattern, sentence)
        if match:
            return match, sentence
    return None, None


def extract_title(text: str) -> str | None:
    quoted = list(dict.fromkeys(re.findall(r"«([^»]+)»", text)))
    programmes = set(re.findall(r"برنامج «([^»]+)» لهذا الموسم", text))
    titles = [q for q in quoted if q not in programmes]
    return titles[0] if len(titles) == 1 else None


def extract_location(text: str, facts: dict, evidence: dict) -> None:
    match, sentence = find_sentence(text, r"المشاركة عن بعد بالكامل")
    if match:
        facts.update(delivery_mode="online", physical_attendance_required=False,
                     remote_participation_allowed=True)
        evidence["delivery_mode"] = sentence
        return
    match, sentence = find_sentence(text, r"البداية حضورية في ([^،]+)، ثم تُعقد جلسة متابعة عن بعد")
    if match:
        # First meeting on site is required to complete; only the follow-up is remote.
        facts.update(delivery_mode="hybrid", city=match.group(1).strip(),
                     physical_attendance_required=True)
        evidence["delivery_mode"] = evidence["city"] = sentence
        return
    match, sentence = find_sentence(text, r"الحضور في ([^.،]+?) داخل مقر ([^.]+)\.")
    if match:
        facts.update(delivery_mode="in_person", city=match.group(1).strip(),
                     venue=match.group(2).strip(), physical_attendance_required=True)
        evidence["delivery_mode"] = evidence["city"] = sentence
        return
    match, sentence = find_sentence(text, r"العمل حضوري داخل مقر (.+?) في ([^.]+)\.")
    if match:
        facts.update(delivery_mode="in_person", city=match.group(2).strip(),
                     venue=match.group(1).strip(), physical_attendance_required=True)
        evidence["delivery_mode"] = evidence["city"] = sentence


def extract_dates(text: str, facts: dict, evidence: dict) -> None:
    match, sentence = find_sentence(text, rf"يبدأ البرنامج يوم {DATE} وينتهي يوم {DATE}")
    if match:
        g = match.groups()
        facts["start_date"], facts["end_date"] = parse_date(*g[:3]), parse_date(*g[3:])
        evidence["start_date"] = evidence["end_date"] = sentence
        return
    match, sentence = find_sentence(text, rf"هذا نشاط ليوم واحد بتاريخ {DATE}")
    if match:
        facts["start_date"] = facts["end_date"] = parse_date(*match.groups())
        evidence["start_date"] = evidence["end_date"] = sentence


def extract_sessions(text: str, facts: dict, evidence: dict) -> None:
    match, sentence = find_sentence(text, r"يشمل (\d+) لقاءات، مدة كل لقاء (ساعتان|ساعة|(\d+) ساعات)")
    if not match:
        return
    count = int(match.group(1))
    hours = 2.0 if match.group(2) == "ساعتان" else 1.0 if match.group(2) == "ساعة" else float(match.group(3))
    facts.update(session_count=count, hours_per_session=hours, total_hours=count * hours)
    evidence["session_count"] = sentence


def extract_status(text: str, facts: dict, evidence: dict) -> None:
    for pattern, status in STATUS_PHRASES:
        match, sentence = find_sentence(text, pattern)
        if match:
            facts["application_status"] = status
            evidence["application_status"] = sentence
            if status == "not_yet_open":
                facts["registration_opens_on"] = parse_date(*match.groups())
            return


def extract_requirements(text: str, facts: dict, evidence: dict) -> None:
    mandatory, preferred, skills = [], [], []
    for sentence in sentences(text):
        if sentence.startswith("يشترط"):
            mandatory.append(sentence)
            if age := re.search(r"عمر المشارك (\d+) سنة فأكثر", sentence):
                facts["min_age"] = int(age.group(1))
            # Fields can be several words ("خدمة المجتمع").
            if field := re.search(r"طالباً في السنة الأخيرة من تخصص ذي صلة بـ\s*(.+?)\.?$", sentence):
                facts["required_study_field"] = field.group(1)
        elif skill := re.search(r"^معرفة (.+?) مطلوبة\.?$", sentence):
            mandatory.append(sentence)
            skills.append(skill.group(1))
        elif re.search(r"^تُعد معرفة .+? ميزة إضافية وليست شرطاً", sentence):
            preferred.append(sentence)
    no_experience = find_sentence(text, r"الخبرة السابقة ليست شرطاً")[1]

    if mandatory:
        facts["mandatory_requirements"] = mandatory
        evidence["mandatory_requirements"] = " | ".join(mandatory)
    if preferred:
        facts["preferred_requirements"] = preferred
        evidence["preferred_requirements"] = " | ".join(preferred)
    if skills:
        facts["required_skills"] = skills
        evidence["required_skills"] = " | ".join(s for s in mandatory if "معرفة" in s)
    elif no_experience:
        # Explicitly none: distinct from unknown (null).
        facts["required_skills"] = []
        evidence["required_skills"] = no_experience


def extract_updates(text: str, facts: dict, evidence: dict) -> None:
    match, sentence = find_sentence(
        text, rf"تحديث من .+? بتاريخ {DATE} لفرصة «([^»]+)» المعلنة يوم {DATE}"
    )
    if match:
        g = match.groups()
        facts["update"] = {
            "kind": "update", "updated_on": parse_date(*g[:3]), "title": g[3],
            "refers_to_published_on": parse_date(*g[4:7]),
        }
        if moved := re.search(rf"تغيّر الموعد من {DATE} إلى {DATE}", text):
            m = moved.groups()
            facts["update"]["date_changed"] = {"from": parse_date(*m[:3]), "to": parse_date(*m[3:])}
        if extended := re.search(rf"مُدد آخر يوم للتقديم من {DATE} إلى {DATE}", text):
            e = extended.groups()
            facts["update"]["deadline_changed"] = {"from": parse_date(*e[:3]), "to": parse_date(*e[3:])}
        evidence["update"] = sentence
        return
    match, sentence = find_sentence(text, rf"تذكير من .+? بشأن «([^»]+)»، المعلن يوم {DATE}")
    if match:
        g = match.groups()
        facts["update"] = {"kind": "reminder", "title": g[0],
                           "refers_to_published_on": parse_date(*g[1:])}
        evidence["update"] = sentence
        return
    match, sentence = find_sentence(text, r"إعادة نشر")
    if match:
        facts["update"] = {"kind": "repost"}
        evidence["update"] = sentence


def extract(record: dict) -> tuple[dict, list[str]]:
    text = record["content"]
    facts: dict = {"evidence": {}}
    evidence = facts["evidence"]
    warnings: list[str] = []

    facts["title"] = extract_title(text)
    if facts["title"] is None:
        warnings.append("title not identified; needs review")

    types = {TYPE_PHRASES[p] for p in TYPE_PHRASES if p in text}
    if len(types) == 1:
        facts["opportunity_type"] = types.pop()
        evidence["opportunity_type"] = next(s for s in sentences(text)
                                            if any(p in s for p in TYPE_PHRASES))
    else:
        facts["opportunity_type"] = None
        warnings.append(f"opportunity type unclear ({len(types)} candidates)")

    extract_location(text, facts, evidence)
    if facts.get("city"):
        facts["city"] = CITY_ALIASES.get(facts["city"], facts["city"])
    if not facts.get("delivery_mode"):
        warnings.append("delivery mode not stated")

    extract_dates(text, facts, evidence)
    if facts.get("start_date") and facts.get("end_date") and facts["end_date"] < facts["start_date"]:
        warnings.append("end date before start date")
    extract_sessions(text, facts, evidence)
    extract_status(text, facts, evidence)
    extract_requirements(text, facts, evidence)
    extract_updates(text, facts, evidence)

    if facts.get("required_skills") and "الخبرة السابقة ليست شرطاً" in text:
        warnings.append("states a required skill and that prior experience is not required")
    if "لم تُحدد رسوم المشاركة" in text:
        warnings.append("fee explicitly not yet set")

    facts["description"] = next(
        (s for s in sentences(text) if s.startswith(ACTIVITY_LEADS)), None
    )
    return facts, warnings


def duration_days(start: str | None, end: str | None) -> int | None:
    if not (start and end):
        return None
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    return days if days > 0 else None


def opportunity_id_for(source_id: str) -> str:
    # One opportunity per source until related announcements are reviewed and grouped.
    return "OPP-" + source_id.split("-", 1)[-1]


def apply_updates(extracted: list[tuple[dict, dict, list[str]]]) -> int:
    """Apply explicit update posts to the announcement they name. Returns how many applied.

    An update ("تحديث … لفرصة «T» المعلنة يوم D: … ومُدد آخر يوم للتقديم من A إلى B") names its
    original by title and publication date and shares its activity link. Only then are its
    changes applied: the new deadline, the update's registration status, and the new start
    date if the original's start is the one that changed (a moved start does not establish a
    new end). Similar text alone is never used; nothing is merged or deleted, and both source
    IDs stay.
    """
    applied = 0
    for record, facts, warnings in extracted:
        update = facts.get("update") or {}
        if update.get("kind") != "update":
            continue
        new_deadline = (update.get("deadline_changed") or {}).get("to")
        if new_deadline and not facts.get("deadline_date"):
            facts["deadline_date"] = new_deadline  # the update states it only as "من … إلى …"
        for original, original_facts, original_warnings in extracted:
            if (original is record or not record.get("activity_url")
                    or original.get("activity_url") != record["activity_url"]
                    or original_facts.get("title") != update.get("title")
                    or (original.get("published_at") or "")[:10] != update.get("refers_to_published_on")):
                continue
            if new_deadline:
                original_facts["deadline_date"] = new_deadline
            moved = update.get("date_changed") or {}
            if moved.get("to") and original_facts.get("start_date") == moved.get("from"):
                original_facts["start_date"] = moved["to"]
            if facts.get("application_status"):  # the update's newer statement, e.g. closed
                original_facts["application_status"] = facts["application_status"]
                if facts.get("registration_opens_on"):
                    original_facts["registration_opens_on"] = facts["registration_opens_on"]
            # The evidence for the change is in the update's own text, kept with its source ID.
            original_facts["updated_by"] = {"source_id": record["source_id"],
                                            "sentence": update_sentence(record["content"])}
            original_warnings.append(f"updated by {record['source_id']}")
            applied += 1
    return applied


def update_sentence(text: str) -> str:
    return next((s for s in sentences(text) if s.startswith(("📌", "✨")) or "تحديث من" in s), "")


def build(records: list[dict], conn: sqlite3.Connection) -> list[list[str]]:
    conn.executescript(SCHEMA)
    extracted = []
    for r in records:
        facts, warnings = extract(r)
        # The notebook export writes date-only deadlines as midnight timestamps.
        facts["deadline_date"] = (r.get("deadline_date") or "")[:10] or None
        extracted.append((r, facts, warnings))
    applied = apply_updates(extracted)
    print(f"Applied {applied} explicit updates to their original announcements.")

    all_warnings = []
    for r, facts, warnings in extracted:
        all_warnings.append(warnings)
        conn.execute(
            "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r["source_id"], opportunity_id_for(r["source_id"]), r.get("source_type"),
                r.get("source_name"), r.get("published_at"), r["content"],
                r.get("activity_url"), facts["title"], facts["opportunity_type"],
                facts.get("city"), facts.get("delivery_mode"), facts.get("start_date"),
                facts.get("end_date"), duration_days(facts.get("start_date"), facts.get("end_date")),
                r.get("cost_sar"), r.get("fee_evidence"), facts["deadline_date"],
                facts.get("application_status"),
                json.dumps(facts, ensure_ascii=False), json.dumps(warnings, ensure_ascii=False),
                EXTRACTION_METHOD,
            ),
        )
    conn.commit()
    return all_warnings


def report(conn: sqlite3.Connection, warnings: list[list[str]]) -> None:
    total = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    print(f"Stored {total} sources. Coverage (stated in text / total):")
    for column in ("title", "opportunity_type", "delivery_mode", "city", "start_date",
                   "cost_sar", "deadline_date", "application_status"):
        known = conn.execute(f"SELECT COUNT({column}) FROM sources").fetchone()[0]
        print(f"  {column:<20} {known:>4} / {total}")
    flagged = sum(1 for w in warnings if w)
    print(f"Sources with review warnings: {flagged}")


def main() -> None:
    settings = get_settings()
    prepared_path = settings.data_dir / PREPARED_FILE
    if not prepared_path.is_file():
        sys.exit(f"{prepared_path} not found: run the data notebook first.")
    records = json.loads(prepared_path.read_text(encoding="utf-8"))

    store_path = settings.data_dir / STORE_FILE
    store_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(store_path)
    try:
        warnings = build(records, conn)
        report(conn, warnings)
    finally:
        conn.close()
    print(f"Store written to {store_path}")


if __name__ == "__main__":
    main()
