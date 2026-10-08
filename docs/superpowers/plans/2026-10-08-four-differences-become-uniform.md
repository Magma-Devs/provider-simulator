# Four Differences Become Uniform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Four differences between gRPC and the HTTP interfaces become uniform: the method of a provider-wide `down` row, `latency_ms` of a `down` row and of a `hang` row, `corruption_mode="invalid_json"` on gRPC, and the fault keys of a per-method override on gRPC and on Tendermint RPC. The hooks `early_identity` and `unpaid_latency` are removed. No other reply and no other history row changes.

**Architecture:** `Listener.serve` writes the row of a provider-wide `down` itself, with the method `*` and `latency_ms` 0, and it writes 0 for a `hang` row. `GrpcListener` and `TendermintListener` lose their `method_key`, so the flow merges the fault keys of a per-method override with the method name as the key, and the per-method `down` branch of the flow asks `build_down()`. The control API refuses `invalid_json` for a provider that has only gRPC endpoints. `server.py` does not change.

**Tech Stack:** Python 3.12, `grpcio` and `grpcio-reflection` 1.81.1, pytest 9.1.1 with pytest-timeout 2.4.0. The checks of CI: black 26.5.1, ruff 0.15.20 and mypy 2.2.0.

**Spec:** `docs/superpowers/specs/2026-10-06-one-request-flow-design.md`, version 2.3, on the branch `docs/one-request-flow-design` (pull request 137 of `provider-simulator`): sections 6, 8.3, 8.5, 9.3 (differences 1, 2, 5 and 6), 9.4, 10.5, 12 and 13. The decision record `docs/adr/001-one-request-flow.md`. This plan is pull request 2c of that design. The plan of pull request 2b is `docs/superpowers/plans/2026-10-08-grpc-uses-listener-serve.md`.

## The state of this plan

Written on 2026-10-08, after pull request 2b merged as the commit `0b5204c` of `main`. Nothing of this plan is done in the repository.

**What RAN for this plan.** Each code block below ran on a copy of `main` at `0b5204c` in a temporary folder, on a Mac:

- The four changes together, with each test of this plan: `1860 passed`. The same copy with no change gives `1794 passed`.
- The four changes with no change of a test: 16 tests of today fail, and no other test. The table "The expected values that change" names each one.
- The records of Task 1 on the code of `main`: 30 pass. With the four changes, 10 of them fail with their values of today.
- `black --check .`, `ruff check .` and `mypy .` in the versions of CI: clean.
- 37 breaks of the source with the tests that use no socket: each break fails one test or more, and each of the 72 new test cases fails under one break or more. 8 breaks with the 6 socket tests of this plan: each break fails its test.

**What did not run.** The state of the branch after each single task: the counts of Tasks 2 to 4 are computed from the test lists of this plan. Linux: CI is the first Linux run. The automation suites: Task 9.

A step that does not go as this plan says is a stop: tell Victoria, and do not work around it.

## Choices of this plan that the design does not fix

Victoria can change each one.

1. **The rule for `invalid_json` reads the endpoints of the provider and no filter.** Section 10.5 says "a provider that has only gRPC endpoints". A provider with one endpoint of another interface is accepted: that endpoint can apply the corruption. No provider of the topology has a gRPC endpoint and an endpoint of another interface, so the test for it builds its own registry.
2. **A per-method `down` on gRPC and on Tendermint RPC is the per-method `down` of JSON-RPC and REST.** Its row names the method and the request id and records the latency of the entry, and the adapter waits for that latency: the request was read to find the entry. Section 6 of the design keeps that rule. On gRPC the reply is the status of `build_down()`: `UNAVAILABLE`, "provider down".
3. **The flow merges the fault keys of the entry of the called method only.** A fault key in the entry `default` is not read, as on JSON-RPC and REST. A chain still reads the content keys of `default`.
4. **A fault key comes before a content key of the same entry.** With `mode: rate_limit` and `error_stub` in one entry the caller gets the rate limit, as on JSON-RPC and REST: the flow decides a fault before it asks the chain.
5. **The grid of pull request 2a keeps its shape.** The method of a `down` row is written one time, in the assertion of the grid: `*`. The four cases that say "a fault key in a per-method override is not read" leave the grid, and a table of its own holds the fault keys, with each row written out.
6. **The behaviour of today is recorded first, on the code of `main`** (Task 1), for the two changes that no test holds today: `latency_ms` of a `down` row and of a `hang` row on the HTTP interfaces, and the fault keys of a per-method override on Tendermint RPC. So each change shows as a changed expected value, which is the method of pull request 2a.
7. **Task 1 also holds what the review of pull request 2b left open:** the tests for its rows A, B and C, which Victoria accepted on 2026-10-08, the tests for its Minor 1 and Minor 2, and the stale texts of its Minor 5.
8. **This plan file merges with the pull request.** It is the first commit of the branch.
9. **One paired pull request of the automation repository holds the skill pages of 2b and of 2c** (Task 10). Task 9 of the plan of 2b is not done, and its four places are in the same three files.

## Global Constraints

1. No reply and no history row changes but the four changes of section 10.5 of the design. Each changed expected value has a row in the table below, and the pull request names each one.
2. Differences 3, 4, 7, 8, 9, 10, 11 and 12 of section 9.3 stay. The grid cases of 2a for differences 3 and 4 pass with no edit: `wrong-type`, `invalid-proto`, `rate-limit-with-a-corruption-stays-rate-limit` and their neighbours.
3. The hooks `build_down` and `corrupt` stay. After this plan no file of `provider_simulator/` or of `tests/` names `early_identity` or `unpaid_latency`, and no document for a user does: `README.md`, `CLAUDE.md`, `CONTEXT.md`, `docs/using_grpc.md`, `docs/using_the_simulator.md` and `docs/curl_reference.md`.
4. The mode `down` does not change on the wire: HTTP 503 with no body, and `UNAVAILABLE` with "provider down" on gRPC. The row of a provider-wide `down` has no request id.
5. `server.py` does not change.
6. JSON-RPC and REST keep their per-method behaviour: `tests/test_simulator_per_method_overrides.py` passes with no edit.
7. No pull request of the design edits a test file of the cache simulator or of the RESP proxy. The four topology tests pass with no edit: `tests/test_domain_topology.py`, `tests/test_control_api_providers.py`, `tests/test_service_publishes_every_port.py` and `tests/test_values_sim_matches_topology.py`.
8. No test and no document tells a reader to filter `down` rows by a method name (section 13 of the design).
9. Linux decides. CI runs on `ubuntu-latest`. A green run on a Mac does not prove a socket test.
10. The checks of CI: `black --check .`, `ruff check .`, `mypy .`. Line length 120.
11. A commit has a conventional subject, `<type>(<scope>): <summary>`. It has no `Co-Authored-By` line and no trailer. Stage each file by its name. Never use `git add -A`.
12. Every comment, docstring, document line and commit message of this plan is in ASD-STE100 Simplified Technical English.
13. The branch has a name for the behaviour, and the pull request has the label `no-ticket`. No ticket key is in the branch name, the title or the body.
14. The definition of done of the ticket for a pull request: each new test runs three times in a row with the same result; each new test is shown to fail when the thing that it tests is broken, and the pull request says what was broken; the count of what ran is recorded.
15. Before the merge: the automation suites run on the k3d cluster with the branch build (section 12 of the design, the fourth rule). That run needs Victoria's go for each step and a free cluster.
16. A new test file is not put directly into `tests/`. This plan creates no test file.

## The expected values that change

Twenty-six expected values change on purpose: 16 of tests that are on `main` today, and 10 of the records that Task 1 adds. RAN: with the four changes and no change of a test, exactly these fail.

| Change of section 10.5 | Test | Today | After |
|---|---|---|---|
| 1, the method of a `down` row on gRPC | `tests/test_listener_grpc.py::test_down_is_unavailable_and_records_method_not_star` | `GetLatestBlock` | `*`, and the test gets a new name |
| 1 | `tests/test_listener_grpc.py::test_a_provider_wide_down_row_has_no_request_id` | `AllBalances` | `*` |
| 1 | `test_what_serve_decides_for_one_call`, the five cases with a `down` row: `down`, `per-method-mode-success-does-not-lift-a-down`, `down-with-a-corruption-stays-down`, `mode-with-a-transports-filter-that-names-the-endpoint`, `mode-with-a-ports-filter-that-names-the-endpoint` | `GetLatestBlock` | `*` |
| 1 | `tests/test_simulator_grpc.py::TestGrpcAllBalances::test_a_down_provider_row_has_no_request_id` | `AllBalances` | `*` |
| 2, `latency_ms` of a `down` row and of a `hang` row | The six records of Task 1: `test_latency_ms_of_the_row_of_a_call_that_the_provider_did_not_wait_for`, the cases `down` and `hang`, in the listener tests of JSON-RPC, REST and Tendermint RPC | 250 | 0 |
| 2 | `tests/test_listener_rest.py::test_the_flow_asks_the_hooks_for_the_row_and_the_reply_of_a_down_provider` | the value of the hook | 0, and the hook is gone |
| 2 | `tests/test_listener_rest.py::test_the_flow_asks_the_hook_for_the_latency_of_a_hang_row_and_of_no_other_row` | the value of the hook | The test is deleted with the hook. The six records hold the value, and `test_a_rate_limit_row_records_the_latency_that_the_provider_waited` holds "and of no other row". |
| 3, `invalid_json` on gRPC | `tests/test_simulator_grpc.py::TestGrpcStatusTexts::test_invalid_json_corruption_does_nothing_on_grpc` | HTTP 200 | HTTP 400, and the test gets a new name |
| 4, the fault keys of a per-method override | `test_what_serve_decides_for_one_call[per-method-mode-down-is-not-read]` | a reply, row `success` | `UNAVAILABLE`, row `down` with the method |
| 4 | `[per-method-mode-rate-limit-is-not-read]` | a reply, row `success` | `RESOURCE_EXHAUSTED`, row `rate_limit` |
| 4 | `[per-method-latency-is-not-read]` | no wait, row latency 0 | wait 700, row latency 700 |
| 4 | `[per-method-error-probability-is-not-read]` | a reply, row `success` | `UNKNOWN`, row `error` |
| 4 | `tests/test_simulator_grpc.py::TestGrpcWhichCallsWait::test_a_fault_key_in_a_per_method_override_is_not_read` | a reply at once | `UNAVAILABLE` after the latency of the entry, and the test gets a new name |
| 4 | The four records of Task 1 for Tendermint RPC: `test_what_a_fault_key_in_a_per_method_override_does`, the cases `mode-down`, `mode-rate-limit`, `latency`, `error-probability` | a result, row `success` | 503; 429; a wait of 700; an error body |

## Review Focus

Five conditions that the design implies and that no test of today holds. Each one has its test in the task that owns the code.

1. **A per-method `down` on gRPC.** The per-method `down` branch of `Listener.serve` returned the HTTP 503 result and did not ask `build_down()`. A gRPC call then gets a result with no body, and the adapter fails. Tests of Task 5: the cases `mode-down` and `mode-down-waits-for-the-latency-of-the-entry`, `test_the_flow_asks_the_hook_for_the_reply_of_a_per_method_down_and_gives_it_the_latency`, and the socket test `test_a_fault_key_in_a_per_method_override_reaches_the_call_of_that_method`.
2. **A per-method `latency_ms` under a filter that does not name the endpoint.** It must not delay the endpoint. A break of the source that failed no test found this one: two guards hold a per-method `mode` back, and one guard holds a per-method latency back. Tests of Task 5: the case `a-filter-that-does-not-name-the-endpoint-holds-the-latency-of-the-entry-back`, on gRPC and on Tendermint RPC.
3. **`invalid_json` for a gRPC provider in a request that also writes another provider.** A refusal must write no provider of the request. Test of Task 4: `test_an_invalid_json_refusal_writes_no_provider_of_the_request`.
4. **The GET form of Tendermint RPC.** It must use the same per-method entry as the POST form. Test of Task 5: `test_the_get_form_uses_the_same_per_method_entry_as_the_post_form`, and the socket test `test_a_per_method_mode_reaches_the_calls_of_that_method_only`.
5. **A fault key and an `error_stub` in one entry.** The fault comes first (choice 4). Test of Task 5: the case `a-fault-key-comes-before-an-error-stub-of-the-same-entry`.

