"""Reconciliation of unknown publication outcomes.

``comments.insert`` takes no idempotency key, so after a timeout ReplyAI cannot know
whether its request created a reply. The automated check looks for a reply on the
parent comment that *could* be ours and records what it saw. It never sets ``posted``
and never deletes a guard: a matching reply might have been typed by the creator
in YouTube Studio. Only a person resolves the attempt.

Match rule for a candidate reply:

    authorChannelId == channel_id
    ∧ NFC + whitespace-collapsed text == NFC + whitespace-collapsed attempt.text
    ∧ publishedAt ≥ attempt.created_at − 120 s
"""
from datetime import UTC, datetime, timedelta

from app.models.session import utc_now
from app.services import revalidation, youtube_api

CLOCK_SKEW = timedelta(seconds=120)
STATES = ("external_match_found", "no_match_seen", "eligible_for_human_reconciliation")


def _when(value):
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def matches(snippet: dict, attempt, channel_id: str) -> bool:
    if (snippet.get("authorChannelId") or {}).get("value") != channel_id:
        return False
    text = youtube_api.comment_text(snippet)
    if text is None or revalidation.normalized(text) != revalidation.normalized(attempt.text):
        return False
    created = attempt.created_at if attempt.created_at.tzinfo else attempt.created_at.replace(tzinfo=UTC)
    published = _when(snippet.get("publishedAt"))
    return published is not None and published >= created - CLOCK_SKEW


async def check(credentials, attempt, *, max_pages: int = 2) -> dict:
    """Read the parent's replies and describe what was seen. Read-only."""
    candidates, page_token, pages = [], None, 0
    for _ in range(max_pages):
        params = {"part": "snippet", "parentId": attempt.parent_comment_id,
                  "maxResults": 100, "textFormat": "plainText"}
        if page_token:
            params["pageToken"] = page_token
        page = await youtube_api.youtube_request(credentials, "comments", "list", **params)
        pages += 1
        for item in page.get("items", []):
            if matches(item.get("snippet") or {}, attempt, attempt.channel_id):
                candidates.append({"reply_comment_id": item.get("id"),
                                   "published_at": (item.get("snippet") or {}).get("publishedAt")})
        page_token = page.get("nextPageToken")
        if not page_token:
            break
    complete = not page_token
    if candidates:
        state = "external_match_found"
    elif complete:
        # Absence on a complete scan is still not proof: the reply may have been removed
        # or held for review by YouTube. A person decides.
        state = "no_match_seen"
    else:
        state = "eligible_for_human_reconciliation"
    return {"state": state, "candidates": candidates, "scan_complete": complete,
            "pages_read": pages, "checked_at": utc_now().isoformat()}
