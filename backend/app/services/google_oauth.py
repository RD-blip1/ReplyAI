from collections.abc import Sequence
from pathlib import Path
from typing import Any

from google_auth_oauthlib.flow import Flow

from app.config import settings
from app.security import EncryptionNotConfigured, token_cipher

YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
YOUTUBE_FORCE_SSL_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
GOOGLE_REDIRECT_URI = settings.google_redirect_uri
OAUTH_SCOPES = (YOUTUBE_READONLY_SCOPE,)
YOUTUBE_PERMISSION_UPGRADE_SCOPES = (YOUTUBE_READONLY_SCOPE, YOUTUBE_FORCE_SSL_SCOPE)

# YouTube's current discovery schema requires youtube.force-ssl for
# commentThreads.list and comments.list. A youtube.readonly grant is not sufficient
# for comment retrieval, even though it remains sufficient for video listing.
YOUTUBE_COMMENT_READ_SCOPES = frozenset({YOUTUBE_FORCE_SSL_SCOPE})


def has_comment_read_scope(scopes: str | None) -> bool:
    """Whether the granted scopes allow reading comments (readonly or force-ssl)."""
    return bool(set((scopes or "").split()) & YOUTUBE_COMMENT_READ_SCOPES)


class OAuthSetupRequired(Exception):
    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        self.message = message
        super().__init__(reason)


def oauth_setup_issue() -> tuple[str, str] | None:
    configured_path = settings.google_oauth_client_secrets_file.strip()
    if not configured_path:
        return (
            "client_file_missing",
            "Set GOOGLE_OAUTH_CLIENT_SECRETS_FILE to the OAuth client JSON file outside this repository.",
        )
    if not Path(configured_path).expanduser().is_file():
        return (
            "client_file_unavailable",
            "The configured OAuth client JSON file is not available to the backend process.",
        )
    if not settings.token_encryption_key.get_secret_value():
        return (
            "encryption_key_missing",
            "Set TOKEN_ENCRYPTION_KEY to a locally generated Fernet key before connecting YouTube.",
        )
    try:
        token_cipher()
    except EncryptionNotConfigured:
        return (
            "encryption_key_invalid",
            "TOKEN_ENCRYPTION_KEY must be a valid Fernet key generated locally.",
        )
    return None


def require_oauth_setup() -> None:
    issue = oauth_setup_issue()
    if issue:
        raise OAuthSetupRequired(*issue)


def create_flow(
    *,
    scopes: Sequence[str] = OAUTH_SCOPES,
    state: str | None = None,
    code_verifier: str | None = None,
    autogenerate_code_verifier: bool = True,
) -> Flow:
    require_oauth_setup()
    return Flow.from_client_secrets_file(
        str(Path(settings.google_oauth_client_secrets_file).expanduser()),
        scopes=list(scopes),
        redirect_uri=GOOGLE_REDIRECT_URI,
        state=state,
        code_verifier=code_verifier,
        autogenerate_code_verifier=autogenerate_code_verifier,
    )


def client_config() -> dict[str, Any]:
    return create_flow(autogenerate_code_verifier=False).client_config