"""mode="port_closed" on a gRPC provider, over real sockets.

Every other gRPC fault is a status reply: the provider answers, and the answer
says UNAVAILABLE or CANCELLED. This mode is the one where the provider answers
nothing, because its port is closed. So these tests read the port itself, with a
raw TCP connection, and not only what a gRPC client reports.

Three things are checked on the line right after the control call returns, with
no sleep, because the control API promises the port has already changed:

  - closed:  a new TCP connection is refused, and a call on a channel that was
             open before the fault fails with a message that is the client's own
             and not one of the simulator's status messages;
  - open:    a new TCP connection is accepted and a call answers;
  - neither: a refused block leaves the port open and stores nothing.

Runs against the shared in-process simulator (see conftest.py) and the real
lava-sim-grpc pool. The filter tests build a second simulator, because no shipped
provider serves two gRPC ports or a gRPC port beside an HTTP one. The test of a
port that cannot bind runs on it too.

Run with:
  pytest tests/test_simulator_grpc_port_closed.py -v
"""

from __future__ import annotations

import errno
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import grpc
import pytest

# Splice cosmos_pb2 onto sys.path so the generated stubs resolve. Must run
# before the `from cosmos...` import below (see test_simulator_grpc.py).
import cosmos_pb2  # noqa: F401  isort: split

from cosmos.base.tendermint.v1beta1 import query_pb2, query_pb2_grpc  # isort: skip

import server as server_module
from provider_simulator import control_api, topology
from provider_simulator.chains.lava import GRPC_LATEST_BLOCK
from provider_simulator.topology import port_of

POOL = "lava-sim-grpc"
_PORT = {pid: port_of(POOL, pid, "grpc", "http2") for pid in ("1", "2")}
# The two status messages the simulator itself sends for an unreachable gRPC
# provider (listeners/grpc.py). A closed port must produce neither.
_SIMULATOR_MESSAGES = ("provider down", "connection dropped")


# ── helpers ──────────────────────────────────────────────────────────────────


def _post(url: str, body: dict) -> tuple[int, dict]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _set(control: str, key: str, **block) -> tuple[int, dict]:
    return _post(f"{control}/scenario", {"providers": {key: block}})


