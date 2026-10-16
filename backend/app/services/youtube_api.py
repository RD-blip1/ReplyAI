import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

import google_auth_httplib2
import httplib2
from fastapi.concurrency import run_in_threadpool
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import YouTubeConnection
from app.security import StoredTokenInvalid, decrypt_tokens, encrypt_tokens
from app.services.google_oauth import client_config

VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
SAFE_GOOGLE_REASONS = {
    "authError",
    "commentTextTooLong",
    "commentsDisabled",
    "dailyLimitExceeded",
    "forbidden",
    "ineligibleAccount",
    "insufficientPermissions",
    "invalidCredentials",
    "invalidCommentMetadata",
    "invalidCustomEmoji",
    "operationNotSupported",
    "parentCommentIsPrivate",
    "parentCommentNotFound",
    "parentIdMissing",
    "processingFailure",
    "playlistItemNotFound",
    "quotaExceeded",
    "userRateLimitExceeded",
    "videoNotFound",
}
logger = logging.getLogger(__name__)


class YouTubeFailure(Exception):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(code)


class YouTubePublishFailure(YouTubeFailure):
    """A failed attempt to insert a reply, carrying what is known about the outcome.

    ``outcome`` is the only thing that decides whether a person may try again, so it is
    set from the documented meaning of the provider's response and never guessed.

    ``rejected``
        YouTube refused the request and created nothing. Retrying is safe.
    ``unknown``
        The request may have been created before the failure. Retrying risks a second
        public reply, so it is never retried automatically.
    """

    def __init__(self, code: str, message: str, status_code: int, outcome: str) -> None:
        self.outcome = outcome
        super().__init__(code, message, status_code)



def _error_reasons(error: HttpError) -> set[str]:
    try:
        payload = json.loads(error.content.decode("utf-8"))
        return {
            item.get("reason", "")
            for item in payload.get("error", {}).get("errors", [])
            if isinstance(item, dict)
        }
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
        return set()


def map_google_error(error: HttpError) -> YouTubeFailure:
    reasons = _error_reasons(error)
    if "commentsDisabled" in reasons:
        return YouTubeFailure(
            "comments_disabled", "Comments are disabled for this video.", 409
        )
    if reasons & {"quotaExceeded", "dailyLimitExceeded", "userRateLimitExceeded"}:
        return YouTubeFailure(
            "quota_exceeded", "YouTube API quota is temporarily unavailable.", 429
        )
    if reasons & {"videoNotFound", "playlistItemNotFound"} or error.resp.status == 404:
        return YouTubeFailure("video_unavailable", "This video is unavailable.", 404)
    if "insufficientPermissions" in reasons:
        return YouTubeFailure(
            "insufficient_scope",
            "Google denied this request because the granted permissions are insufficient. Update YouTube permissions to retry comment access. ReplyAI reads channel and comment data and can post one reply you confirm; it never edits or deletes YouTube content.",
            403,
        )
    if error.resp.status == 401 or "invalidCredentials" in reasons:
        return YouTubeFailure(
            "reconnect_required", "YouTube access needs to be reconnected.", 401
        )
    if error.resp.status == 403:
        return YouTubeFailure(
            "youtube_forbidden", "Google denied this read-only YouTube request.", 403
        )
    return YouTubeFailure(
        "youtube_unavailable", "YouTube could not complete this request. Try again later.", 502
    )


# Only these documented reasons mean YouTube refused the request and created nothing.
# "processingFailure" is deliberately absent: the API reports that the server could not
# process the request, which does not rule out that the comment was created, so it is
# treated as uncertain rather than as a safe rejection. Any unlisted reason, status, or
# transport failure is uncertain too, because nothing in it proves either way.
PUBLISH_REJECTED_REASONS = {
    "authError",
    "commentTextTooLong",
    "commentsDisabled",
    "dailyLimitExceeded",
    "forbidden",
    "ineligibleAccount",
    "insufficientPermissions",
    "invalidCommentMetadata",
    "invalidCredentials",
    "invalidCustomEmoji",
    "operationNotSupported",
    "parentCommentIsPrivate",
    "parentCommentNotFound",
    "parentIdMissing",
    "quotaExceeded",
    "userRateLimitExceeded",
}
PUBLISH_UNKNOWN_MESSAGE = (
    "YouTube may or may not have posted this reply. Check the video's replies before "
    "trying again, so the comment is not posted twice."
)


