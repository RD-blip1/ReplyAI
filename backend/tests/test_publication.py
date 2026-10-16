"""Publishing one approved reply, and refusing every other way to post one.

Every test here mocks the insert. The shared workspace fixture also patches the read
helper with a function that raises if anything but a list call reaches it, and the write
path does not use that helper at all, so no test in this file can reach YouTube.

Several tests write draft state straight to the test database. That is deliberate: the
publication rules are the server's, so the cases that must be refused are set up behind
the API rather than by editing the object the client happens to be holding.
"""
import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httplib2
import pytest
from googleapiclient.errors import HttpError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from support import generate, post

from app.models import (
    PublishedReplyGuard,
    ReplyDraft,
    ReplyPublication,
    YouTubeConnection,
)
from app.services import publishing, youtube_api
from app.services.google_oauth import YOUTUBE_READONLY_SCOPE


def _write(w, model, key, **values):
    """Put a row into a state the API would only reach through some other history."""
    async def update():
        async with w.sessions() as db:
            row = await db.get(model, key)
            for name, value in values.items():
                setattr(row, name, value)
            await db.commit()
    asyncio.run(update())


def _set_draft(w, draft_id, **values):
    _write(w, ReplyDraft, draft_id, **values)


def _set_scopes(w, scopes):
    async def update():
        async with w.sessions() as db:
            connection = await db.scalar(select(YouTubeConnection))
            connection.scopes = scopes
            await db.commit()
    asyncio.run(update())


def approved(w, comment="comment"):
    """A draft that satisfies every publication rule, with nothing published yet."""
    draft = generate(w, comment)
    assert post(w, draft, "decision", decision="approved").status_code == 200
    return w.client.get(f'/api/drafts/{draft["id"]}').json()


def publish(w, draft, **body):
    return w.client.post(
        f'/api/drafts/{draft["id"]}/publication', headers=w.headers,
        json={"revision": draft["revision"], "text": draft["text"], **body})


def state_of(w, draft_id):
    return w.client.get(f'/api/drafts/{draft_id}/publication').json()


@pytest.fixture
def inserts(workspace, monkeypatch):
    """Stand in for the only mutating call, and record every one of them."""
    async def credentials(*args):
        return object()
    monkeypatch.setattr("app.routers.publication._credentials", credentials)

    calls: list[tuple[str, str]] = []

    def result(effect):
        async def insert(_credentials, parent_comment_id, text):
            calls.append((parent_comment_id, text))
            if isinstance(effect, BaseException):
                raise effect
            return effect
        monkeypatch.setattr("app.routers.publication.insert_comment_reply", insert)

    result("reply-comment-id")
    return SimpleNamespace(calls=calls, result=result)


def uncertain():
    return youtube_api.YouTubePublishFailure(
        "publish_outcome_unknown", youtube_api.PUBLISH_UNKNOWN_MESSAGE, 502, "unknown")


# --- the request the confirmation produces -------------------------------------


def test_publishes_the_saved_reply_once_and_records_it(workspace, inserts):
    draft = approved(workspace)
    response = publish(workspace, draft)

    assert response.status_code == 200, response.text
    record = response.json()
    assert record["status"] == "posted"
    assert record["reply_comment_id"] == "reply-comment-id"
    assert record["text"] == draft["text"]
    assert record["draft_revision"] == draft["revision"]
    assert record["parent_comment_id"] == "comment"
    assert record["video_id"] == "abcdefghijk"
    assert record["channel_id"] == "channel"
    # One insert, carrying the saved text and the top-level comment as parentId.
    assert inserts.calls == [("comment", draft["text"])]
    # The draft is released, so it is editable again.
    assert post(workspace, draft, "edit", text="Changed after posting").status_code == 200


def test_publishing_never_calls_a_model(workspace, inserts):
    draft = approved(workspace)
    spent = len(workspace.calls)
    assert publish(workspace, draft).status_code == 200
    assert len(workspace.calls) == spent


def test_confirming_twice_posts_one_comment(workspace, inserts):
    draft = approved(workspace)
    first = publish(workspace, draft)
    second = publish(workspace, draft)

    assert first.json() == second.json()
    assert len(inserts.calls) == 1


# --- rules the server decides, not the browser --------------------------------


@pytest.mark.parametrize("values, code", [
    ({"human_decision": "pending"}, "approval_required"),
    ({"human_decision": "rejected"}, "approval_required"),
    ({"review_status": "pending"}, "review_required"),
    ({"review_status": "failed"}, "review_required"),
    ({"quality": None}, "review_required"),
    ({"quality": {"relevance": "concern", "unsupported_claims": "pass",
                  "inappropriate_content": "pass", "findings": ["unclear ask"]}},
     "quality_not_passed"),
    ({"quality": {"relevance": "pass", "unsupported_claims": "uncertain",
                  "inappropriate_content": "pass", "findings": []}}, "quality_not_passed"),
])
def test_ineligible_drafts_are_refused_without_contacting_youtube(
        workspace, inserts, values, code):
    draft = approved(workspace)
    _set_draft(workspace, draft["id"], **values)

    response = publish(workspace, draft)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == code
    assert inserts.calls == []