def _connect(port: int) -> int:
    """Try one TCP connection. 0 when accepted, else the errno."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(2.0)
    try:
        return probe.connect_ex(("127.0.0.1", port))
    finally:
        probe.close()


def _fresh_channel(port: int) -> grpc.Channel:
    """A channel that shares no connection state with any other channel.

    gRPC keeps one subchannel per address for the whole process. After a refused
    connection that subchannel backs off for about a second, and every channel to
    the same address fails fast meanwhile. Its own pool is what lets this channel
    connect at once, so a call on it measures the port and not the back-off.
    """
    return grpc.insecure_channel(f"127.0.0.1:{port}", options=[("grpc.use_local_subchannel_pool", 1)])


def _latest_block(channel: grpc.Channel, **kwargs):
    stub = query_pb2_grpc.ServiceStub(channel)
    return stub.GetLatestBlock(query_pb2.GetLatestBlockRequest(), timeout=5, **kwargs)


def _history_count(control: str, pool: str, pid: str) -> int:
    _, body = _get(f"{control}/history?pool={pool}&pid={pid}")
    return body["count"]


def _mode(control: str, key: str) -> str:
    _, body = _get(f"{control}/scenario")
    return body["providers"][key]["mode"]


@pytest.fixture(autouse=True)
def clean_state(sim):
    """Reset scenario AND clear history before and after every test."""
    _post(f"{sim['control']}/reset/all", {})
    yield
    status, body = _post(f"{sim['control']}/reset/all", {})
    assert status == 200, f"the reset that reopens every port failed: {body}"


# ── the port is really closed ────────────────────────────────────────────────


class TestThePortIsClosed:
    def test_both_providers_accept_and_answer_with_nothing_set(self, sim):
        """The baseline. Without it "refused" below would also be satisfied by a
        port that never listened."""
        for pid, port in _PORT.items():
            assert _connect(port) == 0, f"provider {pid} port {port} must accept with no fault set"
            with _fresh_channel(port) as channel:
                assert _latest_block(channel).block.header.height == GRPC_LATEST_BLOCK

    def test_a_new_connection_is_refused_on_the_line_after_the_call(self, sim):
        status, body = _set(sim["control"], f"{POOL}:1", mode="port_closed")
        refused = _connect(_PORT["1"])
        assert status == 200, body
        assert body["applied"][f"{POOL}:1"] == {"mode": "port_closed"}
        assert refused == errno.ECONNREFUSED, (
            f"port {_PORT['1']} must refuse a connection once the control call has returned; "
            f"connect_ex gave {refused} ({errno.errorcode.get(refused, 'accepted')})"
        )

    def test_a_channel_used_before_the_fault_fails_with_the_clients_own_message(self, sim):
        with _fresh_channel(_PORT["1"]) as channel:
            assert _latest_block(channel).block.header.height == GRPC_LATEST_BLOCK  # opened AND used
            status, body = _set(sim["control"], f"{POOL}:1", mode="port_closed")
            assert status == 200, body
            with pytest.raises(grpc.RpcError) as caught:
                _latest_block(channel)
        assert caught.value.code() is grpc.StatusCode.UNAVAILABLE
        details = caught.value.details() or ""
        for message in _SIMULATOR_MESSAGES:
            assert message not in details, (
                f"the call failed with the simulator's own status message {message!r}, so the "
                f"provider answered. A closed port answers nothing. details={details!r}"
            )

    @pytest.mark.parametrize("mode, message", [("down", "provider down"), ("drop_connection", "connection dropped")])
    def test_the_two_status_reply_modes_do_send_the_simulators_message(self, sim, mode, message):
        """The control for the test above. These two modes answer UNAVAILABLE as
        well, with the simulator's own message and with the port still open, so
        the message check above CAN fail: it is what tells a status reply from
        a closed port. It also pins what these two modes do on gRPC."""
        status, body = _set(sim["control"], f"{POOL}:1", mode=mode)
        assert status == 200, body
        assert _connect(_PORT["1"]) == 0, f"mode={mode} answers; it must leave the port open"
        with _fresh_channel(_PORT["1"]) as channel:
            with pytest.raises(grpc.RpcError) as caught:
                _latest_block(channel)
        assert caught.value.code() is grpc.StatusCode.UNAVAILABLE
        assert caught.value.details() == message
        assert message in _SIMULATOR_MESSAGES
        assert _history_count(sim["control"], POOL, "1") == 1, f"mode={mode} receives the call and records it"

    def test_a_call_in_flight_is_cut_and_the_control_call_does_not_wait_for_it(self, sim):
        """Every open connection is closed, one with a call in flight included.

        The provider hangs first, so the call sits inside the server for up to
        30 seconds. Closing the port must end it at once and must not wait for
        the handler to finish.
        """
        status, body = _set(sim["control"], f"{POOL}:1", mode="hang")
        assert status == 200, body
        outcome: dict = {}

        def call() -> None:
            with _fresh_channel(_PORT["1"]) as channel:
                try:
                    query_pb2_grpc.ServiceStub(channel).GetLatestBlock(query_pb2.GetLatestBlockRequest(), timeout=20)
                    outcome["code"] = grpc.StatusCode.OK
                except grpc.RpcError as exc:
                    outcome["code"], outcome["details"] = exc.code(), exc.details()

        caller = threading.Thread(target=call)
        caller.start()
        deadline = time.monotonic() + 5.0
        while _history_count(sim["control"], POOL, "1") == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (
            _history_count(sim["control"], POOL, "1") == 1
        ), "the call must be inside the server before the port closes"

        started = time.monotonic()
        status, body = _set(sim["control"], f"{POOL}:1", mode="port_closed")
        took = time.monotonic() - started
        refused = _connect(_PORT["1"])
        caller.join(timeout=5.0)

        assert status == 200, body
        assert refused == errno.ECONNREFUSED
        assert took < 2.0, f"the control call waited {took:.2f}s; it must not wait out the hanging handler"
        assert not caller.is_alive(), "the call in flight must end when the port closes, not at its own deadline"
        assert outcome["code"] is grpc.StatusCode.UNAVAILABLE, outcome
        assert outcome["details"] not in _SIMULATOR_MESSAGES + ("hang timeout",), outcome

    def test_another_provider_of_the_pool_still_answers(self, sim):
        """The control. If closing provider 1 also silenced provider 2, every
        check in this class would pass for a simulator that had simply died."""
        status, body = _set(sim["control"], f"{POOL}:1", mode="port_closed")
        assert status == 200, body
        assert _connect(_PORT["1"]) == errno.ECONNREFUSED
        assert _connect(_PORT["2"]) == 0
        with _fresh_channel(_PORT["2"]) as channel:
            assert _latest_block(channel).block.header.chain_id == "lava-sim"

    def test_the_closed_provider_records_nothing(self, sim):
        """A closed port receives nothing, so its history must not grow.

        Provider 2 is called the same way and its history DOES grow. That is
        what makes the zero meaningful: the history can see a gRPC call.
        """
        control = sim["control"]
        status, body = _set(control, f"{POOL}:1", mode="port_closed")
        assert status == 200, body
        before = {pid: _history_count(control, POOL, pid) for pid in _PORT}
        assert before["1"] == 0, "the fixture cleared the history; nothing has called provider 1 yet"
        with _fresh_channel(_PORT["1"]) as channel:
            with pytest.raises(grpc.RpcError):
                _latest_block(channel)
        with _fresh_channel(_PORT["2"]) as channel:
            _latest_block(channel)
        assert _history_count(control, POOL, "2") == before["2"] + 1
        assert _history_count(control, POOL, "1") == 0

    def test_one_request_can_close_several_providers(self, sim):
        """Both ports are closed when the one call returns, and the third
        provider, which the request did not name, still accepts."""
        third = port_of(POOL, "3", "grpc", "http2")
        status, body = _post(
            f"{sim['control']}/scenario",
            {"providers": {f"{POOL}:1": {"mode": "port_closed"}, f"{POOL}:2": {"mode": "port_closed"}}},
        )
        states = (_connect(_PORT["1"]), _connect(_PORT["2"]), _connect(third))
        assert status == 200, body
        assert states == (errno.ECONNREFUSED, errno.ECONNREFUSED, 0)

    def test_get_scenario_shows_the_mode(self, sim):
        status, body = _set(sim["control"], f"{POOL}:1", mode="port_closed")
        assert status == 200, body
        assert _mode(sim["control"], f"{POOL}:1") == "port_closed"
        assert _mode(sim["control"], f"{POOL}:2") == "success"

    def test_readiness_names_the_closed_port_and_stays_ready(self, sim):
        """The pod's readiness probe reads /ready. A port closed on purpose must
        not fail it, or one closed provider takes the whole pod out of its
        Service and every router loses every provider."""
        _, baseline = _get(f"{sim['control']}/ready")
        assert baseline["closed_by_scenario"] == []
        status, body = _set(sim["control"], f"{POOL}:1", mode="port_closed")
        assert status == 200, body
        ready_status, ready = _get(f"{sim['control']}/ready")
        assert ready_status == 200, ready
        assert ready["closed_by_scenario"] == [_PORT["1"]]
        assert ready["expected"] == baseline["expected"] - 1, "exactly the closed port leaves the check"
        assert ready["listening"] == ready["expected"]
        _post(f"{sim['control']}/reset", {})
        ready_status, ready = _get(f"{sim['control']}/ready")
        assert (ready_status, ready["closed_by_scenario"], ready["expected"]) == (200, [], baseline["expected"])

    def test_an_endpoint_that_is_not_grpc_closes_too(self, sim):
        """eth-sim:1 serves jsonrpc over http and ws. Each of the two ports has
        a gate and a loop, as a gRPC port has, so the mode closes both. The
        other tests of these ports are in
        tests/integration/test_http_and_ws_port_closed.py."""
        http_port, ws_port = port_of("eth-sim", "1"), port_of("eth-sim", "1", transport="ws")
        status, body = _set(sim["control"], "eth-sim:1", mode="port_closed")
        ports = (_connect(http_port), _connect(ws_port))
        assert status == 200, body
        assert ports == (errno.ECONNREFUSED, errno.ECONNREFUSED)


# ── the port opens again ─────────────────────────────────────────────────────


class TestThePortOpensAgain:
    def _close(self, sim) -> None:
        status, body = _set(sim["control"], f"{POOL}:1", mode="port_closed")
        assert status == 200, body
        assert _connect(_PORT["1"]) == errno.ECONNREFUSED, "the port must be closed before it can reopen"

    def test_mode_success_reopens_it_on_the_line_after_the_call(self, sim):
        self._close(sim)
        status, body = _set(sim["control"], f"{POOL}:1", mode="success")
        accepted = _connect(_PORT["1"])
        assert status == 200, body
        assert accepted == 0, f"port {_PORT['1']} must accept once the control call has returned, got {accepted}"
        with _fresh_channel(_PORT["1"]) as channel:
            assert _latest_block(channel).block.header.chain_id == "lava-sim"

    def test_another_fault_mode_reopens_it_and_answers_as_that_mode(self, sim):
        """Leaving port_closed for `down` is still leaving it: the port accepts
        again, and what answers is the status reply `down` has always sent."""
        self._close(sim)
        status, body = _set(sim["control"], f"{POOL}:1", mode="down")
        accepted = _connect(_PORT["1"])
        assert status == 200, body
        assert accepted == 0
        with _fresh_channel(_PORT["1"]) as channel:
            with pytest.raises(grpc.RpcError) as caught:
                _latest_block(channel)
        assert (caught.value.code(), caught.value.details()) == (grpc.StatusCode.UNAVAILABLE, "provider down")

    def test_a_channel_opened_before_the_fault_serves_again(self, sim):
        """The same channel, through closed and back. The client reconnects by
        itself after its own back-off, so the call waits for the connection."""
        with _fresh_channel(_PORT["1"]) as channel:
            assert _latest_block(channel).block.header.height == GRPC_LATEST_BLOCK
            self._close(sim)
            with pytest.raises(grpc.RpcError):
                _latest_block(channel)
            status, body = _set(sim["control"], f"{POOL}:1", mode="success")
            assert status == 200, body
            assert _latest_block(channel, wait_for_ready=True).block.header.chain_id == "lava-sim"

    @pytest.mark.parametrize(
        "path, body",
        [
            ("/reset", {}),
            ("/reset/all", {}),
            ("/reset", {"pool": POOL}),
            ("/reset/all", {"pool": POOL}),
        ],
    )
    def test_a_reset_reopens_it_on_the_line_after_the_call(self, sim, path, body):
        self._close(sim)
        status, reply = _post(f"{sim['control']}{path}", body)
        accepted = _connect(_PORT["1"])
        assert status == 200, reply
        assert accepted == 0, f"{path} {body} must reopen port {_PORT['1']}, connect_ex gave {accepted}"
        assert _mode(sim["control"], f"{POOL}:1") == "success"
        with _fresh_channel(_PORT["1"]) as channel:
            assert _latest_block(channel).block.header.chain_id == "lava-sim"

    @pytest.mark.parametrize("path", ["/reset", "/reset/all"])
    def test_a_reset_of_another_pool_leaves_it_closed(self, sim, path):
        """The control for the test above: a reset reopens the port because it
        reset THIS provider, not because any reset reopens every port."""
        self._close(sim)
        status, reply = _post(f"{sim['control']}{path}", {"pool": "eth-sim"})
        assert status == 200, reply
        assert _connect(_PORT["1"]) == errno.ECONNREFUSED
        assert _mode(sim["control"], f"{POOL}:1") == "port_closed"

    def test_clearing_history_leaves_it_closed(self, sim):
        self._close(sim)
        status, reply = _post(f"{sim['control']}/history/clear", {})
        assert status == 200, reply
        assert _connect(_PORT["1"]) == errno.ECONNREFUSED

    def test_the_time_to_live_sweep_reopens_it_on_the_line_after_the_pass(self, sim):
        """The sweep is a control write like a reset: it waits for the port.

        ``_revert_stale_scenarios`` is the pass the sweep daemon runs, called
        here with a clock one second past the time-to-live. The port accepts
        when the pass returns, with no sleep.
        """
        self._close(sim)
        ttl_s = 900
        server_module._revert_stale_scenarios(sim["server"].control, ttl_s, time.time() + ttl_s + 1)
        accepted = _connect(_PORT["1"])
        assert _mode(sim["control"], f"{POOL}:1") == "success", "the sweep must have reverted the scenario"
        assert accepted == 0, f"the port must accept once the sweep pass has returned, connect_ex gave {accepted}"
        with _fresh_channel(_PORT["1"]) as channel:
            assert _latest_block(channel).block.header.chain_id == "lava-sim"

    def test_a_sweep_inside_the_time_to_live_leaves_it_closed(self, sim):
        """The control for the test above: the sweep reopens the port because the
        scenario was stale, not because any sweep does."""
        self._close(sim)
        server_module._revert_stale_scenarios(sim["server"].control, 900, time.time())
        time.sleep(3 * server_module._GRPC_PORT_POLL_S)
        assert _mode(sim["control"], f"{POOL}:1") == "port_closed"
        assert _connect(_PORT["1"]) == errno.ECONNREFUSED

    def test_closing_and_opening_many_times_keeps_working(self, sim):
        for round_number in range(10):
            status, body = _set(sim["control"], f"{POOL}:1", mode="port_closed")
            assert (status, _connect(_PORT["1"])) == (200, errno.ECONNREFUSED), (round_number, body)
            status, body = _set(sim["control"], f"{POOL}:1", mode="success")
            assert (status, _connect(_PORT["1"])) == (200, 0), (round_number, body)

    def test_asking_twice_for_the_same_state_answers_200_both_times(self, sim):
        """A call that changes nothing still has to answer for the state the
        port is in: closed stays closed, open stays open."""
        for mode, expected in (("port_closed", errno.ECONNREFUSED), ("success", 0)):
            for attempt in (1, 2):
                status, body = _set(sim["control"], f"{POOL}:1", mode=mode)
                assert (status, _connect(_PORT["1"])) == (200, expected), (mode, attempt, body)


# ── what the control API refuses ─────────────────────────────────────────────


def _assert_refused_and_nothing_changed(sim, key: str, port: int, block: dict, reason: str) -> None:
    """One refusal: HTTP 400, the reason named, nothing stored, the port open."""
    before = _mode(sim["control"], key)
    status, body = _set(sim["control"], key, **block)
    assert status == 400, f"{block} must be refused, got {status}: {body}"
    assert reason in body["error"], f"the refusal must say why; wanted {reason!r} in {body['error']!r}"
    assert key in body["error"], "the refusal must name the provider it refused"
    assert _mode(sim["control"], key) == before, "a refused block must not be stored"
    assert _connect(port) == 0, "a refused block must leave the port open"


class TestRefusals:
    def test_a_block_that_targets_no_endpoint_is_refused(self, sim):
        """The gRPC provider has no ws endpoint, so this filter matches nothing."""
        _assert_refused_and_nothing_changed(
            sim,
            f"{POOL}:1",
            _PORT["1"],
            {"mode": "port_closed", "transports": ["ws"]},
            "targets no endpoint of this provider",
        )

    def test_a_per_method_override_is_refused(self, sim):
        _assert_refused_and_nothing_changed(
            sim,
            f"{POOL}:1",
            _PORT["1"],
            {"responses": {"GetLatestBlock": {"mode": "port_closed"}}},
            "per-method mode='port_closed' not allowed",
        )

    def test_a_per_route_override_on_rest_is_refused(self, sim):
        """The REST shape of the same override: a list of [[verb, template], cfg]."""
        _assert_refused_and_nothing_changed(
            sim,
            "lava-sim-rest:1",
            port_of("lava-sim-rest", "1", "rest", "http"),
            {"responses": [[["GET", "/cosmos/base/tendermint/v1beta1/blocks/latest"], {"mode": "port_closed"}]]},
            "per-method mode='port_closed' not allowed",
        )

    def test_then_mode_is_refused(self, sim):
        _assert_refused_and_nothing_changed(
            sim,
            f"{POOL}:1",
            _PORT["1"],
            {"mode": "error", "fail_first_n": 2, "then_mode": "port_closed"},
            "then_mode 'port_closed' is not accepted",
        )

    @pytest.mark.parametrize(
        "field_name, value",
        [
            ("fail_first_n", 2),
            ("error_probability", 0.5),
            ("latency_ms", 300),
            ("corruption_mode", "invalid_proto"),
            ("pause_at", "mid_body"),
        ],
    )
    def test_a_field_that_needs_a_request_to_arrive_is_refused(self, sim, field_name, value):
        _assert_refused_and_nothing_changed(
            sim,
            f"{POOL}:1",
            _PORT["1"],
            {"mode": "port_closed", field_name: value},
            f"{field_name}={value!r} cannot apply with mode 'port_closed'",
        )

    def test_a_field_stored_by_an_earlier_call_still_refuses_the_mode(self, sim):
        """A POST merges. fail_first_n set in one call and port_closed in the next
        add up to the same block, so the second call is refused too."""
        key = f"{POOL}:1"
        status, body = _set(sim["control"], key, mode="error", fail_first_n=2)
        assert status == 200, body
        _assert_refused_and_nothing_changed(
            sim, key, _PORT["1"], {"mode": "port_closed"}, "fail_first_n=2 cannot apply with mode 'port_closed'"
        )

    def test_the_same_fields_at_their_defaults_are_accepted(self, sim):
        """A client that always sends the whole block sends these at their
        defaults. That is not a combination, and it must not be refused."""
        status, body = _set(
            sim["control"],
            f"{POOL}:1",
            mode="port_closed",
            fail_first_n=0,
            then_mode="success",
            error_probability=0.0,
            latency_ms=0,
            corruption_mode=None,
            pause_at=None,
            transports=["http2"],
        )
        assert status == 200, body
        assert _connect(_PORT["1"]) == errno.ECONNREFUSED

    def test_what_the_provider_would_answer_is_not_a_combination_either(self, sim):
        """responses, blocks_behind and http_status say WHAT the provider answers,
        on every endpoint it has and whatever its mode. They are not faults on a
        request, so they are accepted, and the port still closes."""
        status, body = _set(
            sim["control"],
            f"{POOL}:1",
            mode="port_closed",
            blocks_behind=3,
            http_status=429,
            responses={"GetLatestBlock": {"result": {"height": 7}}},
        )
        assert status == 200, body
        assert _connect(_PORT["1"]) == errno.ECONNREFUSED
        status, body = _set(sim["control"], f"{POOL}:1", mode="success")
        assert status == 200, body
        with _fresh_channel(_PORT["1"]) as channel:
            assert _latest_block(channel).block.header.height == 7, "the stored override must be served once open"


# ── a port that does not change is an error, never a 200 ─────────────────────


def test_a_port_that_does_not_close_in_time_is_an_error_naming_it(sim, monkeypatch):
    """The listener is made deaf: on its own thread the scenario always reads
    as "open", so the port stays open whatever is asked. Every other thread,
    the control API included, still reads the real scenario. The control call
    must then say the port did not close."""
    gate = sim["server"].control.port_gates[_PORT["1"]]
    real_wish = gate.wants_closed

    def deaf_on_the_listeners_thread() -> bool:
        if threading.current_thread().name == f"grpc-{POOL}:1":
            return False
        return real_wish()

    monkeypatch.setattr(gate, "wants_closed", deaf_on_the_listeners_thread)
    monkeypatch.setattr(control_api, "_PORT_SETTLE_S", 0.5)

    status, body = _set(sim["control"], f"{POOL}:1", mode="port_closed")

    assert status == 500, f"a port that did not close must not answer {status}: {body}"
    for named in (repr(POOL), "provider '1'", f"port {_PORT['1']}", "is not closed"):
        assert named in body["error"], f"the error must name {named}; got {body['error']!r}"
    assert _connect(_PORT["1"]) == 0, "the port is still open, which is what the error says"


# ── a request that answers 400 changes no provider ───────────────────────────

# A block that passes every per-field rule and is refused by a rule the scenario
# holds itself (ScenarioConfig._validate). It is the SECOND block of each request
# below; the first block is valid and must not be written.
_REFUSED_BY_THE_SCENARIO = [
    pytest.param({"mode": "down", "transports": ["grpc"]}, "unknown transport(s)", id="transports-grpc"),
    pytest.param({"mode": "port_closed", "ports": []}, "ports must not be empty", id="ports-empty"),
    pytest.param({"mode": "down", "pause_at": "mid_body"}, "cannot apply with mode='down'", id="pause-with-down"),
]


def _scenario_of(control: str, key: str) -> dict:
    _, body = _get(f"{control}/scenario")
    return body["providers"][key]


class TestARefusedRequestChangesNoProvider:
    @pytest.mark.parametrize("second_block, reason", _REFUSED_BY_THE_SCENARIO)
    def test_a_port_closed_block_before_a_refused_block_closes_no_port(self, sim, second_block, reason):
        """The port is read twice: right after the answer, and one second later,
        because a scenario stored by a refused request closes the port on the
        listener's next poll and not at once."""
        control, first, second = sim["control"], f"{POOL}:1", f"{POOL}:2"
        before = {key: _scenario_of(control, key) for key in (first, second)}
        assert before[first]["mode"] == "success", "the fixture reset every provider"

        status, body = _post(
            f"{control}/scenario", {"providers": {first: {"mode": "port_closed"}, second: second_block}}
        )
        at_once = _connect(_PORT["1"])
        stored = {key: _scenario_of(control, key) for key in (first, second)}
        time.sleep(1.0)
        later = _connect(_PORT["1"])

        assert status == 400, f"{second_block} must be refused, got {status}: {body}"
        assert reason in body["error"] and second in body["error"], body
        assert (stored, at_once, later) == (before, 0, 0), (
            f"a request answered 400 changed a provider: {first} is stored as mode "
            f"{stored[first]['mode']!r}; a connection to port {_PORT['1']} gave {at_once} right after "
            f"the answer and {later} one second later (0 is accepted)"
        )
        with _fresh_channel(_PORT["1"]) as channel:
            assert _latest_block(channel).block.header.chain_id == "lava-sim"

    def test_a_down_block_before_a_refused_block_is_not_stored(self, sim):
        """The same rule for a mode that moves no port: the first provider stays
        in `success` and answers."""
        control, first, second = sim["control"], f"{POOL}:1", f"{POOL}:2"
        status, body = _post(
            f"{control}/scenario",
            {"providers": {first: {"mode": "down"}, second: {"mode": "down", "transports": ["grpc"]}}},
        )
        assert status == 400, body
        assert "unknown transport(s)" in body["error"] and second in body["error"], body
        assert _mode(control, first) == "success", "the valid block beside a refused one was stored"
        with _fresh_channel(_PORT["1"]) as channel:
            assert _latest_block(channel).block.header.chain_id == "lava-sim"