def map_publish_error(error: HttpError) -> YouTubePublishFailure:
    """Map a comments.insert failure to a safe message and an honest outcome."""
    reasons = _error_reasons(error)
    if not reasons & PUBLISH_REJECTED_REASONS:
        return YouTubePublishFailure(
            "publish_outcome_unknown", PUBLISH_UNKNOWN_MESSAGE, 502, "unknown"
        )
    if "parentCommentNotFound" in reasons:
        return YouTubePublishFailure(
            "parent_comment_unavailable",
            "YouTube could not find the comment being replied to. It may have been deleted.",
            404, "rejected",
        )
    if "operationNotSupported" in reasons:
        return YouTubePublishFailure(
            "reply_not_allowed",
            "YouTube does not allow a reply to that comment.", 409, "rejected",
        )
    if "parentCommentIsPrivate" in reasons:
        return YouTubePublishFailure(
            "parent_comment_private",
            "YouTube does not allow replies to that private comment.", 409, "rejected",
        )
    if "commentTextTooLong" in reasons:
        return YouTubePublishFailure(
            "reply_too_long", "YouTube rejected the reply as too long.", 422, "rejected"
        )
    if reasons & {"invalidCustomEmoji", "invalidCommentMetadata", "parentIdMissing"}:
        return YouTubePublishFailure(
            "reply_rejected", "YouTube rejected the reply as invalid.", 400, "rejected"
        )
    if "ineligibleAccount" in reasons:
        return YouTubePublishFailure(
            "channel_not_eligible",
            "This Google account is not eligible to post comments on YouTube.", 403, "rejected",
        )
    if reasons & {"insufficientPermissions", "forbidden"}:
        return YouTubePublishFailure(
            "insufficient_scope",
            "Google denied the request because the granted permissions are insufficient. "
            "Update YouTube permissions to retry posting.",
            403, "rejected",
        )
    if "commentsDisabled" in reasons:
        return YouTubePublishFailure(
            "comments_disabled", "Comments are disabled for this video.", 409, "rejected"
        )
    if reasons & {"quotaExceeded", "dailyLimitExceeded", "userRateLimitExceeded"}:
        return YouTubePublishFailure(
            "quota_exceeded",
            "YouTube API quota is temporarily unavailable, so nothing was posted.", 429, "rejected",
        )
    return YouTubePublishFailure(
        "reconnect_required", "YouTube access needs to be reconnected.", 401, "rejected"
    )


def _credentials(connection: YouTubeConnection, tokens: dict[str, str | None]) -> Credentials:
    config = client_config()
    expiry_text = tokens.get("expiry")
    expiry = datetime.fromisoformat(expiry_text) if expiry_text else None
    if expiry and expiry.tzinfo:
        expiry = expiry.astimezone(UTC).replace(tzinfo=None)
    return Credentials(
        token=tokens.get("access_token"),
        refresh_token=tokens.get("refresh_token"),
        token_uri=config["token_uri"],
        client_id=config["client_id"],
        client_secret=config["client_secret"],
        scopes=connection.scopes.split(),
        expiry=expiry,
    )


def _aware_utc(value: datetime | None) -> datetime | None:
    if value and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC) if value else None


def _serialize_credentials(credentials: Credentials, previous_refresh_token: str | None) -> str:
    refresh_token = credentials.refresh_token or previous_refresh_token
    expiry = _aware_utc(credentials.expiry)
    return encrypt_tokens(
        {
            "access_token": credentials.token,
            "refresh_token": refresh_token,
            "expiry": expiry.isoformat() if expiry else None,
        }
    )


