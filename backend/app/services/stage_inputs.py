"""One explicit provider-input contract for UI, graph, worker and evaluation."""
import re

from app.config import settings
from app.services import reply_preferences

DEFAULT_ACCEPTED_LANGUAGES = ("en", "hi", "hinglish")
DESCRIPTION_EXCERPT_CHARS = 2000
CHAPTER_RE = re.compile(r"^\s*\(?((?:\d{1,2}:)?\d{1,2}:\d{2})\)?\s*[-–—:]?\s*(.+?)\s*$", re.MULTILINE)


def chapters(description: str | None) -> list[str]:
    """Chapter lines ("00:00 Intro") parsed from a video description (part of the triage scope card)."""
    return [f"{stamp} {title}"[:120] for stamp, title in CHAPTER_RE.findall(description or "")][:30]


def evidence(ctx: dict) -> dict:
    """The evidence pack for grounding variants v1+: what the creator published."""
    return {"video_title": ctx.get("video_title") or "Unknown",
            "description_excerpt": (ctx.get("video_description") or "")[:DESCRIPTION_EXCERPT_CHARS],
            "chapters": chapters(ctx.get("video_description"))}


def spotlight(text: str, marker: str) -> str:
    """Delimit untrusted text with a per-request random boundary."""
    return f"<<{marker}>>{text}<</{marker}>>"


def build(stage: str, ctx: dict, *, reply: str = '') -> dict:
    comment = ctx['original_comment']
    # Opt-in input hardening; both default off, so the original text is sent.
    if ctx.get('sanitize_model_input', settings.sanitize_model_input):
        from app.services.input_analysis import sanitize
        comment = sanitize(comment)
    if ctx.get('spotlight_marker'):
        comment = spotlight(comment, ctx['spotlight_marker'])
    variant = ctx.get('grounding_variant', settings.grounding_variant)
    if stage == 'triage':
        description = ctx.get('video_description') or 'None'
        return {'comment': comment, 'video_title': ctx.get('video_title') or 'Unknown',
                'video_description': description[:DESCRIPTION_EXCERPT_CHARS],
                'chapters': chapters(ctx.get('video_description')),
                'channel_title': ctx.get('channel_title', ''),
                'accepted_input_languages': list(ctx.get('accepted_input_languages')
                                                 or DEFAULT_ACCEPTED_LANGUAGES)}
    if stage == 'generation':
        data = {'comment': comment, 'video_id': ctx['video_id'],
                'channel': {'channel_id': ctx['channel_id'], 'title': ctx.get('channel_title', '')},
                'response_preferences': ctx.get('response_preferences') or reply_preferences.context(None)}
        if variant != 'v0':
            data['evidence'] = evidence(ctx)
        ignore = [s['text_span'] for s in (ctx.get('triage') or {}).get('segments') or []
                  if s.get('action') == 'ignore']
        if ignore:
            data['do_not_engage'] = ignore  # parts of the comment not to respond to
        return data
    if stage in ('classification', 'review_classification'):
        return {'comment': comment}
    if stage == 'quality':
        data = {'comment': comment, 'reply': reply}
        if variant in ('v2', 'v3'):
            data['evidence'] = evidence(ctx)
        return data
    raise ValueError(f'Unknown stage: {stage}')
