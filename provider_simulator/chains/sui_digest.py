"""The Sui transaction digest and base58, written in Python.

A transaction digest is ``base58(blake2b-256(b"TransactionData::" + bytes))``, where ``bytes`` are
the BCS bytes of the TransactionData. A provider gives those bytes as ``transactionBcs`` and the
digest as ``digest``, and ``executeTransaction`` takes them as ``transactionDataBcs``.

Rewritten from the Mysten Labs TypeScript SDK (``MystenLabs/ts-sdks``, Apache-2.0,
Copyright (c) Mysten Labs, Inc.):

* ``packages/sui/src/transactions/hash.ts``: ``hashTypedData``
* ``packages/sui/src/transactions/TransactionData.ts``: ``TransactionDataBuilder.getDigestFromBytes``
* ``packages/sui/src/utils/sui-types.ts``: ``isValidTransactionDigest``

This file is a rewrite in another language. It is not a copy of the TypeScript text.
"""

from __future__ import annotations

import hashlib

__all__ = [
    "DIGEST_LENGTH",
    "base58_decode",
    "base58_encode",
    "hash_typed_data",
    "is_valid_transaction_digest",
    "transaction_digest",
]

DIGEST_LENGTH = 32

# The Bitcoin alphabet: no 0, O, I or l.
_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {char: position for position, char in enumerate(_ALPHABET)}


def base58_encode(data: bytes) -> str:
    """Encode bytes as base58. Each leading zero byte becomes one ``1``."""
    leading_zeros = len(data) - len(data.lstrip(b"\x00"))
    number = int.from_bytes(data, "big")
    digits: list[str] = []
    while number:
        number, remainder = divmod(number, 58)
        digits.append(_ALPHABET[remainder])
    return "1" * leading_zeros + "".join(reversed(digits))


def base58_decode(text: str) -> bytes:
    """Decode base58 text. Raise ValueError for a character outside the alphabet."""
    number = 0
    for char in text:
        if char not in _INDEX:
            raise ValueError(f"not a base58 character: {char!r}")
        number = number * 58 + _INDEX[char]
    leading_ones = len(text) - len(text.lstrip("1"))
    body = number.to_bytes((number.bit_length() + 7) // 8, "big")
    return b"\x00" * leading_ones + body


def hash_typed_data(type_tag: str, data: bytes) -> bytes:
    """Hash ``data`` under a type tag: blake2b-256 of ``"<type_tag>::"`` followed by ``data``."""
    return hashlib.blake2b(f"{type_tag}::".encode() + data, digest_size=DIGEST_LENGTH).digest()


def transaction_digest(transaction_data_bcs: bytes) -> str:
    """The digest of a transaction, from the BCS bytes of its TransactionData."""
    return base58_encode(hash_typed_data("TransactionData", transaction_data_bcs))


def is_valid_transaction_digest(value: str) -> bool:
    """True when ``value`` is base58 text that decodes to exactly 32 bytes."""
    try:
        return len(base58_decode(value)) == DIGEST_LENGTH
    except ValueError:
        return False
