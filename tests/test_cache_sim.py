"""Offline tests for the cache-sim decision layer.

No network and no gRPC server: every test drives ``CacheSim.plan()`` with the
JSON body the router would have sent, and reads the plan it returns. That keeps
the wire shape and the staging rules under test on a machine with nothing
running.

What these lock:

* the reply the router decodes -- field names, nesting, and base64 for bytes,
  because a wrong name is not an error on either side, it is a field the router
  silently reads as empty;
* a miss being a successful call with no reply, which is what the real cache
  server does rather than failing the call;
* the key each lookup is recorded under, matching the real cache's key scheme;
* the call log, which is the only witness a test has that the secondary was
  reached at all.
"""

from __future__ import annotations

import base64
import json

import pytest

from provider_simulator.cache_sim import (
    GET_RELAY_METHOD,
    HANG_SECONDS,
    CacheEntry,
    CacheSim,
    CacheSimRegistry,
    UnknownMode,
    UnknownStatus,
    relay_key,
)

# A request hash of 0xdeadbeef, sent the way Go writes a []byte: base64.
HASH_BYTES = bytes.fromhex("deadbeef")


def lookup(
    *,
    request_hash: bytes = HASH_BYTES,
    chain_id: str = "ETH1",
    requested_block: int = 18_000_000,
    finalized: bool = False,
    seen_block: int = 0,
    shared_state_id: str = "",
    blocks_hashes_to_heights: list | None = None,
) -> bytes:
    """The RelayCacheGet body the router sends, as JSON bytes."""
    return json.dumps(
        {
            "request_hash": base64.b64encode(request_hash).decode(),
            "block_hash": None,
            "finalized": finalized,
            "requested_block": requested_block,
            "shared_state_id": shared_state_id,
            "chain_id": chain_id,
            "seen_block": seen_block,
            "blocks_hashes_to_heights": blocks_hashes_to_heights,
        }
    ).encode()


class TestTheAnswerAMissGives:
    """A miss is a successful call carrying no reply, not a failed call.

    The real cache server swallows its not-found error and returns a reply whose
    ``reply`` field is absent. The router reads a hit as "no transport error and
    a non-nil reply", so answering with an error here would still read as a miss
    but would stop matching what a real cache does.
    """

    def test_default_mode_answers_a_miss_without_failing_the_call(self) -> None:
        sim = CacheSim()
        plan = sim.plan(lookup())
        assert plan.action == "respond", f"a miss must not fail the call, got {plan.action}"
        assert plan.body == {"reply": None}, f"unexpected miss body: {plan.body}"

    def test_an_unstaged_sim_never_claims_a_hit(self) -> None:
        sim = CacheSim()
        assert sim.state()["has_entry"] is False
        assert sim.plan(lookup()).body == {"reply": None}


