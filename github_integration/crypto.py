from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


PREFIX = "fernet:"


def _get_fernet():
    key = settings.GITHUB_TOKEN_ENCRYPTION_KEY

    if not key:
        raise RuntimeError(
            "GITHUB_TOKEN_ENCRYPTION_KEY is not configured."
        )

    if isinstance(key, str):
        key = key.encode()

    return Fernet(key)


def encrypt_github_token(token):
    if not token:
        return token

    # Prevent accidental double encryption.
    if token.startswith(PREFIX):
        return token

    encrypted = _get_fernet().encrypt(
        token.encode()
    ).decode()

    return PREFIX + encrypted


def decrypt_github_token(value):
    if not value:
        return value

    # Temporary compatibility with existing plaintext tokens.
    # We will migrate the existing database token next.
    if not value.startswith(PREFIX):
        return value

    encrypted = value[len(PREFIX):]

    try:
        return _get_fernet().decrypt(
            encrypted.encode()
        ).decode()
    except InvalidToken:
        raise ValueError(
            "Unable to decrypt GitHub access token."
        )