# Pin Today's WebSocket Rows Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tests record what a WebSocket endpoint of the simulator does today in the places of `server.py` that decide a fault and write a history row outside `Listener.serve`: the upgrade request, a subscribe frame and an unsubscribe frame, and a pushed event. The next step, in the same pull request, can then move that code into `provider_simulator/listeners/` and show each reply and each row that changed. The design calls that step 3b.

**Architecture:** Tests only, at the end of one file that exists, `tests/test_simulator_ws.py`. They use the shared simulator of the test session, the control API over HTTP, and a plain socket that sends the upgrade request and the frames. No source file changes, and no test file is created.

**Tech Stack:** Python 3.12, pytest 9.1.1 with pytest-timeout 2.4.0, the module `socket` of the standard library, and the codec `provider_simulator/listeners/ws_protocol.py`. The checks of CI: black 26.5.1, ruff 0.15.20 and mypy 2.2.0.

**Spec:** `docs/superpowers/specs/2026-10-06-one-request-flow-design.md`, version 2.3, on the branch `docs/one-request-flow-design` (pull request 137 of `provider-simulator`): sections 4.2, 4.3, 4.4 (rows 2 to 5), 8.4, 9.2 (rows 3 to 6), 9.3 (differences 8 to 12), 10 ("Step 3", pull request 3a), 12, 12.1 (the list "3a, WebSocket") and 14.6. The decision record `docs/adr/001-one-request-flow.md`. This plan is pull request 3a of that design: "tests that record today's WebSocket rows". The plan of pull request 2c, `docs/superpowers/plans/2026-10-08-four-differences-become-uniform.md`, is the form of this plan.

## The state of this plan

Written on 2026-10-09, after pull request 2c merged as the commit `8ed08aa` of `main`. Victoria gave her go for the worktree, the branch and this plan file on 2026-10-09. Reviews then changed the tests. The code blocks and the counts of this plan now hold those changes, as the last section says.

The state on 2026-10-10: the branch is pushed, and pull request 144 is open. The same pull request also holds a second step, the move of the WebSocket code, with its own plan file `2026-10-10-websocket-frames-use-listener-serve.md`. This plan describes the commits up to `5657f71`. In this plan, "this pull request" and "tests only" mean those commits.

| | |
|---|---|
| Worktree | `.claude/worktrees/pin-todays-websocket-rows` of the simulator checkout |
| Branch | `pin-todays-websocket-rows`, made from `main` at `8ed08aa`. Pushed. Its first commit is this plan. |

**What RAN for this plan on 2026-10-09.** The runs used a copy of `main` at `8ed08aa` in a temporary folder, on a Mac (Darwin 25.3, Python 3.12.12, pytest 9.1.1, pytest-timeout 2.4.0). They used the code blocks of that day, before the reviews:

- On 2026-10-09, the file `tests/test_simulator_ws.py` of `main`: `59 passed`. With the code of Tasks 1 to 4 of that day: `167 passed`, three times in a row (40.64 s, 40.73 s and 40.54 s). 30 seconds of each run are the one slow test (choice 2).
- On 2026-10-09, the whole suite with the code of Tasks 1 to 4: `1971 passed in 237.01s (0:03:57)`, from 23:03:00 to 23:06:57 IDT, with no test skipped. `main` has 1863 tests: READ from the log of the CI run 37976966668 of `main` at `8ed08aa`, job `test`, `1863 passed in 205.71s`.
- On 2026-10-09, `black --check`, `ruff check` and `mypy` on the test file of that day: clean, with black 26.5.1, ruff 0.15.22 and mypy 2.3.0. CI has ruff 0.15.20 and mypy 2.2.0. Task 6 runs the versions of CI.
- On 2026-10-09, 109 breaks of the source, one at a time, each one with the new tests of that day. Each break failed one test or more, and each of the 108 new cases of that day failed under one break or more. The section "The breaks" has each run.
- On 2026-10-09, the file ran as it was after Task 1, after Task 2 and after Task 3. `black --check` and `ruff check` were clean for each of the three states. Pytest gave `90 passed`, `138 passed` and `156 passed`.
- On 2026-10-09, on Linux: the file ran one time in a container on the Mac: Linux 6.12.68 (arm64), Python 3.12.13, `167 passed in 41.16s`. Task 6, Step 4 has the command.

**What did not run on 2026-10-09.** The three checks with the versions of CI. The whole suite on Linux: the job `test` of CI ran it on 2026-10-10. The commands of Tasks 1 to 4 in the worktree: they ran on the copy, which holds the same files.

**What RAN on 2026-10-10.** These runs used the final test file, which has 114 new cases:

- On a Mac, the test file ran two times in a row: `173 passed` (40.65 s and 40.61 s). That is the 59 tests of `main` and the 114 new cases. The file had the checksum `19233b2b7df87b97b4000b75085fe31a`, which is the file of the commit `dfe4b3b`. Victoria chose two runs in a row, and not three.
- On Linux, the job `test` of the pull request: `1977 passed`. That is the 1863 tests of `main` and the 114 new cases.
- All 118 breaks of the source ran, one at a time. They ran on the test file of the commit `dfe4b3b`, against the four source files of `main` at `8ed08aa`. The control run with no break gave `113 passed, 60 deselected`. Each break made one case or more fail, and each of the 114 new cases failed in one run or more. 109 of the 118 breaks are the runs of 2026-10-09 in the section "The breaks". The reviews added nine more: the last section of this plan names them.
- The commit `21a48df` then changed two docstrings of the test file and no code. So the file of the checkpoint (Task 4, Step 4) has another checksum than the file of the Mac runs and of the breaks.
- The counts after Tasks 1, 2 and 3 come from the case counts, and not from a run: `90 passed`, `139 passed` and `160 passed`.

A step that does not go as this plan says is a stop: tell Victoria, and do not work around it.

## Choices of this plan that the design does not fix

Victoria can change each one.

1. **Each new test goes into `tests/test_simulator_ws.py`.** Section 12 of the design names that file for each test of 3a. So this plan creates no test file. The file grows from 1397 lines to 2889.
2. **One test takes 30 seconds.** `test_a_hung_upgrade_ends_after_30_seconds_with_a_closed_connection_and_no_byte` waits for the close of a hung upgrade. The adapter holds a hung upgrade for 30 seconds, and no test of today reads what comes after the first second: `test_mode_hang_pre_handshake_sleeps_then_closes` stops after 1 second. Pull request 3b moves the code that performs the hang of an upgrade. The cost is 30 seconds for each run of the suite, which takes 206 seconds in CI today. The other way: leave the close after 30 seconds with no test. The plan of pull request 2a made the same choice for the gRPC hang.
3. **The new tests use a plain socket, and not the class `WsClient` of `tests/ws_client.py`.** `WsClient` sends no extra header, and a row must carry the lava headers of the upgrade request. It uses a random key, and this plan compares the 101 reply byte for byte with the sample key of RFC 6455. It reads frames only, and a refused upgrade and a dropped frame are raw bytes. The helpers are private functions of the test file. `tests/ws_client.py` does not change.
4. **Today's side of rows 3 to 6 of section 9.2 is recorded as it is.** Each of the four behaviours that pull request 3b changes on purpose has a test here. So each change of 3b shows as a changed expected value: the table "The expected values that pull request 3b changes" names each test. This is the method of pull requests 2a and 2c.
5. **Two behaviours are recorded that section 9.2 does not name.** The `hang` row of a subscribe frame records the configured latency, and the request flow records 0 since pull request 2c. A frame under a per-method `down` with a latency: its row records the latency, and the WebSocket adapter closes the connection with no wait. The section "Findings for the plan of pull request 3b" has both.
6. **The time bounds.** A test that says "at once" sets a latency or a pause of 1500 ms and requires less than 1.0 second. A test that says "waits" sets 300 ms and requires 0.3 seconds or more. A test that says "no reply" reads the socket for 0.5 seconds.
7. **A fault of the provider is set after the WebSocket is open.** A provider-wide fault refuses the upgrade, so a test that sets it first never reaches the frame code. A per-method fault and a latency do not reach the upgrade, so a test can set them first.
8. **A behaviour that a test of `main` holds gets no second test.** The section "Spec coverage" names the test of `main` for each one.
9. **This plan file merges with the pull request**, as the plan of pull request 2c did. It is the first commit of the branch. The design says "Documents: None" for 3a: that means the documents for a user, and none of them changes.
10. **No run of the automation suites.** This pull request changes no source file, so the simulator image of the branch is the image of `main`. Section 12 of the design asks for that run before a merge of 1, 2b, 2c, 3b and 4a, and not of 3a.

## Global Constraints

1. Tests only. One file of the repository changes: `tests/test_simulator_ws.py`. One file is created: this plan. `server.py` and each file of `provider_simulator/` stay as they are on `main`.
2. Each new test passes on `main` with no code change.
3. No test that exists is edited: no assertion, no name, no body. The file gets two imports, and new constants, helpers and tests at its end.
4. Each expected value is written out by hand. In pull request 3b a value that fails is a row of section 9.2 of the design that Victoria accepted, or it is a defect of 3b. It is not edited to pass.
5. No pull request of the design edits a test file of the cache simulator or of the RESP proxy (section 12 of the design). The four topology tests pass with no edit: `tests/test_domain_topology.py`, `tests/test_control_api_providers.py`, `tests/test_service_publishes_every_port.py` and `tests/test_values_sim_matches_topology.py`.
6. Linux decides. CI runs on `ubuntu-latest`. A green run on a Mac does not prove a socket test.
7. The new tests use the shared simulator of the session, on its real ports. Run one pytest process at a time on a machine. Before a run, `lsof -nP -iTCP:19000 -sTCP:LISTEN` must print nothing.
8. The checks of CI: `black --check .`, `ruff check .`, `mypy .`. Line length 120.
9. A commit has a conventional subject, `<type>(<scope>): <summary>`. It has no `Co-Authored-By` line and no trailer. It does not name an agent configuration file. Stage each file by its name. Never use `git add -A`.
10. Every comment, docstring, document line and commit message of this plan is in ASD-STE100 Simplified Technical English.
11. The branch has a name for the behaviour, and the pull request has the label `no-ticket`. No ticket key is in the branch name, the title or the body: in this repository a key in one of them posts a Jira comment, and a `#Closes` tag moves the ticket of all eight pull requests to Testing.
12. The definition of done of the ticket for a pull request: each new test runs three times in a row with the same result; each new test is shown to fail when the thing that it tests is broken, and the pull request says what was broken; the count of what ran is recorded.
13. A new test file is not put directly into `tests/`. This plan creates no test file.
14. A worktree has no virtual environment. Use the interpreter of the virtual environment of the main checkout, `.venv/bin/python` there. The commands of this plan write `python` for it.

## Review Focus

Five conditions that the design implies and that a plain WebSocket client test does not reach. Each one has its test in the task that owns it.

1. **A row that a test reads while the provider still waits.** The row of a subscribe frame must be complete, and its subscription must be registered, before the wait for the latency. Test: `test_the_row_and_the_subscription_exist_while_the_provider_still_waits` (Task 2).
2. **A fault that starts after the connection is open.** A provider-wide fault refuses the upgrade. A test that sets the fault first reads the refusal, and it never reaches the code of the frames. Each test of a provider-wide fault on a frame opens the WebSocket first (choice 7). Tests: the tests of Task 2 that name a `down`, `hung`, `rate_limited`, `error` or `dropped` provider.
3. **A subscription that must not exist.** A fault on a subscribe frame must register nothing. A connection must not remove the subscription of another connection, and a close must remove the subscriptions of its own connection only. Tests: each fault test of Task 2 reads `GET /ws/subscriptions`, and the class `TestASubscriptionBelongsToOneConnection` (Task 3).
4. **A request that reaches no listener.** A wrong path, a request with no upgrade headers, a text frame that is not JSON and a binary frame are answered or dropped by the adapter. They must write no row, also on a `down` provider. Tests: the two `..._writes_no_row` tests of Task 1 with the case `down`, and `test_a_frame_that_the_adapter_cannot_read_gets_no_reply_and_no_row` (Task 4).
5. **Bytes that a WebSocket client does not show.** The drop points of an upgrade and of a frame, and the body of a refused upgrade, are raw bytes. The tests read the socket to its close and compare byte for byte. Tests: `test_a_dropped_upgrade_gets_the_bytes_of_its_drop_point_and_writes_one_row`, `test_an_upgrade_that_succeeds_gets_the_101_reply_and_writes_no_row` (Task 1) and `test_a_dropped_subscribe_frame_gets_the_bytes_of_its_drop_point` (Task 2).

## The expected values that pull request 3b changes

Pull request 3b changes four behaviours on purpose: rows 3, 4, 5 and 6 of section 9.2 of the design. Each row has a test of this plan that records today's side. All of these tests are in the class `TestSubscribeAndUnsubscribeFrames`.

| Row of section 9.2 | Test | Today | After 3b |
|---|---|---|---|
| 3, a `corruption_mode` on the reply of a subscribe frame | `test_a_corruption_mode_does_not_change_the_reply_of_a_subscribe_or_an_unsubscribe_frame`, six cases | The reply is whole | The corruption applies, and the subscription is registered |
| 4, a per-method `error_probability` | `test_a_per_method_error_probability_is_not_read_for_a_subscribe_frame` | The subscribe succeeds | The error reply, the row `error`, and no subscription |
| 5, a canned `body` for a subscribe method | `test_a_canned_body_is_not_read_for_a_subscribe_frame` | The subscribe registers a subscription and answers its id | The canned body, and no subscription |
| 6, a provider-wide `down` and a per-method override | `test_a_per_method_success_comes_before_a_provider_wide_down_for_a_subscribe_frame` | The subscribe succeeds | No reply, the connection closes, and the row has `*` and no request id |
| 6, the `down` row of a subscribe frame | `test_a_subscribe_frame_of_a_down_provider_closes_the_connection_and_its_row_names_the_frame` | `("eth_subscribe", "down", 1500, 7)` | `("*", "down", 0, None)` |

## Findings for the plan of pull request 3b

