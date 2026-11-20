"""Map OfferBomber ResumePlan + knowledge base into Reactive Resume ResumeData.

Schema fields follow the official OpenAPI spec (rxresu.me /api/openapi/spec.json,
verified 2026-11-19): `basics`, `sections.{experience,education,projects,...}`,
`customSections`, plus the current `metadata`/`design` preserved from the cloned
source document. We never fabricate a design/template — the caller supplies the
source resume's document and we only replace content fields.
"""

from __future__ import annotations

import html
import re
import uuid
from typing import Any

from ..core.resume_plan import ResumePlan
from ..storage.models import Education, Experience, Profile


def _uid() -> str:
    return uuid.uuid4().hex


def _website() -> dict[str, Any]:
    return {"url": "", "label": "", "inlineLink": False}


def _section(title: str, icon: str = "") -> dict[str, Any]:
    return {
        "title": title,
        "icon": icon,
        "columns": 1,
        "hidden": False,
        "showHeading": True,
        "keepTogether": False,
        "startOnNewPage": False,
        "items": [],
    }


def _normalize_date(value: str | None) -> str | None:
    """Coerce resume date strings to the official YYYY or YYYY-MM format.

    The Reactive Resume OpenAPI enforces /^\\d{4}(?:-(?:0[1-9]|1[0-2]))?$/ on
    `dates.start`/`dates.end`. Real resumes use many separators (2026.07,
    2026/07, 2026年07月, "July 2026", etc.), so normalize before sending.
    Returns None for open-ended markers ("至今", "present", "now").
    """
    if not value:
        return None
    text = str(value).strip()
    if not text or re.search(r"至今|present|now|current|至今", text, re.I):
        return None
    match = re.search(r"(\d{4})\s*[.\-/年]\s*(\d{1,2})", text)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12:
            return f"{year:04d}-{month:02d}"
    match = re.search(r"(\d{4})", text)
    if match:
        return match.group(1)
    return None


def _is_present(value: str | None) -> bool:
    return bool(value and re.search(r"至今|present|now|current", str(value), re.I))


# Publication status must never be upgraded. Ordered from weakest to strongest.
_SUBMITTED_RE = re.compile(r"投稿|在审|under review|submitted|preprint|预投|in submission", re.I)
_ACCEPTED_RE = re.compile(r"已录用|accepted|录用通知", re.I)
_PUBLISHED_RE = re.compile(r"已发表|published|正式发表", re.I)


def _publication_status(text: str) -> str:
    """Classify a paper's real status from its confirmed fact text.

    Returns one of SUBMITTED / ACCEPTED / PUBLISHED / UNKNOWN. Never infers
    PUBLISHED from a mere submission; unknown stays unknown.
    """
    if _PUBLISHED_RE.search(text):
        return "PUBLISHED"
    if _ACCEPTED_RE.search(text):
        return "ACCEPTED"
    if _SUBMITTED_RE.search(text):
        return "SUBMITTED"
    return "UNKNOWN"


_STATUS_LABELS = {
    "PUBLISHED": "Published",
    "ACCEPTED": "Accepted (not yet published)",
    "SUBMITTED": "Submitted / under review",
    "UNKNOWN": "Publication status not confirmed",
}


def _safe_skills(
    plan: ResumePlan, highlight_skills: list[str]
) -> list[str]:
    """Skills actually evidenced by planned fact items.

    The last line of defence: even if a model injected a skill into
    plan.highlight_skills, it cannot appear in the PDF unless it also appears
    in a confirmed fact item that made it into the plan. Ordering follows the
    model's ranking for evidenced skills, then the rest deterministically.
    """
    evidenced = {
        skill.strip()
        for section in plan.sections
        for item in section.items
        for skill in item.skills
        if skill and skill.strip()
    }
    ordered = [s for s in highlight_skills if s in evidenced]
    ordered.extend(sorted(s for s in evidenced if s not in ordered))
    return ordered[:40]


def _dates(start: str | None, end: str | None) -> dict[str, Any]:
    norm_start = _normalize_date(start)
    norm_end = _normalize_date(end)
    present = _is_present(end)
    return {
        "start": norm_start,
        "end": None if present else norm_end,
        "present": present,
        "expected": False,
        "raw": " ".join(part for part in (start, end) if part),
    }


