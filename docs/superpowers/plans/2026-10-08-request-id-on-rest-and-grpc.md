# Request Id on REST and gRPC History Rows Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A test can read the history rows of one REST request and of one gRPC request through the smart-router, with `GET /history?request_id=<id>&pool=<pool>`.

**Architecture:** The row field `request_id` and the filter stay as they are. Each interface names one place where the caller puts the id. REST reads the query parameter `request_id`, then the header `X-Request-Id` in any letter case, then the counter of the simulator. gRPC reads the field `address` of one new served method, `cosmos.bank.v1beta1.Query/AllBalances`. The listener of each interface finds the id. The socket adapter in `server.py` only hands the request over.

**Tech Stack:** Python 3.12, `http.server`, `grpcio` and `grpcio-reflection` (`grpc.aio`), the compiled protobuf stubs in `cosmos_pb2/`, pytest. The checks of CI: black 26.5.1, ruff 0.15.20 and mypy 2.2.0.

**Spec:** `docs/superpowers/specs/2026-10-06-one-request-flow-design.md`, sections 8.2, 9.1, 9.2 (row 1), 10 ("Step 1"), 11, 12 and 13, and the decision record `docs/adr/002-request-id-on-every-interface.md`. This plan is pull request 1 of that design: "a request id on REST and gRPC". Victoria gave her go for ADR-002 and for this plan on 2026-10-08.

## What is proven, and how

Each line says who did the check and how.

| Fact | How it is known |
|---|---|
| The router passes the REST query parameter `request_id` and the gRPC field `address` of `AllBalances` on to the provider, and the chain spec lists `AllBalances`. | RUN by the design session and by the session `worker` on 2026-10-07, on the local k3d cluster, with a probe image. Section 11 of the design, rows A1, A2 and A3. |
| Each of the 52 new tests was run before the change of its task and after it. 44 fail before the change and pass after it. Eight pass before it and after it, because they pin a rule that holds today: five in `tests/test_listener_rest.py`, two in `tests/test_simulator_rest.py` and one in `tests/test_chains_lava.py`. Each task names them. | RUN by the author of this plan on 2026-10-08, on a throwaway copy of commit `e80acbf`, on macOS, with Python 3.12.12 and `grpcio` 1.81.1. Each task gives the result that was seen. |
| The five touched test files and two neighbours pass with the whole change: `tests/test_listener_rest.py`, `tests/test_simulator_rest.py`, `tests/test_chains_lava.py`, `tests/test_listener_grpc.py`, `tests/test_simulator_grpc.py`, `tests/test_simulator_grpc_port_closed.py`, `tests/test_fault_policy.py`. | RUN on the same copy: `332 passed`. |
| The four source files and the five test files of this plan (section "File Structure") pass `black --check` and `ruff check` with the versions of CI. `mypy .` passes. | RUN on the same copy: black 26.5.1, ruff 0.15.20. mypy was version 2.3.0; CI installs 2.2.0. |
| The command line examples that Task 5 puts into the documents. | RUN on the same copy with `grpcurl` 1.9.3 and `curl`. |
| The whole suite with the change. | RUN on the branch `request-id-on-rest-and-grpc` on 2026-10-08, on macOS: `1601 passed` after Task 6, and `1605 passed` after the fixes of the review (section "After the review of the branch"). The baseline `1549 passed` at `e80acbf` is read from section 16.5 of the design. |
| Linux. | NOT RUN. CI runs on `ubuntu-latest`, and CI decides. |
| The change through a router. | NOT RUN with this code. Task 7 runs it with the branch build. |

## Global Constraints

1. The router does not change.
2. One row field, `request_id`, and one filter, `GET /history?request_id=<id>&pool=<pool>`. No new row field and no new filter.
3. REST reads the id in this order: the query parameter `request_id`; the header `X-Request-Id`, in any letter case; the counter of the simulator. With more than one value of the parameter, the first value counts. This holds for the five verbs that reach the listener: GET, POST, PUT, DELETE and HEAD.
4. An empty value is no id, on REST and on gRPC.
5. gRPC serves `AllBalances` of the service `cosmos.bank.v1beta1.Query`. Its request field `address` is the id. Its reply is one coin: `ulava`, amount `1000000`. Each other method of the bank service answers `UNIMPLEMENTED`.
6. The gRPC key of a method is its bare name, for example `GetLatestBlock`. The simulator refuses to start with two served methods of one name.
7. The row of a provider-wide `down` has no request id on a JSON-RPC, REST, Tendermint RPC or gRPC call.
8. The test `test_grpc_request_id_is_none` at `tests/test_simulator_grpc.py:546-551` passes with no edit.
9. No reply and no history row changes, except these three: REST records the query parameter `request_id`; gRPC serves `AllBalances` and records its address; the header `X-Request-Id` is read in any letter case.
10. Every test of the repository passes. No test is edited except the five test files that this plan names. An expected value is not edited to pass.
11. Linux decides. CI runs on `ubuntu-latest`. A green run on a Mac does not prove a socket test.
12. The topology does not change: no pool, provider, endpoint or port is added, removed or renumbered.
13. The checks of CI: `black --check .`, `ruff check .`, `mypy .`. Line length 120.
14. A commit has a conventional subject, `<type>(<scope>): <summary>`. It has no `Co-Authored-By` line. It does not name an agent configuration file. Stage each file by its name. Never use `git add -A`.
15. Every comment, docstring, document line and commit message of this plan is in ASD-STE100 Simplified Technical English.
16. The simulator tests bind fixed local ports: 18545 to 18628 and the control port 19000. Run one pytest process at a time on a machine. Before a run, `lsof -nP -iTCP:19000 -sTCP:LISTEN` must print nothing.

## Review Focus

Five conditions that the design implies and that its test table in section 12 does not name. Each one has its test in the task that owns the code.

1. **One request reaches two providers**, because the router tries a second provider. The filter must return one row for each provider, and no row of another request. Tests: `test_query_request_id_selects_the_rows_of_one_request` (Task 1) and `test_address_selects_the_rows_of_one_request` (Task 4).
2. **A provider answers with a fault.** Its row must keep the id, so that a test can read every attempt of one request. The row of a provider-wide `down` must not have it. Tests: `test_a_fault_row_keeps_the_id` and `test_a_provider_wide_down_row_has_no_request_id` (Task 1); `test_a_fault_row_of_all_balances_keeps_the_request_id` and `test_a_provider_wide_down_row_has_no_request_id` (Task 3); `test_a_failed_call_keeps_the_address_on_its_row` and `test_a_down_provider_row_has_no_request_id` (Task 4).
3. **An empty value**: `?request_id=`, an empty header, an empty `address`. It is no id. REST then reads the header, and then uses the counter. Tests: `test_an_empty_query_value_is_no_id_so_the_header_applies`, `test_an_empty_header_value_is_no_id_so_the_counter_applies` and `test_empty_query_request_id_falls_back_to_the_counter` (Task 1); `test_an_empty_address_is_no_request_id` (Task 3).
4. **A REST path that the simulator does not know, or a verb with no route.** The `not_found` row must keep the id. Tests: `test_an_unknown_path_keeps_the_id_on_its_not_found_row` and `test_every_verb_that_reaches_the_listener_records_the_query_id` (Task 1).
5. **A caller sends a plain number as the id.** The simulator does not refuse it. The row holds the text, and the filter also matches a counter row with the same number. Test: `test_a_plain_number_from_the_caller_is_kept_as_text` (Task 1). The documents of Task 5 state the rule for the caller.

## File Structure

No file is created. These files change:

| File | Its part in this change |
|---|---|
| `provider_simulator/listeners/rest.py` | Finds the request id of a REST request: the query parameter, the header, the counter. |
| `provider_simulator/chains/lava.py` | Gives the content of `AllBalances`: the balances of the REST route for the same query. |
| `provider_simulator/listeners/grpc.py` | Holds the table of the served gRPC methods with the request-id field of each one. Finds the id in the request message and writes it on the history row. |
| `server.py` | The gRPC adapter: loads the bank stubs, registers the bank servicer, hands the request message to the listener, builds the reply message. |
| `tests/test_listener_rest.py` | The REST id rules, with no socket. |
| `tests/test_simulator_rest.py` | The REST id through a socket and the control API. |
| `tests/test_chains_lava.py` | The content of `AllBalances`. |
| `tests/test_listener_grpc.py` | The gRPC id rules and the table of served methods, with no socket. |
| `tests/test_simulator_grpc.py` | `AllBalances` and reflection through a real gRPC client. |
| `CONTEXT.md` | Five glossary entries. |
| `docs/curl_reference.md`, `docs/using_the_simulator.md`, `docs/using_grpc.md` | The rule for a caller, and the new method. |

## Before Task 1: three things that Victoria decides

1. **The story and the branch name.** A Jira write needs her go. In `provider-simulator` the workflow `jira-comment.yml` reads a ticket key from the pull request body, then from the branch name, then from the title. So a branch name with a ticket key posts a Jira comment. With a story and a wanted comment, the branch is `mag-<number>`. With no story, the branch is `request-id-on-rest-and-grpc`, and the pull request gets the label `no-ticket`.
2. **The worktree.** The work starts in a new worktree from `origin/main` (skill `superpowers:using-git-worktrees`). It does not start in the worktree of the design: the branch `worktree-one-request-flow-design` holds the probe commit `55f3eaf`, which is not for merge and which changes three files of this plan.
3. **The spec on `main`.** The design and the two decision records are in pull request 137, "docs(design): one request flow for every interface, with two decision records". The simplest order is to merge it first, with this plan in it. Then the new worktree holds the spec and the plan. If it is not merged, the executor reads both from the branch `docs/one-request-flow-design`.

---

### Task 1: REST reads the request id from the query parameter

**Files:**
- Modify: `provider_simulator/listeners/rest.py:56-62` and `:90`
- Test: `tests/test_listener_rest.py` (the import block at line 4, and the end of the file)
- Test: `tests/test_simulator_rest.py` (the header at lines 25-26, the import block at line 32, and the class `TestRestHistory` after line 974)

