# gRPC Uses Listener.serve Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A gRPC call of the simulator goes through `Listener.serve`, the one request flow, and `GrpcListener.plan` and `GrpcPlan` are deleted, with no change of a reply or of a history row but the one that the design names.

**Architecture:** `GrpcListener` becomes a subclass of `Listener` and fills hooks. `Listener` gets four hooks, and their defaults keep JSON-RPC, REST and Tendermint RPC as they are. The servicer classes, the reply builders and the registration move from `server.py` into `provider_simulator/listeners/grpc.py`; the gRPC adapter in `server.py` performs a `ServeResult` and names no protobuf class. The per-method error of gRPC moves into `LavaChain._build_grpc`, as plain data.

**Tech Stack:** Python 3.12, `grpcio` and `grpcio-reflection` 1.81.1 (`grpc.aio`), the compiled protobuf stubs in `cosmos_pb2/`, pytest 9.1.1 with pytest-timeout 2.4.0. The checks of CI: black 26.5.1, ruff 0.15.20 and mypy 2.2.0.

**Spec:** `docs/superpowers/specs/2026-10-06-one-request-flow-design.md`, version 2.3, on the branch `docs/one-request-flow-design` (pull request 137 of `provider-simulator`): sections 8.3, 9.2 (row 2), 9.3 (differences 1 to 6), 10 ("Step 2", pull request 2b), 11, 12, 13 and 14.6. The decision record `docs/adr/001-one-request-flow.md`. This plan is pull request 2b of that design. The plan of pull request 2a, `docs/superpowers/plans/2026-10-08-pin-todays-grpc-behaviour.md`, has the tests that this plan must pass, and its Task 6 has three questions that this plan answers below.

## The state of this plan

Written on 2026-10-08, after pull request 2a merged as the commit `273429e` of `main`. Nothing of this plan is done.

Each code block below is written from a READ of `main` at `273429e`. None of it has RUN. So each task runs its tests before it commits, and a step that does not go as this plan says is a stop: tell Victoria, and do not work around it.

## Choices of this plan that the design does not fix

Victoria can change each one. The first two answer questions of the review of pull request 2a.

1. **The reply message is built after the history row is finished.** `Listener.serve` returns the data of the chain in a `GrpcReply`, and the adapter asks the listener module to build the protobuf message from it (`message_of`). Section 8.3 of the design says that the `ServeResult` holds "a reply message, built from the data of the chain". This plan builds that message one step later, for one reason: `Listener.serve` finishes the row after its hook `build_success`, so a message that is built inside the hook changes the row of a call whose message cannot be built. Today that row says `success`, and the test `test_a_result_override_of_a_wrong_type_ends_the_call_after_the_row_says_success` of 2a holds it. With this choice that test passes with no edit, and `server.py` still names no protobuf class. It is the same split as on the HTTP interfaces: the flow returns a body, and the adapter turns it into bytes after the row is finished. The fix of that fault stays a pull request of its own, before 2b or after it.
2. **The servicer classes stay written by hand.** They move to `provider_simulator/listeners/grpc.py`, and `check_servicers` still compares them with the table `SERVED_METHODS`. Section 8.3 says that a new gRPC method is "one row of this table, and its content in the chain". With this plan a new method is four small changes in one file and one in the chain: its row, its servicer method, its reply builder, the entry of the builder in `_REPLY_BUILDERS`, and its content in the chain. The reason: seven tests of `main` hold the check and its texts (five in `tests/test_listener_grpc.py`, two in `tests/test_simulator_grpc.py`), and the design says that the socket tests of 2a pass with no edit. Servicers that are made from the table need an edit of those seven tests, with Victoria's acceptance. That is a later pull request, if she wants it.
3. **`RawRequest` gets one field, `message`.** It holds the request message that the gRPC library parsed. The method name travels in `path`. The other way is to put the message into `body`, which is typed `bytes`.
4. **The hook `corrupt` returns the status label of the row.** Section 8.3 asks: "Can the corruption change the row's status?" On gRPC it can. So the flow gives the label to the hook and takes the label that the hook returns.
5. **One expected value of 2a changes, on purpose.** `test_latency_ms_is_paid_today_under_a_filter_that_does_not_name_the_endpoint` holds 250 two times. After this plan it holds 0 two times and has a new name. It is row 2 of section 9.2 of the design.
6. **Twenty functions of `tests/test_listener_grpc.py` are rewritten, not thirteen.** The design counted the thirteen oldest functions. Pull request 138 added six more that call `plan`, and the function `_decide` of 2a calls it too. Each rewrite keeps what the test asserts; it changes how the test makes the call.

## Global Constraints

1. The socket tests of 2a pass with no edit: every test of `tests/test_simulator_grpc.py` and of `tests/integration/test_server_start_wiring.py`. So do `tests/test_simulator_grpc_port_closed.py` and `tests/test_control_api_port_closed.py`.
2. In the grid of `tests/test_listener_grpc.py` no expected value changes: `test_what_plan_decides_for_one_call` and `test_which_calls_wait_for_latency_ms`. Only the function `_decide` changes.
3. An expected value is not edited to pass. A pinned value that fails is a row for section 9.2 of the design that Victoria must accept, or it is a defect of this pull request. The one accepted change is choice 5.
4. `GrpcListener.serve is Listener.serve`. `GrpcPlan` does not exist. `server.py` names no protobuf class in the gRPC adapter.
5. JSON-RPC, REST and Tendermint RPC do not change: each test of `tests/test_listener_jsonrpc.py`, `tests/test_listener_rest.py`, `tests/test_listener_tendermint.py`, `tests/test_simulator*.py` and `tests/test_reply_pause.py` passes with no edit.
6. No pull request of the design edits a test file of the cache simulator or of the RESP proxy (section 12 of the design).
7. The name `server._GRPC_PORT_POLL_S`, the signature `server._run_grpc_in_thread(grpc_listener, port, host, gate)` and the thread name `grpc-<pool>:<pid>` stay: tests use them.
8. `server.py` must load with no `grpcio`: every gRPC import of `server.py` stays inside a function.
9. Linux decides. CI runs on `ubuntu-latest`. A green run on a Mac does not prove a socket test.
10. The checks of CI: `black --check .`, `ruff check .`, `mypy .`. Line length 120.
11. A commit has a conventional subject, `<type>(<scope>): <summary>`. It has no `Co-Authored-By` line. Stage each file by its name. Never use `git add -A`.
12. Every comment, docstring, document line and commit message of this plan is in ASD-STE100 Simplified Technical English.
13. The branch has a name for the behaviour, and the pull request has the label `no-ticket`. No ticket key is in the branch name, the title or the body.
14. The definition of done of the ticket for a pull request: each new test runs three times in a row with the same result; each new test is shown to fail when the thing that it tests is broken, and the pull request says what was broken; the count of what ran is recorded.
15. Before the merge: the automation suites run on the k3d cluster with the branch build (section 12 of the design, the fourth rule). That run needs Victoria's go and a free cluster.
16. A new test file is not put directly into `tests/`. This plan creates no test file.

## Review Focus

Five conditions that the design implies and that no test of 2a holds. Each one gets its test in Task 1, on the code of `main`, before any code moves. So each one is a record of today's behaviour, as the tests of 2a are.

1. **A per-method `error` that is no object**, for example `"error": "NOT_FOUND"`. Today `plan` raises before it finishes the row. The caller gets `UNKNOWN`, and the row stays `in_flight`. After the move the chain raises in the same place of the flow. Test: `test_a_per_method_error_that_is_no_object_ends_the_call_and_leaves_the_row_in_flight`.
2. **An `error_stub` whose value is a number.** Today the name lookup gives `UNKNOWN`, and the text is the number as text. Test: `test_an_error_stub_that_is_a_number_gives_unknown_with_the_number_as_the_text`.
3. **A `lava-` header that comes two times in the metadata of one call.** Today the adapter keeps the last value. The move changes the code that reads the metadata. Test: `test_a_lava_header_that_comes_two_times_keeps_its_last_value`.
4. **A status that waits for `latency_ms` when a filter names the endpoint.** The move changes the latency only when the filter does not name the endpoint. Test: the cases of `test_on_grpc_latency_ms_obeys_the_filters` in Task 3 that name the endpoint.
5. **A per-method `error_stub` under a filter that does not name the endpoint, over a socket.** The grid of 2a holds it for `plan`. After the move the chain gives the error, so a real client must read it too. Test: `test_an_error_stub_applies_when_a_filter_does_not_name_the_endpoint`.

## File Structure

No file is created. Seven files change in the simulator repository.

| File | Its part in this change |
|---|---|
| `provider_simulator/listeners/base.py` | Four hooks with defaults: `early_identity`, `build_down`, `unpaid_latency`, `corrupt`. One new field of `RawRequest`: `message`. |
| `provider_simulator/listeners/grpc.py` | `GrpcListener` as a subclass of `Listener`. `GrpcStatus` and `GrpcReply`. The servicer classes, the reply builders, `register` and `message_of`. `GrpcPlan`, `plan` and `_targeted` are deleted. |
| `provider_simulator/chains/lava.py` | `_build_grpc` returns a per-method `error_stub` or `error` as data. |
| `server.py` | The gRPC adapter performs a `ServeResult`. The builders and the servicers leave. Two docstrings. |
| `provider_simulator/fault_policy.py`, `provider_simulator/control_api.py`, `CLAUDE.md` | Texts that name `GrpcListener.plan` or `GrpcListener._targeted`. |
| `tests/test_listener_grpc.py`, `tests/test_chains_lava.py`, `tests/test_listener_rest.py`, `tests/test_fault_policy.py`, `tests/test_simulator_grpc.py` | The tests of Tasks 1 to 4. `tests/test_simulator_grpc.py` gets two new tests in Task 1 and no edit of a test that exists. |

---

### Task 0: The worktree and the baseline

**Files:** none.

**Interfaces:**
- Consumes: `main` of `provider-simulator` at `273429e` or later.
- Produces: a worktree on the branch `grpc-uses-the-request-flow`.

A new branch needs Victoria's go.

- [ ] **Step 1: Make the worktree**