def test_a_blank_saved_reply_is_not_publishable(workspace, inserts):
    # A blank reply cannot even be confirmed, so this is checked where the rule lives.
    draft = approved(workspace)
    _set_draft(workspace, draft["id"], text="   ")
    state = state_of(workspace, draft["id"])
    assert state["eligibility"]["eligible"] is False
    assert state["eligibility"]["blocked_code"] == "no_reply"
    assert inserts.calls == []


def test_classification_disagreement_does_not_block_publishing(workspace, inserts):
    draft = approved(workspace)
    _set_draft(workspace, draft["id"], agreement="disagree")
    assert publish(workspace, draft).status_code == 200
    assert inserts.calls == [("comment", draft["text"])]


def test_client_cannot_publish_text_that_is_not_saved(workspace, inserts):
    draft = approved(workspace)
    response = publish(workspace, draft, text="Something else entirely")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "reply_text_changed"
    assert inserts.calls == []


def test_confirmation_must_match_the_saved_revision(workspace, inserts):
    draft = approved(workspace)
    response = publish(workspace, draft, revision=draft["revision"] + 1)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "stale_draft"
    assert inserts.calls == []


def test_a_draft_for_another_channel_is_not_published(workspace, inserts):
    draft = approved(workspace)
    _set_draft(workspace, draft["id"], channel_id="some-other-channel")

    response = publish(workspace, draft)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "connection_changed"
    assert inserts.calls == []


def test_missing_posting_scope_is_refused_with_the_upgrade_action(workspace, inserts):
    draft = approved(workspace)
    _set_scopes(workspace, YOUTUBE_READONLY_SCOPE)

    response = publish(workspace, draft)
    assert response.status_code == 403
    body = response.json()["detail"]
    assert body["code"] == "insufficient_scope"
    assert "Update YouTube permissions" in body["message"]
    assert inserts.calls == []


def test_publishing_requires_csrf(workspace, inserts):
    draft = approved(workspace)
    response = workspace.client.post(
        f'/api/drafts/{draft["id"]}/publication',
        json={"revision": draft["revision"], "text": draft["text"]})
    assert response.status_code == 403
    assert inserts.calls == []


def test_a_draft_id_alone_is_not_enough_to_publish(workspace, inserts):
    draft = approved(workspace)
    workspace.client.cookies.clear()
    response = publish(workspace, draft)
    assert response.status_code == 401
    assert inserts.calls == []


# --- the outcome is what decides whether a retry is allowed -------------------


def test_an_unknown_outcome_is_never_retried(workspace, inserts):
    draft = approved(workspace)
    inserts.result(uncertain())

    first = publish(workspace, draft)
    assert first.status_code == 502
    assert first.json()["detail"]["code"] == "publish_outcome_unknown"

    again = publish(workspace, draft)
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "publication_unresolved"
    assert len(inserts.calls) == 1

    record = state_of(workspace, draft["id"])["publication"]
    assert record["status"] == "unknown" and record["retryable"] is False


def test_a_recorded_success_survives_a_reload(workspace, inserts):
    draft = approved(workspace)
    # Generating the draft read the comment, so measure from here rather than from zero.
    reads_before = len(workspace.youtube_calls)
    assert publish(workspace, draft).status_code == 200
    state = state_of(workspace, draft["id"])

    assert state["publication"]["status"] == "posted"
    assert state["publication"]["reply_comment_id"] == "reply-comment-id"
    # Reading it posts nothing and calls the provider for nothing.
    assert len(inserts.calls) == 1
    assert len(workspace.youtube_calls) == reads_before


def test_a_definitive_rejection_may_be_tried_again(workspace, inserts):
    draft = approved(workspace)
    inserts.result(youtube_api.YouTubePublishFailure(
        "reply_not_allowed", "YouTube does not allow a reply to that comment.",
        409, "rejected"))
    assert publish(workspace, draft).status_code == 409

    record = state_of(workspace, draft["id"])["publication"]
    assert record["status"] == "failed" and record["retryable"] is True

    inserts.result("reply-comment-id")
    assert publish(workspace, draft).status_code == 200
    assert len(inserts.calls) == 2


def test_a_reply_that_was_posted_cannot_be_posted_again_after_an_edit(workspace, inserts):
    draft = approved(workspace)
    assert publish(workspace, draft).status_code == 200

    edited = post(workspace, draft, "edit", text="A different reply entirely").json()
    post(workspace, edited, "review")
    reviewed = w_get(workspace, edited["id"])
    post(workspace, reviewed, "decision", decision="approved")

    response = publish(workspace, reviewed)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "already_published"
    assert len(inserts.calls) == 1


def test_a_reply_that_was_posted_cannot_be_posted_again_after_a_regenerate(
        workspace, inserts):
    """Regenerating a posted draft rewrites its text, but never its publication record.

    The client sees a brand-new reply it could approve and confirm, so this pins the
    server-side refusal: one insert for the comment's whole life, regardless of how many
    times the draft is regenerated and re-approved afterwards.
    """
    draft = approved(workspace)
    assert publish(workspace, draft).status_code == 200

    regenerated = post(workspace, draft, "regenerate").json()
    assert regenerated["revision"] == draft["revision"] + 1
    post(workspace, regenerated, "review")
    reviewed = w_get(workspace, regenerated["id"])
    post(workspace, reviewed, "decision", decision="approved")

    response = publish(workspace, reviewed)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "already_published"
    assert len(inserts.calls) == 1


