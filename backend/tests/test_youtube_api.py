import asyncio
import json
import logging
from types import SimpleNamespace

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response

from app.routers.youtube import CommentPage
from app.services.youtube_api import (
    YouTubeFailure,
    get_owned_comment,
    list_channel_comments,
    list_top_level_comments,
    list_upload_videos,
    map_google_error,
    video_belongs_to_channel,
    youtube_request,
)


def test_upload_video_listing_uses_upload_playlist_and_page_token(monkeypatch) -> None:
    requests: list[tuple[str, str, dict[str, object]]] = []

    async def fake_request(_credentials, resource, method, **parameters):
        requests.append((resource, method, parameters))
        return {
            "items": [
                {
                    "snippet": {
                        "title": "Real API response title",
                        "publishedAt": "2026-09-01T12:00:00Z",
                        "resourceId": {"videoId": "abcdefghijk"},
                        "thumbnails": {
                            "high": {"url": "https://i.ytimg.com/high.jpg"},
                            "maxres": {"url": "https://i.ytimg.com/max.jpg"},
                        },
                    }
                }
            ],
            "nextPageToken": "next-upload-page",
        }

    monkeypatch.setattr("app.services.youtube_api.youtube_request", fake_request)
    result = asyncio.run(
        list_upload_videos(
            SimpleNamespace(uploads_playlist_id="UU_owned_uploads"),
            object(),
            "incoming-page",
        )
    )

    assert requests == [
        (
            "playlistItems",
            "list",
            {
                "part": "snippet,contentDetails",
                "playlistId": "UU_owned_uploads",
                "maxResults": 20,
                "pageToken": "incoming-page",
            },
        )
    ]
    assert result["next_page_token"] == "next-upload-page"
    assert result["items"][0] == {
        "video_id": "abcdefghijk",
        "title": "Real API response title",
        "thumbnail_url": "https://i.ytimg.com/max.jpg",
        "published_at": "2026-09-01T12:00:00Z",
        "watch_url": "https://www.youtube.com/watch?v=abcdefghijk",
    }


def test_top_level_comments_are_paginated_and_do_not_expand_replies(monkeypatch) -> None:
    requests: list[dict[str, object]] = []

    async def fake_request(_credentials, resource, method, **parameters):
        assert (resource, method) == ("commentThreads", "list")
        requests.append(parameters)
        return {
            "items": [
                {
                    "snippet": {
                        "totalReplyCount": 3,
                        "topLevelComment": {
                            "id": "comment-id",
                            "snippet": {
                                "authorDisplayName": "Viewer",
                                "authorProfileImageUrl": "https://example.invalid/avatar.jpg",
                                "publishedAt": "2026-09-02T10:00:00Z",
                                "textDisplay": "<script>text is rendered as plain text</script>",
                            },
                        },
                    }
                }
            ],
            "nextPageToken": "next-comment-page",
        }

    monkeypatch.setattr("app.services.youtube_api.youtube_request", fake_request)
    result = asyncio.run(list_top_level_comments("abcdefghijk", object(), "next-input"))

    assert requests == [
        {
            "part": "snippet",
            "videoId": "abcdefghijk",
            "maxResults": 100,
            "textFormat": "plainText",
            "pageToken": "next-input",
        }
    ]
    assert result["next_page_token"] == "next-comment-page"
    assert result["items"][0]["total_reply_count"] == 3
    assert result["items"][0]["text"].startswith("<script>")
    assert "replies" not in result["items"][0]


def test_channel_comments_include_video_context(monkeypatch) -> None:
    async def fake_request(_credentials, resource, method, **parameters):
        assert (resource, method) == ("commentThreads", "list")
        assert parameters["allThreadsRelatedToChannelId"] == "channel-123"
        return {
            "items": [
                {
                    "snippet": {
                        "videoId": "video-123",
                        "totalReplyCount": 2,
                        "topLevelComment": {
                            "id": "comment-123",
                            "snippet": {
                                "authorDisplayName": "Viewer",
                                "authorProfileImageUrl": "https://example.invalid/avatar.jpg",
                                "publishedAt": "2026-09-01T12:00:00Z",
                                "textDisplay": "Hello there",
                            },
                        },
                    }
                }
            ],
            "nextPageToken": "next-channel-page",
        }

    monkeypatch.setattr("app.services.youtube_api.youtube_request", fake_request)
    result = asyncio.run(list_channel_comments("channel-123", object(), None))
    response = CommentPage(**result).model_dump()

    assert response["next_page_token"] == "next-channel-page"
    assert response["items"][0]["comment_id"] == "comment-123"
    assert response["items"][0]["video_id"] == "video-123"
    assert response["items"][0]["video_title"] is None