Use the worktree tool of the harness, with the name `grpc-uses-the-request-flow`. It makes the branch from `origin/main`. If the tool gives the branch a prefix, rename it: `git branch -m grpc-uses-the-request-flow`.

- [ ] **Step 2: Run the baseline**

Run: `python -m pytest tests -q -p no:cacheprovider`

Expected: `1774 passed`. Another count is a stop: `main` moved, and the counts of this plan must be read again.

---

### Task 1: Four records of today's behaviour that 2a did not have

**Files:**
- Modify: `tests/test_simulator_grpc.py` (three new tests at the end of the class `TestGrpcPerMethodErrors`, and one at the end of the class `TestGrpcMetadataCapture`)

**Interfaces:**
- Consumes: the helpers `_set_grpc`, `_status_of`, `_call_get_latest_block`, `_rows` and the constant `_GRPC_ADDRS` of the file.
- Produces: four tests that pass on `main` and must pass after each later task with no edit.

- [ ] **Step 1: Write the four tests**

Add at the end of the class `TestGrpcPerMethodErrors`, after `test_an_error_override_gives_its_code_and_its_message`:

```python
    def test_an_error_stub_that_is_a_number_gives_unknown_with_the_number_as_the_text(self, sim):
        """An ``error_stub`` is the name of a status. A number is no name, so the
        caller gets UNKNOWN. The per-method ``error`` override is the key that
        reads a number."""
        status, body = _set_grpc(sim, "1", responses={"GetLatestBlock": {"error_stub": 5}})
        assert status == 200, body
        assert _status_of(_call_get_latest_block, _GRPC_ADDRS["1"]) == (grpc.StatusCode.UNKNOWN, "5")
        assert [row["status"] for row in _rows(sim)] == ["error"]

    def test_a_per_method_error_that_is_no_object_ends_the_call_and_leaves_the_row_in_flight(self, sim):
        """A fault of today, recorded as it is. The ``error`` override must be
        an object. With a text in its place the simulator fails before it
        finishes the row: the caller gets UNKNOWN, and the row stays in_flight."""
        status, body = _set_grpc(sim, "1", responses={"GetLatestBlock": {"error": "NOT_FOUND"}})
        assert status == 200, body
        code, text = _status_of(_call_get_latest_block, _GRPC_ADDRS["1"])
        assert code == grpc.StatusCode.UNKNOWN
        assert "AttributeError" in text
        assert [(row["method"], row["status"]) for row in _rows(sim)] == [("*", "in_flight")]

    def test_an_error_stub_applies_when_a_filter_does_not_name_the_endpoint(self, sim):
        """A per-method override is not a fault of the endpoint, so a filter
        does not hold it back. The endpoint is ``http2``, and the filter names
        ``http``."""
        override = {"GetLatestBlock": {"error_stub": "NOT_FOUND"}}
        status, body = _set_grpc(sim, "1", transports=["http"], responses=override)
        assert status == 200, body
        assert _status_of(_call_get_latest_block, _GRPC_ADDRS["1"]) == (grpc.StatusCode.NOT_FOUND, "NOT_FOUND")
```

Add at the end of the class `TestGrpcMetadataCapture`, after `test_history_filter_by_lava_header`:

```python
    def test_a_lava_header_that_comes_two_times_keeps_its_last_value(self, sim):
        _call_get_latest_block(_GRPC_ADDRS["1"], metadata=(("lava-guid", "first"), ("lava-guid", "last")))
        assert [row["lava_headers"] for row in _rows(sim)] == [{"lava-guid": "last"}]
```

- [ ] **Step 2: Run them on the code of `main`**

Run: `python -m pytest tests/test_simulator_grpc.py -q -p no:cacheprovider -k "is_a_number or is_no_object or applies_when_a_filter or comes_two_times"`

Expected: `5 passed`: the four new tests, and `test_a_result_override_applies_when_a_filter_does_not_name_the_endpoint` of 2a, which the same words select. These tests record today's behaviour, so they pass with no code change.

If the control API answers 400 for one of the scenarios, or if one value differs: stop. Correct the expected value to what the run shows only when the difference is in this plan's reading of `main`, and say so in the pull request. Each value is then fixed for the rest of the plan.

- [ ] **Step 3: Show that each one can fail**

Break one source file in one place, run the four tests, and put the file back from a copy. Compare the checksum. Run with `PYTHONDONTWRITEBYTECODE=1`.

| Break | The test that must fail |
|---|---|
| `_STATUS_BY_NAME.get(method_cfg["error_stub"], grpc.StatusCode.UNKNOWN)` becomes `_STATUS_BY_VALUE.get(method_cfg["error_stub"], grpc.StatusCode.UNKNOWN)` | the first one: the caller gets `NOT_FOUND` |
| `err = method_cfg["error"]` becomes `err = method_cfg["error"] if isinstance(method_cfg["error"], dict) else {}` | the second one: the caller gets `UNKNOWN` with "override", and the row says `error` |
| In `plan`, `method_cfg = responses.get(method) or responses.get("default", {})` becomes `method_cfg = (responses.get(method) or responses.get("default", {})) if self._targeted(scenario) else {}` | the third one: the caller gets a reply |
| In `server.py`, `lava = {k: v for (k, v) in metadata if k.lower().startswith("lava-")}` becomes `lava = {k: v for (k, v) in reversed(list(metadata)) if k.lower().startswith("lava-")}` | the fourth one: the row holds "first" |

- [ ] **Step 4: Commit**

```bash
git add tests/test_simulator_grpc.py
git commit -m "test(grpc): record four more answers of today before gRPC moves into Listener.serve"
```

---

### Task 2: The chain returns the per-method error of gRPC as data

**Files:**
- Modify: `provider_simulator/chains/lava.py` (`_build_grpc`, and the comment above it)
- Test: `tests/test_chains_lava.py` (new tests after `test_grpc_all_balances_per_method_result_override`)

**Interfaces:**
- Consumes: `LavaChain.build_success(request, scenario, quirks, interface)` with `interface="grpc"` and `request={"method": <name>}`.
- Produces: for a per-method `error_stub` or `error`, the data `{"grpc_method": <method>, "error": {"code": <a status name or a status number>, "message": <the text>}}`. Task 3 reads the key `error` in `GrpcListener.build_success`.

`GrpcListener.plan` still reads these two keys itself until Task 4, and it returns before it asks the chain. So this task changes no reply.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_chains_lava.py`, after `test_grpc_all_balances_per_method_result_override`:

```python
def _grpc_error(responses, method="GetLatestBlock"):
    _, body = _chain().build_success({"method": method}, _sc(responses=responses), {}, "grpc")
    return body


def test_grpc_error_stub_is_returned_as_data_with_its_name_as_the_text():
    body = _grpc_error({"GetLatestBlock": {"error_stub": "NOT_FOUND"}})
    assert body == {"grpc_method": "GetLatestBlock", "error": {"code": "NOT_FOUND", "message": "NOT_FOUND"}}


def test_grpc_error_stub_takes_its_text_from_the_key_message():
    body = _grpc_error({"GetLatestBlock": {"error_stub": "NOT_FOUND", "message": "the block is gone"}})
    assert body["error"] == {"code": "NOT_FOUND", "message": "the block is gone"}


def test_grpc_error_stub_with_a_name_that_is_no_status_does_not_raise():
    # The REST lookup of an error stub raises for an unknown name. The gRPC
    # lookup has a fallback in the listener, so the chain passes the name on.
    assert _grpc_error({"GetLatestBlock": {"error_stub": "revert"}})["error"] == {"code": "revert", "message": "revert"}


def test_grpc_error_stub_that_is_a_number_has_no_code():
    assert _grpc_error({"GetLatestBlock": {"error_stub": 5}})["error"] == {"code": "", "message": "5"}


def test_grpc_error_override_is_returned_as_data():
    body = _grpc_error({"GetLatestBlock": {"error": {"code": 7, "message": "by number"}}})
    assert body["error"] == {"code": 7, "message": "by number"}


def test_grpc_error_override_that_is_empty_has_the_default_text():
    assert _grpc_error({"GetLatestBlock": {"error": {}}})["error"] == {"code": "", "message": "override"}


def test_grpc_error_in_the_default_entry_reaches_each_method():
    body = _grpc_error({"default": {"error_stub": "ABORTED"}}, method="GetNodeInfo")
    assert body == {"grpc_method": "GetNodeInfo", "error": {"code": "ABORTED", "message": "ABORTED"}}


def test_grpc_error_stub_comes_before_the_error_override_and_the_result_override():
    entry = {"error_stub": "NOT_FOUND", "error": {"code": "ABORTED"}, "result": {"height": 7}}
    assert _grpc_error({"GetLatestBlock": entry})["error"]["code"] == "NOT_FOUND"
    del entry["error_stub"]
    assert _grpc_error({"GetLatestBlock": entry})["error"]["code"] == "ABORTED"
    del entry["error"]
    assert _grpc_error({"GetLatestBlock": entry}) == {"grpc_method": "GetLatestBlock", "result": {"height": 7}}
```

- [ ] **Step 2: Run them to see them fail**

Run: `python -m pytest tests/test_chains_lava.py -q -p no:cacheprovider -k "grpc_error"`

Expected: `8 failed`. Seven fail with a `KeyError: 'error'` or an assertion that shows the default data of the method. The last one fails at its first assertion.

- [ ] **Step 3: Write the code**

In `provider_simulator/chains/lava.py`, replace the last sentence of the comment above `_build_grpc`, "Per-method `responses` result overrides win. gRPC faults (errors, corruption) are the listener's job.", with:

```python
    # A per-method ``responses`` entry wins: an ``error_stub`` or an ``error``
    # is returned as data under the key ``error``, and the listener turns it
    # into a status; a ``result`` replaces the data. A provider-wide fault and
    # a corruption are the listener's job.
```

Replace these two lines of `_build_grpc`:

```python
        if isinstance(method_cfg, dict) and "result" in method_cfg:
            return 200, {"grpc_method": method, "result": method_cfg["result"]}
