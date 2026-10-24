"""Telegram webhook request checks. Pure: no I/O, no Workers runtime imports.

Every check must pass before the Worker reads a request body; anything that fails
gets a plain 404, so the endpoint looks like it doesn't exist.

- Secret header: Telegram sends setWebhook's secret_token back on every delivery as
  X-Telegram-Bot-Api-Secret-Token. Compared in constant time.
- Path: derived from the same secret, so only someone who knows it can find it.
- POST with a JSON body under MAX_BODY_BYTES.
- Client IP in Telegram's published webhook ranges (defense in depth).
"""

import hashlib
import hmac
import re
from ipaddress import ip_address, ip_network

MAX_BODY_BYTES = 64 * 1024
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"  # noqa: S105 - a header name, not a secret
# https://core.telegram.org/bots/webhooks: webhook requests come from these ranges.
TELEGRAM_NETWORKS = (ip_network("149.154.160.0/20"), ip_network("91.108.4.0/22"))
# Telegram allows 1-256 of [A-Za-z0-9_-] for secret_token; we insist on a long one.
_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{32,256}$")


def valid_secret(secret):
    return isinstance(secret, str) and bool(_SECRET_RE.match(secret))


def webhook_path(secret):
    """Unguessable path derived from the secret: /telegram/<32 hex chars>."""
    digest = hashlib.sha256(b"boiler-guard webhook path\0" + secret.encode()).hexdigest()
    return f"/telegram/{digest[:32]}"


def is_telegram_ip(value):
    try:
        address = ip_address((value or "").strip())
    except ValueError:
        return False
    return any(address in network for network in TELEGRAM_NETWORKS)


def authorized(method, path, header_secret, content_type, content_length, client_ip, secret, check_ip=True):
    """True only for a request that can be a genuine Telegram webhook delivery."""
    if not valid_secret(secret) or method != "POST":
        return False
    if not hmac.compare_digest(str(path or "").encode(), webhook_path(secret).encode()):
        return False
    if not isinstance(header_secret, str) or not hmac.compare_digest(header_secret.encode(), secret.encode()):
        return False
    if (content_type or "").split(";")[0].strip().lower() != "application/json":
        return False
    try:
        length = int(content_length)
    except (TypeError, ValueError):
        return False  # Telegram always sends Content-Length
    if not 0 < length <= MAX_BODY_BYTES:
        return False
    return is_telegram_ip(client_ip) if check_ip else True


def expired(expires_at, now, required=False):
    """True when a deployment with EXPIRES_AT (epoch seconds) has run out. With
    required (the test environment), a missing or broken value counts as expired,
    so a test Worker deployed without an expiry never runs."""
    if expires_at in (None, ""):
        return required
    try:
        return now >= float(expires_at)
    except (TypeError, ValueError):
        return True


def cooled_down(last, now, seconds):
    """True when at least `seconds` have passed since `last` (epoch, may be None)."""
    try:
        return last is None or now - float(last) >= seconds
    except (TypeError, ValueError):
        return True