def test_a_reply_that_was_posted_cannot_be_posted_again_after_a_refinement(
        workspace, inserts):
    """Applying a suggested rewrite is another edit, and it cannot republish either."""
    draft = approved(workspace)
    assert publish(workspace, draft).status_code == 200

    refinement = post(workspace, draft, "refine", instruction="Make it warmer").json()
    turn_id = refinement["turn"]["id"]
    refined = post(workspace, draft, f"refine/{turn_id}/apply").json()
    assert refined["revision"] == draft["revision"] + 1
    post(workspace, refined, "review")
    reviewed = w_get(workspace, refined["id"])
    post(workspace, reviewed, "decision", decision="approved")

    response = publish(workspace, reviewed)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "already_published"
    assert len(inserts.calls) == 1


def test_a_record_that_cannot_be_saved_is_reported_as_unknown(workspace, inserts, monkeypatch):
    draft = approved(workspace)

    async def broken(*args, **kwargs):
        raise SQLAlchemyError("write failed")
    monkeypatch.setattr("app.routers.publication.settle", broken)

    response = publish(workspace, draft)
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "publish_outcome_unknown"
    # The insert did happen, and it is never repeated.
    assert len(inserts.calls) == 1
    assert publish(workspace, draft).status_code == 409


def test_a_draft_is_read_as_publishable_and_reading_costs_nothing(workspace, inserts):
    draft = approved(workspace)
    reads_before = len(workspace.youtube_calls)
    state = state_of(workspace, draft["id"])

    assert state["publication"] is None
    assert state["eligibility"]["eligible"] is True
    assert state["eligibility"]["scope_available"] is True
    assert state["eligibility"]["review"]["checks"] == {
        "relevance": "pass", "unsupported_claims": "pass", "inappropriate_content": "pass"}
    assert len(workspace.youtube_calls) == reads_before
    assert inserts.calls == []


def test_eligibility_read_tracks_the_current_revision_state(workspace, inserts):
    """The read the browser gates on must move with the draft, not lag behind it.

    A frontend bug once fetched this only when the draft opened, so after approving it
    still displayed the pre-approval reason next to the new "approved" decision. The
    server read was always correct; this pins it so the client can rely on re-reading:
    approval_required before the decision, eligible immediately after, and
    review_required once an edit clears both the review and the approval.
    """
    draft = generate(workspace, "comment")
    before = state_of(workspace, draft["id"])["eligibility"]
    assert before["eligible"] is False
    assert before["blocked_code"] == "approval_required"
    assert before["revision"] == draft["revision"]

    assert post(workspace, draft, "decision", decision="approved").status_code == 200
    after = state_of(workspace, draft["id"])["eligibility"]
    assert after["eligible"] is True
    assert after["blocked_code"] is None
    assert after["revision"] == draft["revision"]

    edited = post(workspace, draft, "edit", text="A different reply").json()
    cleared = state_of(workspace, draft["id"])["eligibility"]
    assert cleared["eligible"] is False
    assert cleared["blocked_code"] == "review_required"
    assert cleared["revision"] == edited["revision"]
    assert cleared["revision"] != before["revision"]


def w_get(w, draft_id):
    return w.client.get(f"/api/drafts/{draft_id}").json()


# --- the durable record -------------------------------------------------------


def test_an_abandoned_in_flight_record_reads_as_unknown(workspace, inserts):
    draft = approved(workspace)
    assert publish(workspace, draft).status_code == 200

    # Leave the record exactly as a process that died between sending and recording
    # would have left it.
    _set_draft(workspace, draft["id"], operation_token="in-flight")
    async def freeze():
        async with workspace.sessions() as db:
            record = await db.scalar(
                select(ReplyPublication).where(ReplyPublication.draft_id == draft["id"]))
            record.status = "sending"
            record.reply_comment_id = None
            record.resolved_at = None
            await db.commit()
    asyncio.run(freeze())

    record = state_of(workspace, draft["id"])["publication"]
    assert record["status"] == "unknown"
    assert record["retryable"] is False
    assert publish(workspace, draft).status_code == 409
    assert len(inserts.calls) == 1


def test_deleting_retained_work_is_blocked_while_an_outcome_is_open(workspace, inserts):
    draft = approved(workspace)
    inserts.result(uncertain())
    assert publish(workspace, draft).status_code == 502

    response = workspace.client.delete("/api/drafts", headers=workspace.headers)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "publication_unresolved"
    # The record survived, so it can still be checked against YouTube.
    assert w_get(workspace, draft["id"])["id"] == draft["id"]


def test_deleting_retained_work_is_allowed_once_the_outcome_is_known(workspace, inserts):
    draft = approved(workspace)
    assert publish(workspace, draft).status_code == 200
    assert workspace.client.delete("/api/drafts", headers=workspace.headers).status_code == 204


# --- a publish in flight owns the draft ---------------------------------------