```

with:

```python
        if isinstance(method_cfg, dict):
            # An ``error_stub`` is the name of a gRPC status. The listener has
            # the fallback for a name that is no status, so this lookup does
            # not use the table of the REST error stubs, which raises for an
            # unknown name.
            if "error_stub" in method_cfg:
                name = method_cfg["error_stub"]
                return 200, {
                    "grpc_method": method,
                    "error": {
                        "code": name if isinstance(name, str) else "",
                        "message": str(method_cfg.get("message", name)),
                    },
                }
            if "error" in method_cfg:
                err = method_cfg["error"]
                return 200, {
                    "grpc_method": method,
                    "error": {"code": err.get("code", ""), "message": err.get("message", "override")},
                }
            if "result" in method_cfg:
                return 200, {"grpc_method": method, "result": method_cfg["result"]}
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_chains_lava.py tests/test_listener_grpc.py tests/test_simulator_grpc.py -q -p no:cacheprovider`

Expected: all pass. `tests/test_chains_lava.py` has 8 more tests than on `main`. The two gRPC files do not change their result, because `plan` returns before it asks the chain.

- [ ] **Step 5: Commit**

```bash
git add provider_simulator/chains/lava.py tests/test_chains_lava.py
git commit -m "refactor(lava): the chain returns the per-method error of gRPC as data"
```

---

### Task 3: The hooks, and gRPC in the request flow

**Files:**
- Modify: `provider_simulator/listeners/base.py` (`RawRequest`, `Listener.serve`, four new methods)
- Modify: `provider_simulator/listeners/grpc.py` (`GrpcStatus`, `GrpcReply`, `_status_of`, `GrpcListener` as a subclass)
- Test: `tests/test_listener_rest.py` (four new tests), `tests/test_listener_grpc.py` (twenty functions rewritten, one test changed on purpose, one new test)

**Interfaces:**
- Consumes: the data of Task 2, `{"grpc_method": ..., "error": {"code": ..., "message": ...}}`.
- Produces:
  - `RawRequest.message: object = None`.
  - `Listener.early_identity(request: RawRequest) -> tuple[str, int | str | None]`, `Listener.build_down() -> ServeResult`, `Listener.unpaid_latency(latency_ms: int) -> int`, `Listener.corrupt(result: ServeResult, status_label: str, scenario: dict) -> str`.
  - `GrpcStatus(code: str, text: str)` and `GrpcReply(method: str, data: dict)`, dataclasses of `provider_simulator/listeners/grpc.py`.
  - `GrpcListener.serve(RawRequest(path=<method>, headers=<metadata>, message=<request message>)) -> ServeResult`. Its `body` is a `GrpcStatus` or a `GrpcReply`. Its `action` is `respond`, `hang` or `drop`.

`GrpcListener.plan` and `GrpcPlan` stay until Task 4, because the adapter still calls `plan`. So after this task the listener tests go through `serve`, and the socket tests still go through `plan`.

- [ ] **Step 1: Write the failing tests of the hooks**

In `tests/test_listener_rest.py`, change the import line to:

```python
from provider_simulator.listeners import RawRequest, RestListener, ServeResult
```

Add at the end of the file:

```python
# ── The hooks of the request flow ────────────────────────────────────────────
# The flow asks four hooks in the places where the interfaces differ. Their
# defaults are what JSON-RPC, REST and Tendermint RPC do, and the tests above
# hold those. These tests show that the flow asks the hooks.


class _ProbeListener(RestListener):
    def early_identity(self, request):
        return "probe-method", "probe-id"

    def build_down(self):
        return ServeResult(action="respond", status=418, body={"probe": "down"})

    def unpaid_latency(self, latency_ms):
        return 7

    def corrupt(self, result, status_label, scenario):
        result.body = {"probe": "corrupted"}
        return "probe-label"


def _probe():
    provider = Pool(name="lava-sim-rest", chain="lava").add_provider("1", [REST])
    return _ProbeListener(provider, REST), provider


def test_the_flow_asks_the_hooks_for_the_row_and_the_reply_of_a_down_provider():
    listener, provider = _probe()
    provider.scenario.update({"mode": "down", "latency_ms": 250})
    res = listener.serve(_get(_BLOCKS_LATEST))
    assert (res.action, res.status, res.body) == ("respond", 418, {"probe": "down"})
    row = provider.log.get_history()[0]
    assert (row["method"], row["status"], row["latency_ms"], row["request_id"]) == ("probe-method", "down", 7, "probe-id")


def test_the_flow_asks_the_hook_for_the_latency_of_a_hang_row_and_of_no_other_row():
    listener, provider = _probe()
    provider.scenario.update({"mode": "hang", "latency_ms": 250})
    assert listener.serve(_get(_BLOCKS_LATEST)).action == "hang"
    provider.scenario.update({"mode": "rate_limit", "latency_ms": 250})
    listener.serve(_get(_BLOCKS_LATEST))
    assert [row["latency_ms"] for row in provider.log.get_history()] == [7, 250]


def test_the_flow_asks_the_hook_to_corrupt_a_reply_and_takes_its_row_label():
    listener, provider = _probe()
    provider.scenario.update({"corruption_mode": "truncated"})
    res = listener.serve(_get(_BLOCKS_LATEST))
    assert res.body == {"probe": "corrupted"}
    assert res.corruption_mode is None
    assert provider.log.get_history()[0]["status"] == "probe-label"


def test_the_flow_does_not_ask_the_hook_to_corrupt_when_the_filter_does_not_name_the_endpoint():
    listener, provider = _probe()
    provider.scenario.update({"corruption_mode": "truncated", "transports": ["ws"]})
    res = listener.serve(_get(_BLOCKS_LATEST))
    assert res.body != {"probe": "corrupted"}
    assert provider.log.get_history()[0]["status"] == "success"
```

- [ ] **Step 2: Run them to see them fail**

Run: `python -m pytest tests/test_listener_rest.py -q -p no:cacheprovider -k "the_flow"`

Expected: `3 failed, 1 passed`. The first three fail because the flow does not ask a hook: the reply is `no_body` with 503, the hang row holds 250, and the body is not the body of the probe. The fourth passes at once. Task 6 shows that it can fail.

- [ ] **Step 3: Add the hooks to `Listener`**

In `provider_simulator/listeners/base.py`:

Replace the docstring and the fields of `RawRequest` with:

```python
    """The transport-agnostic request the socket adapter hands to serve().

    JSON-RPC reads ``body``; REST and Tendermint's GET form read ``verb`` /
    ``path`` / ``query``; gRPC reads ``path``, which holds the method name, and
    ``message``. Every field defaults empty, so a POST-body transport can pass
    just ``body`` (and ``headers``).
    """

    body: bytes = b""
    headers: dict = field(default_factory=dict)
    verb: str = ""
    path: str = ""
    query: dict = field(default_factory=dict)
    # gRPC only: the request message of the call, as the gRPC library parsed it.
    message: object = None
```

In `Listener.serve`, replace the block of the provider-wide `down`:

```python
        # Provider-wide down is pre-parse: no body is read, so method="*" /
        # request_id=None, and no latency is paid (a dead node answers nothing).
        if targeted and mode == "down":
            self.provider.log.finalize(entry, method="*", status="down", latency_ms=latency)
            return ServeResult(action="no_body", status=503)
```

with:

```python
        # Provider-wide down is pre-parse: no body is read, so the row records
        # what ``early_identity`` knows with no parse (by default method="*" and
        # no request id), and the provider does not wait for the latency.
        if targeted and mode == "down":
            method, request_id = self.early_identity(request)
            self.provider.log.finalize(
                entry,
                method=method,
                status="down",
                latency_ms=self.unpaid_latency(latency),
                request_id=request_id,
            )
            return self.build_down()
```

Add one line before the line `override = self.build_body_override(method_cfg) if method_cfg else None`:

```python
        waited = True  # False for a hang: the provider does not wait for the latency
```

Replace:

```python
            if verdict.kind != "none":
                result = self.build_fault(verdict, parsed)
                status_label = _STATUS_LABEL[verdict.kind]
                request_id = self.request_id(parsed)
```

with:

```python
            if verdict.kind != "none":
                result = self.build_fault(verdict, parsed)
                status_label = _STATUS_LABEL[verdict.kind]
                request_id = self.request_id(parsed)
                waited = verdict.kind != "hang"
```

Replace:

```python
        if result.action == "respond" and targeted:
            result.corruption_mode = scenario.get("corruption_mode")
            result.missing_field = scenario.get("missing_field")
            # A pause composes the same way and for the same reason: it changes
```

with:

```python
        if result.action == "respond" and targeted:
            status_label = self.corrupt(result, status_label, scenario)
            # A pause composes the same way and for the same reason: it changes
```

In the last `finalize` call of `serve`, replace `latency_ms=latency,` with:

```python
            latency_ms=latency if waited else self.unpaid_latency(latency),
```

Add these four methods after the abstract method `build_success`:

```python
    def early_identity(self, request: RawRequest) -> "tuple[str, int | str | None]":
        """The method and the request id that a provider-wide ``down`` row
        records. The request is not parsed at that point. Default: ``"*"`` and
        no id, because a dead node does not read the request."""
        return "*", None

    def build_down(self) -> ServeResult:
        """The reply of a provider in the mode ``down``. Default: HTTP 503 with
        no body."""
        return ServeResult(action="no_body", status=503)

    def unpaid_latency(self, latency_ms: int) -> int:
        """The ``latency_ms`` that a row records when the provider did not wait
        for it: a provider-wide ``down`` row and a ``hang`` row. Default: the
        configured value."""
        return latency_ms

    def corrupt(self, result: ServeResult, status_label: str, scenario: dict) -> str:
        """Apply the corruption of the scenario to a reply of this endpoint, and
        return the status label of the history row. Default: mark the reply, and
        the socket adapter corrupts the bytes. The label stays."""
        result.corruption_mode = scenario.get("corruption_mode")
        result.missing_field = scenario.get("missing_field")
        return status_label
