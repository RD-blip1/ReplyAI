import json

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


class EncryptionNotConfigured(Exception):
    pass


class StoredTokenInvalid(Exception):
    pass


def token_cipher() -> Fernet:
    key = settings.token_encryption_key.get_secret_value()
    if not key:
        raise EncryptionNotConfigured
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError, UnicodeEncodeError) as exc:
        raise EncryptionNotConfigured from exc


def encrypt_tokens(tokens: dict[str, str | None]) -> str:
    serialized = json.dumps(tokens, separators=(",", ":")).encode("utf-8")
    return token_cipher().encrypt(serialized).decode("ascii")


def decrypt_tokens(encrypted: str) -> dict[str, str | None]:
    try:
        decoded = token_cipher().decrypt(encrypted.encode("ascii"))
        result = json.loads(decoded)
    except (InvalidToken, ValueError, TypeError, UnicodeEncodeError, json.JSONDecodeError) as exc:
        raise StoredTokenInvalid from exc
    if not isinstance(result, dict):
        raise StoredTokenInvalid
    return result