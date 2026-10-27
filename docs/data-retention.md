# Data retention

Drafts and their usage history are retained per **channel**, not per browser session and
not per OAuth credential. This document states what is kept, what ends access, and what
destroys data.

## Why ownership moved

`reply_drafts` used to cascade from two parents, both `ON DELETE CASCADE`:

- `app_sessions.session_hash`
- `youtube_connections.id`

That made ordinary, unremarkable events destructive. Disconnecting removed the
connection and took every draft and usage row with it. Logging out removed the session
and did the same. An expired session was deleted by routine cleanup and did the same. In
each case the user's work disappeared as a side effect of managing a credential, with no
warning and no way to get it back.

Ownership now lives in `channel_owners`: one row per channel, created when a channel is
first verified, and reachable only through a server-side OAuth check. Sessions and
credentials no longer own draft lifetime.

## What is retained, and for how long

| Event | Credential | Access | Drafts and usage |
| --- | --- | --- | --- |
| Reconnect to the same verified channel | new credential issued | restored | retained |
| Disconnect | removed | ends | **retained** |
| Logout | removed | ends | **retained** |
| Session expiry | removed | ends | **retained** |
| Permanent deletion | untouched | ends | **deleted** |

There is no time limit and no expiry sweep over drafts. Retention ends only through the
explicit permanent-deletion action.

`reply_publications` is retained on the same terms as the draft it belongs to, because it
is the only durable record of whether a reply became public. It stores a snapshot of the
text and revision that were sent, the resulting reply comment ID when YouTube confirmed
one, and a sanitized failure otherwise. It never stores a credential. The record is the
reason a `sending` or `unknown` attempt blocks deletion: removing the only evidence that a
reply may already be public would make an accidental double reply untraceable.

## How access is decided

A request reaches a draft only by resolving, in order:

1. the caller's server-side session;
2. the OAuth-verified connection belonging to that session, which exists only after a
   completed OAuth flow and a `channels.list` call that proved channel control;
3. the `channel_owners` row for **that connection's** channel;
4. the draft, matched on `channel_owner_id`.

Consequences worth stating plainly:

- **A channel id supplied by a client is never used for authorization.** The channel is
  read from the verified connection, so guessing or passing a channel id grants nothing.
- **A draft id alone is not access.** Requests are scoped to the caller's own channel
  owner, so another channel's draft id returns `404`, not the record.
- **Disconnecting really does end access.** With no connection there is no verified
  channel, so every draft route returns `404` until the same channel is reconnected.
  Retained data is not readable while disconnected; it is preserved, not exposed.
- **Two sessions that both completed OAuth for the same channel share one owner** and
  therefore the same drafts. That is the intended meaning of channel ownership: the
  channel is the principal, not the browser.
- **A different channel cannot reach the records**, even on the same browser, because it
  resolves to a different owner.

## Permanent deletion

`DELETE /api/drafts` is the only destructive path, and it is deliberately separate from
disconnecting. It requires an authenticated session **and** a CSRF token **and** a
verified channel, and it deletes the caller's own `channel_owners` row, which cascades to
that channel's drafts and their usage rows. It leaves the OAuth credential alone.

A later reconnect to the same channel creates a fresh owner and starts from an empty
history; it does not resurrect deleted work.

Deletion is refused with a conflict while the channel has any publication in `sending` or
`unknown`, because those attempts may already be public. `posted` and `failed` are settled
outcomes, so they do not block deletion; their rows cascade with the draft.

## What deletion does not delete

Deleting the draft removes the reply text, the review, the approval, the refinement
conversation, and the `reply_publications` attempt row. It deliberately does **not**
remove the record that ReplyAI already replied to a given comment.

`published_reply_guards` holds one row per connected channel and parent comment, with no
foreign key to either a draft or a channel owner, precisely so that deleting retained work
cannot erase it. It keeps only what safety and audit need:

- `channel_id`, the channel whose grant created the reply
- `parent_comment_id`, the comment replied to
- `reply_comment_id`, the public reply YouTube returned
- `status`, one of `posted`, `unknown`, or `sending`
- `draft_id` and `publication_id`, opaque audit pointers
- timestamps

It holds **no reply text and no video id**, and it can be read only by the owner of the
channel it names. This is a deliberate trade: enforcing duplicate protection after
deletion must not mean retaining the user's content after they asked for it to be deleted.

`unknown` guards are retained for the same reason and are never resolvable by deletion,
because an unresolved attempt cannot be distinguished from a reply that did land.

`failed` attempts never create a guard. A definitive refusal created nothing, so its
reservation is released and a deliberate retry remains possible.

A guard row therefore has no deletion path in the interface. That is intentional: it is
the durable half of the duplicate boundary, and `comments.insert` accepts no idempotency
key, so if ReplyAI forgot it had already replied it would post a second public reply with
nothing to stop it. Clearing a guard by hand requires deleting from the database directly
and would be an unlogged, unaudited act.

This was put to the owner explicitly as a data-retention tradeoff — permanent rows, or a
purge path — and permanent retention was chosen. The accepted cost is that local guard
rows accumulate, one per channel-and-comment ReplyAI has replied to, and cannot be purged
through the application. That is the price of the guarantee, not an oversight. If a purge
path is ever added it should be admin-only, audited, and refused for `unknown` and
`sending` guards.

## Migration

Migration `20260927_05` moved ownership from sessions and credentials to
`channel_owners` without deleting data:

- it creates `channel_owners` and backfills one row per connected channel;
- it adds `reply_drafts.channel_owner_id`, attributing each draft to the channel of its
  own connection, and **refuses to run** if any draft cannot be attributed;
- it replaces the per-connection unique constraint `(connection_id, comment_id)` with
  `(channel_owner_id, comment_id)`, so reconnecting cannot create a second draft for a
  retained comment.

Its downgrade refuses to run if a retained draft has no current connection, because the
old model would drop it. Prefer forward fixes, and exercise any downgrade on a disposable
database first.