async def credentials_for_connection(
    connection: YouTubeConnection, db: AsyncSession
) -> Credentials:
    if connection.reconnect_required:
        raise YouTubeFailure(
            "reconnect_required", "Reconnect YouTube to continue.", 401
        )
    try:
        tokens = decrypt_tokens(connection.encrypted_tokens)
        credentials = _credentials(connection, tokens)
    except (StoredTokenInvalid, KeyError, TypeError, ValueError):
        connection.reconnect_required = True
        await db.commit()
        raise YouTubeFailure(
            "reconnect_required", "Reconnect YouTube to continue.", 401
        ) from None

    if credentials.expired or not credentials.token:
        if not credentials.refresh_token:
            connection.reconnect_required = True
            await db.commit()
            raise YouTubeFailure(
                "reconnect_required", "Reconnect YouTube to continue.", 401
            )
        try:
            def refresh_with_timeout() -> None:
                auth_request = GoogleAuthRequest()

                def bounded_request(url: str, **kwargs: Any) -> Any:
                    kwargs["timeout"] = 10
                    return auth_request(url, **kwargs)

                credentials.refresh(bounded_request)

            await run_in_threadpool(refresh_with_timeout)
        except RefreshError as exc:
            reason = exc.args[0] if exc.args else ""
            invalid_grant = (isinstance(reason, dict) and reason.get("error") == "invalid_grant") or (
                "invalid_grant" in str(reason)
            )
            if invalid_grant:
                connection.reconnect_required = True
                await db.commit()
                raise YouTubeFailure(
                    "reconnect_required", "Reconnect YouTube to continue.", 401
                ) from None
            raise YouTubeFailure(
                "google_temporarily_unavailable",
                "Google could not refresh access right now. Try again later.",
                503,
            ) from None
        connection.encrypted_tokens = _serialize_credentials(
            credentials, credentials.refresh_token
        )
        connection.token_expires_at = _aware_utc(credentials.expiry)
        await db.commit()

    return credentials


async def youtube_request(
    credentials: Credentials, resource: str, method: str, **parameters: Any
) -> dict[str, Any]:
    def execute() -> dict[str, Any]:
        http = google_auth_httplib2.AuthorizedHttp(
            credentials,
            http=httplib2.Http(timeout=10),
            refresh_status_codes=(),
        )
        service = build("youtube", "v3", http=http, cache_discovery=False)
        request = getattr(getattr(service, resource)(), method)(**parameters)
        return request.execute(num_retries=0)

    try:
        return await run_in_threadpool(execute)
    except HttpError as exc:
        mapped = map_google_error(exc)
        reasons = _error_reasons(exc)
        safe_reason = next(iter(reasons & SAFE_GOOGLE_REASONS), "unclassified")
        logger.warning(
            "Google YouTube API call failed resource=%s method=%s status=%d reason=%s code=%s message=%s",
            resource,
            method,
            exc.resp.status,
            safe_reason,
            mapped.code,
            mapped.message,
        )
        if mapped.code == "reconnect_required":
            raise mapped from None
        raise mapped from None
    except (OSError, TimeoutError):
        raise YouTubeFailure(
            "youtube_temporarily_unavailable",
            "YouTube could not be reached. Try again later.",
            503,
        ) from None


def channel_from_response(payload: dict[str, Any]) -> dict[str, str | None] | None:
    items = payload.get("items") or []
    if not items:
        return None
    channel = items[0]
    snippet = channel.get("snippet", {})
    thumbnails = snippet.get("thumbnails", {})
    thumbnail = next(
        (thumbnails[size].get("url") for size in ("high", "medium", "default") if thumbnails.get(size)),
        None,
    )
    uploads = channel.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
    if not channel.get("id") or not uploads:
        return None
    return {
        "channel_id": channel["id"],
        "channel_title": snippet.get("title", "YouTube channel"),
        "channel_thumbnail_url": thumbnail,
        "uploads_playlist_id": uploads,
    }


async def fetch_authorized_channel(credentials: Credentials) -> dict[str, str | None] | None:
    payload = await youtube_request(
        credentials,
        "channels",
        "list",
        part="snippet,contentDetails",
        mine=True,
        maxResults=1,
    )
    return channel_from_response(payload)


def _best_thumbnail(thumbnails: dict[str, Any]) -> str | None:
    for size in ("maxres", "standard", "high", "medium", "default"):
        if thumbnails.get(size, {}).get("url"):
            return thumbnails[size]["url"]
    return None


async def list_upload_videos(
    connection: YouTubeConnection, credentials: Credentials, page_token: str | None
) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "part": "snippet,contentDetails",
        "playlistId": connection.uploads_playlist_id,
        "maxResults": 20,
    }
    if page_token:
        parameters["pageToken"] = page_token
    payload = await youtube_request(credentials, "playlistItems", "list", **parameters)
    videos = []
    for item in payload.get("items", []):
        snippet = item.get("snippet", {})
        video_id = snippet.get("resourceId", {}).get("videoId")
        if not video_id:
            continue
        videos.append(
            {
                "video_id": video_id,
                "title": snippet.get("title", "Untitled video"),
                "thumbnail_url": _best_thumbnail(snippet.get("thumbnails", {})),
                "published_at": snippet.get("publishedAt"),
                "watch_url": f"https://www.youtube.com/watch?v={video_id}",
            }
        )
    return {"items": videos, "next_page_token": payload.get("nextPageToken")}


