"""Deterministic reply policy: rules R1–R11 over the saved reply text.

Levels, weakest first: pass < warn < review_required < blocked_pending_override.
``review_required`` and above block automatic publication (``reply_policy_mode =
enforce_auto``). In manual mode every violation is a warning shown to the person who
approves; a human may still publish through the existing confirm flow.

Matching runs on the input-signal detection view (``input_analysis``), so the same URL, contact
and invisible-character extractors serve input signals and output policy.
"""
import re
from dataclasses import asdict, dataclass

from app.services import input_analysis as ia

POLICY_VERSION = "reply-policy-v1"
LEVELS = ("pass", "warn", "review_required", "blocked_pending_override")
BLOCKING_LEVELS = {"review_required", "blocked_pending_override"}
MAX_REPLY_CHARS = 1500

COMMITMENT_RE = re.compile(
    r"(?i)\bguarantee[ds]?\b|\b100\s?%|\bpromise[ds]?\b"
    r"|\b(?:i'll|i will|we'll|we will|i'm going to|we're going to)\s+(?:\w+\s+)?"
    r"(?:make|upload|post|send|share|email|dm|release|cover|add|do|reply|give|bring)\b"
    r"|\bnext video\b|\btomorrow\b|\bby (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|tonight|next week|the end of|\d)"
    r"|\bpakka\b|\bguarantee hai\b|\bkal tak\b|\bagl[ae] video\b|\bzaroor bhej\w*"
    r"|\b\w+\s+(?:karenge|karunga|karungi|bhejunga|bhejenge|denge|dunga|dungi)\b"
    r"|गारंटी|पक्का|कल तक|अगले? वीडियो")
PROMO_TERMS_RE = re.compile(r"(?i)\b(?:off|discount|coupon|promo|code|offer|sale|chhoot|deal|cashback)\b")
AMOUNT_RE = re.compile(r"(?i)(?:₹|rs\.?\s?|\$|€|£|inr\s?|usd\s?)\d|\d+\s?%|\b\d+\s?(?:rupees|rupaye)\b")
# The code token itself is case-sensitive: an all-caps/digit coupon, not "Code: https".
PROMO_CODE_RE = re.compile(r"(?i:\bcode\b)[\s:]*((?=[A-Z0-9]*\d)[A-Z0-9]{4,}|[A-Z]{4,})\b")
AUTHORITY_RE = re.compile(
    r"(?i)\b(?:this (?:reply |comment )?(?:was|has been) (?:posted|approved|verified)"
    r"|as an? official|officially (?:approved|confirmed)|approved by (?:youtube|google|the team)"
    r"|on behalf of (?:youtube|google)|i am the (?:channel owner|creator) and i (?:approve|confirm))")


@dataclass(frozen=True)
class Violation:
    rule: str
    level: str
    span: tuple[int, int] | None = None
    detail: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def worst(violations) -> str:
    return max((v.level for v in violations), key=LEVELS.index, default="pass")


def blocking(violations) -> Violation | None:
    """The first violation that blocks automatic publication, by severity then rule."""
    candidates = [v for v in violations if v.level in BLOCKING_LEVELS]
    if not candidates:
        return None
    top = worst(candidates)
    return next(v for v in candidates if v.level == top)


def _allow(allowlist) -> dict:
    allowlist = allowlist or {}
    return {key: [str(v) for v in allowlist.get(key) or []]
            for key in ("domains", "url_prefixes", "contacts", "handles", "approved_statements")}


def _norm_url(url: str) -> str:
    bare = re.sub(r"(?i)^https?://", "", url).removeprefix("www.").rstrip("/")
    return bare.casefold()