```

In the module docstring of `base.py`, add this paragraph before the paragraph that starts "serve() returns a ServeResult":

```python
Four more hooks have a default that is right for the HTTP interfaces, and gRPC
overrides each one: ``early_identity`` (what a provider-wide down row records
with no parse), ``build_down`` (the reply of a down provider),
``unpaid_latency`` (the latency of a down row and of a hang row) and
``corrupt`` (how the interface corrupts a reply, and the label of its row).
```

- [ ] **Step 4: Run the hook tests and the HTTP listeners**

Run: `python -m pytest tests/test_listener_rest.py tests/test_listener_jsonrpc.py tests/test_listener_tendermint.py tests/test_reply_pause.py -q -p no:cacheprovider`

Expected: all pass, with 4 more tests than on `main`.

- [ ] **Step 5: Rewrite the listener tests of gRPC to call `serve`**

In `tests/test_listener_grpc.py`:

Replace the module docstring with:

```python
"""GrpcListener — gRPC in the request flow of Listener.serve.
No running gRPC server is needed: serve() returns a ServeResult, and its body is
a GrpcStatus or a GrpcReply."""
```

Replace the import of the listener module with:

```python
from provider_simulator.listeners import Listener, RawRequest
from provider_simulator.listeners.grpc import GrpcListener, GrpcReply, GrpcStatus, check_servicers, request_id_fields
```

Add after the function `_upd`:

```python
def _serve(listener, method="GetLatestBlock", request=None, lava_headers=None):
    """One call through the request flow, as the gRPC adapter makes it."""
    return listener.serve(RawRequest(path=method, headers=lava_headers or {}, message=request))
```

Replace the thirteen functions from `test_success_plan_carries_latest_block_data` to `test_missing_field_corruption_stays_respond` with:

```python
def test_success_carries_the_data_of_the_latest_block():
    listener, provider = _listener()
    result = _serve(listener, "GetLatestBlock")
    assert result.action == "respond"
    assert isinstance(result.body, GrpcReply)
    assert result.body.method == "GetLatestBlock"
    assert result.body.data["height"] == 25_000_000
    assert result.body.data["chain_id"] == "lava-sim"
    hist = provider.log.get_history()[0]
    assert hist["status"] == "success"
    assert hist["method"] == "GetLatestBlock"


def test_node_info_success():
    listener, _ = _listener()
    result = _serve(listener, "GetNodeInfo")
    assert result.action == "respond"
    assert result.body.data["network"] == "lava-sim"


def test_blocks_behind_shifts_head():
    listener, _ = _listener()
    _upd(listener, {"blocks_behind": 7})
    assert _serve(listener, "GetLatestBlock").body.data["height"] == 25_000_000 - 7


def test_down_is_unavailable_and_records_method_not_star():
    listener, provider = _listener()
    _upd(listener, {"mode": "down"})
    result = _serve(listener, "GetLatestBlock")
    assert result.body == GrpcStatus("UNAVAILABLE", "provider down")
    hist = provider.log.get_history()[0]
    assert hist["status"] == "down"
    assert hist["method"] == "GetLatestBlock"  # gRPC always knows the method


def test_hang_is_cancelled_with_the_action_hang():
    listener, _ = _listener()
    _upd(listener, {"mode": "hang"})
    result = _serve(listener, "GetNodeInfo")
    assert result.body.code == "CANCELLED"
    assert result.action == "hang"


def test_rate_limit_resource_exhausted():
    listener, _ = _listener()
    _upd(listener, {"mode": "rate_limit"})
    assert _serve(listener, "GetLatestBlock").body.code == "RESOURCE_EXHAUSTED"


def test_error_maps_status_name_from_message():
    listener, _ = _listener()
    _upd(listener, {"mode": "error", "error_message": "NOT_FOUND"})
    assert _serve(listener, "GetLatestBlock").body.code == "NOT_FOUND"


def test_error_defaults_to_unknown():
    listener, _ = _listener()
    _upd(listener, {"mode": "error", "error_message": "not a status", "error_code": -1})
    assert _serve(listener, "GetLatestBlock").body.code == "UNKNOWN"


def test_per_method_error_stub_is_a_status():
    listener, _ = _listener()
    _upd(listener, {"responses": {"GetLatestBlock": {"error_stub": "NOT_FOUND"}}})
    result = _serve(listener, "GetLatestBlock")
    assert isinstance(result.body, GrpcStatus)
    assert result.body.code == "NOT_FOUND"


def test_wrong_type_corruption_is_internal():
    listener, _ = _listener()
    _upd(listener, {"corruption_mode": "wrong_type"})
    assert _serve(listener, "GetLatestBlock").body.code == "INTERNAL"


def test_invalid_proto_corruption_is_unknown():
    listener, _ = _listener()
    _upd(listener, {"corruption_mode": "invalid_proto"})
    assert _serve(listener, "GetLatestBlock").body.code == "UNKNOWN"


def test_null_body_corruption_is_unknown():
    # A whole-body JSON null has no gRPC shape; falling through to a clean
    # success would be a fault that arms with a 200 and does nothing.
    listener, _ = _listener()
    _upd(listener, {"corruption_mode": "null_body"})
    assert _serve(listener, "GetLatestBlock").body.code == "UNKNOWN"


def test_missing_field_corruption_stays_a_reply():
    listener, _ = _listener()
    _upd(listener, {"corruption_mode": "missing_field", "missing_field": "block"})
    result = _serve(listener, "GetLatestBlock")
    assert isinstance(result.body, GrpcReply)
    assert result.corruption_mode == "missing_field"
    assert result.missing_field == "block"
```

Each of the thirteen keeps its assertions. Seven have a new name, because the old name holds the word `plan` or `abort`.

Replace the six functions of the block "The request id", from `test_all_balances_row_holds_the_address_as_the_request_id` to `test_a_provider_wide_down_row_has_no_request_id`, with:

```python
def test_all_balances_row_holds_the_address_as_the_request_id():
    listener, provider = _listener()
    result = _serve(listener, "AllBalances", _all_balances("lava1-probe-a"))
    assert result.action == "respond"
    assert result.body.method == "AllBalances"
    assert result.body.data["balances"] == [{"denom": "ulava", "amount": "1000000"}]
    hist = provider.log.get_history()[0]
    assert hist["method"] == "AllBalances"
    assert hist["status"] == "success"
    assert hist["request_id"] == "lava1-probe-a"


def test_get_latest_block_row_holds_no_request_id():
    listener, provider = _listener()
    _serve(listener, "GetLatestBlock", query_pb2.GetLatestBlockRequest())
    assert provider.log.get_history()[0]["request_id"] is None


def test_an_empty_address_is_no_request_id():
    listener, provider = _listener()
    _serve(listener, "AllBalances", _all_balances(""))
    assert provider.log.get_history()[0]["request_id"] is None


def test_a_call_with_no_request_message_has_no_request_id():
    # serve() keeps working for a caller that passes the method name only.
    listener, provider = _listener()
    _serve(listener, "AllBalances")
    assert provider.log.get_history()[0]["request_id"] is None


@pytest.mark.parametrize(
    "scenario, status",
    [
        ({"mode": "error", "error_message": "NOT_FOUND"}, "error"),
        ({"mode": "rate_limit"}, "rate_limit"),
        ({"mode": "hang"}, "hang"),
        ({"mode": "drop_connection"}, "drop_connection"),
        ({"responses": {"AllBalances": {"error_stub": "NOT_FOUND"}}}, "error"),
        ({"responses": {"AllBalances": {"error": {"code": "NOT_FOUND", "message": "gone"}}}}, "error"),
        ({"corruption_mode": "wrong_type"}, "error"),
        ({"corruption_mode": "invalid_proto"}, "error"),
    ],
)
def test_a_fault_row_of_all_balances_keeps_the_request_id(scenario, status):
    listener, provider = _listener()
    _upd(listener, scenario)
    result = _serve(listener, "AllBalances", _all_balances("lava1-probe-f"))
    assert isinstance(result.body, GrpcStatus)
    hist = provider.log.get_history()[0]
    assert (hist["status"], hist["request_id"]) == (status, "lava1-probe-f")


def test_a_provider_wide_down_row_has_no_request_id():
    # A dead node does not read the request. The row keeps the method, as every
    # gRPC row does, and it has no request id, as on JSON-RPC, REST and
    # Tendermint RPC.
    listener, provider = _listener()
    _upd(listener, {"mode": "down"})
    _serve(listener, "AllBalances", _all_balances("never-read"))
    hist = provider.log.get_history()[0]
    assert (hist["status"], hist["method"], hist["request_id"]) == ("down", "AllBalances", None)
```

In the comment block "The grid", replace the three lines that start with "``_decide`` is the one function" and the paragraph after them with:

```python
# ``_decide`` is the one function of this block that calls serve() and reads a
# ServeResult. Before gRPC moved into Listener.serve, it called plan() and read
# a GrpcPlan, and the move changed no expected value of the two tables.
#
# The content of a reply message is not in these tables. A caller reads the
# message that the adapter builds from the data of the chain.
# tests/test_simulator_grpc.py holds that content, read by a real client:
# TestGrpcHappy, TestGrpcAllBalances and TestGrpcReplyFields.
```

Replace the function `_decide` with:

```python
def _decide(listener, method="GetLatestBlock", request=None, lava_headers=None):
    result = _serve(listener, method, request, lava_headers)
    if isinstance(result.body, GrpcStatus):
        drop_at = result.drop_at if result.action == "drop" else None
        return _Decision(result.body.code, result.body.text, result.latency_ms, result.action == "hang", drop_at, None)
    assert isinstance(result.body, GrpcReply), result.body
    clears = result.missing_field if result.corruption_mode == "missing_field" else None
    return _Decision("OK", "", result.latency_ms, False, None, clears)