async def video_belongs_to_channel(
    connection: YouTubeConnection, credentials: Credentials, video_id: str
) -> bool:
    if not VIDEO_ID_PATTERN.fullmatch(video_id):
        return False
    payload = await youtube_request(
        credentials,
        "playlistItems",
        "list",
        part="id",
        playlistId=connection.uploads_playlist_id,
        videoId=video_id,
        maxResults=1,
    )
    return bool(payload.get("items"))


async def list_top_level_comments(
    video_id: str, credentials: Credentials, page_token: str | None
) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "part": "snippet",
        "videoId": video_id,
        "maxResults": 100,
        "textFormat": "plainText",
    }
    if page_token:
        parameters["pageToken"] = page_token
    payload = await youtube_request(credentials, "commentThreads", "list", **parameters)
    comments = []
    for thread in payload.get("items", []):
        thread_snippet = thread.get("snippet", {})
        comment = thread_snippet.get("topLevelComment", {})
        snippet = comment.get("snippet", {})
        comments.append(
            {
                "comment_id": comment.get("id"),
                "video_id": thread_snippet.get("videoId"),
                "video_title": thread_snippet.get("videoTitle") or None,
                "author_name": snippet.get("authorDisplayName", "YouTube user"),
                "author_thumbnail_url": snippet.get("authorProfileImageUrl"),
                "published_at": snippet.get("publishedAt"),
                "text": snippet.get("textDisplay", ""),
                "total_reply_count": thread_snippet.get("totalReplyCount", 0),
            }
        )
    return {"items": comments, "next_page_token": payload.get("nextPageToken")}

def comment_text(snippet: dict[str, Any]) -> str | None:
    """Return the first usable comment body, or None.

    ``textOriginal`` can be present but null or blank, so a plain
    ``get(key, fallback)`` would discard a perfectly readable ``textDisplay``.
    """
    for key in ("textOriginal", "textDisplay"):
        value = snippet.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


async def get_owned_comment(connection, credentials, video_id: str, comment_id: str) -> str:
    """Read authoritative text; never accept client ownership or text claims."""
    if not await video_belongs_to_channel(connection, credentials, video_id):
        raise YouTubeFailure("video_unavailable", "Video is not in your channel uploads.", 404)

    # An id-filtered comments.list response omits snippet.videoId, so it cannot bind a
    # comment to a video. The thread response always carries videoId, and a thread whose
    # topLevelComment is this id also proves the comment is top-level.
    threads = await youtube_request(
        credentials, "commentThreads", "list", part="snippet", id=comment_id
    )
    thread_items = threads.get("items") or []
    if not thread_items:
        raise YouTubeFailure(
            "comment_not_returned",
            "YouTube did not return this comment as a top-level thread. It may be a "
            "reply, deleted, or still under review.",
            404,
        )
    thread_snippet = thread_items[0].get("snippet") or {}
    returned_video = thread_snippet.get("videoId")
    if not isinstance(returned_video, str) or not returned_video.strip():
        raise YouTubeFailure(
            "comment_video_unverifiable",
            "YouTube did not report which video this comment belongs to.",
            404,
        )
    if returned_video != video_id:
        raise YouTubeFailure(
            "comment_video_mismatch", "That comment belongs to a different video.", 404
        )
    if (thread_snippet.get("topLevelComment") or {}).get("id") != comment_id:
        raise YouTubeFailure(
            "comment_is_reply",
            "Replies cannot be drafted. Open the top-level comment instead.",
            404,
        )

    payload = await youtube_request(
        credentials, "comments", "list", part="snippet", id=comment_id, textFormat="plainText"
    )
    items = payload.get("items") or []
    if not items:
        raise YouTubeFailure("comment_not_returned", "That comment is no longer available.", 404)
    for item in items:
        if item.get("id") != comment_id:
            continue
        snippet = item.get("snippet") or {}
        if snippet.get("channelId") != connection.channel_id:
            raise YouTubeFailure(
                "comment_channel_mismatch", "That comment is not on this channel's video.", 404
            )
        if snippet.get("parentId"):
            raise YouTubeFailure(
                "comment_is_reply",
                "Replies cannot be drafted. Open the top-level comment instead.",
                404,
            )
        text = comment_text(snippet)
        if text is None:
            raise YouTubeFailure(
                "comment_text_unavailable", "That comment has no readable text.", 404
            )
        if len(text) > 20000:
            raise YouTubeFailure("comment_text_unavailable", "That comment is too long to draft.", 404)
        return text
    raise YouTubeFailure(
        "comment_id_mismatch", "YouTube returned a different comment than requested.", 404
    )