def test_draft_changes_are_refused_while_a_publish_is_in_flight(workspace, inserts, monkeypatch):
    draft = approved(workspace)
    started, release = threading.Event(), threading.Event()

    async def slow(_credentials, parent_comment_id, text):
        inserts.calls.append((parent_comment_id, text))
        started.set()
        await asyncio.to_thread(release.wait, 5)
        return "reply-comment-id"
    monkeypatch.setattr("app.routers.publication.insert_comment_reply", slow)

    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(publish, workspace, draft)
        assert started.wait(5)
        for action, body in (("edit", {"text": "Nope"}), ("review", {}),
                             ("decision", {"decision": "approved"}),
                             ("decision", {"decision": "rejected"})):
            assert post(workspace, draft, action, **body).status_code == 409
        release.set()
        assert running.result().status_code == 200

    assert post(workspace, draft, "edit", text="Now allowed").status_code == 200


def test_a_confirmation_arriving_mid_flight_is_refused(workspace, inserts, monkeypatch):
    draft = approved(workspace)
    started, release = threading.Event(), threading.Event()

    async def slow(_credentials, parent_comment_id, text):
        inserts.calls.append((parent_comment_id, text))
        started.set()
        await asyncio.to_thread(release.wait, 5)
        return "reply-comment-id"
    monkeypatch.setattr("app.routers.publication.insert_comment_reply", slow)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(publish, workspace, draft)
        assert started.wait(5)
        second = pool.submit(publish, workspace, draft)
        # The second request is answered while the first still has not been sent, so it
        # must be refused rather than allowed to start a second attempt.
        second_code = second.result().status_code
        release.set()
        first_code = first.result().status_code

    assert (first_code, second_code) == (200, 409)
    assert len(inserts.calls) == 1


def test_the_schema_refuses_a_second_open_attempt(workspace, inserts):
    """The backstop for two claims that both pass the application check."""
    draft = approved(workspace)

    async def competing_claims():
        async with workspace.sessions() as db:
            for index in range(2):
                db.add(ReplyPublication(
                    draft_id=draft["id"], operation_id=f"operation-{index}",
                    channel_id="channel", video_id="abcdefghijk",
                    parent_comment_id="comment", draft_revision=1,
                    text=draft["text"], status="sending"))
            await db.commit()

    with pytest.raises(IntegrityError) as raised:
        asyncio.run(competing_claims())
    # The partial index is the authority, so it reports the column it guards.
    assert "reply_publications.draft_id" in str(raised.value)

    # A rejected attempt posted nothing, so it does not block the next one.
    async def rejected_then_open():
        async with workspace.sessions() as db:
            db.add(ReplyPublication(
                draft_id=draft["id"], operation_id="operation-failed", channel_id="channel",
                video_id="abcdefghijk", parent_comment_id="comment", draft_revision=1,
                text=draft["text"], status="failed"))
            db.add(ReplyPublication(
                draft_id=draft["id"], operation_id="operation-retry", channel_id="channel",
                video_id="abcdefghijk", parent_comment_id="comment", draft_revision=1,
                text=draft["text"], status="sending"))
            await db.commit()
    asyncio.run(rejected_then_open())


# --- the insert itself --------------------------------------------------------


def test_the_insert_is_sent_once_with_no_retry_and_no_refresh_resend(monkeypatch):
    seen = {}

    class Request:
        def execute(self, num_retries=None):
            seen["num_retries"] = num_retries
            return {"id": "reply-1"}

    class Comments:
        def insert(self, **parameters):
            seen["parameters"] = parameters
            return Request()

    class Service:
        def comments(self):
            return Comments()

    class AuthorizedHttp:
        def __init__(self, credentials, http=None, refresh_status_codes=()):
            seen["refresh_status_codes"] = refresh_status_codes

    monkeypatch.setattr(youtube_api.google_auth_httplib2, "AuthorizedHttp", AuthorizedHttp)
    monkeypatch.setattr(youtube_api, "build", lambda *a, **k: Service())

    reply_id = asyncio.run(youtube_api.insert_comment_reply(object(), "parent", "Hello"))

    assert reply_id == "reply-1"
    # No automatic replay, and no refresh-and-resend that would insert a second comment.
    assert seen["num_retries"] == 0
    assert seen["refresh_status_codes"] == ()
    assert seen["parameters"]["part"] == "snippet"
    assert seen["parameters"]["body"] == {
        "snippet": {"textOriginal": "Hello", "parentId": "parent"}}


def _http_error(status, reasons):
    content = json.dumps(
        {"error": {"errors": [{"reason": reason} for reason in reasons]}}).encode()
    return HttpError(httplib2.Response({"status": status}), content, uri="https://example.invalid")


@pytest.mark.parametrize("reason, rejected", [
    ("operationNotSupported", True),
    ("parentCommentNotFound", True),
    ("parentCommentIsPrivate", True),
    ("commentTextTooLong", True),
    ("invalidCustomEmoji", True),
    ("ineligibleAccount", True),
    ("insufficientPermissions", True),
    ("quotaExceeded", True),
    ("commentsDisabled", True),
    # Documented as a server-side processing failure, which does not prove the comment
    # was not created, so it must not be treated as safe to repeat.
    ("processingFailure", False),
    ("somethingUndocumented", False),
])
def test_only_documented_rejections_are_safe_to_repeat(reason, rejected):
    mapped = youtube_api.map_publish_error(_http_error(400, [reason]))
    assert (mapped.outcome == "rejected") is rejected


