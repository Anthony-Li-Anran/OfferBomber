"""Model-driven resume parser (task book Part C).

Keeps DeterministicResumeParser as the fallback. The model parser never writes
to the knowledge base: it produces CandidateProfile suggestions, with every
fact carrying a locator into the original PDF text. Fabricated quantifiable
claims are flagged and dropped from candidates.
"""

from __future__ import annotations

import re
from typing import Any

from ..models.base import ModelError, ModelProvider, TaskType
from .pdf import PDFDocument
from .schema import CandidateProfile

# Claims that must be traceable to source text before being accepted.
_QUANT = re.compile(r"\b\d+(?:\.\d+)?\s*(?:%|x\b|倍|万|亿|人|个|ms\b|s\b|\$|元|K\b|M\b)", re.I)
_ACCEPTED = re.compile(r"(已录用|accepted|published|录用通知|offer)", re.I)
_SUBMITTED = re.compile(r"(已投稿|投稿|submitted|under review|在审)", re.I)


def text_quality(document: PDFDocument) -> tuple[bool, list[str]]:
    """Check whether extracted text is good enough to send to a model."""
    warnings: list[str] = []
    if not document.extractable:
        warnings.append("PDF text is not extractable (scanned image?); OCR not available")
        return False, warnings
    size = len(document.text.strip())
    if size < 80:
        warnings.append(f"very little text extracted ({size} chars); results may be unreliable")
        return False, warnings
    return True, warnings


def _evidence_ok(candidate_text: str, source_text: str) -> bool:
    """A fact's distinctive words must appear in the source."""
    words = [w for w in re.findall(r"[\w\u3400-\u9fff]{3,}", candidate_text.lower()) if w]
    if not words:
        return False
    haystack = source_text.lower()
    return sum(1 for w in words if w in haystack) / len(words) >= 0.6


class ModelResumeParser:
    def __init__(self, provider: ModelProvider) -> None:
        self.provider = provider

    async def parse(self, document: PDFDocument) -> CandidateProfile:
        usable, warnings = text_quality(document)
        if not usable:
            return CandidateProfile(
                warnings=warnings,
                parser=f"model:{self.provider.name}:unusable_text",
            )

        numbered = "\n".join(
            f"[p{index + 1}] {page}" for index, page in enumerate(document.pages)
        )
        prompt = (
            "Extract resume content as structured JSON. Rules:\n"
            "- Copy names, dates, organizations and quantification VERBATIM from the text.\n"
            "- Never invent education, internships, paper acceptance, awards or metrics.\n"
            "- Distinguish 'submitted/under review' from 'accepted'.\n"
            "- Every experience/fact needs a locator like 'p1'.\n"
            "- If a field is missing, leave it null; do not guess.\n"
            "- Support Chinese, English and mixed resumes.\n"
            f"Resume text:\n{numbered}"
        )
        try:
            result = await self.provider.generate_structured(
                TaskType.RESUME_EXTRACTION,
                [{"role": "user", "content": prompt}],
                CandidateProfile,
            )
        except ModelError as exc:
            return CandidateProfile(
                warnings=warnings + [f"model extraction failed ({exc.kind.value}); use deterministic fallback"],
                parser=f"model:{self.provider.name}:failed",
            )

        result.parser = f"model:{self.provider.name}:v1"
        result.warnings = warnings + result.warnings
        self._guard_truthfulness(result, document.text)
        return result

    @staticmethod
    def _guard_truthfulness(candidate: CandidateProfile, source_text: str) -> None:
        for experience in candidate.experiences:
            kept = []
            for fact in experience.facts:
                if fact.locator and "p" not in str(fact.locator):
                    candidate.warnings.append(f"fact has invalid locator: {fact.locator}")
                if _QUANT.search(fact.content) and not _evidence_ok(fact.content, source_text):
                    candidate.warnings.append(
                        f"fact dropped: unverifiable quantitative claim: {fact.content[:80]}"
                    )
                    continue
                if _ACCEPTED.search(fact.content) and _SUBMITTED.search(source_text) and not _ACCEPTED.search(source_text):
                    candidate.warnings.append(
                        f"fact dropped: source says submitted but model claimed accepted: {fact.content[:80]}"
                    )
                    continue
                kept.append(fact)
            experience.facts = kept
