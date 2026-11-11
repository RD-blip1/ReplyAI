"""Deterministic guard on the generated draft text for cases the policy already holds.

The automation policy (``automation_policy.decide``) keeps creator-only questions,
sarcasm and dictated replies away from automatic publication. The draft itself could
still be misleading for the person who reviews it: an invented answer about the
creator's setup, an enthusiastic reply to a sarcastic complaint, or text the viewer
dictated. This guard replaces such a draft with a short holding reply in the creator's
voice and records why, using only signals that already exist (triage, the two
classifications, the deterministic input analysis). It makes no model call.

It never changes the routing inputs: triage category, classifications and input signals
stay as the models and detectors produced them, so ``decide()`` reaches the same verdict
and a guarded draft is never more publishable than the original.
"""
from dataclasses import dataclass

from app.config import settings
from app.services import input_analysis, reply_policy

HOLDING_REPLIES = {
    "creator_only": {
        "en": "Good question! I want to give you the exact details rather than guess, "
              "so I'll check and reply properly.",
        "hi": "अच्छा सवाल! अंदाज़ा लगाने के बजाय सही जानकारी देखकर ही जवाब दिया जाएगा।",
        "hinglish": "Accha sawaal! Guess karne ke bajaye sahi details check karke reply karte hain.",
    },
    "sarcasm": {
        "en": "Sorry to hear this didn't work out the way you expected. "
              "Could you share a bit more about what happened?",
        "hi": "यह सुनकर खेद है कि यह उम्मीद के मुताबिक नहीं रहा। क्या आप थोड़ा और बता सकते हैं कि क्या हुआ?",
        "hinglish": "Sorry ki ye expect ki tarah kaam nahi kiya. Thoda aur bata sakte ho kya hua?",
    },
    "dictated_reply": {
        "en": "Thanks for the comment!",
        "hi": "टिप्पणी के लिए धन्यवाद!",
        "hinglish": "Comment ke liye thanks!",
    },
}

REASONS = {
    "creator_only": "The question needs facts only the creator has ({missing}), so the "
                    "model's answer was replaced with a holding reply instead of a guess.",
    "sarcasm": "The comment reads as sarcastic while the generated reply took it literally, "
               "so it was replaced with a neutral reply that asks what went wrong.",
    "dictated_reply": "The generated reply repeated wording the commenter dictated, so it "
                      "was replaced with a neutral reply.",
}


@dataclass(frozen=True)
class Guard:
    kind: str
    language: str
    text: str
    reason: str

    def record(self, model_reply: str) -> dict:
        return {"kind": self.kind, "language": self.language, "reason": self.reason,
                "model_reply": (model_reply or "")[:500]}


def language_of(triage: dict | None, comment: str) -> str:
    claimed = ((triage or {}).get("language") or {}).get("primary")
    if claimed in ("hi", "hinglish"):
        return claimed
    profile = input_analysis.analyze(comment).script_profile
    return "hi" if profile == "Deva" else "en"


def sarcasm_flagged(primary: dict | None, secondary: dict | None, cues: list[str]) -> bool:
    """The same sarcasm conditions ``decide()`` routes on (classifiers first, then cues)."""
    classifications = [c or {} for c in (primary, secondary)]
    for c in classifications:
        if c.get("sarcasm") in ("yes", "uncertain"):
            return True
        if c.get("intended_sentiment") == "negative" and c.get("sentiment") in ("positive", "neutral", "mixed"):
            return True
    return bool(settings.sarcasm_routing == "model_and_cues" and cues
                and not any(c.get("sarcasm") == "no" for c in classifications))


def _generator_read_it_negatively(primary: dict | None) -> bool:
    primary = primary or {}
    return (primary.get("sentiment") == "negative" or primary.get("sarcasm") == "yes"
            or primary.get("intended_sentiment") == "negative")


def assess(*, comment: str, reply: str, triage: dict | None,
           primary: dict | None, secondary: dict | None) -> Guard | None:
    """The guard for this draft, or None when the generated text stands."""
    # No triage means no generation happened on the normal paths; leave such drafts alone so
    # the guard can never turn a missing triage into a present one.
    if not settings.reply_guard or not reply or not triage:
        return None
    triage = triage or {}
    language = language_of(triage, comment)

    def guard(kind: str, **fields) -> Guard:
        return Guard(kind, language, HOLDING_REPLIES[kind][language], REASONS[kind].format(**fields))

    # Same condition as decide(): answerability routing "always", or a grounded variant.
    grounded = settings.grounding_variant != "v0" or settings.answerability_routing == "always"
    if grounded and triage.get("answer_source") == "creator_only":
        return guard("creator_only", missing=(triage.get("missing_information") or "creator details")[:80])
    if reply_policy.dictated_echo(reply, comment):
        return guard("dictated_reply")
    cues = ((triage.get("input_signals") or {}).get("sarcasm_cues")
            or input_analysis.analyze(comment).sarcasm_cues)
    # Only when the generator itself took the comment literally: a reply written by a
    # generator that understood the complaint is kept for the person to judge.
    if sarcasm_flagged(primary, secondary, cues) and not _generator_read_it_negatively(primary):
        return guard("sarcasm")
    return None


def apply(*, comment: str, reply: str, triage: dict | None,
          primary: dict | None, secondary: dict | None) -> tuple[str, dict | None]:
    """(text to save, triage to save). Triage gains a ``reply_guard`` record when applied."""
    found = assess(comment=comment, reply=reply, triage=triage, primary=primary, secondary=secondary)
    if found is None:
        return reply, triage
    return found.text, {**(triage or {}), "reply_guard": found.record(reply)}
