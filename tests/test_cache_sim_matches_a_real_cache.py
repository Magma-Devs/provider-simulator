"""The cache-sim's replies must have the same shape a real cache's replies have.

Nothing compared the two until 2026-09-10, and when somebody finally recorded a
real cache the shapes turned out to differ in four places. The cache-sim is what
every secondary-cache test reasons about as a cache, so a shape it invents is a
shape those tests come to depend on.

**What "shape" means here, and why it is not the values.** A real reply carries a
real Ethereum block and a real upstream's response headers. Neither can be
reproduced and neither is the point. What must match is the STATE of each field:
absent (``null``), present-and-empty (``""`` or ``[]``), or present. Go decides
that state from what was stored -- it writes a nil byte list as ``null`` and an
empty one as ``""`` -- so the state is exactly the part a hand-written stub gets
wrong, and exactly the part a router could one day read.

**Why this file can fail.** ``TestTheCheckCatchesTheShapeThisTicketFixed`` runs
the same comparison against the shape the cache-sim produced BEFORE the fix and
requires every one of the four differences to be named. A comparison that passes
because it compared nothing is the fault this whole ticket is about, so the
mutation is part of the deliverable rather than a nicety.

The real bytes come from ``tests/fixtures/real_cache_replies.json``, whose own
header names the recording, the date and the lines taken from it.

Refs MAG-3578.
"""

from __future__ import annotations

import base64
import json
import pathlib
from typing import Any

from provider_simulator.cache_sim import CacheEntry, CacheSim

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "real_cache_replies.json"

# The six top-level fields of a CacheRelayReply, and the six of the nested reply.
# Written out rather than derived from either side, so a field that disappears
# from BOTH is still caught.
TOP_FIELDS = (
    "blocks_hashes_to_heights",
    "is_node_error",
    "optional_metadata",
    "reply",
    "seen_block",
    "status_code",
)
REPLY_FIELDS = (
    "data",
    "finalized_blocks_hashes",
    "latest_block",
    "metadata",
    "sig",
    "sig_blocks",
)