**Interfaces:**
- Consumes: `RawRequest.query`, a dict in which each value is a list, as `parse_qs` gives it (`server.py:144`). `RawRequest.headers`, a dict with each header name as the client wrote it (`server.py:141`).
- Produces: `_caller_request_id(request: RawRequest) -> str | None` in `provider_simulator/listeners/rest.py`. No other task uses it.

- [ ] **Step 1: Write the listener tests**

In `tests/test_listener_rest.py`, add the import of pytest above the first import:

```python
import pytest

from provider_simulator.domain.endpoint import Endpoint
```

Add this block at the end of the file:

```python
# --- The request id ---------------------------------------------------------
# The caller chooses the request id, and a test reads the rows of one request
# with it. The smart-router passes a query string on to the provider, and it
# passes a header only when the chain spec declares that header. So the query
# parameter ``request_id`` is the place that works through the router. The
# header ``X-Request-Id`` stays for a caller that talks to the simulator
# directly.

_VALIDATORS = "/cosmos/staking/v1beta1/validators"


def _with_query(path, query, headers=None, verb="GET"):
    return RawRequest(verb=verb, path=path, query=query, headers=headers or {})


def _only_row(provider):
    rows = provider.log.get_history()
    assert len(rows) == 1, f"expected one history row, got {len(rows)}"
    return rows[0]


def test_request_id_comes_from_the_query_parameter():
    listener, provider = _listener()
    listener.serve(_with_query(_VALIDATORS, {"request_id": ["probe-a1"]}))
    assert _only_row(provider)["request_id"] == "probe-a1"


def test_the_query_parameter_wins_over_the_header():
    listener, provider = _listener()
    listener.serve(_with_query(_VALIDATORS, {"request_id": ["from-query"]}, headers={"X-Request-Id": "from-header"}))
    assert _only_row(provider)["request_id"] == "from-query"


def test_the_first_value_counts_when_the_parameter_repeats():
    listener, provider = _listener()
    listener.serve(_with_query(_VALIDATORS, {"request_id": ["first", "second"]}))
    assert _only_row(provider)["request_id"] == "first"


def test_a_query_value_that_is_not_in_a_list_is_read_too():
    # A live request goes through parse_qs, which puts each value in a list. A
    # request that a test builds by hand can carry the bare value.
    listener, provider = _listener()
    listener.serve(_with_query(_VALIDATORS, {"request_id": "bare"}))
    assert _only_row(provider)["request_id"] == "bare"


@pytest.mark.parametrize("header_name", ["X-Request-Id", "x-request-id", "X-REQUEST-ID", "X-request-id"])
def test_the_header_is_read_in_any_letter_case(header_name):
    listener, provider = _listener()
    listener.serve(_get(_VALIDATORS, headers={header_name: "trace-7"}))
    assert _only_row(provider)["request_id"] == "trace-7"


def test_an_empty_query_value_is_no_id_so_the_header_applies():
    listener, provider = _listener()
    listener.serve(_with_query(_VALIDATORS, {"request_id": [""]}, headers={"X-Request-Id": "from-header"}))
    assert _only_row(provider)["request_id"] == "from-header"


def test_an_empty_header_value_is_no_id_so_the_counter_applies():
    listener, provider = _listener()
    listener.serve(_get(_VALIDATORS, headers={"x-request-id": ""}))
    assert isinstance(_only_row(provider)["request_id"], int)


def test_with_no_id_the_counter_of_the_simulator_stays():
    listener, provider = _listener()
    listener.serve(_get(_VALIDATORS))
    listener.serve(_get(_VALIDATORS))
    ids = sorted(row["request_id"] for row in provider.log.get_history())
    assert all(isinstance(request_id, int) for request_id in ids)
    assert ids[1] == ids[0] + 1


def test_a_plain_number_from_the_caller_is_kept_as_text():
    # The counter gives numbers and the filter compares text, so a caller must
    # not send a plain number. The simulator does not refuse one: the row holds
    # the text that arrived.
    listener, provider = _listener()
    listener.serve(_with_query(_VALIDATORS, {"request_id": ["7"]}))
    assert _only_row(provider)["request_id"] == "7"


@pytest.mark.parametrize("verb", ["GET", "POST", "PUT", "DELETE", "HEAD"])
def test_every_verb_that_reaches_the_listener_records_the_query_id(verb):
    listener, provider = _listener()
    listener.serve(_with_query(_VALIDATORS, {"request_id": [f"id-{verb}"]}, verb=verb))
    assert _only_row(provider)["request_id"] == f"id-{verb}"


def test_an_unknown_path_keeps_the_id_on_its_not_found_row():
    listener, provider = _listener()
    res = listener.serve(_with_query("/nope", {"request_id": ["lost-1"]}))
    assert res.status == 404
    row = _only_row(provider)
    assert (row["status"], row["request_id"]) == ("not_found", "lost-1")


def test_a_fault_row_keeps_the_id():
    listener, provider = _listener()
    provider.scenario.update({"mode": "error", "error_code": -1, "error_message": "boom"})
    listener.serve(_with_query(_VALIDATORS, {"request_id": ["fault-1"]}))
    row = _only_row(provider)
    assert (row["status"], row["request_id"]) == ("error", "fault-1")


def test_a_provider_wide_down_row_has_no_request_id():
    # A dead node does not read the request, so the row has the method "*" and
    # no request id. A test counts those calls with /stats.
    listener, provider = _listener()
    provider.scenario.update({"mode": "down"})
    listener.serve(_with_query(_VALIDATORS, {"request_id": ["never-read"]}))
    row = _only_row(provider)
    assert (row["status"], row["method"], row["request_id"]) == ("down", "*", None)
```

- [ ] **Step 2: Write the socket tests**

In `tests/test_simulator_rest.py`, change the last two lines of the header docstring. Old:

```
  History tracking            — REST requests show up in /history with the
                               X-Request-Id header correlated when present.
```

New:

```
  History tracking            — REST requests show up in /history under the
                               request id of the query parameter
                               ``request_id`` or of the X-Request-Id header.
```

Add one import above `import json`:

```python
import http.client
import json
```

In the class `TestRestHistory`, add these five tests after `test_sim_side_request_id_when_header_missing` and before `test_404_recorded_with_method_label`:

```python
    def test_query_request_id_selects_the_rows_of_one_request(self, sim):
        """The query parameter ``request_id`` is the id that passes the
        smart-router. One request that reached two providers gives two rows
        under its id, and no row of another request."""
        path = "/cosmos/bank/v1beta1/balances/lava1probe"
        _get(_REST_URLS["1"] + path + "?request_id=mag3800-rest-a")
        _get(_REST_URLS["2"] + path + "?request_id=mag3800-rest-a")
        _get(_REST_URLS["1"] + path + "?request_id=mag3800-rest-b")
        _get(_REST_URLS["1"] + path)
        _, hist, _ = _get(_ctrl(sim, "/history?request_id=mag3800-rest-a&pool=lava-sim-rest"))
        assert hist["count"] == 2
        assert sorted(e["pid"] for e in hist["history"]) == ["1", "2"]
        assert {e["request_id"] for e in hist["history"]} == {"mag3800-rest-a"}
        assert {e["method"] for e in hist["history"]} == {"GET /cosmos/bank/v1beta1/balances/{address}"}

    def test_query_request_id_does_not_change_the_reply(self, sim):
        """The parameter is for the history row only. The reply is the reply
        of the same request with no id, and the page cursor still arrives."""
        url = _REST_URLS["1"] + "/cosmos/staking/v1beta1/validators"
        _, plain, _ = _get(url + "?pagination.key=CURSOR1")
        status, with_id, _ = _get(url + "?request_id=mag3800-rest-c&pagination.key=CURSOR1")
        assert status == 200
        assert with_id == plain
        assert with_id["pagination"]["inbound_key"] == "CURSOR1"

    def test_query_request_id_wins_over_the_header_through_a_socket(self, sim):
        _get(
            _REST_URLS["1"] + "/cosmos/staking/v1beta1/validators?request_id=mag3800-rest-d",
            headers={"X-Request-Id": "from-header"},
        )
        _, by_query, _ = _get(_ctrl(sim, "/history?request_id=mag3800-rest-d&pool=lava-sim-rest"))
        _, by_header, _ = _get(_ctrl(sim, "/history?request_id=from-header&pool=lava-sim-rest"))
        assert by_query["count"] == 1
        assert by_header["count"] == 0

    def test_lower_case_header_is_read_through_a_socket(self, sim):
        """urllib sends every header name in title case, so this test uses
        http.client, which sends the name as it is written."""
        conn = http.client.HTTPConnection("127.0.0.1", port_of("lava-sim-rest", "1", "rest"), timeout=5)
        try:
            conn.request("GET", "/cosmos/staking/v1beta1/validators", headers={"x-request-id": "lower-case-9"})
            assert conn.getresponse().status == 200
        finally:
            conn.close()
        _, hist, _ = _get(_ctrl(sim, "/history?request_id=lower-case-9&pool=lava-sim-rest"))
        assert hist["count"] == 1

    def test_empty_query_request_id_falls_back_to_the_counter(self, sim):
        _get(_REST_URLS["1"] + "/cosmos/staking/v1beta1/validators?request_id=")
        _, hist, _ = _get(_ctrl(sim, "/history?pool=lava-sim-rest&pid=1"))
        assert isinstance(hist["history"][-1]["request_id"], int)
```

- [ ] **Step 3: Run the tests to verify that they fail**

Run: `python -m pytest tests/test_listener_rest.py -q -p no:cacheprovider`

Expected: `15 failed, 28 passed`. The 15 that fail: the four query tests (`test_request_id_comes_from_the_query_parameter`, `test_the_query_parameter_wins_over_the_header`, `test_the_first_value_counts_when_the_parameter_repeats`, `test_a_query_value_that_is_not_in_a_list_is_read_too`), three cases of `test_the_header_is_read_in_any_letter_case` (`x-request-id`, `X-REQUEST-ID`, `X-request-id`), `test_a_plain_number_from_the_caller_is_kept_as_text`, the five cases of `test_every_verb_that_reaches_the_listener_records_the_query_id`, `test_an_unknown_path_keeps_the_id_on_its_not_found_row` and `test_a_fault_row_keeps_the_id`. Each one fails because the row holds a counter number, or the value of the header, in place of the id.