1. **The `hang` row of a subscribe frame.** Today it records the configured latency: `test_a_subscribe_frame_of_a_hung_provider_gets_no_reply_and_the_connection_stays_open` holds `("eth_subscribe", "hang", 250, 7)`. `Listener.serve` records 0 for a `hang` row. Section 9.2 of the design does not name this change. So 3b keeps 250 with a hook, or Victoria accepts 0 as a new row of section 9.2.
2. **A per-method `down` with a latency.** The row records the latency of the entry, and the WebSocket adapter closes the connection with no wait: `test_a_per_method_down_with_a_latency_closes_at_once_and_its_row_records_the_latency` for a subscribe frame, and `test_a_per_method_down_with_a_latency_closes_another_frame_at_once` for each other frame. The HTTP adapter and the gRPC adapter wait for that latency. If the WebSocket adapter of 3b waits too, both tests fail, and the change is a new row for Victoria.
3. **The upgrade reads less of the scenario than a request.** For `error` it reads `error_message` only: `error_code` and `http_status` do not change the 400. For `rate_limit` it does not read `rate_limit_body`. It reads no latency, no corruption, no pause and no entry of `responses`. The class `TestTheUpgradeRequest` holds each one, so the upgrade method of the new WebSocket listener must not start to read them.
4. **A pushed event asks no fault policy.** `test_an_event_reaches_its_subscriber_under_each_mode_of_the_provider` holds it. When `listeners/ws.py` writes the row of a pushed event, it must still ask no fault policy.
5. **An unsubscribe method removes a subscription of each subscribe method.** `eth_unsubscribe` removes a subscription that `accountSubscribe` made. The test records it and does not judge it.
6. **The module `server.py` has its own copy of the table of status labels**, `_STATUS_LABEL`, and only the subscribe code uses it. The breaks of the section "The breaks" show that the new tests hold each label of it.
7. **The subscribe code asks no chain.** The content keys `result`, `error_stub` and `error` of an entry of `responses`, and the entry `default`, do not reach a subscribe frame: `test_a_content_key_of_responses_is_not_read_for_a_subscribe_frame`. With the hook `build_content` of section 8.4 of the design that stays so. If 3b gives a subscribe frame to the chain, that test fails.
8. **A JSON frame with no method.** A number, `null`, a text or an object with no method goes to `Listener.serve` today, and the eth chain answers it as the method `unknown`: `test_a_json_frame_with_no_method_goes_to_the_request_flow`. The new WebSocket listener must read such a frame with no error.

## File Structure

One file changes, and one file is created.

| File | Its part in this change |
|---|---|
| `tests/test_simulator_ws.py` | Two imports in the import block: `contextlib` and `re`. At the end of the file: seven constants, eighteen helpers and five test classes with 56 test functions, 114 cases. |
| `docs/superpowers/plans/2026-10-09-pin-todays-websocket-rows.md` | This plan |

The five classes, and the code of `server.py` that each one holds:

| Class | Cases | The code that pull request 3b moves |
|---|---|---|
| `TestTheUpgradeRequest` | 31 | `_WsHandler.do_GET` and `_WsHandler._refuse_upgrade` |
| `TestSubscribeAndUnsubscribeFrames` | 49 | `_WsHandler._serve_subscription_frame` |
| `TestAPushedEvent` | 16 | `_WireSubscriptions.emit` |
| `TestASubscriptionBelongsToOneConnection` | 5 | `_WsHandler._reader_loop` and `_serve_subscription_frame`, with `WsSubscriptions` of `provider_simulator/listeners/ws.py` |
| `TestFramesOutsideTheSubscribeCode` | 13 | `_WsHandler._reader_loop` and `_WsHandler._perform_frame` |

---

### Task 0: The worktree, this plan file and the baseline

**Files:**
- Create: `docs/superpowers/plans/2026-10-09-pin-todays-websocket-rows.md` (this plan)

**Interfaces:**
- Consumes: `main` of `provider-simulator` at `8ed08aa`.
- Produces: a worktree on the branch `pin-todays-websocket-rows`, with this plan as its first commit.

The worktree, the branch and the commit of this plan were made on 2026-10-09, on Victoria's go. The steps below check them.

- [ ] **Step 1: Check the worktree and the branch**

Run: `git status -sb`

Expected: the first line is `## pin-todays-websocket-rows`, and no file of `tests/` is changed.

Run: `git log --oneline -2`

Expected: the first line is the commit of this plan, `docs(plan): the implementation plan of pull request 3a of the request-flow design`. The second line starts with `8ed08aa`.

- [ ] **Step 2: Check that `main` did not move**

Run: `git ls-remote origin refs/heads/main`

Expected: the line starts with `8ed08aa`. Another commit is a stop: read the diff of `main` first. A change of `server.py`, of `provider_simulator/` or of `tests/test_simulator_ws.py` can change the counts and the expected values of this plan.

- [ ] **Step 3: Run the baseline**

Run: `lsof -nP -iTCP:19000 -sTCP:LISTEN`

Expected: no output.

Run: `python -m pytest tests/test_simulator_ws.py -q -p no:cacheprovider`

Expected: `59 passed`.

RAN on 2026-10-09 on the copy of `main` at `8ed08aa`: `59 passed in 4.35s`.

---

### Task 1: The helpers, and the upgrade request

**Files:**
- Modify: `tests/test_simulator_ws.py` (the import block, and the end of the file)

**Interfaces:**
- Consumes: the names that the file has today: `_control(sim, method, path, body=None)`, `_WS_HOST`, `_WS_PORTS`, `port_of`, `ws_protocol`, and the fixtures `sim` and `clean_state`.
- Produces: the constants `_SAMPLE_KEY`, `_HANDSHAKE_OF_PROVIDER_1`, `_LAVA_HEADERS`, `_RATE_LIMIT_TEXT`, `_PORT`, `_SUBSCRIBE` and `_BLOCK_NUMBER`. The helpers that Tasks 2 to 4 use:
  - `_set_scenario(sim, block, pid="1")`: sets one scenario block on a provider of the pool `eth-sim`.
  - `_rows(sim, pid="1")`: the history rows of the ws endpoint of a provider, oldest first.
  - `_rows_when_complete(sim, count, pid="1", timeout_s=2.0)`: the same rows, read when `count` rows exist and none is in flight.
  - `_every_row(sim)`: each history row of the simulator.
  - `_facts(row)`: the tuple `(method, status, latency_ms, request_id)` of a row.
  - `_endpoint(row)`: the tuple `(interface, transport, port)` of a row.
  - `_subscriptions(sim)`: the list of `GET /ws/subscriptions`.
  - `_emit(sim, body)`: `POST /ws/emit`, returns `(status, body)` for a refusal too.
  - `_request_upgrade(port=_PORT, path="/ws", upgrade_headers=True, lava_headers=None)`: returns the socket of one upgrade request.
  - `_read_until_the_close(sock, timeout_s=3.0)`: the bytes that arrive until the simulator closes the connection.
  - `_refusal(sock)`: the tuple `(HTTP status, JSON body)` of a refused upgrade.
  - `_read_the_handshake(sock)`: the bytes of the reply to an upgrade, up to the end of its headers.
  - `_websocket(port=_PORT, lava_headers=None)`: a context manager that gives the socket of an open WebSocket.
  - `_send_frame(sock, message)`, `_payload(sock, timeout_s=3.0)`, `_reply(sock, timeout_s=3.0)`, `_assert_no_frame(sock, wait_s=0.5)` and `_subscribe(sock, method="eth_subscribe", frame_id=1)`.

- [ ] **Step 1: Add the import `contextlib`**

The import block of the file is this today:

```python
from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
```

Change it to this. One line is new: `import contextlib`. The helper `_websocket` of Step 2 uses it. The import `re` comes with Task 2: before that task no line uses it, and `ruff` refuses an import that is not used.

```python
from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
```

- [ ] **Step 2: Add the constants, the helpers and the class `TestTheUpgradeRequest`**

Append this to the end of `tests/test_simulator_ws.py`, after two empty lines:

