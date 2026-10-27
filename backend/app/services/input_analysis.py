"""Deterministic input signals. Observe-only by default; never changes model input.

Unicode canonicalisation does not stop prompt injection. These detectors remove
*concealment*: they report characters and payloads a person would not see but a model
would read. The original text is never rewritten for storage or for providers;
``detection_view`` exists only so detectors see through folding tricks.

High-risk signals (the only ones a routing mode may act on): tag characters outside an
emoji tag sequence, bidi controls, invisible characters inside a Latin word, and URL
hosts that are punycode, mixed-script or confusable with an allow-listed host.
"""
import base64
import binascii
import ipaddress
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit

ZWJ, ZWNJ = "\u200d", "\u200c"
VARIATION_SELECTORS = {"\ufe0e", "\ufe0f"}
BIDI_CONTROLS = set("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
DIRECTION_MARKS = {"\u200e", "\u200f"}
TAG_RANGE = range(0xE0020, 0xE007F)  # tag letters; CANCEL TAG (E007F) closes
BLACK_FLAG, CANCEL_TAG = "\U0001f3f4", "\U000e007f"
# Indic viramas: a ZWJ/ZWNJ after one of these shapes a conjunct legitimately.
VIRAMAS = {"्", "্", "੍", "્", "୍", "்", "్", "್", "്"}

# A vendored subset of Unicode confusables.txt (Cyrillic/Greek → Latin). The full file is
# not available offline; this subset covers the lowercase look-alikes used in practice.
CONFUSABLES = {
    "а": "a", "с": "c", "е": "e", "о": "o", "р": "p", "х": "x", "у": "y", "к": "k",
    "м": "m", "т": "t", "в": "b", "н": "h", "і": "i", "ј": "j", "ѕ": "s", "ԁ": "d",
    "ԛ": "q", "ԝ": "w", "һ": "h", "ӏ": "l", "α": "a", "ο": "o", "ρ": "p", "ν": "v",
    "τ": "t", "ι": "i", "κ": "k", "χ": "x", "υ": "u", "ε": "e", "0": "o", "1": "l",
}
CONFUSABLE_SUBSET = "vendored-cyrillic-greek-subset-v1"

SHORTENERS = {"bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "is.gd", "buff.ly",
              "cutt.ly", "rb.gy", "shorturl.at", "tiny.cc", "t.ly", "rebrand.ly"}

INJECTION_PHRASES = (
    "ignore previous instructions", "ignore all instructions", "ignore the above",
    "ignore all previous", "disregard previous", "system prompt", "you are now",
    "reviewer:", "new instructions:", "developer mode", "jailbreak",
    "pichle instructions ignore", "sab instructions ignore", "instructions ignore karo",
    "सभी निर्देश अनदेखा", "पिछले निर्देश", "निर्देशों को अनदेखा",
)

URL_RE = re.compile(
    r"(?i)\b(?:https?://[^\s<>\"'`]+|(?:www\.)?[\w\- -￿]+(?:\.[\w\- -￿]+)*"
    r"\.(?:com|org|net|io|dev|in|co|ly|gl|gd|me|app|xyz|info|biz|at|cc|us|tv|be|ai|edu|gov)"
    r"(?:/[^\s<>\"'`]*)?)")
EMAIL_RE = re.compile(r"(?i)\b[\w.+\-]+@[a-z0-9\-]+(?:\.[a-z0-9\-]+)+\b")
# UPI: a handle with no dot after '@' (distinguishes it from an email address).
UPI_RE = re.compile(r"(?i)(?<![\w.@])[\w.\-]{2,256}@[a-z]{2,64}\b(?!\.[a-z0-9])")
PAN_RE = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
# Phone fallback (phonenumbers unavailable offline): Indian mobiles and E.164 forms.
PHONE_RE = re.compile(r"(?<![\w+])(?:\+91[\s\-]?|0)?[6-9]\d{4}[\s\-]?\d{5}(?!\d)"
                      r"|(?<![\w+])\+(?!91)[1-9](?:[\s\-]?\d){7,14}(?!\d)")
AADHAAR_RE = re.compile(r"(?<!\d)(\d{4})([\s\-]?)(\d{4})\2(\d{4})(?!\d)")
AADHAAR_CONTEXT = ("aadhaar", "aadhar", "आधार", "uid")
HANDLE_RE = re.compile(r"(?<![\w@.])@([A-Za-z0-9._\-]{3,30})")
CODE_SPAN_RE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)
BASE64_RE = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{24,}={0,2}(?![A-Za-z0-9+/=])")
HEX_RE = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{32,}(?![0-9A-Fa-f])")
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
EARNINGS_RE = re.compile(r"(?i)(?:₹|rs\.?|\$|inr|usd)\s?\d[\d,]*(?:k)?\s*(?:/|per)\s*(?:day|week|month|din|hafte)"
                         r"|earn\s+(?:₹|rs\.?|\$)?\d|work from home|paise kamao|kamaye|crypto signals?")