# ── two control writes on one provider at the same moment ────────────────────


def _close(sim) -> tuple[int, dict]:
    return _set(sim["control"], f"{POOL}:1", mode="port_closed")


# Every control call that opens the port again, each next to the same closer.
_OPENERS = {
    "mode=success": lambda sim: _set(sim["control"], f"{POOL}:1", mode="success"),
    "POST /reset": lambda sim: _post(f"{sim['control']}/reset", {"pool": POOL}),
    "POST /reset/all": lambda sim: _post(f"{sim['control']}/reset/all", {"pool": POOL}),
}


@pytest.mark.parametrize("opener", list(_OPENERS))
def test_closing_and_opening_one_provider_at_the_same_moment_never_answers_500(sim, opener):
    """One caller closes the port and one opens it, started together, 20 times.

    Each is answered for its own write, so both get 200. After each round the
    port is where the stored scenario puts it, whichever caller came last.
    """
    control, key, port = sim["control"], f"{POOL}:1", _PORT["1"]
    writes = {"close": _close, opener: _OPENERS[opener]}
    for round_number in range(20):
        replies = {}
        together = threading.Barrier(2)

        def call(name: str) -> None:
            together.wait(timeout=10)
            replies[name] = writes[name](sim)

        callers = [threading.Thread(target=call, args=(name,)) for name in writes]
        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join()

        assert sorted(replies) == sorted(writes), f"round {round_number}: a caller got no answer"
        for name, (status, body) in replies.items():
            assert status == 200, f"round {round_number}: the caller of {name!r} got {status}: {body}"
        stored = _mode(control, key)
        accepted = _connect(port) == 0
        assert accepted == (stored != "port_closed"), (
            f"round {round_number}: the stored mode is {stored!r} and the port "
            f"{'accepts' if accepted else 'refuses'} a connection"
        )