def _shape(value: Any) -> str:
    """Name the STATE of one JSON value, ignoring how much it holds.

    A list's length is deliberately not part of the answer: one real upstream
    sent 14 response headers and another sent 10, and neither number is
    something a simulator should imitate. Whether the list is absent, empty or
    populated IS, because Go writes those three differently and they say three
    different things.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, str):
        return "empty-string" if value == "" else "string"
    if isinstance(value, list):
        return "empty-list" if not value else "list"
    if isinstance(value, dict):
        return "object"
    raise TypeError(f"{value!r} is not a JSON value the wire can carry")


def flatten(reply: dict[str, Any]) -> dict[str, str]:
    """One flat map of field path to state, for both levels of the message.

    Every name in TOP_FIELDS and REPLY_FIELDS appears in the result whether or
    not the message carried it, so a MISSING field reads as ``<absent>`` rather
    than vanishing from the comparison. That is the whole miss-reply defect:
    five fields were simply not there, and a comparison built from the keys
    present on both sides would have had nothing to say about them.
    """
    flat: dict[str, str] = {}
    for name in TOP_FIELDS:
        flat[name] = _shape(reply[name]) if name in reply else "<absent>"
    nested = reply.get("reply")
    if isinstance(nested, dict):
        for name in REPLY_FIELDS:
            flat[f"reply.{name}"] = _shape(nested[name]) if name in nested else "<absent>"
    return flat


def differences(real: dict[str, Any], ours: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """Every field where our state and the real state disagree."""
    r, o = flatten(real), flatten(ours)
    return {k: (r[k], o.get(k, "<absent>")) for k in r if r[k] != o.get(k, "<absent>")}


def _report(diffs: dict[str, tuple[str, str]]) -> str:
    return "; ".join(f"{k}: real={real} ours={mine}" for k, (real, mine) in sorted(diffs.items()))


def real_replies() -> dict[str, dict[str, Any]]:
    return json.loads(FIXTURE.read_text())["replies"]


def a_lookup() -> bytes:
    """A RelayCacheGet body, shaped the way the router sends one."""
    return json.dumps(
        {
            "request_hash": base64.b64encode(bytes.fromhex("deadbeef")).decode(),
            "block_hash": None,
            "finalized": False,
            "requested_block": 18_000_000,
            "shared_state_id": "",
            "chain_id": "ETH1",
            "seen_block": 0,
            "blocks_hashes_to_heights": None,
        }
    ).encode()


def our_miss(seen_block: int) -> dict[str, Any]:
    sim = CacheSim()
    sim.stage(mode="miss", seen_block=seen_block)
    body = sim.plan(a_lookup()).body
    assert body is not None
    return body


def our_hit() -> dict[str, Any]:
    """A hit staged to stand beside a real one.

    Only the fields a real writer would have filled are set. Everything left
    alone is the part under test: whether an untouched cache-sim renders the
    same states a real cache renders.
    """
    sim = CacheSim()
    sim.stage(
        mode="hit",
        entry=CacheEntry(
            data=b'{"jsonrpc":"2.0","id":1,"result":"0x18b0f4a"}',
            latest_block=25_946_260,
            seen_block=25_946_260,
            metadata=[{"name": "Content-Type", "value": "application/json"}],
            status_code=200,
        ),
    )
    body = sim.plan(a_lookup()).body
    assert body is not None
    return body


class TestAMissLooksLikeARealMiss:
    """281 real misses were recorded and every one carried all six fields."""

    def test_a_cache_with_no_head_yet_matches_the_real_one(self) -> None:
        real = real_replies()["miss_before_the_cache_had_a_head"]
        diffs = differences(real["reply"], our_miss(seen_block=0))
        assert not diffs, f"our miss does not look like the real one (seq {real['seq']}): {_report(diffs)}"

    def test_a_cache_with_a_head_matches_the_real_one(self) -> None:
        real = real_replies()["miss_with_a_head"]
        diffs = differences(real["reply"], our_miss(seen_block=real["reply"]["seen_block"]))
        assert not diffs, f"our miss does not look like the real one (seq {real['seq']}): {_report(diffs)}"

    def test_the_head_a_miss_reports_is_the_value_the_real_one_reported(self) -> None:
        """The states matching is not enough on its own: two different ints have
        the same state. This checks the number itself travels."""
        real = real_replies()["miss_with_a_head"]["reply"]
        assert real["seen_block"] > 0, "the fixture line chosen must be a cache that HAD a head"
        assert our_miss(seen_block=real["seen_block"])["seen_block"] == real["seen_block"]


class TestAHitLooksLikeARealHit:
    """Both upstreams in the recording, so a per-provider quirk cannot pass as
    the contract."""

    def test_our_hit_matches_the_first_upstreams_real_hit(self) -> None:
        real = real_replies()["hit_from_upstream_1"]
        diffs = differences(real["reply"], our_hit())
        assert not diffs, f"our hit does not look like the real one (seq {real['seq']}): {_report(diffs)}"

    def test_our_hit_matches_the_second_upstreams_real_hit(self) -> None:
        real = real_replies()["hit_from_upstream_2"]
        diffs = differences(real["reply"], our_hit())
        assert not diffs, f"our hit does not look like the real one (seq {real['seq']}): {_report(diffs)}"

    def test_the_body_we_staged_is_the_body_that_comes_back(self) -> None:
        """Read the payload, not only the field states.

        MAG-3562 served hits whose headers said everything and whose body
        carried nothing, through three green runs, because no test had ever read
        a body the simulator served. This one reads it.
        """
        body = our_hit()
        payload = json.loads(base64.b64decode(body["reply"]["data"]))
        assert payload["result"] == "0x18b0f4a", f"the staged payload did not survive: {payload}"


class TestTheCheckCatchesTheShapeThisTicketFixed:
    """The mutation that proves this file can fail.

    ``_the_old_shape`` reproduces what the cache-sim rendered before MAG-3578:
    a one-field miss, a null signature, and empty lists where a real cache sends
    nothing at all. Each test below requires the comparison to name its field.
    Without these, every assertion above would still pass if ``differences``
    quietly compared an empty set.
    """

    @staticmethod
    def _the_old_miss() -> dict[str, Any]:
        return {"reply": None}

    @staticmethod
    def _the_old_hit() -> dict[str, Any]:
        hit = json.loads(json.dumps(our_hit()))
        hit["reply"]["sig"] = None
        hit["optional_metadata"] = []
        hit["blocks_hashes_to_heights"] = []
        return hit

    def test_the_five_fields_the_old_miss_omitted_are_each_named(self) -> None:
        real = real_replies()["miss_with_a_head"]["reply"]
        diffs = differences(real, self._the_old_miss())
        assert set(diffs) == {
            "optional_metadata",
            "seen_block",
            "blocks_hashes_to_heights",
            "is_node_error",
            "status_code",
        }, f"the check did not name exactly the five missing fields: {_report(diffs)}"
        for field, (_, ours) in diffs.items():
            assert ours == "<absent>", f"{field} should read as absent in the old shape, read {ours}"

    def test_a_null_signature_is_caught(self) -> None:
        real = real_replies()["hit_from_upstream_2"]["reply"]
        diffs = differences(real, self._the_old_hit())
        assert diffs.get("reply.sig") == (
            "empty-string",
            "null",
        ), f'a null signature where a real cache sends "" went unnoticed: {_report(diffs)}'

    def test_an_empty_list_where_a_real_cache_sends_nothing_is_caught(self) -> None:
        real = real_replies()["hit_from_upstream_2"]["reply"]
        diffs = differences(real, self._the_old_hit())
        for field in ("optional_metadata", "blocks_hashes_to_heights"):
            assert diffs.get(field) == (
                "null",
                "empty-list",
            ), f"{field} as an empty list went unnoticed: {_report(diffs)}"

    def test_the_fixed_shape_is_the_only_one_that_passes(self) -> None:
        """The two halves in one place: the old shape is caught, the new one is
        clean, and the same function judged both."""
        real = real_replies()["hit_from_upstream_2"]["reply"]
        assert differences(real, self._the_old_hit()), "the mutation was not caught at all"
        assert not differences(real, our_hit()), "the fixed shape should compare clean"


class TestTheFixtureIsWhatItClaimsToBe:
    """A copied fixture with no provenance is unverifiable the first time
    somebody doubts it, and a fixture nothing reads is worse than none."""

    def test_every_recorded_reply_carries_its_line_and_its_digest(self) -> None:
        loaded = json.loads(FIXTURE.read_text())
        provenance = loaded["_provenance"]
        assert "recorded-2026-09-10.jsonl" in provenance["source"]
        assert provenance["recorded"] == "2026-09-10"
        assert loaded["replies"], "the fixture holds no replies"
        for name, entry in loaded["replies"].items():
            assert entry["seq"] > 0, f"{name} does not name the line it came from"
            assert len(entry["reply_sha256"]) == 64, f"{name} carries no digest"

    def test_the_recorded_bytes_still_hash_to_the_digest_beside_them(self) -> None:
        """The bytes and the digest travelled together, so a hand-edited fixture
        is caught here rather than believed."""
        import hashlib

        for name, entry in real_replies().items():
            # The recorder hashed the exact bytes it saw, which is the compact
            # JSON the cache wrote, so it is re-rendered that way to compare.
            raw = json.dumps(entry["reply"], separators=(",", ":")).encode()
            assert (
                hashlib.sha256(raw).hexdigest() == entry["reply_sha256"]
            ), f"{name} no longer matches the digest recorded with it"
