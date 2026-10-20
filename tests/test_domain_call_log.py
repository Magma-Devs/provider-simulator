import json
import threading

import pytest

from provider_simulator.domain.call_log import CallLog, decode_row_headers


def _log() -> CallLog:
    return CallLog(pool="eth-sim", pid="1")


def _entry_addr(e):
    return (e["pool"], e["pid"], e["interface"], e["transport"], e["port"])


def test_push_records_a_call_with_full_identity():
    log = _log()
    log.push(
        "eth_blockNumber",
        "success",
        0,
        request_id=7,
        interface="jsonrpc",
        transport="http",
        port=18545,
    )
    hist = log.get_history()
    assert len(hist) == 1
    e = hist[0]
    assert e["method"] == "eth_blockNumber"
    assert e["status"] == "success"
    assert e["request_id"] == 7
    assert _entry_addr(e) == ("eth-sim", "1", "jsonrpc", "http", 18545)
    assert log.stats()["total_calls"] == 1
    assert log.stats()["calls_by_status"] == {"success": 1}


def test_arrival_then_finalize_updates_in_place_not_appends():
    log = _log()
    stub = log.record_arrival("jsonrpc", "http", 18545)
    assert stub["status"] == "in_flight"
    assert log.stats()["total_calls"] == 1  # arrival already counts
    log.finalize(stub, method="eth_call", status="success", latency_ms=12, request_id=9)
    hist = log.get_history()
    assert len(hist) == 1  # updated in place, not a second row
    assert hist[0]["status"] == "success"
    assert hist[0]["method"] == "eth_call"
    assert hist[0]["request_id"] == 9
    assert log.stats()["calls_by_status"] == {"success": 1}


def test_finalize_after_clear_reappends_with_fresh_timestamp():
    log = _log()
    stub = log.record_arrival("jsonrpc", "http", 18545)
    arrival_ts = stub["ts"]
    log.clear()  # a reset landed between arrival and completion
    assert log.stats()["total_calls"] == 0
    log.finalize(stub, method="eth_call", status="success", latency_ms=1)
    assert log.stats()["total_calls"] == 1
    hist = log.get_history()
    assert len(hist) == 1
    assert log.stats()["calls_by_status"] == {"success": 1}
    # Post-clear history must not carry pre-clear timestamps — the re-appended
    # row is stamped at finalize time, so timestamp-based "only calls after the
    # reset" filters keep working.
    assert hist[0]["ts"] >= arrival_ts


def test_finalize_on_pushed_entry_updates_in_place_no_double_count():
    log = _log()
    entry = log.push("eth_call", "in_flight", 0, interface="jsonrpc", transport="http", port=18545)
    log.finalize(entry, method="eth_call", status="success", latency_ms=3)
    assert log.stats()["total_calls"] == 1
    assert len(log.get_history()) == 1
    assert log.stats()["calls_by_status"] == {"success": 1}


def test_clear_wipes_history_and_counters():
    log = _log()
    log.push("eth_blockNumber", "success", 0, interface="jsonrpc", transport="http", port=18545)
    log.clear()
    assert log.get_history() == []
    assert log.stats()["total_calls"] == 0
    assert log.stats()["calls_by_status"] == {}


def test_get_history_returns_independent_sanitized_copies():
    log = _log()
    stub = log.record_arrival("jsonrpc", "http", 18545)
    snap = log.get_history()
    # No internal bookkeeping keys leak into reader-facing rows.
    assert all(not k.startswith("_") for k in snap[0])
    # The reader's copy is independent: finalizing afterwards must not mutate it...
    log.finalize(stub, method="eth_call", status="success", latency_ms=1)
    assert snap[0]["status"] == "in_flight"
    # ...and editing the reader's copy must not corrupt the log.
    snap[0]["status"] = "vandalized"
    assert log.get_history()[0]["status"] == "success"
    assert log.stats()["calls_by_status"] == {"success": 1}


def test_rollover_caps_history_but_not_the_all_time_counters():
    log = CallLog(pool="eth-sim", pid="1", history_max=3)
    for i in range(5):
        log.push("m", "success", 0, request_id=i, interface="jsonrpc", transport="http", port=1)
    stats = log.stats()
    assert stats["history_entries"] == 3  # ring buffer capped
    assert stats["total_calls"] == 5  # all-time counter keeps counting
    assert [e["request_id"] for e in log.get_history()] == [2, 3, 4]  # oldest evicted