```

In the comment of the grid, replace "# A gRPC call is answered through GrpcListener.plan. The tables below record\n# what plan() decides" with "# A gRPC call is answered through Listener.serve. The tables below record\n# what serve() decides". Rename `test_what_plan_decides_for_one_call` to `test_what_serve_decides_for_one_call` and `test_the_row_is_complete_when_plan_returns` to `test_the_row_is_complete_when_serve_returns`; in the comment of the second one, replace "after plan() returns" with "after serve() returns". No parameter and no expected value of the two tables changes.

Replace the function `test_latency_ms_is_paid_today_under_a_filter_that_does_not_name_the_endpoint` with:

```python
@pytest.mark.parametrize(
    "filters, want_ms",
    [
        pytest.param({}, 250, id="no-filter"),
        pytest.param({"ports": [18548]}, 250, id="a-ports-filter-that-names-the-endpoint"),
        pytest.param({"transports": ["http2"]}, 250, id="a-transports-filter-that-names-the-endpoint"),
        pytest.param({"ports": [_ANOTHER_PORT]}, 0, id="a-ports-filter-that-does-not-name-it"),
        pytest.param({"transports": ["ws"]}, 0, id="a-transports-filter-that-does-not-name-it"),
    ],
)
def test_on_grpc_latency_ms_obeys_the_filters(filters, want_ms):
    # A filter that does not name the endpoint holds the latency back, as on
    # JSON-RPC, REST and Tendermint RPC. Before gRPC moved into Listener.serve
    # it read ``latency_ms`` with no look at the filters, and the two cases
    # with 0 held 250. The design "One request flow for every interface"
    # changed that on purpose (its section 9.2, row 2).
    listener, provider = _listener()
    _upd(listener, {"latency_ms": 250, **filters})
    assert _decide(listener).wait_ms == want_ms
    assert _row(provider) == ("GetLatestBlock", "success", want_ms, None)
```

Add after it:

```python
def test_the_grpc_listener_has_no_request_flow_of_its_own():
    assert GrpcListener.serve is Listener.serve
```

- [ ] **Step 6: Run the gRPC listener tests to see them fail**

Run: `python -m pytest tests/test_listener_grpc.py -q -p no:cacheprovider`

Expected: an error at collection: `ImportError: cannot import name 'GrpcReply'`. That is the right failure: the new names do not exist yet.

- [ ] **Step 7: Make `GrpcListener` a subclass of `Listener`**

In `provider_simulator/listeners/grpc.py`:

Replace the import block of the package with:

```python
from provider_simulator import fault_policy
from provider_simulator.chains import chain_for
from provider_simulator.listeners.base import Listener, RawRequest, ServeResult
```

`Endpoint` and `Provider` are no longer imported: the class takes its constructor from `Listener`.

Add after the class `GrpcPlan`:

```python
@dataclass
class GrpcStatus:
    """A reply that is a status: the name of a ``grpc.StatusCode`` and its text."""

    code: str
    text: str


@dataclass
class GrpcReply:
    """A reply that is a message: the served method and the data of the chain.
    The protobuf message is built from it after the history row is finished."""

    method: str
    data: dict
```

Add after the function `_status_name`:

```python
def _status_of(code: object) -> str:
    """The status that a per-method error names: the name of a status, or the
    number of a status. Each other value gives UNKNOWN."""
    by_name = _STATUS_BY_NAME.get(code if isinstance(code, str) else "")
    by_number = _STATUS_BY_VALUE.get(code if isinstance(code, int) else -1)
    return (by_name or by_number or grpc.StatusCode.UNKNOWN).name
```

Replace the line `class GrpcListener:` and its `__init__` method with:

```python
class GrpcListener(Listener):
    """gRPC in the request flow of ``Listener.serve``. This class has no order
    of steps of its own. It fills the hooks, and each hook holds one place where
    gRPC differs from the HTTP interfaces."""

    def parse_request(self, request: RawRequest) -> dict:
        # The gRPC library parsed the call before the adapter saw it. The
        # adapter passes the method name in ``path`` and the request message in
        # ``message``.
        return {"method": request.path, "message": request.message}

    def early_identity(self, request: RawRequest) -> "tuple[str, int | str | None]":
        # The method of a gRPC call is known with no parse. A dead node does
        # not read the request, so the row has no request id.
        return request.path, None

    def build_down(self) -> ServeResult:
        return ServeResult(action="respond", body=GrpcStatus("UNAVAILABLE", "provider down"))

    def unpaid_latency(self, latency_ms: int) -> int:
        # A down row and a hang row record 0: the provider did not wait.
        return 0

    def method_key(self, request: dict) -> object:
        # gRPC does not merge the fault keys of a per-method override. The
        # chain reads the content keys of the override.
        return None

    def build_fault(self, verdict: fault_policy.Verdict, request: dict) -> ServeResult:
        if verdict.kind == "hang":
            return ServeResult(action="hang", body=GrpcStatus("CANCELLED", "hang timeout"))
        if verdict.kind == "drop":
            status = GrpcStatus("UNAVAILABLE", "connection dropped")
            return ServeResult(action="drop", drop_at=verdict.drop_at, body=status)
        if verdict.kind == "rate_limit":
            # The verdict also carries an HTTP status and a body text. gRPC has
            # no place for them.
            return ServeResult(action="respond", body=GrpcStatus("RESOURCE_EXHAUSTED", "Too many requests"))
        status = GrpcStatus(_status_name(verdict.error_message, verdict.error_code), verdict.error_message)
        return ServeResult(action="respond", body=status)

    def build_success(self, status: int, body: object) -> ServeResult:
        data = body if isinstance(body, dict) else {}
        error = data.get("error")
        if error is not None:
            # A per-method error of the chain is a status.
            return ServeResult(action="respond", body=GrpcStatus(_status_of(error.get("code")), error.get("message")))
        return ServeResult(action="respond", body=GrpcReply(str(data.get("grpc_method", "")), data))

    def corrupt(self, result: ServeResult, status_label: str, scenario: dict) -> str:
        if not isinstance(result.body, GrpcReply):
            # A corruption acts on a reply message only. A status stays as it is.
            return status_label
        corruption = scenario.get("corruption_mode")
        # wrong_type is a status by design: proto3 fields are typed, so a value
        # of a wrong type cannot be put into the reply message the way a JSON
        # body can carry one. INTERNAL is the closest thing that a caller can
        # read for "the provider answered with garbage types".
        if corruption == "wrong_type":
            field_name = scenario.get("missing_field") or "response"
            result.body = GrpcStatus("INTERNAL", f"wrong_type corruption on {field_name}")
            return "error"
        if corruption in ("invalid_proto", "empty_response", "truncated", "null_body"):
            result.body = GrpcStatus("UNKNOWN", f"corruption: {corruption}")
            return "error"
        if corruption == "missing_field":
            result.corruption_mode = "missing_field"
            result.missing_field = scenario.get("missing_field")
        # invalid_json does nothing on gRPC.
        return status_label

    def request_id(self, request: dict):
        return _request_id(request.get("method", ""), request.get("message"))

    def response_id(self, body: object):
        return None  # the data of the chain carries no request id
```

The methods `plan` and `_targeted` stay below these, with no change, until Task 4. `plan` uses `self.provider` and `self.endpoint`, which `Listener.__init__` sets.

- [ ] **Step 8: Run the tests**

Run: `python -m pytest tests/test_listener_grpc.py tests/test_listener_rest.py tests/test_fault_policy.py -q -p no:cacheprovider`

Expected: all pass. `tests/test_listener_grpc.py` has 119 tests: the 114 of `main`, less the one latency test, plus its five cases and the one new test.

A case of `test_what_serve_decides_for_one_call` or of `test_which_calls_wait_for_latency_ms` that fails is a stop. Do not edit its expected value. Read the hook that gives the other value.

Run: `python -m pytest tests -q -p no:cacheprovider`

Expected: no failure. The socket tests still go through `plan`.

- [ ] **Step 9: Commit**

```bash
git add provider_simulator/listeners/base.py provider_simulator/listeners/grpc.py tests/test_listener_grpc.py tests/test_listener_rest.py
git commit -m "refactor(grpc): GrpcListener fills the hooks of Listener.serve, and the listener tests call serve"
```

---

### Task 4: The adapter performs a ServeResult, and `plan` is deleted

**Files:**
- Modify: `provider_simulator/listeners/grpc.py` (the stub imports, the builders, `servicers`, `register`, `message_of`; delete `GrpcPlan`, `plan`, `_targeted`)
- Modify: `server.py` (`_run_grpc_in_thread`)
- Modify: `tests/test_fault_policy.py` (delete one test)

**Interfaces:**
- Consumes: `GrpcListener.serve`, `GrpcStatus` and `GrpcReply` of Task 3.
- Produces, in `provider_simulator/listeners/grpc.py`:
  - `servicers(perform) -> tuple[tuple[str, type, Callable], ...]`: each row is the full name of a service, its servicer class, and the generated function that registers it. `perform` is a coroutine function: `await perform(method: str, request, context)`.
  - `register(server, table) -> None`: registers the servicers of `table` and the reflection service with one `grpc.aio` server.
  - `message_of(result: ServeResult)`: builds the protobuf reply message of a `ServeResult` whose body is a `GrpcReply`, and clears the field that a `missing_field` corruption names.

The socket tests are the tests of this task. They exist, and they must pass with no edit. So this task has no "write the failing test" step: its red state is the run of Step 2.

- [ ] **Step 1: Run the socket tests before the change**

Run: `python -m pytest tests/test_simulator_grpc.py tests/test_simulator_grpc_port_closed.py tests/integration -q -p no:cacheprovider`

Expected: all pass. Write down the count.

- [ ] **Step 2: Delete `plan`, and see what fails**

In `provider_simulator/listeners/grpc.py`, delete the class `GrpcPlan`, the method `GrpcListener.plan` and the method `GrpcListener._targeted`. Delete the imports that only they used: `field` of `dataclasses`, and `chain_for`.

Run: `python -m pytest tests/test_simulator_grpc.py -q -p no:cacheprovider -x`

Expected: a failure in the first test that makes a gRPC call. The adapter still calls `grpc_listener.plan`, so the caller gets `UNKNOWN` with `AttributeError`. That is the red state of this task.

- [ ] **Step 3: Delete the test of `_targeted`**

In `tests/test_fault_policy.py`, delete the function `test_the_grpc_listener_asks_the_same_predicate_rather_than_its_own`. It tests the method `GrpcListener._targeted`, which Step 2 deleted. The flow of `Listener.serve` asks the filter one time for the mode, the latency and the corruption, and `test_the_grpc_listener_has_no_request_flow_of_its_own` of Task 3 holds that gRPC has no other flow.

- [ ] **Step 4: Move the builders and the servicers into the listener module**

In `provider_simulator/listeners/grpc.py`, replace the imports at the top of the module with:

```python
import datetime
from dataclasses import dataclass

import grpc
from google.protobuf import descriptor_pool