class TestTheReplyTheRouterDecodes:
    """The staged entry rendered as CacheRelayReply JSON.

    Each assertion names a field the router reads. A wrong name would not raise
    on either side -- Go would decode the field as its zero value and the test
    built on it would pass while proving nothing.
    """

    def test_entry_fields_land_under_the_names_the_router_reads(self) -> None:
        entry = CacheEntry(
            data=b'{"jsonrpc":"2.0","id":1,"result":"0x1"}',
            sig=b"\x01\x02",
            sig_blocks=b"\x03",
            latest_block=99_000_000,
            metadata=[{"name": "X-Node-Id", "value": "internal-7"}],
            seen_block=42,
            blocks_hashes_to_heights=[{"hash": "0xabc", "height": 17}],
            is_node_error=True,
            status_code=429,
        )
        sim = CacheSim()
        sim.stage(mode="hit", entry=entry)

        body = sim.plan(lookup()).body
        assert body is not None
        reply = body["reply"]

        assert reply["data"] == base64.b64encode(entry.data).decode()  # type: ignore[arg-type]
        assert reply["sig"] == base64.b64encode(b"\x01\x02").decode()
        assert reply["sig_blocks"] == base64.b64encode(b"\x03").decode()
        assert reply["latest_block"] == 99_000_000
        assert reply["metadata"] == [{"name": "X-Node-Id", "value": "internal-7"}]
        assert body["seen_block"] == 42
        assert body["blocks_hashes_to_heights"] == [{"hash": "0xabc", "height": 17}]
        assert body["is_node_error"] is True
        assert body["status_code"] == 429

    def test_unset_byte_fields_are_null_not_empty_string(self) -> None:
        """Go writes a nil []byte as null. An empty string decodes to empty
        bytes, which is a different thing from absent and would let a test
        claiming "the signature was stripped" pass against an entry that never
        carried one."""
        sim = CacheSim()
        sim.stage(mode="hit", entry=CacheEntry(data=b"x"))
        reply = sim.plan(lookup()).body["reply"]  # type: ignore[index]
        assert reply["sig"] is None
        assert reply["sig_blocks"] is None
        assert reply["finalized_blocks_hashes"] is None

    def test_a_status_of_zero_survives_and_is_not_turned_into_two_hundred(self) -> None:
        """Zero means the entry's writer recorded no status, which the router
        keeps distinct from an observed 200. Substituting 200 here would hide
        the case the field exists to preserve."""
        sim = CacheSim()
        sim.stage(mode="hit", entry=CacheEntry(data=b"x", status_code=0))
        assert sim.plan(lookup()).body["status_code"] == 0  # type: ignore[index]

    def test_the_rendered_reply_survives_a_json_round_trip(self) -> None:
        """It used to call json.dumps and discard the result, which also passes
        when the body is None -- the exact vacuous shape this file warns about
        elsewhere. Now it round-trips and checks the payload came back."""
        sim = CacheSim()
        sim.stage(mode="hit", entry=CacheEntry(data="plain text", status_code=204))

        body = sim.plan(lookup()).body
        assert body is not None
        restored = json.loads(json.dumps(body))
        assert restored == body
        assert base64.b64decode(restored["reply"]["data"]) == b"plain text"
        assert restored["status_code"] == 204


class TestBehavingBadlyOnPurpose:
    """Each of these must leave the router serving from a provider."""

    def test_error_mode_aborts_with_the_named_status(self) -> None:
        sim = CacheSim()
        sim.stage(mode="error", error_status="INTERNAL", error_message="boom")
        plan = sim.plan(lookup())
        assert plan.action == "abort"
        assert plan.status_code == "INTERNAL"
        assert plan.message == "boom"

    def test_hang_mode_answers_later_than_the_routers_default_budget(self) -> None:
        """The router's secondary-cache-timeout defaults to 50ms. The hang must
        clear a raised one too, so it is measured in seconds."""
        sim = CacheSim()
        sim.stage(mode="hang")
        plan = sim.plan(lookup())
        assert plan.sleep_s == HANG_SECONDS
        assert plan.sleep_s > 0.05, "a hang must outlast the 50ms default timeout"

    def test_hang_answers_a_miss_because_the_answer_is_never_read(self) -> None:
        sim = CacheSim()
        sim.stage(mode="hang")
        assert sim.plan(lookup()).body == {"reply": None}

    def test_malformed_mode_sends_bytes_that_are_not_a_reply(self) -> None:
        sim = CacheSim()
        sim.stage(mode="malformed", malformed_body="{ broken")
        plan = sim.plan(lookup())
        assert plan.action == "raw"
        assert plan.raw_body == b"{ broken"
        with pytest.raises(ValueError):
            json.loads(plan.raw_body)  # type: ignore[arg-type]

    def test_latency_is_carried_on_a_hit(self) -> None:
        sim = CacheSim()
        sim.stage(mode="hit", entry=CacheEntry(data=b"x"), latency_ms=120)
        assert sim.plan(lookup()).sleep_s == pytest.approx(0.12)