Five new cases pass before the change, because they pin a rule that holds today: the header in the letter case `X-Request-Id`, the empty query value with a header, the empty header value, the counter, and the provider-wide `down` row.

Run: `python -m pytest "tests/test_simulator_rest.py::TestRestHistory" -q -p no:cacheprovider`

Expected: `3 failed, 8 passed`. The three that fail: `test_query_request_id_selects_the_rows_of_one_request` (`assert 0 == 2`), `test_query_request_id_wins_over_the_header_through_a_socket` (`assert 0 == 1`) and `test_lower_case_header_is_read_through_a_socket` (`assert 0 == 1`). Two new tests pass before the change, because they pin a rule that holds today: `test_query_request_id_does_not_change_the_reply` and `test_empty_query_request_id_falls_back_to_the_counter`.

- [ ] **Step 4: Write the implementation**

In `provider_simulator/listeners/rest.py`, replace the function `_next_request_id` (lines 56-62) with this block. The function keeps its body; only its docstring changes, and the helper is new:

```python
def _next_request_id() -> int:
    """Sim-side monotonic id used when the caller sends no request id, so every
    REST call still gets a stable /history correlation."""
    global _ID
    with _ID_LOCK:
        _ID += 1
        return _ID


# Where a REST caller puts the request id. The query parameter comes first: the
# smart-router passes a query string on to the provider, and it passes a header
# only when the chain spec declares that header.
_REQUEST_ID_PARAM = "request_id"
_REQUEST_ID_HEADER = "x-request-id"


def _caller_request_id(request: RawRequest) -> str | None:
    """The request id that the caller chose, or None when it sent none.

    The query parameter ``request_id`` wins over the header ``X-Request-Id``.
    The header name is matched in any letter case, because a header name has no
    letter case in HTTP. An empty value is no id.
    """
    value = (request.query or {}).get(_REQUEST_ID_PARAM)
    if isinstance(value, (list, tuple)):
        # parse_qs puts each value in a list. The first value counts.
        value = value[0] if value else None
    if value:
        return str(value)
    for name, header_value in (request.headers or {}).items():
        if name.lower() == _REQUEST_ID_HEADER and header_value:
            return str(header_value)
    return None
```

In `RestListener.parse_request`, replace the line that sets `req_id` (line 90). Old:

```python
        req_id = request.headers.get("X-Request-Id") or _next_request_id()
```

New:

```python
        req_id = _caller_request_id(request) or _next_request_id()
```

- [ ] **Step 5: Run the tests to verify that they pass**

Run: `python -m pytest tests/test_listener_rest.py tests/test_simulator_rest.py -q -p no:cacheprovider`

Expected: `133 passed`.

- [ ] **Step 6: Commit**

```bash
git add provider_simulator/listeners/rest.py tests/test_listener_rest.py tests/test_simulator_rest.py
git commit -m "feat(rest): read the request id from the query parameter request_id"
```

---

### Task 2: The Lava chain gives the content of AllBalances

**Files:**
- Modify: `provider_simulator/chains/lava.py:314-324`
- Test: `tests/test_chains_lava.py` (after `test_grpc_per_method_result_override`, line 264)

**Interfaces:**
- Consumes: `REST_METHOD_DEFAULTS[("GET", "/cosmos/bank/v1beta1/balances/{address}")]` of `stubs_rest.py`, a dict with the key `balances`.
- Produces: `LavaChain.build_success({"method": "AllBalances"}, scenario, quirks, "grpc")` returns `(200, {"grpc_method": "AllBalances", "balances": [{"denom": "ulava", "amount": "1000000"}]})`. Task 3 and Task 4 read the key `balances`.

- [ ] **Step 1: Write the tests**

In `tests/test_chains_lava.py`, add these three tests after `test_grpc_per_method_result_override`:

```python
def test_grpc_all_balances_has_the_balances_of_the_rest_route():
    st, body = _chain().build_success({"method": "AllBalances"}, _sc(), {}, "grpc")
    _, rest_body = _chain().build_success(_rest(_BALANCES, path_params={"address": "lava1abc"}), _sc(), {}, "rest")
    assert st == 200
    assert body["grpc_method"] == "AllBalances"
    assert body["balances"] == [{"denom": "ulava", "amount": "1000000"}]
    assert body["balances"] == rest_body["balances"]


def test_grpc_all_balances_reply_does_not_share_the_stub():
    first = _chain().build_success({"method": "AllBalances"}, _sc(), {}, "grpc")[1]
    first["balances"].append({"denom": "leak", "amount": "1"})
    second = _chain().build_success({"method": "AllBalances"}, _sc(), {}, "grpc")[1]
    assert second["balances"] == [{"denom": "ulava", "amount": "1000000"}]


def test_grpc_all_balances_per_method_result_override():
    st, body = _chain().build_success(
        {"method": "AllBalances"},
        _sc(responses={"AllBalances": {"result": {"balances": []}}}),
        {},
        "grpc",
    )
    assert body["result"] == {"balances": []}
```

- [ ] **Step 2: Run the tests to verify that they fail**

Run: `python -m pytest tests/test_chains_lava.py -q -p no:cacheprovider`

Expected: `2 failed, 37 passed`. The two that fail are `test_grpc_all_balances_has_the_balances_of_the_rest_route` and `test_grpc_all_balances_reply_does_not_share_the_stub`, each with `KeyError: 'balances'`. The override test passes before the change: the override rule is the same for every method.

- [ ] **Step 3: Write the implementation**

In `provider_simulator/chains/lava.py`, replace the comment above `_build_grpc` and the first lines of the function, through the `result` override. Old:

```python
    # message. request = {method}. Only the two unary methods the router uses
    # are covered (GetLatestBlock / GetNodeInfo). Per-method `responses` result
    # overrides win. gRPC faults (errors, corruption) are the listener's job.
    def _build_grpc(self, request: dict, scenario: dict) -> tuple[int, dict]:
        method = request.get("method", "unknown")
        responses = scenario.get("responses") or {}
        method_cfg = responses.get(method) or responses.get("default", {})
        if isinstance(method_cfg, dict) and "result" in method_cfg:
            return 200, {"grpc_method": method, "result": method_cfg["result"]}
```

New:

```python
    # message. request = {method}. Three unary methods are covered: the two
    # that the router uses for its own polls (GetLatestBlock / GetNodeInfo) and
    # AllBalances, which carries a request id. Per-method `responses` result
    # overrides win. gRPC faults (errors, corruption) are the listener's job.
    def _build_grpc(self, request: dict, scenario: dict) -> tuple[int, dict]:
        method = request.get("method", "unknown")
        responses = scenario.get("responses") or {}
        method_cfg = responses.get(method) or responses.get("default", {})
        if isinstance(method_cfg, dict) and "result" in method_cfg:
            return 200, {"grpc_method": method, "result": method_cfg["result"]}

        if method == "AllBalances":
            # The balances of the REST route for the same query. One stub holds
            # the coin, so the two interfaces cannot give different balances.
            rest_stub = REST_METHOD_DEFAULTS[("GET", "/cosmos/bank/v1beta1/balances/{address}")]
            return 200, {"grpc_method": method, "balances": deepcopy(rest_stub["balances"])}
```

`deepcopy` and `REST_METHOD_DEFAULTS` are imported at the top of the file already.

- [ ] **Step 4: Run the tests to verify that they pass**

Run: `python -m pytest tests/test_chains_lava.py -q -p no:cacheprovider`

Expected: `39 passed`.

- [ ] **Step 5: Commit**

```bash
git add provider_simulator/chains/lava.py tests/test_chains_lava.py
git commit -m "feat(lava): give AllBalances the content of the REST balances route"
```

---

### Task 3: The gRPC listener records the request id

**Files:**
- Modify: `provider_simulator/listeners/grpc.py:1-14` (the module docstring) and `:64-93` (the code between `_status_name` and the `down` branch of `plan`)
- Test: `tests/test_listener_grpc.py` (the import block at lines 4-6, and the end of the file)

**Interfaces:**
- Consumes: the content of `AllBalances` from Task 2. The request messages of the compiled stubs: `cosmos.bank.v1beta1.query_pb2.QueryAllBalancesRequest`, which has the text field `address`.
- Produces:
  - `SERVED_METHODS: tuple[tuple[str, str, str | None], ...]`, one row for each served method: the full name of the service, the bare name of the method, the request-id field or `None`.
  - `request_id_fields(served=SERVED_METHODS) -> dict[str, str | None]`. It raises `ValueError` when two rows have the same method name.
  - `GrpcListener.plan(method: str, lava_headers: dict | None = None, request: object | None = None) -> GrpcPlan`. Task 4 calls it with the request message as the third argument.

- [ ] **Step 1: Write the tests**

In `tests/test_listener_grpc.py`, replace the import block (lines 4-6). Old:

```python
from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Pool
from provider_simulator.listeners.grpc import GrpcListener
```

New:

```python
import pytest

# Splice cosmos_pb2 onto sys.path so the generated stubs resolve. It must run
# before the ``from cosmos...`` imports below, so isort must not reorder them.
import cosmos_pb2  # noqa: F401  isort: split

from cosmos.bank.v1beta1 import query_pb2 as bank_query_pb2  # isort: skip
from cosmos.base.tendermint.v1beta1 import query_pb2  # isort: skip

from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Pool
from provider_simulator.listeners.grpc import GrpcListener, request_id_fields
```

Add this block at the end of the file:

```python
# --- The request id ---------------------------------------------------------
# gRPC has no id of its own. A served method can name one field of its request
# message as the request id. For AllBalances that field is ``address``.


def _all_balances(address=""):
    return bank_query_pb2.QueryAllBalancesRequest(address=address)


def test_all_balances_row_holds_the_address_as_the_request_id():
    listener, provider = _listener()
    plan = listener.plan("AllBalances", request=_all_balances("lava1-probe-a"))
    assert plan.action == "respond"
    assert plan.grpc_method == "AllBalances"
    assert plan.data["balances"] == [{"denom": "ulava", "amount": "1000000"}]
    hist = provider.log.get_history()[0]
    assert hist["method"] == "AllBalances"
    assert hist["status"] == "success"
    assert hist["request_id"] == "lava1-probe-a"


def test_get_latest_block_row_holds_no_request_id():
    listener, provider = _listener()
    listener.plan("GetLatestBlock", request=query_pb2.GetLatestBlockRequest())
    assert provider.log.get_history()[0]["request_id"] is None


def test_an_empty_address_is_no_request_id():
    listener, provider = _listener()
    listener.plan("AllBalances", request=_all_balances(""))
    assert provider.log.get_history()[0]["request_id"] is None


def test_a_call_with_no_request_message_has_no_request_id():
    # plan() keeps working for a caller that passes the method name only.
    listener, provider = _listener()
    listener.plan("AllBalances")
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
    plan = listener.plan("AllBalances", request=_all_balances("lava1-probe-f"))
    assert plan.action == "abort"
    hist = provider.log.get_history()[0]
    assert (hist["status"], hist["request_id"]) == (status, "lava1-probe-f")


def test_a_provider_wide_down_row_has_no_request_id():
    # A dead node does not read the request. The row keeps the method, as every
    # gRPC row does, and it has no request id, as on JSON-RPC, REST and
    # Tendermint RPC.
    listener, provider = _listener()
    _upd(listener, {"mode": "down"})
    listener.plan("AllBalances", request=_all_balances("never-read"))
    hist = provider.log.get_history()[0]
    assert (hist["status"], hist["method"], hist["request_id"]) == ("down", "AllBalances", None)


def test_the_served_methods_and_their_request_id_fields():
    assert request_id_fields() == {"GetLatestBlock": None, "GetNodeInfo": None, "AllBalances": "address"}


def test_two_served_methods_of_one_name_are_refused():
    # ``Params`` is a method of three compiled services. The bare name is the
    # key of a history row and of a ``responses`` override, so it must be unique.
    served = (
        ("cosmos.auth.v1beta1.Query", "Params", None),
        ("cosmos.bank.v1beta1.Query", "Params", None),
    )
    with pytest.raises(ValueError, match="cosmos.auth.v1beta1.Query/Params.*cosmos.bank.v1beta1.Query/Params"):
        request_id_fields(served)
```

- [ ] **Step 2: Run the tests to verify that they fail**

Run: `python -m pytest tests/test_listener_grpc.py -q -p no:cacheprovider`

Expected: the file is not collected. The output has `ImportError: cannot import name 'request_id_fields' from 'provider_simulator.listeners.grpc'` and ends with `1 error`.

- [ ] **Step 3: Write the implementation**

In `provider_simulator/listeners/grpc.py`, change the module docstring in two places. In the first paragraph, old:

```
message, not an HTTP body. So this listener exposes ``plan(method, lava_headers)``
— a pure decision that reuses the shared fault policy and LavaChain's success
DATA and returns a GrpcPlan the async servicer glue performs (abort with a
status, or build + return the proto). That glue (protobuf + abort) lands with the
server cut-over; keeping the decision here makes it unit-testable without a
running gRPC server.

gRPC-specific rules preserved from the flat handler:
- The RPC method is always known, so even a ``down`` call records that method
  (not ``"*"`` like the pre-body-parse HTTP down).
```

New:

```
message, not an HTTP body. So this listener exposes
``plan(method, lava_headers, request)`` — a pure decision that reuses the shared
fault policy and LavaChain's success DATA and returns a GrpcPlan the async
servicer glue performs (abort with a status, or build + return the proto). That
glue (protobuf + abort) lands with the server cut-over; keeping the decision here
makes it unit-testable without a running gRPC server.

gRPC-specific rules preserved from the flat handler:
- The RPC method is always known, so even a ``down`` call records that method
  (not ``"*"`` like the pre-body-parse HTTP down).
- A served method can name one field of its request message as the request id
  (``SERVED_METHODS``). For AllBalances that field is ``address``. A method that
  names no field records no request id, and so does a provider-wide ``down``
  row: a dead node does not read the request.
```

Then replace the code from the class statement `class GrpcListener:` through the `down` branch of `plan`. Old:

```python
class GrpcListener:
    def __init__(self, provider: Provider, endpoint: Endpoint) -> None:
        self.provider = provider
        self.endpoint = endpoint

    def plan(self, method: str, lava_headers: dict | None = None) -> GrpcPlan:
        entry = self.provider.log.record_arrival(
            self.endpoint.interface,
            self.endpoint.transport,
            self.endpoint.port,
            lava_headers=lava_headers or {},
        )
        scenario = self.provider.scenario.snapshot()
        verdict = fault_policy.decide(scenario, self.endpoint, self.provider)
        latency = scenario.get("latency_ms", 0)

        def _finalize(status: str, latency_ms: int) -> None:
            self.provider.log.finalize(entry, method=method, status=status, latency_ms=latency_ms)

        # ── Provider-wide fault verdicts → status aborts ──
        if verdict.kind == "down":
            _finalize("down", 0)
```

New:

```python
# The gRPC methods that this simulator serves. Each row holds the full name of
# the service, the bare name of the method, and the field of the request message
# that carries the request id. None means that the method has no such field.
#
# The bare name is the key of a history row and of a ``responses`` override, so
# two served methods must not share one. ``Params`` is a method of three
# compiled services, so a new row can break that rule.
SERVED_METHODS: tuple[tuple[str, str, str | None], ...] = (
    ("cosmos.base.tendermint.v1beta1.Service", "GetLatestBlock", None),
    ("cosmos.base.tendermint.v1beta1.Service", "GetNodeInfo", None),
    ("cosmos.bank.v1beta1.Query", "AllBalances", "address"),
)


def request_id_fields(
    served: tuple[tuple[str, str, str | None], ...] = SERVED_METHODS,
) -> dict[str, str | None]:
    """Map the bare name of each served method to its request-id field.

    Raises ValueError when two served methods have the same bare name. This
    module calls it at import, so the simulator does not start with such a
    table.
    """
    fields: dict[str, str | None] = {}
    services: dict[str, str] = {}
    for service, method, id_field in served:
        if method in fields:
            raise ValueError(
                f"two served gRPC methods have the name {method!r}: {services[method]}/{method} and "
                f"{service}/{method}. The bare method name is the key of a history row and of a "
                "responses override, so it must be unique."
            )
        fields[method] = id_field
        services[method] = service
    return fields


_REQUEST_ID_FIELD = request_id_fields()


def _request_id(method: str, request: object | None) -> str | None:
    """The request id of one call: the value of the field that the method
    names. None when the method names no field, when the caller passed no
    request message, or when the value is empty."""
    id_field = _REQUEST_ID_FIELD.get(method)
    if id_field is None or request is None:
        return None
    value = getattr(request, id_field, None)
    return str(value) if value else None


class GrpcListener:
    def __init__(self, provider: Provider, endpoint: Endpoint) -> None:
        self.provider = provider
        self.endpoint = endpoint

    def plan(self, method: str, lava_headers: dict | None = None, request: object | None = None) -> GrpcPlan:
        entry = self.provider.log.record_arrival(
            self.endpoint.interface,
            self.endpoint.transport,
            self.endpoint.port,
            lava_headers=lava_headers or {},
        )
        scenario = self.provider.scenario.snapshot()
        verdict = fault_policy.decide(scenario, self.endpoint, self.provider)
        latency = scenario.get("latency_ms", 0)
        request_id = _request_id(method, request)

        def _finalize(status: str, latency_ms: int, with_request_id: bool = True) -> None:
            self.provider.log.finalize(
                entry,
                method=method,
                status=status,
                latency_ms=latency_ms,
                request_id=request_id if with_request_id else None,
            )

        # ── Provider-wide fault verdicts → status aborts ──
        if verdict.kind == "down":
            # A dead node does not read the request, so its row has no request
            # id. JSON-RPC, REST and Tendermint RPC have the same rule.
            _finalize("down", 0, with_request_id=False)
```

The rest of `plan` does not change. Each other call of `_finalize` keeps its two arguments, so each other row gets the request id.

- [ ] **Step 4: Run the tests to verify that they pass**

Run: `python -m pytest tests/test_listener_grpc.py -q -p no:cacheprovider`

Expected: `28 passed`.

- [ ] **Step 5: Commit**

```bash
git add provider_simulator/listeners/grpc.py tests/test_listener_grpc.py
git commit -m "feat(grpc): record the request id that a served method names"
```

---

### Task 4: The gRPC adapter serves AllBalances

**Files:**
- Modify: `server.py:1121-1122` (the stub imports), `:1148-1169` (the reply builders and the servicer), `:1195-1208` (the bank servicer and the server set-up). All of them are in the function `_run_grpc_in_thread`.
- Test: `tests/test_simulator_grpc.py` (the header at lines 18-20, the import block at lines 28-43, the helpers after line 157, and new classes after `TestGrpcHistoryTracking`, line 551)

**Interfaces:**
- Consumes: `GrpcListener.plan(method, lava_headers, request)` from Task 3. The plan data `{"grpc_method": "AllBalances", "balances": [...]}` from Task 2.
- Produces: the gRPC method `cosmos.bank.v1beta1.Query/AllBalances` on each gRPC endpoint of the simulator. Nothing in a later task uses a name of this task.

- [ ] **Step 1: Write the tests**

In `tests/test_simulator_grpc.py`, add three lines to the header docstring after the entry "History tracking". Old:

```
  History tracking            — gRPC requests show up in /history exactly
                                like ETH/BTC ones, with the gRPC method name
                                preserved (no JSON-RPC id).
```

New:

```
  History tracking            — gRPC requests show up in /history exactly
                                like ETH/BTC ones, with the gRPC method name
                                preserved (no JSON-RPC id).
  Request id                  — AllBalances records its ``address`` as the
                                request id of its history row.
  Reflection                  — a served service is found by its symbol.
```

Replace the import block. Old:

```python
import asyncio
import json
import time
import urllib.error
import urllib.request

import grpc
import pytest

# Splice cosmos_pb2 onto sys.path so the generated stubs resolve. Must run
# before the `from cosmos...` import below — isort must not reorder these
# two (see cosmos_pb2/__init__.py's own docstring for why import order here
# matters).
import cosmos_pb2  # noqa: F401  isort: split

from cosmos.base.tendermint.v1beta1 import query_pb2, query_pb2_grpc  # isort: skip
```