# Importing the package splices cosmos_pb2/ onto sys.path, so that the absolute
# imports of the generated stubs resolve. It must run before the ``from
# cosmos...`` imports below, so isort must not reorder them.
import cosmos_pb2  # noqa: F401  isort: split

from cosmos.bank.v1beta1 import query_pb2 as bank_query_pb2  # isort: skip
from cosmos.bank.v1beta1 import query_pb2_grpc as bank_query_pb2_grpc  # isort: skip
from cosmos.base.tendermint.v1beta1 import query_pb2, query_pb2_grpc  # isort: skip
from tendermint.types import block_pb2, types_pb2  # isort: skip

from provider_simulator import fault_policy
from provider_simulator.chains.lava import GRPC_LATEST_BLOCK, LAVA_SIM_CHAIN_ID
from provider_simulator.listeners.base import Listener, RawRequest, ServeResult
```

Add at the end of the module:

```python
# ── The reply messages ────────────────────────────────────────────────────────
# Each builder makes the protobuf reply of one served method from the data of
# the chain. The adapter calls ``message_of`` after Listener.serve finished the
# history row of the call.


def _merged(data: dict) -> dict:
    # A per-method `responses` result override arrives as {"result": {...}};
    # its keys shadow the defaults of the builder.
    merged = dict(data)
    result = merged.pop("result", None)
    if isinstance(result, dict):
        merged.update(result)
    return merged


def build_latest_block(data: dict):
    merged = _merged(data)
    now = datetime.datetime.now(datetime.timezone.utc)
    header = types_pb2.Header(
        chain_id=merged.get("chain_id", LAVA_SIM_CHAIN_ID),
        height=merged.get("height", GRPC_LATEST_BLOCK),
    )
    header.time.seconds = int(now.timestamp())
    header.time.nanos = now.microsecond * 1000
    block = block_pb2.Block(header=header)
    block_id = types_pb2.BlockID(hash=b"\xab" * 32)
    return query_pb2.GetLatestBlockResponse(block_id=block_id, block=block)


def build_node_info(data: dict):
    merged = _merged(data)
    resp = query_pb2.GetNodeInfoResponse()
    resp.default_node_info.network = merged.get("network", LAVA_SIM_CHAIN_ID)
    resp.default_node_info.moniker = merged.get("moniker", "lava-sim-grpc-provider")
    resp.default_node_info.version = merged.get("version", "sim-1.0")
    resp.application_version.name = "lava-sim"
    resp.application_version.app_name = merged.get("app_name", "lava-sim-app")
    resp.application_version.version = merged.get("app_version", "sim-1.0")
    return resp


def build_all_balances(data: dict):
    # A `result` override is free JSON, and the history row of the call is
    # written before this reply is built. So this builder raises for no
    # shape: each item that is an object is a coin, with its denom and its
    # amount as text, and each other item is skipped. An error here would
    # leave a row that says success for a call that got no reply.
    def _text(value) -> str:
        return "" if value is None else str(value)

    merged = _merged(data)
    resp = bank_query_pb2.QueryAllBalancesResponse()
    balances = merged.get("balances", [])
    for coin in balances if isinstance(balances, list) else []:
        if isinstance(coin, dict):
            resp.balances.add(denom=_text(coin.get("denom")), amount=_text(coin.get("amount")))
    return resp


# The builder of the reply of each served method, by the bare method name.
_REPLY_BUILDERS = {
    "GetLatestBlock": build_latest_block,
    "GetNodeInfo": build_node_info,
    "AllBalances": build_all_balances,
}


def message_of(result: ServeResult):
    """Build the reply message of one call. ``result.body`` is a GrpcReply.

    A ``missing_field`` corruption clears one field of the message. A name that
    the message does not have clears nothing."""
    reply = result.body
    assert isinstance(reply, GrpcReply), reply
    response = _REPLY_BUILDERS[reply.method](reply.data)
    if result.corruption_mode == "missing_field" and result.missing_field:
        # proto3 fields are clearable; the receiver sees the field unset.
        if response.DESCRIPTOR.fields_by_name.get(result.missing_field):
            response.ClearField(result.missing_field)
    return response


# ── The servicers ─────────────────────────────────────────────────────────────


def servicers(perform) -> tuple:
    """The servicer classes of this simulator.

    ``perform`` is the coroutine function of the gRPC adapter:
    ``await perform(method, request, context)`` answers one call. Each row of
    the result holds the full name of a service, its servicer class, and the
    generated function that registers it. A class serves the methods that it
    defines itself. Each other method keeps the generated default, which
    answers UNIMPLEMENTED.

    The adapter gives this list to ``check_servicers`` before it starts a
    server, and to ``register`` for each server that it starts. So a servicer
    is not registered with no check."""

    class _Servicer(query_pb2_grpc.ServiceServicer):
        async def GetLatestBlock(self, request, context):
            return await perform("GetLatestBlock", request, context)

        async def GetNodeInfo(self, request, context):
            return await perform("GetNodeInfo", request, context)

    class _BankServicer(bank_query_pb2_grpc.QueryServicer):
        """One method of the bank service: AllBalances, whose ``address`` is
        the request id of the call."""

        async def AllBalances(self, request, context):
            return await perform("AllBalances", request, context)

    return (
        (
            query_pb2.DESCRIPTOR.services_by_name["Service"].full_name,
            _Servicer,
            query_pb2_grpc.add_ServiceServicer_to_server,
        ),
        (
            bank_query_pb2.DESCRIPTOR.services_by_name["Query"].full_name,
            _BankServicer,
            bank_query_pb2_grpc.add_QueryServicer_to_server,
        ),
    )


def register(server, table) -> None:
    """Register the servicers of ``table`` and the reflection service with one
    ``grpc.aio`` server."""
    from grpc_reflection.v1alpha import reflection

    for _, servicer, add_to_server in table:
        add_to_server(servicer(), server)
    # Server reflection lets grpcurl discover services without a proto bundle —
    # a dev/test convenience worth the negligible surface. The smart-router
    # does not read this list: it asks reflection for the symbol of a service,
    # which is found because the stubs are imported at the top of this module.
    # The list is what ``grpcurl list`` prints.
    service_names = (*(service for service, _, _ in table), reflection.SERVICE_NAME)
    reflection.enable_server_reflection(service_names, server)
```

Replace the module docstring of `provider_simulator/listeners/grpc.py` with:

```python
"""gRPC listener — Cosmos gRPC over http2.

A gRPC call goes through the request flow of ``Listener.serve``, as a JSON-RPC,
a REST and a Tendermint RPC request do. ``GrpcListener`` fills the hooks. The
gRPC adapter in server.py performs the ServeResult that the flow returns: it
waits, and then it ends the call with a status or returns the reply message.

A reply is one of two things. A ``GrpcStatus`` is a status code with a text. A
``GrpcReply`` holds the data of the chain, and ``message_of`` builds the
protobuf message from it after the history row is finished.

This module owns every protobuf class of the simulator's gRPC side: the
servicer classes, their registration with the server and with reflection, the
table of the served methods, and the builders of the reply messages.

The rules that are special to gRPC, and the hook that holds each one:
- The method of a call is known with no parse, so a provider-wide ``down`` row
  records it, and not ``"*"`` (``early_identity``).
- A served method can name one field of its request message as the request id
  (``SERVED_METHODS``). For AllBalances that field is ``address``. A method that
  names no field records no request id, and so does a provider-wide ``down``
  row: a dead node does not read the request.
- A fault is a status: down -> UNAVAILABLE (``build_down``); hang -> CANCELLED
  after 30 seconds, drop -> UNAVAILABLE, rate_limit -> RESOURCE_EXHAUSTED,
  error -> the status that error_message names, or error_code as a number,
  else UNKNOWN (``build_fault``).
- A down row and a hang row record latency 0, because the provider did not wait
  (``unpaid_latency``).
- A per-method error_stub or error override is a status, not a body. The chain
  returns it as data (``build_success``).
- gRPC does not merge the fault keys of a per-method override (``method_key``).
- A corruption acts on a reply message only: missing_field clears a field;
  wrong_type gives INTERNAL; invalid_proto, empty_response, truncated and
  null_body give UNKNOWN; invalid_json does nothing (``corrupt``).

One mode is not served here at all. ``port_closed`` is not a reply to a call:
the endpoint's server is stopped, so no call arrives. ``down`` and ``drop``
above are replies: both answer UNAVAILABLE with this module's own text, over a
connection that stays open. The serve loop in server.py performs
``port_closed`` (see ``provider_simulator/port_gate.py``); a call that arrives
while the mode is set came in before the port closed and is served like a call
on an open port.
"""
```

- [ ] **Step 5: Make the adapter perform a ServeResult**

In `server.py`, replace the body of `_run_grpc_in_thread` from its docstring down to the line `check_servicers({service: servicer for service, servicer, _ in servicers})` with:

```python
    """Run one gRPC endpoint: an asyncio event loop on this (daemon) thread
    hosting the servicers of the gRPC listener module. Each call goes through
    ``Listener.serve``, and ``_perform`` below performs its ServeResult.

    ``gate`` is how the port is closed and opened again by a scenario
    (``mode="port_closed"``): the loop below stops and starts the server on this
    thread's own event loop and reports each change to it.

    All gRPC imports are local so a missing grpcio never breaks the HTTP-only
    simulator (the caller downgraded gRPC to a warning at bootstrap). This
    function names no protobuf class: the listener module owns them.
    """
    import asyncio

    import grpc

    from provider_simulator.listeners import grpc as grpc_wire

    async def _perform(method: str, request, context):
        metadata = context.invocation_metadata() or []
        # The flow keeps the lava-* headers for the history row. The listener
        # gets the request message: it knows which field of which method
        # carries the request id.
        headers = {k: v for (k, v) in metadata}
        result = grpc_listener.serve(RawRequest(path=method, headers=headers, message=request))

        status = result.body
        if isinstance(status, grpc_wire.GrpcStatus):
            if result.action == "hang":
                # Long enough for the client deadline to fire, finite so
                # the asyncio task doesn't leak.
                await asyncio.sleep(30)
            elif result.latency_ms > 0:
                await asyncio.sleep(result.latency_ms / 1000.0)
            if result.action == "drop" and result.drop_at in ("after_headers", "mid_body"):
                # Half-open response: metadata out, then the status. Unary
                # RPCs can't stream mid-body, so both variants collapse here.
                try:
                    await context.send_initial_metadata([])
                except Exception:
                    pass
            await context.abort(grpc.StatusCode[status.code], status.text)
            return None  # unreachable — abort raises

        # The history row of the call is finished. A reply that cannot be built
        # ends the call here, with the row as it is.
        response = grpc_wire.message_of(result)
        if result.latency_ms > 0:
            await asyncio.sleep(result.latency_ms / 1000.0)
        return response

    # One list gives the check, the registration and the reflection list their
    # servicers, so a servicer is not registered with no check.
    servicers = grpc_wire.servicers(_perform)

    # The servicers above and the table of the listener must name the same
    # methods. A served method with no row would record no request id, and the
    # refusal of two methods of one name reads the table. So this endpoint does
    # not start when the two differ.
    grpc_wire.check_servicers({service: servicer for service, servicer, _ in servicers})
