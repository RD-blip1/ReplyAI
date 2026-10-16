# Draft Retention

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

`20260927_05` migrates ownership rather than recreating it:

- creates `channel_owners` and backfills one row per channel already connected;
- adds `reply_drafts.channel_owner_id` and backfills it by matching each draft to the
  channel of its own connection, so no draft is reassigned to the wrong channel;
- **refuses to run** if any draft cannot be attributed to a channel, rather than quietly
  orphaning a user's work;
- replaces the per-connection unique constraint on `(connection_id, comment_id)` with
  `(channel_owner_id, comment_id)`, so reconnecting can no longer create a second draft
  for a comment that is already retained;
- drops `session_hash` and `connection_id` from `reply_drafts`.

No draft or usage row is deleted by the migration. On the local database it moved 1 draft
onto 1 owner with 0 unowned.

The `downgrade` re-establishes per-connection ownership and **refuses to run** if any
retained draft has no current connection, because restoring the old model would drop it.

**The downgrade has not been executed against the live database, by deliberate choice.**
It was left unverified rather than tested, because on this deployment it would drop and
recreate the `channel_owners` rows and restore the two cascading foreign keys — a
destructive round trip against the only copy of the data, with no scratch database to run
it in. Treat it as untested code: read it before relying on it, and prefer a forward fix.
A safe first exercise would be a throwaway database, not this one.

`20260927_07` adds `reply_publications` plus `reply_drafts.publication_token`. It is
purely additive and drops no existing column, so its downgrade removes only the new table
and column. It was verified by upgrade, downgrade to `20260927_06`, and re-upgrade on a
disposable PostgreSQL database before being applied to the live database, which was backed
up first. SQLite cannot run the migration series from scratch because `20260927_05` alters
a constraint; that is a pre-existing limitation, not a new one, and PostgreSQL is the
authoritative check for this project.

## Backups

Backups are logical dumps taken by a script rather than by the PostgreSQL client tools.
They are stored outside the repository, directory mode `0700`, file mode `0600`, because
they contain encrypted credential material.

| Backup | Taken | Location |
| --- | --- | --- |
| `replyai-20260927T175602` | before the isolation refactor | `~/.local/share/replyai/backups/` |
| `replyai-20260927T181832` | before migration `20260927_05` | `~/.local/share/replyai/backups/` |
| `replyai-20260927T183729` | after `20260927_05`, before refinement | `~/.local/share/replyai/backups/` |
| `replyai-20260927T184644` | superseded schema, after `20260927_06` | `~/.local/share/replyai/backups/` |
| `replyai-20260927T193529` | current schema, after `20260927_06`, before `20260927_07` | `~/.local/share/replyai/backups/` |

Each copy was verified against its source file by SHA-256 before the source was trusted,
and each archive was confirmed readable: the manifest parses, every table's row count
matches the manifest, and every column named in the data files exists in the recorded
schema. No restore was performed against the development database.

`replyai-20260927T175602` and `replyai-20260927T181832` predate migration `20260927_05`, so
neither contains the `channel_owners` table or the `channel_owner_id` column. Restoring
either would return the database to the old, cascade-prone ownership model, so they are
kept only as evidence of the earlier state.

`replyai-20260927T193529` is the current backup and the pre-publishing one. It records
migration `20260927_06` and covers all six pre-existing application tables with SHA-256
`658a92648ffd6103d2633fd5a39cb874e1b07295c49547d1c9217ff2fa9cb896`. Restoring it returns
the database to the state before any reply could be published.

`replyai-20260927T183729` is superseded but retained as the last pre-refinement state.
The manifest digest covers each archived file's name and bytes and excludes the manifest
itself, since a manifest cannot contain its own digest.

Restoring the current backup is a deliberate manual step: the recorded migration version
should be brought to the target application's Alembic head before use.

## Tests

`tests/test_retention.py` covers retention across disconnect, logout, and session expiry;
access restoration after reconnecting the same verified channel; rejection of a different
channel, of an unauthenticated caller, and of a CSRF-less permanent delete; the
permanence of the explicit delete; and that ownership is per channel rather than per
session. All OAuth and provider responses are mocked, and no test contacts a real channel.

`tests/test_isolation.py` covers the separate guarantee that the test suite cannot reach
the development database.

One testing detail is load-bearing: SQLite ignores `ON DELETE CASCADE` unless
`PRAGMA foreign_keys=ON` is set, so the test harness enables it. Without that, these tests
would pass while the cascade that destroys retained work never actually ran.

`tests/test_publication.py` additionally covers the guarantee that an unresolved
`sending` or `unknown` attempt blocks permanent deletion, while `posted` and `failed` do
not. It asserts the refusal on an isolated temporary database; the concurrency cases use
their own database rather than the development one.