New:

```python
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import grpc
import pytest
from grpc_reflection.v1alpha import reflection_pb2, reflection_pb2_grpc

# Splice cosmos_pb2 onto sys.path so the generated stubs resolve. Must run
# before the `from cosmos...` import below — isort must not reorder these
# two (see cosmos_pb2/__init__.py's own docstring for why import order here
# matters).
import cosmos_pb2  # noqa: F401  isort: split

from cosmos.bank.v1beta1 import query_pb2 as bank_query_pb2  # isort: skip
from cosmos.bank.v1beta1 import query_pb2_grpc as bank_query_pb2_grpc  # isort: skip
from cosmos.base.tendermint.v1beta1 import query_pb2, query_pb2_grpc  # isort: skip
```

Add these helpers after the function `_call_get_node_info`:

```python
def _call_all_balances(address: str, account: str, timeout: float = 5.0) -> bank_query_pb2.QueryAllBalancesResponse:
    """Open an insecure channel, call AllBalances for ``account``, return the response."""

    async def _do():
        channel = grpc.aio.insecure_channel(address)
        try:
            stub = bank_query_pb2_grpc.QueryStub(channel)
            req = bank_query_pb2.QueryAllBalancesRequest(address=account)
            return await asyncio.wait_for(stub.AllBalances(req), timeout=timeout)
        finally:
            await channel.close()

    return asyncio.run(_do())


def _ask_reflection(address: str, request: reflection_pb2.ServerReflectionRequest, timeout: float = 5.0):
    """Send one request to the server reflection service, return its reply."""

    async def _do():
        channel = grpc.aio.insecure_channel(address)
        try:
            stub = reflection_pb2_grpc.ServerReflectionStub(channel)
            call = stub.ServerReflectionInfo(iter([request]))
            return await asyncio.wait_for(call.read(), timeout=timeout)
        finally:
            await channel.close()

    return asyncio.run(_do())


def _free_port() -> int:
    """A local TCP port that nothing listens on now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# One gRPC endpoint in a process of its own, and a reflection lookup for each
# symbol given on the command line. This script must NOT import a bank stub:
# the lookup must find the bank service because the SERVER loaded it.
_REFLECTION_PROBE = """
import asyncio
import sys
import threading
import time

import grpc
from grpc_reflection.v1alpha import reflection_pb2, reflection_pb2_grpc

import server
from provider_simulator.domain.endpoint import Endpoint
from provider_simulator.domain.provider import Pool
from provider_simulator.listeners.grpc import GrpcListener
from provider_simulator.port_gate import PortGate

port = int(sys.argv[1])
endpoint = Endpoint("grpc", "http2", port)
provider = Pool(name="lava-sim-grpc", chain="lava").add_provider("1", [endpoint])
gate = PortGate(provider, endpoint)
threading.Thread(
    target=server._run_grpc_in_thread,
    args=(GrpcListener(provider, endpoint), port, "127.0.0.1", gate),
    daemon=True,
).start()
deadline = time.monotonic() + 10
while not gate.accepts():
    if time.monotonic() > deadline:
        sys.exit("the gRPC endpoint did not open")
    time.sleep(0.05)


async def lookup(symbol):
    async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
        stub = reflection_pb2_grpc.ServerReflectionStub(channel)
        request = reflection_pb2.ServerReflectionRequest(file_containing_symbol=symbol)
        reply = await stub.ServerReflectionInfo(iter([request])).read()
        return reply.WhichOneof("message_response")


for symbol in sys.argv[2:]:
    print(symbol, asyncio.run(lookup(symbol)), flush=True)
"""
```

Add these two classes after the class `TestGrpcHistoryTracking`, before the banner comment "Cross-pool fault isolation":

```python
# ─────────────────────────────────────────────────────────────────────────────
# The request id — AllBalances carries it in the field ``address``
# ─────────────────────────────────────────────────────────────────────────────


class TestGrpcAllBalances:
    """The bank method that carries a request id. The caller chooses the
    ``address``, and the history row holds it as the request id."""

    def test_all_balances_returns_one_ulava_coin(self, sim):
        resp = _call_all_balances(_GRPC_ADDRS["1"], "lava1-grpc-a")
        assert [(coin.denom, coin.amount) for coin in resp.balances] == [("ulava", "1000000")]

    def test_address_selects_the_rows_of_one_request(self, sim):
        """One request that reached two providers gives two rows under its
        address, and no row of another request."""
        _call_all_balances(_GRPC_ADDRS["1"], "lava1-mag3800-a")
        _call_all_balances(_GRPC_ADDRS["2"], "lava1-mag3800-a")
        _call_all_balances(_GRPC_ADDRS["1"], "lava1-mag3800-b")
        _call_get_latest_block(_GRPC_ADDRS["1"])
        _, hist = _get(_ctrl(sim, "/history?request_id=lava1-mag3800-a&pool=lava-sim-grpc"))
        assert hist["count"] == 2
        assert sorted(e["pid"] for e in hist["history"]) == ["1", "2"]
        assert {e["method"] for e in hist["history"]} == {"AllBalances"}
        assert {e["request_id"] for e in hist["history"]} == {"lava1-mag3800-a"}

    def test_a_failed_call_keeps_the_address_on_its_row(self, sim):
        _set_grpc(sim, "1", mode="error", error_message="RESOURCE_EXHAUSTED")
        with pytest.raises(grpc.RpcError) as excinfo:
            _call_all_balances(_GRPC_ADDRS["1"], "lava1-mag3800-c")
        assert excinfo.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
        _, hist = _get(_ctrl(sim, "/history?request_id=lava1-mag3800-c&pool=lava-sim-grpc"))
        assert hist["count"] == 1
        assert hist["history"][0]["status"] == "error"

    def test_a_down_provider_row_has_no_request_id(self, sim):
        _set_grpc(sim, "1", mode="down")
        with pytest.raises(grpc.RpcError):
            _call_all_balances(_GRPC_ADDRS["1"], "lava1-mag3800-d")
        _, by_id = _get(_ctrl(sim, "/history?request_id=lava1-mag3800-d&pool=lava-sim-grpc"))
        _, by_status = _get(_ctrl(sim, "/history?pool=lava-sim-grpc&pid=1&status=down"))
        assert by_id["count"] == 0
        assert by_status["count"] == 1
        assert by_status["history"][0]["method"] == "AllBalances"

    def test_missing_field_corruption_clears_the_balances(self, sim):
        _set_grpc(sim, "1", corruption_mode="missing_field", missing_field="balances")
        resp = _call_all_balances(_GRPC_ADDRS["1"], "lava1-grpc-e")
        assert len(resp.balances) == 0

    def test_result_override_replaces_the_balances(self, sim):
        _set_grpc(sim, "1", responses={"AllBalances": {"result": {"balances": [{"denom": "uatom", "amount": "5"}]}}})
        resp = _call_all_balances(_GRPC_ADDRS["1"], "lava1-grpc-f")
        assert [(coin.denom, coin.amount) for coin in resp.balances] == [("uatom", "5")]

    def test_other_bank_methods_answer_unimplemented(self, sim):
        """The simulator serves one method of the bank service. Each other
        method keeps the generated default. A service that is not registered
        gives another text, "Method not found!", so the text is read too."""

        async def _do():
            channel = grpc.aio.insecure_channel(_GRPC_ADDRS["1"])
            try:
                stub = bank_query_pb2_grpc.QueryStub(channel)
                await asyncio.wait_for(stub.TotalSupply(bank_query_pb2.QueryTotalSupplyRequest()), timeout=5.0)
            finally:
                await channel.close()

        with pytest.raises(grpc.RpcError) as excinfo:
            asyncio.run(_do())
        assert excinfo.value.code() == grpc.StatusCode.UNIMPLEMENTED
        assert "Method not implemented!" in excinfo.value.details()


class TestGrpcReflection:
    """The smart-router reads the description of a gRPC method from the
    provider: it asks the provider's reflection for the SYMBOL of the service.
    The list of services is for ``grpcurl list`` only."""

    def test_reflection_lists_the_served_services(self, sim):
        reply = _ask_reflection(_GRPC_ADDRS["1"], reflection_pb2.ServerReflectionRequest(list_services=""))
        assert {service.name for service in reply.list_services_response.service} == {
            "cosmos.bank.v1beta1.Query",
            "cosmos.base.tendermint.v1beta1.Service",
            "grpc.reflection.v1alpha.ServerReflection",
        }

    def test_reflection_finds_the_bank_symbol_because_the_server_loads_it(self, sim):
        """This test module imports the bank stubs for its client, so a lookup
        in this process finds the symbol with any server. The lookup runs in a
        process of its own, where only the server can load the bank stubs.
        The staking service is the control: its stubs are compiled, the
        server does not load them, and the lookup fails."""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                _REFLECTION_PROBE,
                str(_free_port()),
                "cosmos.bank.v1beta1.Query",
                "cosmos.base.tendermint.v1beta1.Service",
                "cosmos.staking.v1beta1.Query",
            ],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [
            "cosmos.bank.v1beta1.Query file_descriptor_response",
            "cosmos.base.tendermint.v1beta1.Service file_descriptor_response",
            "cosmos.staking.v1beta1.Query error_response",
        ]
```

Do not edit `test_grpc_request_id_is_none`. Global constraint 8 names it.

- [ ] **Step 2: Run the tests to verify that they fail**

Run: `python -m pytest tests/test_simulator_grpc.py -q -p no:cacheprovider`

Expected: `9 failed, 46 passed`. The nine that fail are the seven tests of `TestGrpcAllBalances` and the two tests of `TestGrpcReflection`. An `AllBalances` call fails with the status `UNIMPLEMENTED` and the text "Method not found!", because the bank service is not registered. The list of services has two names and not three. In the reflection test the first line of the output is `cosmos.bank.v1beta1.Query error_response`.

- [ ] **Step 3: Write the implementation**

All four edits are in the function `_run_grpc_in_thread` of `server.py`.

Edit 1, the stub imports. Old:

```python
    from cosmos.base.tendermint.v1beta1 import query_pb2, query_pb2_grpc  # isort: skip
    from tendermint.types import block_pb2, types_pb2  # isort: skip
```

New:

```python
    from cosmos.bank.v1beta1 import query_pb2 as bank_query_pb2  # isort: skip
    from cosmos.bank.v1beta1 import query_pb2_grpc as bank_query_pb2_grpc  # isort: skip
    from cosmos.base.tendermint.v1beta1 import query_pb2, query_pb2_grpc  # isort: skip
    from tendermint.types import block_pb2, types_pb2  # isort: skip
```

Edit 2, the reply builder and the servicer. Old:

```python
        resp.application_version.version = merged.get("app_version", "sim-1.0")
        return resp

    class _Servicer(query_pb2_grpc.ServiceServicer):
        async def GetLatestBlock(self, request, context):
            return await self._perform("GetLatestBlock", context, build_latest_block)

        async def GetNodeInfo(self, request, context):
            return await self._perform("GetNodeInfo", context, build_node_info)

        async def _perform(self, method: str, context, build_fn):
            metadata = context.invocation_metadata() or []
            lava = {k: v for (k, v) in metadata if k.lower().startswith("lava-")}
            plan = grpc_listener.plan(method, lava)
```

New:

```python
        resp.application_version.version = merged.get("app_version", "sim-1.0")
        return resp

    def build_all_balances(data: dict):
        merged = _merged(data)
        resp = bank_query_pb2.QueryAllBalancesResponse()
        for coin in merged.get("balances", []):
            resp.balances.add(denom=coin["denom"], amount=coin["amount"])
        return resp

    class _Servicer(query_pb2_grpc.ServiceServicer):
        async def GetLatestBlock(self, request, context):
            return await self._perform("GetLatestBlock", context, build_latest_block, request)

        async def GetNodeInfo(self, request, context):
            return await self._perform("GetNodeInfo", context, build_node_info, request)

        async def _perform(self, method: str, context, build_fn, request=None):
            metadata = context.invocation_metadata() or []
            lava = {k: v for (k, v) in metadata if k.lower().startswith("lava-")}
            # The listener gets the request message: it knows which field of
            # which method carries the request id.
            plan = grpc_listener.plan(method, lava, request)
```

Edit 3 and edit 4, the bank servicer and the server set-up. Old:

```python
            if plan.latency_ms > 0:
                await asyncio.sleep(plan.latency_ms / 1000.0)
            return response

    bind = f"[::]:{port}" if host == "0.0.0.0" else f"{host}:{port}"

    def _new_server():
        server = grpc.aio.server()
        query_pb2_grpc.add_ServiceServicer_to_server(_Servicer(), server)
        # Server reflection lets grpcurl discover services without a proto
        # bundle — a dev/test convenience worth the negligible surface.
        service_names = (
            query_pb2.DESCRIPTOR.services_by_name["Service"].full_name,
            reflection.SERVICE_NAME,
        )
```

New:

```python
            if plan.latency_ms > 0:
                await asyncio.sleep(plan.latency_ms / 1000.0)
            return response

    class _BankServicer(bank_query_pb2_grpc.QueryServicer):
        """One method of the bank service: AllBalances, whose ``address`` is
        the request id of the call. Each other method keeps the generated
        default, which answers UNIMPLEMENTED."""

        _perform = _Servicer._perform

        async def AllBalances(self, request, context):
            return await self._perform("AllBalances", context, build_all_balances, request)

    bind = f"[::]:{port}" if host == "0.0.0.0" else f"{host}:{port}"

    def _new_server():
        server = grpc.aio.server()
        query_pb2_grpc.add_ServiceServicer_to_server(_Servicer(), server)
        bank_query_pb2_grpc.add_QueryServicer_to_server(_BankServicer(), server)
        # Server reflection lets grpcurl discover services without a proto
        # bundle — a dev/test convenience worth the negligible surface. The
        # smart-router does not read this list: it asks reflection for the
        # symbol of a service, which is found because the stubs are imported
        # above. The list is what ``grpcurl list`` prints.
        service_names = (
            query_pb2.DESCRIPTOR.services_by_name["Service"].full_name,
            bank_query_pb2.DESCRIPTOR.services_by_name["Query"].full_name,
            reflection.SERVICE_NAME,
        )
```

- [ ] **Step 4: Run the tests to verify that they pass**

Run: `python -m pytest tests/test_simulator_grpc.py -q -p no:cacheprovider`

Expected: `55 passed`.

Run: `python -m pytest tests/test_simulator_grpc_port_closed.py tests/test_fault_policy.py -q -p no:cacheprovider`

Expected: `77 passed`. These two files call `plan` and the gRPC serve loop, and this plan does not edit them.

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_simulator_grpc.py
git commit -m "feat(grpc): serve AllBalances of the bank service"
```

---

### Task 5: The glossary and the three user documents

**Files:**
- Modify: `CONTEXT.md` (a new section before "## Telemetry", line 276, and two entries after the entry "History", line 286)
- Modify: `docs/curl_reference.md:116`
- Modify: `docs/using_the_simulator.md` (after the code block that ends at line 142)
- Modify: `docs/using_grpc.md:38-58`

**Interfaces:**
- Consumes: the behaviour of Tasks 1 to 4.
- Produces: nothing that code uses.

Use the skill `mattpocock-skills:domain-modeling` for the glossary. The glossary holds no implementation detail: no file name and no line number.

- [ ] **Step 1: Add the five glossary entries**

In `CONTEXT.md`, add this section above the line `## Telemetry`:

```markdown
## The request flow

**Request flow**:
The fixed order of steps that answers one request, from the first fault check to
the history row.
_Avoid_: pipeline, handler chain, request path

**Listener**:
The request flow of one endpoint. An endpoint is the door a provider listens on;
a listener is what answers a request that comes through that door.
_Avoid_: handler, servicer, endpoint (the endpoint is the door, not the flow)

**Ability**:
Something that a test can make an interface do or read. The request id is an
ability.
_Avoid_: feature, capability, option

```

In the section "Telemetry", add these two entries after the entry "History" and before the entry "Stats":

```markdown
**History row**:
One call in the history of a provider.
_Avoid_: entry, record, log line

**Request id**:
The value that says which request a history row belongs to. The caller chooses
it, and each interface names the one place where the caller puts it.
_Avoid_: correlation id, trace id, call id

```

- [ ] **Step 2: Correct the filter row of `docs/curl_reference.md`**

Old, line 116:

```markdown
| `request_id=<id>` | int | Filter by the JSON-RPC `id` field echoed in the request |
```

New:

```markdown
| `request_id=<id>` | text | Filter by the request id of a call. The id is the JSON-RPC `id` of the body, the REST query parameter `request_id` (or the header `X-Request-Id`), or the `address` of a gRPC `AllBalances` call. The filter compares text, so `request_id=1` also matches a REST row that holds the counter value 1. |
```

- [ ] **Step 3: Add the rule to `docs/using_the_simulator.md`**

Add this block after the code block of the filters, which ends with the line `curl -s "$SIM_CONTROL_URL/history?last=120&lava_header_lava_stateful_api=true"` and its closing fence, and before the line that starts "For the full filter catalogue":

````markdown
### Read the rows of one request

A caller chooses a request id. `GET /history?request_id=<id>&pool=<pool>` then returns the rows of that one request: one row for each provider that received it.

| Interface | Where the caller puts the id |
|---|---|
| `jsonrpc` | The `id` of the body. |
| `tendermintrpc`, POST | The `id` of the body. |
| `rest` | The query parameter `request_id`. The smart-router passes it on to the provider. The header `X-Request-Id`, in any letter case, works only for a caller that talks to the simulator directly: the router passes a header only when the chain spec declares it. |
| `grpc` | The field `address` of `cosmos.bank.v1beta1.Query/AllBalances`. `GetLatestBlock` and `GetNodeInfo` have an empty request, so their rows have no request id. |

Three rules:

- On REST and on gRPC, the id must not be a plain number. A REST call with no id gets a counter value of the simulator (1, 2, 3 and so on), and so does a Tendermint RPC call in the URL form. The filter compares text. So the filter for `request_id=1` matches the caller id `1` and the counter value 1. On JSON-RPC and on a Tendermint RPC POST the id is the `id` of the body, and it can be a number: choose a value that no other caller sends.
- The row of a provider-wide `down` has no request id on a JSON-RPC, REST, Tendermint RPC or gRPC call. A dead node does not read the request. Count those calls with `GET /stats`. One case differs: the `down` row of a WebSocket subscribe frame keeps its method and its id.
- To prove that NO provider received a request, first send a control request with its own id and require one row or more for that id. That proves that the id travels. Then send the request under test and require zero rows for its id. This proof holds only while no provider of the pool is in the mode `down`: a `down` row has no request id, so a request that reached only a `down` provider also gives zero rows for its id. With a `down` provider in the pool, also require that no new `down` row appears: `GET /history?pool=<pool>&status=down`.

```bash
# REST: the id is in the query string
curl -s "http://localhost:18551/cosmos/bank/v1beta1/balances/lava1probe?request_id=mytest-9c1d"
curl -s "$SIM_CONTROL_URL/history?request_id=mytest-9c1d&pool=lava-sim-rest"

# gRPC: the id is the address of AllBalances
grpcurl -plaintext -d '{"address":"mytest-7f3a"}' localhost:18548 cosmos.bank.v1beta1.Query/AllBalances
curl -s "$SIM_CONTROL_URL/history?request_id=mytest-7f3a&pool=lava-sim-grpc"
```

````

- [ ] **Step 4: Add the new method to `docs/using_grpc.md`**

In the code block of the section "Quickstart commands", replace the output of command 1. Old:

```bash
# 1. Service catalogue — proves reflection is up
grpcurl -plaintext localhost:18548 list
# → cosmos.base.tendermint.v1beta1.Service
#   grpc.reflection.v1alpha.ServerReflection
```

New:

```bash
# 1. Service catalogue — proves reflection is up
grpcurl -plaintext localhost:18548 list
# → cosmos.bank.v1beta1.Query
#   cosmos.base.tendermint.v1beta1.Service
#   grpc.reflection.v1alpha.ServerReflection
```

