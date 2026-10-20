"""Tests of the Sui digest and base58 port, against real transactions.

The vectors are 20 transactions read from ``graphql.mainnet.sui.io``: for each, the field
``transactionBcs`` (the TransactionData bytes, base64) and the field ``digest`` that the provider
reported. Ten are at checkpoint 200,000,000 and ten at a recent checkpoint.
"""

import base64
import json
import random
from pathlib import Path

import pytest

from provider_simulator.chains.sui_digest import (
    DIGEST_LENGTH,
    base58_decode,
    base58_encode,
    hash_typed_data,
    is_valid_transaction_digest,
    transaction_digest,
)

_VECTORS = json.loads((Path(__file__).parent / "fixtures" / "sui_transactions.json").read_text())["vectors"]


def _bytes(vector: dict) -> bytes:
    return base64.b64decode(vector["transaction_data_bcs_base64"])


def test_the_vector_file_holds_twenty_transactions_from_two_checkpoints():
    assert len(_VECTORS) == 20
    assert len({v["checkpoint"] for v in _VECTORS}) == 2
    assert len({v["digest"] for v in _VECTORS}) == 20


@pytest.mark.parametrize(
    "vector",
    _VECTORS,
    ids=[f"cp{v['checkpoint']}-{v['digest'][:6]}" for v in _VECTORS],
)
def test_a_real_transaction_hashes_to_the_digest_its_provider_reported(vector):
    assert transaction_digest(_bytes(vector)) == vector["digest"]


def test_without_the_type_tag_no_real_transaction_matches_its_digest():
    # Negative control: the same bytes hashed with no "TransactionData::" prefix.
    plain = [base58_encode(__import__("hashlib").blake2b(_bytes(v), digest_size=32).digest()) for v in _VECTORS]
    assert [p == v["digest"] for p, v in zip(plain, _VECTORS)] == [False] * len(_VECTORS)


def test_the_type_tag_changes_the_hash():
    data = _bytes(_VECTORS[0])
    assert hash_typed_data("TransactionData", data) != hash_typed_data("SenderSignedData", data)
    assert len(hash_typed_data("TransactionData", data)) == DIGEST_LENGTH


@pytest.mark.parametrize("vector", _VECTORS[:5], ids=lambda v: v["digest"][:6])
def test_a_real_digest_is_valid(vector):
    assert is_valid_transaction_digest(vector["digest"])


@pytest.mark.parametrize(
    "value",
    [
        "",  # nothing
        "0" + "1" * 43,  # 0 is not in the alphabet
        "O" * 44,  # neither is O
        "l" * 44,  # nor l
        "I" * 44,  # nor I
        base58_encode(b"\x07" * 31),  # 31 bytes
        base58_encode(b"\x07" * 33),  # 33 bytes
    ],
)
def test_a_value_that_is_not_a_32_byte_base58_string_is_not_a_valid_digest(value):
    assert not is_valid_transaction_digest(value)


@pytest.mark.parametrize(
    ("raw", "text"),
    [
        (b"", ""),
        (b"\x00", "1"),
        (b"\x00\x00\x00", "111"),
        (b"Hello World!", "2NEpo7TZRRrLZSi2U"),
    ],
)
def test_base58_known_values(raw, text):
    assert base58_encode(raw) == text
    assert base58_decode(text) == raw


def test_base58_round_trips_bytes_of_every_length_with_leading_zeros():
    rng = random.Random(7)
    for length in range(0, 41):
        for zeros in (0, 1, 3):
            raw = b"\x00" * zeros + bytes(rng.randrange(256) for _ in range(length))
            assert base58_decode(base58_encode(raw)) == raw


def test_base58_decode_refuses_a_character_outside_the_alphabet():
    with pytest.raises(ValueError, match="not a base58 character"):
        base58_decode("abc0")