class TestStagingRefusesWhatWouldPassQuietly:
    def test_an_unknown_mode_raises_and_names_the_real_ones(self) -> None:
        sim = CacheSim()
        with pytest.raises(UnknownMode) as excinfo:
            sim.stage(mode="hitt")
        message = str(excinfo.value)
        assert "hitt" in message
        assert "hit" in message and "miss" in message

    def test_a_hit_with_no_entry_raises_instead_of_serving_nothing(self) -> None:
        sim = CacheSim()
        with pytest.raises(ValueError):
            sim.stage(mode="hit")

    def test_a_rejected_stage_leaves_the_previous_answer_untouched(self) -> None:
        sim = CacheSim()
        sim.stage(mode="hit", entry=CacheEntry(data=b"first"))
        with pytest.raises(UnknownMode):
            sim.stage(mode="nonsense")
        assert sim.plan(lookup()).body["reply"]["data"] == base64.b64encode(b"first").decode()  # type: ignore[index]

    def test_an_entry_can_be_staged_as_a_plain_dict(self) -> None:
        sim = CacheSim()
        sim.stage(mode="hit", entry={"data": b"from a dict", "status_code": 200})
        body = sim.plan(lookup()).body
        assert body["status_code"] == 200  # type: ignore[index]

    def test_unknown_keys_in_a_staged_dict_are_refused_naming_them(self) -> None:
        """INVERTED 2026-09-10, and the old direction was the MAG-3562 enabler.

        This test used to lock the silent drop: an unknown key vanished and
        the known ones survived. Every caller staged {"result": ...}, result
        was not a field, the drop swallowed it, and the router served hits
        with empty bodies while every header said otherwise. Nothing staged
        may vanish quietly - an unknown key now refuses, naming itself and
        the known set."""
        sim = CacheSim()
        with pytest.raises(ValueError, match="unknown entry field.*not_a_field"):
            sim.stage(mode="hit", entry={"data": b"x", "status_code": 418, "not_a_field": 1})


class TestTheCallLog:
    """The only witness a test has that the secondary was reached.

    Neither tier names itself in the response, so a test proving a secondary hit
    reads this log or the router's per-tier counter.
    """

    def test_a_fresh_sim_has_been_asked_nothing(self) -> None:
        assert CacheSim().call_count() == 0

    def test_every_lookup_is_recorded_even_when_it_misses(self) -> None:
        sim = CacheSim()
        sim.plan(lookup())
        sim.plan(lookup())
        assert sim.call_count() == 2, "a miss is still a lookup the router made"

    def test_a_recorded_call_carries_what_the_router_asked_for(self) -> None:
        sim = CacheSim()
        sim.plan(lookup(chain_id="ETH1", requested_block=18_500_000, seen_block=7))
        call = sim.calls()[0]
        assert call["method"] == GET_RELAY_METHOD
        assert call["chain_id"] == "ETH1"
        assert call["requested_block"] == 18_500_000
        assert call["seen_block"] == 7
        assert call["request_hash"] == "deadbeef"

    def test_the_recorded_key_matches_the_scheme_a_real_cache_files_under(self) -> None:
        """A test never names the key; it reads the one the router asked under.
        Recording it in the real scheme is what makes a key-mismatch fault
        visible instead of looking like a cold cache."""
        sim = CacheSim()
        sim.plan(lookup(chain_id="ETH1", requested_block=18_000_000, finalized=False))
        assert sim.calls()[0]["key"] == "rel:t:ETH1:deadbeef:18000000"

    def test_a_finalized_lookup_records_the_finalized_prefix(self) -> None:
        sim = CacheSim()
        sim.plan(lookup(finalized=True))
        assert sim.calls()[0]["key"].startswith("rel:f:")

    def test_the_log_records_which_mode_answered(self) -> None:
        sim = CacheSim()
        sim.plan(lookup())
        sim.stage(mode="hit", entry=CacheEntry(data=b"x"))
        sim.plan(lookup())
        assert [c["mode"] for c in sim.calls()] == ["miss", "hit"]

    def test_calls_are_oldest_first(self) -> None:
        sim = CacheSim()
        sim.plan(lookup(requested_block=1))
        sim.plan(lookup(requested_block=2))
        assert [c["requested_block"] for c in sim.calls()] == [1, 2]

    def test_an_unreadable_body_is_still_recorded_as_a_lookup(self) -> None:
        """A lookup that reached us is a lookup. Dropping it would let a test
        assert "the secondary was never asked" against a request that arrived
        and could not be parsed."""
        sim = CacheSim()
        plan = sim.plan(b"not json at all")
        assert sim.call_count() == 1
        assert plan.body == {"reply": None}

    def test_clear_calls_keeps_the_staged_answer(self) -> None:
        sim = CacheSim()
        sim.stage(mode="hit", entry=CacheEntry(data=b"x"))
        sim.plan(lookup())
        sim.clear_calls()
        assert sim.call_count() == 0
        assert sim.plan(lookup()).body["reply"] is not None  # type: ignore[index]

    def test_reset_drops_both_the_answer_and_the_log(self) -> None:
        sim = CacheSim()
        sim.stage(mode="hit", entry=CacheEntry(data=b"x"))
        sim.plan(lookup())
        sim.reset()
        assert sim.call_count() == 0
        assert sim.plan(lookup()).body == {"reply": None}