```python
# ─────────────────────────────────────────────────────────────────────────────
# What WebSocket does today, in the places that write a history row outside
# Listener.serve
#
# Three places of server.py write a history row outside Listener.serve. The
# upgrade request: _WsHandler.do_GET and _refuse_upgrade. A subscribe frame and
# an unsubscribe frame: _serve_subscription_frame. A pushed event:
# _WireSubscriptions.emit. The first two also decide a fault. The tests below
# record the reply and the row of each place. Each expected value is written
# out by hand. A change of that code can make a value fail. Then the value is
# not edited to pass: the change is a change of behaviour, or it is a defect.
# ─────────────────────────────────────────────────────────────────────────────

# The sample key of RFC 6455, section 1.3. The RFC gives the accept value
# s3pPLMBiTxaQ9kYGzzhZRbK+xOo= for it.
_SAMPLE_KEY = "dGhlIHNhbXBsZSBub25jZQ=="

# The reply to an upgrade that succeeds on provider 1 with the sample key.
_HANDSHAKE_OF_PROVIDER_1 = (
    b"HTTP/1.1 101 Switching Protocols\r\n"
    b"Upgrade: websocket\r\n"
    b"Connection: Upgrade\r\n"
    b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n"
    b"Lava-Provider-Address: sim-provider-eth-sim:1\r\n"
    b"\r\n"
)

_LAVA_HEADERS = {"lava-consumer-relay": "7", "lava-stateful-api": "true"}
_RATE_LIMIT_TEXT = b"Rate limit exceeded. Reduce your request rate, or use an API key for a higher limit."
_PORT = _WS_PORTS["1"]
_SUBSCRIBE = {"jsonrpc": "2.0", "method": "eth_subscribe", "params": ["newHeads"], "id": 7}
_BLOCK_NUMBER = {"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 8}


def _set_scenario(sim, block, pid="1"):
    """Set one scenario block on a provider of the pool eth-sim."""
    status, _ = _control(sim, "POST", "/scenario", {"providers": {f"eth-sim:{pid}": block}})
    assert status == 200


def _rows(sim, pid="1"):
    """The history rows of the ws endpoint of one provider, oldest first."""
    _, body = _control(sim, "GET", f"/history?pool=eth-sim&pid={pid}&transport=ws")
    return body["history"]


def _rows_when_complete(sim, count, pid="1", timeout_s=2.0):
    """The rows of the ws endpoint, read when `count` rows exist and none is in flight."""
    deadline = time.monotonic() + timeout_s
    while True:
        rows = _rows(sim, pid)
        if len(rows) >= count and all(row["status"] != "in_flight" for row in rows):
            return rows
        if time.monotonic() > deadline:
            pytest.fail(f"expected {count} complete rows in {timeout_s} s, got {rows!r}")
        time.sleep(0.02)


def _every_row(sim):
    """Each history row of the simulator, of every pool and every transport."""
    _, body = _control(sim, "GET", "/history")
    return body["history"]


def _facts(row):
    """The method, the status, the latency and the request id of a row."""
    return (row["method"], row["status"], row["latency_ms"], row["request_id"])


def _endpoint(row):
    """The endpoint that a row names."""
    return (row["interface"], row["transport"], row["port"])


def _subscriptions(sim):
    _, body = _control(sim, "GET", "/ws/subscriptions")
    return body["subscriptions"]


def _emit(sim, body):
    """POST /ws/emit. Return the status and the body, for a refusal too."""
    request = urllib.request.Request(
        f"{sim['control']}/ws/emit",
        method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps(body).encode(),
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as refusal:
        return refusal.code, json.loads(refusal.read())


def _request_upgrade(port=_PORT, path="/ws", upgrade_headers=True, lava_headers=None):
    """Open a connection and send one request for an upgrade. Return the socket."""
    lines = [f"GET {path} HTTP/1.1", "Host: x"]
    if upgrade_headers:
        lines += [
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {_SAMPLE_KEY}",
            "Sec-WebSocket-Version: 13",
        ]
    lines += [f"{name}: {value}" for name, value in (lava_headers or {}).items()]
    sock = socket.create_connection((_WS_HOST, port), timeout=3)
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
    return sock


def _read_until_the_close(sock, timeout_s=3.0):
    """Read each byte that arrives until the simulator closes the connection.

    A reset counts as a close. A connection that stays open for `timeout_s`
    fails the test, and the failure shows the bytes that arrived.
    """
    sock.settimeout(timeout_s)
    received = b""
    while True:
        try:
            chunk = sock.recv(4096)
        except ConnectionError:
            return received
        except socket.timeout:
            pytest.fail(f"the connection stayed open for {timeout_s} s, and these bytes arrived: {received!r}")
        if not chunk:
            return received
        received += chunk


def _refusal(sock):
    """Read a refused upgrade to its end. Return its HTTP status and its JSON body."""
    try:
        raw = _read_until_the_close(sock)
    finally:
        sock.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    try:
        return int(head.split(b" ", 2)[1]), json.loads(body)
    except (IndexError, ValueError):
        pytest.fail(f"expected a refusal with an HTTP status and a JSON body, and these bytes arrived: {raw!r}")


def _read_the_handshake(sock):
    """Read the reply to an upgrade request, up to the end of its headers."""
    head = b""
    while not head.endswith(b"\r\n\r\n"):
        chunk = sock.recv(4096)
        if not chunk:
            break
        head += chunk
    return head


@contextlib.contextmanager
def _websocket(port=_PORT, lava_headers=None):
    """Open a WebSocket with the sample key. Give the socket after the handshake."""
    sock = _request_upgrade(port, lava_headers=lava_headers)
    try:
        head = _read_the_handshake(sock)
        assert head.startswith(b"HTTP/1.1 101 "), f"the upgrade did not succeed: {head!r}"
        yield sock
    finally:
        try:
            sock.sendall(ws_protocol.encode_frame(ws_protocol.OPCODE_CLOSE, b"", mask=True))
        except OSError:
            pass
        sock.close()


def _send_frame(sock, message):
    """Send one JSON-RPC message as a text frame."""
    sock.sendall(ws_protocol.encode_frame(ws_protocol.OPCODE_TEXT, json.dumps(message).encode(), mask=True))


def _payload(sock, timeout_s=3.0):
    """The payload bytes of the next frame. The frame must be a text frame."""
    sock.settimeout(timeout_s)
    try:
        frame = ws_protocol.parse_frame(sock.recv)
    except socket.timeout:
        pytest.fail(f"expected a frame in {timeout_s} s, and no frame arrived")
    assert (
        frame.opcode == ws_protocol.OPCODE_TEXT
    ), f"expected a text frame, and a frame with the opcode {frame.opcode} arrived: {frame.payload!r}"
    return frame.payload


def _reply(sock, timeout_s=3.0):
    """The next frame, read as JSON."""
    payload = _payload(sock, timeout_s)
    try:
        return json.loads(payload)
    except ValueError:
        pytest.fail(f"expected a JSON reply, and this payload arrived: {payload!r}")


def _assert_no_frame(sock, wait_s=0.5):
    """No byte arrives in `wait_s`, and the connection stays open."""
    sock.settimeout(wait_s)
    try:
        arrived = sock.recv(1)
    except socket.timeout:
        return
    except ConnectionError as reset:
        pytest.fail(f"expected an open connection for {wait_s} s, and the simulator reset it: {reset!r}")
    if arrived:
        pytest.fail(f"expected no byte in {wait_s} s, and this byte arrived: {arrived!r}")
    pytest.fail(f"expected an open connection for {wait_s} s, and the simulator closed it")


def _subscribe(sock, method="eth_subscribe", frame_id=1):
    """Send one subscribe frame. Return the subscription id of the reply."""
    _send_frame(sock, {"jsonrpc": "2.0", "method": method, "params": ["newHeads"], "id": frame_id})
    reply = _reply(sock)
    assert (
        isinstance(reply, dict) and "result" in reply
    ), f"expected a subscription id, and this reply arrived: {reply!r}"
    return reply["result"]


class TestTheUpgradeRequest:
    """The upgrade request that opens a WebSocket: a refusal, and an upgrade
    that succeeds.

    The upgrade is not a request of the request flow. `_WsHandler.do_GET`
    asks the fault policy itself, and `_refuse_upgrade` writes the row of a
    refusal. An upgrade that succeeds writes no row.
    """

    @pytest.mark.parametrize(
        "block, status, body, method, row_status",
        [
            pytest.param({"mode": "down"}, 503, {"error": "provider down"}, "*", "down", id="down"),
            pytest.param(
                {"mode": "rate_limit"}, 429, {"error": "rate limited"}, "ws_upgrade", "rate_limit", id="rate-limit"
            ),
            pytest.param(
                {"mode": "rate_limit", "rate_limit_body": "slow down"},
                429,
                {"error": "rate limited"},
                "ws_upgrade",
                "rate_limit",
                id="rate-limit-with-a-rate-limit-body",
            ),
            pytest.param(
                {"mode": "error"},
                400,
                {"error": "Internal error"},
                "ws_upgrade",
                "error",
                id="error-with-the-default-message",
            ),
            pytest.param(
                {"mode": "error", "error_message": "refused by the test", "error_code": -32099, "http_status": 503},
                400,
                {"error": "refused by the test"},
                "ws_upgrade",
                "error",
                id="error-with-a-message-a-code-and-an-http-status",
            ),
            pytest.param(
                {"mode": "success", "error_probability": 1.0},
                400,
                {"error": "Internal error"},
                "ws_upgrade",
                "error",
                id="error-probability-of-one",
            ),
        ],
    )
    def test_a_refused_upgrade_gets_its_status_and_its_body_and_writes_one_row(
        self, sim, block, status, body, method, row_status
    ):
        """Each fault that answers refuses the upgrade with its own HTTP status
        and its own JSON body. The refusal writes one complete row. Its method
        is `*` for `down` and `ws_upgrade` for each other fault. The row has
        `latency_ms` 0, no request id, and the lava headers of the upgrade
        request."""
        _set_scenario(sim, {**block, "transports": ["ws"]})

        answer = _refusal(_request_upgrade(lava_headers=_LAVA_HEADERS))

        assert answer == (status, body)
        rows = _rows(sim)
        assert [_facts(row) for row in rows] == [(method, row_status, 0, None)]
        assert _endpoint(rows[0]) == ("jsonrpc", "ws", _PORT)
        assert rows[0]["lava_headers"] == _LAVA_HEADERS

    def test_a_hung_upgrade_gets_no_byte_and_writes_one_row(self, sim):
        """A `hang` provider sends no byte for the upgrade. The row is written
        before the wait, so a test can read it while the upgrade hangs."""
        _set_scenario(sim, {"mode": "hang", "transports": ["ws"]})

        sock = _request_upgrade(lava_headers=_LAVA_HEADERS)
        try:
            _assert_no_frame(sock)
            rows = _rows_when_complete(sim, 1)
        finally:
            sock.close()

        assert [_facts(row) for row in rows] == [("ws_upgrade", "hang", 0, None)]
        assert _endpoint(rows[0]) == ("jsonrpc", "ws", _PORT)
        assert rows[0]["lava_headers"] == _LAVA_HEADERS

    def test_a_hung_upgrade_ends_after_30_seconds_with_a_closed_connection_and_no_byte(self, sim):
        """The adapter holds a hung upgrade for 30 seconds. Then it closes the
        connection, and it sends no byte. This is the one slow test of this
        part of the file."""
        _set_scenario(sim, {"mode": "hang", "transports": ["ws"]})

        sock = _request_upgrade()
        started = time.monotonic()
        try:
            received = _read_until_the_close(sock, timeout_s=40.0)
        finally:
            sock.close()
        waited = time.monotonic() - started

        assert received == b""
        assert 29.0 <= waited <= 35.0, f"the upgrade hung for {waited:.1f} s"

    @pytest.mark.parametrize(
        "drop_at, reply",
        [
            pytest.param(None, b"", id="no-drop-point-in-the-scenario"),
            pytest.param("before_headers", b"", id="before-headers"),
            pytest.param("after_headers", _HANDSHAKE_OF_PROVIDER_1, id="after-headers"),
            pytest.param("mid_body", b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: webso", id="mid-body"),
        ],
    )
    def test_a_dropped_upgrade_gets_the_bytes_of_its_drop_point_and_writes_one_row(self, sim, drop_at, reply):
        """`drop_connection` closes the connection of the upgrade. The drop
        point says which bytes arrive first: none, the complete 101 reply, or
        the first 48 bytes of it."""
        block = {"mode": "drop_connection", "transports": ["ws"]}
        if drop_at is not None:
            block["drop_at"] = drop_at
        _set_scenario(sim, block)

        sock = _request_upgrade(lava_headers=_LAVA_HEADERS)
        try:
            received = _read_until_the_close(sock)
        finally:
            sock.close()

        assert received == reply
        rows = _rows(sim)
        assert [_facts(row) for row in rows] == [("ws_upgrade", "drop_connection", 0, None)]
        assert _endpoint(rows[0]) == ("jsonrpc", "ws", _PORT)
        assert rows[0]["lava_headers"] == _LAVA_HEADERS

    def test_an_upgrade_that_succeeds_gets_the_101_reply_and_writes_no_row(self, sim):
        """An upgrade with no fault gets the 101 reply with the provider name,
        and the simulator writes no history row for it."""
        sock = _request_upgrade(lava_headers=_LAVA_HEADERS)
        try:
            head = _read_the_handshake(sock)
        finally:
            sock.close()

        assert head == _HANDSHAKE_OF_PROVIDER_1
        assert _every_row(sim) == []

    @pytest.mark.parametrize("mode", ["success", "down"])
    def test_a_request_for_another_path_gets_404_and_writes_no_row(self, sim, mode):
        """The adapter answers a wrong path itself, before it asks the fault
        policy. So a `down` provider answers 404 too, and no row is written."""
        _set_scenario(sim, {"mode": mode, "transports": ["ws"]})

        answer = _refusal(_request_upgrade(path="/"))

        assert answer == (404, {"error": "not found"})
        assert _every_row(sim) == []

    @pytest.mark.parametrize("mode", ["success", "down"])
    def test_a_request_with_no_upgrade_headers_gets_400_and_writes_no_row(self, sim, mode):
        """The adapter answers a request that is no upgrade itself, before it
        asks the fault policy. So a `down` provider answers 400 too, and no
        row is written."""
        _set_scenario(sim, {"mode": mode, "transports": ["ws"]})

        answer = _refusal(_request_upgrade(upgrade_headers=False))

        assert answer == (400, {"error": "bad WS upgrade request"})
        assert _every_row(sim) == []

    def test_a_refused_upgrade_does_not_wait_for_latency_ms(self, sim):
        """The upgrade applies no `latency_ms`: the refusal comes at once, and
        its row records 0."""
        _set_scenario(sim, {"mode": "rate_limit", "latency_ms": 1500, "transports": ["ws"]})

        started = time.monotonic()
        answer = _refusal(_request_upgrade())
        waited = time.monotonic() - started

        assert answer == (429, {"error": "rate limited"})
        assert waited < 1.0, f"the refusal took {waited:.2f} s"
        assert [_facts(row) for row in _rows(sim)] == [("ws_upgrade", "rate_limit", 0, None)]

    def test_an_upgrade_that_succeeds_does_not_wait_for_latency_ms(self, sim):
        """The upgrade applies no `latency_ms`: the 101 reply comes at once."""
        _set_scenario(sim, {"mode": "success", "latency_ms": 1500, "transports": ["ws"]})

        started = time.monotonic()
        sock = _request_upgrade()
        try:
            head = _read_the_handshake(sock)
        finally:
            sock.close()
        waited = time.monotonic() - started

        assert head == _HANDSHAKE_OF_PROVIDER_1
        assert waited < 1.0, f"the handshake took {waited:.2f} s"

    @pytest.mark.parametrize(
        "corruption_mode", ["truncated", "invalid_json", "empty_response", "null_body", "missing_field", "wrong_type"]
    )
    def test_the_upgrade_applies_no_corruption(self, sim, corruption_mode):
        """A `corruption_mode` does not change the reply of the upgrade: not
        the body of a refusal, and not the 101 reply."""
        corruption = {"corruption_mode": corruption_mode, "missing_field": "error", "transports": ["ws"]}

        _set_scenario(sim, {"mode": "rate_limit", **corruption})
        assert _refusal(_request_upgrade()) == (429, {"error": "rate limited"})

        _set_scenario(sim, {"mode": "success", **corruption})
        sock = _request_upgrade()
        try:
            assert _read_the_handshake(sock) == _HANDSHAKE_OF_PROVIDER_1
        finally:
            sock.close()

    @pytest.mark.parametrize("key", ["ws_upgrade", "*"])
    def test_the_upgrade_reads_no_per_method_override(self, sim, key):
        """An entry of `responses` does not reach the upgrade, also when its
        key is the method name of the row of a refused upgrade."""
        _set_scenario(sim, {"mode": "success", "transports": ["ws"], "responses": {key: {"mode": "down"}}})
        sock = _request_upgrade()
        try:
            assert _read_the_handshake(sock) == _HANDSHAKE_OF_PROVIDER_1
        finally:
            sock.close()
        assert _every_row(sim) == []

        _set_scenario(sim, {"mode": "rate_limit", "transports": ["ws"], "responses": {key: {"mode": "success"}}})
        assert _refusal(_request_upgrade()) == (429, {"error": "rate limited"})

    @pytest.mark.parametrize(
        "scope",
        [
            pytest.param({"transports": ["http"]}, id="a-transports-filter"),
            pytest.param({"ports": [port_of("eth-sim", "1")]}, id="a-ports-filter"),
        ],
    )
    def test_a_filter_that_does_not_name_the_ws_endpoint_leaves_the_upgrade_alone(self, sim, scope):
        """A `down` with a filter that names the http endpoint only does not
        reach the upgrade: it succeeds, and no row is written."""
        _set_scenario(sim, {"mode": "down", **scope})

        sock = _request_upgrade()
        try:
            head = _read_the_handshake(sock)
        finally:
            sock.close()

        assert head == _HANDSHAKE_OF_PROVIDER_1
        assert _every_row(sim) == []

    def test_the_upgrade_performs_no_pause(self, sim):
        """`pause_at` holds a reply of the http endpoint of the provider. The
        upgrade of its ws endpoint is not held."""
        _set_scenario(sim, {"mode": "success", "pause_at": "mid_body", "pause_ms": 1500})

        started = time.monotonic()
        sock = _request_upgrade()
        try:
            head = _read_the_handshake(sock)
        finally:
            sock.close()
        waited = time.monotonic() - started

        assert head == _HANDSHAKE_OF_PROVIDER_1
        assert waited < 1.0, f"the handshake took {waited:.2f} s"

    def test_each_upgrade_uses_one_count_of_the_fail_first_n_window(self, sim):
        """With `fail_first_n` 2 on the ws endpoint, two upgrades are refused
        and each one writes its row. The third upgrade succeeds and writes no
        row."""
        _set_scenario(sim, {"mode": "rate_limit", "fail_first_n": 2, "then_mode": "success", "transports": ["ws"]})

        first = _refusal(_request_upgrade())
        second = _refusal(_request_upgrade())
        sock = _request_upgrade()
        try:
            third = _read_the_handshake(sock)
        finally:
            sock.close()

        assert first == (429, {"error": "rate limited"})
        assert second == (429, {"error": "rate limited"})
        assert third == _HANDSHAKE_OF_PROVIDER_1
        assert [_facts(row) for row in _rows(sim)] == [("ws_upgrade", "rate_limit", 0, None)] * 2
```

- [ ] **Step 3: Run the file**

Run: `lsof -nP -iTCP:19000 -sTCP:LISTEN`

Expected: no output.

Run: `python -m pytest tests/test_simulator_ws.py -q -p no:cacheprovider`

Expected: `90 passed`. That is the 59 tests of `main` and the 31 cases of `TestTheUpgradeRequest`. The run takes about 35 seconds: 30 of them are the one slow test.

- [ ] **Step 4: Run the checks on the file**

Run: `python -m black --check tests/test_simulator_ws.py`

Expected: `1 file would be left unchanged.`

Run: `python -m ruff check tests/test_simulator_ws.py`

Expected: `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add tests/test_simulator_ws.py
git commit -m "test(ws): record the reply and the row of an upgrade request"
```

---

### Task 2: A subscribe frame and an unsubscribe frame

**Files:**
- Modify: `tests/test_simulator_ws.py` (the import block, and the end of the file)

**Interfaces:**
- Consumes: the constants and the helpers of Task 1.
- Produces: the class `TestSubscribeAndUnsubscribeFrames`. It holds the five tests of the table "The expected values that pull request 3b changes".

- [ ] **Step 1: Add the import `re`**

In the import block, add the line `import re` between `import os` and `import socket`. The block is then:

```python
from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
```

- [ ] **Step 2: Add the class `TestSubscribeAndUnsubscribeFrames`**

Append this to the end of `tests/test_simulator_ws.py`, after two empty lines:

```python
class TestSubscribeAndUnsubscribeFrames:
    """A subscribe frame and an unsubscribe frame.

    `_WsHandler._serve_subscription_frame` serves these frames with its own
    copy of the request flow. It writes their rows itself.
    """

    def test_each_frame_writes_one_success_row_with_its_method_and_its_request_id(self, sim):
        """Three frames: a subscribe frame, an unsubscribe frame that removes
        the subscription, and an unsubscribe frame that removes nothing. Each
        one writes one `success` row with the method and the id of the frame.
        Each row carries the lava headers of the upgrade request."""
        with _websocket(lava_headers=_LAVA_HEADERS) as sock:
            _send_frame(sock, {"jsonrpc": "2.0", "method": "eth_subscribe", "params": ["newHeads"], "id": 41})
            subscription_id = _reply(sock)["result"]
            _send_frame(sock, {"jsonrpc": "2.0", "method": "eth_unsubscribe", "params": [subscription_id], "id": 42})
            removed = _reply(sock)
            _send_frame(sock, {"jsonrpc": "2.0", "method": "eth_unsubscribe", "params": [subscription_id], "id": 43})
            not_removed = _reply(sock)
            rows = _rows(sim)

        assert re.fullmatch(r"0x[0-9a-f]{32}", subscription_id), subscription_id
        assert removed == {"jsonrpc": "2.0", "id": 42, "result": True}
        assert not_removed == {"jsonrpc": "2.0", "id": 43, "result": False}
        assert [_facts(row) for row in rows] == [
            ("eth_subscribe", "success", 0, 41),
            ("eth_unsubscribe", "success", 0, 42),
            ("eth_unsubscribe", "success", 0, 43),
        ]
        for row in rows:
            assert _endpoint(row) == ("jsonrpc", "ws", _PORT)
            assert row["lava_headers"] == _LAVA_HEADERS

    @pytest.mark.parametrize(
        "subscribe, unsubscribe",
        [
            pytest.param("eth_subscribe", "eth_unsubscribe", id="eth"),
            pytest.param("subscribe", "unsubscribe", id="tendermint"),
            pytest.param("accountSubscribe", "accountUnsubscribe", id="solana-account"),
            pytest.param("logsSubscribe", "logsUnsubscribe", id="solana-logs"),
            pytest.param("accountSubscribe", "eth_unsubscribe", id="an-unsubscribe-method-of-another-chain"),
        ],
    )
    def test_each_subscribe_method_and_each_unsubscribe_method_writes_a_row_with_its_own_name(
        self, sim, subscribe, unsubscribe
    ):
        """Each of the four subscribe methods registers a subscription, and each
        of the four unsubscribe methods removes a subscription of its own
        connection. The method name of each row is the method of its frame."""
        with _websocket() as sock:
            subscription_id = _subscribe(sock, subscribe, frame_id=1)
            _send_frame(sock, {"jsonrpc": "2.0", "method": unsubscribe, "params": [subscription_id], "id": 2})
            removed = _reply(sock)
            rows = _rows(sim)
            subscriptions = _subscriptions(sim)

        assert removed == {"jsonrpc": "2.0", "id": 2, "result": True}
        assert [_facts(row) for row in rows] == [(subscribe, "success", 0, 1), (unsubscribe, "success", 0, 2)]
        assert subscriptions == []

    def test_a_text_id_goes_into_the_reply_and_into_the_row(self, sim):
        """The id of a subscribe frame can be text. The reply and the row carry
        it as it is."""
        with _websocket() as sock:
            _send_frame(sock, {"jsonrpc": "2.0", "method": "eth_subscribe", "params": ["newHeads"], "id": "sub-a"})
            reply = _reply(sock)
            rows = _rows(sim)

        assert reply["id"] == "sub-a"
        assert [_facts(row) for row in rows] == [("eth_subscribe", "success", 0, "sub-a")]

    def test_a_frame_with_no_id_gets_a_reply_with_the_id_null_and_a_row_with_no_request_id(self, sim):
        """A subscribe frame and an unsubscribe frame with no id: the reply has
        the id `null`, and the row has no request id. An unsubscribe frame with
        no params removes nothing."""
        with _websocket() as sock:
            _send_frame(sock, {"jsonrpc": "2.0", "method": "eth_subscribe", "params": ["newHeads"]})
            subscribed = _reply(sock)
            _send_frame(sock, {"jsonrpc": "2.0", "method": "eth_unsubscribe"})
            not_removed = _reply(sock)
            rows = _rows(sim)
            subscriptions = _subscriptions(sim)

        assert subscribed["id"] is None
        assert not_removed == {"jsonrpc": "2.0", "id": None, "result": False}
        assert [_facts(row) for row in rows] == [
            ("eth_subscribe", "success", 0, None),
            ("eth_unsubscribe", "success", 0, None),
        ]
        assert [entry["subscription_id"] for entry in subscriptions] == [subscribed["result"]]

    def test_a_subscribe_frame_waits_for_latency_ms_and_its_row_records_it(self, sim):
        """The reply of a subscribe frame comes after the latency of the
        scenario, and the row records that latency."""
        _set_scenario(sim, {"mode": "success", "latency_ms": 300, "transports": ["ws"]})

        with _websocket() as sock:
            started = time.monotonic()
            _send_frame(sock, _SUBSCRIBE)
            reply = _reply(sock)
            waited = time.monotonic() - started
            rows = _rows(sim)

        assert re.fullmatch(r"0x[0-9a-f]{32}", reply["result"]), reply
        assert waited >= 0.3, f"the reply came after {waited:.2f} s"
        assert [_facts(row) for row in rows] == [("eth_subscribe", "success", 300, 7)]

    def test_the_row_and_the_subscription_exist_while_the_provider_still_waits(self, sim):
        """The row of a subscribe frame is complete, and the subscription is
        registered, before the provider waits for the latency. A test can read
        both while the reply is still held back."""
        _set_scenario(sim, {"mode": "success", "latency_ms": 1500, "transports": ["ws"]})

        with _websocket() as sock:
            _send_frame(sock, _SUBSCRIBE)
            rows = _rows_when_complete(sim, 1, timeout_s=1.0)
            subscriptions = _subscriptions(sim)
            _assert_no_frame(sock, wait_s=0.2)
            reply = _reply(sock)

        assert [_facts(row) for row in rows] == [("eth_subscribe", "success", 1500, 7)]
        assert [entry["subscription_id"] for entry in subscriptions] == [reply["result"]]

    def test_a_subscribe_frame_of_a_down_provider_closes_the_connection_and_its_row_names_the_frame(self, sim):
        """A provider-wide `down` on a subscribe frame: no reply, and the
        connection closes with no wait. The row records the method, the id of
        the frame and the configured latency. This differs from each other
        frame, whose `down` row has the method `*`, no request id and 0."""
        with _websocket(lava_headers=_LAVA_HEADERS) as sock:
            _set_scenario(sim, {"mode": "down", "latency_ms": 1500, "transports": ["ws"]})
            started = time.monotonic()
            _send_frame(sock, _SUBSCRIBE)
            received = _read_until_the_close(sock)
            waited = time.monotonic() - started

        assert received == b""
        assert waited < 1.0, f"the connection closed after {waited:.2f} s"
        rows = _rows(sim)
        assert [_facts(row) for row in rows] == [("eth_subscribe", "down", 1500, 7)]
        assert _endpoint(rows[0]) == ("jsonrpc", "ws", _PORT)
        assert rows[0]["lava_headers"] == _LAVA_HEADERS
        assert _subscriptions(sim) == []

    def test_a_subscribe_frame_of_a_hung_provider_gets_no_reply_and_the_connection_stays_open(self, sim):
        """A `hang` on a subscribe frame: no reply, and no subscription. The
        row records the configured latency. The connection stays open: it
        answers the next frame when the fault is gone."""
        with _websocket() as sock:
            _set_scenario(sim, {"mode": "hang", "latency_ms": 250, "transports": ["ws"]})
            _send_frame(sock, _SUBSCRIBE)
            _assert_no_frame(sock)
            rows = _rows_when_complete(sim, 1)
            subscriptions = _subscriptions(sim)
            _set_scenario(sim, {"mode": "success", "latency_ms": 0, "transports": ["ws"]})
            _send_frame(sock, _BLOCK_NUMBER)
            next_reply = _reply(sock)

        assert [_facts(row) for row in rows] == [("eth_subscribe", "hang", 250, 7)]
        assert subscriptions == []
        assert next_reply["id"] == 8

    @pytest.mark.parametrize(
        "block, text",
        [
            pytest.param({"mode": "rate_limit"}, _RATE_LIMIT_TEXT, id="the-default-text"),
            pytest.param({"mode": "rate_limit", "rate_limit_body": "slow down"}, b"slow down", id="rate-limit-body"),
        ],
    )
    def test_a_subscribe_frame_of_a_rate_limited_provider_gets_the_text_of_the_rate_limit(self, sim, block, text):
        """`rate_limit` on a subscribe frame: the reply is a text frame with
        the rate-limit text, and no subscription is registered."""
        with _websocket() as sock:
            _set_scenario(sim, {**block, "latency_ms": 100, "transports": ["ws"]})
            _send_frame(sock, _SUBSCRIBE)
            payload = _payload(sock)
            rows = _rows(sim)
            subscriptions = _subscriptions(sim)

        assert payload == text
        assert [_facts(row) for row in rows] == [("eth_subscribe", "rate_limit", 100, 7)]
        assert subscriptions == []

    @pytest.mark.parametrize(
        "block, frame, reply, expected_row",
        [
            pytest.param(
                {"mode": "error"},
                _SUBSCRIBE,
                {"jsonrpc": "2.0", "id": 7, "error": {"code": -32000, "message": "Internal error"}},
                ("eth_subscribe", "error", 0, 7),
                id="error-with-the-default-code-and-message",
            ),
            pytest.param(
                {"mode": "error", "error_code": -32601, "error_message": "Method not found"},
                _SUBSCRIBE,
                {"jsonrpc": "2.0", "id": 7, "error": {"code": -32601, "message": "Method not found"}},
                ("eth_subscribe", "error", 0, 7),
                id="error-with-a-code-and-a-message",
            ),
            pytest.param(
                {"mode": "success", "error_probability": 1.0, "error_code": -32007, "error_message": "Forced"},
                _SUBSCRIBE,
                {"jsonrpc": "2.0", "id": 7, "error": {"code": -32007, "message": "Forced"}},
                ("eth_subscribe", "error", 0, 7),
                id="a-provider-wide-error-probability-of-one",
            ),
            pytest.param(
                {"mode": "error"},
                {"jsonrpc": "2.0", "method": "eth_subscribe", "params": ["newHeads"]},
                {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "Internal error"}},
                ("eth_subscribe", "error", 0, None),
                id="a-frame-with-no-id-gets-the-id-1-in-the-error",
            ),
            pytest.param(
                {"mode": "error"},
                {"jsonrpc": "2.0", "method": "eth_unsubscribe", "params": ["0x00"], "id": 9},
                {"jsonrpc": "2.0", "id": 9, "error": {"code": -32000, "message": "Internal error"}},
                ("eth_unsubscribe", "error", 0, 9),
                id="an-unsubscribe-frame",
            ),
        ],
    )
    def test_a_subscription_frame_of_a_provider_with_an_error_gets_the_error_reply(
        self, sim, block, frame, reply, expected_row
    ):
        """`error`, or an `error_probability` of the provider, on a subscribe
        frame or an unsubscribe frame. The reply is the JSON-RPC error of the
        scenario, and no subscription is registered."""
        with _websocket() as sock:
            _set_scenario(sim, {**block, "transports": ["ws"]})
            _send_frame(sock, frame)
            answer = _reply(sock)
            rows = _rows(sim)
            subscriptions = _subscriptions(sim)

        assert answer == reply
        assert [_facts(row) for row in rows] == [expected_row]
        assert subscriptions == []

    def test_an_unsubscribe_frame_of_a_provider_with_an_error_removes_nothing(self, sim):
        """`error` on an unsubscribe frame that names a subscription of its own
        connection: the reply is the JSON-RPC error, and the subscription stays."""
        with _websocket() as sock:
            subscription_id = _subscribe(sock, frame_id=1)
            _set_scenario(sim, {"mode": "error", "transports": ["ws"]})
            _send_frame(sock, {"jsonrpc": "2.0", "method": "eth_unsubscribe", "params": [subscription_id], "id": 2})
            answer = _reply(sock)
            rows = _rows(sim)
            subscriptions = _subscriptions(sim)

        assert answer == {"jsonrpc": "2.0", "id": 2, "error": {"code": -32000, "message": "Internal error"}}
        assert [_facts(row) for row in rows] == [("eth_subscribe", "success", 0, 1), ("eth_unsubscribe", "error", 0, 2)]
        assert [entry["subscription_id"] for entry in subscriptions] == [subscription_id]

    @pytest.mark.parametrize(
        "drop_at, received",
        [
            pytest.param(None, b"", id="no-drop-point-in-the-scenario"),
            pytest.param("before_headers", b"", id="before-headers"),
            pytest.param("after_headers", bytes([0x81, 100]), id="after-headers"),
            pytest.param("mid_body", bytes([0x81, 100]) + b"X" * 50, id="mid-body"),
        ],
    )
    def test_a_dropped_subscribe_frame_gets_the_bytes_of_its_drop_point(self, sim, drop_at, received):
        """`drop_connection` on a subscribe frame closes the connection. The
        drop point says which bytes arrive first: none, a frame header that
        declares 100 payload bytes, or that header and 50 bytes."""
        block = {"mode": "drop_connection", "transports": ["ws"]}
        if drop_at is not None:
            block["drop_at"] = drop_at

        with _websocket() as sock:
            _set_scenario(sim, block)
            _send_frame(sock, _SUBSCRIBE)
            arrived = _read_until_the_close(sock)

        assert arrived == received
        assert [_facts(row) for row in _rows(sim)] == [("eth_subscribe", "drop_connection", 0, 7)]
        assert _subscriptions(sim) == []

    @pytest.mark.parametrize(
        "corruption_mode", ["truncated", "invalid_json", "empty_response", "null_body", "missing_field", "wrong_type"]
    )
    def test_a_corruption_mode_does_not_change_the_reply_of_a_subscribe_or_an_unsubscribe_frame(
        self, sim, corruption_mode
    ):
        """Today a `corruption_mode` does not reach the success reply of a
        subscribe frame or of an unsubscribe frame. The caller reads the
        subscription id, and the subscription is registered."""
        _set_scenario(
            sim,
            {"mode": "success", "corruption_mode": corruption_mode, "missing_field": "result", "transports": ["ws"]},
        )

        with _websocket() as sock:
            _send_frame(sock, _SUBSCRIBE)
            subscribed = _reply(sock)
            registered = [entry["subscription_id"] for entry in _subscriptions(sim)]
            _send_frame(
                sock, {"jsonrpc": "2.0", "method": "eth_unsubscribe", "params": [subscribed["result"]], "id": 9}
            )
            removed = _reply(sock)

        assert set(subscribed) == {"jsonrpc", "id", "result"}
        assert subscribed["id"] == 7
        assert re.fullmatch(r"0x[0-9a-f]{32}", subscribed["result"]), subscribed
        assert registered == [subscribed["result"]]
        assert removed == {"jsonrpc": "2.0", "id": 9, "result": True}

    @pytest.mark.parametrize(
        "block, payload, row_status",
        [
            pytest.param(
                {"mode": "error", "corruption_mode": "invalid_json"},
                b"}{ {{ not valid json",
                "error",
                id="invalid-json-on-an-error-reply",
            ),
            pytest.param(
                {"mode": "rate_limit", "corruption_mode": "empty_response"},
                b"",
                "rate_limit",
                id="empty-response-on-a-rate-limit-reply",
            ),
        ],
    )
    def test_a_corruption_mode_changes_the_fault_reply_of_a_subscribe_frame(self, sim, block, payload, row_status):
        """A `corruption_mode` reaches the reply of a fault on a subscribe
        frame: the `error` reply and the `rate_limit` reply. The row keeps the
        status of the fault."""
        with _websocket() as sock:
            _set_scenario(sim, {**block, "transports": ["ws"]})
            _send_frame(sock, _SUBSCRIBE)
            arrived = _payload(sock)
            rows = _rows(sim)

        assert arrived == payload
        assert [_facts(row) for row in rows] == [("eth_subscribe", row_status, 0, 7)]

    def test_a_per_method_rate_limit_reaches_the_subscribe_frame_only(self, sim):
        """An entry of `responses` for `eth_subscribe` with `mode: rate_limit`.
        The subscribe frame gets the rate-limit text, and another frame of the
        same connection gets its result."""
        _set_scenario(
            sim, {"mode": "success", "transports": ["ws"], "responses": {"eth_subscribe": {"mode": "rate_limit"}}}
        )

        with _websocket() as sock:
            _send_frame(sock, _SUBSCRIBE)
            payload = _payload(sock)
            _send_frame(sock, _BLOCK_NUMBER)
            other = _reply(sock)
            rows = _rows(sim)
            subscriptions = _subscriptions(sim)

        assert payload == _RATE_LIMIT_TEXT
        assert other["id"] == 8 and other["result"].startswith("0x")
        assert [_facts(row)[:2] for row in rows] == [("eth_subscribe", "rate_limit"), ("eth_blockNumber", "success")]
        assert subscriptions == []

    def test_a_per_method_down_with_a_latency_closes_at_once_and_its_row_records_the_latency(self, sim):
        """An entry of `responses` for `eth_subscribe` with `mode: down` and a
        latency: the connection closes with no wait and with no reply. The row
        records the method, the id of the frame and the latency of the entry."""
        _set_scenario(
            sim,
            {
                "mode": "success",
                "transports": ["ws"],
                "responses": {"eth_subscribe": {"mode": "down", "latency_ms": 1500}},
            },
        )

        with _websocket() as sock:
            started = time.monotonic()
            _send_frame(sock, _SUBSCRIBE)
            received = _read_until_the_close(sock)
            waited = time.monotonic() - started

        assert received == b""
        assert waited < 1.0, f"the connection closed after {waited:.2f} s"
        assert [_facts(row) for row in _rows(sim)] == [("eth_subscribe", "down", 1500, 7)]
        assert _subscriptions(sim) == []

    def test_a_per_method_hang_gives_no_reply_to_the_subscribe_frame(self, sim):
        """An entry of `responses` for `eth_subscribe` with `mode: hang`: no
        reply and no subscription, and the row has the status `hang`."""
        _set_scenario(sim, {"mode": "success", "transports": ["ws"], "responses": {"eth_subscribe": {"mode": "hang"}}})

        with _websocket() as sock:
            _send_frame(sock, _SUBSCRIBE)
            _assert_no_frame(sock)
            rows = _rows_when_complete(sim, 1)
            subscriptions = _subscriptions(sim)

        assert [_facts(row) for row in rows] == [("eth_subscribe", "hang", 0, 7)]
        assert subscriptions == []

    def test_a_per_method_drop_point_reaches_the_subscribe_frame(self, sim):
        """An entry of `responses` for `eth_subscribe` with `drop_connection`
        and the drop point `mid_body`. The frame header that declares 100
        bytes arrives with 50 bytes, and the connection closes."""
        _set_scenario(
            sim,
            {
                "mode": "success",
                "transports": ["ws"],
                "responses": {"eth_subscribe": {"mode": "drop_connection", "drop_at": "mid_body"}},
            },
        )

        with _websocket() as sock:
            _send_frame(sock, _SUBSCRIBE)
            arrived = _read_until_the_close(sock)

        assert arrived == bytes([0x81, 100]) + b"X" * 50
        assert [_facts(row) for row in _rows(sim)] == [("eth_subscribe", "drop_connection", 0, 7)]

    def test_a_per_method_latency_delays_the_subscribe_frame_only(self, sim):
        """An entry of `responses` for `eth_subscribe` with a latency: the
        subscribe frame waits and its row records the latency. The row of
        another frame of the same connection records 0."""
        _set_scenario(
            sim, {"mode": "success", "transports": ["ws"], "responses": {"eth_subscribe": {"latency_ms": 300}}}
        )

        with _websocket() as sock:
            started = time.monotonic()
            _send_frame(sock, _SUBSCRIBE)
            _reply(sock)
            waited = time.monotonic() - started
            _send_frame(sock, _BLOCK_NUMBER)
            _reply(sock)
            rows = _rows(sim)

        assert waited >= 0.3, f"the reply came after {waited:.2f} s"
        assert [_facts(row) for row in rows] == [
            ("eth_subscribe", "success", 300, 7),
            ("eth_blockNumber", "success", 0, 8),
        ]

    def test_a_per_method_error_code_and_message_reach_the_subscribe_frame_only(self, sim):
        """Under a provider-wide `error`, an entry of `responses` for
        `eth_subscribe` gives the subscribe frame its own error code and its
        own message. Another frame gets the code and the message of the
        provider."""
        with _websocket() as sock:
            _set_scenario(
                sim,
                {
                    "mode": "error",
                    "error_code": -32000,
                    "error_message": "of the provider",
                    "transports": ["ws"],
                    "responses": {"eth_subscribe": {"error_code": -32601, "error_message": "of this method"}},
                },
            )
            _send_frame(sock, _SUBSCRIBE)
            of_the_method = _reply(sock)
            _send_frame(sock, _BLOCK_NUMBER)
            of_the_provider = _reply(sock)

        assert of_the_method == {"jsonrpc": "2.0", "id": 7, "error": {"code": -32601, "message": "of this method"}}
        assert of_the_provider == {"jsonrpc": "2.0", "id": 8, "error": {"code": -32000, "message": "of the provider"}}

    def test_a_per_method_error_probability_is_not_read_for_a_subscribe_frame(self, sim):
        """Today the subscribe code does not read `error_probability` from an
        entry of `responses`: the subscribe succeeds. The request flow reads
        that key: the same entry for `eth_blockNumber` gives an error."""
        _set_scenario(
            sim,
            {
                "mode": "success",
                "transports": ["ws"],
                "responses": {
                    "eth_subscribe": {"error_probability": 1.0},
                    "eth_blockNumber": {"error_probability": 1.0},
                },
            },
        )

        with _websocket() as sock:
            _send_frame(sock, _SUBSCRIBE)
            subscribed = _reply(sock)
            _send_frame(sock, _BLOCK_NUMBER)
            other = _reply(sock)
            rows = _rows(sim)
            subscriptions = _subscriptions(sim)

        assert re.fullmatch(r"0x[0-9a-f]{32}", subscribed["result"]), subscribed
        assert other == {"jsonrpc": "2.0", "id": 8, "error": {"code": -32000, "message": "Internal error"}}
        assert [_facts(row) for row in rows] == [("eth_subscribe", "success", 0, 7), ("eth_blockNumber", "error", 0, 8)]
        assert [entry["subscription_id"] for entry in subscriptions] == [subscribed["result"]]

    def test_a_canned_body_is_not_read_for_a_subscribe_frame(self, sim):
        """Today the subscribe code does not read a canned `body` from an entry
        of `responses`: the subscribe registers a subscription and answers its
        id. The request flow reads that key: the same entry for
        `eth_blockNumber` gives the canned body."""
        canned = {"jsonrpc": "2.0", "id": 1, "result": "0xcanned"}
        _set_scenario(
            sim,
            {
                "mode": "success",
                "transports": ["ws"],
                "responses": {"eth_subscribe": {"body": canned}, "eth_blockNumber": {"body": canned}},
            },
        )

        with _websocket() as sock:
            _send_frame(sock, _SUBSCRIBE)
            subscribed = _reply(sock)
            _send_frame(sock, _BLOCK_NUMBER)
            other = _reply(sock)
            subscriptions = _subscriptions(sim)

        assert subscribed["id"] == 7
        assert re.fullmatch(r"0x[0-9a-f]{32}", subscribed["result"]), subscribed
        assert other == canned
        assert [entry["subscription_id"] for entry in subscriptions] == [subscribed["result"]]

    @pytest.mark.parametrize(
        "responses, other_reply",
        [
            pytest.param(
                {"eth_subscribe": {"result": "0xcanned"}, "eth_blockNumber": {"result": "0xcanned"}},
                ("result", "0xcanned"),
                id="result",
            ),
            pytest.param(
                {"eth_subscribe": {"error_stub": "revert"}, "eth_blockNumber": {"error_stub": "revert"}},
                ("error", 3, "execution reverted"),
                id="error-stub",
            ),
            pytest.param(
                {
                    "eth_subscribe": {"error": {"code": -32099, "message": "canned"}},
                    "eth_blockNumber": {"error": {"code": -32099, "message": "canned"}},
                },
                ("error", -32099, "canned"),
                id="error",
            ),
            pytest.param({"default": {"result": "0xcanned"}}, ("result", "0xcanned"), id="the-entry-default"),
        ],
    )
    def test_a_content_key_of_responses_is_not_read_for_a_subscribe_frame(self, sim, responses, other_reply):
        """A chain reads the content keys `result`, `error_stub` and `error` of
        an entry of `responses`, and the entry `default`. The subscribe code
        asks no chain: the subscribe registers a subscription and answers its
        id. The same key reaches `eth_blockNumber` on the same connection."""
        _set_scenario(sim, {"mode": "success", "transports": ["ws"], "responses": responses})

        with _websocket() as sock:
            _send_frame(sock, _SUBSCRIBE)
            subscribed = _reply(sock)
            _send_frame(sock, _BLOCK_NUMBER)
            other = _reply(sock)
            rows = _rows(sim)
            subscriptions = _subscriptions(sim)

        if "result" in other:
            other_facts = ("result", other["result"])
        else:
            other_facts = ("error", other["error"]["code"], other["error"]["message"])
        assert set(subscribed) == {"jsonrpc", "id", "result"}
        assert re.fullmatch(r"0x[0-9a-f]{32}", subscribed["result"]), subscribed
        assert other_facts == other_reply
        assert _facts(rows[0]) == ("eth_subscribe", "success", 0, 7)
        assert [entry["subscription_id"] for entry in subscriptions] == [subscribed["result"]]

    def test_a_subscribe_frame_performs_no_pause(self, sim):
        """`pause_at` holds a reply of the http endpoint of the provider. The
        reply of a subscribe frame is not held."""
        _set_scenario(sim, {"mode": "success", "pause_at": "mid_body", "pause_ms": 1500})

        with _websocket() as sock:
            started = time.monotonic()
            _send_frame(sock, _SUBSCRIBE)
            subscribed = _reply(sock)
            waited = time.monotonic() - started

        assert re.fullmatch(r"0x[0-9a-f]{32}", subscribed["result"]), subscribed
        assert waited < 1.0, f"the reply came after {waited:.2f} s"

    def test_a_per_method_success_comes_before_a_provider_wide_down_for_a_subscribe_frame(self, sim):
        """Today the subscribe code reads the entry of `responses` before the
        `down` check. Under a provider-wide `down`, an entry with
        `mode: success` makes the subscribe succeed. Each other frame gets the
        `down`: the connection closes, and the row has the method `*`."""
        with _websocket() as sock:
            _set_scenario(
                sim, {"mode": "down", "transports": ["ws"], "responses": {"eth_subscribe": {"mode": "success"}}}
            )
            _send_frame(sock, _SUBSCRIBE)
            subscribed = _reply(sock)
            subscriptions = _subscriptions(sim)
            _send_frame(sock, _BLOCK_NUMBER)
            received = _read_until_the_close(sock)

        assert re.fullmatch(r"0x[0-9a-f]{32}", subscribed["result"]), subscribed
        assert [entry["subscription_id"] for entry in subscriptions] == [subscribed["result"]]
        assert received == b""
        assert [_facts(row) for row in _rows(sim)] == [("eth_subscribe", "success", 0, 7), ("*", "down", 0, None)]

    @pytest.mark.parametrize(
        "scope",
        [
            pytest.param({"transports": ["http"]}, id="a-transports-filter"),
            pytest.param({"ports": [port_of("eth-sim", "1")]}, id="a-ports-filter"),
        ],
    )
    def test_a_filter_that_does_not_name_the_ws_endpoint_holds_everything_back(self, sim, scope):
        """A filter that names the http endpoint only: the mode, the latency
        and the entry of `responses` do not reach a subscribe frame."""
        _set_scenario(
            sim,
            {"mode": "rate_limit", "latency_ms": 1500, "responses": {"eth_subscribe": {"mode": "down"}}, **scope},
        )

        with _websocket() as sock:
            started = time.monotonic()
            _send_frame(sock, _SUBSCRIBE)
            subscribed = _reply(sock)
            waited = time.monotonic() - started
            rows = _rows(sim)

        assert re.fullmatch(r"0x[0-9a-f]{32}", subscribed["result"]), subscribed
        assert waited < 1.0, f"the reply came after {waited:.2f} s"
        assert [_facts(row) for row in rows] == [("eth_subscribe", "success", 0, 7)]

    def test_each_subscribe_frame_uses_one_count_of_the_fail_first_n_window(self, sim):
        """With `fail_first_n` 2 on the ws endpoint, the first two subscribe
        frames get the fault and the third one succeeds."""
        with _websocket() as sock:
            _set_scenario(sim, {"mode": "rate_limit", "fail_first_n": 2, "then_mode": "success", "transports": ["ws"]})
            answers = []
            for frame_id in (1, 2, 3):
                _send_frame(sock, {"jsonrpc": "2.0", "method": "eth_subscribe", "params": ["newHeads"], "id": frame_id})
                answers.append(_payload(sock))
            rows = _rows(sim)

        assert answers[:2] == [_RATE_LIMIT_TEXT, _RATE_LIMIT_TEXT]
        assert re.fullmatch(r"0x[0-9a-f]{32}", json.loads(answers[2])["result"]), answers[2]
        assert [_facts(row) for row in rows] == [
            ("eth_subscribe", "rate_limit", 0, 1),
            ("eth_subscribe", "rate_limit", 0, 2),
            ("eth_subscribe", "success", 0, 3),
        ]
```