def map_resume_data(
    *,
    plan: ResumePlan,
    profile: Profile | None,
    educations: list[Education],
    experiences: dict[int, Experience],
) -> dict[str, Any]:
    """Build the content portion of ResumeData. Layout is preserved by caller."""
    basics = {
        "name": (profile.display_name if profile else None) or "",
        "headline": (profile.target_position if profile else None) or plan.target_position or "",
        "email": "",
        "phone": "",
        "location": "",
        "website": _website(),
        "customFields": [],
    }
    contact = (profile.contact if profile else None) or ""
    email_match = re.search(r"[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}", contact)
    remainder = contact
    if email_match:
        basics["email"] = email_match.group(0)
        remainder = contact[:email_match.start()] + " " + contact[email_match.end():]
    phone_match = re.search(r"(?<!\d)(?:\+?\d[\d -]{6,}\d)(?!\d)", remainder)
    if phone_match:
        basics["phone"] = phone_match.group(0).strip()

    sections: dict[str, Any] = {
        "profiles": _section("Profiles"),
        "experience": _section("Experience"),
        "education": _section("Education"),
        "projects": _section("Projects"),
        "skills": _section("Skills"),
        "languages": _section("Languages"),
        "interests": _section("Interests"),
        "awards": _section("Awards"),
        "certifications": _section("Certifications"),
        "publications": _section("Publications"),
        "volunteer": _section("Volunteer"),
        "references": _section("References"),
    }

    summary = _section("Summary")
    summary["content"] = html.escape((profile.summary if profile else None) or "")
    summary["hidden"] = not summary["content"]

    for education in educations:
        sections["education"]["items"].append(
            {
                "id": _uid(), "hidden": False,
                "school": education.school or "",
                "degree": education.degree or "",
                "area": education.major or "",
                "grade": "",
                "location": "",
                "period": " ".join(p for p in (education.start_date, education.end_date) if p),
                "dates": _dates(education.start_date, education.end_date),
                "website": _website(),
                "description": html.escape(education.description or ""),
            }
        )

    # Group planned items by their experience so structure reflects the KB.
    grouped: dict[int, list] = {}
    for section in plan.sections:
        for item in section.items:
            grouped.setdefault(item.experience_id, []).append(item)

    for exp_id, items in grouped.items():
        experience = experiences.get(exp_id)
        bullets = "".join(f"<li>{html.escape(item.text)}</li>" for item in items)
        description = f"<ul>{bullets}</ul>" if bullets else ""
        # Classification by the authoritative Experience.type. RESEARCH is a
        # research project, NOT a publication; only PAPER goes to publications,
        # and even then the real status is preserved in the description.
        target_section = "projects"
        if experience:
            if experience.type in {"WORK", "INTERNSHIP"}:
                target_section = "experience"
            elif experience.type == "PAPER":
                target_section = "publications"
            elif experience.type in {"COMPETITION", "AWARD"}:
                target_section = "awards"
            elif experience.type == "RESEARCH":
                target_section = "projects"
        item: dict[str, Any] = {
            "id": _uid(),
            "hidden": False,
            "website": _website(),
            "description": description,
        }
        if target_section == "experience":
            item.update({
                "company": (experience.organization if experience else None) or "",
                "position": (experience.title if experience else None) or "",
                "location": (experience.location if experience else None) or "",
                "period": " ".join(
                    p for p in ((experience.start_date if experience else None),
                                (experience.end_date if experience else None)) if p
                ),
                "dates": _dates(
                    experience.start_date if experience else None,
                    experience.end_date if experience else None,
                ),
                "roles": [],
            })
        elif target_section == "publications":
            # Never let a PAPER item imply publication. Prefix the real status
            # so a submission stays visibly "Submitted / under review" and an
            # unconfirmed status stays "not confirmed", never "Published".
            status_text = " ".join(item_text.text for item_text in items)
            status = _publication_status(status_text)
            status_label = _STATUS_LABELS[status]
            item["description"] = (
                f"<p><strong>Publication status: {html.escape(status_label)}</strong></p>"
                + description
            )
            item.update({
                "title": (experience.title if experience else None) or "",
                "publisher": (experience.organization if experience else None) or "",
                "date": (experience.start_date if experience else None) or "",
                "dates": _dates(experience.start_date if experience else None, None),
            })
        elif target_section == "awards":
            item.update({
                "title": (experience.title if experience else None) or "",
                "awarder": (experience.organization if experience else None) or "",
                "date": (experience.start_date if experience else None) or "",
                "dates": _dates(experience.start_date if experience else None, None),
            })
        else:
            item.update({
                "name": (experience.title if experience else None) or "",
                "period": " ".join(
                    p for p in ((experience.start_date if experience else None),
                                (experience.end_date if experience else None)) if p
                ),
                "dates": _dates(
                    experience.start_date if experience else None,
                    experience.end_date if experience else None,
                ),
            })
        sections[target_section]["items"].append(item)

    skills = _safe_skills(plan, list(plan.highlight_skills))
    if skills:
        sections["skills"]["items"].append(
            {
                "id": _uid(), "hidden": False, "icon": "", "iconColor": "",
                "name": "Skills", "proficiency": "", "level": 0,
                "keywords": skills[:40],
            }
        )
    else:
        sections["skills"]["hidden"] = True

    for name in ("profiles", "languages", "interests", "certifications", "volunteer", "references"):
        sections[name]["hidden"] = True

    return {
        "picture": {
            "hidden": True, "fit": "cover", "url": "", "size": 64,
            "rotation": 0, "aspectRatio": 1, "borderRadius": 0,
            "borderColor": "rgba(0, 0, 0, 0)", "borderWidth": 0,
            "shadowColor": "rgba(0, 0, 0, 0)", "shadowWidth": 0,
        },
        "basics": basics,
        "summary": summary,
        "sections": sections,
        "customSections": [],
    }
