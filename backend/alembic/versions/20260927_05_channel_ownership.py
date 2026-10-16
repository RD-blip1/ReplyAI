"""Give drafts a durable channel owner that outlives sessions and credentials.

Browser sessions and OAuth credential records were both cascading parents of
``reply_drafts``, so an ordinary session expiry, disconnect, or logout destroyed the
user's drafts and usage history along with the credential that happened to be nearby.
Ownership now lives in ``channel_owners``, one row per channel, which is reached only
after a server-side OAuth check has proven the caller controls that channel. Sessions
and connections stop owning draft lifetime; the only path that deletes retained work is
deleting the channel owner, which the permanent-deletion action does explicitly.

Ownership is migrated, not recreated: every existing draft is re-pointed at the owner of
the channel it was written for, so no draft or usage row is dropped. A draft unique on
its comment per owner replaces the old per-connection constraint, so a reconnect can no
longer produce a second draft for a comment that is already retained.
"""
import sqlalchemy as sa

from alembic import op

revision = "20260927_05"
down_revision = "20260927_04"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "channel_owners",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("channel_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_id", name="uq_channel_owners_channel_id"),
    )
    op.create_index("ix_channel_owners_channel_id", "channel_owners", ["channel_id"])

    # One owner per channel already present. Drafts are matched to their own channel
    # rather than to whichever connection happens to exist at migration time.
    connection = op.get_bind()
    channels = connection.execute(
        sa.text("SELECT DISTINCT channel_id FROM youtube_connections")
    ).scalars().all()
    for channel_id in channels:
        connection.execute(
            sa.text(
                "INSERT INTO channel_owners (id, channel_id, created_at) "
                "VALUES (gen_random_uuid()::text, :channel_id, now())"
            ),
            {"channel_id": channel_id},
        )

    op.add_column("reply_drafts", sa.Column("channel_owner_id", sa.String(length=36)))
    connection.execute(
        sa.text(
            "UPDATE reply_drafts AS d SET channel_owner_id = o.id "
            "FROM youtube_connections AS c JOIN channel_owners AS o "
            "  ON o.channel_id = c.channel_id "
            "WHERE c.id = d.connection_id"
        )
    )
    # A draft whose connection no longer exists cannot be attributed to a channel, and
    # leaving it unattributed would make it unreachable rather than retained. Refuse
    # the migration instead of quietly orphaning a user's work.
    orphans = connection.execute(
        sa.text("SELECT count(*) FROM reply_drafts WHERE channel_owner_id IS NULL")
    ).scalar()
    if orphans:
        raise RuntimeError(
            f"refusing to migrate: {orphans} draft(s) have no resolvable channel owner; "
            "reconnect that channel first so ownership can be established"
        )

    op.create_foreign_key(
        "fk_reply_drafts_channel_owner", "reply_drafts", "channel_owners",
        ["channel_owner_id"], ["id"], ondelete="CASCADE",
    )
    op.create_index("ix_reply_drafts_channel_owner_id", "reply_drafts", ["channel_owner_id"])
    op.alter_column("reply_drafts", "channel_owner_id", nullable=False)

    op.drop_constraint("uq_draft_comment", "reply_drafts", type_="unique")
    op.create_unique_constraint(
        "uq_draft_comment", "reply_drafts", ["channel_owner_id", "comment_id"]
    )

    # Sessions and credentials stop owning draft lifetime.
    op.drop_constraint("reply_drafts_session_hash_fkey", "reply_drafts", type_="foreignkey")
    op.drop_constraint("reply_drafts_connection_id_fkey", "reply_drafts", type_="foreignkey")
    op.drop_index("ix_reply_drafts_session_hash", table_name="reply_drafts")
    op.drop_index("ix_reply_drafts_connection_id", table_name="reply_drafts")
    op.drop_column("reply_drafts", "session_hash")
    op.drop_column("reply_drafts", "connection_id")


def downgrade():
    op.add_column("reply_drafts", sa.Column("connection_id", sa.String(length=36)))
    op.add_column("reply_drafts", sa.Column("session_hash", sa.String(length=256)))
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE reply_drafts AS d SET connection_id = c.id, session_hash = c.session_hash "
            "FROM youtube_connections AS c "
            "WHERE c.channel_id = d.channel_id"
        )
    )
    unowned = connection.execute(
        sa.text("SELECT count(*) FROM reply_drafts WHERE connection_id IS NULL")
    ).scalar()
    if unowned:
        raise RuntimeError(
            f"cannot downgrade: {unowned} retained draft(s) have no current connection, "
            "and restoring per-connection ownership would drop them"
        )
    op.create_foreign_key(
        "reply_drafts_connection_id_fkey", "reply_drafts", "youtube_connections",
        ["connection_id"], ["id"], ondelete="CASCADE",
    )
    op.create_foreign_key(
        "reply_drafts_session_hash_fkey", "reply_drafts", "app_sessions",
        ["session_hash"], ["session_hash"], ondelete="CASCADE",
    )
    op.create_index("ix_reply_drafts_connection_id", "reply_drafts", ["connection_id"])
    op.create_index("ix_reply_drafts_session_hash", "reply_drafts", ["session_hash"])
    op.alter_column("reply_drafts", "session_hash", nullable=False)
    op.alter_column("reply_drafts", "connection_id", nullable=False)

    op.drop_constraint("uq_draft_comment", "reply_drafts", type_="unique")
    op.create_unique_constraint(
        "uq_draft_comment", "reply_drafts", ["connection_id", "comment_id"]
    )
    op.drop_index("ix_reply_drafts_channel_owner_id", table_name="reply_drafts")
    op.drop_constraint("fk_reply_drafts_channel_owner", "reply_drafts", type_="foreignkey")
    op.drop_column("reply_drafts", "channel_owner_id")
    op.drop_index("ix_channel_owners_channel_id", table_name="channel_owners")
    op.drop_table("channel_owners")
