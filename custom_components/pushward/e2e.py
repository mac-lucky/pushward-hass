"""End-to-end encryption of notification contents (PushWard envelope pw1).

With a key in the integration options, the title, subtitle, body and url of every
notification are sealed here with AES-256-GCM and the server only stores and
forwards the envelope; the PushWard app opens it with the same key. Everything
else (level, sound, thread, source, media, metadata, actions) stays readable to
the server, which needs it to deliver the push.

key  = 32 random bytes, written as 64 hex characters
enc  = HKDF-SHA256(key, salt empty, info "pushward/e2e/v1/enc", 32 bytes)
kid  = hex(HKDF-SHA256(key, salt empty, info "pushward/e2e/v1/kid", 4 bytes))
wire = "pw1." + kid + "." + base64url-nopad(nonce || ciphertext || tag),
       AAD "pw1." + kid, at most 3072 characters
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import os
import re

import voluptuous as vol
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .const import validate_tap_action_url

ENVELOPE_MAX_LEN = 3072
TITLE_MAX = 256
SUBTITLE_MAX = 256
BODY_MAX = 4096

_NONCE_LEN = 12
_TAG_LEN = 16
# nonce + at least a 2-byte plaintext ("{}") + tag
_MIN_SEALED_LEN = _NONCE_LEN + 2 + _TAG_LEN
_PAD_BLOCK = 64
# The largest plaintext whose envelope still fits in ENVELOPE_MAX_LEN.
PLAINTEXT_MAX = (ENVELOPE_MAX_LEN - len("pw1.") - 8 - 1) * 3 // 4 - _NONCE_LEN - _TAG_LEN

_ENVELOPE_RE = re.compile(r"pw1\.([0-9a-f]{8})\.([A-Za-z0-9_-]{40,})")
_KEY_RE = re.compile(r"[0-9a-fA-F]{64}")
_ASCII_SPACE_RE = re.compile(r"[ \t\n\r\f\v]")


class E2EError(ValueError):
    """A key, notification or envelope the pw1 format does not accept."""


def parse_key(text: str) -> bytes:
    """The 32 key bytes from their hex form; case and whitespace do not matter."""
    compact = _ASCII_SPACE_RE.sub("", text)
    if compact.startswith(("hlk_", "hla_")):
        raise E2EError("that is an integration key, not an encryption key")
    if not _KEY_RE.fullmatch(compact):
        raise E2EError("an encryption key is 64 hexadecimal characters")
    return bytes.fromhex(compact)


def _hkdf(key: bytes, info: bytes, length: int) -> bytes:
    # salt=None is RFC 5869's all-zero salt, the same as an empty one.
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=info).derive(key)


def key_id(key: bytes) -> str:
    """The Key ID the PushWard app shows next to the same key."""
    return _hkdf(key, b"pushward/e2e/v1/kid", 4).hex()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _envelope_len(plaintext_len: int) -> int:
    sealed = _NONCE_LEN + plaintext_len + _TAG_LEN
    return len("pw1.") + 8 + 1 + -(-4 * sealed // 3)


def seal_plaintext(key: bytes, plaintext: bytes, *, nonce: bytes | None = None) -> str:
    """Encrypt an already serialized plaintext. ``nonce`` is only for test vectors."""
    kid = key_id(key)
    if nonce is None:
        nonce = os.urandom(_NONCE_LEN)
    sealed = AESGCM(_hkdf(key, b"pushward/e2e/v1/enc", 32)).encrypt(nonce, plaintext, f"pw1.{kid}".encode())
    return f"pw1.{kid}.{_b64(nonce + sealed)}"


def _fields(title: str, body: str, subtitle: str | None, url: str | None) -> dict[str, str]:
    fields = {"title": title}
    if subtitle:
        fields["subtitle"] = subtitle
    fields["body"] = body
    if url:
        fields["url"] = url
    return fields


def _serialize(fields: dict[str, str]) -> bytes:
    try:
        return json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode()
    except UnicodeEncodeError as err:
        # A lone surrogate (from a bad \ud800 escape somewhere upstream) has no UTF-8 form.
        raise E2EError("the text is not valid Unicode (it holds an unpaired surrogate)") from err


def _trim(text: str, excess: int) -> tuple[str, int]:
    """Drop code points from the end of text until their JSON bytes cover excess; one always stays."""
    end = len(text)
    while excess > 0 and end > 1:
        end -= 1
        excess -= len(_serialize({"": text[end]})) - len('{"":""}')
    return text[:end], excess


def fit(title: str, body: str, *, subtitle: str | None = None, url: str | None = None) -> tuple[str, str]:
    """title and body cut down, the body first, until the notification fits one envelope.

    For text that may be shortened rather than refused, like a to-do item's
    description: in scripts like Devanagari or CJK a code point takes three UTF-8
    bytes, so text within the plain-field limits can still be far too long to seal.
    Each keeps at least one code point, so seal() still refuses when subtitle and
    url alone do not fit.
    """
    title, body = title[:TITLE_MAX], body[:BODY_MAX]
    excess = len(_serialize(_fields(title, body, subtitle, url))) - PLAINTEXT_MAX
    if excess > 0:
        body, excess = _trim(body, excess)
        title, _ = _trim(title, excess)
    return title, body


def seal(
    key: bytes,
    *,
    title: str,
    body: str,
    subtitle: str | None = None,
    url: str | None = None,
) -> str:
    """The envelope for one notification's title, subtitle, body and url.

    Checks the same limits the server would have checked on the plain fields,
    because it cannot once they are sealed.
    """
    if not title or not body:
        raise E2EError("title and body must not be empty")
    limits = (("title", title, TITLE_MAX), ("subtitle", subtitle, SUBTITLE_MAX), ("body", body, BODY_MAX))
    for name, value, limit in limits:
        if value and len(value) > limit:
            raise E2EError(f"{name} is {len(value)} characters, at most {limit} can be encrypted")
    if url:
        try:
            validate_tap_action_url(url)
        except vol.Invalid as err:
            raise E2EError(f"url: {err}") from err

    plaintext = _serialize(_fields(title, body, subtitle, url))
    # Padding to a multiple of 64 bytes hides the exact length, unless that alone
    # would push the envelope over the cap.
    padded = plaintext + b" " * (-len(plaintext) % _PAD_BLOCK)
    if _envelope_len(len(padded)) <= ENVELOPE_MAX_LEN:
        plaintext = padded
    elif _envelope_len(len(plaintext)) > ENVELOPE_MAX_LEN:
        raise E2EError(
            f"the notification is too long to encrypt ({len(plaintext)} bytes of JSON, at most {PLAINTEXT_MAX} fit);"
            " shorten the body"
        )
    return seal_plaintext(key, plaintext)


def parse_envelope(envelope: str) -> tuple[str, bytes]:
    """Split an envelope into its kid and nonce || ciphertext || tag."""
    match = _ENVELOPE_RE.fullmatch(envelope) if len(envelope) <= ENVELOPE_MAX_LEN else None
    if match is None:
        raise E2EError("not a pw1 envelope")
    kid, encoded = match.groups()
    try:
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
    except binascii.Error as err:
        raise E2EError("the envelope is not valid base64url") from err
    # Strict: unused trailing bits must be zero, so only one spelling decodes.
    if _b64(raw) != encoded or len(raw) < _MIN_SEALED_LEN:
        raise E2EError("the envelope is not valid base64url")
    return kid, raw


def open_envelope(key: bytes, envelope: str) -> dict[str, str]:
    """Decrypt an envelope and return its fields, clamped the way the app shows them."""
    kid, raw = parse_envelope(envelope)
    if kid != key_id(key):
        raise E2EError(f"sealed with key {kid}, not this one")
    try:
        plaintext = AESGCM(_hkdf(key, b"pushward/e2e/v1/enc", 32)).decrypt(
            raw[:_NONCE_LEN], raw[_NONCE_LEN:], f"pw1.{kid}".encode()
        )
    except InvalidTag as err:
        raise E2EError("the envelope does not open with this key") from err
    try:
        fields = json.loads(plaintext.decode())
    except ValueError as err:
        raise E2EError("the envelope does not hold a notification") from err
    if not isinstance(fields, dict):
        raise E2EError("the envelope does not hold a notification")
    title, body = fields.get("title"), fields.get("body")
    if not isinstance(title, str) or not isinstance(body, str) or not title or not body:
        raise E2EError("the envelope does not hold a title and a body")
    subtitle, url = fields.get("subtitle", ""), fields.get("url", "")
    if not isinstance(subtitle, str) or not isinstance(url, str):
        raise E2EError("the envelope does not hold a notification")

    opened = {"title": title[:TITLE_MAX], "body": body[:BODY_MAX]}
    if subtitle:
        opened["subtitle"] = subtitle[:SUBTITLE_MAX]
    if url:
        # A url the server would refuse is dropped; the rest still shows.
        with contextlib.suppress(vol.Invalid):
            opened["url"] = validate_tap_action_url(url)
    return opened