def test_video_ownership_query_is_limited_to_connected_uploads(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_request(_credentials, resource, method, **parameters):
        captured.update({"resource": resource, "method": method, **parameters})
        return {"items": [{"id": "playlist-item"}]}

    monkeypatch.setattr("app.services.youtube_api.youtube_request", fake_request)
    belongs = asyncio.run(
        video_belongs_to_channel(
            SimpleNamespace(uploads_playlist_id="UU_owned_uploads"),
            object(),
            "abcdefghijk",
        )
    )

    assert belongs is True
    assert captured == {
        "resource": "playlistItems",
        "method": "list",
        "part": "id",
        "playlistId": "UU_owned_uploads",
        "videoId": "abcdefghijk",
        "maxResults": 1,
    }
    assert asyncio.run(
        video_belongs_to_channel(
            SimpleNamespace(uploads_playlist_id="UU_owned_uploads"),
            object(),
            "not-a-video-id",
        )
    ) is False


def test_google_api_errors_map_to_safe_user_messages() -> None:
    def error(status: int, reason: str) -> HttpError:
        response = Response({"status": str(status)})
        body = json.dumps(
            {"error": {"errors": [{"reason": reason, "message": "private detail"}]}}
        ).encode("utf-8")
        return HttpError(response, body)

    comments_disabled = map_google_error(error(403, "commentsDisabled"))
    quota = map_google_error(error(403, "quotaExceeded"))
    unavailable = map_google_error(error(404, "videoNotFound"))
    revoked = map_google_error(error(401, "invalidCredentials"))
    unauthorized = map_google_error(error(401, "unknown"))
    insufficient_scope = map_google_error(error(403, "insufficientPermissions"))

    assert (comments_disabled.code, comments_disabled.status_code) == ("comments_disabled", 409)
    assert (quota.code, quota.status_code) == ("quota_exceeded", 429)
    assert (unavailable.code, unavailable.status_code) == ("video_unavailable", 404)
    assert (revoked.code, revoked.status_code) == ("reconnect_required", 401)
    assert (unauthorized.code, unauthorized.status_code) == ("reconnect_required", 401)
    assert (insufficient_scope.code, insufficient_scope.status_code) == (
        "insufficient_scope",
        403,
    )
    assert "permissions are insufficient" in insufficient_scope.message
    assert all(
        "private detail" not in failure.message
        for failure in (
            comments_disabled,
            quota,
            unavailable,
            revoked,
            unauthorized,
            insufficient_scope,
        )
    )


def test_youtube_error_logs_only_sanitized_status_reason_and_message(
    monkeypatch, caplog
) -> None:
    private_body = "private-provider-detail-must-not-be-logged"
    response = Response({"status": "403"})
    http_error = HttpError(
        response,
        json.dumps(
            {
                "error": {
                    "errors": [
                        {"reason": "insufficientPermissions", "message": private_body}
                    ]
                }
            }
        ).encode("utf-8"),
    )

    class FailingRequest:
        def execute(self, *, num_retries):
            assert num_retries == 0
            raise http_error

    service = SimpleNamespace(
        channels=lambda: SimpleNamespace(list=lambda **_parameters: FailingRequest())
    )
    monkeypatch.setattr("app.services.youtube_api.google_auth_httplib2.AuthorizedHttp", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("app.services.youtube_api.build", lambda *_args, **_kwargs: service)
    caplog.set_level(logging.WARNING, logger="app.services.youtube_api")

    with pytest.raises(YouTubeFailure) as caught:
        asyncio.run(youtube_request(object(), "channels", "list", mine=True))

    assert getattr(caught.value, "code", None) == "insufficient_scope"
    assert "resource=channels method=list status=403 reason=insufficientPermissions" in caplog.text
    assert "code=insufficient_scope" in caplog.text
    assert "permissions are insufficient" in caplog.text
    assert private_body not in caplog.text

THREAD_ID = "Ugz-threads-are-not-comment-ids"
TOP_LEVEL_ID = "Ugx-realTopLevelCommentId000"
REPLY_ID = "Ugy-realReplyCommentId0000000"
VIDEO_ID = "KynQ5qkE7Ik"
OWNER_CHANNEL = "UCowner0000000000000000"
VIEWER_CHANNEL = "UCviewer1111111111111111"


PLAYLIST_ITEM_ID = "PL-item-id-not-a-video-id"
VIDEO_ID = "KynQ5qkE7Ik"
THREAD_ID = "Ugz-threadId0000000000000"
TOP_LEVEL_ID = "Ugx-realTopLevelCommentId000"
REPLY_ID = "Ugy-realReplyOrTopLevelId000"
OTHER_VIDEO_ID = "OtherVideo1111"
OWNER_CHANNEL = "UCowner0000000000000000"
VIEWER_CHANNEL = "UCviewer1111111111111111"
UPLOADS_PLAYLIST = "UUowner0000000000000000"


def _connection():
    return SimpleNamespace(channel_id=OWNER_CHANNEL, uploads_playlist_id=UPLOADS_PLAYLIST)


def _comment_payload(**snippet_overrides):
    """Realistic id-filtered comments.list item.

    Observed against the live API: this response has NO videoId and NO parentId key,
    because an id-filtered comments.list omits them. The author is not the owner.
    """
    snippet = {
        "authorDisplayName": "Some Viewer",
        "authorChannelId": {"value": VIEWER_CHANNEL},
        "channelId": OWNER_CHANNEL,
        "publishedAt": "2026-09-20T10:00:00Z",
        "textOriginal": "Poor quality",
        "textDisplay": "Poor quality",
    }
    snippet.update(snippet_overrides)
    return {"items": [{"kind": "youtube#comment", "etag": "etag",
                       "id": TOP_LEVEL_ID, "snippet": snippet}]}


def _thread_payload(*, video_id=VIDEO_ID, top_level_id=TOP_LEVEL_ID, omit_video_id=False):
    thread = {"id": THREAD_ID, "snippet": {"totalReplyCount": 0,
               "topLevelComment": {"id": top_level_id}}}
    if not omit_video_id:
        thread["snippet"]["videoId"] = video_id
    return {"items": [thread]}


def _patch(monkeypatch, comment, thread, *, owned=True):
    seen = []

    async def fake_request(_credentials, resource, method, **parameters):
        seen.append((resource, method, parameters))
        if resource == "playlistItems":
            if not owned:
                return {"items": []}
            return {"items": [{"id": PLAYLIST_ITEM_ID,
                               "snippet": {"resourceId": {"videoId": VIDEO_ID}}}]}
        if resource == "commentThreads":
            return thread
        return comment

    monkeypatch.setattr("app.services.youtube_api.youtube_request", fake_request)
    return seen


def test_list_returns_top_level_comment_id_not_the_thread_id(monkeypatch) -> None:
    async def fake_request(_credentials, resource, method, **parameters):
        assert (resource, method) == ("commentThreads", "list")
        return {"items": [{"id": THREAD_ID, "snippet": {
            "videoId": VIDEO_ID, "totalReplyCount": 2,
            "topLevelComment": {"id": TOP_LEVEL_ID,
                                "snippet": {"authorDisplayName": "Viewer",
                                            "textDisplay": "Poor quality"}},
        }}]}

    monkeypatch.setattr("app.services.youtube_api.youtube_request", fake_request)
    result = asyncio.run(list_top_level_comments(VIDEO_ID, object(), None))

    assert [item["comment_id"] for item in result["items"]] == [TOP_LEVEL_ID]
    assert THREAD_ID not in {item["comment_id"] for item in result["items"]}


def test_owned_comment_accepts_comment_when_comments_list_omits_video_id(monkeypatch) -> None:
    """Regression: comments.list omits snippet.videoId, so ownership must come from
    the thread. The old code compared None to the video id and reported a false
    video mismatch."""
    seen = _patch(monkeypatch, _comment_payload(), _thread_payload())
    text = asyncio.run(get_owned_comment(_connection(), object(), VIDEO_ID, TOP_LEVEL_ID))

    assert text == "Poor quality"
    assert [call[:2] for call in seen] == [
        ("playlistItems", "list"), ("commentThreads", "list"), ("comments", "list")]
    assert "videoId" not in _comment_payload()["items"][0]["snippet"]


def test_owned_comment_does_not_infer_reply_from_id_prefix(monkeypatch) -> None:
    """A Ugy-prefixed id is resolved through the thread, not rejected by prefix."""
    comment = {"items": [{"id": REPLY_ID, "snippet": {
        "channelId": OWNER_CHANNEL, "textDisplay": "Looks fine to me"}}]}
    thread = {"items": [{"id": THREAD_ID, "snippet": {
        "videoId": VIDEO_ID, "topLevelComment": {"id": REPLY_ID}}}]}
    _patch(monkeypatch, comment, thread)
    text = asyncio.run(get_owned_comment(_connection(), object(), VIDEO_ID, REPLY_ID))
    assert text == "Looks fine to me"


@pytest.mark.parametrize("thread_kwargs,expected_code", [
    ({"video_id": OTHER_VIDEO_ID}, "comment_video_mismatch"),
    ({"omit_video_id": True}, "comment_video_unverifiable"),
    ({"top_level_id": THREAD_ID}, "comment_is_reply"),
])
def test_thread_binding_rejections_are_named(monkeypatch, thread_kwargs, expected_code) -> None:
    _patch(monkeypatch, _comment_payload(), _thread_payload(**thread_kwargs))
    with pytest.raises(YouTubeFailure) as failure:
        asyncio.run(get_owned_comment(_connection(), object(), VIDEO_ID, TOP_LEVEL_ID))
    assert failure.value.code == expected_code


@pytest.mark.parametrize("overrides,expected_code", [
    ({"textOriginal": None, "textDisplay": ""}, "comment_text_unavailable"),
    ({"textOriginal": "", "textDisplay": "   "}, "comment_text_unavailable"),
    ({"channelId": "UCsomeoneelse2222222"}, "comment_channel_mismatch"),
    ({"parentId": TOP_LEVEL_ID}, "comment_is_reply"),
])
def test_comment_resource_rejections_are_named(monkeypatch, overrides, expected_code) -> None:
    _patch(monkeypatch, _comment_payload(**overrides), _thread_payload())
    with pytest.raises(YouTubeFailure) as failure:
        asyncio.run(get_owned_comment(_connection(), object(), VIDEO_ID, TOP_LEVEL_ID))
    assert failure.value.code == expected_code


@pytest.mark.parametrize("blank_text_original", [None, "", "   "])
def test_owned_comment_falls_back_to_text_display(monkeypatch, blank_text_original) -> None:
    _patch(monkeypatch,
           _comment_payload(textOriginal=blank_text_original, textDisplay="Still readable"),
           _thread_payload())
    text = asyncio.run(get_owned_comment(_connection(), object(), VIDEO_ID, TOP_LEVEL_ID))
    assert text == "Still readable"


@pytest.mark.parametrize("comment,expected_code", [
    ({"items": []}, "comment_not_returned"),
    ({"items": [{"id": THREAD_ID, "snippet": {"channelId": OWNER_CHANNEL,
                                              "textDisplay": "x"}}]}, "comment_id_mismatch"),
])
def test_missing_comment_is_reported_distinctly(monkeypatch, comment, expected_code) -> None:
    _patch(monkeypatch, comment, _thread_payload())
    with pytest.raises(YouTubeFailure) as failure:
        asyncio.run(get_owned_comment(_connection(), object(), VIDEO_ID, TOP_LEVEL_ID))
    assert failure.value.code == expected_code


def test_unreturned_thread_is_reported_before_comment_lookup(monkeypatch) -> None:
    _patch(monkeypatch, _comment_payload(), {"items": []})
    with pytest.raises(YouTubeFailure) as failure:
        asyncio.run(get_owned_comment(_connection(), object(), VIDEO_ID, TOP_LEVEL_ID))
    assert failure.value.code == "comment_not_returned"


def test_video_ownership_is_still_enforced_before_any_comment_lookup(monkeypatch) -> None:
    seen = _patch(monkeypatch, _comment_payload(), _thread_payload(), owned=False)
    with pytest.raises(YouTubeFailure) as failure:
        asyncio.run(get_owned_comment(_connection(), object(), VIDEO_ID, TOP_LEVEL_ID))
    assert failure.value.code == "video_unavailable"
    assert [call[0] for call in seen] == ["playlistItems"]