def test_a_response_without_a_comment_id_is_not_treated_as_posted(monkeypatch):
    class Request:
        def execute(self, num_retries=None):
            return {"kind": "youtube#comment"}

    class Service:
        def comments(self):
            return type("Comments", (), {"insert": lambda _s, **k: Request()})()

    monkeypatch.setattr(youtube_api, "build", lambda *a, **k: Service())
    with pytest.raises(youtube_api.YouTubePublishFailure) as raised:
        asyncio.run(youtube_api.insert_comment_reply(object(), "parent", "Hello"))
    assert raised.value.outcome == "unknown"


def test_a_transport_failure_is_uncertain():
    def refuse(*args, **kwargs):
        raise OSError("connection reset after the request was sent")
    monkeypatch_target = youtube_api.build
    youtube_api.build = refuse
    try:
        with pytest.raises(youtube_api.YouTubePublishFailure) as raised:
            asyncio.run(youtube_api.insert_comment_reply(object(), "parent", "Hello"))
    finally:
        youtube_api.build = monkeypatch_target
    assert raised.value.outcome == "unknown"


# --- database state, not just the response body -------------------------------
#
# The tests above mostly assert what the API answered. The tests below read the rows
# back out of the database instead, because a handler can return a plausible body while
# having written the wrong record, and only the row is the durable evidence.


def publications(w, draft_id):
    """Every publication row for a draft, read straight from the database."""
    async def read():
        async with w.sessions() as db:
            rows = (await db.execute(
                select(ReplyPublication)
                .where(ReplyPublication.draft_id == draft_id)
                .order_by(ReplyPublication.created_at, ReplyPublication.id)
            )).scalars().all()
            return [{
                "id": row.id,
                "status": row.status, "revision": row.draft_revision, "text": row.text,
                "reply_comment_id": row.reply_comment_id,
                "failure": dict(row.failure or {}),
                "channel_id": row.channel_id, "video_id": row.video_id,
                "parent_comment_id": row.parent_comment_id,
                "operation_id": row.operation_id,
                "created": row.created_at is not None, "sent": row.sent_at is not None,
                "resolved": row.resolved_at is not None,
            } for row in rows]
    return asyncio.run(read())


def draft_row(w, draft_id):
    async def read():
        async with w.sessions() as db:
            row = await db.get(ReplyDraft, draft_id)
            return {
                "revision": row.revision, "decision": row.human_decision,
                "review": row.review_status, "text": row.text,
                "operation_token": row.operation_token,
                "publication_token": row.publication_token,
            }
    return asyncio.run(read())


def _insert_service_raising(exc):
    """A youtube service whose insert raises, standing in for the provider."""
    class Request:
        def execute(self, num_retries=None):
            raise exc

    class Comments:
        def insert(self, **parameters):
            return Request()

    class Service:
        def comments(self):
            return Comments()

    return lambda *args, **kwargs: Service()


def _real_insert(workspace, monkeypatch, *, build):
    """Use the real write service, so a transport failure is mapped as in production.

    The publication router imported ``insert_comment_reply`` by name, so patching the
    service's own ``build`` is what keeps the whole mapping under test instead of
    replacing the function that decides the outcome.
    """
    async def credentials(*args):
        return object()
    monkeypatch.setattr("app.routers.publication._credentials", credentials)
    monkeypatch.setattr(youtube_api, "build", build)


def test_a_successful_publish_writes_one_row_and_releases_the_draft(workspace, inserts):
    draft = approved(workspace)
    before = draft_row(workspace, draft["id"])
    assert before["operation_token"] is None
    assert before["publication_token"] is None

    assert publish(workspace, draft).status_code == 200

    rows = publications(workspace, draft["id"])
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "posted"
    assert row["reply_comment_id"] == "reply-comment-id"
    # The row describes what was actually sent, from the draft and not from the client.
    assert row["text"] == draft["text"]
    assert row["revision"] == draft["revision"]
    assert row["parent_comment_id"] == "comment"
    assert row["channel_id"] == "channel"
    assert row["failure"] == {}
    # Recorded before the send, and only settled afterwards.
    assert row["created"] and row["sent"] and row["resolved"]

    after = draft_row(workspace, draft["id"])
    assert after["operation_token"] is None, "the review claim must be released on settle"
    assert after["publication_token"] is None, "the publish claim must be released on settle"


def test_a_definitive_refusal_records_failed_and_allows_exactly_one_retry(workspace, inserts):
    draft = approved(workspace)
    inserts.result(youtube_api.YouTubePublishFailure(
        "comments_disabled", "Replies are turned off on this comment.", 403, "rejected"))
    assert publish(workspace, draft).status_code == 403

    rows = publications(workspace, draft["id"])
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["reply_comment_id"] is None, "a refusal created nothing to link to"
    assert rows[0]["failure"]["code"] == "comments_disabled"
    assert rows[0]["resolved"] is True

    settled = draft_row(workspace, draft["id"])
    assert settled["publication_token"] is None
    assert settled["operation_token"] is None

    inserts.result("reply-comment-id")
    assert publish(workspace, draft).status_code == 200

    rows = publications(workspace, draft["id"])
    assert [row["status"] for row in rows] == ["failed", "posted"]
    assert len(rows[0]["operation_id"]) == 36
    assert rows[0]["operation_id"] != rows[1]["operation_id"]
    assert len(inserts.calls) == 2


