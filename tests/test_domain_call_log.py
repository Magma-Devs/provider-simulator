from provider_simulator.domain.call_log import CallLog


def _entry_addr(e):
    return (e["pool"], e["pid"], e["interface"], e["transport"], e["port"])


def test_push_records_a_call_with_full_identity():
    log = CallLog()
    log.push(
        "eth_blockNumber",
        "success",
        0,
        request_id=7,
        pool="eth-sim",
        pid="1",
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
    log = CallLog()
    stub = log.record_arrival("eth-sim", "1", "jsonrpc", "http", 18545)
    assert stub["status"] == "in_flight"
    assert log.stats()["total_calls"] == 1  # arrival already counts
    log.finalize(stub, method="eth_call", status="success", latency_ms=12, request_id=9)
    hist = log.get_history()
    assert len(hist) == 1  # updated in place, not a second row
    assert hist[0]["status"] == "success"
    assert hist[0]["method"] == "eth_call"
    assert hist[0]["request_id"] == 9
    assert log.stats()["calls_by_status"] == {"success": 1}


def test_finalize_after_clear_reappends_so_counts_stay_consistent():
    log = CallLog()
    stub = log.record_arrival("eth-sim", "1", "jsonrpc", "http", 18545)
    log.clear()  # a reset landed between arrival and completion
    assert log.stats()["total_calls"] == 0
    log.finalize(stub, method="eth_call", status="success", latency_ms=1)
    assert log.stats()["total_calls"] == 1
    assert len(log.get_history()) == 1
    assert log.stats()["calls_by_status"] == {"success": 1}


def test_clear_wipes_history_and_counters():
    log = CallLog()
    log.push(
        "eth_blockNumber",
        "success",
        0,
        pool="eth-sim",
        pid="1",
        interface="jsonrpc",
        transport="http",
        port=18545,
    )
    log.clear()
    assert log.get_history() == []
    assert log.stats()["total_calls"] == 0
    assert log.stats()["calls_by_status"] == {}


def test_get_history_returns_a_copy():
    log = CallLog()
    log.push("m", "success", 0, pool="p", pid="1", interface="jsonrpc", transport="http", port=1)
    snap = log.get_history()
    snap.append("garbage")
    assert len(log.get_history()) == 1