## File Structure

No file is created but this plan. `server.py` does not change.

| File | Its part in this change |
|---|---|
| `provider_simulator/listeners/base.py` | `Listener.serve`: the row of a provider-wide `down`, the row of a `hang`, the reply of a per-method `down`. The hooks `early_identity` and `unpaid_latency` are deleted. Two docstrings. |
| `provider_simulator/listeners/grpc.py` | `GrpcListener` loses `early_identity`, `unpaid_latency` and `method_key`. The module docstring. |
| `provider_simulator/listeners/tendermint.py` | `TendermintListener` loses `method_key`. The module docstring. |
| `provider_simulator/control_api.py` | One rule: `_an_invalid_json_no_endpoint_can_apply`. |
| `provider_simulator/chains/lava.py`, `README.md` | Three stale texts of the review of 2b. |
| `docs/using_grpc.md`, `docs/using_the_simulator.md` | Five sentences that state the old behaviour. |
| `tests/test_listener_grpc.py`, `tests/test_listener_rest.py`, `tests/test_listener_jsonrpc.py`, `tests/test_listener_tendermint.py`, `tests/test_chains_lava.py`, `tests/test_control_api.py`, `tests/test_simulator_grpc.py`, `tests/test_simulator_tendermintrpc.py` | The tests of Tasks 1 to 5. |

---

### Task 0: The worktree, the plan file and the baseline

**Files:**
- Create: `docs/superpowers/plans/2026-10-08-four-differences-become-uniform.md` (this plan)

**Interfaces:**
- Consumes: `main` of `provider-simulator` at `0b5204c` or later.
- Produces: a worktree on the branch `four-differences-become-uniform`, with this plan as its first commit.

A new branch needs Victoria's go.

- [ ] **Step 1: Make the worktree**

Use the worktree tool of the harness, with the name `four-differences-become-uniform`. It makes the branch from `origin/main`. If the tool gives the branch a prefix, rename it: `git branch -m four-differences-become-uniform`.

- [ ] **Step 2: Run the baseline**

Run: `python -m pytest tests -q -p no:cacheprovider`

Expected: `1794 passed`. Another count is a stop: `main` moved, and the counts of this plan must be read again.

- [ ] **Step 3: Commit the plan**

```bash
git add docs/superpowers/plans/2026-10-08-four-differences-become-uniform.md
git commit -m "docs(plan): the implementation plan of pull request 2c of the request-flow design"
```

---

### Task 1: Records of today's behaviour, and the stale texts of the review of 2b

**Files:**
- Modify: `tests/test_listener_grpc.py` (one comment, and new tests at the end of the file)
- Modify: `tests/test_chains_lava.py` (one new test after `test_grpc_error_stub_that_is_a_number_has_no_code`)
- Modify: `tests/test_simulator_grpc.py` (one new test at the end of the class `TestGrpcPerMethodErrors`, and the docstring of the class `TestGrpcServedMethods`)
- Modify: `tests/test_listener_jsonrpc.py`, `tests/test_listener_rest.py`, `tests/test_listener_tendermint.py` (new tests)
- Modify: `provider_simulator/chains/lava.py` (two comments), `README.md` (one line)

**Interfaces:**
- Consumes: the helpers `_listener`, `_upd`, `_serve`, `_decide`, `_status`, `_override` and `_row` of `tests/test_listener_grpc.py`; `_grpc_error` of `tests/test_chains_lava.py`; `_set_grpc`, `_status_of`, `_call_get_latest_block`, `_rows` and `_GRPC_ADDRS` of `tests/test_simulator_grpc.py`; `_listener`, `_serve`, `_get` and `_post` of the three HTTP listener test files.
- Produces: 30 test cases that pass on `main` with no code change. Tasks 3 and 5 change ten of their expected values on purpose. The other twenty must pass after each later task with no edit.

No code of the simulator changes in this task: only tests, comments and one line of the README.

- [ ] **Step 1: The records for a scenario value of a wrong JSON type, and for a status with a latency and a filter**

These are rows A, B and C and Minor 2 of the review of pull request 2b. Add at the end of `tests/test_listener_grpc.py`:

```python
# --- A scenario value of a wrong JSON type ------------------------------------
# The control API stores each JSON type for ``error_stub``, ``error_message``,
# ``error_code`` and ``blocks_behind``. These tests record what a call gets
# then. Three of the answers changed when gRPC moved into Listener.serve: the
# row of an error_stub that is a list or an object, the row of an error whose
# message or code is a list or an object, and the answer for a blocks_behind
# that is no number under a corruption.


@pytest.mark.parametrize(
    "override, want_text",
    [
        pytest.param({"error_stub": ["NOT_FOUND"]}, "['NOT_FOUND']", id="a-list"),
        pytest.param({"error_stub": {"code": "NOT_FOUND"}}, "{'code': 'NOT_FOUND'}", id="an-object"),
        pytest.param({"error_stub": ["NOT_FOUND"], "message": "gone"}, "gone", id="a-list-with-the-key-message"),
    ],
)
def test_an_error_stub_that_is_a_list_or_an_object_gives_unknown_and_an_error_row(override, want_text):
    listener, provider = _listener()
    _upd(listener, _override(override))
    assert _decide(listener) == _status("UNKNOWN", want_text)
    assert _row(provider) == ("GetLatestBlock", "error", 0, None)


@pytest.mark.parametrize(
    "scenario",
    [
        pytest.param({"mode": "error", "error_message": ["NOT_FOUND"]}, id="error-message-is-a-list"),
        pytest.param({"mode": "error", "error_message": {"name": "NOT_FOUND"}}, id="error-message-is-an-object"),
        pytest.param({"mode": "error", "error_code": [5]}, id="error-code-is-a-list"),
        pytest.param({"error_probability": 1.0, "error_code": {"code": 5}}, id="error-code-is-an-object"),
    ],
)
def test_an_error_whose_message_or_code_is_a_list_or_an_object_leaves_the_row_in_flight(scenario):
    # A fault of today, recorded as it is. The lookup of the status fails
    # before the flow finishes the row. The gRPC library then ends the call
    # with UNKNOWN: tests/test_simulator_grpc.py holds that for the same kind
    # of fault, in test_a_per_method_error_that_is_no_object_...
    listener, provider = _listener()
    _upd(listener, scenario)
    with pytest.raises(TypeError, match="unhashable type"):
        _serve(listener)
    assert _row(provider) == ("*", "in_flight", 0, None)


def test_an_error_code_that_is_a_list_is_not_read_when_error_message_names_a_status():
    listener, provider = _listener()
    _upd(listener, {"mode": "error", "error_message": "NOT_FOUND", "error_code": [5]})
    assert _decide(listener) == _status("NOT_FOUND", "NOT_FOUND")
    assert _row(provider) == ("GetLatestBlock", "error", 0, None)


@pytest.mark.parametrize("corruption", ["wrong_type", "invalid_proto", "empty_response", "truncated", "null_body"])
def test_a_blocks_behind_that_is_no_number_ends_the_call_before_the_corruption(corruption):
    # The flow asks the chain for the content before it corrupts the reply, as
    # on JSON-RPC, REST and Tendermint RPC. So the fault of the chain comes
    # first, and the row stays in_flight.
    listener, provider = _listener()
    _upd(listener, {"blocks_behind": "abc", "corruption_mode": corruption})
    with pytest.raises(TypeError, match="unsupported operand"):
        _serve(listener)
    assert _row(provider) == ("*", "in_flight", 0, None)


def test_get_node_info_does_not_read_blocks_behind():
    listener, provider = _listener()
    _upd(listener, {"blocks_behind": "abc", "corruption_mode": "wrong_type"})
    assert _decide(listener, "GetNodeInfo") == _status("INTERNAL", "wrong_type corruption on response")
    assert _row(provider) == ("GetNodeInfo", "error", 0, None)


@pytest.mark.parametrize(
    "filters, want_ms",
    [
        pytest.param({"transports": ["http2"]}, 250, id="a-filter-that-names-the-endpoint"),
        pytest.param({"transports": ["ws"]}, 0, id="a-filter-that-does-not-name-it"),
    ],
)
def test_on_grpc_a_status_waits_for_latency_ms_only_when_the_filters_name_the_endpoint(filters, want_ms):
    # An error_stub is a status that no filter holds back. The latency of the
    # scenario is held back by a filter that does not name the endpoint.
    listener, provider = _listener()
    _upd(listener, {"latency_ms": 250, **_override({"error_stub": "NOT_FOUND"}), **filters})
    decision = _decide(listener)
    assert (decision.code, decision.wait_ms) == ("NOT_FOUND", want_ms)
    assert _row(provider) == ("GetLatestBlock", "error", want_ms, None)
```

Add to `tests/test_chains_lava.py`, after `test_grpc_error_stub_that_is_a_number_has_no_code`:

```python
@pytest.mark.parametrize(
    "value, want_text",
    [
        pytest.param(["NOT_FOUND"], "['NOT_FOUND']", id="a-list"),
        pytest.param({"code": "NOT_FOUND"}, "{'code': 'NOT_FOUND'}", id="an-object"),
    ],
)
def test_grpc_error_stub_that_is_a_list_or_an_object_has_no_code(value, want_text):
    # The shape of the ``error`` override, given to ``error_stub`` by mistake.
    # The chain does not raise: the listener gives UNKNOWN, with the value as
    # the text.
    assert _grpc_error({"GetLatestBlock": {"error_stub": value}})["error"] == {"code": "", "message": want_text}
```

Add at the end of the class `TestGrpcPerMethodErrors` of `tests/test_simulator_grpc.py`, after `test_an_error_stub_applies_when_a_filter_does_not_name_the_endpoint`:

```python
    def test_an_error_stub_that_is_an_object_gives_unknown_with_the_object_as_the_text(self, sim):
        """The shape of the ``error`` override, given to ``error_stub`` by
        mistake. The control API stores it. The caller gets UNKNOWN, the text
        is the object as text, and the row says error."""
        override = {"GetLatestBlock": {"error_stub": {"code": "NOT_FOUND"}}}
        status, body = _set_grpc(sim, "1", responses=override)
        assert status == 200, body
        answer = _status_of(_call_get_latest_block, _GRPC_ADDRS["1"])
        assert answer == (grpc.StatusCode.UNKNOWN, "{'code': 'NOT_FOUND'}")
        assert [(row["method"], row["status"]) for row in _rows(sim)] == [("GetLatestBlock", "error")]
```

- [ ] **Step 2: The records of `latency_ms` of a `down` row and of a `hang` row on the HTTP interfaces**

This is Minor 1 of the review of 2b: no HTTP test reads that value today.

In `tests/test_listener_jsonrpc.py`, add `import pytest` below `import json`, with one empty line between them. Then add after `test_latency_is_carried_on_the_serve_result`:

```python
@pytest.mark.parametrize("mode", ["down", "hang"])
def test_latency_ms_of_the_row_of_a_call_that_the_provider_did_not_wait_for(mode):
    # A down provider answers at once, and a hung call waits its own 30
    # seconds: the adapter does not wait for latency_ms. Today the row records
    # the configured value all the same. Pull request 2c makes it 0.
    listener, provider = _listener()
    provider.scenario.update({"mode": mode, "latency_ms": 250})
    assert _serve(listener, "eth_blockNumber").latency_ms == 0
    assert [(row["status"], row["latency_ms"]) for row in provider.log.get_history()] == [(mode, 250)]


def test_a_rate_limit_row_records_the_latency_that_the_provider_waited():
    # The control for the test above: a call that waits records its wait.
    listener, provider = _listener()
    provider.scenario.update({"mode": "rate_limit", "latency_ms": 250})
    assert _serve(listener, "eth_blockNumber").latency_ms == 250
    assert provider.log.get_history()[0]["latency_ms"] == 250
```

In `tests/test_listener_rest.py`, add above the comment line `# ── The hooks of the request flow ──`:

```python
@pytest.mark.parametrize("mode", ["down", "hang"])
def test_latency_ms_of_the_row_of_a_call_that_the_provider_did_not_wait_for(mode):
    # A down provider answers at once, and a hung call waits its own 30
    # seconds: the adapter does not wait for latency_ms. Today the row records
    # the configured value all the same. Pull request 2c makes it 0.
    listener, provider = _listener()
    provider.scenario.update({"mode": mode, "latency_ms": 250})
    assert listener.serve(_get(_BLOCKS_LATEST)).latency_ms == 0
    assert [(row["status"], row["latency_ms"]) for row in provider.log.get_history()] == [(mode, 250)]
```

In `tests/test_listener_tendermint.py`, add `import pytest` below `import json`, with one empty line between them. Then add at the end of the file:

```python
@pytest.mark.parametrize("mode", ["down", "hang"])
def test_latency_ms_of_the_row_of_a_call_that_the_provider_did_not_wait_for(mode):
    # A down provider answers at once, and a hung call waits its own 30
    # seconds: the adapter does not wait for latency_ms. Today the row records
    # the configured value all the same. Pull request 2c makes it 0.
    listener, provider = _listener()
    provider.scenario.update({"mode": mode, "latency_ms": 250})
    assert listener.serve(_post("status")).latency_ms == 0
    assert [(row["status"], row["latency_ms"]) for row in provider.log.get_history()] == [(mode, 250)]
```

- [ ] **Step 3: The record of a fault key in a per-method override on Tendermint RPC**

Pull request 2a recorded this for gRPC, in the grid. No test records it for Tendermint RPC. Add at the end of `tests/test_listener_tendermint.py`:

```python
# --- A fault key in a per-method override -------------------------------------
# Today Tendermint RPC does not read the fault keys of a per-method override:
# mode, latency_ms, error_probability, error_code, error_message, http_status
# and drop_at. Pull request 2c makes the flow merge them, as on JSON-RPC and
# REST.


def _row(provider):
    rows = provider.log.get_history()
    assert len(rows) == 1, f"expected one history row, got {len(rows)}"
    return rows[0]["method"], rows[0]["status"], rows[0]["latency_ms"], rows[0]["request_id"]


@pytest.mark.parametrize(
    "scenario, want_reply, want_row",
    [
        # want_reply: the action, the HTTP status, the wait in milliseconds, and
        # the key of the JSON-RPC body ("result" or "error"; None with no body).
        # want_row: the method, the status, latency_ms and the request id.
        pytest.param(
            {"responses": {"status": {"mode": "down"}}},
            ("respond", 200, 0, "result"),
            ("status", "success", 0, 5),
            id="mode-down",
        ),
        pytest.param(
            {"responses": {"status": {"mode": "rate_limit"}}},
            ("respond", 200, 0, "result"),
            ("status", "success", 0, 5),
            id="mode-rate-limit",
        ),
        pytest.param(
            {"responses": {"status": {"latency_ms": 700}}},
            ("respond", 200, 0, "result"),
            ("status", "success", 0, 5),
            id="latency",
        ),
        pytest.param(
            {"responses": {"status": {"error_probability": 1.0}}},
            ("respond", 200, 0, "result"),
            ("status", "success", 0, 5),
            id="error-probability",
        ),
    ],
)
def test_what_a_fault_key_in_a_per_method_override_does(scenario, want_reply, want_row):
    listener, provider = _listener()
    provider.scenario.update(scenario)
    res = listener.serve(_post("status", req_id=5))
    body_key = next((key for key in ("result", "error") if isinstance(res.body, dict) and key in res.body), None)
    assert (res.action, res.status, res.latency_ms, body_key) == want_reply
    assert _row(provider) == want_row
```

- [ ] **Step 4: Run them on the code of `main`**

Run: `python -m pytest tests/test_listener_grpc.py tests/test_chains_lava.py tests/test_listener_jsonrpc.py tests/test_listener_rest.py tests/test_listener_tendermint.py -q -p no:cacheprovider`

Expected: no failure. These tests record today's behaviour, so each one passes with no code change. A test that fails is a stop: this plan read the code wrong.

Run: `python -m pytest tests/test_simulator_grpc.py -q -p no:cacheprovider -k "error_stub_that_is_an_object"`

Expected: `1 passed`.

Run: `python -m pytest tests -q -p no:cacheprovider`

Expected: `1824 passed` (1794 and the 30 new cases).

- [ ] **Step 5: Show that each record can fail**

Break the source in one place, run the listener tests and the chain tests, and put the file back from a copy. Task 7 has the rules for such a run.

| Break | Records that must fail |
|---|---|
| `Listener.unpaid_latency` of `provider_simulator/listeners/base.py` returns 0 | The six cases of `test_latency_ms_of_the_row_of_a_call_that_the_provider_did_not_wait_for`. Before this task that break failed no test. |
| In `Listener.serve`, `latency_ms=latency if waited else self.unpaid_latency(latency),` becomes `latency_ms=0,` | `test_a_rate_limit_row_records_the_latency_that_the_provider_waited` |
| `TendermintListener.method_key` is deleted | The four cases of `test_what_a_fault_key_in_a_per_method_override_does` of `tests/test_listener_tendermint.py` |
| In `_build_grpc` of `provider_simulator/chains/lava.py`, `"code": name if isinstance(name, str) else "",` becomes `"code": name,` | The two cases of `test_grpc_error_stub_that_is_a_list_or_an_object_has_no_code` |
| In `_build_grpc`, `"message": str(method_cfg.get("message", name)),` becomes `"message": method_cfg.get("message", name),` | The cases `a-list` and `an-object` of `test_an_error_stub_that_is_a_list_or_an_object_gives_unknown_and_an_error_row`; the socket test of Step 1 |
| In `_build_grpc`, the same line becomes `"message": str(name),` | The case `a-list-with-the-key-message` |
| `_status_name` of `provider_simulator/listeners/grpc.py` reads `error_message if isinstance(error_message, str) else ""` and `error_code if isinstance(error_code, int) else -1` | The four cases of `test_an_error_whose_message_or_code_is_a_list_or_an_object_leaves_the_row_in_flight` |
| `_status_name` reads `error_code` before `error_message` | `test_an_error_code_that_is_a_list_is_not_read_when_error_message_names_a_status` |
| In `_build_grpc`, a `blocks_behind` that is no number is read as 0 | The five cases of `test_a_blocks_behind_that_is_no_number_ends_the_call_before_the_corruption` |
| In `_build_grpc`, the branch of `GetNodeInfo` reads `scenario.get("blocks_behind", 0) + 0` | `test_get_node_info_does_not_read_blocks_behind` |
| In `Listener.serve`, `latency = scenario.get("latency_ms", 0) if targeted else 0` loses its condition | The case `a-filter-that-does-not-name-it` of `test_on_grpc_a_status_waits_for_latency_ms_only_when_the_filters_name_the_endpoint` |

Expected: each break fails the records that its row names.

- [ ] **Step 6: Correct the stale texts**

Minor 5 of the review of 2b: since pull request 2b the servicer classes are in the listener module, and the chain returns a per-method error as data. Change four texts.

In `tests/test_listener_grpc.py`, in the comment block `# --- The table and the servicers ---`, replace the line

```
# The gRPC adapter in server.py writes its servicer classes by hand. The check
```

with

```
# The gRPC listener module writes its servicer classes by hand. The check
```

In `tests/test_simulator_grpc.py`, replace the docstring of the class `TestGrpcServedMethods` with:

```python
    """The gRPC listener module writes its servicers by hand and holds the
    table of the served methods. The adapter compares the two before it starts
    a server."""
```

In `provider_simulator/chains/lava.py`, in the module docstring, replace the item `- gRPC:` (four lines) with:

```
- gRPC:          the returned dict is the DATA of the reply (height, chain id,
                 node info, balances), or a per-method error under the key
                 ``error``. The gRPC listener module builds the protobuf message
                 from the data, and it turns an error and a corruption into a
                 status, because those are wire concerns, not content.
```

In the same file, in the comment above `_build_grpc`, replace the lines

```
    # Returns plain success-DATA the gRPC listener serializes into a protobuf
    # message. request = {method}. Three unary methods are covered: the two
```

with

```
    # Returns the data of the reply. The gRPC listener module builds the
    # protobuf message from it. request = {method, message}; this function
    # reads the method only. Three unary methods are covered: the two
```

In `README.md`, replace the two lines of the item `listeners/` with:

```
  listeners/                  — base template + jsonrpc/rest/tendermint/grpc, a WS
                                subscription registry, and the corruption serializer
```

The fifth text of Minor 5, the docstring of `Listener.method_key`, changes in Task 5 with its code.

- [ ] **Step 7: Run the suite and the three checks**

Run: `python -m pytest tests -q -p no:cacheprovider`

Expected: `1824 passed`.

Run: `black --check .`, `ruff check .` and `mypy .`

Expected: each one is clean.

- [ ] **Step 8: Commit**

```bash
git add tests/test_listener_grpc.py tests/test_chains_lava.py tests/test_simulator_grpc.py tests/test_listener_jsonrpc.py tests/test_listener_rest.py tests/test_listener_tendermint.py
git commit -m "test: record today's behaviour before four differences become uniform"
git add provider_simulator/chains/lava.py README.md
git commit -m "docs: the gRPC listener module owns the servicers and builds the reply message"
```

The first commit holds the two text edits of the test files too.

---

### Task 2: The row of a `down` provider has the method `*` on gRPC too

Change 1 of section 10.5 of the design. The hook `early_identity` is deleted.

**Files:**
- Modify: `tests/test_listener_grpc.py` (two tests, and the assertion of the grid)
- Modify: `tests/test_listener_rest.py` (the probe class and one test)
- Modify: `tests/test_simulator_grpc.py` (one expected value)
- Modify: `provider_simulator/listeners/base.py` (the `down` block of `serve`; `early_identity` is deleted)
- Modify: `provider_simulator/listeners/grpc.py` (`GrpcListener.early_identity` is deleted)

**Interfaces:**
- Consumes: the branch after Task 1.
- Produces: `Listener.serve` writes `method="*"` and no request id for a provider-wide `down` row, with no hook. `Listener.early_identity` and `GrpcListener.early_identity` do not exist. `Listener.unpaid_latency` still exists until Task 3.

- [ ] **Step 1: Change the expected values**

In `tests/test_listener_grpc.py`, replace the function `test_down_is_unavailable_and_records_method_not_star` with:

```python
def test_down_is_unavailable_and_its_row_names_no_method():
    listener, provider = _listener()
    _upd(listener, {"mode": "down"})
    result = _serve(listener, "GetLatestBlock")
    assert result.body == GrpcStatus("UNAVAILABLE", "provider down")
    hist = provider.log.get_history()[0]
    assert hist["status"] == "down"
    assert hist["method"] == "*"  # a dead node does not read the request
```

In the same file, replace the body of `test_a_provider_wide_down_row_has_no_request_id` with:

```python
    # A dead node does not read the request. The row has the method "*" and no
    # request id, as on JSON-RPC, REST and Tendermint RPC.
    listener, provider = _listener()
    _upd(listener, {"mode": "down"})
    _serve(listener, "AllBalances", _all_balances("never-read"))
    hist = provider.log.get_history()[0]
    assert (hist["status"], hist["method"], hist["request_id"]) == ("down", "*", None)
```

In the same file, replace the function `test_what_serve_decides_for_one_call` (its body only: the decorator and its cases stay) with:

```python
def test_what_serve_decides_for_one_call(scenario, want, want_row_status):
    listener, provider = _listener()
    _upd(listener, scenario)
    assert _decide(listener) == want
    # A down provider does not read the request, so its row has the method "*",
    # as on JSON-RPC, REST and Tendermint RPC. Each down row of this table is
    # the row of a provider-wide down.
    want_method = "*" if want_row_status == "down" else "GetLatestBlock"
    assert _row(provider) == (want_method, want_row_status, 0, None)
```

In `tests/test_listener_rest.py`, delete the method `early_identity` of the class `_ProbeListener`, and in `test_the_flow_asks_the_hooks_for_the_row_and_the_reply_of_a_down_provider` replace the last assertion with:

