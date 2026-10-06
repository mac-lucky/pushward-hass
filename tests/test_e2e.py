"""Tests for the pw1 envelope against the shared PushWard test vectors.

``testdata/e2e-vectors-v1.json`` is a byte-identical copy of the vectors every
PushWard implementation (server, apps, CLI, MCP) is tested against.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.pushward.e2e import (
    ENVELOPE_MAX_LEN,
    E2EError,
    _hkdf,
    key_id,
    open_envelope,
    parse_envelope,
    parse_key,
    seal,
    seal_plaintext,
)

_VECTORS = json.loads((Path(__file__).parent / "testdata" / "e2e-vectors-v1.json").read_text())
KEY_HEX = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
KEY = bytes.fromhex(KEY_HEX)


def _case_id(case: dict) -> str:
    return case["name"]


@pytest.mark.parametrize("case", _VECTORS["seal"], ids=_case_id)
def test_seal_vector(case: dict) -> None:
    key = parse_key(case["key_hex"])
    assert _hkdf(key, b"pushward/e2e/v1/enc", 32).hex() == case["enc_key_hex"]
    assert key_id(key) == case["kid"]
    envelope = seal_plaintext(key, case["plaintext"].encode(), nonce=bytes.fromhex(case["nonce_hex"]))
    assert envelope == case["envelope"]
    assert open_envelope(key, case["envelope"]) == case["expect"]


@pytest.mark.parametrize("case", _VECTORS["open_fail"], ids=_case_id)
def test_open_fail_vector(case: dict) -> None:
    with pytest.raises(E2EError):
        open_envelope(parse_key(case["key_hex"]), case["envelope"])


@pytest.mark.parametrize("case", _VECTORS["parse_fail"], ids=_case_id)
def test_parse_fail_vector(case: dict) -> None:
    with pytest.raises(E2EError):
        parse_envelope(case["envelope"])


def test_parse_key_ignores_case_and_whitespace() -> None:
    spaced = " ".join(KEY_HEX.upper()[i : i + 8] for i in range(0, 64, 8))
    assert parse_key(f"\t{spaced}\n") == KEY


@pytest.mark.parametrize("text", ["", KEY_HEX[:-2], KEY_HEX + "00", KEY_HEX[:-1] + "g"])
def test_parse_key_rejects_wrong_length_or_digits(text: str) -> None:
    with pytest.raises(E2EError, match="64 hexadecimal"):
        parse_key(text)


@pytest.mark.parametrize("text", ["hlk_0123456789abcdef0123456789abcdef", " hla_abc"])
def test_parse_key_names_an_integration_key(text: str) -> None:
    with pytest.raises(E2EError, match="integration key"):
        parse_key(text)


def test_seal_round_trips_every_field() -> None:
    envelope = seal(KEY, title="Door", subtitle="Hall", body="Opened", url="homeassistant://navigate/lovelace")
    assert envelope.startswith(f"pw1.{key_id(KEY)}.")
    assert open_envelope(KEY, envelope) == {
        "title": "Door",
        "subtitle": "Hall",
        "body": "Opened",
        "url": "homeassistant://navigate/lovelace",
    }


def test_seal_pads_to_64_bytes_and_uses_a_fresh_nonce() -> None:
    first = seal(KEY, title="Door", body="Opened")
    second = seal(KEY, title="Door", body="Opened")
    assert first != second
    for envelope in (first, second):
        _, raw = parse_envelope(envelope)
        assert (len(raw) - 12 - 16) % 64 == 0


def test_seal_drops_empty_optional_fields() -> None:
    assert open_envelope(KEY, seal(KEY, title="t", body="b", subtitle="", url="")) == {"title": "t", "body": "b"}


def test_seal_up_to_the_envelope_cap_goes_unpadded() -> None:
    # {"title":"Max","body":"..."} with this body is 2266 bytes: the largest plaintext
    # that fits in 3072 characters, and padding it to 2304 would not.
    envelope = seal(KEY, title="Max", body="x" * 2241)
    assert len(envelope) == ENVELOPE_MAX_LEN
    assert open_envelope(KEY, envelope)["body"] == "x" * 2241

    with pytest.raises(E2EError, match="too long"):
        seal(KEY, title="Max", body="x" * 2242)


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"title": "", "body": "b"}, "must not be empty"),
        ({"title": "t", "body": ""}, "must not be empty"),
        ({"title": "a" * 257, "body": "b"}, "title is 257 characters"),
        ({"title": "t", "subtitle": "s" * 257, "body": "b"}, "subtitle is 257 characters"),
        ({"title": "t", "body": "b" * 4097}, "body is 4097 characters"),
        ({"title": "t", "body": "b", "url": "javascript:alert(1)"}, "url"),
        ({"title": "t", "body": "b", "url": "https://"}, "url"),
    ],
)
def test_seal_refuses_what_the_server_would(fields: dict, message: str) -> None:
    with pytest.raises(E2EError, match=message):
        seal(KEY, **fields)


def test_seal_counts_code_points_not_bytes() -> None:
    title = "\U0001f525" * 256  # 1024 bytes, 256 code points
    assert open_envelope(KEY, seal(KEY, title=title, body="b"))["title"] == title


def test_open_with_another_key_names_the_kid() -> None:
    other = bytes(range(32, 64))
    with pytest.raises(E2EError, match=key_id(KEY)):
        open_envelope(other, seal(KEY, title="t", body="b"))