def test_the_time_to_live_sweep_waits_for_a_control_write_in_progress(sim):
    """The sweep pass is the fourth writer that moves a port, and it answers
    nobody, so the test above cannot see it lose. Here the lock is held as a
    control call holds it between its write and its settle: the pass must leave
    the scenario alone until the lock is free, and then revert it and wait for
    the port.
    """
    control, api, key = sim["control"], sim["server"].control, f"{POOL}:1"
    status, body = _close(sim)
    assert status == 200, body
    sweeper = threading.Thread(
        target=server_module._revert_stale_scenarios, args=(api, 900, time.time() + 901), daemon=True
    )
    with api.port_lock:
        sweeper.start()
        sweeper.join(timeout=0.5)
        waiting, stored_meanwhile = sweeper.is_alive(), _mode(control, key)
    sweeper.join(timeout=10)
    assert (waiting, stored_meanwhile) == (
        True,
        "port_closed",
    ), "the sweep reverted a scenario while another control write held the port lock"
    assert not sweeper.is_alive()
    assert (_mode(control, key), _connect(_PORT["1"])) == ("success", 0)


# ── /ready while the simulator itself reopens a port ─────────────────────────


def test_ready_is_200_right_after_the_time_to_live_reopens_a_port(sim):
    """/ready is read on the line after the sweep pass, 20 times. The pass has
    waited for the port, so the reply never names it as missing."""
    control, api = sim["control"], sim["server"].control
    _, baseline = _get(f"{control}/ready")
    not_ready = []
    for round_number in range(20):
        status, body = _set(control, f"{POOL}:1", mode="port_closed")
        assert status == 200, body
        server_module._revert_stale_scenarios(api, 900, time.time() + 901)
        ready_status, ready = _get(f"{control}/ready")
        if (ready_status, ready.get("expected"), ready.get("closed_by_scenario")) != (200, baseline["expected"], []):
            not_ready.append((round_number, ready_status, ready))
    assert not not_ready, f"/ready was not 200 in {len(not_ready)} of 20 rounds; first: {not_ready[0]}"