- [ ] **Step 3: Run the file**

Run: `lsof -nP -iTCP:19000 -sTCP:LISTEN`

Expected: no output.

Run: `python -m pytest tests/test_simulator_ws.py -q -p no:cacheprovider`

Expected: `139 passed`. That is 49 more cases.

- [ ] **Step 4: Run the checks on the file**

Run: `python -m black --check tests/test_simulator_ws.py`

Expected: `1 file would be left unchanged.`

Run: `python -m ruff check tests/test_simulator_ws.py`

Expected: `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add tests/test_simulator_ws.py
git commit -m "test(ws): record the rows of a subscribe frame and an unsubscribe frame"
```

---

### Task 3: A pushed event, and the connection that owns a subscription

**Files:**
- Modify: `tests/test_simulator_ws.py` (the end of the file)

**Interfaces:**
- Consumes: the constants and the helpers of Task 1.
- Produces: the classes `TestAPushedEvent` and `TestASubscriptionBelongsToOneConnection`.

- [ ] **Step 1: Add the two classes**

Append this to the end of `tests/test_simulator_ws.py`, after two empty lines:

```python
class TestAPushedEvent:
    """An event that the control API pushes with POST /ws/emit.

    `_WireSubscriptions.emit` puts the frame on the queue of the connection and
    writes the row. It asks no fault policy.
    """

    @pytest.mark.parametrize(
        "subscribe, row_method",
        [
            pytest.param("eth_subscribe", "eth_subscription push", id="eth"),
            pytest.param("subscribe", "tendermint_event push", id="tendermint"),
            pytest.param("accountSubscribe", "solana_account push", id="solana-account"),
            pytest.param("logsSubscribe", "solana_logs push", id="solana-logs"),
        ],
    )
    def test_a_pushed_event_writes_one_row_with_the_subscription_id_as_its_request_id(self, sim, subscribe, row_method):
        """One pushed event writes one `success` row. Its method is the
        envelope of the subscribe method and the word `push`. Its request id is
        the subscription id. It names the ws endpoint of the provider, and it
        carries no lava header, also when the upgrade request had some."""
        with _websocket(lava_headers=_LAVA_HEADERS) as sock:
            subscription_id = _subscribe(sock, subscribe)
            answer = _emit(sim, {"subscription_id": subscription_id, "event": {"tag": "A"}})
            _reply(sock)
            rows = _rows(sim)

        assert answer == (200, {"status": "emitted", "subscription_id": subscription_id})
        assert [_facts(row) for row in rows] == [
            (subscribe, "success", 0, 1),
            (row_method, "success", 0, subscription_id),
        ]
        assert _endpoint(rows[1]) == ("jsonrpc", "ws", _PORT)
        assert rows[1]["lava_headers"] == {}

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({"event": "text"}, id="a-text"),
            pytest.param({"event": 5}, id="a-number"),
            pytest.param({"event": ["a"]}, id="a-list"),
            pytest.param({"event": None}, id="null"),
            pytest.param({}, id="no-event-key"),
        ],
    )
    def test_an_event_that_is_no_object_is_pushed_as_an_empty_object(self, sim, body):
        """The control API accepts an event that is a text, a number, a list or
        `null`, and a body with no event. Each one reaches the caller as an
        empty object, and it writes its row."""
        with _websocket() as sock:
            subscription_id = _subscribe(sock)
            answer = _emit(sim, {"subscription_id": subscription_id, **body})
            frame = _reply(sock)
            rows = _rows(sim)

        assert answer == (200, {"status": "emitted", "subscription_id": subscription_id})
        assert frame == {
            "jsonrpc": "2.0",
            "method": "eth_subscription",
            "params": {"subscription": subscription_id, "result": {}},
        }
        assert _facts(rows[1]) == ("eth_subscription push", "success", 0, subscription_id)

    def test_an_event_for_an_unknown_subscription_gets_404_and_writes_no_row(self, sim):
        """An event for a subscription id that does not exist: the control API
        answers 404, and no row is written."""
        answer = _emit(sim, {"subscription_id": "0x" + "ab" * 16, "event": {"tag": "A"}})

        assert answer == (404, {"error": "no active subscription '0xabababababababababababababababab'"})
        assert _every_row(sim) == []

    def test_an_event_for_a_subscription_that_was_removed_gets_404_and_writes_no_row(self, sim):
        """After an unsubscribe frame removed the subscription, an event for
        its id gets 404 and writes no row."""
        with _websocket() as sock:
            subscription_id = _subscribe(sock)
            _send_frame(sock, {"jsonrpc": "2.0", "method": "eth_unsubscribe", "params": [subscription_id], "id": 2})
            _reply(sock)
            status, _ = _emit(sim, {"subscription_id": subscription_id, "event": {"tag": "A"}})
            rows = _rows(sim)

        assert status == 404
        assert [_facts(row)[0] for row in rows] == ["eth_subscribe", "eth_unsubscribe"]

    @pytest.mark.parametrize("mode", ["down", "hang", "rate_limit", "error", "drop_connection"])
    def test_an_event_reaches_its_subscriber_under_each_mode_of_the_provider(self, sim, mode):
        """A pushed event asks no fault policy. A fault that is set after the
        subscribe does not stop the event, and the row is a `success` row."""
        with _websocket() as sock:
            subscription_id = _subscribe(sock)
            _set_scenario(sim, {"mode": mode, "latency_ms": 1500, "transports": ["ws"]})
            started = time.monotonic()
            answer = _emit(sim, {"subscription_id": subscription_id, "event": {"tag": "A"}})
            frame = _reply(sock)
            waited = time.monotonic() - started
            rows = _rows(sim)

        assert answer[0] == 200
        assert frame["params"] == {"subscription": subscription_id, "result": {"tag": "A"}}
        assert waited < 1.0, f"the event came after {waited:.2f} s"
        assert _facts(rows[1]) == ("eth_subscription push", "success", 0, subscription_id)


class TestASubscriptionBelongsToOneConnection:
    """The registry that GET /ws/subscriptions shows, and the connection that
    owns each subscription."""

    def test_ws_subscriptions_shows_one_entry_for_each_subscribe_with_its_pool_and_its_provider(self, sim):
        """Each subscribe adds one entry. The entry names the subscription id,
        the pool, the provider id and the subscribe method."""
        with _websocket(_WS_PORTS["2"]) as sock:
            first = _subscribe(sock, "accountSubscribe", frame_id=1)
            second = _subscribe(sock, "eth_subscribe", frame_id=2)
            entries = _subscriptions(sim)

        assert sorted(entries, key=lambda entry: entry["method"]) == [
            {"subscription_id": first, "pool": "eth-sim", "pid": "2", "method": "accountSubscribe", "queue_depth": 0},
            {"subscription_id": second, "pool": "eth-sim", "pid": "2", "method": "eth_subscribe", "queue_depth": 0},
        ]

    def test_an_unsubscribe_from_another_connection_answers_false_and_removes_nothing(self, sim):
        """A connection cannot remove the subscription of another connection of
        the same endpoint. The owner still gets a pushed event, and the owner
        can remove the subscription."""
        with _websocket() as owner, _websocket() as other:
            subscription_id = _subscribe(owner)
            unsubscribe = {"jsonrpc": "2.0", "method": "eth_unsubscribe", "params": [subscription_id], "id": 9}

            _send_frame(other, unsubscribe)
            from_the_other = _reply(other)
            after_the_other = [entry["subscription_id"] for entry in _subscriptions(sim)]
            emit_status, _ = _emit(sim, {"subscription_id": subscription_id, "event": {"tag": "A"}})
            event = _reply(owner)

            _send_frame(owner, unsubscribe)
            from_the_owner = _reply(owner)
            after_the_owner = _subscriptions(sim)

        assert from_the_other == {"jsonrpc": "2.0", "id": 9, "result": False}
        assert after_the_other == [subscription_id]
        assert emit_status == 200
        assert event["params"] == {"subscription": subscription_id, "result": {"tag": "A"}}
        assert from_the_owner == {"jsonrpc": "2.0", "id": 9, "result": True}
        assert after_the_owner == []

    def test_a_close_removes_the_subscriptions_of_its_own_connection_only(self, sim):
        """When a connection closes, the simulator removes each subscription of
        that connection. A subscription of another connection stays."""
        with _websocket() as other:
            kept = _subscribe(other)
            with _websocket() as owner:
                _subscribe(owner, frame_id=1)
                _subscribe(owner, frame_id=2)
                assert len(_subscriptions(sim)) == 3

            deadline = time.monotonic() + 2.0
            while len(_subscriptions(sim)) != 1 and time.monotonic() < deadline:
                time.sleep(0.02)
            left = [entry["subscription_id"] for entry in _subscriptions(sim)]

        assert left == [kept]

    @pytest.mark.parametrize(
        "down_closes_it",
        [
            pytest.param(False, id="the-client-closes-the-socket"),
            pytest.param(True, id="a-down-provider-closes-the-connection"),
        ],
    )
    def test_a_connection_that_ends_with_no_close_frame_loses_its_subscriptions(self, sim, down_closes_it):
        """A connection can end with no close frame: the client closes the
        socket, or a `down` provider closes the connection. The simulator then
        removes each subscription of that connection."""
        sock = _request_upgrade()
        try:
            head = _read_the_handshake(sock)
            assert head.startswith(b"HTTP/1.1 101 "), f"the upgrade did not succeed: {head!r}"
            _subscribe(sock, frame_id=1)
            _subscribe(sock, frame_id=2)
            before = len(_subscriptions(sim))
            received = b""
            if down_closes_it:
                _set_scenario(sim, {"mode": "down", "transports": ["ws"]})
                _send_frame(sock, _BLOCK_NUMBER)
                received = _read_until_the_close(sock)
        finally:
            sock.close()

        deadline = time.monotonic() + 2.0
        while _subscriptions(sim) and time.monotonic() < deadline:
            time.sleep(0.02)

        assert before == 2
        assert received == b""
        assert _subscriptions(sim) == []
```