MESSENGER_RE = re.compile(r"(?i)\b(?:whatsapp|telegram|wa\.me|t\.me|dm me|inbox me)\b")
REPEAT_RE = re.compile(r"(.)\1{5,}")

# Verhoeff tables (dihedral group D5) for Aadhaar-like checksums.
_D = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
      [3, 4, 0, 1, 2, 8, 9, 5, 6, 7], [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
      [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3], [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
      [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]]
_P = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
      [8, 9, 1, 6, 0, 4, 3, 5, 2, 7], [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
      [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8]]


def verhoeff_valid(digits: str) -> bool:
    check = 0
    for position, digit in enumerate(reversed(digits)):
        check = _D[check][_P[position % 8][int(digit)]]
    return check == 0


@dataclass(frozen=True)
class Signal:
    kind: str
    risk: str  # "low" | "high"
    index: int
    detail: str = ""


@dataclass
class InputAnalysis:
    signals: list[Signal] = field(default_factory=list)
    script_profile: str = "none"
    urls: list[dict] = field(default_factory=list)
    contacts: list[dict] = field(default_factory=list)
    encoded_runs: list[dict] = field(default_factory=list)
    injection_phrases: list[str] = field(default_factory=list)
    spam_features: dict = field(default_factory=dict)

    @property
    def high_risk(self) -> bool:
        return any(signal.risk == "high" for signal in self.signals)

    def as_dict(self) -> dict:
        data = asdict(self)
        data["high_risk"] = self.high_risk
        data["high_risk_kinds"] = sorted({s.kind for s in self.signals if s.risk == "high"})
        data["confusable_table"] = CONFUSABLE_SUBSET
        return data


def is_emoji(char: str | None) -> bool:
    if not char:
        return False
    code = ord(char)
    return (0x1F000 <= code <= 0x1FAFF or 0x2600 <= code <= 0x27BF or 0x2B00 <= code <= 0x2BFF
            or 0x1F3FB <= code <= 0x1F3FF or code in (0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122))


def is_latin_letter(char: str | None) -> bool:
    return bool(char) and char.isalpha() and "LATIN" in unicodedata.name(char, "")


def invisible(char: str) -> bool:
    return (unicodedata.category(char) == "Cf" or char in VARIATION_SELECTORS
            or ord(char) in TAG_RANGE or char == CANCEL_TAG)


def strip_invisible(text: str) -> str:
    return "".join(c for c in text if not invisible(c))


def detection_view(text: str) -> str:
    """Folded copy for detectors only. Lossy (NFKC); never stored or sent."""
    return unicodedata.normalize("NFKC", strip_invisible(text)).casefold()


def skeleton(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    return "".join(CONFUSABLES.get(c, c) for c in folded)


def script_of(char: str) -> str | None:
    if not char.isalpha():
        return None
    name = unicodedata.name(char, "")
    for script in ("LATIN", "DEVANAGARI", "CYRILLIC", "GREEK"):
        if name.startswith(script):
            return script
    return "OTHER"


def script_profile(text: str) -> str:
    scripts = {s for s in map(script_of, text) if s}
    if not scripts:
        return "none"
    if scripts == {"LATIN"}:
        return "Latn"
    if scripts == {"DEVANAGARI"}:
        return "Deva"
    if scripts <= {"LATIN", "DEVANAGARI"}:
        return "mixed"
    return "other"


def _character_signals(text: str) -> list[Signal]:
    signals = []
    index = 0
    while index < len(text):
        char = text[index]
        before = text[index - 1] if index else None
        after = text[index + 1] if index + 1 < len(text) else None
        code = ord(char)
        if code in TAG_RANGE or char == CANCEL_TAG:
            # Legitimate only as BLACK FLAG + tag letters + CANCEL TAG (subdivision flags).
            start = index
            while index < len(text) and ord(text[index]) in TAG_RANGE:
                index += 1
            closed = index < len(text) and text[index] == CANCEL_TAG
            if not (closed and start > 0 and text[start - 1] == BLACK_FLAG):
                hidden = "".join(chr(ord(c) - 0xE0000) for c in text[start:index]
                                 if 0xE0020 <= ord(c) <= 0xE007E)
                signals.append(Signal("tag_characters", "high", start, hidden[:80]))
            index += 1
            continue
        if char in BIDI_CONTROLS:
            signals.append(Signal("bidi_control", "high", index, f"U+{code:04X}"))
        elif char in DIRECTION_MARKS:
            signals.append(Signal("direction_mark", "low", index, f"U+{code:04X}"))
        elif char == ZWJ:
            if not ((is_emoji(before) or before in VARIATION_SELECTORS) and is_emoji(after)) \
                    and before not in VIRAMAS:
                signals.append(_in_word(before, after, index, "zwj"))
        elif char == ZWNJ:
            if before not in VIRAMAS:
                signals.append(_in_word(before, after, index, "zwnj"))
        elif char in VARIATION_SELECTORS:
            if not (is_emoji(before) or (before and (before in "#*0123456789"
                                                    or 0x2000 <= ord(before) <= 0x2BFF))):
                signals.append(Signal("stray_variation_selector", "low", index, f"U+{code:04X}"))
        elif unicodedata.category(char) == "Cf":
            signals.append(_in_word(before, after, index, f"format_U+{code:04X}"))
        index += 1
    return signals


def _in_word(before, after, index, detail) -> Signal:
    if is_latin_letter(before) and is_latin_letter(after):
        return Signal("invisible_in_word", "high", index, detail)
    return Signal("invisible_character", "low", index, detail)


def _mixed_script_tokens(text: str) -> list[Signal]:
    signals = []
    for match in re.finditer(r"\w+", strip_invisible(text)):
        scripts = {s for s in map(script_of, match.group()) if s}
        if "LATIN" in scripts and scripts & {"CYRILLIC", "GREEK"}:
            signals.append(Signal("mixed_script_token", "low", match.start(), match.group()[:40]))
    return signals


def host_of(url: str) -> str:
    target = url if "://" in url else f"http://{url}"
    try:
        return (urlsplit(target).hostname or "").rstrip(".")
    except ValueError:
        return ""


def classify_host(host: str, allow_hosts=()) -> list[str]:
    """Host-level concerns; empty list means an ordinary host."""
    flags = []
    if not host:
        return ["unparseable"]
    try:
        ipaddress.ip_address(host)
        flags.append("ip_literal")
    except ValueError:
        pass
    labels = host.split(".")
    if any(label.startswith("xn--") for label in labels):
        flags.append("punycode")
    if any(ord(c) > 127 for c in host):
        flags.append("non_ascii")
        if any(len({s for s in map(script_of, label) if s}) > 1 for label in labels):
            flags.append("mixed_script")
    if host.removeprefix("www.") in SHORTENERS:
        flags.append("shortener")
    allowed = {h.casefold().removeprefix("www.") for h in allow_hosts}
    bare = host.casefold().removeprefix("www.")
    if bare not in allowed and skeleton(bare) in {skeleton(h) for h in allowed}:
        flags.append("confusable_with_allowlisted")
    return flags


def extract_urls(text: str, allow_hosts=()) -> list[dict]:
    clean = strip_invisible(text)
    emails = [m.span() for m in EMAIL_RE.finditer(clean)]
    urls = []
    for match in URL_RE.finditer(clean):
        raw = match.group().rstrip(".,;:!?)]}")
        start, end = match.span()
        if any(a < end and start < b for a, b in emails) or "@" in raw.split("/")[0]:
            continue  # the domain part of an email address is not a link
        host = host_of(raw).casefold()
        urls.append({"url": raw, "host": host, "flags": classify_host(host, allow_hosts)})
    return urls


def extract_contacts(text: str) -> list[dict]:
    clean = strip_invisible(text)
    found, taken = [], []

    def add(kind, match, value=None):
        span = match.span()
        if any(a < span[1] and span[0] < b for a, b in taken):
            return
        taken.append(span)
        found.append({"kind": kind, "value": value or match.group(), "start": span[0]})

    for match in EMAIL_RE.finditer(clean):
        add("email", match, match.group().casefold())
    for match in UPI_RE.finditer(clean):
        add("upi", match, match.group().casefold())
    for match in PAN_RE.finditer(clean):
        add("pan", match)
    lowered = clean.casefold()
    for match in AADHAAR_RE.finditer(clean):
        digits = "".join(match.group(1, 3, 4))
        grouped = bool(match.group(2))
        context = any(word in lowered for word in AADHAAR_CONTEXT)
        if digits[0] not in "01" and verhoeff_valid(digits) and (grouped or context):
            add("aadhaar", match, digits)
    for match in PHONE_RE.finditer(clean):
        add("phone", match, re.sub(r"\D", "", match.group())[-10:])
    return found


def _encoded_runs(text: str) -> list[dict]:
    runs = []
    for match in BASE64_RE.finditer(text):
        token = match.group()
        try:
            decoded = base64.b64decode(token + "=" * (-len(token) % 4), validate=True)
        except (binascii.Error, ValueError):
            continue
        runs.extend(_printable_run("base64", token, decoded))
    for match in HEX_RE.finditer(text):
        token = match.group()
        if len(token) in (40, 64) or UUID_RE.search(text[max(0, match.start() - 9):match.end() + 1]):
            continue
        try:
            decoded = bytes.fromhex(token if len(token) % 2 == 0 else token[:-1])
        except ValueError:
            continue
        runs.extend(_printable_run("hex", token, decoded))
    return runs


def _printable_run(kind, token, decoded) -> list[dict]:
    try:
        decoded_text = decoded.decode("utf-8")
    except UnicodeDecodeError:
        return []
    if not decoded_text:
        return []
    printable = sum(c.isprintable() or c.isspace() for c in decoded_text) / len(decoded_text)
    if printable < 0.8:
        return []
    return [{"kind": kind, "length": len(token), "decoded": decoded_text[:200]}]


SPACED_RUN_RE = re.compile(r"\b(?:\w ){3,}\w\b")


def injection_phrases(text: str) -> list[str]:
    """Weak multilingual phrase heuristic; text inside code spans is exempt.

    Phrases are matched on the folded view, its confusable skeleton (homoglyphs) and a
    copy with letter-spaced runs ("i g n o r e") joined back into words.
    """
    outside_code = detection_view(CODE_SPAN_RE.sub(" ", text))
    collapsed = " ".join(outside_code.split())
    despaced = SPACED_RUN_RE.sub(lambda m: m.group().replace(" ", ""), collapsed)
    views = (collapsed, skeleton(collapsed), despaced, skeleton(despaced))
    return [phrase for phrase in INJECTION_PHRASES
            if any(phrase.casefold() in view or phrase.casefold().replace(" ", "") in view
                   for view in views)]


def analyze(text: str, *, allow_hosts=()) -> InputAnalysis:
    analysis = InputAnalysis()
    analysis.signals.extend(_character_signals(text))
    analysis.signals.extend(_mixed_script_tokens(text))
    analysis.script_profile = script_profile(text)
    analysis.urls = extract_urls(text, allow_hosts)
    for url in analysis.urls:
        if {"punycode", "mixed_script", "confusable_with_allowlisted"} & set(url["flags"]):
            analysis.signals.append(Signal("confusable_url_host", "high", 0, url["host"]))
    analysis.contacts = extract_contacts(text)
    analysis.encoded_runs = _encoded_runs(strip_invisible(text))
    analysis.injection_phrases = injection_phrases(text)
    for run in analysis.encoded_runs:
        # Decode-and-rescan: payloads hidden in base64/hex are scanned like plain text.
        hidden = injection_phrases(run["decoded"])
        analysis.injection_phrases.extend(p for p in hidden if p not in analysis.injection_phrases)
        analysis.urls.extend(extract_urls(run["decoded"], allow_hosts))
        analysis.signals.append(Signal("encoded_payload", "low", 0, run["kind"]))
    for signal in [s for s in analysis.signals if s.kind == "tag_characters"]:
        # Tag smuggling hides ASCII; the decoded text is rescanned like a payload.
        hidden = injection_phrases(signal.detail)
        analysis.injection_phrases.extend(p for p in hidden if p not in analysis.injection_phrases)
    if analysis.injection_phrases:
        analysis.signals.append(Signal("injection_phrase", "low", 0, analysis.injection_phrases[0]))
    view = detection_view(text)
    analysis.spam_features = {
        "url_count": len(analysis.urls),
        "shortener": any("shortener" in u["flags"] for u in analysis.urls),
        "handles": len(HANDLE_RE.findall(CODE_SPAN_RE.sub(" ", text))),
        "messenger_or_phone": bool(MESSENGER_RE.search(view))
        or any(c["kind"] == "phone" for c in analysis.contacts),
        "earnings_phrase": bool(EARNINGS_RE.search(view)),
        "repeated_characters": bool(REPEAT_RE.search(view)),
    }
    return analysis


def sanitize(text: str) -> str:
    """Drop concealment characters, keep legitimate joiners and selectors.

    Removed: tag characters outside emoji tag sequences, bidi controls, direction marks,
    and ZWJ/ZWNJ/format characters that the signal rules flag. Kept: emoji ZWJ sequences,
    Indic virama joiners, emoji variation selectors and subdivision-flag tags.
    Opt-in only (``sanitize_model_input``); by default model input is never rewritten.
    """
    drop = set()
    for signal in _character_signals(text):
        if signal.kind == "tag_characters":
            index = signal.index
            while index < len(text) and (ord(text[index]) in TAG_RANGE or text[index] == CANCEL_TAG):
                drop.add(index)
                index += 1
        elif signal.kind in ("bidi_control", "direction_mark", "invisible_in_word",
                             "invisible_character", "stray_variation_selector"):
            drop.add(signal.index)
    return "".join(c for i, c in enumerate(text) if i not in drop)