def test_ready_asked_part_way_through_a_reopen_waits_for_it(sim, monkeypatch):
    """/ready is asked BETWEEN the write that reverts the scenario and the port
    accepting again, which is where a sweep pass on its own thread leaves it.

    The test plays the pass step by step, holding the lock the pass holds:
    revert the scenario, ask /ready from another thread, then wait for the
    port and let go. /ready must wait for the lock and then answer 200.

    The listener's own poll is slowed first, so the port moves only when it is
    asked to and stays closed for as long as the test needs it closed.
    """
    control, api = sim["control"], sim["server"].control
    provider = sim["registry"].provider(POOL, "1")
    monkeypatch.setattr(server_module, "_GRPC_PORT_POLL_S", 30.0)
    status, body = _set(control, f"{POOL}:1", mode="port_closed")  # wakes the listener: its next wait is the slow one
    assert status == 200, body
    answer = {}

    def ask_ready() -> None:
        answer["ready"] = _get(f"{control}/ready")

    asker = threading.Thread(target=ask_ready)
    with api.port_lock:
        provider.scenario.reset()  # the wish says open; the port is still closed
        assert _connect(_PORT["1"]) == errno.ECONNREFUSED, "the port must still be closed when /ready is asked"
        asker.start()
        time.sleep(0.2)  # /ready has arrived and is waiting for the lock, or has answered without it
        assert api.settle_ports_of([provider]) == ""
    asker.join(timeout=10)
    ready_status, ready = answer["ready"]
    assert ready_status == 200, f"/ready named a port the simulator was reopening: {ready}"
    assert ready["closed_by_scenario"] == []


