"""
Integration tests for the ``earliest_block`` Ethereum quirk: a pruned node.

Runs against the shared in-process simulator (see conftest.py). A pruned node
keeps only recent history. The router's archive check asks for the block
``earliest`` and expects block 0, so a provider with this quirk fails it, while
every check and relay at a recent height is answered as before. That is the
"provider 1 is a pruned node" scenario of MAG-4505 (A2 and D2).

Run with:
  pytest tests/test_simulator_pruned_node.py -v
"""

import json
import urllib.error
import urllib.request

import pytest

from provider_simulator.topology import port_of, ports_of

_P1 = f"http://127.0.0.1:{port_of('eth-sim', '1')}"
_P2 = f"http://127.0.0.1:{port_of('eth-sim', '2')}"
# The one provider that serves one interface and transport at two addresses.
_TWO_KEY = "eth-failover-twoaddr-sim:1"
_TWO_PORTS = ports_of("eth-failover-twoaddr-sim", "1")
_TWO_A, _TWO_B = (f"http://127.0.0.1:{port}" for port in _TWO_PORTS)
_ADDRESS = "0x" + "a" * 40


def _post(url: str, body: dict) -> tuple[int, dict]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, json.loads(raw) if raw else {}


def _get(url: str) -> tuple[int, dict]:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, json.loads(resp.read())


def _rpc(url: str, method: str, params: list) -> dict:
    status, body = _post(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    assert status == 200, (method, status, body)
    return body


def _head(url: str) -> int:
    return int(_rpc(url, "eth_blockNumber", [])["result"], 16)


@pytest.fixture(autouse=True)
def clean_state(sim):
    _post(sim["control"] + "/reset/all", {})
    yield
    _post(sim["control"] + "/reset/all", {})


def _prune(sim, earliest: int | None, key: str = "eth-sim:1") -> tuple[int, dict]:
    return _post(sim["control"] + "/scenario", {"providers": {key: {"earliest_block": earliest}}})


def test_a_pruned_provider_answers_its_earliest_block_and_its_peer_does_not(sim):
    earliest = _head(_P1) - 1_000
    status, body = _prune(sim, earliest)
    assert status == 200, body

    got = _rpc(_P1, "eth_getBlockByNumber", ["earliest", False])["result"]["number"]
    assert int(got, 16) == earliest
    peer = _rpc(_P2, "eth_getBlockByNumber", ["earliest", False])["result"]["number"]
    assert peer == "0x0", "the quirk is per provider"


def test_a_pruned_provider_holds_recent_blocks_and_state_only(sim):
    earliest = _head(_P1) - 1_000
    _prune(sim, earliest)

    assert _rpc(_P1, "eth_getBlockByNumber", [hex(earliest - 1), False])["result"] is None
    assert _rpc(_P1, "eth_getBlockByNumber", [hex(earliest), False])["result"]["number"] == hex(earliest)
    assert isinstance(_rpc(_P1, "eth_getBlockByNumber", ["latest", False])["result"], dict)

    old = _rpc(_P1, "eth_getBalance", [_ADDRESS, hex(earliest - 1)])
    assert old["error"] == {"code": -32000, "message": "missing trie node"}
    assert "error" not in _rpc(_P1, "eth_getBalance", [_ADDRESS, "latest"])
    assert "error" not in _rpc(_P2, "eth_getBalance", [_ADDRESS, hex(earliest - 1)]), "its peer is an archive node"


def test_the_quirk_reads_back_and_a_reset_makes_the_provider_an_archive_node_again(sim):
    _prune(sim, 123)
    _, body = _get(sim["control"] + "/scenario")
    assert body["providers"]["eth-sim:1"]["earliest_block"] == 123
    assert body["providers"]["eth-sim:2"]["earliest_block"] is None

    _post(sim["control"] + "/reset/all", {})
    assert _rpc(_P1, "eth_getBlockByNumber", ["earliest", False])["result"]["number"] == "0x0"


@pytest.mark.parametrize("bad", [-5, "0x10", True])
def test_an_earliest_block_that_is_not_a_height_is_refused_and_writes_nothing(sim, bad):
    status, body = _prune(sim, bad)
    assert status == 400, body
    assert _rpc(_P1, "eth_getBlockByNumber", ["earliest", False])["result"]["number"] == "0x0"


def test_a_provider_of_another_chain_refuses_the_quirk(sim):
    """A setting that no listener of that chain reads is refused, not stored."""
    status, body = _prune(sim, 10, key="btc-sim:1")
    assert status == 400, body
    assert "earliest_block" in json.dumps(body)


def test_a_provider_pruned_at_one_of_its_addresses_is_an_archive_node_at_the_other(sim):
    """``earliest_block_ports`` scopes the pruned node to the addresses it names, so a
    router that checks each node url at its own address finds archive at one only."""
    assert len(_TWO_PORTS) == 2, _TWO_PORTS
    earliest = _head(_TWO_A) - 1_000
    status, body = _post(
        sim["control"] + "/scenario",
        {"providers": {_TWO_KEY: {"earliest_block": earliest, "earliest_block_ports": [_TWO_PORTS[0]]}}},
    )
    assert status == 200, body

    assert int(_rpc(_TWO_A, "eth_getBlockByNumber", ["earliest", False])["result"]["number"], 16) == earliest
    assert _rpc(_TWO_B, "eth_getBlockByNumber", ["earliest", False])["result"]["number"] == "0x0"
    assert _rpc(_TWO_A, "eth_getBlockByNumber", [hex(earliest - 1), False])["result"] is None
    assert _rpc(_TWO_B, "eth_getBlockByNumber", [hex(earliest - 1), False])["result"]["number"] == hex(earliest - 1)
    _, read = _get(sim["control"] + "/scenario")
    assert read["providers"][_TWO_KEY]["earliest_block_ports"] == [_TWO_PORTS[0]]

    status, body = _post(sim["control"] + "/scenario", {"providers": {_TWO_KEY: {"earliest_block_ports": None}}})
    assert status == 200, body
    assert (
        int(_rpc(_TWO_B, "eth_getBlockByNumber", ["earliest", False])["result"]["number"], 16) == earliest
    ), "with no ports named, the pruned node applies at every address"


def test_earliest_block_ports_naming_an_address_the_provider_does_not_serve_is_refused(sim):
    status, body = _post(
        sim["control"] + "/scenario",
        {"providers": {"eth-sim:1": {"earliest_block": 10, "earliest_block_ports": [_TWO_PORTS[0]]}}},
    )
    assert status == 400, body
    assert "earliest_block_ports" in json.dumps(body)
    assert _rpc(_P1, "eth_getBlockByNumber", ["earliest", False])["result"]["number"] == "0x0", "nothing was written"