```python
    assert (row["method"], row["status"], row["latency_ms"], row["request_id"]) == ("*", "down", 7, None)
```

In the comment above the class, "The flow asks four hooks" becomes "The flow asks three hooks".

In `tests/test_simulator_grpc.py`, in `TestGrpcAllBalances::test_a_down_provider_row_has_no_request_id`, replace the last line with:

```python
        assert by_status["history"][0]["method"] == "*"
```

- [ ] **Step 2: Run them to see them fail**

Run: `python -m pytest tests/test_listener_grpc.py tests/test_listener_rest.py -q -p no:cacheprovider`

Expected: 7 fail, all in `tests/test_listener_grpc.py`: `test_down_is_unavailable_and_its_row_names_no_method`, `test_a_provider_wide_down_row_has_no_request_id`, and the cases `down`, `per-method-mode-success-does-not-lift-a-down`, `down-with-a-corruption-stays-down`, `mode-with-a-transports-filter-that-names-the-endpoint` and `mode-with-a-ports-filter-that-names-the-endpoint` of `test_what_serve_decides_for_one_call`. Each one reads `GetLatestBlock` or `AllBalances` where it wants `*`. The probe test of REST passes: the default of the hook gives `*`.

Run: `python -m pytest tests/test_simulator_grpc.py -q -p no:cacheprovider -k "test_a_down_provider_row_has_no_request_id"`

Expected: `1 failed`.

- [ ] **Step 3: Write the code**

In `provider_simulator/listeners/base.py`, replace the `down` block of `serve`

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

with

```python
        # Provider-wide down is pre-parse: no body is read, so the row records
        # method="*" and no request id, on every interface. The provider does
        # not wait for the latency.
        if targeted and mode == "down":
            self.provider.log.finalize(entry, method="*", status="down", latency_ms=self.unpaid_latency(latency))
            return self.build_down()
```

In the same file, delete the method `early_identity` of `Listener` (the five lines from `def early_identity` to `return "*", None`, and the empty line after them).

In `provider_simulator/listeners/grpc.py`, delete the method `early_identity` of `GrpcListener` (the four lines from `def early_identity` to `return request.path, None`, and the empty line after them).

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests -q -p no:cacheprovider`

Expected: `1824 passed`. The count is computed: this task adds no test and deletes none.

- [ ] **Step 5: Commit**

```bash
git add tests/test_listener_grpc.py tests/test_listener_rest.py tests/test_simulator_grpc.py provider_simulator/listeners/base.py provider_simulator/listeners/grpc.py
git commit -m "feat(grpc): the row of a down provider has the method * on gRPC too"
```

---

### Task 3: A `down` row and a `hang` row record `latency_ms` 0 on every interface

Change 2 of section 10.5. The hook `unpaid_latency` is deleted.

**Files:**
- Modify: `tests/test_listener_jsonrpc.py`, `tests/test_listener_rest.py`, `tests/test_listener_tendermint.py` (the records of Task 1; the probe tests of REST)
- Modify: `provider_simulator/listeners/base.py` (two lines of `serve`; `unpaid_latency` is deleted)
- Modify: `provider_simulator/listeners/grpc.py` (`GrpcListener.unpaid_latency` is deleted)

**Interfaces:**
- Consumes: the branch after Task 2.
- Produces: `Listener.serve` records `latency_ms` 0 for a provider-wide `down` row and for a `hang` row, with no hook. `unpaid_latency` does not exist. A per-method `down` row still records the latency of its entry.

- [ ] **Step 1: Change the expected values**

In each of the three files `tests/test_listener_jsonrpc.py`, `tests/test_listener_rest.py` and `tests/test_listener_tendermint.py`, in `test_latency_ms_of_the_row_of_a_call_that_the_provider_did_not_wait_for`: the last line asserts `[(mode, 250)]`. Change it to `[(mode, 0)]`, and replace the three comment lines with:

```python
    # A down provider answers at once, and a hung call waits its own 30
    # seconds: the adapter does not wait for latency_ms. So the row records 0,
    # as on gRPC.
```

In `tests/test_listener_rest.py`, replace the comment above the class `_ProbeListener`, the class, and the two tests `test_the_flow_asks_the_hooks_for_the_row_and_the_reply_of_a_down_provider` and `test_the_flow_asks_the_hook_for_the_latency_of_a_hang_row_and_of_no_other_row` with the text below. The function `_probe` and the two tests of the hook `corrupt` stay as they are.

```python
# ── The hooks of the request flow ────────────────────────────────────────────
# The flow asks two hooks in the places where gRPC differs from the HTTP
# interfaces. Their defaults are what JSON-RPC, REST and Tendermint RPC do, and
# the tests above hold those. These tests show that the flow asks the hooks.


class _ProbeListener(RestListener):
    def build_down(self):
        return ServeResult(action="respond", status=418, body={"probe": "down"})

    def corrupt(self, result, status_label, scenario):
        result.body = {"probe": "corrupted"}
        return "probe-label"
```

and, after `_probe`:

```python
def test_the_flow_asks_the_hook_for_the_reply_of_a_down_provider():
    listener, provider = _probe()
    provider.scenario.update({"mode": "down", "latency_ms": 250})
    res = listener.serve(_get(_BLOCKS_LATEST))
    assert (res.action, res.status, res.body, res.latency_ms) == ("respond", 418, {"probe": "down"}, 0)
    row = provider.log.get_history()[0]
    assert (row["method"], row["status"], row["latency_ms"], row["request_id"]) == ("*", "down", 0, None)
```

The test of the hang hook is deleted with its hook. The records of Task 1 hold the value of a `hang` row on each HTTP interface, and `test_a_rate_limit_row_records_the_latency_that_the_provider_waited` holds the row of a call that waits.

- [ ] **Step 2: Run them to see them fail**

Run: `python -m pytest tests/test_listener_jsonrpc.py tests/test_listener_rest.py tests/test_listener_tendermint.py -q -p no:cacheprovider`

Expected: 7 fail: the six cases of `test_latency_ms_of_the_row_of_a_call_that_the_provider_did_not_wait_for`, and `test_the_flow_asks_the_hook_for_the_reply_of_a_down_provider`. Each one reads 250 where it wants 0.

- [ ] **Step 3: Write the code**

In `provider_simulator/listeners/base.py`, replace the `down` block of `serve` with:

```python
        # Provider-wide down is pre-parse: no body is read, so the row records
        # method="*" and no request id, on every interface. The provider does
        # not wait for the latency, so the row records 0.
        if targeted and mode == "down":
            self.provider.log.finalize(entry, method="*", status="down", latency_ms=0)
            return self.build_down()
```

In the same function, replace

```python
        waited = True  # False for a hang: the provider does not wait for the latency
```

with

```python
        waited = True  # False for a hang: the provider does not wait, and the row records 0
```

and, in the last `finalize` call of the function, replace

```python
            latency_ms=latency if waited else self.unpaid_latency(latency),
```

with

```python
            latency_ms=latency if waited else 0,
```

In the same file, delete the method `unpaid_latency` of `Listener` (the five lines from `def unpaid_latency` to `return latency_ms`, and the empty line after them).

In `provider_simulator/listeners/grpc.py`, delete the method `unpaid_latency` of `GrpcListener` (the three lines from `def unpaid_latency` to `return 0`, and the empty line after them).

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests -q -p no:cacheprovider`

Expected: `1823 passed`. The count is computed: this task deletes one test.

- [ ] **Step 5: Commit**

```bash
git add tests/test_listener_jsonrpc.py tests/test_listener_rest.py tests/test_listener_tendermint.py provider_simulator/listeners/base.py provider_simulator/listeners/grpc.py
git commit -m "feat(listeners): a down row and a hang row record latency_ms 0 on every interface"
```

---

### Task 4: The control API refuses `invalid_json` for a provider that has only gRPC endpoints

Change 3 of section 10.5.

**Files:**
- Modify: `tests/test_control_api.py` (new tests at the end of the file)
- Modify: `tests/test_simulator_grpc.py` (one test), `tests/test_listener_grpc.py` (one comment of the grid)
- Modify: `provider_simulator/control_api.py` (one new function and its call)
- Modify: `provider_simulator/listeners/grpc.py` (one comment)

**Interfaces:**
- Consumes: `ControlApi`, `build_registry` and `WsSubscriptions`, which `tests/test_control_api.py` imports today, and its helper `_api`.
- Produces: `_an_invalid_json_no_endpoint_can_apply(provider: object, scenario_updates: dict) -> str` in `provider_simulator/control_api.py`. An empty text means that the block is accepted. `POST /scenario` answers HTTP 400 with the text.

- [ ] **Step 1: Write the tests**

In `tests/test_control_api.py`, add `import pytest` above the line `from provider_simulator.chains import CHAINS`, with one empty line between them. Then add at the end of the file:

```python
# ── invalid_json on a provider that has only gRPC endpoints ───────────────────


def test_invalid_json_on_a_provider_with_only_grpc_endpoints_is_400():
    """``invalid_json`` breaks the bytes of a JSON body, and a gRPC reply has
    none. Before this rule the value was stored, and it did nothing: each call
    got a clean reply. The message names the mode that a gRPC provider can
    apply."""
    api = _api()
    st, resp = api.apply_scenario({"providers": {"lava-sim-grpc:1": {"corruption_mode": "invalid_json"}}})
    assert st == 400
    assert "lava-sim-grpc:1" in resp["error"]
    assert "'invalid_json' cannot apply" in resp["error"]
    assert "only gRPC endpoints" in resp["error"]
    assert "'invalid_proto'" in resp["error"]
    _, scen = api.get_scenario()
    assert scen["providers"]["lava-sim-grpc:1"]["corruption_mode"] is None


@pytest.mark.parametrize("key", ["eth-sim:1", "lava-sim-rest:1", "lava-sim-tm:1"])
def test_invalid_json_on_a_provider_with_a_json_endpoint_is_accepted(key):
    """The positive control for the test above. Without it, a rule that refused
    ``invalid_json`` for each provider would pass that test."""
    api = _api()
    st, resp = api.apply_scenario({"providers": {key: {"corruption_mode": "invalid_json"}}})
    assert st == 200, resp
    assert resp["applied"][key]["corruption_mode"] == "invalid_json"


@pytest.mark.parametrize(
    "mode", ["truncated", "missing_field", "empty_response", "wrong_type", "null_body", "invalid_proto"]
)
def test_each_other_corruption_mode_is_accepted_on_a_grpc_provider(mode):
    """The gRPC listener applies each of the six: the rule refuses one mode."""
    api = _api()
    st, resp = api.apply_scenario({"providers": {"lava-sim-grpc:1": {"corruption_mode": mode}}})
    assert st == 200, resp
    assert resp["applied"]["lava-sim-grpc:1"]["corruption_mode"] == mode


def test_an_invalid_json_refusal_writes_no_provider_of_the_request():
    api = _api()
    blocks = {
        "eth-sim:1": {"mode": "down"},
        "lava-sim-grpc:1": {"mode": "rate_limit", "corruption_mode": "invalid_json"},
    }
    st, resp = api.apply_scenario({"providers": blocks})
    assert st == 400, resp
    _, scen = api.get_scenario()
    assert scen["providers"]["eth-sim:1"]["mode"] == "success"
    assert scen["providers"]["lava-sim-grpc:1"]["mode"] == "success"


def test_invalid_json_on_a_grpc_provider_is_refused_with_a_filter_too():
    """The rule reads the endpoints of the provider and no filter. A filter can
    only name fewer endpoints, and each endpoint of this provider is gRPC."""
    api = _api()
    block = {"corruption_mode": "invalid_json", "transports": ["http2"]}
    st, resp = api.apply_scenario({"providers": {"lava-sim-grpc:1": block}})
    assert st == 400
    assert "only gRPC endpoints" in resp["error"]


def test_invalid_json_is_accepted_when_one_endpoint_of_the_provider_is_not_grpc():
    """No provider of the topology has a gRPC endpoint and an endpoint of
    another interface. This provider has both, and its REST endpoint can apply
    the corruption."""
    endpoints = (("grpc", "http2", 40001), ("rest", "http", 40002))
    rows = (("lava-mixed-sim", "lava", "1", "LavaMixedProvider1", False, "", endpoints),)
    api = ControlApi(build_registry(rows), WsSubscriptions())
    st, resp = api.apply_scenario({"providers": {"lava-mixed-sim:1": {"corruption_mode": "invalid_json"}}})
    assert st == 200, resp
```