In the same code block, add command 6 after command 5, which is the `GetNodeInfo` call:

```bash

# 6. Real call — AllBalances returns one coin (ulava, 1000000). The `address`
#    is the request id of the history row, so a test can read the rows of this
#    one call. The bank service lists more methods, and the simulator serves
#    this one only: each other method answers Unimplemented.
grpcurl -plaintext -d '{"address":"mytest-7f3a"}' localhost:18548 cosmos.bank.v1beta1.Query/AllBalances
curl -s "https://sim-control.<BASE_DOMAIN>/history?request_id=mytest-7f3a&pool=lava-sim-grpc"
```

- [ ] **Step 5: Verify the examples**

Start the simulator in one shell: `python -u run.py`. In a second shell, run the two `grpcurl` commands and the two REST lines of step 3, with `http://localhost:19000` for the control URL.

Expected: `grpcurl ... list` prints the three service names of step 4. The `AllBalances` call prints one balance with `"denom": "ulava"` and `"amount": "1000000"`. Each history read prints `"count": 1`, and its row holds the id that was sent. Stop the simulator.

- [ ] **Step 6: Commit**

```bash
git add CONTEXT.md docs/curl_reference.md docs/using_the_simulator.md docs/using_grpc.md
git commit -m "docs: the request id on each interface, and five glossary entries"
```

---

### Task 6: The whole suite and the three checks of CI

**Files:** none.

**Interfaces:**
- Consumes: the commits of Tasks 1 to 5.
- Produces: the proof that nothing else broke.

- [ ] **Step 1: Check that no other test run is active**

Run: `lsof -nP -iTCP:19000 -sTCP:LISTEN`

Expected: no output. If a line prints, another simulator or another suite uses the ports: wait for it.

- [ ] **Step 2: Run the whole suite**

Run: `python -m pytest tests -q -p no:cacheprovider`

Expected: `1601 passed`. That is the 1549 tests of `e80acbf` and the 52 tests of this plan: 20 in `tests/test_listener_rest.py`, 5 in `tests/test_simulator_rest.py`, 3 in `tests/test_chains_lava.py`, 15 in `tests/test_listener_grpc.py` and 9 in `tests/test_simulator_grpc.py`. No test fails and no test is skipped. If `main` moved after `e80acbf`, the first number is the count of the new `main`.

The four tests that guard the topology pass with no edit: `tests/test_domain_topology.py`, `tests/test_control_api_providers.py`, `tests/test_service_publishes_every_port.py` and `tests/test_values_sim_matches_topology.py`.

- [ ] **Step 3: Run the three checks with the versions of CI**

```bash
pip install 'black==26.5.1' 'ruff==0.15.20' 'mypy==2.2.0'
black --check .
ruff check .
mypy .
```

Expected: black prints "would be left unchanged" for every file. ruff prints `All checks passed!`. mypy prints `Success: no issues found`.

- [ ] **Step 4: If a check changed a file, commit it**

```bash
git status --short
```

Expected: no output. If black or ruff asked for a change, make it, run step 2 again, and commit the file by its name with the subject `style: format the request id change`.

---

### Task 7: The measurement on the local k3d cluster, before the merge

Section 10 of the design asks for it: one request through each router with the branch build, and a read of its row. Section 12 asks for more before the merge: every automation suite with the branch build. Task 8, step 3, runs those suites in CI.

**Files:** none in the repository. Save each output with the evidence of the work, outside the repository.

**Interfaces:**
- Consumes: the branch of Tasks 1 to 6.
- Produces: the outputs that the pull request body of Task 8 quotes.

**Every step that changes the cluster needs Victoria's go for that step.** Another session can use the cluster: ask before the first step. The skill `local-cluster` leads to the skill `deploy-k3d-cluster`, which owns the commands below, and the skill `debug-endpoint` owns the pairing reset. Every `kubectl` call names the kubeconfig file, the context and the namespace, because the shell of a session can point at another cluster. In the commands below, `<k3d-kubeconfig-file>` is the file that `k3d kubeconfig write smart-router-local` prints.

- [ ] **Step 1: Build the branch image with a name that `/version` can show**

```bash
docker build . -t provider-simulator:request-id --build-arg SIM_GIT_COMMIT=$(git rev-parse HEAD) --build-arg SIM_GIT_DESCRIBE=$(git describe --tags --always)-request-id
```

- [ ] **Step 2: Load the image and switch the simulator to it**

```bash
k3d image import provider-simulator:request-id -c smart-router-local
docker exec k3d-smart-router-local-server-0 ctr -n k8s.io images ls -q | grep provider-simulator
kubectl --kubeconfig <k3d-kubeconfig-file> --context k3d-smart-router-local -n smart-router set image deploy/provider-simulator provider-simulator=provider-simulator:request-id
kubectl --kubeconfig <k3d-kubeconfig-file> --context k3d-smart-router-local -n smart-router rollout status deploy/provider-simulator --timeout=180s
curl -s http://localhost:31000/version
```

Expected: the second command prints the tag `request-id`. `/version` prints a `git_describe` that ends with `-request-id`.

- [ ] **Step 3: Reset the pairing of every router**

A restart of the simulator leaves each router with every provider blocked. Send `POST /debug/reset-pairing` to the debug port of each running router. The file `tools/local-cluster/routers.yml` of the automation repository gives the port of each router in its field `debug_port`.

```bash
for port in <the debug_port of each running router>; do curl -s -o /dev/null -w "$port %{http_code}\n" -X POST "http://localhost:$port/debug/reset-pairing"; done
```

Expected: one line for each port, each with `200`. Then send one request through `lava-sim-rest-router` and one through `lava-sim-grpc-router`, and read that each one answers. A reset that returned 200 does not prove that a router serves.

- [ ] **Step 4: REST through the router**

```bash
curl -s -D - -o /dev/null "http://localhost:30003/cosmos/bank/v1beta1/balances/lava1plan?request_id=step1-rest-a"
curl -s "http://localhost:31000/history?pool=lava-sim-rest&request_id=step1-rest-a"
curl -s -D - -o /dev/null "http://localhost:30003/cosmos/bank/v1beta1/balances/lava1plan?request_id=step1-rest-b"
curl -s "http://localhost:31000/history?pool=lava-sim-rest&request_id=step1-rest-b"
```

Expected for each id: HTTP 200 with the header `Lava-Provider-Address`. The history read prints `"count": 1`. Its row holds the id, the method `GET /cosmos/bank/v1beta1/balances/{address}`, and the name of the provider that the header named.

Then the control, a relay with no id:

```bash
curl -s -o /dev/null "http://localhost:30003/cosmos/bank/v1beta1/balances/lava1plancontrol"
curl -s "http://localhost:31000/history?pool=lava-sim-rest&last=60"
```

Expected: the newest row of the route `GET /cosmos/bank/v1beta1/balances/{address}` holds a plain number, the counter of the simulator. If it holds another text, something between the router and the provider adds the header `x-request-id`, which the listener now reads in any letter case: stop and tell Victoria.

- [ ] **Step 5: gRPC through the router**

Save this client as `all_balances_probe.py` in a temporary folder outside the repository:

```python
"""One AllBalances call to a gRPC address. Usage: python all_balances_probe.py <host:port> <address>

Run it with the root of a provider-simulator checkout on PYTHONPATH: the
generated stubs are in that checkout.
"""

import sys

import grpc

import cosmos_pb2  # noqa: F401  splices the generated stubs onto sys.path

from cosmos.bank.v1beta1 import query_pb2, query_pb2_grpc  # isort: skip

target, address = sys.argv[1], sys.argv[2]
with grpc.insecure_channel(target) as channel:
    stub = query_pb2_grpc.QueryStub(channel)
    reply, call = stub.AllBalances.with_call(query_pb2.QueryAllBalancesRequest(address=address), timeout=10)
    print("reply:", [(coin.denom, coin.amount) for coin in reply.balances])
    print("headers:", dict(call.initial_metadata()))
```

Run it from the root of the worktree, two times:

```bash
PYTHONPATH=. python <the temporary folder>/all_balances_probe.py localhost:30004 step1-grpc-a
curl -s "http://localhost:31000/history?pool=lava-sim-grpc&request_id=step1-grpc-a"
PYTHONPATH=. python <the temporary folder>/all_balances_probe.py localhost:30004 step1-grpc-b
curl -s "http://localhost:31000/history?pool=lava-sim-grpc&request_id=step1-grpc-b"
```

Expected for each address: `reply: [('ulava', '1000000')]`. The headers hold `lava-user-request-type: cosmos.bank.v1beta1.Query/AllBalances`, with no `Default-` in front, and `lava-provider-address`. The history read prints `"count": 1`. Its row holds the address as the request id, the method `AllBalances`, and the name of the provider that the header named.

This client was run against a provider port of the simulator, with no router: it printed the reply and the row. It was not run through a router.

- [ ] **Step 6: Go back to the image of `main`**

```bash
kubectl --kubeconfig <k3d-kubeconfig-file> --context k3d-smart-router-local -n smart-router set image deploy/provider-simulator provider-simulator=ghcr.io/magma-devs/provider-simulator:main
kubectl --kubeconfig <k3d-kubeconfig-file> --context k3d-smart-router-local -n smart-router rollout status deploy/provider-simulator --timeout=180s
curl -s http://localhost:31000/version
```

Expected: `/version` prints the commit of `main`. Reset the pairing of every router again, as in step 3. The router `lava-sim-grpc-router` keeps the description of the bank service for the life of its pod, so it still sends `AllBalances` to a provider that does not serve it. A restart of that router pod removes that: ask Victoria. Then send one request through each of the two routers and read that each one answers.

---

### Task 8: The pull request

**Files:** none.

**Interfaces:**
- Consumes: the branch of Tasks 1 to 6 and the outputs of Task 7.
- Produces: the merged change, and the contract for the author of the tests of MAG-3800.

The push and the pull request need Victoria's go. The merge needs its own go. Use the skill `pr`.

- [ ] **Step 1: Open the pull request**