def test_a_timeout_on_the_insert_records_unknown_and_is_never_resent(
        workspace, monkeypatch):
    """The dangerous case, end to end.

    YouTube may have created the reply before the connection dropped, so the only safe
    outcome is an unknown record and a refusal to try again. The real write service is
    used, because mapping a lost response to a retryable failure is exactly the mistake
    this test exists to prevent.
    """
    draft = approved(workspace)
    sends = []

    def drops(*args, **kwargs):
        sends.append(1)
        raise httplib2.HttpLib2Error("timed out after the request was sent")

    _real_insert(workspace, monkeypatch, build=drops)

    first = publish(workspace, draft)
    assert first.status_code == 502
    assert first.json()["detail"]["code"] == "publish_outcome_unknown"

    rows = publications(workspace, draft["id"])
    assert len(rows) == 1
    assert rows[0]["status"] == "unknown"
    assert rows[0]["reply_comment_id"] is None
    assert rows[0]["resolved"] is True
    # The draft is not stuck busy, but it is also not offered a retry.
    assert draft_row(workspace, draft["id"])["publication_token"] is None

    again = publish(workspace, draft)
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "publication_unresolved"
    # The insert ran once. Two send attempts would mean two public replies.
    assert len(sends) == 1
    assert len(publications(workspace, draft["id"])) == 1


def test_a_provider_server_error_is_unknown_rather_than_failed(workspace, monkeypatch):
    draft = approved(workspace)
    sends = []

    def drops(*args, **kwargs):
        sends.append(1)
        return _insert_service_raising(_http_error(503, ["backendError"]))(*args, **kwargs)

    _real_insert(workspace, monkeypatch, build=drops)

    assert publish(workspace, draft).status_code == 502
    rows = publications(workspace, draft["id"])
    assert len(rows) == 1 and rows[0]["status"] == "unknown"
    assert publish(workspace, draft).status_code == 409
    assert len(sends) == 1


def test_a_2xx_without_a_reply_id_is_recorded_unknown(workspace, monkeypatch):
    """A success the user cannot be shown is not treated as a confirmed post."""
    draft = approved(workspace)

    class Request:
        def execute(self, num_retries=None):
            return {"kind": "youtube#comment"}

    class Comments:
        def insert(self, **parameters):
            return Request()

    class Service:
        def comments(self):
            return Comments()

    _real_insert(workspace, monkeypatch,
                 build=lambda *a, **k: Service())

    assert publish(workspace, draft).status_code == 502
    rows = publications(workspace, draft["id"])
    assert len(rows) == 1
    assert rows[0]["status"] == "unknown"
    assert rows[0]["reply_comment_id"] is None
    assert publish(workspace, draft).status_code == 409


def test_repeating_a_confirmation_adds_no_second_row_and_no_second_insert(
        workspace, inserts):
    draft = approved(workspace)
    bodies = [publish(workspace, draft).json() for _ in range(3)]

    assert bodies[0] == bodies[1] == bodies[2]
    assert len(inserts.calls) == 1
    assert len(publications(workspace, draft["id"])) == 1


def test_a_confirmation_for_the_previous_revision_is_refused_after_an_edit(
        workspace, inserts):
    """An approval is for one exact reply, and an edit spends it.

    The browser can only confirm what it displayed. If the reply changed underneath it,
    the stale confirmation must not publish the newer text under the older approval.
    """
    draft = approved(workspace)
    edited = post(workspace, draft, "edit", text="A completely different reply").json()
    assert edited["revision"] == 2

    row = draft_row(workspace, draft["id"])
    assert row["decision"] == "pending", "saving an edit must clear the approval"
    assert row["review"] == "pending", "saving an edit must clear the completed review"
    assert row["text"] == "A completely different reply"

    stale = workspace.client.post(
        f'/api/drafts/{draft["id"]}/publication', headers=workspace.headers,
        json={"revision": 1, "text": draft["text"]})
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "stale_draft"

    # Carrying the new revision is not a way around it either. The edit cleared both the
    # approval and the completed review, and the review gate is checked first, so the
    # draft is not publishable at all until it is reviewed and approved again.
    fresh = workspace.client.post(
        f'/api/drafts/{draft["id"]}/publication', headers=workspace.headers,
        json={"revision": 2, "text": edited["text"]})
    assert fresh.status_code == 409
    assert fresh.json()["detail"]["code"] == "review_required"

    assert inserts.calls == []
    assert publications(workspace, draft["id"]) == []


def test_a_held_publish_claim_blocks_a_second_attempt(workspace, inserts):
    draft = approved(workspace)
    # A claim left behind by a process that stopped before it could settle.
    _set_draft(workspace, draft["id"], operation_token="held", publication_token="held")

    response = publish(workspace, draft)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "draft_conflict"
    assert inserts.calls == []
    assert publications(workspace, draft["id"]) == []


