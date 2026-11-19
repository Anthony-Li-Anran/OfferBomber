"""Map OfferBomber ResumePlan + knowledge base into Reactive Resume ResumeData.

Schema fields follow the official OpenAPI spec (rxresu.me /api/openapi/spec.json,
verified 2026-11-19): `basics`, `sections.{experience,education,projects,...}`,
`customSections`, plus the current `metadata`/`design` preserved from the cloned
source document. We never fabricate a design/template — the caller supplies the
source resume's document and we only replace content fields.
"""

from __future__ import annotations

import html
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


def _dates(start: str | None, end: str | None) -> dict[str, Any]:
    return {
        "start": start or None,
        "end": end or None,
        "present": False,
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
    if "@" in contact:
        basics["email"] = contact.strip()
    elif any(ch.isdigit() for ch in contact):
        basics["phone"] = contact.strip()

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
        target_section = "projects"
        if experience:
            if experience.type in {"WORK", "INTERNSHIP"}:
                target_section = "experience"
            elif experience.type in {"PAPER", "RESEARCH"}:
                target_section = "publications"
            elif experience.type in {"COMPETITION", "AWARD"}:
                target_section = "awards"
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

    skills = sorted(
        {skill for section in plan.sections for item in section.items for skill in item.skills}
        | set(plan.highlight_skills)
    )
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