def test_a_log_built_without_a_name_leaves_the_name_empty():
    """Empty is the deliberate choice, not an oversight.

    The row already carries pool and pid in their own fields, so a stand-in like
    "eth-sim:1" would only repeat them — in the exact shape of Provider.key, the
    address the control API accepts, so it would read like a name the router
    reported. Tests learn which provider served a request by matching the
    Lava-Provider-Address header against these names, and no real name is ever
    empty. An empty name cannot be mistaken for a real one; "eth-sim:1" could.
    """
    log = _log()  # no name= — the shape every direct construction in the tests uses
    log.push("eth_blockNumber", "success", 0, interface="jsonrpc", transport="http", port=18545)
    entry = log.get_history()[0]
    assert entry["name"] == ""
    # The two fields a stand-in would have duplicated are already here.
    assert (entry["pool"], entry["pid"]) == ("eth-sim", "1")
    assert entry["name"] != f"{entry['pool']}:{entry['pid']}"


def test_a_named_log_carries_the_name_onto_entries_from_both_paths():
    """push and record_arrival/finalize are separate entry-creation paths, so
    both are checked: a name that only survived one of them would leave half the
    saved rows unreadable months later."""
    log = CallLog(pool="eth-sim", pid="4", name="EthBackupProvider4")
    log.push("eth_blockNumber", "success", 0, interface="jsonrpc", transport="http", port=18560)
    stub = log.record_arrival("jsonrpc", "http", 18560)
    log.finalize(stub, method="eth_getBalance", status="success", latency_ms=1)
    assert [e["name"] for e in log.get_history()] == ["EthBackupProvider4", "EthBackupProvider4"]


def _push_calls(log, count):
    for i in range(count):
        log.push(
            "eth_call" if i % 2 else "eth_blockNumber",
            "success",
            0,
            request_id=i,
            lava_headers={"Lava-Stateful-Api": "true"} if i < 4 else None,
            interface="jsonrpc",
            transport="http",
            port=18545,
        )


def test_get_history_with_a_filter_gives_only_the_rows_that_pass_it_in_buffer_order():
    log = _log()
    _push_calls(log, 6)
    rows = log.get_history(where=lambda row: row["method"] == "eth_call")
    assert [row["request_id"] for row in rows] == [1, 3, 5]
    rows = log.get_history(where=lambda row: "Lava-Stateful-Api" in row["lava_headers"] and row["request_id"] > 1)
    assert [row["request_id"] for row in rows] == [2, 3]


def test_rows_read_through_a_filter_are_independent_sanitized_copies():
    log = _log()
    _push_calls(log, 4)
    rows = log.get_history(where=lambda row: row["request_id"] in (1, 2))
    assert all(not key.startswith("_") for row in rows for key in row)
    # Editing what the reader got must not reach the log, the headers included.
    rows[0]["status"] = "vandalized"
    rows[0]["lava_headers"]["Lava-Stateful-Api"] = "vandalized"
    again = log.get_history(where=lambda row: row["request_id"] in (1, 2))
    assert [(row["status"], row["lava_headers"]) for row in again] == [("success", {"Lava-Stateful-Api": "true"})] * 2


def test_a_filter_that_passes_no_row_reads_nothing_and_removes_nothing():
    log = _log()
    _push_calls(log, 3)
    assert log.get_history(where=lambda row: False) == []
    assert log.get_history(where=lambda row: True) == log.get_history()
    assert len(log.get_history()) == 3
    assert log.stats()["history_entries"] == 3


def test_a_where_test_that_raises_leaves_the_log_usable():
    """``where`` runs under the lock of the log. If the lock stayed held when it
    raised, every later call on this log would wait for ever. So the proof is a
    plain read in a thread with a time limit: a held lock fails this test after
    two seconds, and does not hang it."""

    class WhereBroke(Exception):
        pass

    def broken(row):
        raise WhereBroke

    log = _log()
    _push_calls(log, 3)
    with pytest.raises(WhereBroke) as raised:
        log.get_history(where=broken)
    assert raised.type is WhereBroke  # the caller gets the error of the test itself, not a wrapper

    reads = []
    reader = threading.Thread(target=lambda: reads.append(log.get_history()), daemon=True)
    reader.start()
    reader.join(timeout=2)
    assert not reader.is_alive(), "a plain read is still waiting for the lock after where raised"
    assert [row["request_id"] for row in reads[0]] == [0, 1, 2]

    # The log still takes a call and counts it: the next read holds it.
    stub = log.record_arrival("jsonrpc", "http", 18545)
    log.finalize(stub, method="eth_call", status="success", latency_ms=1, request_id=3)
    assert [row["request_id"] for row in log.get_history()] == [0, 1, 2, 3]
    assert log.stats()["total_calls"] == 4
    assert log.stats()["history_entries"] == 4
    assert log.stats()["calls_by_status"] == {"success": 4}


# ── What a row says about the request: headers, deadline, gRPC status ─────────