The title: `feat(history): a request id on REST and gRPC rows`. The body has `#Closes MAG-<number>` on its first line when Victoria named a story, or the command has `--label no-ticket`. The command has `--assignee`. The body has three parts: Why, What changed, How to verify. "How to verify" holds the two test commands of Task 6 and the outputs of Task 7, steps 4 and 5. The body holds no path of one machine.

- [ ] **Step 2: Read the checks**

Run: `gh pr checks <number>`

Expected: `lint`, `test` and "Suite must pass before anything is published" pass. `test` runs on Linux: it is the first Linux run of the socket tests of this plan. Open the log of a red check before you call it red.

- [ ] **Step 3: Run the automation suites in CI with the commit of the branch**

Section 12 of the design asks for every automation suite with the branch build before the merge, the cache suites and the RESP suites too. On a developer machine that run is long: on 2026-10-08 the session `worker` measured 120 minutes for 112 tests of one selection on the local k3d cluster. So this plan runs the suites in CI.

Four k3d workflows of the automation repository have a manual start and the input `provider_simulator_ref`: `nightly-sim-suite-k3d-by-feature.yml`, `cache-regression-k3d.yml`, `failover-regression-k3d.yml` and `weekly-slow-suite-k3d.yml`. The input takes a commit, a tag or a branch of the simulator. Give the commit id of the pushed branch: a branch name can move between two runs.

Each start is a write outside this machine, so each one needs Victoria's go. The skill `run-nightly` of the automation repository owns the start and the monitoring of such a run. Check with the skill `pick-suite` that the four workflows cover every suite. A suite that no workflow covers runs on the local cluster with the branch image of Task 7 (skill `local-cluster-testing`), and that run sets `CLUSTER_PROFILE=local`.

Expected: each test keeps the result that it has in the newest run of the same workflow on `main`.

- [ ] **Step 4: After the merge, give the contract to the author of the tests of MAG-3800**

| Interface | Send | Read |
|---|---|---|
| REST | Add `request_id=<a unique value>` to the query string. | `GET /history?request_id=<the value>&pool=lava-sim-rest` |
| gRPC | Call `cosmos.bank.v1beta1.Query/AllBalances` with a unique `address`. | `GET /history?request_id=<the address>&pool=lava-sim-grpc` |

Three notes go with it. The value is not a plain number. A test that requires zero rows first sends a control request and requires one row or more. The row of a provider-wide `down` has no request id, so the zero-rows proof holds only while no provider of the pool is in the mode `down`.

**Rollback:** revert this pull request while it is the newest one that merged. No other pull request of the design depends on it yet.

---

## After the review of the branch

Tasks 1 to 6 were executed on 2026-10-08 on the branch `request-id-on-rest-and-grpc`. One fresh reviewer then read the whole branch. It found no critical fault, one important fault and six minor points. These changes followed, and the branch holds them in two more commits:

1. **The table and the servicers are tied.** The reviewer found that `server.py` did not read `SERVED_METHODS`, so global constraint 6 held for the table only. A new function `check_servicers(servicers, served=None)` in `provider_simulator/listeners/grpc.py` compares the public methods that each servicer class defines with the rows of the table, and it raises `ValueError` when they differ. `_run_grpc_in_thread` calls it before it starts a server. Four tests: three in `tests/test_listener_grpc.py` (`test_servicers_that_match_the_table_are_accepted`, `test_a_servicer_method_with_no_row_is_refused`, `test_a_row_with_no_servicer_method_is_refused`) and one in `tests/test_simulator_grpc.py` (`TestGrpcServedMethods::test_the_adapter_refuses_to_start_when_a_served_method_has_no_row`). Each one failed before the change and passes after it.
2. **The reflection test is bounded.** The child script of `test_reflection_finds_the_bank_symbol_because_the_server_loads_it` waits at most 10 s for each lookup, and it ends with `os._exit(0)`, so it does not wait for the gRPC server on its daemon thread.
3. **Three document sentences are corrected.** Task 5, step 3, above holds the new text of two rules. In `docs/using_grpc.md` the overview now names the three served methods and the table `SERVED_METHODS`. Global constraint 7 and two comments name the interfaces in place of "every interface": the `down` row of a WebSocket subscribe frame keeps its method and its id today.
4. **Task 7, step 4, has a control read**: a relay with no id must record a counter.

The counts after these changes: `tests/test_listener_grpc.py` has 31 tests, `tests/test_simulator_grpc.py` has 56, and the whole suite gives `1605 passed`.

After the pull request was open, the adversary round of the skill `pr` ran on it: one more fresh helper, with only the pull request and the task to argue against the merge. It found no failing behaviour in the code. Two of its points were fixed on Victoria's order, in one more commit of the branch:

5. **The zero-rows proof has a limit.** A `down` row has no request id. So "zero rows for one id" proves that no provider received the request only while no provider of the pool is in the mode `down`. Task 5, step 3, and Task 8, step 4, above hold the new text.
6. **Three texts that the change made false are corrected.** The docstring of `test_grpc_request_id_is_none` said "every gRPC request"; its assertion is not edited, which is what global constraint 8 protects. The docstring of `test_x_request_id_correlates_into_history` said that the router passes the header on. A comment in `chains/lava.py` and the name of one test said "content" where only the balances are copied: the test is now `test_grpc_all_balances_has_the_balances_of_the_rest_route`. This replaces choice 3 of the section "Five choices" below.

One minor point is open for Victoria: a malformed `result` override for `AllBalances` (an amount that is a number, a `balances` that is no list, a coin with no `amount`) raises an error in the gRPC adapter after the history row says `success`. The two older reply builders do the same with a wrong type.

## Not in this plan

These are changes of the automation repository. They follow this pull request, because the CI of the automation repository checks out the default branch of the simulator.

1. The request helpers add the id on REST, and a client helper calls `AllBalances` on gRPC.
2. The parameter `request_id` of `get_history` is typed as a number today (`tests/simulator/sim_control.py:2161`). On REST and gRPC the id is text.
3. The `failover` skill has the rule "Assert on the reply, never on an absence in the history" (`references/what-the-simulator-can-tell-you.md:190-193`). It gets one exception: zero rows for ONE request id, after a control request.
4. The `chaintracker` skill says that the rows of lava REST, Tendermint RPC and gRPC have the request id "1 on every entry" (`references/what-the-simulator-can-tell-you.md:34-37`). Check that sentence against the code, and correct it.
5. Rule R45 of the automation repository can then cover REST and gRPC.

Two more things are outside this plan:

- The other pull requests of the design. Each one gets its own plan.
- The stale names in `docs/using_grpc.md` (`handlers_grpc.py`, `chain_family`). Section 17 of the design, item 7, lists them as not checked.

## Five choices of this plan that the design does not fix

Victoria can change each one before the work starts.

1. **The table `SERVED_METHODS` and the refusal of two methods of one name are in this pull request.** ADR-002, decision 6, states the refusal. Section 10 of the design does not list it in the code of step 1, and ADR-001 puts a method table into pull request 2b, where gRPC moves into `Listener.serve`. The smaller way: a map of one entry now, `{"AllBalances": "address"}`, and the refusal with the table of pull request 2b. This plan takes the first way, because ADR-002 is the accepted record of this step.
2. **`plan` gets the request message.** Section 10 of the design says so. The probe of 2026-10-07 gave `plan` the id itself. With the message, the rule "which field of which method" stays in the listeners package.
3. **The docstring of `test_grpc_request_id_is_none` stays.** It says "request_id=None for every gRPC request", and after this change that holds for `GetLatestBlock` and `GetNodeInfo` only. ADR-002 names this test as one that passes with no edit, so this plan does not touch it. One line of words can be corrected on her word.
4. **The reflection test runs in a process of its own.** In the test process the test module itself loads the bank stubs, so a lookup there cannot fail. The test starts one gRPC endpoint in a new process and asks for three symbols. The staking service is the control that fails.
5. **The automation suites run in CI, not on a developer machine.** Section 12 of the design says "on the k3d cluster with the branch build". A k3d workflow of CI and the local cluster both meet that. The local way took 120 minutes for 112 tests on 2026-10-08, and the sessions share the local cluster. The CI way starts four workflows, and each start needs Victoria's go (Task 8, step 3).

One fact that the handoff of the design session states in another way: a method of a registered service that the simulator does not serve answers `UNIMPLEMENTED` with the text `Unexpected <class 'NotImplementedError'>: Method not implemented!` (`grpcio` 1.81.1, `grpc.aio`). The test reads the status and the part "Method not implemented!".

## Spec coverage

| Requirement of the design | Task |
|---|---|
| Section 8.2: REST reads the query parameter, then the header in any letter case, then the counter; the first value; the five verbs; an empty value is no id | 1 |
| Section 8.2: one field and one filter | 1 and 4: the socket tests read the rows through the filter |
| Section 8.2: an id must not be a plain number | 1 (the test) and 5 (the documents) |
| Section 8.2: gRPC serves `AllBalances`; `address` is the id; the content of the REST route; `GetLatestBlock` and `GetNodeInfo` keep no id; the other bank methods answer `UNIMPLEMENTED`; reflection | 2, 3, 4 |
| Section 8.2 and ADR-002, decision 6: the bare method name is the key; two served methods of one name are refused | 3 |
| Section 8.2 and ADR-002, decision 5: the row of a provider-wide `down` has no request id | 1, 3, 4 |
| Section 8.2: a row gets its request id before the provider waits | 3: the `hang` case keeps the id |
| Section 9.1: the two new abilities. Section 9.2, row 1: the header in any letter case | 1, 3, 4 |
| Section 10, step 1, documents: `CONTEXT.md`, `docs/using_the_simulator.md`, `docs/using_grpc.md`, `docs/curl_reference.md` line 116. Section 15.1, Q15: five glossary entries | 5 |
| Section 12, the five test rows of pull request 1 | 1, 2, 3, 4 |
| Section 12, the four rules for every pull request | Global constraints 10 and 11; Task 6; Task 8, steps 2 and 3 |
| Section 10, step 1, "Before the merge" and "Done when" | 6, 7, 8 |
| Section 13: the simulator merges first; the contract for the tests | 8, and "Not in this plan" |
| Section 13.1: the topology does not change | Global constraint 12; Task 6, step 2 |