In `tests/test_simulator_grpc.py`, replace the first three lines of `TestGrpcStatusTexts::test_invalid_json_corruption_does_nothing_on_grpc` (the `def` line, the `_set_grpc` line and the `assert status == 200` line) with the lines below. The last three lines of the test stay: the provider answers as before.

```python
    def test_invalid_json_corruption_is_refused_for_a_grpc_provider(self, sim):
        """``invalid_json`` breaks the bytes of a JSON body, and a gRPC reply
        has none. The control API refuses it, and the provider answers as
        before."""
        status, body = _set_grpc(sim, "1", corruption_mode="invalid_json")
        assert status == 400, body
        assert "only gRPC endpoints" in body["error"]
```

In `tests/test_listener_grpc.py`, in the grid, replace the comment

```python
        # Each corruption mode. Five of them turn the reply message into a
        # status, and the row then says error. One clears a field. One does
        # nothing on gRPC.
```

with

```python
        # Each corruption mode. Five of them turn the reply message into a
        # status, and the row then says error. One clears a field. One,
        # invalid_json, does nothing on gRPC: the control API refuses it for a
        # provider that has only gRPC endpoints, and this table writes the
        # scenario with no control API.
```

The grid case `invalid-json-does-nothing` stays with no edit: it is the record of what the listener does when the value reaches it.

- [ ] **Step 2: Run them to see them fail**

Run: `python -m pytest tests/test_control_api.py -q -p no:cacheprovider`

Expected: 3 fail, each with `assert 200 == 400`: `test_invalid_json_on_a_provider_with_only_grpc_endpoints_is_400`, `test_an_invalid_json_refusal_writes_no_provider_of_the_request` and `test_invalid_json_on_a_grpc_provider_is_refused_with_a_filter_too`. The ten other new cases pass: they are the controls.

Run: `python -m pytest tests/test_simulator_grpc.py -q -p no:cacheprovider -k "test_invalid_json_corruption_is_refused_for_a_grpc_provider"`

Expected: `1 failed`.

- [ ] **Step 3: Write the code**

In `provider_simulator/control_api.py`, add above the function `_a_port_closed_that_cannot_happen`:

```python
def _an_invalid_json_no_endpoint_can_apply(provider: object, scenario_updates: dict) -> str:
    """Refuse ``corruption_mode="invalid_json"`` for a provider that has only
    gRPC endpoints.

    ``invalid_json`` breaks the bytes of a JSON body. A gRPC reply is a protobuf
    message or a status, so the gRPC listener has nothing to apply it to. Before
    this rule the value was stored, ``GET /scenario`` echoed it, and each call
    got a clean reply. That is the outcome this module refuses each time: a
    fault that is accepted and does nothing reads as a router that recovered.

    The rule reads the endpoints of the provider and no filter. A provider with
    one endpoint of another interface can apply the corruption there, so its
    block is accepted.
    """
    if scenario_updates.get("corruption_mode") != "invalid_json":
        return ""
    endpoints = list(provider.endpoints)  # type: ignore[attr-defined]
    if endpoints and all(ep.interface == "grpc" for ep in endpoints):
        return (
            "corruption_mode 'invalid_json' cannot apply: it breaks the bytes of a JSON body, and "
            "this provider has only gRPC endpoints, whose replies are protobuf messages. Accepting "
            "it would corrupt nothing, the provider would answer normally, and a test would read "
            "that as a router that recovered. Use 'invalid_proto' on a gRPC provider: it ends the "
            "call with the status UNKNOWN"
        )
    return ""
```

In `ControlApi._apply_scenario` of the same file, replace

```python
            err = _ports_the_provider_does_not_serve(provider, scenario_updates) or _a_pause_no_named_port_can_serve(
                provider, scenario_updates
            )
```

with

```python
            err = (
                _ports_the_provider_does_not_serve(provider, scenario_updates)
                or _a_pause_no_named_port_can_serve(provider, scenario_updates)
                or _an_invalid_json_no_endpoint_can_apply(provider, scenario_updates)
            )
```

In `GrpcListener.corrupt` of `provider_simulator/listeners/grpc.py`, replace the comment

```python
        # invalid_json does nothing on gRPC.
```

with

```python
        # invalid_json does nothing here. The control API refuses it for a
        # provider that has only gRPC endpoints.
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests -q -p no:cacheprovider`

Expected: `1836 passed`. The count is computed: this task adds 13 cases.

- [ ] **Step 5: Commit**

```bash
git add tests/test_control_api.py tests/test_simulator_grpc.py tests/test_listener_grpc.py provider_simulator/control_api.py provider_simulator/listeners/grpc.py
git commit -m "feat(control): invalid_json is refused for a provider that has only gRPC endpoints"
```

---

### Task 5: gRPC and Tendermint RPC merge the fault keys of a per-method override

Change 4 of section 10.5.

**Files:**
- Modify: `tests/test_listener_grpc.py` (two helpers, four cases of the grid, one new table, one new test)
- Modify: `tests/test_listener_tendermint.py` (the table of Task 1, and two new tests)
- Modify: `tests/test_listener_rest.py` (one new probe test)
- Modify: `tests/test_simulator_grpc.py` (one test), `tests/test_simulator_tendermintrpc.py` (one new class)
- Modify: `provider_simulator/listeners/base.py` (the per-method `down` branch of `serve`; the docstring of `method_key`)
- Modify: `provider_simulator/listeners/grpc.py`, `provider_simulator/listeners/tendermint.py` (`method_key` is deleted in each)

**Interfaces:**
- Consumes: the branch after Task 4. `Listener.method_key` returns `request.get("method")`: the parsed request of gRPC and of Tendermint RPC has the key `method`.
- Produces: on gRPC and on Tendermint RPC, `Listener.serve` merges the keys of `_METHOD_OVERRIDE_KEYS` from `responses[<method name>]`. The per-method `down` branch returns `self.build_down()` with `latency_ms` set to the latency of the entry.

- [ ] **Step 1: Change the tests of the gRPC listener**

In `tests/test_listener_grpc.py`, replace the helpers `_status` and `_reply` with:

```python
def _status(code, text, *, hangs=False, drop_at=None, wait_ms=0):
    """The caller gets a status. With no ``wait_ms`` the adapter does not wait."""
    return _Decision(code, text, wait_ms, hangs, drop_at, None)


def _reply(*, clears=None, wait_ms=0):
    """The caller gets a reply message. With no ``wait_ms`` the adapter does not wait."""
    return _Decision("OK", "", wait_ms, False, None, clears)
```

In the grid, delete the comment `# A fault key in a per-method override is not read on gRPC.` and the four cases below it with the ids `per-method-mode-down-is-not-read`, `per-method-mode-rate-limit-is-not-read`, `per-method-latency-is-not-read` and `per-method-error-probability-is-not-read`. The case `per-method-mode-success-does-not-lift-a-down` stays. Put this comment above it:

```python
        # A down provider does not read the request, so a per-method override
        # cannot lift its down. The table of the fault keys of a per-method
        # override is below this one.
```

Add below the function `test_what_serve_decides_for_one_call`:

```python
# --- A fault key in a per-method override -------------------------------------
# The flow merges the seven fault keys of the entry of the called method into
# the scenario, as on JSON-RPC and REST: mode, latency_ms, error_probability,
# error_code, error_message, http_status and drop_at. The row of a per-method
# down names its method and waits for its latency: the request was read to find
# the entry.

_GLB = "GetLatestBlock"


@pytest.mark.parametrize(
    "scenario, want, want_row",
    [
        pytest.param(
            _override({"mode": "down"}),
            _status("UNAVAILABLE", "provider down"),
            (_GLB, "down", 0, None),
            id="mode-down",
        ),
        pytest.param(
            _override({"mode": "down", "latency_ms": 250}),
            _status("UNAVAILABLE", "provider down", wait_ms=250),
            (_GLB, "down", 250, None),
            id="mode-down-waits-for-the-latency-of-the-entry",
        ),
        pytest.param(
            _override({"mode": "rate_limit"}),
            _status("RESOURCE_EXHAUSTED", "Too many requests"),
            (_GLB, "rate_limit", 0, None),
            id="mode-rate-limit",
        ),
        pytest.param(
            _override({"mode": "hang", "latency_ms": 250}),
            _status("CANCELLED", "hang timeout", hangs=True),
            (_GLB, "hang", 0, None),
            id="mode-hang-does-not-wait-for-a-latency",
        ),
        pytest.param(
            _override({"mode": "drop_connection", "drop_at": "after_headers"}),
            _status("UNAVAILABLE", "connection dropped", drop_at="after_headers"),
            (_GLB, "drop_connection", 0, None),
            id="mode-drop-connection-with-its-drop-at",
        ),
        pytest.param(_override({"latency_ms": 700}), _reply(wait_ms=700), (_GLB, "success", 700, None), id="latency"),
        pytest.param(
            {"latency_ms": 250, **_override({"latency_ms": 700})},
            _reply(wait_ms=700),
            (_GLB, "success", 700, None),
            id="the-latency-of-the-entry-wins-over-the-latency-of-the-provider",
        ),
        pytest.param(
            _override({"error_probability": 1.0}),
            _status("UNKNOWN", "Internal error"),
            (_GLB, "error", 0, None),
            id="error-probability",
        ),
        pytest.param(
            _override({"error_probability": 1.0, "error_message": "NOT_FOUND"}),
            _status("NOT_FOUND", "NOT_FOUND"),
            (_GLB, "error", 0, None),
            id="error-message-of-the-entry",
        ),
        pytest.param(
            _override({"error_probability": 1.0, "error_message": "no status has this name", "error_code": 5}),
            _status("NOT_FOUND", "no status has this name"),
            (_GLB, "error", 0, None),
            id="error-code-of-the-entry",
        ),
        pytest.param(
            {"mode": "rate_limit", **_override({"mode": "success"})},
            _reply(),
            (_GLB, "success", 0, None),
            id="mode-success-lifts-a-fault-of-the-provider-that-is-not-down",
        ),
        pytest.param(
            _override({"mode": "rate_limit"}, method="GetNodeInfo"),
            _reply(),
            (_GLB, "success", 0, None),
            id="an-entry-of-another-method-does-not-apply",
        ),
        pytest.param(
            {"responses": {"default": {"mode": "rate_limit"}}},
            _reply(),
            (_GLB, "success", 0, None),
            id="a-fault-key-in-the-default-entry-is-not-read",
        ),
        pytest.param(
            {**_override({"mode": "rate_limit"}), "transports": ["ws"]},
            _reply(),
            (_GLB, "success", 0, None),
            id="a-filter-that-does-not-name-the-endpoint-holds-the-fault-key-back",
        ),
        pytest.param(
            {**_override({"latency_ms": 700}), "transports": ["ws"]},
            _reply(),
            (_GLB, "success", 0, None),
            id="a-filter-that-does-not-name-the-endpoint-holds-the-latency-of-the-entry-back",
        ),
        pytest.param(
            {**_override({"mode": "rate_limit"}), "transports": ["http2"]},
            _status("RESOURCE_EXHAUSTED", "Too many requests"),
            (_GLB, "rate_limit", 0, None),
            id="a-filter-that-names-the-endpoint-lets-the-fault-key-through",
        ),
        pytest.param(
            _override({"mode": "rate_limit", "error_stub": "NOT_FOUND"}),
            _status("RESOURCE_EXHAUSTED", "Too many requests"),
            (_GLB, "rate_limit", 0, None),
            id="a-fault-key-comes-before-an-error-stub-of-the-same-entry",
        ),
    ],
)
def test_what_a_fault_key_in_a_per_method_override_does(scenario, want, want_row):
    listener, provider = _listener()
    _upd(listener, scenario)
    assert _decide(listener) == want
    assert _row(provider) == want_row


def test_the_row_of_a_per_method_down_keeps_the_request_id():
    listener, provider = _listener()
    _upd(listener, _override({"mode": "down"}, method="AllBalances"))
    want = _status("UNAVAILABLE", "provider down")
    assert _decide(listener, "AllBalances", _all_balances("lava1-probe-d")) == want
    assert _row(provider) == ("AllBalances", "down", 0, "lava1-probe-d")
```

