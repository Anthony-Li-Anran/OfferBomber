"""Multilingual recruitment-email classification (Phase 5, section 5.1).

Deterministic first pass covers common zh/en terms. The host model can override
with a structured classification; unknown/ambiguous mail goes to the human
queue. Classification never alone changes an application's status - association
evidence is required (section 5.2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CATEGORIES = [
    "APPLICATION_RECEIVED", "INTERVIEW_INVITATION", "ASSESSMENT_INVITATION",
    "SCHEDULING_REQUEST", "ADDITIONAL_INFORMATION", "REJECTION", "OFFER",
    "FOLLOW_UP", "UNRELATED", "UNKNOWN",
]

# (compiled pattern, category). Ordered: more specific first.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\boffer\s+(letter|of employment)\b|\bwe(?:\'ve|\s+have)?\s+decided\s+to\s+offer\b|录用通知|正式录取|发放\s*offer", re.I), "OFFER"),
    (re.compile(r"\brejection\b|\bnot (?:selected|moving forward|successful)\b|we regret to inform|很遗憾|未通过|不匹配|不合适", re.I), "REJECTION"),
    (re.compile(r"\binterview\b|\bschedule\s+(?:an?\s+)?(?:call|meeting)\b|面试|面谈|视频面试|电话面试", re.I), "INTERVIEW_INVITATION"),
    (re.compile(r"\bassessment\b|\bonline test\b|\btake-home\b|\bcoding challenge\b|笔试|在线测评|测评邀请", re.I), "ASSESSMENT_INVITATION"),
    (re.compile(r"\b(reschedule|availability|confirm your time|scheduling)\b|改期|确认时间|协调时间", re.I), "SCHEDULING_REQUEST"),
    (re.compile(r"\b(additional information|please provide|documents? required|proof of)\b|请提供|补充材料|材料", re.I), "ADDITIONAL_INFORMATION"),
    (re.compile(r"\b(application received|we have received your application|thank you for applying)\b|已收到你的申请|申请已收到", re.I), "APPLICATION_RECEIVED"),
    (re.compile(r"\b(follow[- ]?up|checking in|status update)\b|跟进|进展", re.I), "FOLLOW_UP"),
]


@dataclass
class Classification:
    category: str
    confidence: float
    reason: str
    needs_human: bool

    def as_dict(self) -> dict:
        return {
            "category": self.category,
            "confidence": self.confidence,
            "reason": self.reason,
            "needs_human": self.needs_human,
        }


class EmailClassifier:
    def classify(self, subject: str, body: str) -> Classification:
        text = f"{subject}\n{body}"
        hits = [(pattern.search(text), category) for pattern, category in _PATTERNS]
        matched = [(m, c) for m, c in hits if m]
        if matched:
            match, category = matched[0]
            confidence = 0.82 if len(matched) == 1 else 0.68
            return Classification(category, confidence, f"matched: {match.group(0)[:60]}", False)
        # No keyword evidence: could still be a relevant mail in another language.
        return Classification("UNKNOWN", 0.0, "no deterministic signal; needs human/model", True)

    def classify_with_model(
        self, subject: str, body: str, model_category: str
    ) -> Classification:
        """Host model may propose a category; validate against the enum."""
        if model_category not in CATEGORIES:
            return self.classify(subject, body)
        if model_category == "UNKNOWN":
            return Classification("UNKNOWN", 0.0, "model could not determine", True)
        return Classification(model_category, 0.6, "host model classification", False)
