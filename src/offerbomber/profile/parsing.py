"""Deterministic resume structure recognition.

This is NOT a language model. It only detects headings, contact strings and
verbatim lines. Every emitted candidate keeps its page/line locator and a low
confidence so an operator can decide what becomes a confirmed fact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from .pdf import PDFDocument
from .schema import (
    CandidateEducation,
    CandidateExperience,
    CandidateFact,
    CandidateProfile,
)

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?:\+?86[-\s]?)?1[3-9]\d{9}(?:/?\d{3,4})?")
_DATE = re.compile(r"(?:19|20)\d{2}(?:[.\-/年]\s?(?:0?[1-9]|1[0-2]|\d{1,2}\s*月?))?")
_TECH = re.compile(
    r"\b(?:Python|C\+\+|Java|Go|Rust|SQL|Spark|Kafka|PyTorch|TensorFlow|Linux|Docker|"
    r"Kubernetes|Redis|Git|Scala|R|MATLAB|Excel|Power\s*BI|Tableau)\b",
    re.IGNORECASE,
)
_HEADINGS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("EDUCATION", ("教育", "学历", "education")),
    ("INTERNSHIP", ("实习", "intern")),
    ("WORK", ("工作经", "工作经歴", "工作经历", "work experience", "employment")),
    ("RESEARCH", ("科研", "研究经", "research")),
    ("PROJECT", ("项目经", "项目", "project")),
    ("PAPER", ("论文", "publication", "paper")),
    ("COMPETITION", ("竞赛", "competition")),
    ("AWARD", ("奖项", "荣誉", "award")),
    ("OPEN_SOURCE", ("开源", "open source")),
    ("SKILL", ("技能", "skill")),
    ("SUMMARY", ("个人简介", "自我介绍", "summary", "profile")),
)


@dataclass(frozen=True)
class _Line:
    text: str
    locator: str


class ResumeParser(Protocol):
    """Future model-backed parsers can implement this same interface."""

    def parse(self, document: PDFDocument) -> CandidateProfile: ...


class DeterministicResumeParser:
    def parse(self, document: PDFDocument) -> CandidateProfile:
        lines = self._lines(document)
        sections = self._sections(lines)
        header = sections.get("HEADER", [])
        profile = CandidateProfile()
        header_text = " ".join(line.text for line in header)

        email = _EMAIL.search(header_text)
        phone = _PHONE.search(header_text)
        profile.email = email.group(0) if email else None
        profile.phone = phone.group(0) if phone else None
        profile.contact = " | ".join(
            value for value in (profile.email, profile.phone) if value
        ) or None
        profile.display_name = self._name(header)
        for line in header:
            if "求职意向" in line.text or "目标岗位" in line.text:
                profile.target_position = line.text.split(":", 1)[-1].split("：", 1)[-1].strip()

        profile.summary = self._join(sections.get("SUMMARY", []))
        profile.educations = self._educations(sections.get("EDUCATION", []))
        for section, lines_in_section in sections.items():
            if section in {"HEADER", "EDUCATION", "SUMMARY", "SKILL"}:
                continue
            kind = section if section in {
                "INTERNSHIP", "WORK", "RESEARCH", "PROJECT", "PAPER",
                "COMPETITION", "AWARD", "OPEN_SOURCE",
            } else "CUSTOM"
            profile.experiences.extend(
                self._experiences(kind, lines_in_section)
            )

        skill_lines = sections.get("SKILL", [])
        if skill_lines:
            skills = self._skills(" ".join(line.text for line in skill_lines))
            profile.experiences.append(
                CandidateExperience(
                    type="CUSTOM",
                    title="技能清单",
                    description=self._join(skill_lines),
                    facts=[
                        CandidateFact(
                            content="技能： " + "、".join(skills),
                            category="SKILL",
                            skills=skills,
                            confidence=0.5,
                            locator=skill_lines[0].locator,
                        )
                    ],
                    confidence=0.5,
                    locator=skill_lines[0].locator,
                )
            )

        if not document.extractable:
            profile.warnings.append("PDF text layer is missing or too short; OCR is not available.")
        if not profile.display_name:
            profile.warnings.append("Name could not be identified reliably; left unknown.")
        if not profile.experiences:
            profile.warnings.append("No structured experience sections were recognized.")
        profile.warnings.append(
            "Deterministic parsing only: all candidates require manual review before confirmation."
        )
        return profile

    @staticmethod
    def _lines(document: PDFDocument) -> list[_Line]:
        result: list[_Line] = []
        for page_no, page in enumerate(document.pages, start=1):
            for line_no, raw in enumerate(page.splitlines(), start=1):
                text = raw.strip()
                if text:
                    result.append(_Line(text, f"p{page_no}:L{line_no}"))
        return result

    @staticmethod
    def _join(lines: list[_Line]) -> str | None:
        text = " ".join(line.text for line in lines).strip()
        return text or None

    @staticmethod
    def _name(header: list[_Line]) -> str | None:
        for line in header[:3]:
            text = line.text
            if 2 <= len(text) <= 24 and not _EMAIL.search(text) and not _PHONE.search(text):
                if not any(ch.isdigit() for ch in text) and "|" not in text and "@" not in text:
                    return text
        return None

    @staticmethod
    def _sections(lines: list[_Line]) -> dict[str, list[_Line]]:
        sections: dict[str, list[_Line]] = {"HEADER": []}
        current = "HEADER"
        for line in lines:
            matched = DeterministicResumeParser._heading(line.text)
            if matched:
                current = matched
                sections.setdefault(current, [])
                continue
            sections.setdefault(current, []).append(line)
        return sections

    @staticmethod
    def _heading(text: str) -> str | None:
        candidate = text.strip("：: ").lower()
        if not candidate or len(candidate) > 24 or "。" in candidate:
            return None
        for name, keywords in _HEADINGS:
            if any(keyword.lower() in candidate for keyword in keywords):
                return name
        return None

    @staticmethod
    def _skills(text: str) -> list[str]:
        parts = [part.strip() for part in re.split(r"[,，、;；/|]", text) if part.strip()]
        found = {match.group(0) for match in _TECH.finditer(text)}
        for part in parts:
            if 1 < len(part) <= 20 and not _EMAIL.search(part):
                found.add(part)
        return sorted(found)[:30]

    @staticmethod
    def _educations(lines: list[_Line]) -> list[CandidateEducation]:
        if not lines:
            return []
        groups: list[list[_Line]] = []
        current: list[_Line] = []
        for line in lines:
            if current and _DATE.search(line.text) and "大学" in current[-1].text:
                groups.append(current)
                current = []
            current.append(line)
        if current:
            groups.append(current)

        results: list[CandidateEducation] = []
        for group in groups:
            text = " ".join(line.text for line in group)
            dates = _DATE.findall(text)
            degree = next(
                (
                    word
                    for word in ("博士", "硕士", "本科", "PhD", "Master", "Bachelor")
                    if word.lower() in text.lower()
                ),
                None,
            )
            school_line = next(
                (line.text for line in group if "大学" in line.text or "University" in line.text),
                None,
            )
            if school_line:
                for word in (" Master", " Bachelor", " PhD", " 硕士", " 本科", " 博士"):
                    if word in school_line:
                        school_line = school_line.split(word, 1)[0]
                        break
            results.append(
                CandidateEducation(
                    school=school_line,
                    degree=degree,
                    major=text.split("专业", 1)[1][:40].strip() if "专业" in text else None,
                    start_date=dates[0] if dates else None,
                    end_date=dates[1] if len(dates) > 1 else None,
                    description=" ".join(line.text for line in group[1:]) or None,
                    confidence=0.45,
                    locator=group[0].locator,
                )
            )
        return results

    @staticmethod
    def _experiences(kind: str, lines: list[_Line]) -> list[CandidateExperience]:
        groups: list[list[_Line]] = []
        current: list[_Line] = []
        for line in lines:
            is_new = bool(current) and (
                line.text.startswith(("-", "•", "*", "·"))
                or bool(_DATE.search(line.text))
            )
            if is_new:
                groups.append(current)
                current = []
            current.append(line)
        if current:
            groups.append(current)

        results: list[CandidateExperience] = []
        for group in groups:
            title = re.sub(r"^[-•*\s]+", "", group[0].text).strip()
            if not title:
                continue
            fact_lines = []
            for line in group[1:]:
                text = re.sub(r"^[-•*\s]+", "", line.text).strip()
                if text:
                    fact_lines.append((text, line))
            details = [text for text, _ in fact_lines]
            dates = _DATE.findall(group[0].text)
            facts = [
                CandidateFact(
                    content=text,
                    category=kind,
                    skills=sorted(set(match.group(0) for match in _TECH.finditer(text))),
                    confidence=0.4,
                    locator=line.locator,
                )
                for text, line in fact_lines
            ]
            if not facts:
                facts = [
                    CandidateFact(
                        content=title,
                        category=kind,
                        skills=sorted(set(match.group(0) for match in _TECH.finditer(title))),
                        confidence=0.4,
                        locator=group[0].locator,
                    )
                ]
            results.append(
                CandidateExperience(
                    type=kind,
                    title=title[:300],
                    description=" ".join(details) or None,
                    start_date=dates[0] if dates else None,
                    end_date=dates[1] if len(dates) > 1 else None,
                    facts=facts,
                    confidence=0.4,
                    locator=group[0].locator,
                )
            )
        return results