- [ ] **Step 2: Run the file**

Run: `lsof -nP -iTCP:19000 -sTCP:LISTEN`

Expected: no output.

Run: `python -m pytest tests/test_simulator_ws.py -q -p no:cacheprovider`

Expected: `160 passed`. That is 16 more cases of `TestAPushedEvent` and 5 of `TestASubscriptionBelongsToOneConnection`.

- [ ] **Step 3: Run the checks on the file**

Run: `python -m black --check tests/test_simulator_ws.py`

Expected: `1 file would be left unchanged.`

Run: `python -m ruff check tests/test_simulator_ws.py`

Expected: `All checks passed!`

- [ ] **Step 4: Commit**

```bash
git add tests/test_simulator_ws.py
git commit -m "test(ws): record the row of a pushed event and the owner of a subscription"
```

---

### Task 4: The frames that the subscribe code does not serve

**Files:**
- Modify: `tests/test_simulator_ws.py` (the end of the file)

**Interfaces:**
- Consumes: the constants and the helpers of Task 1.
- Produces: the class `TestFramesOutsideTheSubscribeCode`, and the complete test file.

- [ ] **Step 1: Add the class `TestFramesOutsideTheSubscribeCode`**

Append this to the end of `tests/test_simulator_ws.py`, after two empty lines:

```python
class TestFramesOutsideTheSubscribeCode:
    """Frames that the subscribe code does not serve: a frame that the adapter
    cannot read, and a frame that goes to `Listener.serve`."""

    @pytest.mark.parametrize(
        "opcode, payload",
        [
            pytest.param(ws_protocol.OPCODE_TEXT, b"this is not JSON", id="a-text-frame-that-is-not-json"),
            pytest.param(ws_protocol.OPCODE_TEXT, b"\xff\xfe\xfd", id="a-text-frame-that-is-not-utf-8"),
            pytest.param(
                ws_protocol.OPCODE_BINARY,
                b'{"jsonrpc": "2.0", "method": "eth_subscribe", "params": ["newHeads"], "id": 1}',
                id="a-binary-frame-with-a-subscribe-request",
            ),
        ],
    )
    def test_a_frame_that_the_adapter_cannot_read_gets_no_reply_and_no_row(self, sim, opcode, payload):
        """The adapter drops a text frame that is not JSON and a frame that is
        not text. It sends no reply and writes no row, and it registers no
        subscription. The connection stays open and answers the next frame."""
        with _websocket() as sock:
            sock.sendall(ws_protocol.encode_frame(opcode, payload, mask=True))
            _assert_no_frame(sock)
            rows_after_the_frame = _every_row(sim)
            subscriptions = _subscriptions(sim)
            _send_frame(sock, _BLOCK_NUMBER)
            next_reply = _reply(sock)
            rows = _rows(sim)

        assert rows_after_the_frame == []
        assert subscriptions == []
        assert next_reply["id"] == 8
        assert [_facts(row) for row in rows] == [("eth_blockNumber", "success", 0, 8)]

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(b"5", id="a-number"),
            pytest.param(b"null", id="null"),
            pytest.param(b'"eth_subscribe"', id="a-text"),
            pytest.param(b"{}", id="an-object-with-no-method"),
        ],
    )
    def test_a_json_frame_with_no_method_goes_to_the_request_flow(self, sim, payload):
        """A text frame that is JSON and names no method goes to the request
        flow. The eth chain answers it as the method `unknown`, and the row has
        that method."""
        with _websocket() as sock:
            sock.sendall(ws_protocol.encode_frame(ws_protocol.OPCODE_TEXT, payload, mask=True))
            reply = _reply(sock)
            rows = _rows(sim)
            subscriptions = _subscriptions(sim)

        assert reply == {"jsonrpc": "2.0", "id": 1, "result": "0x1"}
        assert [_facts(row) for row in rows] == [("unknown", "success", 0, 1)]
        assert subscriptions == []

    def test_a_list_of_requests_gets_the_batch_error_and_registers_no_subscription(self, sim):
        """A text frame that is a JSON list goes to the request flow, also when
        the list holds a subscribe request. The reply is the batch error, the
        row has the method `batch`, and no subscription is registered."""
        with _websocket() as sock:
            _send_frame(sock, [_SUBSCRIBE])
            reply = _reply(sock)
            rows = _rows(sim)
            subscriptions = _subscriptions(sim)

        assert reply == {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "batch requests are not supported"},
        }
        assert [_facts(row) for row in rows] == [("batch", "error", 0, None)]
        assert subscriptions == []

    @pytest.mark.parametrize(
        "block, expected_row",
        [
            pytest.param({"mode": "down", "latency_ms": 250}, ("*", "down", 0, None), id="down"),
            pytest.param({"mode": "hang", "latency_ms": 250}, ("eth_blockNumber", "hang", 0, 8), id="hang"),
        ],
    )
    def test_the_down_row_and_the_hang_row_of_another_frame_record_no_latency(self, sim, block, expected_row):
        """A frame such as `eth_blockNumber` goes to `Listener.serve`. Its `down`
        row has the method `*`, no request id and `latency_ms` 0, and its `hang`
        row records 0. The rows of a subscribe frame differ: the tests of
        `TestSubscribeAndUnsubscribeFrames` hold them."""
        with _websocket() as sock:
            _set_scenario(sim, {**block, "transports": ["ws"]})
            _send_frame(sock, _BLOCK_NUMBER)
            rows = _rows_when_complete(sim, 1)

        assert [_facts(row) for row in rows] == [expected_row]

    def test_a_per_method_down_with_a_latency_closes_another_frame_at_once(self, sim):
        """An entry of `responses` with `mode: down` and a latency, for a frame
        that goes to `Listener.serve`. The row records the method, the id and
        the latency. The WebSocket adapter closes the connection with no wait."""
        _set_scenario(
            sim,
            {
                "mode": "success",
                "transports": ["ws"],
                "responses": {"eth_blockNumber": {"mode": "down", "latency_ms": 1500}},
            },
        )

        with _websocket() as sock:
            started = time.monotonic()
            _send_frame(sock, _BLOCK_NUMBER)
            received = _read_until_the_close(sock)
            waited = time.monotonic() - started

        assert received == b""
        assert waited < 1.0, f"the connection closed after {waited:.2f} s"
        assert [_facts(row) for row in _rows(sim)] == [("eth_blockNumber", "down", 1500, 8)]

    @pytest.mark.parametrize(
        "method",
        [
            pytest.param(["eth_subscribe"], id="a-list"),
            pytest.param({"name": "eth_subscribe"}, id="an-object"),
        ],
    )
    def test_a_frame_whose_method_is_a_list_or_an_object_closes_the_connection(self, sim, method):
        """This test records a defect and does not judge it. A JSON frame whose
        `method` is a list or an object stops the reader of the connection. The
        caller gets no byte, and the connection closes. No row is written, and
        no subscription is registered."""
        with _websocket() as sock:
            _send_frame(sock, {"jsonrpc": "2.0", "method": method, "params": [], "id": 1})
            received = _read_until_the_close(sock)

        assert received == b""
        assert _every_row(sim) == []
        assert _subscriptions(sim) == []
```

- [ ] **Step 2: Run the file**

Run: `lsof -nP -iTCP:19000 -sTCP:LISTEN`

Expected: no output.

Run: `python -m pytest tests/test_simulator_ws.py -q -p no:cacheprovider`

Expected: `173 passed`. That is 13 more cases.

- [ ] **Step 3: Run the checks on the file**

Run: `python -m black --check tests/test_simulator_ws.py`

Expected: `1 file would be left unchanged.`

Run: `python -m ruff check tests/test_simulator_ws.py`

Expected: `All checks passed!`

Run: `python -m mypy tests/test_simulator_ws.py`

