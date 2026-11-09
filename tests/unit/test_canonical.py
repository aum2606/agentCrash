import math
import unicodedata

import pytest

from agentcrash.domain.canonical import CanonicalizationError, canonical_bytes, canonical_hash

# Expected digests were computed independently with `printf '%s' <json> | sha256sum`.
VECTORS = [
    (
        {"b": 1, "a": [2, {"d": "é", "c": None}]},
        "d10093329c89448b6ccaff0fdea44c642890b6e1281872881a7bfbf13f12bdce",
    ),
    ({}, "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"),
    ([], "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"),
]


@pytest.mark.parametrize(("value", "digest"), VECTORS)
def test_known_hash_vectors(value: object, digest: str) -> None:
    assert canonical_hash(value) == digest


def test_encoding_shape() -> None:
    raw = canonical_bytes({"b": [True, 1], "a": "é"})
    assert raw == '{"a":"é","b":[true,1]}'.encode()  # sorted, compact, UTF-8 unescaped


def test_array_order_is_preserved() -> None:
    assert canonical_hash([1, 2]) != canonical_hash([2, 1])


def test_unicode_is_not_normalized() -> None:
    nfc, nfd = unicodedata.normalize("NFC", "é"), unicodedata.normalize("NFD", "é")
    assert nfc != nfd
    assert canonical_hash(nfc) != canonical_hash(nfd)


@pytest.mark.parametrize(
    "bad", [1.5, math.nan, math.inf, {"a": 0.0}, [1, [2.5]], {1: "x"}, b"x", {1}]
)
def test_rejects_floats_and_non_json(bad: object) -> None:
    with pytest.raises(CanonicalizationError):
        canonical_bytes(bad)


def test_rejects_lone_surrogate_and_excess_depth() -> None:
    with pytest.raises(CanonicalizationError):
        canonical_bytes("\ud800")
    deep: object = 1
    for _ in range(80):
        deep = [deep]
    with pytest.raises(CanonicalizationError):
        canonical_bytes(deep)