def test_a_row_holds_every_request_header_and_the_log_stores_them_as_one_text():
    # A reader gets ``headers`` as an object. The stored row holds the same
    # headers as one JSON text under a private key, and it has no ``headers``
    # key. A ``where`` test is given the stored row, so it can show that row.
    log = _log()
    sent = {"Lava-Guid": "GUID_1", "Content-Type": "application/json"}
    log.record_arrival("jsonrpc", "http", 18545, lava_headers={"Lava-Guid": "GUID_1"}, headers=sent)

    stored: list[dict] = []
    log.get_history(where=lambda entry: stored.append(dict(entry)) is None)
    assert stored[0]["_headers_json"] == '{"Lava-Guid":"GUID_1","Content-Type":"application/json"}'
    assert "headers" not in stored[0]

    row = log.get_history()[0]
    assert row["headers"] == sent
    assert row["lava_headers"] == {"Lava-Guid": "GUID_1"}
    assert all(not key.startswith("_") for key in row)


def test_a_reader_that_changes_headers_does_not_change_the_log():
    log = _log()
    log.record_arrival("jsonrpc", "http", 18545, headers={"X-Kept": "1"})
    first = log.get_history()[0]
    first["headers"]["X-Kept"] = "vandalized"
    first["headers"]["X-New"] = "added"
    assert log.get_history()[0]["headers"] == {"X-Kept": "1"}


def test_a_header_value_of_bytes_is_read_as_base64_text():
    # gRPC gives the value of a ``-bin`` metadata key as bytes. JSON has no
    # bytes, so the row holds the base64 text of the value.
    log = _log()
    log.record_arrival("grpc", "http2", 19090, headers={"trace-bin": b"\x00\xff\x10", "user-agent": "grpc-go"})
    assert log.get_history()[0]["headers"] == {"trace-bin": "AP8Q", "user-agent": "grpc-go"}


def test_a_row_with_no_headers_reads_an_empty_object_and_no_grpc_value():
    # Both paths that make a row: an arrival and a push.
    log = _log()
    log.record_arrival("jsonrpc", "http", 18545)
    log.push("eth_subscription push", "success", 0, interface="jsonrpc", transport="ws", port=18546)
    assert [(row["headers"], row["deadline_remaining_ms"], row["grpc_status"]) for row in log.get_history()] == [
        ({}, None, None),
        ({}, None, None),
    ]


def test_a_push_keeps_the_headers_that_it_is_given():
    log = _log()
    log.push(
        "ws_upgrade", "rate_limit", 0, interface="jsonrpc", transport="ws", port=18546, headers={"Upgrade": "websocket"}
    )
    assert log.get_history()[0]["headers"] == {"Upgrade": "websocket"}


def test_a_grpc_row_keeps_its_deadline_and_gets_its_status_name_after_finalize():
    # finalize() runs before the provider waits. The status name comes after
    # the wait, so it has a method of its own, and the row holds None until then.
    log = _log()
    stub = log.record_arrival("grpc", "http2", 19090, deadline_remaining_ms=4850)
    log.finalize(stub, method="GetLatestBlock", status="error", latency_ms=0)
    assert [(row["deadline_remaining_ms"], row["grpc_status"]) for row in log.get_history()] == [(4850, None)]

    log.set_grpc_status(stub, "NOT_FOUND")
    row = log.get_history()[0]
    assert (row["status"], row["deadline_remaining_ms"], row["grpc_status"]) == ("error", 4850, "NOT_FOUND")
    assert log.stats()["calls_by_status"] == {"error": 1}


def test_a_reader_that_cuts_first_takes_the_headers_as_text_and_decodes_the_rows_it_keeps():
    # GET /history sorts and cuts the rows of many logs before it answers. It
    # asks for the stored text, and it decodes the rows that it keeps.
    log = _log()
    log.record_arrival("jsonrpc", "http", 18545, headers={"X-Kept": "1"})
    row = log.get_history(decode_headers=False)[0]
    assert row["headers"] == '{"X-Kept":"1"}'
    assert all(not key.startswith("_") for key in row)

    decode_row_headers(row)
    assert row["headers"] == {"X-Kept": "1"}
    # A row that holds the object already stays as it is.
    decode_row_headers(row)
    assert row["headers"] == {"X-Kept": "1"}


def test_a_bytes_value_in_lava_headers_is_read_as_base64_text_and_the_rows_are_valid_json():
    # A row that held bytes could not be written as JSON. So one such row made
    # every read that returned it fail.
    log = _log()
    log.record_arrival(
        "grpc",
        "http2",
        19090,
        lava_headers={"lava-x-bin": b"\x00\x01", "lava-guid": "g1"},
        headers={"lava-x-bin": b"\x00\x01"},
    )
    row = log.get_history()[0]
    assert row["lava_headers"] == {"lava-x-bin": "AAE=", "lava-guid": "g1"}
    assert row["headers"] == {"lava-x-bin": "AAE="}
    assert json.loads(json.dumps(log.get_history())) == log.get_history()


def test_a_row_has_no_path_until_one_is_recorded():
    log = _log()
    entry = log.record_arrival("graphql", "http", 18999)
    assert log.get_history()[0]["path"] is None
    log.record_path(entry, "/graphql")
    assert log.get_history()[0]["path"] == "/graphql"