Expected: `Success: no issues found in 1 source file`

- [ ] **Step 4: Check the file against the checkpoint**

Run: `md5 -q tests/test_simulator_ws.py` (on Linux: `md5sum tests/test_simulator_ws.py`)

Expected: at the commit `21a48df`, the file has 2889 lines and the checksum `82d87881e7125ca31057e3479d33a60d`. Another value means that a code block of Tasks 1 to 4 did not arrive as it is written here: compare the file with the four code blocks before you go on. Later commits of the same pull request, from the plan of the WebSocket move, change the file again. So this checkpoint is valid for the file at `21a48df`.

- [ ] **Step 5: Commit**

```bash
git add tests/test_simulator_ws.py
git commit -m "test(ws): record the frames that the subscribe code does not serve"
```

---

### Task 5: Two runs in a row, and the proof that each new test can fail

**Files:** none in the repository. The script of the breaks and its output go into the evidence folder of the design, `handoffs/evidence-2026-10-07-one-request-flow-design/proof-runs-3a-pin-todays-websocket-rows/` of the automation checkout. Git ignores that folder.

**Interfaces:**
- Consumes: the branch after Task 4.
- Produces: the counts for the pull request, and the list of breaks for its body.

- [ ] **Step 1: Run the file two times in a row**

Run two times: `python -m pytest tests/test_simulator_ws.py -q -p no:cacheprovider`

Expected each time: `173 passed`. Record the two run times.

RAN on 2026-10-10 on a Mac: `173 passed`, two times in a row (40.65 s and 40.61 s). The file had the checksum `19233b2b7df87b97b4000b75085fe31a`, which is the file of the commit `dfe4b3b`. The definition of done of the ticket (Global Constraint 12) names three runs. Victoria chose two runs in a row, and not three.

- [ ] **Step 2: Decide if the proof of this plan holds**

The proof ran on 2026-10-10. All 118 breaks ran, one at a time. They ran on the test file of the commit `dfe4b3b` (checksum `19233b2b7df87b97b4000b75085fe31a`). They ran against `server.py`, `provider_simulator/listeners/base.py`, `provider_simulator/listeners/ws.py` and `provider_simulator/control_api.py` of `main` at `8ed08aa`. The control run with no break gave `113 passed, 60 deselected`. Each break made one case or more fail, and each of the 114 new cases failed in one run or more. 109 of the 118 runs are the runs of 2026-10-09 in the section "The breaks". The reviews added nine more: the last section of this plan names them.

The commit `21a48df` then changed two docstrings of the test file and no code. So the file of the checkpoint (Task 4, Step 4) has another checksum than the file of the proof.

- If the file has the checksum of Task 4, Step 4, and `main` did not move (Task 0, Step 2): the proof holds. The code of that file is the code of the proof, so do not run the breaks again.
- If a review changed a test, or `main` moved: run the breaks of the changed part again (Step 3).

- [ ] **Step 3: The rules for a run of the breaks**

The rules are in the skill `add-simulator-entity`, section "Proving a guard can fail, and the two ways a mutation lies to you". Break one source file in one place. Check first that the old text is in the file exactly as often as the break expects. Run with `PYTHONDONTWRITEBYTECODE=1`. Put the file back from a copy, and not with `git checkout`. Compare the checksum. Make the run on a copy of the worktree in a temporary folder, so that the worktree holds no broken file at any time.

A break that fails no test is not "equivalent" until every path to the changed line is read.

- [ ] **Step 4: Save the evidence**

Copy the script of the breaks, its log and its result file into the evidence folder that the head of this task names. The session that wrote this plan holds them as `mutate.py`, `mutation-run.log` and `mutation-results.json`. The final run of 2026-10-10 is in the subfolder `final-run-2026-10-10/` of that folder: `mutate.py`, `mutation-results.json`, `mutation-results-only.json` and the log `mutation-run-final.log`.

---

### Task 6: The whole suite, the checks of CI, and Linux

**Files:** none.

**Interfaces:**
- Consumes: the commits of Tasks 1 to 4.
- Produces: the proof that the new tests break no other test.

- [ ] **Step 1: Check that no other test run is active**

Run: `lsof -nP -iTCP:19000 -sTCP:LISTEN`

Expected: no output. Another session can run a timed test on the local k3d cluster. Ask it before a run of the whole suite, because the run loads the machine for about four minutes.

- [ ] **Step 2: Run the whole suite**

Run: `python -m pytest tests -q -p no:cacheprovider`

Expected: `1977 passed`. That is the 1863 tests of `main` at `8ed08aa` and the 114 new cases. No test fails and no test is skipped.

The job `test` of the pull request gave `1977 passed` on Linux on 2026-10-10.

- [ ] **Step 3: Run the three checks with the versions of CI**

Make a virtual environment in a temporary folder that holds these three tools only:

```bash
pip install 'black==26.5.1' 'ruff==0.15.20' 'mypy==2.2.0'
black --check .
ruff check .
mypy .
```

Expected: black prints "would be left unchanged" for every file. ruff prints `All checks passed!`. mypy prints `Success: no issues found`.

- [ ] **Step 4: Linux**

The new tests read sockets, and sockets behave differently on macOS and on Linux. So run the file one time on Linux before the push, in a container. A local image of the simulator holds Python 3.12 and the requirements. If the machine has no image `provider-simulator:local`, build it first: `docker build -t provider-simulator:local .`

Run this from the root of the worktree:

```bash
docker run --rm --user 0 -v "$PWD":/src:ro provider-simulator:local sh -c "cp -r /src /tmp/work && cd /tmp/work && pip install -q pytest==9.1.1 pytest-timeout==2.4.0 && python -m pytest tests/test_simulator_ws.py -q -p no:cacheprovider"
```

Expected: `173 passed`.

RAN on 2026-10-09 on the copy of `main`, with the code of Tasks 1 to 4 before the reviews. The image was `provider-simulator:request-id`, a local build of 2026-10-08 for linux/arm64. The result: Linux 6.12.68, Python 3.12.13, `167 passed in 41.16s`.

The job `test` of the pull request then runs the whole suite on `ubuntu-latest`. Open the log of a red check before you call it red. Three kinds of new test can differ on Linux: the tests that read a socket to its close, the tests with a time bound, and the one slow test.

---

### Task 7: The review of the branch

**Files:**
- Modify: `tests/test_simulator_ws.py`, only if the review finds a fault

**Interfaces:**
- Consumes: the branch after Task 6.
- Produces: the branch that the pull request shows.

- [ ] **Step 1: Ask for the review**

Use the skill `superpowers:requesting-code-review`. Give the reviewer this plan, the design sections of the head of this plan, and the diff of the branch against `main`. Ask two questions: does each test hold what its docstring says, and which behaviour of the three places of `server.py` has no test.

- [ ] **Step 2: Handle each finding**

Use the skill `superpowers:receiving-code-review`. Check each finding against the code before you change a test. Show the findings to Victoria, fix the faults, and commit the fix on this branch. For each changed or new test: three runs in a row, and a break of the source that makes it fail (Task 5, Step 3).

---

### Task 8: The pull request

**Files:** none.

**Interfaces:**
- Consumes: the branch of Tasks 0 to 7.
- Produces: the open pull request.

The push and the pull request need Victoria's go: ask "push and open the PR?". The merge needs its own go. Use the skill `pr`.

- [ ] **Step 1: Open the pull request**

The title: `test(ws): record today's WebSocket rows before the move into Listener.serve`. The command has `--label no-ticket` and `--assignee`. The body has five parts: Why; What changed; How to verify; What was broken to prove each test; Not in this pull request. The fourth part holds the counts of the section "The breaks" and names the kinds of break. The body names the ten choices of this plan and the table "The expected values that pull request 3b changes". It holds no ticket key and no path of one machine.

- [ ] **Step 2: Read the checks**

Run: `gh pr checks <number>`

Expected: `lint`, `test`, "Suite must pass before anything is published" and "Build and publish image" pass. The log of `test` must say `1977 passed` for the commits of this plan. The second step of the same pull request adds tests, and its plan gives the count after it. Open the log of a red check before you call it red. The job `comment-jira` must print that it found no Jira key.

- [ ] **Step 3: Before the merge**

Three things must be clean before Victoria merges: a Copilot review, the skill `can-i-merge`, and the tests. The Copilot review and the merge each need Victoria's go.

- [ ] **Step 4: After the merge**

Read `tests/test_simulator_ws.py` on `main`, and read the run "Lint and test" of the merge commit. Its job `test` must say `1977 passed` for the commits of this plan. The next step, the move of the WebSocket code, is in the same pull request, and its plan gives the count after both steps.

**Rollback:** revert this pull request. It holds tests only, so no behaviour changes with it or without it.

---

## The breaks

Each row is one run of 2026-10-09 on the copy of `main` at `8ed08aa`, with the new tests of that day. One source file was changed in one place, the new tests ran, and the file was put back from a copy. "Failed" is the count of new cases that failed, of the 107 cases that ran on that day. The one slow test ran only with the break of the wait of a hung upgrade, so that run had one case more.

109 runs of 2026-10-09 in all. Each one made one test or more fail. Each of the 108 new cases of that day failed in one run or more.

On 2026-10-10 all 118 breaks ran again, on the test file of the commit `dfe4b3b`: these 109 and nine more. The last section names the nine. The counts of the column "Failed" below are those of 2026-10-09. The evidence folder of the final run holds the counts of 2026-10-10 (Task 5, Step 4). Each break made one case or more fail, and each of the 114 new cases failed in one run or more.

**The upgrade request: `_WsHandler.do_GET` and `_WsHandler._refuse_upgrade` in `server.py`.** 32 runs.

| # | What was broken | Failed |
|---|---|---|
| 1 | The down row has the method ws_upgrade | 1 |
| 2 | Down answers 502 | 1 |
| 3 | The down body has another text | 1 |
| 4 | Rate_limit writes no row | 4 |
| 5 | The rate_limit body has another text | 12 |
| 6 | Rate_limit answers 503 | 12 |
| 7 | Error answers the http_status of the scenario | 3 |
| 8 | The error body is the error code | 3 |
| 9 | The error row has the status rate_limit | 3 |
| 10 | Hang does not wait | 2 |
| 11 | The hang row has the method * | 1 |
| 12 | The drop row has the status drop | 4 |
| 13 | A drop after_headers sends nothing | 1 |
| 14 | A drop mid_body sends fewer bytes | 1 |
| 15 | A success writes a row | 70 |
| 16 | The 101 reply names the provider in another form | 15 |
| 17 | The 404 body has another text | 2 |
| 18 | A wrong path writes a row | 2 |
| 19 | The fault policy comes before the protocol checks | 4 |
| 20 | The 400 body of a request that is no upgrade has another text | 2 |
| 21 | A request that is no upgrade writes a row | 2 |
| 22 | It waits for latency_ms | 2 |
| 23 | The row records latency_ms | 1 |
| 24 | A corruption changes the body of the refusal | 6 |
| 25 | A corruption refuses the upgrade | 12 |
| 26 | It reads a per-method override | 2 |
| 27 | It uses no count of the fail_first_n window | 1 |
| 28 | The row has no lava headers | 11 |
| 29 | The row names the port 0 | 11 |
| 30 | Rate_limit answers the rate_limit_body of the scenario | 1 |
| 31 | It ignores the filters | 5 |
| 32 | It performs the pause | 1 |

**A subscribe frame: `_WsHandler._serve_subscription_frame` in `server.py`.** 41 runs.

| # | What was broken | Failed |
|---|---|---|
| 33 | The down row has the method * | 2 |
| 34 | The down row records the latency 0 | 2 |
| 35 | The down row has no request id | 2 |
| 36 | Down waits for the latency | 2 |
| 37 | The hang row records the latency 0 | 1 |
| 38 | Each fault row records the latency 0 | 3 |
| 39 | A fault row has no request id | 16 |
| 40 | A fault row has the method * | 18 |
| 41 | The success row has the method * | 25 |
| 42 | The success row records the latency 0 | 3 |
| 43 | The success row has no request id | 23 |
| 44 | The row is written after the wait | 2 |
| 45 | The subscription is registered after the wait | 2 |
| 46 | The reply does not wait for the latency | 3 |
| 47 | The down check comes before the per-method override | 2 |
| 48 | The override reads error_probability | 1 |
| 49 | The override does not read latency_ms | 2 |
| 50 | The override does not read drop_at | 1 |
| 51 | The override does not read error_code | 1 |
| 52 | The override does not read error_message | 1 |
| 53 | The override does not read mode | 5 |
| 54 | A canned body is applied | 1 |
| 55 | A corruption changes the success reply | 6 |
| 56 | A corruption does not change a fault reply | 2 |
| 57 | The rate_limit row has the status error | 5 |
| 58 | The drop row has the status drop | 5 |
| 59 | The hang row has the status error | 2 |
| 60 | The error row has the status failed | 6 |
| 61 | The error reply does not carry the id of the frame | 5 |
| 62 | A subscription is registered before the fault check | 38 |
| 63 | The filters are ignored | 2 |
| 64 | It uses no count of the fail_first_n window | 1 |
| 65 | The row has no lava headers | 2 |
| 66 | The row names the port 0 | 2 |
| 67 | The reply has the id 1 | 9 |
| 68 | The subscription id has 16 hex characters | 19 |
| 69 | The registry gets the method eth_subscribe for each method | 4 |
| 70 | The registry gets another provider id | 14 |
| 71 | It performs the pause | 1 |
| 72 | A result of responses is the reply | 2 |
| 73 | An error of responses is the reply | 2 |

**A frame of each kind: `_WsHandler._reader_loop` and `_WsHandler._perform_frame` in `server.py`.** 10 runs.

| # | What was broken | Failed |
|---|---|---|
| 74 | A drop after_headers declares 99 bytes | 1 |
| 75 | A drop mid_body sends 49 bytes | 2 |
| 76 | A hang closes the connection | 2 |
| 77 | A text frame that is not JSON gets a reply | 2 |
| 78 | A text frame that is not JSON goes to the listener | 2 |
| 79 | A binary frame is read as a text frame | 1 |
| 80 | The first request of a list goes to the subscribe code | 1 |
| 81 | A per-method down waits for the latency | 1 |
| 82 | A frame with no method goes to the subscribe code | 5 |
| 83 | The method of a frame that is no object is read | 4 |

**An unsubscribe frame, and the close of a connection: `_serve_subscription_frame` and `_reader_loop` in `server.py`.** 5 runs.

| # | What was broken | Failed |
|---|---|---|
| 84 | A connection removes the subscription of another connection | 1 |
| 85 | It answers true and removes nothing | 7 |
| 86 | The subscriptions of the connection stay | 1 |
| 87 | Every subscription of the simulator is removed | 1 |
| 88 | The reply is always true | 3 |

**A pushed event: `_WireSubscriptions.emit` in `server.py`.** 11 runs.