def _url_rules(reply, comment, evidence, allow) -> list[Violation]:
    out = []
    allowed_hosts = {h.casefold().removeprefix("www.") for h in allow["domains"]}
    prefixes = [_norm_url(p) for p in allow["url_prefixes"]]
    comment_urls = {_norm_url(u["url"]) for u in ia.extract_urls(comment)}
    comment_hosts = {u["host"].removeprefix("www.") for u in ia.extract_urls(comment)}
    evidence_hosts = {u["host"].removeprefix("www.") for u in ia.extract_urls(evidence)}
    for url in ia.extract_urls(reply, allow_hosts=allowed_hosts | {p.split("/")[0] for p in prefixes}):
        host = url["host"].removeprefix("www.")
        normalized = _norm_url(url["url"])
        dangerous = {"shortener", "ip_literal", "punycode", "mixed_script", "non_ascii",
                     "confusable_with_allowlisted"} & set(url["flags"])
        if dangerous:
            out.append(Violation("R4", "blocked_pending_override", None,
                                 f"{host}:{','.join(sorted(dangerous))}"))
        elif normalized in comment_urls or (host in comment_hosts and host not in allowed_hosts):
            out.append(Violation("R4", "blocked_pending_override", None, f"{host}:copied_from_comment"))
        elif host in allowed_hosts or any(normalized.startswith(p) for p in prefixes):
            continue
        elif host in evidence_hosts:
            out.append(Violation("R4", "warn", None, f"{host}:in_evidence"))
        else:
            out.append(Violation("R4", "review_required", None, f"{host}:not_allowlisted"))
    return out


def _contact_rules(reply, comment, evidence, allow) -> list[Violation]:
    out = []
    in_comment = {c["value"] for c in ia.extract_contacts(comment)}
    in_evidence = {c["value"] for c in ia.extract_contacts(evidence)}
    approved = {ia.strip_invisible(c).casefold() for c in allow["contacts"]}
    approved |= {re.sub(r"\D", "", c)[-10:] for c in allow["contacts"] if re.sub(r"\D", "", c)}
    for contact in ia.extract_contacts(reply):
        value, kind = contact["value"], contact["kind"]
        if value in approved:
            continue  # R3: exact allow-listed creator contact
        if value in in_comment:
            out.append(Violation("R1", "blocked_pending_override", None, f"{kind}:echoed_from_comment"))
        elif value in in_evidence:
            out.append(Violation("R2", "warn", None, f"{kind}:in_evidence"))
        else:
            out.append(Violation("R2", "review_required", None, f"{kind}:not_allowlisted"))
    return out


def _handle_rules(reply, allow, commenter_handle, channel_handle) -> list[Violation]:
    permitted = {h.casefold().lstrip("@") for h in allow["handles"]}
    permitted |= {h.casefold().lstrip("@") for h in (commenter_handle, channel_handle) if h}
    # Mentions inside code spans (decorators such as `@property`, `@app.get(`) are code.
    text = ia.CODE_SPAN_RE.sub(lambda m: " " * len(m.group()), ia.strip_invisible(reply))
    out = []
    for match in ia.HANDLE_RE.finditer(text):
        following = text[match.end():match.end() + 1]
        if following == "(":
            continue
        name = match.group(1).casefold().rstrip(".")
        if name not in permitted:
            out.append(Violation("R5", "review_required", match.span(), f"@{name}"))
    return out


def check(reply: str, *, comment: str = "", evidence: str = "", allowlist=None,
          commenter_handle: str | None = None, channel_handle: str | None = None,
          canaries=()) -> list[Violation]:
    allow = _allow(allowlist)
    view = ia.detection_view(reply)
    violations = []
    violations += _contact_rules(reply, comment, evidence or "", allow)
    violations += _url_rules(reply, comment, evidence or "", allow)
    violations += _handle_rules(reply, allow, commenter_handle, channel_handle)

    approved = [ia.detection_view(s) for s in allow["approved_statements"]]
    for match in COMMITMENT_RE.finditer(view):
        if not any(match.group().strip() in statement for statement in approved):
            violations.append(Violation("R6", "review_required", match.span(), match.group().strip()))
            break
    if (AMOUNT_RE.search(view) and PROMO_TERMS_RE.search(view)) or PROMO_CODE_RE.search(
            ia.strip_invisible(reply)):
        violations.append(Violation("R7", "review_required", None, "promotion_or_money"))
    if AUTHORITY_RE.search(view):
        violations.append(Violation("R8", "review_required", None, "state_or_authority_claim"))
    hidden = [s for s in ia.analyze(reply).signals if s.risk == "high" and s.kind != "confusable_url_host"]
    if hidden:
        violations.append(Violation("R9", "blocked_pending_override", None, hidden[0].kind))
    if len(reply) > MAX_REPLY_CHARS:
        violations.append(Violation("R10", "warn", None, f"{len(reply)} characters"))
    for canary in canaries:
        if canary and canary in reply:
            violations.append(Violation("R11", "blocked_pending_override", None, "evaluation_canary"))
    return violations