def test_two_concurrent_confirmations_leave_one_row_and_one_reply(
        workspace, inserts, monkeypatch):
    draft = approved(workspace)
    started, release = threading.Event(), threading.Event()

    async def slow(_credentials, parent_comment_id, text):
        inserts.calls.append((parent_comment_id, text))
        started.set()
        await asyncio.to_thread(release.wait, 5)
        return "reply-comment-id"
    monkeypatch.setattr("app.routers.publication.insert_comment_reply", slow)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(publish, workspace, draft)
        assert started.wait(5)
        second = pool.submit(publish, workspace, draft)
        second_code = second.result().status_code
        release.set()
        first_code = first.result().status_code

    assert (first_code, second_code) == (200, 409)
    assert len(inserts.calls) == 1
    rows = publications(workspace, draft["id"])
    assert len(rows) == 1
    assert rows[0]["status"] == "posted"
    assert draft_row(workspace, draft["id"])["publication_token"] is None


def test_an_unknown_attempt_cannot_be_reconciled_into_a_second_post(
        workspace, inserts, monkeypatch):
    """Reconciling means checking YouTube, not resending.

    A person who has looked at the video and confirmed nothing was posted still cannot
    make this application resend, because the record of the earlier attempt is exactly
    what makes a second reply preventable. A new draft is the deliberate way forward.
    """
    draft = approved(workspace)
    inserts.result(uncertain())
    assert publish(workspace, draft).status_code == 502

    for _ in range(3):
        response = publish(workspace, draft)
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "publication_unresolved"

    assert len(inserts.calls) == 1
    rows = publications(workspace, draft["id"])
    assert len(rows) == 1 and rows[0]["status"] == "unknown"



# --- durable per-comment duplicate protection ----------------------------------
#
# reply_publications is scoped to a draft and cascades with it, so a successful post could
# be forgotten by deleting the draft, and a fresh draft would then post a second reply to
# the same comment. published_reply_guards is keyed by the external publication identity
# instead and carries no foreign key, so it survives that deletion. These tests drive the
# real ownership, claim, and settle paths and read every row back out of the database.
#
# Only insert_comment_reply is replaced. The guard, the claim, the uniqueness, the
# eligibility, and the deletion endpoint are all production code.


def guards(w, channel_id=None, parent_comment_id=None):
    """Retained guards, read straight from the database."""
    async def read():
        async with w.sessions() as db:
            query = select(PublishedReplyGuard).order_by(PublishedReplyGuard.created_at)
            if channel_id is not None:
                query = query.where(PublishedReplyGuard.channel_id == channel_id)
            if parent_comment_id is not None:
                query = query.where(
                    PublishedReplyGuard.parent_comment_id == parent_comment_id)
            rows = (await db.execute(query)).scalars().all()
            return [{
                "channel_id": row.channel_id,
                "parent_comment_id": row.parent_comment_id,
                "status": row.status, "reply_comment_id": row.reply_comment_id,
                "draft_id": row.draft_id, "publication_id": row.publication_id,
                "created": row.created_at is not None,
            } for row in rows]
    return asyncio.run(read())


def guard_names():
    return {column.name for column in PublishedReplyGuard.__table__.columns}


def delete_retained(w):
    """Run the real permanent-deletion endpoint for this channel."""
    return w.client.delete("/api/drafts", headers=w.headers)


def test_a_successful_publish_writes_durable_evidence(workspace, inserts):
    draft = approved(workspace)
    assert publish(workspace, draft).status_code == 200

    rows = guards(workspace, channel_id="channel", parent_comment_id="comment")
    assert len(rows) == 1
    assert rows[0]["status"] == "posted"
    assert rows[0]["reply_comment_id"] == "reply-comment-id"
    assert rows[0]["draft_id"] == draft["id"]
    # The guard names the attempt that owns it, so settle() can release only its own row.
    assert rows[0]["publication_id"] == publications(workspace, draft["id"])[0]["id"]
    assert rows[0]["created"] is True


def test_the_retained_evidence_holds_no_reply_text(workspace):
    """Keeping a guard after the draft is deleted must not mean keeping the content."""
    names = guard_names()
    assert "text" not in names
    # Just enough to identify the external publication and to audit where it came from.
    assert {"channel_id", "parent_comment_id", "reply_comment_id", "status",
            "created_at"} <= names


def test_deleting_a_posted_draft_keeps_the_guard_and_blocks_a_second_reply(
        workspace, inserts):
    """The point of the change: the evidence outlives the draft."""
    draft = approved(workspace)
    assert publish(workspace, draft).status_code == 200
    assert len(inserts.calls) == 1

    assert delete_retained(workspace).status_code == 204

    # The draft and its publication row really are gone.
    assert publications(workspace, draft["id"]) == []
    assert workspace.client.get(
        f'/api/drafts/{draft["id"]}', headers=workspace.headers).status_code == 404

    # The safety evidence is not gone.
    rows = guards(workspace, channel_id="channel", parent_comment_id="comment")
    assert len(rows) == 1
    assert rows[0]["status"] == "posted"
    assert rows[0]["reply_comment_id"] == "reply-comment-id"

    # A new draft for the same comment is still allowed to exist and be approved:
    # deletion stays available, and the refusal is a clear state, not a broken panel.
    again = approved(workspace)
    assert again["comment_id"] == draft["comment_id"]
    eligibility = state_of(workspace, again["id"])["eligibility"]
    assert eligibility["eligible"] is False
    assert eligibility["blocked_code"] == "already_replied"
    assert "already has a reply published" in eligibility["blocked_message"]

    response = publish(workspace, again)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "already_replied"
    # Nothing was sent, so no second reply can have been created.
    assert len(inserts.calls) == 1


