"""Adaptive routing: spend model calls only where they can change the outcome.

``routing_mode = fixed`` (default): every comment gets triage, and every
on-topic or generic comment gets generation, classification and quality.

``routing_mode = tiered``:
- tier 0, deterministic, no model call: an empty or emoji-only comment is generic
  engagement (a person decides; nothing is generated), and a comment with
  high-precision spam features (earnings phrase together with a messenger/phone or a
  link shortener) is spam. Tier 0 can only make the outcome more conservative.
- review-bound triage (generic engagement) in an automatic mode skips generation, since
  the central policy would route the draft to a person regardless of the reply.

The two-model invariant is untouched: anything that can reach AUTO_PUBLISH still
gets primary generation and secondary classification and review.
"""
import unicodedata

from app.services import input_analysis
from app.services.publishing import AUTOMATIC_MODES

REVIEW_BOUND = ("generic_engagement",)


def tier0(comment: str, analysis: input_analysis.InputAnalysis | None = None) -> dict | None:
    analysis = analysis or input_analysis.analyze(comment)
    visible = input_analysis.strip_invisible(comment).strip()
    if not any(unicodedata.category(c)[0] in "LN" for c in visible):
        return {"category": "generic_engagement", "confidence": 1.0, "routing": "tier0",
                "reason": "Empty or emoji-only comment (deterministic rule, no model call)."}
    features = analysis.spam_features
    if features.get("earnings_phrase") and (features.get("messenger_or_phone") or features.get("shortener")):
        return {"category": "spam_or_suspicious", "confidence": 1.0, "routing": "tier0",
                "reason": "Earnings offer with a contact or short link (deterministic rule)."}
    return None


def skip_generation(category: str | None, publishing_mode: str | None) -> bool:
    return category in REVIEW_BOUND and publishing_mode in AUTOMATIC_MODES