class TestTheKeyHelper:
    def test_the_key_shape_matches_the_routers_own(self) -> None:
        assert (
            relay_key(finalized=False, chain_id="ETH1", request_hash=HASH_BYTES, block=17) == "rel:t:ETH1:deadbeef:17"
        )

    def test_finality_selects_the_prefix_and_changes_nothing_else(self) -> None:
        temp = relay_key(finalized=False, chain_id="ETH1", request_hash=HASH_BYTES, block=17)
        final = relay_key(finalized=True, chain_id="ETH1", request_hash=HASH_BYTES, block=17)
        assert temp.removeprefix("rel:t:") == final.removeprefix("rel:f:")


class TestAddressingByName:
    """A cache has no pool and takes no provider slot, so it is addressed by
    name. Two zones means two cache-sims, and one must not answer for another."""

    def test_the_same_name_returns_the_same_sim(self) -> None:
        registry = CacheSimRegistry()
        assert registry.get_or_create("internal") is registry.get_or_create("internal")

    def test_two_names_are_two_independent_sims(self) -> None:
        registry = CacheSimRegistry()
        internal = registry.get_or_create("internal")
        other = registry.get_or_create("other-zone")

        internal.stage(mode="hit", entry=CacheEntry(data=b"internal"))
        assert other.plan(lookup()).body == {"reply": None}
        assert internal.plan(lookup()).body["reply"] is not None  # type: ignore[index]
        assert other.call_count() == 1
        assert internal.call_count() == 1

    def test_an_unknown_name_is_not_invented_on_a_read(self) -> None:
        registry = CacheSimRegistry()
        assert registry.get("nobody") is None
        assert registry.names() == []

    def test_reset_all_clears_every_sim(self) -> None:
        registry = CacheSimRegistry()
        for name in ("a", "b"):
            sim = registry.get_or_create(name)
            sim.stage(mode="hit", entry=CacheEntry(data=b"x"))
            sim.plan(lookup())

        registry.reset_all()

        for name in ("a", "b"):
            sim = registry.get_or_create(name)
            assert sim.call_count() == 0
            assert sim.plan(lookup()).body == {"reply": None}


class TestStagingRefusesInputTheRouterCannotAct0n:
    """Each of these used to be accepted, and each produced a cold-looking cache
    rather than the behaviour the test asked for."""

    def test_an_error_status_of_ok_is_refused_because_it_hangs_the_call(self) -> None:
        """Aborting with OK neither answers nor fails the call, so the router
        waits out its whole budget. That is a hang wearing the name of an
        error, and a test asking for an error would never see one."""
        sim = CacheSim()
        with pytest.raises(UnknownStatus) as excinfo:
            sim.stage(mode="error", error_status="OK")
        assert "OK" in str(excinfo.value)

    def test_a_misspelt_error_status_is_refused_rather_than_becoming_unknown(self) -> None:
        """The router treats every gRPC error as a miss, so a typo would pass a
        test that meant to exercise UNAVAILABLE while exercising UNKNOWN."""
        sim = CacheSim()
        with pytest.raises(UnknownStatus):
            sim.stage(mode="error", error_status="UNAVAILABEL")

    def test_a_real_status_is_still_accepted(self) -> None:
        sim = CacheSim()
        sim.stage(mode="error", error_status="RESOURCE_EXHAUSTED")
        assert sim.plan(lookup()).status_code == "RESOURCE_EXHAUSTED"

    def test_a_number_where_the_body_belongs_is_refused(self) -> None:
        """Go decodes the whole reply in one step, so one wrongly-typed field
        fails the entire unmarshal and the router logs a miss."""
        sim = CacheSim()
        with pytest.raises(TypeError) as excinfo:
            sim.stage(mode="hit", entry={"data": 12345})
        assert "data" in str(excinfo.value)

    def test_a_string_where_a_block_height_belongs_is_refused(self) -> None:
        sim = CacheSim()
        with pytest.raises(TypeError) as excinfo:
            sim.stage(mode="hit", entry={"data": b"x", "latest_block": "99000000"})
        assert "latest_block" in str(excinfo.value)

    def test_a_bool_is_not_accepted_as_a_block_height(self) -> None:
        sim = CacheSim()
        with pytest.raises(TypeError):
            sim.stage(mode="hit", entry={"data": b"x", "seen_block": True})

    def test_a_refused_entry_leaves_the_previous_one_serving(self) -> None:
        sim = CacheSim()
        sim.stage(mode="hit", entry=CacheEntry(data=b"good"))
        with pytest.raises(TypeError):
            sim.stage(mode="hit", entry={"data": 999})
        body = sim.plan(lookup()).body
        assert body is not None
        assert base64.b64decode(body["reply"]["data"]) == b"good"