# ── wait_ready ───────────────────────────────────────────────────────────────


def test_wait_ready_leaves_out_a_port_a_scenario_closed(sim):
    """wait_ready raises for a port that does not accept. A port closed on
    purpose is not one of those, as in /ready."""
    status, body = _set(sim["control"], f"{POOL}:1", mode="port_closed")
    assert status == 200, body
    assert _connect(_PORT["1"]) == errno.ECONNREFUSED
    sim["server"].wait_ready(timeout_s=1.0)


# ── filters, on a provider with more than one port ───────────────────────────

_MIXED_CONTROL = 29711
_MIXED_GRPC_A, _MIXED_GRPC_B, _MIXED_REST = 28711, 28712, 28713
_MIXED_OTHER = 28714
_MIXED_ROWS = [
    (
        "mixed-sim",
        "lava",
        "1",
        "MixedPrimaryProvider1",
        False,
        "",
        (("grpc", "http2", _MIXED_GRPC_A), ("grpc", "http2", _MIXED_GRPC_B), ("rest", "http", _MIXED_REST)),
    ),
    ("mixed-sim", "lava", "2", "MixedPrimaryProvider2", False, "", (("grpc", "http2", _MIXED_OTHER),)),
]


@pytest.fixture(scope="module")
def mixed():
    """A second simulator whose provider serves two gRPC ports and a REST port.

    No shipped provider has that shape, and the filters cannot be shown on a
    provider with one port. The table is swapped only while this server builds
    its registry.
    """
    shipped = topology.TOPOLOGY
    topology.TOPOLOGY = _MIXED_ROWS
    try:
        srv = server_module.SimulatorServer(
            host="127.0.0.1", control_port=_MIXED_CONTROL, scenario_ttl_s=0, cache_ports={}, resp_proxy_ports={}
        )
    finally:
        topology.TOPOLOGY = shipped
    srv.start()
    srv.wait_ready(20.0)
    yield f"http://127.0.0.1:{_MIXED_CONTROL}"
    _post(f"http://127.0.0.1:{_MIXED_CONTROL}/reset/all", {})
    srv.stop()