```

In the function `_new_server` of the same function, replace:

```python
        server = grpc.aio.server()
        for _, servicer, add_to_server in servicers:
            add_to_server(servicer(), server)
        # Server reflection lets grpcurl discover services without a proto
        # bundle — a dev/test convenience worth the negligible surface. The
        # smart-router does not read this list: it asks reflection for the
        # symbol of a service, which is found because the stubs are imported
        # above. The list is what ``grpcurl list`` prints.
        service_names = (*(service for service, _, _ in servicers), reflection.SERVICE_NAME)
        reflection.enable_server_reflection(service_names, server)
        server.add_insecure_port(bind)
        return server
```

with:

```python
        server = grpc.aio.server()
        grpc_wire.register(server, servicers)
        server.add_insecure_port(bind)
        return server
```

The rest of the function, the loop `_serve` and the call `asyncio.run(_serve())`, does not change.

The comprehension keeps the last value of a key that comes two times, as the old adapter did. Do not write `dict(metadata)`: the metadata object of `grpc.aio` is a mapping too, and `dict()` of a mapping can take another value for such a key. `Listener.serve` keeps the keys that start with `lava-`, in any letter case, as the old adapter did. `test_a_lava_header_that_comes_two_times_keeps_its_last_value` of Task 1 holds this.

- [ ] **Step 6: Run the socket tests**

Run: `python -m pytest tests/test_simulator_grpc.py tests/test_simulator_grpc_port_closed.py tests/integration -q -p no:cacheprovider`

Expected: all pass, with the count of Step 1, and with no edit of a test.

A test that fails is a stop. Three are the most likely, and each one names its cause:

| Test | What a failure means |
|---|---|
| `test_a_result_override_of_a_wrong_type_ends_the_call_after_the_row_says_success` | The reply was built before the row was finished. Read choice 1. |
| `test_the_adapter_refuses_to_start_when_a_served_method_has_no_row`, `test_an_endpoint_that_is_refused_leaves_the_simulator_up_and_not_ready` | `check_servicers` does not run before the serve loop, or it does not read `SERVED_METHODS` at the time of the call. |
| `test_two_drop_points_send_the_initial_metadata_before_the_status` | The adapter reads `drop_at` of a result that is no drop, or it does not send the metadata. |

- [ ] **Step 7: Run the whole suite and the three checks**

Run: `python -m pytest tests -q -p no:cacheprovider`

Expected: `1794 passed`. That is the 1774 tests of `main`, plus 4 of Task 1, 8 of Task 2, 4 hook tests of Task 3 and 5 more in `tests/test_listener_grpc.py` (119 against 114), less the 1 test of `tests/test_fault_policy.py` that Step 3 deleted. No test fails and no test is skipped.

```bash
black --check .
ruff check .
mypy .
```

Expected: no change, no finding and no issue.

- [ ] **Step 8: Commit**

```bash
git add provider_simulator/listeners/grpc.py server.py tests/test_fault_policy.py
git commit -m "refactor(grpc): the adapter performs a ServeResult, and GrpcListener.plan and GrpcPlan are deleted"
```

---

### Task 5: The texts that name the deleted code

**Files:**
- Modify: `server.py` (the module docstring)
- Modify: `provider_simulator/control_api.py` (one comment)
- Modify: `provider_simulator/fault_policy.py` (the docstring of `targets`)
- Modify: `CLAUDE.md` (the paragraph "gRPC ports and `mode="port_closed"`")

**Interfaces:**
- Consumes: the names of Tasks 3 and 4.
- Produces: no code.

- [ ] **Step 1: Find each place**

Run: `git grep -n -E "GrpcListener\.plan|GrpcPlan|plan\(\)|_targeted" -- "*.py" "*.md" ":!docs/superpowers" ":!docs/adr" ":!tests/test_cache*" ":!tests/test_control_api_cache.py"`

Expected: the four places below, and no other. `CacheSim.plan` of the cache simulator is another method and stays. A place that this list does not name is a stop.

- [ ] **Step 2: Change the four texts**

`server.py`, the module docstring, the item about gRPC. Replace:

```
- gRPC endpoints run an async servicer that performs ``GrpcListener.plan()``:
  abort with a status code, or build the protobuf from the plan's data. The same
```

with:

```
- gRPC endpoints run the servicers of the gRPC listener module. Each call goes
  through ``Listener.serve()``, and the adapter ends the call with a status or
  returns the reply message that the listener module builds. The same
```

`provider_simulator/control_api.py`, the comment above `_NEEDS_A_REQUEST`. Replace `Listener.serve, GrpcListener.plan), and a closed port serves none.` with `Listener.serve), and a closed port serves none.`

`provider_simulator/fault_policy.py`, the docstring of `targets`. Replace the paragraph that starts "Exported rather than inlined into ``resolve_mode``" with:

```python
    Exported rather than inlined into ``resolve_mode`` because it is not the only
    caller: the control API asks it which endpoints a block names. The gRPC
    listener once asked it a second time for ``corruption_mode``, with a copy of
    the rule that read ``transports`` and never learned about ``ports``. Since
    gRPC went into ``Listener.serve``, one answer of ``resolve_mode`` gates the
    mode, the latency and the corruption of every interface.