- [ ] **Step 2: Change the tests of the Tendermint RPC listener**

In `tests/test_listener_tendermint.py`, replace the comment block `# --- A fault key in a per-method override ---` (its five comment lines) with:

```python
# --- A fault key in a per-method override -------------------------------------
# The flow merges the seven fault keys of the entry of the called method into
# the scenario, as on JSON-RPC and REST: mode, latency_ms, error_probability,
# error_code, error_message, http_status and drop_at. The key of the entry is
# the method name, for the POST form and for the GET form.
```

Replace the four cases of `test_what_a_fault_key_in_a_per_method_override_does` with these nine. The first four have the ids of Task 1 and new expected values:

```python
        pytest.param(
            {"responses": {"status": {"mode": "down"}}},
            ("no_body", 503, 0, None),
            ("status", "down", 0, 5),
            id="mode-down",
        ),
        pytest.param(
            {"responses": {"status": {"mode": "rate_limit"}}},
            ("respond", 429, 0, "error"),
            ("status", "rate_limit", 0, 5),
            id="mode-rate-limit",
        ),
        pytest.param(
            {"responses": {"status": {"latency_ms": 700}}},
            ("respond", 200, 700, "result"),
            ("status", "success", 700, 5),
            id="latency",
        ),
        pytest.param(
            {"responses": {"status": {"error_probability": 1.0}}},
            ("respond", 200, 0, "error"),
            ("status", "error", 0, 5),
            id="error-probability",
        ),
        pytest.param(
            {"responses": {"status": {"mode": "down", "latency_ms": 250}}},
            ("no_body", 503, 250, None),
            ("status", "down", 250, 5),
            id="mode-down-waits-for-the-latency-of-the-entry",
        ),
        pytest.param(
            {"mode": "rate_limit", "responses": {"status": {"mode": "success"}}},
            ("respond", 200, 0, "result"),
            ("status", "success", 0, 5),
            id="mode-success-lifts-a-fault-of-the-provider-that-is-not-down",
        ),
        pytest.param(
            {"responses": {"block": {"mode": "rate_limit"}}},
            ("respond", 200, 0, "result"),
            ("status", "success", 0, 5),
            id="an-entry-of-another-method-does-not-apply",
        ),
        pytest.param(
            {"responses": {"status": {"mode": "rate_limit"}}, "transports": ["ws"]},
            ("respond", 200, 0, "result"),
            ("status", "success", 0, 5),
            id="a-filter-that-does-not-name-the-endpoint-holds-the-fault-key-back",
        ),
        pytest.param(
            {"responses": {"status": {"latency_ms": 700}}, "transports": ["ws"]},
            ("respond", 200, 0, "result"),
            ("status", "success", 0, 5),
            id="a-filter-that-does-not-name-the-endpoint-holds-the-latency-of-the-entry-back",
        ),
```

Add at the end of the file:

```python
def test_a_per_method_error_takes_its_code_its_message_and_its_http_status_from_the_entry():
    listener, _ = _listener()
    entry = {"error_probability": 1.0, "error_code": -32005, "error_message": "limit exceeded", "http_status": 500}
    listener.provider.scenario.update({"responses": {"status": entry}})
    res = listener.serve(_post("status", req_id=9))
    assert (res.status, res.body["id"], res.body["error"]) == (500, 9, {"code": -32005, "message": "limit exceeded"})


def test_the_get_form_uses_the_same_per_method_entry_as_the_post_form():
    listener, provider = _listener()
    provider.scenario.update({"responses": {"status": {"mode": "rate_limit"}}})
    assert listener.serve(_get("/status")).status == 429
    assert listener.serve(_get("/health")).status == 200
    assert [(row["method"], row["status"]) for row in provider.log.get_history()] == [
        ("status", "rate_limit"),
        ("health", "success"),
    ]
```

- [ ] **Step 3: Add the probe test of a per-method `down`, and change the socket tests**

In `tests/test_listener_rest.py`, add after `test_the_flow_asks_the_hook_for_the_reply_of_a_down_provider`:

```python
def test_the_flow_asks_the_hook_for_the_reply_of_a_per_method_down_and_gives_it_the_latency():
    # The row of a per-method down names its method and its request id, and it
    # records the latency: the request was read to find the entry, and the
    # adapter waits before it answers.
    listener, provider = _probe()
    override = {("GET", _BLOCKS_LATEST): {"mode": "down", "latency_ms": 250}}
    provider.scenario.update({"responses": override})
    res = listener.serve(_with_query(_BLOCKS_LATEST, {"request_id": ["probe-id"]}))
    assert (res.action, res.status, res.body, res.latency_ms) == ("respond", 418, {"probe": "down"}, 250)
    row = provider.log.get_history()[0]
    assert (row["method"], row["status"], row["latency_ms"], row["request_id"]) == (
        f"GET {_BLOCKS_LATEST}",
        "down",
        250,
        "probe-id",
    )
```

In `tests/test_simulator_grpc.py`, replace the whole test `TestGrpcWhichCallsWait::test_a_fault_key_in_a_per_method_override_is_not_read` with:

```python
    def test_a_fault_key_in_a_per_method_override_reaches_the_call_of_that_method(self, sim):
        """The flow merges the fault keys of a per-method override, such as
        ``mode`` and ``latency_ms``, as on JSON-RPC and REST. The call of the
        named method waits for the latency of the entry, and then it gets the
        status of a down provider. Its row names the method: the request was
        read to find the entry. A call of another method is answered at once."""
        status, body = _set_grpc(sim, "1", responses={"GetLatestBlock": {"mode": "down", "latency_ms": 600}})
        assert status == 200, body
        started = time.monotonic()
        answer = _status_of(_call_get_latest_block, _GRPC_ADDRS["1"])
        elapsed = time.monotonic() - started
        assert answer == (grpc.StatusCode.UNAVAILABLE, "provider down")
        assert 0.55 <= elapsed < 5.0, f"the status came after {elapsed:.3f} s with latency_ms=600 in the entry"
        assert _call_get_node_info(_GRPC_ADDRS["1"]).default_node_info.network == "lava-sim"
        assert [(row["method"], row["status"], row["latency_ms"]) for row in _rows(sim)] == [
            ("GetLatestBlock", "down", 600),
            ("GetNodeInfo", "success", 0),
        ]
```

In `tests/test_simulator_tendermintrpc.py`, add after the class `TestTmErrorStubs`:

```python
class TestTmPerMethodFaultKeys:
    """The request flow merges the fault keys of a per-method override on
    Tendermint RPC, as on JSON-RPC and REST. The key of the entry is the method
    name, for the POST form and for the GET form."""

    def test_a_per_method_mode_reaches_the_calls_of_that_method_only(self, sim):
        status, body, _ = _set_tm(sim, "1", responses={"status": {"mode": "rate_limit"}})
        assert status == 200, body
        assert _tm_post(sim, "1", "status")[0] == 429
        assert _tm_get(sim, "1", "status")[0] == 429
        assert _tm_post(sim, "1", "health")[0] == 200
        _, hist, _ = _request("GET", _ctrl(sim, "/history?pool=lava-sim-tm&pid=1"))
        assert [(e["method"], e["status"]) for e in hist["history"]] == [
            ("status", "rate_limit"),
            ("status", "rate_limit"),
            ("health", "success"),
        ]

    def test_a_per_method_down_answers_503_and_its_row_names_the_method(self, sim):
        """The row of a provider-wide down has the method "*". The row of a
        per-method down names its method and its request id: the request was
        read to find the entry."""
        status, body, _ = _set_tm(sim, "1", responses={"status": {"mode": "down"}})
        assert status == 200, body
        assert _tm_post(sim, "1", "status", request_id=77)[0] == 503
        _, hist, _ = _request("GET", _ctrl(sim, "/history?pool=lava-sim-tm&pid=1"))
        assert [(e["method"], e["status"], e["request_id"]) for e in hist["history"]] == [("status", "down", 77)]
```

- [ ] **Step 4: Run them to see them fail**

Run: `python -m pytest tests/test_listener_grpc.py tests/test_listener_tendermint.py tests/test_listener_rest.py -q -p no:cacheprovider`

Expected: 23 fail.

- 14 in `tests/test_listener_grpc.py`: `test_the_row_of_a_per_method_down_keeps_the_request_id`, and 13 cases of `test_what_a_fault_key_in_a_per_method_override_does`. Its four cases `an-entry-of-another-method-does-not-apply`, `a-fault-key-in-the-default-entry-is-not-read` and the two cases `a-filter-that-does-not-name-the-endpoint-...` pass: they say that a key is not read.
- 8 in `tests/test_listener_tendermint.py`: the two new tests, and six cases of the table. Its three cases `an-entry-of-another-method-does-not-apply` and `a-filter-that-does-not-name-the-endpoint-...` pass.
- 1 in `tests/test_listener_rest.py`: the new probe test. It reads the HTTP 503 result where it wants the reply of the hook.

Run: `python -m pytest tests/test_simulator_grpc.py tests/test_simulator_tendermintrpc.py -q -p no:cacheprovider -k "a_fault_key_in_a_per_method_override or TestTmPerMethodFaultKeys"`

Expected: `3 failed`.

- [ ] **Step 5: Write the code**

In `Listener.serve` of `provider_simulator/listeners/base.py`, in the branch `if verdict.kind == "down":`, replace

```python
                return ServeResult(action="no_body", status=503, latency_ms=latency)
```

with

```python
                result = self.build_down()
                result.latency_ms = latency
                return result
```

In the same file, replace the docstring of `method_key` with:

```python
        """The ``responses`` key that selects this request's per-method fault
        override. Default: the method name of the request, which is the key on
        JSON-RPC, Tendermint RPC and gRPC. REST overrides to its (verb,
        template) pair."""
```

In `provider_simulator/listeners/grpc.py`, delete the method `method_key` of `GrpcListener` (the four lines from `def method_key` to `return None`, and the empty line after them).

In `provider_simulator/listeners/tendermint.py`, delete the method `method_key` of `TendermintListener` (the four lines from `def method_key` to `return None`, and the empty line after them).

- [ ] **Step 6: Run the tests**

Run: `python -m pytest tests -q -p no:cacheprovider`

Expected: `1860 passed`. RAN on the copy of `main` with the four changes.

- [ ] **Step 7: Commit**

```bash
git add tests/test_listener_grpc.py tests/test_listener_tendermint.py tests/test_listener_rest.py tests/test_simulator_grpc.py tests/test_simulator_tendermintrpc.py provider_simulator/listeners/base.py provider_simulator/listeners/grpc.py provider_simulator/listeners/tendermint.py
git commit -m "feat(listeners): gRPC and Tendermint RPC merge the fault keys of a per-method override"
```

---

### Task 6: The texts that state the old behaviour

**Files:**
- Modify: `provider_simulator/listeners/base.py`, `provider_simulator/listeners/grpc.py`, `provider_simulator/listeners/tendermint.py` (the module docstrings)
- Modify: `docs/using_grpc.md`, `docs/using_the_simulator.md`

**Interfaces:**
- Consumes: the branch after Task 5.
- Produces: no docstring and no document of the repository states a behaviour that this plan changed.

- [ ] **Step 1: The module docstring of `provider_simulator/listeners/base.py`**

Replace the three paragraphs from "``down`` is evaluated and emitted BEFORE the body is parsed" to "(how the interface corrupts a reply, and the label of its row)." with:

```
``down`` is evaluated and emitted BEFORE the body is parsed (a dead node never
reads the request), so a down call's history carries method ``"*"`` and
``request_id`` None on every interface — matching the long-standing contract
other code relies on. The provider does not wait for the latency, so the row
records ``latency_ms`` 0. A ``hang`` row records 0 for the same reason.
The exception is a per-method ``responses`` override with ``mode="down"``: the
method had to be parsed to find the override, so that entry carries the real
method, request id, and the configured latency, which the adapter waits for.

Per-method ``responses`` overrides can shadow the fault keys (mode, latency_ms,
error probability/code/message, http_status, drop_at) for one method — the
merged config inherits every provider-wide key the override doesn't set. The
override key is the transport's ``method_key`` (JSON-RPC, Tendermint RPC and
gRPC: the method name; REST: the (verb, template) route pair). The transports
filter scopes per-method overrides the same way it scopes everything else in
the block.

Two more hooks have a default that is right for the HTTP interfaces, and gRPC
overrides each one: ``build_down`` (the reply of a down provider) and
``corrupt`` (how the interface corrupts a reply, and the label of its row).
```

- [ ] **Step 2: The module docstring of `provider_simulator/listeners/grpc.py`**

Replace the list "The rules that are special to gRPC, and the hook that holds each one:" (the line and its seven items) with:

```
The rules that are special to gRPC, and the hook that holds each one:
- A served method can name one field of its request message as the request id
  (``SERVED_METHODS``). For AllBalances that field is ``address``. A method that
  names no field records no request id (``request_id``).
- A fault is a status: down -> UNAVAILABLE (``build_down``); hang -> CANCELLED
  after 30 seconds, drop -> UNAVAILABLE, rate_limit -> RESOURCE_EXHAUSTED,
  error -> the status that error_message names, or error_code as a number,
  else UNKNOWN (``build_fault``).
- A per-method error_stub or error override is a status, not a body. The chain
  returns it as data (``build_success``).
- A corruption acts on a reply message only: missing_field clears a field;
  wrong_type gives INTERNAL; invalid_proto, empty_response, truncated and
  null_body give UNKNOWN (``corrupt``). invalid_json has no meaning for a
  protobuf message: the control API refuses it for a provider that has only
  gRPC endpoints, and this listener does nothing with it.

Three rules are the same as on the HTTP interfaces, and the flow holds them:
- The row of a provider-wide ``down`` has the method ``"*"`` and no request id:
  a dead node does not read the request.
- A provider-wide ``down`` row and a ``hang`` row record latency 0, because the
  provider did not wait.
- The fault keys of a per-method override are merged into the scenario. The key
  of the entry is the bare method name.
```

- [ ] **Step 3: The module docstring of `provider_simulator/listeners/tendermint.py`**

Add one paragraph at the end of the docstring, after the line "JSON-RPC ``error`` envelope — both matching the flat TendermintHandler." and one empty line:

```
A per-method ``responses`` entry has two kinds of keys. The request flow merges
its fault keys (``mode``, ``latency_ms`` and the others) with the method name as
the key, as on JSON-RPC. LavaChain reads its content keys (``error_stub``,
``error``, ``body``) in the success path.
```

- [ ] **Step 4: The two documents**

In `docs/using_grpc.md`, in the table "Which primitives apply", add this row after the row of `latency_ms` / `error_probability`:

```
| a fault key in a per-method `responses` entry: `mode`, `latency_ms`, `error_probability`, `error_code`, `error_message`, `drop_at` | yes | the key of the entry is the bare method name, for example `GetLatestBlock`. The entry reaches the calls of that method only, and its fault comes before an `error_stub` of the same entry. The control API refuses a per-method `mode=error` and a per-method `mode=port_closed`, as on every interface |
```

and replace the row of `corruption_mode="invalid_json"` with:

```
| `corruption_mode="invalid_json"` | no | JSON-only. The control API answers HTTP 400 for a provider that has only gRPC endpoints: the value would be stored and do nothing |
```

In `docs/using_the_simulator.md`, three sentences change.

The sentence "The row of a provider-wide `down` has no request id on a JSON-RPC, REST, Tendermint RPC or gRPC call. A dead node does not read the request. Count those calls with `GET /stats`." becomes:

```
The row of a provider-wide `down` has the method `*` and no request id on a JSON-RPC, REST, Tendermint RPC or gRPC call. A dead node does not read the request. The row records `latency_ms` 0, and so does a `hang` row: the provider did not wait. Count those calls with `GET /stats`.
```

The two sentences "A REST `down` row has the method `*`, and the smart-router polls each provider all the time, so the `down` row of the request and the `down` row of a poll look the same. A gRPC `down` row holds the method name today, but do not filter on it: the design "One request flow for every interface" makes that method `*` too, and the filter then finds no row and proves nothing." become:

```
A `down` row has the method `*` on every interface, and the smart-router polls each provider all the time, so the `down` row of the request and the `down` row of a poll look the same. Do not filter `down` rows by a method name: the filter finds no row and proves nothing.
```

In the table "Fault primitives across chain families", in the row of `corruption_mode`, the gRPC cell gets one more part at its end:

```
; `invalid_json` is refused with HTTP 400 for a provider that has only gRPC endpoints
```

- [ ] **Step 5: Search for a text that is left**

Run: `git grep -n -E "early_identity|unpaid_latency" -- provider_simulator tests server.py README.md CLAUDE.md CONTEXT.md docs/using_grpc.md docs/using_the_simulator.md docs/curl_reference.md`

Expected: no line.

Run: `git grep -n -i -E "does not merge|not read on gRPC|down. row holds the method|records it, and not" -- provider_simulator tests docs/using_grpc.md docs/using_the_simulator.md README.md CONTEXT.md`

Expected: no line. On `main` at `0b5204c` this search finds five lines, so it can find one. A line that it finds now is a stop: show it to Victoria.

- [ ] **Step 6: Run the suite and the three checks**

Run: `python -m pytest tests -q -p no:cacheprovider`

Expected: `1860 passed`.

Run: `black --check .`, `ruff check .` and `mypy .`

Expected: each one is clean.

- [ ] **Step 7: Commit**

```bash
git add provider_simulator/listeners/base.py provider_simulator/listeners/grpc.py provider_simulator/listeners/tendermint.py docs/using_grpc.md docs/using_the_simulator.md
git commit -m "docs: the texts for the four behaviours that are uniform now"
```

---

### Task 7: Show that each new or changed test can fail

**Files:** none in the repository. The sets of breaks and their reports go into the evidence folder of the design, next to the day's handoff file of the automation repository.

**Interfaces:**
- Consumes: the branch after Task 6.
- Produces: the list of breaks for the pull request.

The rules for such a run are in the skill `add-simulator-entity`, section "Proving a guard can fail, and the two ways a mutation lies to you". Break one source file in one place. Check first that the old text is in the file exactly one time. Run with `PYTHONDONTWRITEBYTECODE=1`. Put the file back from a copy, and not with `git checkout`. Compare the checksum.

A break that fails no test is not "equivalent" until every path to the changed line is read. For this plan one such break was a missing test: Review Focus 2.

- [ ] **Step 1: The breaks with the tests that use no socket**

Run each break with: `tests/test_listener_grpc.py tests/test_listener_rest.py tests/test_listener_jsonrpc.py tests/test_listener_tendermint.py tests/test_chains_lava.py tests/test_control_api.py` (360 cases). The last column is the count that failed on the copy of `main` with the four changes.

In `Listener.serve` of `provider_simulator/listeners/base.py`:

| Break | Failed |
|---|---|
| The `down` row gets `method=request.path or "*"` | 9 |
| The `down` row gets `latency_ms=latency` | 5 |
| `latency_ms=latency if waited else 0,` becomes `latency_ms=latency,` | 5 |
| The same line becomes `latency_ms=0,` | 19 |
| The per-method `down` branch returns `ServeResult(action="no_body", status=503, latency_ms=latency)` again | 4 |
| The line `result.latency_ms = latency` is deleted | 3 |
| The row of a per-method `down` gets `latency_ms=0` | 3 |
| The row of a per-method `down` gets `request_id=None` | 4 |
| The row of a per-method `down` gets `method="*"` | 6 |
| `key = self.method_key(parsed)` becomes `key = None` | 24 |
| `cfg = responses.get(key) if ...` reads `responses.get(key) or responses.get("default")` | 1 |
| `cfg = responses.get(key) if ...` reads `next(iter(responses.values()), None)` | 4 |
| `if targeted:` above `key = self.method_key(parsed)` becomes `if True:` | 2 |
| The same break, and `fault_policy.ladder(mode, merged) if targeted else fault_policy.NONE_VERDICT` loses its condition | 4 |
| `latency = scenario.get("latency_ms", 0) if targeted else 0` loses its condition | 3 |
| One key is deleted from `_METHOD_OVERRIDE_KEYS`, seven breaks: `mode`, `latency_ms`, `error_probability`, `error_code`, `error_message`, `http_status`, `drop_at` | 16, 6, 5, 2, 3, 1, 1 |

In the listeners, the control API and the chain:

| Break | Failed |
|---|---|
| `GrpcListener` gets a `method_key` that returns `None` | 14 |
| `TendermintListener` gets a `method_key` that returns `None` | 8 |
| `GrpcListener.build_down` gives the status `ABORTED` | 11 |
| `_status_name` gets type guards for `error_message` and `error_code` | 4 |
| `_status_name` reads `error_code` before `error_message` | 2 |
| `_apply_scenario` does not ask `_an_invalid_json_no_endpoint_can_apply` | 3 |
| In that rule, `if endpoints and all(...)` becomes `if endpoints:` | 4 |
| In that rule, `all(` becomes `any(` | 1 |
| In that rule, the first condition becomes `if not scenario_updates.get("corruption_mode"):` | 6 |
| In that rule, the first condition also returns for a block with `transports` | 1 |
| In `_build_grpc`, `"code": name if isinstance(name, str) else "",` becomes `"code": name,` | 3 |
| In `_build_grpc`, `"message": str(method_cfg.get("message", name)),` loses `str(...)` | 5 |
| In `_build_grpc`, the same line becomes `"message": str(name),` | 3 |
| In `_build_grpc`, a `blocks_behind` that is no number is read as 0 | 5 |
| In `_build_grpc`, the branch of `GetNodeInfo` reads `blocks_behind` | 1 |

Expected: each of the 37 breaks fails one test or more, and each new test case of Tasks 1 to 5 fails under one break or more.

- [ ] **Step 2: The breaks with the socket tests**

| Break | The test that must fail |
|---|---|
| The `down` row gets `method=request.path or "*"` | `TestGrpcAllBalances::test_a_down_provider_row_has_no_request_id` |
| `_apply_scenario` does not ask the rule for `invalid_json` | `test_invalid_json_corruption_is_refused_for_a_grpc_provider` |
| `GrpcListener` gets a `method_key` that returns `None` | `test_a_fault_key_in_a_per_method_override_reaches_the_call_of_that_method` |
| The per-method `down` branch returns the HTTP 503 result again | the same test |
| The line `result.latency_ms = latency` is deleted | the same test |
| In `_build_grpc`, the text of an `error_stub` loses `str(...)` | `test_an_error_stub_that_is_an_object_gives_unknown_with_the_object_as_the_text` |
| `TendermintListener` gets a `method_key` that returns `None` | the two tests of `TestTmPerMethodFaultKeys` |
| The row of a per-method `down` gets `method="*"` | `TestTmPerMethodFaultKeys::test_a_per_method_down_answers_503_and_its_row_names_the_method` |

Expected: each break fails its test.

- [ ] **Step 3: Three runs in a row**

Run three times: `python -m pytest tests/test_listener_grpc.py tests/test_listener_rest.py tests/test_listener_jsonrpc.py tests/test_listener_tendermint.py tests/test_chains_lava.py tests/test_control_api.py tests/test_simulator_grpc.py tests/test_simulator_tendermintrpc.py tests/test_simulator_per_method_overrides.py -q -p no:cacheprovider`

Expected: the same count each time, with no failure.

---

### Task 8: The pull request

**Files:** none.

**Interfaces:**
- Consumes: the branch of Tasks 0 to 7.
- Produces: the open pull request.

The push and the pull request need Victoria's go. The merge needs its own go. Use the skill `pr`.