def _rest_answers(port: int) -> bool:
    status, _ = _get(f"http://127.0.0.1:{port}/cosmos/base/tendermint/v1beta1/blocks/latest")
    return status == 200


class TestFiltersOnAProviderWithSeveralPorts:
    @pytest.fixture(autouse=True)
    def reset_mixed(self, mixed):
        _post(f"{mixed}/reset/all", {})
        yield

    def test_with_no_filter_every_port_of_the_provider_closes(self, mixed):
        status, body = _set(mixed, "mixed-sim:1", mode="port_closed")
        assert status == 200, body
        closed = (_connect(_MIXED_GRPC_A), _connect(_MIXED_GRPC_B), _connect(_MIXED_REST))
        assert closed == (errno.ECONNREFUSED, errno.ECONNREFUSED, errno.ECONNREFUSED)
        assert _connect(_MIXED_OTHER) == 0, "the other provider must be untouched"

    @pytest.mark.parametrize(
        "closed, open_port", [(_MIXED_GRPC_A, _MIXED_GRPC_B), (_MIXED_GRPC_B, _MIXED_GRPC_A)], ids=["first", "second"]
    )
    def test_a_ports_filter_closes_only_the_named_port(self, mixed, closed, open_port):
        """Run for both ports: a bug that always closed the first endpoint would
        pass one of these and fail the other."""
        status, body = _set(mixed, "mixed-sim:1", mode="port_closed", ports=[closed])
        assert status == 200, body
        assert _connect(closed) == errno.ECONNREFUSED
        assert _connect(open_port) == 0
        with _fresh_channel(open_port) as channel:
            assert _latest_block(channel).block.header.chain_id == "lava-sim"
        assert _rest_answers(_MIXED_REST), "the provider's REST port must keep serving"
        assert _connect(_MIXED_OTHER) == 0, "the other provider must be untouched"

    def test_a_transports_filter_closes_both_grpc_ports_and_not_the_rest_port(self, mixed):
        status, body = _set(mixed, "mixed-sim:1", mode="port_closed", transports=["http2"])
        assert status == 200, body
        assert (_connect(_MIXED_GRPC_A), _connect(_MIXED_GRPC_B)) == (errno.ECONNREFUSED, errno.ECONNREFUSED)
        assert _rest_answers(_MIXED_REST)
        assert _connect(_MIXED_OTHER) == 0

    def test_moving_the_filter_moves_the_closed_port(self, mixed):
        """One scenario per provider: a second post with another port MOVES the
        fault. The port that left the filter must be open when the call returns."""
        status, body = _set(mixed, "mixed-sim:1", mode="port_closed", ports=[_MIXED_GRPC_A])
        assert status == 200, body
        status, body = _set(mixed, "mixed-sim:1", ports=[_MIXED_GRPC_B])
        assert status == 200, body
        assert (_connect(_MIXED_GRPC_A), _connect(_MIXED_GRPC_B)) == (0, errno.ECONNREFUSED)

    def test_widening_a_stored_filter_onto_the_rest_port_closes_it_too(self, mixed):
        status, body = _set(mixed, "mixed-sim:1", mode="port_closed", ports=[_MIXED_GRPC_A])
        assert status == 200, body
        status, body = _set(mixed, "mixed-sim:1", ports=[_MIXED_GRPC_A, _MIXED_REST])
        assert status == 200, body
        assert _connect(_MIXED_GRPC_A) == errno.ECONNREFUSED, "the stored fault must still hold"
        assert _connect(_MIXED_REST) == errno.ECONNREFUSED, "the REST port is in the filter now"
        assert _connect(_MIXED_GRPC_B) == 0, "the port outside the filter stays open"