async def insert_comment_reply(
    credentials: Credentials, parent_comment_id: str, text: str
) -> str:
    """Insert one public reply to a top-level comment and return the new reply's id.

    This is the only mutating YouTube call in the application, and it is built to be
    made at most once for any given attempt. Two settings guarantee that:

    ``num_retries=0``
        The HTTP layer must not replay a request whose outcome is already unknown. A
        replay after a timeout is exactly how a duplicate public reply happens.
    ``refresh_status_codes=()``
        The authorized HTTP layer must not refresh an expired token and re-send on a
        401. The credential is refreshed before this call, so a 401 here means the grant
        is genuinely broken, and re-sending on it would be a second insert.

    comments.insert offers no idempotency key, so this function cannot recognise a
    repeat of its own request. Duplicate protection is the caller's durable record,
    committed before this runs. The returned id is the provider's confirmation, and its
    absence is never treated as success.
    """
    def execute() -> dict[str, Any]:
        http = google_auth_httplib2.AuthorizedHttp(
            credentials,
            http=httplib2.Http(timeout=10),
            refresh_status_codes=(),
        )
        service = build("youtube", "v3", http=http, cache_discovery=False)
        return service.comments().insert(
            part="snippet",
            body={"snippet": {"textOriginal": text, "parentId": parent_comment_id}},
        ).execute(num_retries=0)

    try:
        payload = await run_in_threadpool(execute)
    except HttpError as exc:
        mapped = map_publish_error(exc)
        reasons = _error_reasons(exc)
        safe_reason = next(iter(reasons & SAFE_GOOGLE_REASONS), "unclassified")
        logger.warning(
            "Google YouTube reply insert failed parent=%s status=%d reason=%s code=%s outcome=%s",
            parent_comment_id,
            exc.resp.status,
            safe_reason,
            mapped.code,
            mapped.outcome,
        )
        raise mapped from None
    except (OSError, TimeoutError, httplib2.HttpLib2Error):
        # The request was sent. A timeout or a broken connection says nothing about
        # whether YouTube created the comment before the reply was lost, so this is
        # unknown, never a failure that is safe to repeat.
        logger.warning(
            "Google YouTube reply insert reached no conclusion parent=%s code=%s",
            parent_comment_id,
            "publish_outcome_unknown",
        )
        raise YouTubePublishFailure(
            "publish_outcome_unknown", PUBLISH_UNKNOWN_MESSAGE, 502, "unknown"
        ) from None

    reply_id = payload.get("id")
    if not isinstance(reply_id, str) or not reply_id.strip():
        # A 2xx without a comment id cannot be shown to anyone or linked to, so it is
        # not treated as a confirmed post.
        raise YouTubePublishFailure(
            "publish_outcome_unknown", PUBLISH_UNKNOWN_MESSAGE, 502, "unknown"
        )
    return reply_id


async def list_channel_comments(
    channel_id: str, credentials: Credentials, page_token: str | None
) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "part": "snippet",
        "allThreadsRelatedToChannelId": channel_id,
        "maxResults": 100,
        "textFormat": "plainText",
    }
    if page_token:
        parameters["pageToken"] = page_token
    payload = await youtube_request(credentials, "commentThreads", "list", **parameters)
    comments = []
    for thread in payload.get("items", []):
        thread_snippet = thread.get("snippet", {})
        comment = thread_snippet.get("topLevelComment", {})
        snippet = comment.get("snippet", {})
        comments.append(
            {
                "comment_id": comment.get("id"),
                "video_id": thread_snippet.get("videoId"),
                "video_title": thread_snippet.get("videoTitle") or None,
                "author_name": snippet.get("authorDisplayName", "YouTube user"),
                "author_thumbnail_url": snippet.get("authorProfileImageUrl"),
                "published_at": snippet.get("publishedAt"),
                "text": comment_text(snippet) or "",
                "total_reply_count": thread_snippet.get("totalReplyCount", 0),
            }
        )
    return {"items": comments, "next_page_token": payload.get("nextPageToken")}