| # | What was broken | Failed |
|---|---|---|
| 89 | The row has the envelope as its method | 13 |
| 90 | The row has no request id | 13 |
| 91 | The row carries a lava header | 4 |
| 92 | The row names the port 0 | 4 |
| 93 | The row has the status pushed | 13 |
| 94 | The row records the latency 1 | 13 |
| 95 | The row names the transport http | 13 |
| 96 | No row is written | 13 |
| 97 | An event that is no object is sent as it is | 5 |
| 98 | It asks the mode of the provider | 4 |
| 99 | It waits for the latency | 4 |

**The control route `POST /ws/emit`: `ControlApi.ws_emit` in `provider_simulator/control_api.py`.** 2 runs.

| # | What was broken | Failed |
|---|---|---|
| 100 | The 404 body has another text | 1 |
| 101 | An unknown subscription gets 200 | 2 |

**The registry: `WsSubscriptions.list` in `provider_simulator/listeners/ws.py`.** 4 runs.

| # | What was broken | Failed |
|---|---|---|
| 102 | An entry has the pool as its pid | 1 |
| 103 | An entry has the method eth_subscribe for each method | 1 |
| 104 | An entry has the queue depth 1 | 1 |
| 105 | An entry has no pool | 1 |

**The request flow: `Listener.serve` in `provider_simulator/listeners/base.py`.** 4 runs.

| # | What was broken | Failed |
|---|---|---|
| 106 | The down row records the latency | 1 |
| 107 | The down row has the method unknown | 2 |
| 108 | The hang row records the latency | 1 |
| 109 | The row of a per-method down records the latency 0 | 1 |

## Not in this plan

1. The next step, 3b of the design: the move of the subscribe frames into `Listener.serve` and of the row writers into `provider_simulator/listeners/`. It goes into the same pull request, and it has a plan file of its own, `2026-10-10-websocket-frames-use-listener-serve.md`. The guard test of section 8.4 of the design belongs to that step.
2. A change of a source file. A fault that a test of this plan shows is recorded as it is.
3. A change of the automation repository. Pull request 3a needs none: it changes no behaviour, so no skill page becomes untrue.
4. A run of the automation suites (choice 10).
5. The three edits of the design document that wait for Victoria's word: its status line, the rows A, B and C of section 9.2, and the limits of a per-method fault.
6. The behaviours that a test of `main` holds. The section "Spec coverage" names each one.
7. A test of `http_status` in a per-method override of a subscribe method. A WebSocket frame has no HTTP status, so the key changes nothing that a test can read (section 9.2 of the design).
8. A test of the queue of a connection that is full, and of an event for a provider that the registry does not hold. No test can reach either state through the control API.
9. A second test class for the frames that go to `Listener.serve`. The classes `TestPostHandshakeFaults`, `TestCorruptionModes` and `TestWsPerMethodFaultOverrides` of `main` hold their replies. This plan adds their rows only where a row of a subscribe frame differs (Task 4).

## Spec coverage

"On `main`" means: a test of `main` at `8ed08aa` holds it, and this plan adds no test for it.

| Requirement of the design | Where it is held |
|---|---|
| Section 12, 3a: the row of a refused upgrade, for each fault: `down`, `rate_limit`, `error`, `hang` and `drop_connection` | Task 1: `test_a_refused_upgrade_gets_its_status_and_its_body_and_writes_one_row` (`down`, `rate_limit` and `error`), `test_a_hung_upgrade_gets_no_byte_and_writes_one_row` and `test_a_dropped_upgrade_gets_the_bytes_of_its_drop_point_and_writes_one_row` |
| Section 12, 3a: the reply body of a refused upgrade | Task 1: the same three tests. The body is compared as JSON, and the bytes of a drop point byte for byte |
| Section 12, 3a: no row for an upgrade that succeeds | Task 1: `test_an_upgrade_that_succeeds_gets_the_101_reply_and_writes_no_row` |
| Section 12, 3a: the row of a pushed event: method, status and request id | Task 3: `test_a_pushed_event_writes_one_row_with_the_subscription_id_as_its_request_id`, one case for each of the four envelopes |
| Section 12, 3a: the rows of a subscribe frame and an unsubscribe frame: method, status and request id | Task 2: `test_each_frame_writes_one_success_row_with_its_method_and_its_request_id`, `test_each_subscribe_method_and_each_unsubscribe_method_writes_a_row_with_its_own_name`, and the row of each fault test. For an unsubscribe frame under `error`: `test_an_unsubscribe_frame_of_a_provider_with_an_error_removes_nothing` |
| Section 12, 3a: a frame that is not JSON gets no reply and no row | Task 4: `test_a_frame_that_the_adapter_cannot_read_gets_no_reply_and_no_row` |
| Section 12.1, item 1: a refused upgrade: its HTTP status (503, 429, 400), its row with the method `ws_upgrade` and its status, and the forms of `drop_at` | Task 1, as above. On `main`: the three statuses, in `TestPreHandshakeFaults` |
| Section 12.1, item 2: the row of a pushed event, the row of a subscribe frame, and a frame that is not JSON | Tasks 3, 2 and 4, as above |
| Section 12.1, item 3: one entry of `/ws/subscriptions` for each subscribe, with its pool and its provider id | Task 3: `test_ws_subscriptions_shows_one_entry_for_each_subscribe_with_its_pool_and_its_provider` |
| Section 12.1, item 4: a `ws` row keeps `transport`, `port` and `method`, and a frame row carries the lava headers of the upgrade | Task 1: the three tests of a refused upgrade. Task 2: `test_each_frame_writes_one_success_row_with_its_method_and_its_request_id` and the test of a `down` provider. Task 3: the test of a pushed event. Each one compares `_endpoint(row)` and `lava_headers`. On `main`: `TestLavaHeaderCapture`, for a frame that goes to `Listener.serve` |
| Section 12.1, item 5: the arrival row of an `http` call is written before the body is read | On `main`: `tests/test_simulator.py`, `test_rst_before_body_arrives_still_records_history`, `test_rst_before_body_does_not_double_record` and `test_arrival_stub_carries_lava_headers`. They must pass with no edit in 3b |
| Section 12.1, item 6: `WsSubscriptions()` with no argument | On `main`: the control API of `tests/test_control_api_cache.py`, `tests/test_cache_sim_wire.py` and `tests/test_resp_control.py` is built with it |
| Section 12.1, item 7, and section 14.6, point 2: a subscription belongs to one connection | Task 3: `test_an_unsubscribe_from_another_connection_answers_false_and_removes_nothing`, `test_a_close_removes_the_subscriptions_of_its_own_connection_only` and `test_a_connection_that_ends_with_no_close_frame_loses_its_subscriptions` |
| Section 4.4, row 2: a subscribe frame reads five fault keys of a per-method override: `mode`, `latency_ms`, `drop_at`, `error_code` and `error_message` | Task 2. `mode`: `test_a_per_method_rate_limit_reaches_the_subscribe_frame_only`, `test_a_per_method_hang_gives_no_reply_to_the_subscribe_frame`, and the tests of the next two keys, which set `down` and `drop_connection`. `latency_ms`: `test_a_per_method_down_with_a_latency_closes_at_once_and_its_row_records_the_latency` and `test_a_per_method_latency_delays_the_subscribe_frame_only`. `drop_at`: `test_a_per_method_drop_point_reaches_the_subscribe_frame`. `error_code` and `error_message`: `test_a_per_method_error_code_and_message_reach_the_subscribe_frame_only`. A key that is not read: `test_a_per_method_error_probability_is_not_read_for_a_subscribe_frame` |
| Section 9.2, rows 3 to 6: today's side | Task 2: the table "The expected values that pull request 3b changes" |
| Section 9.3, difference 8: the upgrade applies no `latency_ms`, and its rows record 0 | Task 1: `test_a_refused_upgrade_does_not_wait_for_latency_ms` and `test_an_upgrade_that_succeeds_does_not_wait_for_latency_ms` |
| Section 9.3, difference 9: the upgrade applies no corruption and no per-method override | Task 1: `test_the_upgrade_applies_no_corruption` and `test_the_upgrade_reads_no_per_method_override` |
| Section 9.3, difference 10: a refused upgrade writes one complete row, and an upgrade that succeeds writes none | Task 1: each refusal test requires exactly one row |
| Section 9.3, difference 11: the upgrade has its own reply bodies | Task 1: the bodies `{"error": "provider down"}`, `{"error": "rate limited"}` and `{"error": <error_message>}` |
| Section 9.3, difference 12: each upgrade uses one count of the `fail_first_n` window | On `main`: `TestWsSequencedFaults`. Task 1: `test_each_upgrade_uses_one_count_of_the_fail_first_n_window`, with the rows |
| Section 8.4: the adapter answers a wrong path and a bad upgrade request itself, with no listener and no row | Task 1: `test_a_request_for_another_path_gets_404_and_writes_no_row` and `test_a_request_with_no_upgrade_headers_gets_400_and_writes_no_row` |
| Section 8.4: after 3b the adapter gives each text frame that is JSON to the listener | Task 4: `test_a_json_frame_with_no_method_goes_to_the_request_flow` and `test_a_list_of_requests_gets_the_batch_error_and_registers_no_subscription` hold what such a frame gets today when it names no subscribe method. `test_a_frame_whose_method_is_a_list_or_an_object_closes_the_connection` holds a frame whose `method` is a list or an object: today the connection closes, and no row is written |
| Section 14.6, the column U of the table: the fields that the upgrade applies | Task 1. These apply: `error_probability`, `error_message` and `drop_at` (the cases of the refusal test and of the drop test), the filters (`test_a_filter_that_does_not_name_the_ws_endpoint_leaves_the_upgrade_alone`) and `fail_first_n`. These do not: `latency_ms`, `error_code`, `http_status` and `rate_limit_body` (cases of the refusal test, and the two latency tests), `responses`, the corruption, and the pause (`test_the_upgrade_performs_no_pause`) |
| Section 14.6, the column F of the table: the fields that a subscribe frame applies | Task 2. These apply: `mode`, `latency_ms`, a provider-wide `error_probability`, `error_code`, `error_message`, `rate_limit_body`, the five fault keys of `responses`, the corruption of a fault reply, `drop_at`, `fail_first_n` (`test_each_subscribe_frame_uses_one_count_of_the_fail_first_n_window`) and the filters (`test_a_filter_that_does_not_name_the_ws_endpoint_holds_everything_back`). These do not: a per-method `error_probability`, the content keys of `responses` (`test_a_content_key_of_responses_is_not_read_for_a_subscribe_frame`), the corruption of a success reply, and the pause (`test_a_subscribe_frame_performs_no_pause`) |
| The handoff of 2026-10-09: the `down` row and the `hang` row of a subscribe frame record the configured latency and the method | Task 2: `test_a_subscribe_frame_of_a_down_provider_closes_the_connection_and_its_row_names_the_frame` and `test_a_subscribe_frame_of_a_hung_provider_gets_no_reply_and_the_connection_stays_open` |
| The handoff of 2026-10-09: a frame under a per-method `down` with a latency: the row records the latency, and the adapter closes with no wait | Task 2: `test_a_per_method_down_with_a_latency_closes_at_once_and_its_row_records_the_latency`. Task 4: `test_a_per_method_down_with_a_latency_closes_another_frame_at_once` |

## What the reviews changed after this plan was written

The code blocks and the counts of this plan now hold the changes that the reviews made after this plan was written. Four task reviews and one review of the whole branch found no test that was wrong about today's behaviour. They found checks that could not fail, and texts that the code did not support. They also found behaviours with no test, and helpers whose failure did not show what arrived. The fixes made these changes:

1. **Seven tests started to read `GET /ws/subscriptions` while the connection was open.** `_reader_loop` removes each subscription of a connection when that connection closes. So a read after the close could not see a subscription that a fault registered. The five tests of Task 2: `test_each_subscribe_method_and_each_unsubscribe_method_writes_a_row_with_its_own_name`, `test_a_subscribe_frame_of_a_rate_limited_provider_gets_the_text_of_the_rate_limit`, `test_a_subscription_frame_of_a_provider_with_an_error_gets_the_error_reply`, `test_a_per_method_rate_limit_reaches_the_subscribe_frame_only` and `test_a_per_method_hang_gives_no_reply_to_the_subscribe_frame`. The two tests of Task 4: `test_a_json_frame_with_no_method_goes_to_the_request_flow` and `test_a_list_of_requests_gets_the_batch_error_and_registers_no_subscription`. In three tests the simulator closed the connection itself, and their read stayed after the close.
2. **The class of the upgrade tests got the name `TestTheUpgradeRequest`.** On 2026-10-09 this plan said `TestARefusedUpgrade`. Victoria chose the new name on 2026-10-10: nine of its fourteen tests do not hold a refusal by a fault.
3. **The reviews added three tests and one case.** On 2026-10-09 the new part had 53 test functions and 108 cases. After the reviews it has 56 and 114.
   - `test_an_unsubscribe_frame_of_a_provider_with_an_error_removes_nothing`, in `TestSubscribeAndUnsubscribeFrames`. The unsubscribe case of the error test named an id that no connection held, so it could not show a removal.
   - `test_a_connection_that_ends_with_no_close_frame_loses_its_subscriptions`, two cases, in `TestASubscriptionBelongsToOneConnection`: the client closes the socket, and a `down` provider closes the connection.
   - `test_a_frame_whose_method_is_a_list_or_an_object_closes_the_connection`, two cases, in `TestFramesOutsideTheSubscribeCode`. It records a defect of `main` and does not judge it: `server.py` raises `TypeError` for such a frame before it writes a row. Victoria chose on 2026-10-10 to record it as it is.
   - The case `error` of `test_an_event_reaches_its_subscriber_under_each_mode_of_the_provider`.
4. **The helpers that fail began to show what arrived.** `_read_until_the_close`, `_refusal`, `_payload`, `_reply`, `_assert_no_frame` and `_subscribe` got a failure text that holds the bytes or the reply. `_payload` also got a check for a text frame, so each test that reads a reply holds the frame type. `_refusal` got a close of its socket in each case.
5. **`test_a_hung_upgrade_gets_no_byte_and_writes_one_row` read its row after a fixed time.** A review changed it to `_rows_when_complete`.
6. **Texts that were not exact.** The reviews corrected the comment at the head of the new part. It names three places that write a history row, and two of them decide a fault. The reviews corrected the docstring of the dropped upgrade to 48 bytes. Task 1 of this plan said 49 first, and a review corrected the plan in place. The reviews changed four more docstrings to say only what their test holds:
   - The row of the other frame, in the test of a per-method latency.
   - The kinds of event that are no object.
   - The frame that goes to `Listener.serve`, with the class that holds the rows of a subscribe frame.
   - The row that `_refuse_upgrade` writes.

   The reviews also split eight sentences of more than 25 words.
7. **The counts rose with the new tests.** The file gave `173 passed`: the 59 tests of `main` and the 114 new cases. The whole suite gave `1977 passed`.
8. **The breaks.** The section "The breaks" holds the 109 runs of 2026-10-09 on the code blocks before the reviews. The reviews added nine breaks. Four are for the tests of item 1, and five are for the tests and the frame-type check of items 3 and 4. On 2026-10-10 all 118 ran on the test file of the commit `dfe4b3b`. Each one made one case or more fail, and each of the 114 cases failed in one run or more. The commit `21a48df` changed two docstrings after that run, and no code.
