from provider_simulator.domain.call_log import CallLog


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