- [ ] **Step 1: Open the pull request**

The title: `feat(listeners): four differences between gRPC and the HTTP interfaces become uniform`. The command has `--label no-ticket` and `--assignee`. The body has six parts: Why; What changed; What changes for a caller (the four changes of section 10.5 of the design, and the table "The expected values that change" of this plan with each test by its name); How to verify; What was broken to prove each test (Task 7); Not in this pull request. It names the nine choices of this plan. It says that the records for rows A, B and C of the review of pull request 141 are in this pull request, and that Victoria accepted the three rows. It holds no ticket key and no path of one machine.

- [ ] **Step 2: Read the checks**

Run: `gh pr checks <number>`

Expected: `lint`, `test`, "Suite must pass before anything is published" and "Build and publish image" pass. The log of `test` must say `1860 passed`. Open the log of a red check before you call it red. This is the first Linux run of the socket tests of this plan.

---

### Task 9: The automation suites on the k3d cluster, before the merge

**Files:** none in the simulator repository. The evidence goes into a new folder next to the day's handoff file of the automation repository.

**Interfaces:**
- Consumes: the image of the branch build, the image of `main` at `0b5204c`, and a free local k3d cluster.
- Produces: the proof for section 13 of the design: no automation test depends on a behaviour that this pull request changes. It is also the run that pull request 2b did not have before its merge.

Each step on the cluster needs Victoria's go, and the cluster must be free: ask each open session first. Read `GET /version` before any measurement. Load the skills `local-cluster`, `local-cluster-testing` and `pick-suite` of the automation repository before the first step, and follow them: this plan does not repeat their commands. Run each automation test with `CLUSTER_PROFILE=local`. Do not run the test file that has the marker `destructive` without Victoria's word.

- [ ] **Step 1: Record the state before**

Record the image and the image id of the simulator pod, `GET /version`, `GET /ready`, and that no test process runs on the machine. On 2026-10-08 the pod ran the commit `6f4e933`, which is older than pull requests 2a and 2b.

- [ ] **Step 2: The image of `main` at `0b5204c`, and all suites**

Build the image of `main` with the three build arguments of `scripts/deploy.sh`, so that `GET /version` names the commit. Import it into the k3d node under a tag of its commit: do not move the tag `ghcr.io/magma-devs/provider-simulator:main` of the node. Set it on the deployment `provider-simulator`, and wait for the rollout. Check `GET /version` and `GET /ready`. Then one pairing reset on each router, and one request through each router.

Run every suite that the skill `pick-suite` lists for a simulator change, the cache suites and the RESP suites too. This run is the base of the comparison. A test that fails here and passed on `6f4e933` is a finding for pull request 2b: show it to Victoria before the next step.

- [ ] **Step 3: The branch build, and all suites**

Build and import the image of the head of the branch in the same way. Check that `GET /version` names the head commit. One pairing reset on each router, and one request through each router.

Send one `AllBalances` call through `lava-sim-grpc-router` with the helpers `send_grpc_all_balances`, `unique_request_id` and `find_request_history` of the automation repository, and read its row by the request id. Expected: one row with the method `AllBalances` and the status `success`.

Run the same suites. The pass condition: the same outcome for each test id as in Step 2. A test that fails on both images is not a result of this pull request; name it in the report.

- [ ] **Step 4: Leave the cluster in a known state**

Set the image that Step 1 recorded on the deployment again, or the image that Victoria names. Check `GET /version` and the image id. One pairing reset on each router, and one request through each.

---

### Task 10: The paired pull request of the automation repository

**Files (automation repository):**
- Modify: `.claude/skills/writing-simulator-tests/SKILL.md`
- Modify: `.claude/skills/failover/references/what-the-simulator-can-tell-you.md`
- Modify: `.claude/skills/add-simulator-entity/SKILL.md`

**Interfaces:**
- Consumes: the merged simulator pull request of Task 8.
- Produces: skill pages that name the code as it is after pull requests 2b and 2c.

The rule is in `add-simulator-entity/SKILL.md`: "Change the code, change the skill, same PR." The simulator pull request merges first, because the CI of the automation repository checks out the default branch of the simulator. This task also does Task 9 of the plan of 2b, which is not done.

- [ ] **Step 1: Find the lines**

Run in the automation repository: `git grep -n "GrpcListener" -- .claude/skills`

Expected: four places in three files. On 2026-10-08, at the commit `44b3fcb77e`, they were `writing-simulator-tests/SKILL.md:210`, `failover/references/what-the-simulator-can-tell-you.md:58`, and `add-simulator-entity/SKILL.md:42` and `:43`. Another place is a stop.

Search the skill pages for a sentence that states a behaviour which pull request 2c changed: the method of a gRPC `down` row, `latency_ms` of a `down` row or of a `hang` row, `invalid_json` on gRPC, and a per-method fault key on gRPC or on Tendermint RPC. A search of 2026-10-08 at `44b3fcb77e` found no such sentence. It had a control: the same search found the four places of `GrpcListener`. A sentence that the search finds now is a stop: show it to Victoria.

- [ ] **Step 2: The four places of pull request 2b**

In `writing-simulator-tests/SKILL.md` and in `failover/references/what-the-simulator-can-tell-you.md`: the sentence names the place that gives the status of a `down` provider. Replace `GrpcListener.plan` with `GrpcListener.build_down`, and keep the file name `provider_simulator/listeners/grpc.py`.

In `add-simulator-entity/SKILL.md`, replace the sentence "It is exported because `GrpcListener._targeted` also asks it." with:

```
It is exported because the control API also asks it which endpoints a block names.
```

In the same page, in the item about `provider_simulator/listeners/`, replace the sentences that say that `GrpcListener` stands on its own and does not subclass `Listener` with:

```
`grpc.py` holds `GrpcListener`. It subclasses `Listener` as the three others do, and a gRPC call goes through `Listener.serve`. The module also holds every protobuf class: the servicer classes, the builders of the reply messages and the registration. `server.py` imports the module inside its gRPC adapter, so the simulator still loads with no `grpcio`.
```

Add one sentence to the same page, where it says where a simulator test goes: "A new test file goes into a folder below `tests/`; `tests/integration/` holds the tests that start a simulator through `SimulatorServer.start()`."

- [ ] **Step 3: The new facts of pull request 2c**

In `failover/references/what-the-simulator-can-tell-you.md`, in the part "**gRPC, `down`**", add one item:

```
- History: the row of a provider-wide `down` has the method `*` and no request
  id, as on JSON-RPC, REST and Tendermint RPC. Do not filter `down` rows by a
  method name. The row records `latency_ms` 0, and so does a `hang` row.
```

In `add-simulator-entity/SKILL.md`, in the paragraph about the checks of a scenario, after the sentence that ends "because only the HTTP write path performs a pause.", add:

```
`corruption_mode="invalid_json"` is refused with 400 for a provider that has only gRPC endpoints, because a gRPC reply has no JSON body (`control_api._an_invalid_json_no_endpoint_can_apply`).
```

In `writing-simulator-tests/SKILL.md`, at the end of the section "Scenario fields - transports and ports", add:

```
**A per-method override can hold a fault key on every interface.** An entry of `responses` can hold `mode`, `latency_ms`, `error_probability`, `error_code`, `error_message`, `http_status` and `drop_at`, and the simulator applies them to the requests of that method only. The key of the entry is the method name on JSON-RPC, Tendermint RPC and gRPC, and the `[verb, template]` pair on REST. A filter that does not name the endpoint holds these keys back. The control API refuses `mode="error"` and `mode="port_closed"` in an entry.
```

- [ ] **Step 4: Open the pull request**

Use the skill `pr` of the automation repository. The push and the pull request need Victoria's go.

---

## Not in this plan

1. A type check of the control API. It stores a value of a wrong JSON type for `error_stub`, `error_message`, `error_code`, `http_status`, `rate_limit_body`, `missing_field` and `blocks_behind`, and it raises `TypeError` for a list or an object in `mode`, `then_mode`, `drop_at`, `pause_at` and `corruption_mode`, and for a number or a boolean in `transports`. RAN on 2026-10-08 with 3,780 scenarios, the same before and after pull request 2b. A check that answers HTTP 400 is a new check: it needs Victoria's approval and a pull request of its own. The records of Task 1 for rows B and C use no control API, so they still reach their code when that check exists.
2. A fault key in the entry `default` of `responses`. It is stored and not read, on every interface (choice 3).
3. A check of the fault keys of a per-method entry. The control API refuses `mode="error"` and `mode="port_closed"` there, and it checks no other value and no type.
4. The rows of a WebSocket subscribe frame. The copy of the flow in `server.py` writes them: its `down` row records the configured latency and the method. Pull requests 3a and 3b own that code. A finding for the plan of 3a: pin `latency_ms` of the `down` row of a subscribe frame.
5. A WebSocket frame under a per-method `down` with a latency. The row records the latency, and the adapter closes the connection with no wait. Step 3 of the design owns that adapter.
6. A check that compares the reply builders of the gRPC listener module with the table of the served methods (Minor 3 of the review of 2b). It is a new check, so it needs Victoria's approval. It matters before the next served gRPC method.
7. The start of the simulator with a partly broken gRPC install (Minor 4 of the review of 2b).
8. The control API still stores `pause_at` for a provider that has only gRPC endpoints, and gRPC does not perform it. The design does not change that.
9. Differences 3, 4, 7, 8, 9, 10, 11 and 12 of section 9.3 of the design.
10. The old parts of `docs/using_grpc.md` and `docs/using_the_simulator.md` that name `chain_family` and `handlers_grpc.py`. They were stale before this design.

## Spec coverage

| Requirement of the design | Where |
|---|---|
| Section 10.5, change 1: the method of a provider-wide `down` row on gRPC becomes `*` | Task 2 |
| Section 10.5, change 2: `latency_ms` of a `down` row and of a `hang` row becomes 0 on JSON-RPC, REST and Tendermint RPC | Task 1, Step 2 for the records; Task 3 |
| Section 10.5, change 3: `invalid_json` on a provider that has only gRPC endpoints gets HTTP 400 | Task 4; choice 1 for the rule |
| Section 10.5, change 4: a per-method fault key on gRPC and on Tendermint RPC takes effect; the hook `method_key` of Tendermint RPC changes | Task 1, Step 3 for the record; Task 5 |
| Section 10.5, "The hooks and 2c": `unpaid_latency` and `early_identity` are removed; `build_down` and `corrupt` stay; a row still holds its method before the latency wait | Tasks 2 and 3; global constraint 3; the test `test_the_row_of_a_call_is_complete_while_the_provider_still_waits` of 2a passes with no edit |
| Section 10.5, part C: one test changes one expected value, `tests/test_listener_grpc.py:46-54` | Task 2. Pull request 2a added seven more places that hold the method of a gRPC `down` row: the table "The expected values that change" |
| Section 10.5, part D: each of differences 2, 5 and 6 gets a new test | Tasks 1, 3, 4 and 5 |
| Section 10.5, "Why 2c is its own pull request": each uniform behaviour shows as a named change of an expected value | The table "The expected values that change"; Task 8, Step 1 |
| Section 6: a per-method `down` records the method and the id, and that stays | Choice 2; Task 5 |
| Section 8.5: gRPC and Tendermint RPC merge the fault keys in 2c | Task 5 |
| Section 9.3: differences 3, 4, 7 to 12 stay | Global constraint 2; "Not in this plan", item 9 |
| Section 12, the fourth rule: the automation suites on the k3d cluster before the merge | Task 9 |
| Section 13, row 2c: the skill pages that state the old behaviour, found by a search | Task 10, Step 1; the search of 2026-10-08 found none |
| Section 13: no test and no document tells a reader to filter `down` rows by method | Global constraint 8; Task 6, Step 4 |
| Section 10.5, "Risk, proof and rollback of 2c": revert 2c while it is the newest merged one | Task 8: one pull request, so one revert |
| The review of pull request 2b: rows A, B and C, Minor 1, Minor 2, Minor 5 | Task 1 |
| The review of pull request 2b, recommendation 5: the per-method `down` branch must ask `build_down()` | Task 5; Review Focus 1 |