# ── a port that cannot bind ──────────────────────────────────────────────────


def test_a_port_that_cannot_bind_is_an_error_and_not_ready_until_it_can(mixed, monkeypatch):
    """Another socket holds the port while the scenario asks for it open.

    The other socket is bound and does not listen, so a connection to the port
    is still refused. The listener's bind fails on every pass, and each pass
    tries it again. A port in that state is broken, not closed on purpose:

      - the control call that asked for it open answers 500 and names it;
      - /ready answers 503 and names it, while that call is still waiting (the
        probe does not hang on a control call that cannot finish) and after;
      - once the other socket is gone the listener binds by itself, and the
        next control call answers 200.

    On the second simulator: its provider 2 has had no connection closed from
    the server's side, so nothing else stops the other socket from binding.
    """
    key, port = "mixed-sim:2", _MIXED_OTHER
    monkeypatch.setattr(control_api, "_PORT_SETTLE_S", 2.0)
    monkeypatch.setattr(server_module, "_READY_LOCK_WAIT_S", 0.3)
    _post(f"{mixed}/reset/all", {})
    status, body = _set(mixed, key, mode="port_closed")
    assert status == 200, body

    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        holder.bind(("127.0.0.1", port))
        reply = {}
        caller = threading.Thread(target=lambda: reply.update(answer=_set(mixed, key, mode="success")))
        caller.start()
        deadline = time.monotonic() + 2.0
        while _mode(mixed, key) != "success" and time.monotonic() < deadline:
            time.sleep(0.01)
        asked = time.monotonic()
        during_status, during = _get(f"{mixed}/ready")  # the control call is waiting for the port
        waited = time.monotonic() - asked
        caller.join(timeout=10)
        status, body = reply["answer"]
        after_status, after = _get(f"{mixed}/ready")

        assert status == 500, f"a port that could not bind must not answer {status}: {body}"
        for named in ("pool 'mixed-sim'", "provider '2'", f"port {port}", "is not accepting connections"):
            assert named in body["error"], f"the error must name {named}; got {body['error']!r}"
        assert _mode(mixed, key) == "success", "the scenario is stored, as the error says"
        for when, ready_status, ready in (("during", during_status, during), ("after", after_status, after)):
            assert ready_status == 503, f"/ready {when} the control call must be 503, got {ready_status}: {ready}"
            assert ready["missing_ports"] == [port], (when, ready)
            assert ready["closed_by_scenario"] == [], (when, ready)
        assert waited < 1.5, f"/ready waited {waited:.2f}s on a control call that could not finish"
    finally:
        holder.close()

    deadline = time.monotonic() + 5.0
    while _connect(port) != 0 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert _connect(port) == 0, "the listener must bind by itself once the port is free"
    status, body = _set(mixed, key, mode="success")
    assert status == 200, body
    ready_status, ready = _get(f"{mixed}/ready")
    assert (ready_status, ready.get("closed_by_scenario")) == (200, []), ready
    with _fresh_channel(port) as channel:
        assert _latest_block(channel).block.header.chain_id == "lava-sim"