```

`CLAUDE.md`, the paragraph "gRPC ports and `mode="port_closed"`". Replace the sentence "Every gRPC fault but one is a status reply from `GrpcListener.plan()`." with "Every gRPC fault but one is a status reply that `Listener.serve()` decides and the gRPC adapter sends."

- [ ] **Step 3: Run the search again, and the suite**

Run the command of Step 1. Expected: no line.

Run: `python -m pytest tests -q -p no:cacheprovider`. Expected: the count of Task 4, Step 7.

- [ ] **Step 4: Commit**

```bash
git add server.py provider_simulator/control_api.py provider_simulator/fault_policy.py CLAUDE.md
git commit -m "docs: the texts that named GrpcListener.plan name Listener.serve"
```

---

### Task 6: Show that each new or changed test can fail

**Files:** none in the repository.

**Interfaces:**
- Consumes: the branch after Task 5.
- Produces: the list of breaks for the pull request.

The rules for such a run are in the skill `add-simulator-entity`, section "Proving a guard can fail, and the two ways a mutation lies to you". Break one source file in one place. Check first that the old text is in the file exactly one time. Run with `PYTHONDONTWRITEBYTECODE=1`. Put the file back from a copy, and not with `git checkout`. Compare the checksum.

A break that fails no test is not "equivalent" until every path to the changed line is read. In pull request 2a one such break was a missing test.

- [ ] **Step 1: The hooks of `Listener` (`provider_simulator/listeners/base.py`), with `tests/test_listener_rest.py` and `tests/test_listener_grpc.py`**

| Break | Tests that must fail |
|---|---|
| The `down` block finalizes with `method="*"` and no hook | `test_the_flow_asks_the_hooks_for_the_row_and_the_reply_of_a_down_provider`; the grid case `down` |
| The `down` block returns `ServeResult(action="no_body", status=503)` | the same hook test; the grid case `down` fails in `_decide` |
| `waited = verdict.kind != "hang"` becomes `waited = True` | `test_the_flow_asks_the_hook_for_the_latency_of_a_hang_row_and_of_no_other_row`; the case `hang` of `test_which_calls_wait_for_latency_ms` |
| `waited = verdict.kind != "hang"` becomes `waited = False` | the same hook test, at the value 250 |
| `status_label = self.corrupt(...)` becomes `self.corrupt(...)` | `test_the_flow_asks_the_hook_to_corrupt_a_reply_and_takes_its_row_label`; the grid cases `wrong-type` and `invalid-proto` |
| `if result.action == "respond" and targeted:` becomes `if result.action == "respond":` | `test_the_flow_does_not_ask_the_hook_to_corrupt_when_the_filter_does_not_name_the_endpoint`; the grid cases `corruption-with-a-...-filter-that-does-not-name-it` |

- [ ] **Step 2: The hooks of `GrpcListener`, with `tests/test_listener_grpc.py`**

One break for each hook: `early_identity` returns `"*"`; `build_down` returns another text; `unpaid_latency` returns its argument; `method_key` returns the method name; `build_fault` gives another status for each of its four kinds; `build_success` does not read the key `error`; `corrupt` does not return at a status; `corrupt` gives another status for `wrong_type`; `request_id` returns None; `latency = scenario.get("latency_ms", 0) if targeted else 0` of `base.py` loses its condition.

Expected: each break fails one case or more of the grid, of the latency table, or of the single tests. The last break must fail the two cases with 0 of `test_on_grpc_latency_ms_obeys_the_filters`.

- [ ] **Step 3: The adapter and the listener module, with the socket tests**

Run the 65 breaks of pull request 2a again. Each one has a twin in the new code: a break of `_perform` in `server.py` stays in `server.py`; a break of a builder or of the registration moves to `provider_simulator/listeners/grpc.py`; a break of `plan` moves to the hook that holds the rule, or to `_build_grpc` for a per-method error. The plan of 2a, section "The mutations", has the list.

Expected: each break fails one test or more, as in 2a.

Add four breaks for the records of Task 1, in the new code:

| Break | The test that must fail |
|---|---|
| In `_build_grpc`, `"code": name if isinstance(name, str) else ""` becomes `"code": name` | `test_grpc_error_stub_that_is_a_number_has_no_code` of Task 2, and `test_an_error_stub_that_is_a_number_gives_unknown_with_the_number_as_the_text` |
| In `_build_grpc`, `err = method_cfg["error"]` becomes `err = method_cfg["error"] if isinstance(method_cfg["error"], dict) else {}` | `test_a_per_method_error_that_is_no_object_ends_the_call_and_leaves_the_row_in_flight` |
| In `Listener.serve`, the chain gets `scenario if targeted else {**scenario, "responses": {}}` | `test_an_error_stub_applies_when_a_filter_does_not_name_the_endpoint`, and the same test of 2a for a `result` override |
| In `_perform`, `headers = {k: v for (k, v) in metadata}` becomes `headers = {k: v for (k, v) in reversed(list(metadata))}` | `test_a_lava_header_that_comes_two_times_keeps_its_last_value` |

- [ ] **Step 4: Three runs in a row**

Run three times: `python -m pytest tests/test_listener_grpc.py tests/test_listener_rest.py tests/test_chains_lava.py tests/test_fault_policy.py tests/test_simulator_grpc.py tests/test_simulator_grpc_port_closed.py tests/integration -q -p no:cacheprovider`

Expected: the same count each time, with no failure.

---

### Task 7: The pull request

**Files:** none.

**Interfaces:**
- Consumes: the branch of Tasks 1 to 6.
- Produces: the open pull request.

The push and the pull request need Victoria's go. The merge needs its own go. Use the skill `pr`.

- [ ] **Step 1: Open the pull request**

The title: `refactor(grpc): a gRPC call goes through Listener.serve, and GrpcListener.plan is deleted`. The command has `--label no-ticket` and `--assignee`. The body has five parts: Why; What changed; What changes for a caller (the one change of choice 5, and no other); How to verify; What was broken to prove each test. It names choices 1 and 2 of this plan as deviations from section 8.3 of the design, with their reasons. It holds no ticket key and no path of one machine.

- [ ] **Step 2: Read the checks**

Run: `gh pr checks <number>`

Expected: `lint`, `test`, "Suite must pass before anything is published" and "Build and publish image" pass. Open the log of a red check before you call it red. This is the first Linux run of the moved registration (assumption A4 of the design).

---

### Task 8: The automation suites on the k3d cluster, before the merge

**Files:** none in the simulator repository. The evidence goes into a new folder next to the day's handoff file of the automation repository.

**Interfaces:**
- Consumes: the image of the branch build, and a free local k3d cluster.
- Produces: the proof for assumption A5 of the design: no automation test depends on a behaviour that this pull request changes.

Each step on the cluster needs Victoria's go, and the cluster must be free. Read `GET /version` before any measurement. Load the skills `local-cluster`, `local-cluster-testing` and `pick-suite` of the automation repository before the first step, and follow them: this plan does not repeat their commands.

- [ ] **Step 1: Record the state before**

Record the image and the image id of the simulator pod, `GET /version`, `GET /ready`, and that no test process runs on the machine.

- [ ] **Step 2: Put the branch build on the cluster**

Build the image of the branch with the three build arguments of `scripts/deploy.sh`, so that `GET /version` names the commit. Import it into the k3d node, set it on the deployment `provider-simulator`, and wait for the rollout. The log `run1-part-b-swap-to-spike-459274f.log` in the evidence folder of the design has the steps that the session `worker-2` used for another image.

Check: `GET /version` names the head commit of the branch. `GET /ready` answers 200. Then one pairing reset on each router, and one request through each of the 32 routers.

- [ ] **Step 3: One request of each gRPC kind through the router, with the request-id helpers**

Use the helpers that pull request 1449 of the automation repository merged: `send_grpc_all_balances` and `unique_request_id` to send, and `find_request_history` to read. Send one `AllBalances` call through `lava-sim-grpc-router` and read its row by the request id. Expected: one row with the method `AllBalances`, the status `success` and the provider that the reply named.

- [ ] **Step 4: Run all suites**

Run every suite that the skill `pick-suite` lists for a simulator change, the cache suites and the RESP suites too. Compare each test id with the same run on the image of `main`: the same outcome for each id is the pass condition. A test that fails on both images is not a result of this pull request; name it in the report.

- [ ] **Step 5: Put `main` back**

Set the image of `main` on the deployment again. Check `GET /version` and the image id against Step 1. One pairing reset on each router, and one request through each.

---

### Task 9: The paired pull request of the automation repository

**Files (automation repository):**
- Modify: `.claude/skills/writing-simulator-tests/SKILL.md` (the line that names `GrpcListener.plan`, line 210 on 2026-10-08)
- Modify: `.claude/skills/failover/references/what-the-simulator-can-tell-you.md` (the line that names `GrpcListener.plan`, line 58 on 2026-10-08)
- Modify: `.claude/skills/add-simulator-entity/SKILL.md` (the item about `provider_simulator/listeners/`, line 43 on 2026-10-08)

**Interfaces:**
- Consumes: the merged simulator pull request of Task 7.
- Produces: three skill pages that name the code as it is.

The rule is in `add-simulator-entity/SKILL.md`: "Change the code, change the skill, same PR." The simulator pull request merges first, because the CI of the automation repository checks out the default branch of the simulator.

- [ ] **Step 1: Find the lines**

Run in the automation repository: `git grep -n "GrpcListener" -- .claude/skills`

Expected: the three places above. Another place is a stop.

- [ ] **Step 2: Change the three texts**

In the two pages that name `GrpcListener.plan`: the sentence names the place that gives the status of a `down` provider. Replace `GrpcListener.plan` with `GrpcListener.build_down`, and keep the file name `provider_simulator/listeners/grpc.py`.

In `add-simulator-entity/SKILL.md`, the item about `provider_simulator/listeners/`: replace the sentences that say that `GrpcListener` stands on its own and does not subclass `Listener` with:

```
`grpc.py` holds `GrpcListener`. It subclasses `Listener` as the three others do, and a gRPC call goes through `Listener.serve`. The module also holds every protobuf class: the servicer classes, the builders of the reply messages and the registration. `server.py` imports the module inside its gRPC adapter, so the simulator still loads with no `grpcio`.
```

Add one sentence to the same page, where it says where a simulator test goes: "A new test file goes into a folder below `tests/`; `tests/integration/` holds the tests that start a simulator through `SimulatorServer.start()`."

- [ ] **Step 3: Open the pull request**

Use the skill `pr` of the automation repository. The push and the pull request need Victoria's go.

---

## Not in this plan

1. Pull request 2c. It makes four differences the same on every interface, and it removes the hooks `early_identity` and `unpaid_latency` again (section 10.5 of the design).
2. The fix of the two reply builders `build_latest_block` and `build_node_info` for a `result` override of a wrong type (choice 1). A pull request of its own.
3. Servicer classes that are made from the table (choice 2).
4. A test for the router bug that Victoria named on 2026-10-08. It needs one new served gRPC method, `GetBlockByHeight`. After this plan that is the four small changes of choice 2 and its content in the chain.
5. The control API still stores `pause_at` for a provider that has only gRPC endpoints, and gRPC does not perform it. The design does not change that.

## Spec coverage

| Requirement of the design | Where |
|---|---|
| Section 8.3: `GrpcListener` is a subclass of `Listener`; `plan` and `GrpcPlan` are deleted | Tasks 3 and 4; `test_the_grpc_listener_has_no_request_flow_of_its_own` |
| Section 8.3: the reply is a status or a reply message; the texts and the codes stay | `GrpcStatus` and `GrpcReply` of Task 3; the grid of 2a; choice 1 for the place of the build |
| Section 8.3: the method table, and who owns what | Task 4: the listener module owns the protobuf classes, and `server.py` names none; choice 2 for the table |
| Section 8.3: three things stay: a request that does not parse, a method that is not served, `port_closed` and the thread name | Not touched: the generated registration stays. `TestGrpcCallsThatReachNoListener` of 2a and `tests/test_simulator_grpc_port_closed.py` hold them |
| Section 8.3: the per-method error moves to `LavaChain._build_grpc`, with the entry `default`, as data | Task 2 |
| Section 14.6, point 3: the gRPC lookup of an `error_stub` keeps its fallback and does not reuse the REST lookup | Task 2: `test_grpc_error_stub_with_a_name_that_is_no_status_does_not_raise` |
| Section 8.3: the four hooks, with defaults that keep the three HTTP listeners | Task 3, Steps 1 to 4 |
| Section 14.6, point 4: the gRPC hooks drop the pause, the HTTP status and the body text of `rate_limit` | Task 3: `build_fault`; the adapter of Task 4 reads no pause. The tests of 2a: `test_a_pause_does_not_delay_a_grpc_call`, the cases `rate-limit...` |
| Section 14.6, point 1: `drop_at` stays in the gRPC adapter | Task 4, Step 5; `TestGrpcDropPoint` of 2a |
| Section 9.2, row 2: `latency_ms` on gRPC obeys the filters | Task 3: `test_on_grpc_latency_ms_obeys_the_filters` |
| Section 9.3, differences 1 to 6 stay | 1: `early_identity`. 2: `unpaid_latency`. 3, 4 and 5: `corrupt`. 6: `method_key`. The grid of 2a holds each one |
| Section 10, 2b, "Done when": the socket tests of 2a pass with no edit; the grid moves from `plan` to `serve` with no changed value; `GrpcListener.serve is Listener.serve`; `GrpcPlan` does not exist | Global constraints 1, 2 and 4; Task 3, Step 8; Task 4, Step 6 |
| Section 10, 2b, documents: `CLAUDE.md:44`, `server.py:13` and `:1101`, `control_api.py:35` | Task 4 for the docstring of the adapter; Task 5 for the three others and for `fault_policy.py` |
| Section 12: the 2b tests | Task 3: `_decide`, `test_on_grpc_latency_ms_obeys_the_filters`, `test_the_grpc_listener_has_no_request_flow_of_its_own` |
| Section 12, the fourth rule: the automation suites on the k3d cluster before the merge | Task 8 |
| Section 13: the three skill pages in a paired pull request | Task 9 |
| Section 11, assumption A4: the generated registration works from another file than `server.py`, on Linux | Task 7, Step 2 |