class TestTheCallLogIsBounded:
    def test_the_log_stops_growing_at_the_cap(self) -> None:
        """The router queries the secondary on every primary miss, so an
        unbounded list grows until the pod restarts. Every provider's log is
        capped the same way."""
        from constants import HISTORY_MAX

        sim = CacheSim()
        for _ in range(HISTORY_MAX + 25):
            sim.plan(lookup())
        assert sim.call_count() == HISTORY_MAX

    def test_the_newest_calls_are_the_ones_kept(self) -> None:
        from constants import HISTORY_MAX

        sim = CacheSim()
        for i in range(HISTORY_MAX + 5):
            sim.plan(lookup(requested_block=i))
        blocks = [c["requested_block"] for c in sim.calls()]
        assert blocks[-1] == HISTORY_MAX + 4
        assert blocks[0] == 5


class TestHangHonoursAStagedLatency:
    def test_a_longer_staged_latency_wins_over_the_default(self) -> None:
        """It used to compute the latency and then discard it, so a test asking
        for a five-second overrun quietly got two."""
        sim = CacheSim()
        sim.stage(mode="hang", latency_ms=5000)
        assert sim.plan(lookup()).sleep_s == 5.0

    def test_a_shorter_one_does_not_shorten_the_hang(self) -> None:
        sim = CacheSim()
        sim.stage(mode="hang", latency_ms=10)
        assert sim.plan(lookup()).sleep_s == HANG_SECONDS


class TestTheResultConvenienceReachesTheWire:
    """MAG-3562: every caller staged 'result'; the old from_dict dropped it.

    The router served hits whose headers said everything and whose body said
    nothing. These lock the mapping, the refusals, and the wire encoding, so
    a staged payload can never silently vanish again.
    """

    def test_result_becomes_a_jsonrpc_envelope_in_data(self):
        entry = CacheEntry.from_dict({"result": "0xbead"})
        body = json.loads(entry.data)
        assert body == {"jsonrpc": "2.0", "id": 0, "result": "0xbead"}

    def test_the_envelope_reaches_the_relay_reply_base64d(self):
        entry = CacheEntry.from_dict({"result": "0xbead"})
        reply = entry.to_cache_relay_reply()
        decoded = json.loads(base64.b64decode(reply["reply"]["data"]))
        assert decoded["result"] == "0xbead"

    def test_error_becomes_an_error_envelope_and_marks_the_node_error(self):
        entry = CacheEntry.from_dict({"error": {"code": -32000, "message": "boom"}})
        body = json.loads(entry.data)
        assert body["error"]["code"] == -32000
        assert entry.is_node_error is True

    def test_an_explicit_is_node_error_false_survives_the_error_convenience(self):
        entry = CacheEntry.from_dict({"error": {"code": -32000, "message": "boom"}, "is_node_error": False})
        assert entry.is_node_error is False

    def test_result_beside_data_is_refused(self):
        with pytest.raises(ValueError, match="two payloads"):
            CacheEntry.from_dict({"result": "a", "data": b"b"})

    def test_result_beside_error_is_refused(self):
        with pytest.raises(ValueError, match="one or the other"):
            CacheEntry.from_dict({"result": "a", "error": {"code": 1}})

    def test_an_unknown_field_is_refused_naming_the_known_set(self):
        with pytest.raises(ValueError, match="unknown entry field.*resutl"):
            CacheEntry.from_dict({"resutl": "typo"})