def test_a_guard_does_not_block_an_unrelated_comment_on_the_same_channel(
        workspace, inserts):
    """The key is the pair, so one replied comment must not silence the channel."""
    first = approved(workspace)
    assert publish(workspace, first).status_code == 200

    other = approved(workspace, "a-different-comment")
    eligibility = state_of(workspace, other["id"])["eligibility"]
    assert eligibility["eligible"] is True
    assert publish(workspace, other).status_code == 200

    assert len(inserts.calls) == 2
    assert len(guards(workspace, channel_id="channel")) == 2


def test_a_definitive_refusal_leaves_no_tombstone(workspace, inserts):
    """A refusal created nothing, so it must not become a permanent block."""
    draft = approved(workspace)
    inserts.result(youtube_api.YouTubePublishFailure(
        "reply_not_allowed", "YouTube does not allow a reply to that comment.",
        409, "rejected"))
    assert publish(workspace, draft).status_code == 409
    assert guards(workspace, channel_id="channel", parent_comment_id="comment") == []

    # The deliberate retry is still free, on the same draft and with no manual cleanup.
    inserts.result("reply-comment-id")
    assert publish(workspace, draft).status_code == 200
    assert len(guards(workspace, channel_id="channel",
                      parent_comment_id="comment")) == 1


def test_an_unknown_outcome_cannot_be_erased_by_deleting(workspace, inserts):
    draft = approved(workspace)
    inserts.result(uncertain())
    assert publish(workspace, draft).status_code == 502

    # Retained work still cannot be deleted while an outcome is unresolved.
    refused = delete_retained(workspace)
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "publication_unresolved"

    rows = guards(workspace, channel_id="channel", parent_comment_id="comment")
    assert len(rows) == 1 and rows[0]["status"] == "unknown"
    assert rows[0]["reply_comment_id"] is None

    # The ambiguity is reported as unknown, not as a confirmed post, so it is never
    # retried and never claimed to have succeeded.
    assert publish(workspace, draft).status_code == 409
    assert len(inserts.calls) == 1


def test_the_key_is_channel_and_comment_not_the_comment_alone(workspace, inserts):
    """Two channels holding the same comment id must not collide."""
    draft = approved(workspace)
    assert publish(workspace, draft).status_code == 200

    async def other_channel():
        async with workspace.sessions() as db:
            row = (await db.execute(select(ReplyDraft)
                                    .where(ReplyDraft.id == draft["id"]))).scalars().one()
            db.add(PublishedReplyGuard(
                channel_id="channel-two", parent_comment_id="comment", status="posted",
                reply_comment_id="reply-on-another-channel"))
            await db.commit()
            return row.channel_id
    original = asyncio.run(other_channel())
    assert original == "channel"

    # Both rows exist, which is only possible because channel_id is part of the key.
    rows = guards(workspace, parent_comment_id="comment")
    assert len(rows) == 2
    assert {row["channel_id"] for row in rows} == {"channel", "channel-two"}

    # And the lookup for this channel returns its own row, not the other one.
    async def lookup():
        async with workspace.sessions() as db:
            found = await publishing.guard_for(db, "channel", "comment")
            return found.channel_id, found.reply_comment_id
    assert asyncio.run(lookup()) == ("channel", "reply-comment-id")


def test_the_database_refuses_a_second_reservation_for_one_comment(workspace, inserts):
    """The unique index is the authority, not an application-level check."""
    draft = approved(workspace)

    async def competing_reservations():
        async with workspace.sessions() as db:
            for index in range(2):
                db.add(PublishedReplyGuard(
                    channel_id="channel", parent_comment_id="comment", status="sending",
                    draft_id=draft["id"], publication_id=f"publication-{index}"))
            await db.commit()

    with pytest.raises(IntegrityError) as raised:
        asyncio.run(competing_reservations())
    assert "published_reply_guards" in str(raised.value)


def test_two_concurrent_publishes_against_a_retained_guard_send_nothing(
        workspace, inserts, monkeypatch):
    """Concurrency must not become a way around the retained evidence."""
    draft = approved(workspace)
    assert publish(workspace, draft).status_code == 200
    assert delete_retained(workspace).status_code == 204
    assert len(inserts.calls) == 1

    again = approved(workspace)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = [future.result() for future in
                   [pool.submit(publish, workspace, again) for _ in range(4)]]

    assert {response.status_code for response in results} == {409}
    assert {response.json()["detail"]["code"] for response in results} == {"already_replied"}
    # Still exactly one insert for the whole lifetime of this comment.
    assert len(inserts.calls) == 1
    assert len(guards(workspace, channel_id="channel",
                      parent_comment_id="comment")) == 1
