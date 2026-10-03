"""Secrets live only in Windows Credential Manager (keyring service "ReelEngine").

Nothing in this module writes a secret to disk, a log, the DB or OneDrive. Every
value read is registered with the log redactor. Token *metadata* (issued/expiry
dates, never the token) is kept in the DB table token_state.
"""
from __future__ import annotations

import os

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from .log import get_logger, register_secret

SERVICE = os.environ.get("REELENGINE_KEYRING_SERVICE", "ReelEngine")  # tests use a separate service
ACCESS_TOKEN = "ig_access_token"  # nosec B105 - the NAME of a keyring entry, not a secret
APP_SECRET = "ig_app_secret"  # nosec B105 - keyring entry name
TELEGRAM_TOKEN = "telegram_bot_token"  # nosec B105 - keyring entry name
KNOWN_NAMES = (ACCESS_TOKEN, APP_SECRET, TELEGRAM_TOKEN)

log = get_logger("secrets")


class SecretMissing(Exception):
    pass


def get_secret(name: str, required: bool = True) -> str | None:
    try:
        value = keyring.get_password(SERVICE, name)
    except KeyringError as exc:
        log.error("keyring read failed for %s: %s", name, exc)
        value = None
    if value:
        register_secret(value)
        return value
    if required:
        raise SecretMissing(f"secret '{name}' is not in Windows Credential Manager (service {SERVICE}). Run `reel connect`.")
    return None


def set_secret(name: str, value: str) -> None:
    if name not in KNOWN_NAMES:
        raise ValueError(f"unknown secret name {name!r}; allowed: {', '.join(KNOWN_NAMES)}")
    value = value.strip()
    if not value:
        raise ValueError("empty secret")
    register_secret(value)
    keyring.set_password(SERVICE, name, value)
    log.info("secret %s stored in keyring", name)


def has_secret(name: str) -> bool:
    try:
        return bool(keyring.get_password(SERVICE, name))
    except KeyringError:
        return False


def delete_secret(name: str) -> bool:
    try:
        keyring.delete_password(SERVICE, name)
        return True
    except (PasswordDeleteError, KeyringError):
        return False


def backend_name() -> str:
    kr = keyring.get_keyring()
    return f"{type(kr).__module__}.{type(kr).__name__}"
