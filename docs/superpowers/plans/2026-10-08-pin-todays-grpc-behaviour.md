# Pin Today's gRPC Behaviour Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tests record what a gRPC call of the simulator gets today, so that pull request 2b can move gRPC into `Listener.serve` and show that no reply and no history row changed.

**Architecture:** Tests only, in three files. A grid of `GrpcListener.plan` results, with no socket, in `tests/test_listener_grpc.py`. Tests through a real gRPC client and the control API in `tests/test_simulator_grpc.py`. Three tests of what `SimulatorServer.start()` wires, in the new file `tests/integration/test_server_start_wiring.py`. No source file changes.

**Tech Stack:** Python 3.12, pytest 9.1.1 with pytest-timeout 2.4.0, `grpcio` and `grpcio-reflection` 1.81.1 (`grpc.aio`), the compiled protobuf stubs in `cosmos_pb2/`. The checks of CI: black 26.5.1, ruff 0.15.20 and mypy 2.2.0.

**Spec:** `docs/superpowers/specs/2026-10-06-one-request-flow-design.md`, version 2.3, on the branch `docs/one-request-flow-design` (pull request 137 of `provider-simulator`): sections 8.3, 9.3, 10 ("Step 2", pull request 2a), 12, 12.1 (the list "2a, gRPC" and the paragraph "The shared wiring of `server.py`") and 14.6. The decision record `docs/adr/001-one-request-flow.md`, decision 6. This plan is pull request 2a of that design: "tests that pin today's gRPC behaviour". The ticket of the whole design is MAG-4216, and its definition of done holds for this pull request.

## The state of this plan

Victoria gave her go for this plan on 2026-10-08. Then she ordered: "work in worktree". So the tests of Tasks 1 to 3 were written and run in the worktree of this pull request while the plan was written. Then she ordered: "open pr". So the branch is pushed and the pull request is open. Then she ran the skill `can-i-merge` on it, and the review found faults: Task 6 has them, and one more commit holds the fixes. The diffs of Tasks 1 to 3 below show the files after that commit:

| | |
|---|---|
| Worktree | `.claude/worktrees/pin-todays-grpc-behaviour` of the simulator checkout |
| Branch | `pin-todays-grpc-behaviour`, made from `main` at `6f4e933`. Pushed on 2026-10-08, with four commits. |
| Task 1 | Done: commit `3120509` |
| Task 2 | Done: commit `481e310` |
| Task 3 | Done: commit `c5fd800` |
| Task 4 | Done. The whole suite: `1774 passed` on macOS and on Linux. The three checks of CI pass on the whole branch. |
| Task 5 | Done. Victoria merged pull request 140 on 2026-10-08 at 18:24 IDT: the commit `273429e` of `main`. A Copilot review of the commit `e1b4cb0` gave no finding. The body of the pull request on GitHub is the body of the first push: it holds the counts from before the review (148 cases, `1759 passed`), and one sentence that the review showed to be untrue, that the default moniker of the reply builder is never used. This plan has the true counts. |
| Task 6 | Done: commit `e1b4cb0`, the fixes of the review |

Each task below holds the code as the diff of its commit against `main`. A diff applies to `main` at `6f4e933` with `git apply`.

## What is proven, and how

Each line says who did the check and how. "RUN" means: run by the author of this plan on 2026-10-08, in the worktree above, on macOS (Darwin 25.3), with Python 3.12.12, `grpcio` 1.81.1 and pytest 9.1.1.

| Fact | How it is known |
|---|---|
| The baseline. On `main` at `6f4e933` the suite has 1611 tests, and all pass on Linux. | READ from the log of the CI run 37777894721 of `main` ("Lint and test", job `test`, Python 3.12.15): `1611 passed in 173.17s`. |
| The four gRPC test files pass on `main` before this change: `tests/test_listener_grpc.py`, `tests/test_simulator_grpc.py`, `tests/test_simulator_grpc_port_closed.py`, `tests/test_fault_policy.py`. | RUN: `170 passed`. |
| Each new test passes on `main` with no code change, three times in a row. | RUN: the three test files together, three times in a row, on the committed files: `256 passed` each time (41.05 s, 41.62 s and 41.11 s). That is 114 tests in `tests/test_listener_grpc.py`, 139 in `tests/test_simulator_grpc.py` and 3 in `tests/integration/test_server_start_wiring.py`. |
| Each new test fails when the thing that it tests is broken. | RUN: 151 runs, one break each, on the committed files: 74 for `tests/test_listener_grpc.py`, 65 for `tests/test_simulator_grpc.py` and 12 for `tests/integration/test_server_start_wiring.py`. Each run made one test or more fail. Each of the 163 new cases failed in one run or more. The section "The mutations" has the list. |
| The whole branch passes `black --check .`, `ruff check .` and `mypy .`. | RUN on 2026-10-08 at 18:06 IDT on the files of the commit `e1b4cb0`, with black 26.5.1, ruff 0.15.20 and mypy 2.2.0 in a virtual environment that holds these three tools only: black `106 files would be left unchanged.`, ruff `All checks passed!`, mypy `Success: no issues found in 106 source files`. The job `lint` of CI passed on the same commit. |
| The whole suite with the change. | RUN on 2026-10-08 from 18:06:34 to 18:09:54 IDT, on the commit `e1b4cb0`: `python -m pytest tests -q -p no:cacheprovider` gave `1774 passed in 199.83s`. The run of the first push, at `c5fd800`, gave `1759 passed in 200.05s`. |
| Linux. | RUN BY CI on the commit `e1b4cb0` (`ubuntu-latest`, run 37798500935, job `test`): `1774 passed in 210.28s`. The job "Suite must pass before anything is published" of the same commit passed too. Each of the 163 new cases has the word PASSED in the log, the HTTP/2 frame tests and the tests with a second simulator too. The first push, at `c5fd800`, gave `1759 passed in 203.81s` (run 37789966082). |

## Global Constraints

1. Tests only. No file outside `tests/` changes.
2. Each new test passes on `main` with no code change.
3. No test that exists is edited: no assertion, no name, no body. The two test files that exist get new helpers and new tests.
4. Each expected value is written out by hand. In pull request 2b a pinned value that fails is a row for section 9.2 of the design that Victoria must accept, or it is a defect of 2b. It is not edited to pass.
5. No pull request of the design edits a test file of the cache simulator or of the RESP proxy (section 12 of the design). So the three wiring tests are in a new file. A new test file is not put directly into `tests/`: Victoria ordered that on 2026-10-08, and she approved the new package `tests/integration/` for this file.
6. Linux decides. CI runs on `ubuntu-latest`. A green run on a Mac does not prove a socket test.
7. A test that starts a simulator of its own uses fixed ports below 32768, in a block that no other test file uses. `tests/test_simulator_grpc.py`: 28721, 28722 and 29721. `tests/integration/test_server_start_wiring.py`: 28731, 28735, 29731 and 29735 for the RESP test; 28741, 28742, 28745 and 29741 for the test with no `grpcio`.
8. The simulator tests bind fixed local ports. Run one pytest process at a time on a machine. Before a run, `lsof -nP -iTCP:19000 -sTCP:LISTEN` must print nothing.
9. The checks of CI: `black --check .`, `ruff check .`, `mypy .`. Line length 120.
10. A commit has a conventional subject, `<type>(<scope>): <summary>`. It has no `Co-Authored-By` line. It does not name an agent configuration file. Stage each file by its name. Never use `git add -A`.
11. Every comment, docstring, document line and commit message of this plan is in ASD-STE100 Simplified Technical English.
12. The branch has a name for the behaviour, and the pull request has the label `no-ticket`. No ticket key is in the branch name, the title or the body: in this repository a key in one of them posts a Jira comment, and a `#Closes` tag moves the ticket of all eight pull requests to Testing.
13. The definition of done of MAG-4216 for a pull request: each new test runs three times in a row with the same result; each new test is shown to fail when the thing that it tests is broken, and the pull request says what was broken; the count of what ran is recorded.

## Review Focus

Five conditions that the design implies and that a plain gRPC client test does not reach. Each one has its test in the task that owns it.

1. **A behaviour that no gRPC client can read.** For the drop points `after_headers` and `mid_body` the adapter sends the initial metadata before the status. A client reads the same status for each drop point. Tests: `TestGrpcDropPoint` (Task 2) reads that status with a real client, and it writes one call as HTTP/2 frames and reads the frames that come back.
2. **A status that comes after 30 seconds.** A hung call ends with `CANCELLED` and "hang timeout" only after the adapter held it for 30 seconds. Test: `test_a_hung_call_ends_after_30_seconds_with_cancelled_and_its_text` (Task 2). It is the one slow test of this plan.
3. **A call that reaches no listener.** A method that is not served, a service that is not registered, and a request that is no message are answered by the gRPC library. They must write no history row and no count. Tests: `TestGrpcCallsThatReachNoListener` (Task 2).
4. **A gRPC endpoint that does not start.** The check of the servicers ends one thread. The simulator must stay up and must not report ready. Test: `test_an_endpoint_that_is_refused_leaves_the_simulator_up_and_not_ready` (Task 2).
5. **A row that a test reads while the provider still waits.** The row must be complete before the latency wait. Tests: `test_the_row_is_complete_when_plan_returns` (Task 1) and `test_the_row_of_a_call_is_complete_while_the_provider_still_waits` (Task 2).

## File Structure

Two files are created: the wiring tests, and the empty `tests/integration/__init__.py` of their package. Two files change.

| File | Its part in this change |
|---|---|
| `tests/test_listener_grpc.py` | The grid: what `GrpcListener.plan` decides for each mode, each corruption mode and each per-method override, with no socket: the status, its text, the wait and the history row. It holds no content of a reply message. One function, `_decide`, calls `plan` and reads a `GrpcPlan`. |
| `tests/test_simulator_grpc.py` | What a real gRPC client reads: the status and the text of each fault, the waits, the per-method errors, the fields of the replies, the calls that reach no listener, and the drop points. |
| `tests/integration/test_server_start_wiring.py` (new) | What `SimulatorServer.start()` wires for the cache simulator, for the RESP proxy, and on a machine with no `grpcio`. |

## Choices of this plan that the design does not fix

Victoria can change each one.

1. **One test takes 30 seconds.** `test_a_hung_call_ends_after_30_seconds_with_cancelled_and_its_text` waits for the status of a hung call. The adapter holds a hung call for 30 seconds, and no test of today reads what comes after: `test_hang_times_out_client` stops at a client deadline of 2 seconds. Pull request 2b rewrites the function that performs the hang. The cost is 30 seconds for each run of the suite, which takes 173 seconds in CI today. The other way: keep the text in the grid only, which Task 1 has too, and leave the socket path of the hang with no test.
2. **A known fault is recorded as it is.** `test_a_result_override_of_a_wrong_type_ends_the_call_after_the_row_says_success`: a `result` override with a `height` that is no number makes `build_latest_block` raise after the row says `success`, and the caller gets `UNKNOWN`. `build_all_balances` got a fix for this fault in step 1. `build_latest_block` and `build_node_info` did not. This pull request holds tests only, so it records the fault and does not fix it. The first form of this choice proposed the fix after 2b. The review showed that this order does not work: in 2b the row of that call changes (Task 6, step 3, point 1). The decision is Victoria's, and my proposal is now a small pull request for the two builders before 2b.
3. **`latency_ms` under a filter that does not name the endpoint has one test beside the grid.** Today gRPC delays such a call. Pull request 2b changes that on purpose (section 9.2 of the design, row 2), and section 10 says that no expected value of the grid changes in 2b. So the grid holds the filters for the mode and for the corruption, and it holds no latency with a filter. One test beside the grid holds today's delay: `test_latency_ms_is_paid_today_under_a_filter_that_does_not_name_the_endpoint`. Pull request 2b edits its two expected values. The first form of this choice had no such test, and the review showed that the change of 2b was then not visible in any test of 2a.
4. **The drop points are read as HTTP/2 frames.** A gRPC client reads the same thing for each of the three drop points: `UNAVAILABLE`, "connection dropped" and no metadata (RUN with a `grpc.aio` client). So `tests/test_simulator_grpc.py` gets a helper, `_frames_of_one_call`, that writes one call as HTTP/2 frames with the standard library and reads the frames that come back. Two control tests show that the helper reads frames: a reply message gives `HEADERS`, `DATA`, `HEADERS`, and a status with no drop point gives one frame.
5. **Sixteen status names.** `test_an_error_stub_gives_the_status_of_its_name_with_the_name_as_the_text` runs for each gRPC status that is not `OK`. The automation test `test_grpc_requests_are_retried_or_returned_at_once_by_the_rule_of_each_error_status.py` sets thirteen of them with `error_stub`, and its docstring names three more that have a test of their own.
6. **Two behaviours of a per-method override are recorded that the design does not name in section 12.** gRPC does not read the fault keys of a per-method override, such as `mode` and `latency_ms`. This is difference 6 of section 9.3, and pull request 2c changes it. And a per-method `error_stub`, `error` or `result` applies also when a filter of the scenario does not name the endpoint: `GrpcListener.plan` asks the filters for the mode and for the corruption only. Pull request 2b must keep both.
7. **The wiring tests are in a new file of a new package**, `tests/integration/test_server_start_wiring.py`. Section 12 of the design forbids an edit of the test files of the cache simulator and of the RESP proxy. `tests/` had no folder for tests, so the package `tests/integration/` is new, on Victoria's approval; the 55 test files that exist stay where they are. The RESP test starts a simulator of its own with a fake store: the shared simulator of the session points at a store address that does not answer in a test run, and `tests/test_resp_wiring.py` says why a test must not wait for that address.
8. **The readiness of a simulator with no `grpcio` is recorded as it is.** Such a simulator serves each HTTP endpoint and the control API, and `/ready` answers 503 and names each gRPC port of its topology. The test does not judge that behaviour, and its docstring says so.
9. **The grid holds no content of a reply message.** Section 12 of the design lists what the grid holds: the status code, the text, and the method, the status, the `latency_ms` and the request id of the row. `plan` gives the content of a reply as a dictionary, `GrpcPlan.data`, that only the adapter reads. `Listener.serve` returns a built message. So a grid that holds that dictionary cannot keep its expected values in 2b. The content is read with a real client in Task 2.

---

### Task 1: The grid of `plan` results

**Files:**
- Modify: `tests/test_listener_grpc.py` (one import above `import pytest`, and the end of the file)

**Interfaces:**
- Consumes: `GrpcListener.plan(method, lava_headers, request)` and `GrpcPlan` of `provider_simulator/listeners/grpc.py`. The helpers `_listener()` and `_upd()` of the test file.
- Produces: `_decide(listener, method="GetLatestBlock", request=None, lava_headers=None) -> _Decision`. It is the one function of the new block that calls `plan` and reads a `GrpcPlan`. Pull request 2b changes it to call `serve`, and it changes no expected value. `_Decision` has the fields `code`, `text`, `wait_ms`, `hangs`, `drop_at` and `clears`. It holds no content of a reply message: `plan` holds that content as a dictionary that only the adapter reads, and pull request 2b cannot keep it. Task 2 reads the content with a real client.

- [x] **Step 1: Write the tests**

Apply this diff. It adds 81 test cases: `test_what_plan_decides_for_one_call` with 62 cases, `test_which_calls_wait_for_latency_ms` with 12 cases, and seven single tests: `test_the_row_of_get_node_info_names_its_method`, `test_latency_ms_is_paid_today_under_a_filter_that_does_not_name_the_endpoint`, `test_the_row_is_complete_when_plan_returns`, `test_the_row_names_the_endpoint_and_keeps_the_lava_headers`, `test_fail_first_n_gives_the_mode_to_the_first_calls_only`, `test_then_mode_is_the_mode_after_the_first_calls` and `test_a_filter_that_does_not_name_the_endpoint_does_not_use_up_fail_first_n`.

```diff
diff --git a/tests/test_listener_grpc.py b/tests/test_listener_grpc.py
index cdc57a2..f89b50c 100644
--- a/tests/test_listener_grpc.py
+++ b/tests/test_listener_grpc.py
@@ -1,6 +1,8 @@
 """GrpcListener — the pure decision core (plan()) an async servicer glue performs.
 No running gRPC server needed: plan() returns a GrpcPlan we assert on directly."""
 
+from typing import NamedTuple
+
 import pytest
 
 # Splice cosmos_pb2 onto sys.path so the generated stubs resolve. It must run
@@ -284,3 +286,496 @@ def test_a_request_id_row_of_a_service_with_no_loaded_stubs_is_refused():
 
     with pytest.raises(ValueError, match=r"no\.such\.Service/Ping"):
         check_servicers({"no.such.Service": Other}, served=(("no.such.Service", "Ping", "id"),))
+
+
+# --- The grid: what plan() decides today ------------------------------------
+# A gRPC call is answered through GrpcListener.plan. The tables below record
+# what plan() decides for each mode, each corruption mode and each per-method
+# override: the status that the caller gets, the text of that status, and the
+# history row of the call. Each expected value is written out by hand.
+#
+# ``_decide`` is the one function of this block that calls plan() and reads a
+# GrpcPlan. A change of the listener that keeps its behaviour changes
+# ``_decide``, and it changes no expected value.
+#
+# The content of a reply message is not in these tables. plan() holds it as a
+# dictionary that only the adapter reads, and a caller reads the message that
+# the adapter builds from it. tests/test_simulator_grpc.py holds that content,
+# read by a real client: TestGrpcHappy, TestGrpcAllBalances and
+# TestGrpcReplyFields.
+
+# GRPC, the endpoint of ``_listener``, is at port 18548. This port is not its port.
+_ANOTHER_PORT = 18549
+
+
+class _Decision(NamedTuple):
+    code: str  # the status that the caller gets; "OK" when the caller gets a reply message
+    text: str  # the text of the status; "" with a reply message
+    wait_ms: int  # how long the adapter waits before it answers
+    hangs: bool  # the adapter waits 30 seconds, and not wait_ms
+    drop_at: str | None  # set when the status stands for a dropped connection
+    clears: str | None  # the field that the adapter clears in the reply message
+
+
+def _decide(listener, method="GetLatestBlock", request=None, lava_headers=None):
+    plan = listener.plan(method, lava_headers, request)
+    if plan.action == "abort":
+        return _Decision(plan.status_code, plan.message, plan.latency_ms, plan.hang, plan.drop_at, None)
+    assert plan.action == "respond", plan.action
+    clears = plan.missing_field if plan.corruption_mode == "missing_field" else None
+    return _Decision("OK", "", plan.latency_ms, False, None, clears)
+
+
+def _status(code, text, *, hangs=False, drop_at=None):
+    """The caller gets a status, with no wait."""
+    return _Decision(code, text, 0, hangs, drop_at, None)
+
+
+def _reply(*, clears=None):
+    """The caller gets a reply message, with no wait."""
+    return _Decision("OK", "", 0, False, None, clears)
+
+
+def _override(cfg, method="GetLatestBlock"):
+    """A scenario with one per-method override."""
+    return {"responses": {method: cfg}}
+
+
+def _row(provider):
+    rows = provider.log.get_history()
+    assert len(rows) == 1, f"expected one history row, got {len(rows)}"
+    return rows[0]["method"], rows[0]["status"], rows[0]["latency_ms"], rows[0]["request_id"]
+
+
+@pytest.mark.parametrize(
+    "scenario, want, want_row_status",
+    [
+        # Each mode. A fault is a status, and each status has its own text.
+        pytest.param({}, _reply(), "success", id="success"),
+        pytest.param({"mode": "down"}, _status("UNAVAILABLE", "provider down"), "down", id="down"),
+        pytest.param({"mode": "hang"}, _status("CANCELLED", "hang timeout", hangs=True), "hang", id="hang"),
+        pytest.param(
+            {"mode": "drop_connection"},
+            _status("UNAVAILABLE", "connection dropped", drop_at="before_headers"),
+            "drop_connection",
+            id="drop-before-headers-is-the-default",
+        ),
+        pytest.param(
+            {"mode": "drop_connection", "drop_at": "after_headers"},
+            _status("UNAVAILABLE", "connection dropped", drop_at="after_headers"),
+            "drop_connection",
+            id="drop-after-headers",
+        ),
+        pytest.param(
+            {"mode": "drop_connection", "drop_at": "mid_body"},
+            _status("UNAVAILABLE", "connection dropped", drop_at="mid_body"),
+            "drop_connection",
+            id="drop-mid-body",
+        ),
+        pytest.param(
+            {"mode": "rate_limit"}, _status("RESOURCE_EXHAUSTED", "Too many requests"), "rate_limit", id="rate-limit"
+        ),
+        pytest.param(
+            {"mode": "rate_limit", "rate_limit_body": "slow down"},
+            _status("RESOURCE_EXHAUSTED", "Too many requests"),
+            "rate_limit",
+            id="rate-limit-body-does-not-change-the-text",
+        ),
+        pytest.param({"mode": "error"}, _status("UNKNOWN", "Internal error"), "error", id="error-with-no-status-named"),
+        pytest.param(
+            {"mode": "error", "error_message": "NOT_FOUND"},
+            _status("NOT_FOUND", "NOT_FOUND"),
+            "error",
+            id="error-message-names-the-status",
+        ),
+        pytest.param(
+            {"mode": "error", "error_message": "no status has this name", "error_code": 5},
+            _status("NOT_FOUND", "no status has this name"),
+            "error",
+            id="error-code-is-the-number-of-the-status",
+        ),
+        pytest.param(
+            {"mode": "error", "error_message": "ABORTED", "error_code": 5},
+            _status("ABORTED", "ABORTED"),
+            "error",
+            id="error-message-wins-over-error-code",
+        ),
+        pytest.param(
+            {"mode": "error", "error_message": "no status has this name", "error_code": -1},
+            _status("UNKNOWN", "no status has this name"),
+            "error",
+            id="error-with-no-status-in-message-or-code",
+        ),
+        pytest.param(
+            {"mode": "error", "error_message": "NOT_FOUND", "http_status": 503},
+            _status("NOT_FOUND", "NOT_FOUND"),
+            "error",
+            id="http-status-does-not-change-the-status",
+        ),
+        pytest.param({"error_probability": 1.0}, _status("UNKNOWN", "Internal error"), "error", id="error-probability"),
+        # Each per-method override. An error override is a status. With a
+        # result override the caller still gets a reply message.
+        pytest.param(
+            _override({"error_stub": "NOT_FOUND"}), _status("NOT_FOUND", "NOT_FOUND"), "error", id="error-stub"
+        ),
+        pytest.param(
+            _override({"error_stub": "NOT_FOUND", "message": "the block is gone"}),
+            _status("NOT_FOUND", "the block is gone"),
+            "error",
+            id="error-stub-with-a-message",
+        ),
+        pytest.param(
+            _override({"error_stub": "revert"}),
+            _status("UNKNOWN", "revert"),
+            "error",
+            id="error-stub-whose-name-is-no-status",
+        ),
+        pytest.param(
+            {"responses": {"default": {"error_stub": "ABORTED"}}},
+            _status("ABORTED", "ABORTED"),
+            "error",
+            id="error-stub-in-the-default-entry",
+        ),
+        pytest.param(
+            {"responses": {"GetLatestBlock": {"error_stub": "NOT_FOUND"}, "default": {"error_stub": "ABORTED"}}},
+            _status("NOT_FOUND", "NOT_FOUND"),
+            "error",
+            id="the-entry-of-the-method-wins-over-the-default-entry",
+        ),
+        pytest.param(
+            _override({"error_stub": "ABORTED"}, method="GetNodeInfo"),
+            _reply(),
+            "success",
+            id="an-override-of-another-method-does-not-apply",
+        ),
+        pytest.param(
+            _override({"error": {"code": "ABORTED", "message": "from the override"}}),
+            _status("ABORTED", "from the override"),
+            "error",
+            id="error-override-with-the-name-of-a-status",
+        ),
+        pytest.param(
+            _override({"error": {"code": 7, "message": "by number"}}),
+            _status("PERMISSION_DENIED", "by number"),
+            "error",
+            id="error-override-with-the-number-of-a-status",
+        ),
+        pytest.param(
+            _override({"error": {"code": "no status has this name"}}),
+            _status("UNKNOWN", "override"),
+            "error",
+            id="error-override-whose-code-is-no-status",
+        ),
+        pytest.param(
+            _override({"error": {}}), _status("UNKNOWN", "override"), "error", id="error-override-that-is-empty"
+        ),
+        pytest.param(
+            _override({"error_stub": "NOT_FOUND", "error": {"code": "ABORTED", "message": "from the override"}}),
+            _status("NOT_FOUND", "NOT_FOUND"),
+            "error",
+            id="error-stub-wins-over-an-error-override",
+        ),
+        pytest.param(
+            _override({"error_stub": "NOT_FOUND", "result": {"height": 7}}),
+            _status("NOT_FOUND", "NOT_FOUND"),
+            "error",
+            id="error-stub-wins-over-a-result-override",
+        ),
+        pytest.param(
+            _override({"result": {"height": 7}}),
+            _reply(),
+            "success",
+            id="result-override",
+        ),
+        pytest.param(
+            {"responses": {"default": {"result": {"height": 9}}}},
+            _reply(),
+            "success",
+            id="result-override-in-the-default-entry",
+        ),
+        # A fault key in a per-method override is not read on gRPC.
+        pytest.param(_override({"mode": "down"}), _reply(), "success", id="per-method-mode-down-is-not-read"),
+        pytest.param(
+            _override({"mode": "rate_limit"}),
+            _reply(),
+            "success",
+            id="per-method-mode-rate-limit-is-not-read",
+        ),
+        pytest.param(_override({"latency_ms": 700}), _reply(), "success", id="per-method-latency-is-not-read"),
+        pytest.param(
+            _override({"error_probability": 1.0}),
+            _reply(),
+            "success",
+            id="per-method-error-probability-is-not-read",
+        ),
+        pytest.param(
+            {"mode": "down", **_override({"mode": "success"})},
+            _status("UNAVAILABLE", "provider down"),
+            "down",
+            id="per-method-mode-success-does-not-lift-a-down",
+        ),
+        # A fault of the provider comes before a per-method override.
+        pytest.param(
+            {"mode": "rate_limit", **_override({"error_stub": "NOT_FOUND"})},
+            _status("RESOURCE_EXHAUSTED", "Too many requests"),
+            "rate_limit",
+            id="rate-limit-comes-before-an-error-stub",
+        ),
+        pytest.param(
+            {"mode": "error", "error_message": "ABORTED", **_override({"error": {"code": "NOT_FOUND"}})},
+            _status("ABORTED", "ABORTED"),
+            "error",
+            id="error-comes-before-an-error-override",
+        ),
+        pytest.param(
+            {"mode": "drop_connection", **_override({"result": {"height": 7}})},
+            _status("UNAVAILABLE", "connection dropped", drop_at="before_headers"),
+            "drop_connection",
+            id="drop-comes-before-a-result-override",
+        ),
+        # Each corruption mode. Five of them turn the reply message into a
+        # status, and the row then says error. One clears a field. One does
+        # nothing on gRPC.
+        pytest.param(
+            {"corruption_mode": "wrong_type"},
+            _status("INTERNAL", "wrong_type corruption on response"),
+            "error",
+            id="wrong-type",
+        ),
+        pytest.param(
+            {"corruption_mode": "wrong_type", "missing_field": "block"},
+            _status("INTERNAL", "wrong_type corruption on block"),
+            "error",
+            id="wrong-type-names-the-field",
+        ),
+        pytest.param(
+            {"corruption_mode": "invalid_proto"},
+            _status("UNKNOWN", "corruption: invalid_proto"),
+            "error",
+            id="invalid-proto",
+        ),
+        pytest.param(
+            {"corruption_mode": "empty_response"},
+            _status("UNKNOWN", "corruption: empty_response"),
+            "error",
+            id="empty-response",
+        ),
+        pytest.param(
+            {"corruption_mode": "truncated"}, _status("UNKNOWN", "corruption: truncated"), "error", id="truncated"
+        ),
+        pytest.param(
+            {"corruption_mode": "null_body"}, _status("UNKNOWN", "corruption: null_body"), "error", id="null-body"
+        ),
+        pytest.param(
+            {"corruption_mode": "missing_field", "missing_field": "block"},
+            _reply(clears="block"),
+            "success",
+            id="missing-field-clears-the-field",
+        ),
+        pytest.param(
+            {"corruption_mode": "missing_field"},
+            _reply(),
+            "success",
+            id="missing-field-with-no-field-clears-nothing",
+        ),
+        pytest.param(
+            {"missing_field": "block"},
+            _reply(),
+            "success",
+            id="a-field-with-no-corruption-mode-clears-nothing",
+        ),
+        pytest.param({"corruption_mode": "invalid_json"}, _reply(), "success", id="invalid-json-does-nothing"),
+        # A fault with a corruption. A corruption acts on a reply message only,
+        # so a status stays as it is.
+        pytest.param(
+            {"mode": "rate_limit", "corruption_mode": "wrong_type"},
+            _status("RESOURCE_EXHAUSTED", "Too many requests"),
+            "rate_limit",
+            id="rate-limit-with-a-corruption-stays-rate-limit",
+        ),
+        pytest.param(
+            {"mode": "error", "error_message": "NOT_FOUND", "corruption_mode": "invalid_proto"},
+            _status("NOT_FOUND", "NOT_FOUND"),
+            "error",
+            id="error-with-a-corruption-stays-the-error",
+        ),
+        pytest.param(
+            {"mode": "down", "corruption_mode": "truncated"},
+            _status("UNAVAILABLE", "provider down"),
+            "down",
+            id="down-with-a-corruption-stays-down",
+        ),
+        pytest.param(
+            {**_override({"error_stub": "ABORTED"}), "corruption_mode": "wrong_type"},
+            _status("ABORTED", "ABORTED"),
+            "error",
+            id="error-stub-with-a-corruption-stays-the-stub",
+        ),
+        pytest.param(
+            {**_override({"result": {"height": 7}}), "corruption_mode": "wrong_type"},
+            _status("INTERNAL", "wrong_type corruption on response"),
+            "error",
+            id="result-override-with-a-corruption-is-corrupted",
+        ),
+        # The transports filter and the ports filter. A mode and a corruption
+        # reach the endpoint only when both filters name it.
+        pytest.param(
+            {"mode": "down", "transports": ["http2"]},
+            _status("UNAVAILABLE", "provider down"),
+            "down",
+            id="mode-with-a-transports-filter-that-names-the-endpoint",
+        ),
+        pytest.param(
+            {"mode": "down", "transports": ["ws"]},
+            _reply(),
+            "success",
+            id="mode-with-a-transports-filter-that-does-not-name-it",
+        ),
+        pytest.param(
+            {"mode": "down", "ports": [18548]},
+            _status("UNAVAILABLE", "provider down"),
+            "down",
+            id="mode-with-a-ports-filter-that-names-the-endpoint",
+        ),
+        pytest.param(
+            {"mode": "down", "ports": [_ANOTHER_PORT]},
+            _reply(),
+            "success",
+            id="mode-with-a-ports-filter-that-does-not-name-it",
+        ),
+        pytest.param(
+            {"mode": "rate_limit", "transports": ["http2"], "ports": [_ANOTHER_PORT]},
+            _reply(),
+            "success",
+            id="mode-with-one-filter-that-names-the-endpoint-and-one-that-does-not",
+        ),
+        pytest.param(
+            {"corruption_mode": "wrong_type", "transports": ["http"]},
+            _reply(),
+            "success",
+            id="corruption-with-a-transports-filter-that-does-not-name-it",
+        ),
+        pytest.param(
+            {"corruption_mode": "wrong_type", "ports": [_ANOTHER_PORT]},
+            _reply(),
+            "success",
+            id="corruption-with-a-ports-filter-that-does-not-name-it",
+        ),
+        pytest.param(
+            {"corruption_mode": "missing_field", "missing_field": "block", "transports": ["ws"]},
+            _reply(),
+            "success",
+            id="missing-field-with-a-filter-that-does-not-name-it-clears-nothing",
+        ),
+        # A per-method override is not a fault of the endpoint: the filters do
+        # not hold it back.
+        pytest.param(
+            {**_override({"error_stub": "NOT_FOUND"}), "transports": ["ws"]},
+            _status("NOT_FOUND", "NOT_FOUND"),
+            "error",
+            id="error-stub-with-a-filter-that-does-not-name-the-endpoint",
+        ),
+        pytest.param(
+            {**_override({"result": {"height": 7}}), "ports": [_ANOTHER_PORT]},
+            _reply(),
+            "success",
+            id="result-override-with-a-filter-that-does-not-name-the-endpoint",
+        ),
+    ],
+)
+def test_what_plan_decides_for_one_call(scenario, want, want_row_status):
+    listener, provider = _listener()
+    _upd(listener, scenario)
+    assert _decide(listener) == want
+    assert _row(provider) == ("GetLatestBlock", want_row_status, 0, None)
+
+
+def test_the_row_of_get_node_info_names_its_method():
+    listener, provider = _listener()
+    assert _decide(listener, "GetNodeInfo") == _reply()
+    assert _row(provider) == ("GetNodeInfo", "success", 0, None)
+
+
+@pytest.mark.parametrize(
+    "scenario, want_wait_ms, want_row_latency_ms",
+    [
+        pytest.param({}, 250, 250, id="success"),
+        # A down provider and a hung provider do not wait for the latency, and
+        # their rows say 0.
+        pytest.param({"mode": "down"}, 0, 0, id="down"),
+        pytest.param({"mode": "hang"}, 0, 0, id="hang"),
+        pytest.param({"mode": "drop_connection"}, 250, 250, id="drop-connection"),
+        pytest.param({"mode": "rate_limit"}, 250, 250, id="rate-limit"),
+        pytest.param({"mode": "error"}, 250, 250, id="error"),
+        pytest.param(_override({"error_stub": "NOT_FOUND"}), 250, 250, id="error-stub"),
+        pytest.param(_override({"error": {"code": "ABORTED"}}), 250, 250, id="error-override"),
+        pytest.param(_override({"result": {"height": 7}}), 250, 250, id="result-override"),
+        pytest.param({"corruption_mode": "wrong_type"}, 250, 250, id="wrong-type"),
+        pytest.param({"corruption_mode": "invalid_proto"}, 250, 250, id="invalid-proto"),
+        pytest.param({"corruption_mode": "missing_field", "missing_field": "block"}, 250, 250, id="missing-field"),
+    ],
+)
+def test_which_calls_wait_for_latency_ms(scenario, want_wait_ms, want_row_latency_ms):
+    listener, provider = _listener()
+    _upd(listener, {**scenario, "latency_ms": 250})
+    assert _decide(listener).wait_ms == want_wait_ms
+    assert _row(provider)[2] == want_row_latency_ms
+
+
+def test_latency_ms_is_paid_today_under_a_filter_that_does_not_name_the_endpoint():
+    # Today gRPC reads ``latency_ms`` with no look at the filters. JSON-RPC, REST
+    # and Tendermint RPC do not delay an endpoint that the filter does not name.
+    # The design "One request flow for every interface" changes gRPC to the same
+    # rule on purpose (its section 9.2, row 2). The pull request that moves gRPC
+    # into Listener.serve edits the two expected values of this test, to 0.
+    listener, provider = _listener()
+    _upd(listener, {"latency_ms": 250, "ports": [_ANOTHER_PORT]})
+    assert _decide(listener).wait_ms == 250
+    assert _row(provider) == ("GetLatestBlock", "success", 250, None)
+
+
+def test_the_row_is_complete_when_plan_returns():
+    # The adapter waits for the latency after plan() returns. So a reader of the
+    # history finds the method, the status and the latency of a call that the
+    # provider still holds.
+    listener, provider = _listener()
+    _upd(listener, {"latency_ms": 250})
+    _decide(listener)
+    assert _row(provider) == ("GetLatestBlock", "success", 250, None)
+    assert provider.log.stats()["calls_by_status"] == {"success": 1}
+
+
+def test_the_row_names_the_endpoint_and_keeps_the_lava_headers():
+    listener, provider = _listener()
+    _decide(listener, lava_headers={"lava-guid": "guid-1"})
+    row = provider.log.get_history()[0]
+    assert (row["pool"], row["pid"]) == ("lava-sim-grpc", "1")
+    assert (row["interface"], row["transport"], row["port"]) == ("grpc", "http2", 18548)
+    assert row["lava_headers"] == {"lava-guid": "guid-1"}
+
+
+def test_fail_first_n_gives_the_mode_to_the_first_calls_only():
+    listener, provider = _listener()
+    _upd(listener, {"mode": "down", "fail_first_n": 3, "then_mode": "success"})
+    assert [_decide(listener).code for _ in range(5)] == ["UNAVAILABLE", "UNAVAILABLE", "UNAVAILABLE", "OK", "OK"]
+    assert [row["status"] for row in provider.log.get_history()] == ["down", "down", "down", "success", "success"]
+
+
+def test_then_mode_is_the_mode_after_the_first_calls():
+    listener, provider = _listener()
+    _upd(listener, {"mode": "error", "fail_first_n": 1, "then_mode": "rate_limit"})
+    assert [_decide(listener).code for _ in range(3)] == ["UNKNOWN", "RESOURCE_EXHAUSTED", "RESOURCE_EXHAUSTED"]
+    assert [row["status"] for row in provider.log.get_history()] == ["error", "rate_limit", "rate_limit"]
+
+
+def test_a_filter_that_does_not_name_the_endpoint_does_not_use_up_fail_first_n():
+    # One provider with two gRPC endpoints. The filter names the second one only.
+    other = Endpoint("grpc", "http2", _ANOTHER_PORT)
+    provider = Pool(name="lava-sim-grpc", chain="lava").add_provider("1", [GRPC, other])
+    not_named, named = GrpcListener(provider, GRPC), GrpcListener(provider, other)
+    provider.scenario.update({"mode": "down", "fail_first_n": 1, "then_mode": "rate_limit", "ports": [_ANOTHER_PORT]})
+    # The endpoint that the filter does not name gets no mode and no then_mode.
+    assert [_decide(not_named).code for _ in range(3)] == ["OK", "OK", "OK"]
+    # Those three calls did not use up the window: the named endpoint still gets
+    # the mode for its first call, and the then_mode after it.
+    assert [_decide(named).code for _ in range(3)] == ["UNAVAILABLE", "RESOURCE_EXHAUSTED", "RESOURCE_EXHAUSTED"]
```

- [x] **Step 2: Run the tests**

Run: `python -m pytest tests/test_listener_grpc.py -q -p no:cacheprovider`

Expected: `114 passed`. That is the 33 tests of `main` and the 81 new cases. Each new case passes with no code change, because it records what the code does today.

- [x] **Step 3: Show that each new case can fail**

A test that passes at once proves nothing until it is seen to fail. Break `provider_simulator/listeners/grpc.py` in one place, run the file, read which cases fail, and put the source file back. One break is in `provider_simulator/fault_policy.py`. The section "The mutations" lists the 74 places that were broken and the count of failed tests for each one.

Result: each of the 74 mutations made one test or more fail, and each of the 81 new cases failed under one mutation or more.

The rules for such a run are in the skill `add-simulator-entity`, section "Proving a guard can fail, and the two ways a mutation lies to you". Check that the old text is in the file exactly one time before the run. Run with `PYTHONDONTWRITEBYTECODE=1`. Put the file back from a copy, and not with `git checkout`. Compare the checksum of the file with the checksum of the copy.

- [x] **Step 4: Run the checks of CI on the file**

```bash
black --check tests/test_listener_grpc.py
ruff check tests/test_listener_grpc.py
mypy tests/test_listener_grpc.py
```

Expected: black prints `1 file would be left unchanged.` ruff prints `All checks passed!` mypy prints `Success: no issues found in 1 source file`.

- [x] **Step 5: Commit**

```bash
git add tests/test_listener_grpc.py
git commit -m "test(grpc): record what GrpcListener.plan decides for each mode, corruption and override"
```

---

### Task 2: What a real gRPC client reads

**Files:**
- Modify: `tests/test_simulator_grpc.py` (one import, three blocks of constants after `_ETH_URLS`, helpers after `_free_port`, one test in `TestGrpcServedMethods`, six new classes before the banner "Cross-pool fault isolation", and six new entries in the list "Coverage" of the module docstring)

**Interfaces:**
- Consumes: the fixture `sim` of `tests/conftest.py` and the autouse fixture `clean_state` of the file. The helpers `_set_grpc`, `_get`, `_ctrl`, `_call_get_latest_block`, `_call_get_node_info` and `_call_all_balances` of the file.
- Produces: the helpers `_status_of`, `_call_raw`, `_rows`, `_wait_until_accepting` and `_frames_of_one_call`. No other task uses them.

- [x] **Step 1: Write the tests**

Apply this diff. It adds 79 test cases: one test in `TestGrpcServedMethods`, and the classes `TestGrpcStatusTexts` (15 cases), `TestGrpcWhichCallsWait` (10 cases), `TestGrpcPerMethodErrors` (23 cases), `TestGrpcReplyFields` (13 cases), `TestGrpcCallsThatReachNoListener` (9 cases) and `TestGrpcDropPoint` (8 cases).

```diff
diff --git a/tests/test_simulator_grpc.py b/tests/test_simulator_grpc.py
index ed00892..47c05c4 100644
--- a/tests/test_simulator_grpc.py
+++ b/tests/test_simulator_grpc.py
@@ -21,6 +21,18 @@ Coverage:
   Request id                  — AllBalances records its ``address`` as the
                                 request id of its history row.
   Reflection                  — a served service is found by its symbol.
+  Status texts                — the status and the text that a caller reads
+                                for each fault, with the history row.
+  Waits                       — which statuses wait for latency_ms.
+  Per-method errors           — error_stub with each status name, and the
+                                per-method error override.
+  Reply fields                — the fields of the replies, with a result
+                                override and with none.
+  Calls with no listener      — a method that is not served, a service that
+                                is not registered, a request that is no
+                                message.
+  Drop points                 — the status and the HTTP/2 frames of each
+                                drop point.
 
 Run with:
   pytest tests/test_simulator_grpc.py -v
@@ -54,6 +66,7 @@ from cosmos.bank.v1beta1 import query_pb2_grpc as bank_query_pb2_grpc  # isort:
 from cosmos.base.tendermint.v1beta1 import query_pb2, query_pb2_grpc  # isort: skip
 
 import server as server_module
+from provider_simulator import topology
 from provider_simulator.chains.lava import GRPC_LATEST_BLOCK
 from provider_simulator.domain.endpoint import Endpoint
 from provider_simulator.domain.provider import Pool
@@ -67,6 +80,38 @@ from provider_simulator.topology import port_of
 _PRIMARY_PIDS = ("1", "2", "3")
 _GRPC_ADDRS = {pid: f"127.0.0.1:{port_of('lava-sim-grpc', pid, 'grpc', 'http2')}" for pid in _PRIMARY_PIDS}
 _ETH_URLS = {pid: f"http://127.0.0.1:{port_of('eth-sim', pid)}" for pid in _PRIMARY_PIDS}
+_GRPC_PORT_1 = port_of("lava-sim-grpc", "1", "grpc", "http2")
+_GET_LATEST_BLOCK_PATH = "/cosmos.base.tendermint.v1beta1.Service/GetLatestBlock"
+
+# A second simulator, for the one test that needs a gRPC endpoint that does not
+# start. Its ports are this file's own block, below the range that the kernel
+# gives to client sockets.
+_REFUSED_CONTROL = 29721
+_REFUSED_GRPC, _REFUSED_REST = 28721, 28722
+_REFUSED_ROWS = [
+    ("lava-refused-sim", "lava", "1", "LavaRefusedPrimaryProvider1", False, "", (("grpc", "http2", _REFUSED_GRPC),)),
+    ("lava-refused-sim", "lava", "2", "LavaRefusedPrimaryProvider2", False, "", (("rest", "http", _REFUSED_REST),)),
+]
+
+# Each gRPC status that is not OK, by its name.
+_ERROR_STATUS_NAMES = [
+    "CANCELLED",
+    "UNKNOWN",
+    "INVALID_ARGUMENT",
+    "DEADLINE_EXCEEDED",
+    "NOT_FOUND",
+    "ALREADY_EXISTS",
+    "PERMISSION_DENIED",
+    "RESOURCE_EXHAUSTED",
+    "FAILED_PRECONDITION",
+    "ABORTED",
+    "OUT_OF_RANGE",
+    "UNIMPLEMENTED",
+    "INTERNAL",
+    "UNAVAILABLE",
+    "DATA_LOSS",
+    "UNAUTHENTICATED",
+]
 
 
 # ── HTTP helpers for the control plane ──────────────────────────────────────
@@ -211,6 +256,123 @@ def _free_port() -> int:
         return probe.getsockname()[1]
 
 
+def _status_of(call, *args, **kwargs) -> tuple[grpc.StatusCode, str]:
+    """Run one of the ``_call_*`` helpers. Return the status that the caller
+    gets and the text of that status. A reply message gives ``(OK, "")``."""
+    try:
+        call(*args, **kwargs)
+    except grpc.RpcError as exc:
+        return exc.code(), exc.details() or ""
+    return grpc.StatusCode.OK, ""
+
+
+def _call_raw(address: str, path: str, payload: bytes = b"", timeout: float = 5.0) -> bytes:
+    """Call one gRPC method by its path, with request bytes that the test
+    chooses, and return the reply bytes. A generated stub cannot call a method
+    that it does not have, and it cannot send bytes that are no message."""
+
+    async def _do():
+        channel = grpc.aio.insecure_channel(address)
+        try:
+            rpc = channel.unary_unary(path, request_serializer=lambda raw: raw, response_deserializer=lambda raw: raw)
+            return await asyncio.wait_for(rpc(payload), timeout=timeout)
+        finally:
+            await channel.close()
+
+    return asyncio.run(_do())
+
+
+def _rows(sim, pid: str = "1", **filters: str) -> list[dict]:
+    """The history rows of one lava-sim-grpc provider, oldest first."""
+    query = "".join(f"&{name}={value}" for name, value in filters.items())
+    _, hist = _get(_ctrl(sim, f"/history?pool=lava-sim-grpc&pid={pid}{query}"))
+    assert isinstance(hist, dict), f"/history did not answer with a JSON object: {hist!r}"
+    return hist["history"]
+
+
+def _wait_until_accepting(port: int, timeout_s: float = 10.0) -> None:
+    """Poll a local port until it accepts a TCP connection."""
+    deadline = time.monotonic() + timeout_s
+    while time.monotonic() < deadline:
+        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
+            probe.settimeout(0.2)
+            if probe.connect_ex(("127.0.0.1", port)) == 0:
+                return
+        time.sleep(0.02)
+    raise AssertionError(f"port {port} did not accept a connection in {timeout_s} s")
+
+
+# ── One call as HTTP/2 frames ────────────────────────────────────────────────
+# A gRPC client reads the same thing for a status that came alone and for a
+# status that came after the initial metadata: the status, and no metadata. The
+# difference is in the frames that the server sends. So the tests of the drop
+# points write one call as HTTP/2 frames by hand, and they read the frames that
+# come back.
+
+_H2_DATA, _H2_HEADERS, _H2_RST_STREAM, _H2_SETTINGS = 0x0, 0x1, 0x3, 0x4
+_H2_END_STREAM, _H2_ACK, _H2_END_HEADERS = 0x1, 0x1, 0x4
+_H2_FRAME_NAMES = {_H2_DATA: "DATA", _H2_HEADERS: "HEADERS", _H2_RST_STREAM: "RST_STREAM"}
+
+
+def _h2_frame(kind: int, flags: int, stream: int, payload: bytes = b"") -> bytes:
+    return len(payload).to_bytes(3, "big") + bytes([kind, flags]) + stream.to_bytes(4, "big") + payload
+
+
+def _h2_header(name: bytes, value: bytes) -> bytes:
+    """One request header as an HPACK literal, with no indexing and no Huffman coding."""
+    assert len(name) < 127 and len(value) < 127, "this helper writes one-byte lengths only"
+    return b"\x00" + bytes([len(name)]) + name + bytes([len(value)]) + value
+
+
+def _frames_of_one_call(port: int, path: str, timeout: float = 5.0) -> list[str]:
+    """Send one unary gRPC call with an empty request message, as HTTP/2 frames.
+
+    Return the frames that the server sent for that call, in order: "HEADERS",
+    "DATA" or "RST_STREAM". The frame that ends the call has "+END_STREAM".
+    """
+    request_headers = b"".join(
+        _h2_header(name, value)
+        for name, value in (
+            (b":method", b"POST"),
+            (b":scheme", b"http"),
+            (b":path", path.encode()),
+            (b":authority", f"127.0.0.1:{port}".encode()),
+            (b"content-type", b"application/grpc"),
+            (b"te", b"trailers"),
+        )
+    )
+    frames: list[str] = []
+    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
+        sock.sendall(
+            b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
+            + _h2_frame(_H2_SETTINGS, 0, 0)
+            + _h2_frame(_H2_HEADERS, _H2_END_HEADERS, 1, request_headers)
+            # One gRPC message of zero bytes: a flag byte and a four-byte length.
+            + _h2_frame(_H2_DATA, _H2_END_STREAM, 1, b"\x00\x00\x00\x00\x00")
+        )
+        buffer = b""
+        while True:
+            while len(buffer) < 9 or len(buffer) < 9 + int.from_bytes(buffer[:3], "big"):
+                chunk = sock.recv(65536)
+                if not chunk:
+                    return frames + ["CONNECTION CLOSED"]
+                buffer += chunk
+            length = int.from_bytes(buffer[:3], "big")
+            kind, flags = buffer[3], buffer[4]
+            stream = int.from_bytes(buffer[5:9], "big") & 0x7FFFFFFF
+            buffer = buffer[9 + length :]
+            if kind == _H2_SETTINGS and not flags & _H2_ACK:
+                sock.sendall(_h2_frame(_H2_SETTINGS, _H2_ACK, 0))
+            if stream != 1:
+                continue
+            name = _H2_FRAME_NAMES.get(kind, f"type {kind}")
+            if kind == _H2_RST_STREAM:
+                return frames + [name]
+            if kind in (_H2_DATA, _H2_HEADERS) and flags & _H2_END_STREAM:
+                return frames + [name + "+END_STREAM"]
+            frames.append(name)
+
+
 _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
 
 # One gRPC endpoint in a process of its own, and a reflection lookup for each
@@ -827,6 +989,553 @@ class TestGrpcServedMethods:
         assert errors, "the adapter started a gRPC server with a served method that has no row"
         assert "Served with no row: ['cosmos.bank.v1beta1.Query/AllBalances']" in errors[0]
 
+    def test_an_endpoint_that_is_refused_leaves_the_simulator_up_and_not_ready(self, monkeypatch):
+        """The check runs on the thread of one gRPC endpoint. When it refuses,
+        that thread ends and the port of the endpoint never opens. The process
+        stays up: the control API answers and each other endpoint serves.
+        /ready answers 503 and names the port, so a deployment of such a
+        simulator does not report ready."""
+        without_all_balances = tuple(row for row in SERVED_METHODS if row[1] != "AllBalances")
+        monkeypatch.setattr(grpc_listener, "SERVED_METHODS", without_all_balances)
+        thread_errors: list[str] = []
+        monkeypatch.setattr(threading, "excepthook", lambda args: thread_errors.append(str(args.exc_value)))
+        shipped = topology.TOPOLOGY
+        topology.TOPOLOGY = _REFUSED_ROWS
+        try:
+            second = server_module.SimulatorServer(
+                host="127.0.0.1", control_port=_REFUSED_CONTROL, scenario_ttl_s=0, cache_ports={}, resp_proxy_ports={}
+            )
+        finally:
+            topology.TOPOLOGY = shipped
+        second.start()
+        try:
+            _wait_until_accepting(_REFUSED_CONTROL)
+            _wait_until_accepting(_REFUSED_REST)
+            deadline = time.monotonic() + 10
+            while not thread_errors and time.monotonic() < deadline:
+                time.sleep(0.02)
+            assert len(thread_errors) == 1, thread_errors
+            assert "Served with no row: ['cosmos.bank.v1beta1.Query/AllBalances']" in thread_errors[0]
+
+            control = f"http://127.0.0.1:{_REFUSED_CONTROL}"
+            status, ready = _get(control + "/ready")
+            assert status == 503
+            assert (ready["status"], ready["missing_ports"]) == ("not_ready", [_REFUSED_GRPC])
+            assert _get(control + "/health")[0] == 200
+            rest_status, _ = _get(f"http://127.0.0.1:{_REFUSED_REST}/cosmos/base/tendermint/v1beta1/blocks/latest")
+            assert rest_status == 200
+        finally:
+            second.stop()
+
+
+# ─────────────────────────────────────────────────────────────────────────────
+# What a caller reads today, fault by fault
+# ─────────────────────────────────────────────────────────────────────────────
+# The classes below record what a real gRPC client reads from the simulator
+# today: the status of each fault with its text, the history row of the call,
+# and the answers that the gRPC library gives before the listener sees a call.
+# A later change moves gRPC into the request flow of Listener.serve. These
+# tests then show that a caller reads the same thing after the move.
+
+
+class TestGrpcStatusTexts:
+    """Each fault reaches the caller as a status with a text, and it leaves one
+    history row. The texts "provider down" and "connection dropped" have their
+    tests in tests/test_simulator_grpc_port_closed.py."""
+
+    @pytest.mark.parametrize(
+        "scenario, want_code, want_text, want_row_status",
+        [
+            pytest.param(
+                {"mode": "rate_limit"},
+                grpc.StatusCode.RESOURCE_EXHAUSTED,
+                "Too many requests",
+                "rate_limit",
+                id="rate-limit",
+            ),
+            pytest.param(
+                {"mode": "rate_limit", "rate_limit_body": "slow down"},
+                grpc.StatusCode.RESOURCE_EXHAUSTED,
+                "Too many requests",
+                "rate_limit",
+                id="rate-limit-body-does-not-change-the-text",
+            ),
+            pytest.param(
+                {"mode": "error"}, grpc.StatusCode.UNKNOWN, "Internal error", "error", id="error-with-no-status-named"
+            ),
+            pytest.param(
+                {"mode": "error", "error_message": "NOT_FOUND"},
+                grpc.StatusCode.NOT_FOUND,
+                "NOT_FOUND",
+                "error",
+                id="error-message-names-the-status",
+            ),
+            pytest.param(
+                {"mode": "error", "error_message": "no status has this name", "error_code": 5},
+                grpc.StatusCode.NOT_FOUND,
+                "no status has this name",
+                "error",
+                id="error-code-is-the-number-of-the-status",
+            ),
+            pytest.param(
+                {"corruption_mode": "wrong_type"},
+                grpc.StatusCode.INTERNAL,
+                "wrong_type corruption on response",
+                "error",
+                id="wrong-type",
+            ),
+            pytest.param(
+                {"corruption_mode": "wrong_type", "missing_field": "block"},
+                grpc.StatusCode.INTERNAL,
+                "wrong_type corruption on block",
+                "error",
+                id="wrong-type-names-the-field",
+            ),
+            pytest.param(
+                {"corruption_mode": "invalid_proto"},
+                grpc.StatusCode.UNKNOWN,
+                "corruption: invalid_proto",
+                "error",
+                id="invalid-proto",
+            ),
+            pytest.param(
+                {"corruption_mode": "empty_response"},
+                grpc.StatusCode.UNKNOWN,
+                "corruption: empty_response",
+                "error",
+                id="empty-response",
+            ),
+            pytest.param(
+                {"corruption_mode": "truncated"},
+                grpc.StatusCode.UNKNOWN,
+                "corruption: truncated",
+                "error",
+                id="truncated",
+            ),
+            pytest.param(
+                {"corruption_mode": "null_body"},
+                grpc.StatusCode.UNKNOWN,
+                "corruption: null_body",
+                "error",
+                id="null-body",
+            ),
+            # A corruption acts on a reply message only, so a status stays as it is.
+            pytest.param(
+                {"mode": "rate_limit", "corruption_mode": "wrong_type"},
+                grpc.StatusCode.RESOURCE_EXHAUSTED,
+                "Too many requests",
+                "rate_limit",
+                id="rate-limit-with-a-corruption-stays-rate-limit",
+            ),
+            pytest.param(
+                {"mode": "error", "error_message": "NOT_FOUND", "corruption_mode": "invalid_proto"},
+                grpc.StatusCode.NOT_FOUND,
+                "NOT_FOUND",
+                "error",
+                id="error-with-a-corruption-stays-the-error",
+            ),
+        ],
+    )
+    def test_a_fault_is_a_status_with_a_text_and_one_row(self, sim, scenario, want_code, want_text, want_row_status):
+        status, body = _set_grpc(sim, "1", **scenario)
+        assert status == 200, body
+        assert _status_of(_call_get_latest_block, _GRPC_ADDRS["1"]) == (want_code, want_text)
+        assert [(row["method"], row["status"]) for row in _rows(sim)] == [("GetLatestBlock", want_row_status)]
+
+    def test_invalid_json_corruption_does_nothing_on_grpc(self, sim):
+        status, body = _set_grpc(sim, "1", corruption_mode="invalid_json")
+        assert status == 200, body
+        resp = _call_get_latest_block(_GRPC_ADDRS["1"])
+        assert (resp.block.header.height, resp.block.header.chain_id) == (GRPC_LATEST_BLOCK, "lava-sim")
+        assert [row["status"] for row in _rows(sim)] == ["success"]
+
+    @pytest.mark.timeout(60)
+    def test_a_hung_call_ends_after_30_seconds_with_cancelled_and_its_text(self, sim):
+        """The adapter holds a hung call for 30 seconds, and then it ends the
+        call with CANCELLED and the text "hang timeout". A caller with a shorter
+        deadline does not read that status, so this test waits for it. It is the
+        one slow test of this file: it takes 30 seconds.
+
+        The scenario also has a latency. The row of a hung call records 0."""
+        status, body = _set_grpc(sim, "1", mode="hang", latency_ms=700)
+        assert status == 200, body
+        started = time.monotonic()
+        answer = _status_of(_call_get_latest_block, _GRPC_ADDRS["1"], timeout=45.0)
+        elapsed = time.monotonic() - started
+        assert answer == (grpc.StatusCode.CANCELLED, "hang timeout")
+        assert 29.5 <= elapsed <= 40.0, f"a hung call must end after 30 s, and it ended after {elapsed:.1f} s"
+        assert [(row["status"], row["latency_ms"]) for row in _rows(sim)] == [("hang", 0)]
+
+
+class TestGrpcWhichCallsWait:
+    """``latency_ms`` delays a reply message and each status but two. A down
+    provider answers at once, and a hung call waits its own 30 seconds. The row
+    of each of the two records 0."""
+
+    @pytest.mark.parametrize(
+        "scenario, want_code, min_s, max_s, want_row_latency_ms",
+        [
+            pytest.param({"mode": "down"}, grpc.StatusCode.UNAVAILABLE, 0.0, 0.5, 0, id="down-answers-at-once"),
+            pytest.param(
+                {"mode": "drop_connection"}, grpc.StatusCode.UNAVAILABLE, 0.55, 5.0, 600, id="drop-connection-waits"
+            ),
+            pytest.param(
+                {"mode": "rate_limit"}, grpc.StatusCode.RESOURCE_EXHAUSTED, 0.55, 5.0, 600, id="rate-limit-waits"
+            ),
+            pytest.param({"mode": "error"}, grpc.StatusCode.UNKNOWN, 0.55, 5.0, 600, id="error-waits"),
+            pytest.param(
+                {"corruption_mode": "wrong_type"}, grpc.StatusCode.INTERNAL, 0.55, 5.0, 600, id="wrong-type-waits"
+            ),
+            pytest.param(
+                {"responses": {"GetLatestBlock": {"error_stub": "NOT_FOUND"}}},
+                grpc.StatusCode.NOT_FOUND,
+                0.55,
+                5.0,
+                600,
+                id="error-stub-waits",
+            ),
+        ],
+    )
+    def test_which_statuses_wait_for_latency_ms(self, sim, scenario, want_code, min_s, max_s, want_row_latency_ms):
+        status, body = _set_grpc(sim, "1", latency_ms=600, **scenario)
+        assert status == 200, body
+        started = time.monotonic()
+        code, _ = _status_of(_call_get_latest_block, _GRPC_ADDRS["1"])
+        elapsed = time.monotonic() - started
+        assert code == want_code
+        assert min_s <= elapsed < max_s, f"the status came after {elapsed:.3f} s with latency_ms=600"
+        assert [row["latency_ms"] for row in _rows(sim)] == [want_row_latency_ms]
+
+    def test_the_row_of_a_call_is_complete_while_the_provider_still_waits(self, sim):
+        """A test of the smart-router reads the row of a call that the provider
+        still holds. So the row must have its method, its status, its latency
+        and its request id before the wait, and not after it."""
+        status, body = _set_grpc(sim, "1", latency_ms=2000)
+        assert status == 200, body
+        outcome: dict = {}
+
+        def _held_call():
+            started = time.monotonic()
+            resp = _call_all_balances(_GRPC_ADDRS["1"], "lava1-held-a")
+            outcome["elapsed"] = time.monotonic() - started
+            outcome["coins"] = [(coin.denom, coin.amount) for coin in resp.balances]
+
+        caller = threading.Thread(target=_held_call)
+        caller.start()
+        try:
+            rows: list[dict] = []
+            deadline = time.monotonic() + 1.5
+            while not rows and time.monotonic() < deadline:
+                rows = _rows(sim, request_id="lava1-held-a")
+                if not rows:
+                    time.sleep(0.02)
+            still_held = caller.is_alive()
+        finally:
+            caller.join(timeout=10)
+        assert rows, "the call left no row in its first 1.5 seconds, while the provider held it"
+        assert still_held, "the call had ended when the row was read, so the read proves nothing"
+        assert [(row["method"], row["status"], row["latency_ms"], row["request_id"]) for row in rows] == [
+            ("AllBalances", "success", 2000, "lava1-held-a")
+        ]
+        assert outcome["coins"] == [("ulava", "1000000")]
+        assert outcome["elapsed"] >= 1.9, f"the reply came after {outcome['elapsed']:.3f} s with latency_ms=2000"
+
+    def test_a_pause_does_not_delay_a_grpc_call(self, sim):
+        """The control API stores a pause for a gRPC provider, and the gRPC
+        adapter does not perform it: a pause belongs to the HTTP write path."""
+        status, body = _set_grpc(sim, "1", pause_at="mid_body", pause_ms=3000)
+        assert status == 200, body
+        started = time.monotonic()
+        resp = _call_get_latest_block(_GRPC_ADDRS["1"])
+        elapsed = time.monotonic() - started
+        assert resp.block.header.height == GRPC_LATEST_BLOCK
+        assert elapsed < 1.5, f"the reply came after {elapsed:.3f} s, so the pause of 3 s was not performed"
+
+    def test_a_fault_key_in_a_per_method_override_is_not_read(self, sim):
+        """gRPC reads ``error_stub``, ``error`` and ``result`` from a per-method
+        override. It does not read the fault keys, such as ``mode`` and
+        ``latency_ms``: the call below is answered, and at once."""
+        status, body = _set_grpc(sim, "1", responses={"GetLatestBlock": {"mode": "down", "latency_ms": 3000}})
+        assert status == 200, body
+        started = time.monotonic()
+        resp = _call_get_latest_block(_GRPC_ADDRS["1"])
+        elapsed = time.monotonic() - started
+        assert resp.block.header.height == GRPC_LATEST_BLOCK
+        assert elapsed < 1.5, f"the reply came after {elapsed:.3f} s, so the latency of the override was applied"
+        assert [(row["status"], row["latency_ms"]) for row in _rows(sim)] == [("success", 0)]
+
+    def test_fail_first_n_gives_exactly_that_many_down_rows(self, sim):
+        status, body = _set_grpc(sim, "1", mode="down", fail_first_n=3, then_mode="success")
+        assert status == 200, body
+        codes = [_status_of(_call_get_latest_block, _GRPC_ADDRS["1"])[0] for _ in range(5)]
+        unavailable, ok = grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.OK
+        assert codes == [unavailable, unavailable, unavailable, ok, ok]
+        assert [row["status"] for row in _rows(sim)] == ["down", "down", "down", "success", "success"]
+        assert len(_rows(sim, status="down")) == 3
+
+
+class TestGrpcPerMethodErrors:
+    """A per-method ``error_stub`` or ``error`` override reaches the caller as a
+    status. The smart-router has a rule for each status, and its tests set each
+    one with ``error_stub``."""
+
+    @pytest.mark.parametrize("name", _ERROR_STATUS_NAMES)
+    def test_an_error_stub_gives_the_status_of_its_name_with_the_name_as_the_text(self, sim, name):
+        status, body = _set_grpc(sim, "1", responses={"GetLatestBlock": {"error_stub": name}})
+        assert status == 200, body
+        code, text = _status_of(_call_get_latest_block, _GRPC_ADDRS["1"])
+        assert (code.name, text) == (name, name)
+        assert [(row["method"], row["status"]) for row in _rows(sim)] == [("GetLatestBlock", "error")]
+
+    def test_an_error_stub_takes_its_text_from_the_key_message(self, sim):
+        override = {"error_stub": "NOT_FOUND", "message": "the block is gone"}
+        status, body = _set_grpc(sim, "1", responses={"GetLatestBlock": override})
+        assert status == 200, body
+        want = (grpc.StatusCode.NOT_FOUND, "the block is gone")
+        assert _status_of(_call_get_latest_block, _GRPC_ADDRS["1"]) == want
+
+    def test_an_error_stub_whose_name_is_no_status_gives_unknown(self, sim):
+        """``revert`` is the name of an error stub of the eth chain. It names no
+        gRPC status, so the caller gets UNKNOWN, with the name as the text."""
+        status, body = _set_grpc(sim, "1", responses={"GetLatestBlock": {"error_stub": "revert"}})
+        assert status == 200, body
+        assert _status_of(_call_get_latest_block, _GRPC_ADDRS["1"]) == (grpc.StatusCode.UNKNOWN, "revert")
+
+    def test_an_error_stub_in_the_default_entry_reaches_each_method(self, sim):
+        status, body = _set_grpc(sim, "1", responses={"default": {"error_stub": "ABORTED"}})
+        assert status == 200, body
+        want = (grpc.StatusCode.ABORTED, "ABORTED")
+        assert _status_of(_call_get_latest_block, _GRPC_ADDRS["1"]) == want
+        assert _status_of(_call_get_node_info, _GRPC_ADDRS["1"]) == want
+        assert _status_of(_call_all_balances, _GRPC_ADDRS["1"], "lava1-default-a") == want
+
+    def test_an_error_stub_of_one_method_leaves_the_other_methods_alone(self, sim):
+        status, body = _set_grpc(sim, "1", responses={"GetLatestBlock": {"error_stub": "ABORTED"}})
+        assert status == 200, body
+        assert _status_of(_call_get_latest_block, _GRPC_ADDRS["1"]) == (grpc.StatusCode.ABORTED, "ABORTED")
+        assert _call_get_node_info(_GRPC_ADDRS["1"]).default_node_info.network == "lava-sim"
+
+    @pytest.mark.parametrize(
+        "error, want_code, want_text",
+        [
+            pytest.param(
+                {"code": "ABORTED", "message": "from the override"},
+                grpc.StatusCode.ABORTED,
+                "from the override",
+                id="the-name-of-a-status",
+            ),
+            pytest.param(
+                {"code": 7, "message": "by number"},
+                grpc.StatusCode.PERMISSION_DENIED,
+                "by number",
+                id="the-number-of-a-status",
+            ),
+            pytest.param(
+                {"code": "no status has this name"}, grpc.StatusCode.UNKNOWN, "override", id="a-code-that-is-no-status"
+            ),
+        ],
+    )
+    def test_an_error_override_gives_its_code_and_its_message(self, sim, error, want_code, want_text):
+        status, body = _set_grpc(sim, "1", responses={"GetLatestBlock": {"error": error}})
+        assert status == 200, body
+        assert _status_of(_call_get_latest_block, _GRPC_ADDRS["1"]) == (want_code, want_text)
+        assert [row["status"] for row in _rows(sim)] == ["error"]
+
+
+class TestGrpcReplyFields:
+    """The fields of the two reply messages of the tendermint service, with no
+    override and with a per-method ``result`` override."""
+
+    def test_get_node_info_has_these_fields(self, sim):
+        resp = _call_get_node_info(_GRPC_ADDRS["1"])
+        node, application = resp.default_node_info, resp.application_version
+        assert (node.network, node.moniker, node.version) == ("lava-sim", "lava-sim-grpc-provider", "sim-1.0")
+        assert (application.name, application.app_name, application.version) == ("lava-sim", "lava-sim-app", "sim-1.0")
+
+    def test_a_result_override_sets_the_fields_of_get_latest_block(self, sim):
+        result = {"height": 7, "chain_id": "another-chain"}
+        status, body = _set_grpc(sim, "1", responses={"GetLatestBlock": {"result": result}})
+        assert status == 200, body
+        resp = _call_get_latest_block(_GRPC_ADDRS["1"])
+        assert (resp.block.header.height, resp.block.header.chain_id) == (7, "another-chain")
+        assert len(resp.block_id.hash) == 32
+
+    def test_a_result_override_sets_the_fields_of_get_node_info(self, sim):
+        result = {"network": "n-1", "moniker": "m-1", "version": "v-1", "app_name": "a-1", "app_version": "av-1"}
+        status, body = _set_grpc(sim, "1", responses={"GetNodeInfo": {"result": result}})
+        assert status == 200, body
+        resp = _call_get_node_info(_GRPC_ADDRS["1"])
+        node, application = resp.default_node_info, resp.application_version
+        assert (node.network, node.moniker, node.version) == ("n-1", "m-1", "v-1")
+        assert (application.name, application.app_name, application.version) == ("lava-sim", "a-1", "av-1")
+
+    def test_a_result_override_in_the_default_entry_reaches_get_latest_block(self, sim):
+        status, body = _set_grpc(sim, "1", responses={"default": {"result": {"height": 9}}})
+        assert status == 200, body
+        assert _call_get_latest_block(_GRPC_ADDRS["1"]).block.header.height == 9
+
+    @pytest.mark.parametrize(
+        "result, want_height, want_chain_id",
+        [
+            pytest.param({"height": 7}, 7, "lava-sim", id="only-the-height"),
+            pytest.param({"chain_id": "another-chain"}, GRPC_LATEST_BLOCK, "another-chain", id="only-the-chain-id"),
+            pytest.param({}, GRPC_LATEST_BLOCK, "lava-sim", id="an-empty-object"),
+            pytest.param("not an object", GRPC_LATEST_BLOCK, "lava-sim", id="no-object"),
+        ],
+    )
+    def test_a_field_that_a_result_override_does_not_set_has_the_default_of_get_latest_block(
+        self, sim, result, want_height, want_chain_id
+    ):
+        """A ``result`` override replaces the data of the chain. The builder of
+        the reply then gives its own default to each field that the override
+        does not set. A ``result`` that is no object sets no field."""
+        status, body = _set_grpc(sim, "1", responses={"GetLatestBlock": {"result": result}})
+        assert status == 200, body
+        resp = _call_get_latest_block(_GRPC_ADDRS["1"])
+        assert (resp.block.header.height, resp.block.header.chain_id) == (want_height, want_chain_id)
+        assert [row["status"] for row in _rows(sim)] == ["success"]
+
+    @pytest.mark.parametrize(
+        "result, want_network",
+        [
+            pytest.param({"network": "n-1"}, "n-1", id="only-the-network"),
+            pytest.param("not an object", "lava-sim", id="no-object"),
+        ],
+    )
+    def test_a_field_that_a_result_override_does_not_set_has_the_default_of_get_node_info(
+        self, sim, result, want_network
+    ):
+        status, body = _set_grpc(sim, "1", responses={"GetNodeInfo": {"result": result}})
+        assert status == 200, body
+        resp = _call_get_node_info(_GRPC_ADDRS["1"])
+        node, application = resp.default_node_info, resp.application_version
+        assert (node.network, node.moniker, node.version) == (want_network, "lava-sim-grpc-provider", "sim-1.0")
+        assert (application.name, application.app_name, application.version) == ("lava-sim", "lava-sim-app", "sim-1.0")
+
+    def test_a_result_override_applies_when_a_filter_does_not_name_the_endpoint(self, sim):
+        """A per-method override is not a fault of the endpoint, so a filter
+        does not hold it back. The endpoint is ``http2``, and the filter names
+        ``http``."""
+        override = {"GetLatestBlock": {"result": {"height": 7}}}
+        status, body = _set_grpc(sim, "1", transports=["http"], responses=override)
+        assert status == 200, body
+        assert _call_get_latest_block(_GRPC_ADDRS["1"]).block.header.height == 7
+
+    def test_a_missing_field_that_names_no_field_of_the_reply_leaves_the_reply_whole(self, sim):
+        status, body = _set_grpc(sim, "1", corruption_mode="missing_field", missing_field="no_such_field")
+        assert status == 200, body
+        resp = _call_get_latest_block(_GRPC_ADDRS["1"])
+        assert (resp.block.header.height, resp.block.header.chain_id) == (GRPC_LATEST_BLOCK, "lava-sim")
+        assert len(resp.block_id.hash) == 32
+        assert [row["status"] for row in _rows(sim)] == ["success"]
+
+    def test_a_result_override_of_a_wrong_type_ends_the_call_after_the_row_says_success(self, sim):
+        """A known fault, recorded as it is today. The row of a call is written
+        before the reply message is built. A ``height`` that is no number makes
+        the build of the reply fail: the caller gets UNKNOWN, and the row says
+        success. AllBalances does not have this fault: its reply is built for
+        each shape of an override."""
+        status, body = _set_grpc(sim, "1", responses={"GetLatestBlock": {"result": {"height": "not a number"}}})
+        assert status == 200, body
+        code, text = _status_of(_call_get_latest_block, _GRPC_ADDRS["1"])
+        assert code == grpc.StatusCode.UNKNOWN
+        assert "TypeError" in text
+        assert [row["status"] for row in _rows(sim)] == ["success"]
+
+
+class TestGrpcCallsThatReachNoListener:
+    """The gRPC library answers three kinds of call before the listener sees
+    them: a method of a served service that the simulator does not serve, a
+    method of a service that is not registered, and a request that is no
+    message. None of them writes a history row or a count."""
+
+    @pytest.mark.parametrize(
+        "method", ["GetSyncing", "GetBlockByHeight", "GetLatestValidatorSet", "GetValidatorSetByHeight", "ABCIQuery"]
+    )
+    def test_a_method_of_the_tendermint_service_that_is_not_served_answers_unimplemented(self, sim, method):
+        """The tendermint service has seven methods. The simulator serves
+        GetLatestBlock and GetNodeInfo, and these are the five others."""
+        code, text = _status_of(_call_raw, _GRPC_ADDRS["1"], f"/cosmos.base.tendermint.v1beta1.Service/{method}")
+        assert code == grpc.StatusCode.UNIMPLEMENTED
+        assert "Method not implemented!" in text
+
+    def test_a_method_of_a_service_that_is_not_registered_has_another_text(self, sim):
+        """The staking stubs are compiled, and the simulator does not register
+        the staking service. The text differs from the text of a method that a
+        registered service does not serve, so a test can tell the two apart."""
+        answer = _status_of(_call_raw, _GRPC_ADDRS["1"], "/cosmos.staking.v1beta1.Query/Params")
+        assert answer == (grpc.StatusCode.UNIMPLEMENTED, "Method not found!")
+
+    @pytest.mark.parametrize(
+        "path",
+        [
+            pytest.param("/cosmos.bank.v1beta1.Query/AllBalances", id="all-balances"),
+            pytest.param(_GET_LATEST_BLOCK_PATH, id="get-latest-block"),
+        ],
+    )
+    def test_a_request_that_is_no_message_answers_unknown(self, sim, path):
+        code, text = _status_of(_call_raw, _GRPC_ADDRS["1"], path, b"\xff\xff\xff")
+        assert code == grpc.StatusCode.UNKNOWN
+        assert "DecodeError" in text
+
+    def test_none_of_these_calls_writes_a_row_or_a_count(self, sim):
+        for path, payload in (
+            ("/cosmos.bank.v1beta1.Query/TotalSupply", b""),
+            ("/cosmos.base.tendermint.v1beta1.Service/GetSyncing", b""),
+            ("/cosmos.staking.v1beta1.Query/Params", b""),
+            ("/cosmos.bank.v1beta1.Query/AllBalances", b"\xff\xff\xff"),
+        ):
+            assert _status_of(_call_raw, _GRPC_ADDRS["1"], path, payload)[0] != grpc.StatusCode.OK
+        _, stats = _get(_ctrl(sim, "/stats"))
+        assert _rows(sim) == []
+        assert stats["providers"]["lava-sim-grpc:1"]["total_calls"] == 0
+
+        # The control: a call that the listener does see writes one row and one count.
+        _call_get_latest_block(_GRPC_ADDRS["1"])
+        _, stats = _get(_ctrl(sim, "/stats"))
+        assert len(_rows(sim)) == 1
+        assert stats["providers"]["lava-sim-grpc:1"]["total_calls"] == 1
+
+
+class TestGrpcDropPoint:
+    """``drop_at`` on gRPC. A gRPC handler cannot cut the connection part way,
+    so each drop point ends the call with UNAVAILABLE and "connection dropped".
+    The drop points differ in one thing: for ``after_headers`` and ``mid_body``
+    the provider sends the initial metadata first, and the status after it."""
+
+    @pytest.mark.parametrize("drop_at", ["before_headers", "after_headers", "mid_body"])
+    def test_a_grpc_client_reads_the_same_status_for_each_drop_point(self, sim, drop_at):
+        """A client cannot tell the drop points apart. That is why the tests
+        below read the frames."""
+        status, body = _set_grpc(sim, "1", mode="drop_connection", drop_at=drop_at)
+        assert status == 200, body
+        want = (grpc.StatusCode.UNAVAILABLE, "connection dropped")
+        assert _status_of(_call_get_latest_block, _GRPC_ADDRS["1"]) == want
+
+    def test_a_reply_message_comes_as_headers_then_data_then_trailers(self, sim):
+        """The control for the tests below. It shows that ``_frames_of_one_call``
+        reads the frames of a call."""
+        assert _frames_of_one_call(_GRPC_PORT_1, _GET_LATEST_BLOCK_PATH) == ["HEADERS", "DATA", "HEADERS+END_STREAM"]
+
+    def test_a_status_with_no_drop_point_comes_as_one_frame(self, sim):
+        status, body = _set_grpc(sim, "1", mode="down")
+        assert status == 200, body
+        assert _frames_of_one_call(_GRPC_PORT_1, _GET_LATEST_BLOCK_PATH) == ["HEADERS+END_STREAM"]
+
+    @pytest.mark.parametrize(
+        "drop_at, want_frames",
+        [
+            pytest.param("before_headers", ["HEADERS+END_STREAM"], id="before-headers-sends-the-status-alone"),
+            pytest.param("after_headers", ["HEADERS", "HEADERS+END_STREAM"], id="after-headers-sends-metadata-first"),
+            pytest.param("mid_body", ["HEADERS", "HEADERS+END_STREAM"], id="mid-body-sends-metadata-first"),
+        ],
+    )
+    def test_two_drop_points_send_the_initial_metadata_before_the_status(self, sim, drop_at, want_frames):
+        status, body = _set_grpc(sim, "1", mode="drop_connection", drop_at=drop_at)
+        assert status == 200, body
+        assert _frames_of_one_call(_GRPC_PORT_1, _GET_LATEST_BLOCK_PATH) == want_frames
+        assert [(row["method"], row["status"]) for row in _rows(sim)] == [("GetLatestBlock", "drop_connection")]
+
 
 # ─────────────────────────────────────────────────────────────────────────────
 # Cross-pool fault isolation
```

- [x] **Step 2: Run the tests**

Check first that no other simulator runs on the machine: `lsof -nP -iTCP:19000 -sTCP:LISTEN` must print nothing.

Run: `python -m pytest tests/test_simulator_grpc.py -q -p no:cacheprovider`

Expected: `139 passed` in about 40 seconds. That is the 60 tests of `main` and the 79 new cases. One test takes 30 seconds (choice 1).

- [x] **Step 3: Show that each new case can fail**

Break `server.py`, `provider_simulator/listeners/grpc.py` or `provider_simulator/chains/lava.py` in one place, as in Task 1. The section "The mutations" has the list.

Result: each of the 65 runs made one test or more fail, and each of the 79 new cases failed in one run or more.

- [x] **Step 4: Run the checks of CI on the file**

```bash
black --check tests/test_simulator_grpc.py
ruff check tests/test_simulator_grpc.py
mypy tests/test_simulator_grpc.py
```

Expected: no change, no finding and no issue.

- [x] **Step 5: Commit**

```bash
git add tests/test_simulator_grpc.py
git commit -m "test(grpc): record what a gRPC client reads from the simulator, fault by fault"
```

---

### Task 3: What `start()` wires

**Files:**
- Create: `tests/integration/__init__.py` (an empty file)
- Create: `tests/integration/test_server_start_wiring.py`

Victoria ordered on 2026-10-08 that a new test file is not put directly into `tests/`, and she approved the new package `tests/integration/`. The three tests are integration tests: each one starts a real simulator and talks to it over sockets.

**Interfaces:**
- Consumes: the fixture `sim_server` of `tests/conftest.py`, which reaches a test in a folder below `tests/`. `FakeRespStore` of `tests/resp_fake_store.py`. `SimulatorServer` of `server.py`, with its arguments `control_port`, `cache_ports`, `resp_control_port`, `resp_proxy_ports` and `resp_store`.
- Produces: nothing that another task uses.

- [x] **Step 1: Write the tests**

Apply this diff. It adds three tests: `test_start_serves_the_cache_simulator_that_the_control_routes_stage`, `test_start_wires_the_resp_proxy_to_the_store_that_it_was_given` and `test_the_simulator_starts_with_no_grpcio_and_serves_its_http_endpoints`.

```diff
diff --git a/tests/integration/__init__.py b/tests/integration/__init__.py
new file mode 100644
index 0000000..e69de29
diff --git a/tests/integration/test_server_start_wiring.py b/tests/integration/test_server_start_wiring.py
new file mode 100644
index 0000000..9852c9c
--- /dev/null
+++ b/tests/integration/test_server_start_wiring.py
@@ -0,0 +1,288 @@
+"""What ``SimulatorServer.start()`` wires: three things that no other test shows.
+
+``start()`` gives each provider endpoint its listener. It also starts parts
+that are not provider endpoints: the listener of the cache simulator, the RESP
+proxy with the store that it forwards to, and the gRPC half, which it leaves
+out when ``grpcio`` is not installed.
+
+Three things about these parts had no test:
+
+- The gRPC port of the cache simulator serves what the control routes stage.
+  The other tests of the cache simulator build their piece by hand.
+- The RESP proxy forwards a command to its store. ``tests/test_resp_wiring.py``
+  shows that ``start()`` binds the two RESP listeners of the shared simulator
+  and wires the control listener. That simulator has no store behind it, so
+  the file cannot show a command that goes through.
+- The simulator starts with no ``grpcio``.
+
+A change of ``start()`` must not take one of the three away, and each test below
+fails when it does.
+
+Ports: the cache test uses the shared simulator of the session. The two other
+tests start a simulator of their own, on this file's own block of ports. The
+block is below the range that the kernel gives to client sockets.
+"""
+
+from __future__ import annotations
+
+import base64
+import json
+import os
+import socket
+import subprocess
+import sys
+import urllib.error
+import urllib.request
+
+import pytest
+
+import server as server_module
+from constants import CACHE_SIM_PORTS, CONTROL_PORT
+from provider_simulator import topology
+from tests.resp_fake_store import FakeRespStore
+
+# This file is two folders below the root of the repository.
+_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
+
+# The simulator of the RESP test: one REST provider, one RESP proxy, one RESP
+# control listener, and no cache simulator.
+_RESP_SIM_CONTROL, _RESP_REST = 29731, 28731
+_RESP_PROXY, _RESP_CONTROL = 28735, 29735
+_RESP_ROWS = (
+    ("lava-wiring-sim", "lava", "1", "LavaWiringPrimaryProvider1", False, "", (("rest", "http", _RESP_REST),)),
+)
+
+# The simulator of the test with no grpcio: one REST provider, one gRPC
+# provider and one cache simulator. It runs in a process of its own.
+_NO_GRPCIO_CONTROL, _NO_GRPCIO_REST = 29741, 28741
+_NO_GRPCIO_GRPC, _NO_GRPCIO_CACHE = 28742, 28745
+
+_BLOCKS_LATEST = "/cosmos/base/tendermint/v1beta1/blocks/latest"
+
+
+def _call(request: urllib.request.Request) -> tuple[int, dict]:
+    try:
+        with urllib.request.urlopen(request, timeout=5) as reply:
+            return reply.status, json.loads(reply.read())
+    except urllib.error.HTTPError as exc:
+        return exc.code, json.loads(exc.read())
+
+
+def _get(url: str) -> tuple[int, dict]:
+    return _call(urllib.request.Request(url, method="GET"))
+
+
+def _post(url: str, body: dict) -> tuple[int, dict]:
+    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
+    request.add_header("Content-Type", "application/json")
+    return _call(request)
+
+
+# ── the cache simulator ───────────────────────────────────────────────────────
+
+
+def _get_relay(grpc, port: int) -> dict:
+    """One lookup against a cache simulator, with raw JSON bytes, as the
+    smart-router sends it. Returns the decoded reply.
+
+    The call waits until the channel is ready: ``wait_ready()`` of the
+    simulator does not wait for the cache port, and the thread of the cache
+    simulator starts last."""
+    lookup = {
+        "request_hash": base64.b64encode(bytes.fromhex("deadbeef")).decode(),
+        "block_hash": None,
+        "finalized": False,
+        "requested_block": 18_000_000,
+        "shared_state_id": "",
+        "chain_id": "ETH1",
+        "seen_block": 0,
+        "blocks_hashes_to_heights": None,
+    }
+    with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
+        rpc = channel.unary_unary(
+            "/smartrouter.pairing.RelayerCache/GetRelay",
+            request_serializer=lambda raw: raw,
+            response_deserializer=lambda raw: raw,
+        )
+        return json.loads(rpc(json.dumps(lookup).encode(), timeout=5, wait_for_ready=True))
+
+
+def test_start_serves_the_cache_simulator_that_the_control_routes_stage(sim_server):
+    """An entry that is staged over HTTP on the control port comes back from the
+    gRPC port of the cache simulator, and the lookup shows in the call log of
+    the control port. So ``start()`` bound the port, and it gave the listener
+    the same cache simulator that the control routes hold."""
+    grpc = pytest.importorskip("grpc", reason="the cache simulator speaks gRPC")
+    control = f"http://127.0.0.1:{CONTROL_PORT}"
+    assert sim_server.cache_sims_enabled is True
+    # The count below is the count of this test alone.
+    _post(control + "/cache/secondary/reset", {})
+    try:
+        status, staged = _post(control + "/cache/secondary/entry", {"mode": "hit", "entry": {"result": "0xfeed"}})
+        assert status == 200, staged
+
+        reply = _get_relay(grpc, CACHE_SIM_PORTS["secondary"])
+        assert json.loads(base64.b64decode(reply["reply"]["data"]))["result"] == "0xfeed"
+
+        status, calls = _get(control + "/cache/secondary/calls")
+        assert (status, calls["count"]) == (200, 1)
+    finally:
+        _post(control + "/cache/secondary/reset", {})
+
+
+# ── the RESP proxy ────────────────────────────────────────────────────────────
+
+
+def _a_simulator(rows: tuple, **settings) -> server_module.SimulatorServer:
+    """A second simulator with a topology of its own. The table is swapped only
+    while this simulator builds its registry."""
+    shipped = topology.TOPOLOGY
+    topology.TOPOLOGY = rows
+    try:
+        return server_module.SimulatorServer(host="127.0.0.1", scenario_ttl_s=0, **settings)
+    finally:
+        topology.TOPOLOGY = shipped
+
+
+def test_start_wires_the_resp_proxy_to_the_store_that_it_was_given():
+    """A command that is sent to the proxy port reaches the store, and the
+    answer of the store comes back. The RESP control listener reads the same
+    store. So ``start()`` bound both listeners, and it gave each one its
+    target."""
+    with FakeRespStore() as store:
+        simulator = _a_simulator(
+            _RESP_ROWS,
+            control_port=_RESP_SIM_CONTROL,
+            cache_ports={},
+            resp_control_port=_RESP_CONTROL,
+            resp_proxy_ports={"primary": _RESP_PROXY},
+            resp_store=("127.0.0.1", store.port),
+        )
+        simulator.start()
+        try:
+            simulator.wait_ready(20.0)
+            seen_before = len(store.commands)
+            with socket.create_connection(("127.0.0.1", _RESP_PROXY), timeout=5) as router:
+                router.sendall(b"*1\r\n$4\r\nPING\r\n")
+                assert router.recv(64) == b"+PONG\r\n"
+            assert store.commands[seen_before:] == [["PING"]]
+
+            store.put("sr:wiring:probe", "value-1")
+            status, keys = _get(f"http://127.0.0.1:{_RESP_CONTROL}/resp/keys")
+            assert status == 200, keys
+            assert [(entry["key"], entry["value"]) for entry in keys["entries"]] == [("sr:wiring:probe", "value-1")]
+        finally:
+            simulator.stop()
+
+
+# ── a machine with no grpcio ──────────────────────────────────────────────────
+
+# One simulator in a process where ``import grpc`` fails. It prints one JSON
+# line with what it saw.
+_NO_GRPCIO_SCRIPT = """
+import json
+import socket
+import sys
+import urllib.error
+import urllib.request
+
+# Make each import of grpc fail, as on a machine with no grpcio.
+sys.modules["grpc"] = None
+
+import server
+from provider_simulator import topology
+
+control_port, rest_port, grpc_port, cache_port = (int(value) for value in sys.argv[1:5])
+topology.TOPOLOGY = [
+    ("lava-nogrpcio-sim", "lava", "1", "LavaNogrpcioPrimaryProvider1", False, "", (("rest", "http", rest_port),)),
+    ("lava-nogrpcio-sim", "lava", "2", "LavaNogrpcioPrimaryProvider2", False, "", (("grpc", "http2", grpc_port),)),
+]
+
+
+def accepts(port):
+    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
+        probe.settimeout(0.5)
+        return probe.connect_ex(("127.0.0.1", port)) == 0
+
+
+def get(url):
+    try:
+        with urllib.request.urlopen(url, timeout=5) as reply:
+            return reply.status, json.loads(reply.read())
+    except urllib.error.HTTPError as exc:
+        return exc.code, json.loads(exc.read())
+
+
+simulator = server.SimulatorServer(
+    host="127.0.0.1",
+    control_port=control_port,
+    scenario_ttl_s=0,
+    cache_ports={"secondary": cache_port},
+    resp_proxy_ports={},
+)
+simulator.start()
+for port in (control_port, rest_port):
+    for _ in range(100):
+        if accepts(port):
+            break
+    else:
+        sys.exit(f"port {port} did not open")
+
+control = f"http://127.0.0.1:{control_port}"
+ready_status, ready = get(control + "/ready")
+print(
+    json.dumps(
+        {
+            "grpc_enabled": simulator.grpc_enabled,
+            "cache_sims_enabled": simulator.cache_sims_enabled,
+            "rest_status": get(f"http://127.0.0.1:{rest_port}__BLOCKS_LATEST__")[0],
+            "health_status": get(control + "/health")[0],
+            "ready_status": ready_status,
+            "ready_missing_ports": ready.get("missing_ports"),
+            "grpc_port_accepts": accepts(grpc_port),
+            "cache_port_accepts": accepts(cache_port),
+        }
+    ),
+    flush=True,
+)
+simulator.stop()
+""".replace("__BLOCKS_LATEST__", _BLOCKS_LATEST)
+
+
+def test_the_simulator_starts_with_no_grpcio_and_serves_its_http_endpoints():
+    """``grpcio`` is an optional dependency. With none, ``start()`` leaves out
+    the gRPC endpoints and the cache simulator, and it still serves each HTTP
+    endpoint and the control API. /ready then answers 503 and names the gRPC
+    port that did not open: the simulator serves, and it does not report ready.
+
+    This test records that answer of /ready as it is today. It does not say
+    that 503 is the right answer for a simulator that serves HTTP only.
+
+    The simulator runs in a process of its own, because this process has
+    ``grpcio`` loaded already."""
+    result = subprocess.run(
+        [
+            sys.executable,
+            "-c",
+            _NO_GRPCIO_SCRIPT,
+            str(_NO_GRPCIO_CONTROL),
+            str(_NO_GRPCIO_REST),
+            str(_NO_GRPCIO_GRPC),
+            str(_NO_GRPCIO_CACHE),
+        ],
+        cwd=_REPO_ROOT,
+        capture_output=True,
+        text=True,
+        timeout=60,
+    )
+    assert result.returncode == 0, result.stderr
+    assert json.loads(result.stdout.splitlines()[-1]) == {
+        "grpc_enabled": False,
+        "cache_sims_enabled": False,
+        "rest_status": 200,
+        "health_status": 200,
+        "ready_status": 503,
+        "ready_missing_ports": [_NO_GRPCIO_GRPC],
+        "grpc_port_accepts": False,
+        "cache_port_accepts": False,
+    }
```

- [x] **Step 2: Run the tests**

Run: `python -m pytest tests/integration -q -p no:cacheprovider`

Expected: `3 passed`.

- [x] **Step 3: Show that each new test can fail**

Break `server.py` in one place, as in Task 1. The section "The mutations" has the 12 places. Result: each of the 12 mutations made one test or more of the three fail, and each test failed under one mutation or more.

- [x] **Step 4: Run the checks of CI on the folder**

```bash
black --check tests/integration
ruff check tests/integration
mypy tests/integration
```

Expected: no change, no finding and no issue.

- [x] **Step 5: Commit**

```bash
git add tests/integration/__init__.py tests/integration/test_server_start_wiring.py
git commit -m "test(server): record what start() wires for the cache simulator, the RESP proxy and a machine with no grpcio"
```

---

### Task 4: The whole suite and the checks of CI

**Files:** none.

**Interfaces:**
- Consumes: the three commits of Tasks 1 to 3.
- Produces: the proof that the new tests break no other test.

- [x] **Step 1: Check that no other test run is active**

Run: `lsof -nP -iTCP:19000 -sTCP:LISTEN`

Expected: no output. Another session can run a timed test on the local k3d cluster: ask it before a run of the whole suite, because the run loads the machine for about four minutes.

Done in another way on 2026-10-08. The session that ran timed tests on the local k3d cluster gave a time for each run of this plan, and `lsof` printed nothing before the socket run of 17:50.

- [x] **Step 2: Run the whole suite**

Run: `python -m pytest tests -q -p no:cacheprovider`

Expected: `1774 passed`. That is the 1611 tests of `main` at `6f4e933` and the 163 new cases: 81 in `tests/test_listener_grpc.py`, 79 in `tests/test_simulator_grpc.py` and 3 in `tests/integration/test_server_start_wiring.py`. No test fails and no test is skipped.

RUN on 2026-10-08 from 18:06:34 to 18:09:54 IDT, on macOS, on the commit `e1b4cb0`: `1774 passed in 199.83s (0:03:19)`.

- [x] **Step 3: Run the three files three times in a row**

Run three times: `python -m pytest tests/test_listener_grpc.py tests/test_simulator_grpc.py tests/integration -q -p no:cacheprovider`

Expected each time: `256 passed`. RUN on 2026-10-08 from 18:04:10 to 18:06:15 IDT, on the committed files: `256 passed` three times, in 41.05 s, 41.62 s and 41.11 s.

- [x] **Step 4: Run the three checks with the versions of CI**

```bash
pip install 'black==26.5.1' 'ruff==0.15.20' 'mypy==2.2.0'
black --check .
ruff check .
mypy .
```

Expected: black prints "would be left unchanged" for every file. ruff prints `All checks passed!`. mypy prints `Success: no issues found`.

RUN on 2026-10-08 at 18:06 IDT on the files of the commit `e1b4cb0`, with black 26.5.1, ruff 0.15.20 and mypy 2.2.0 in a virtual environment that holds these three tools only: black `106 files would be left unchanged.`, ruff `All checks passed!`, mypy `Success: no issues found in 106 source files`. The job `lint` of CI passed on the same commit.

- [x] **Step 5: Linux**

The job `test` of the pull request runs the suite on `ubuntu-latest`. It is the first Linux run of the new socket tests. Open the log of a red check before you call it red. Three kinds of new test can differ on Linux: the HTTP/2 frames of `TestGrpcDropPoint`, the time bounds of `TestGrpcWhichCallsWait`, and the tests that start a simulator of their own.

RUN BY CI on 2026-10-08, on the commit `e1b4cb0`: the job `test` gave `1774 passed in 210.28s (0:03:30)`, with no test skipped. The three kinds of new test that can differ on Linux passed.

---

### Task 5: The pull request

**Files:** none.

**Interfaces:**
- Consumes: the branch of Tasks 1 to 4.
- Produces: the merged tests, which pull request 2b must pass.

The push and the pull request need Victoria's go. The merge needs its own go. Use the skill `pr`.

- [x] **Step 1: Open the pull request**

The title: `test(grpc): record today's gRPC behaviour before the move into Listener.serve`. The command has `--label no-ticket` and `--assignee`. The body has four parts: Why, What changed, How to verify, and What was broken to prove each test. The last part holds the counts of the section "The mutations" and names the kinds of break. The body holds no ticket key and no path of one machine.

Done on 2026-10-08 on Victoria's order "open pr": pull request 140 of `provider-simulator`, with the label `no-ticket` and the assignee. The job `comment-jira` printed "No Jira key found in PR body, branch, or title. Skipping.", so nothing was written to Jira.

- [x] **Step 2: Read the checks**

Run: `gh pr checks <number>`

Expected: `lint`, `test` and "Suite must pass before anything is published" pass. The job `test` must print `1774 passed`.

Done on 2026-10-08 for the commit `e1b4cb0`: `lint`, `test`, "Suite must pass before anything is published" and "Build and publish image" pass. The build of a pull request pushes no image. `check-closes-tag` is skipped for the label. On the first push that check had one red run: GitHub attached the label one second after it opened the pull request, and the run of the event `opened` did not see it. The push of the commit `e1b4cb0` cleared it.

- [x] **Step 3: After the merge**

Read the three test files on `main`, and run the three files there again. Pull request 2b then starts from that `main`.

Done on 2026-10-08. `git diff origin/main e1b4cb0` prints nothing: the files of `main` at `273429e` are the files of the last commit of the branch. That commit gave `1774 passed` on macOS and in both suite jobs on Linux. The run "Lint and test" of `main` at `273429e` (run 37800511394) passed too: `1774 passed in 215.69s`. The three files were not run one more time on a checkout of `main`, because the files are the same.

**Rollback:** revert this pull request. It holds tests only, so no behaviour changes with it or without it.

---

### Task 6: The review of the pull request

**Files:**
- Modify: `tests/test_listener_grpc.py`, `tests/test_simulator_grpc.py`, `tests/integration/test_server_start_wiring.py`

**Interfaces:**
- Consumes: pull request 140, with the three commits of Tasks 1 to 3.
- Produces: the commit `e1b4cb0` on the same branch. The diffs of Tasks 1 to 3 above show the files after this commit.

Victoria ran the skill `can-i-merge` on pull request 140 on 2026-10-08. The skill has two readers with no context of the work: one compares the pull request with the ticket and the design, and one argues that the pull request must not merge. The author read the whole change again too.

- [x] **Step 1: The faults that the review found, and what the commit does for each one**

| Fault | Found by | What the commit does |
|---|---|---|
| The grid held the data of a reply message: 19 expected values were dictionaries of `GrpcPlan.data`, and 3 of them held an override that was not merged. `Listener.serve` returns a built message, so pull request 2b could not keep these values, and the design says that no expected value of the grid changes in 2b. | The second reader | The grid holds no content of a reply. `_Decision` has no field `reply`. A real client reads the content in Task 2. |
| No test held the default of a reply builder. A `result` override that sets one field takes each other field from the builder (`server.py`, `build_latest_block` and `build_node_info`). The first proof run broke the default moniker of the builder, and no test failed. The author called that break "equivalent", and that was wrong. | The second reader | Six cases in `TestGrpcReplyFields`: an override that sets one field, an empty object, and a `result` that is no object. Seven breaks of the defaults now fail them. |
| `test_a_filter_that_does_not_name_the_endpoint_does_not_use_up_fail_first_n` asserted three `OK`. It passed also with the window used up. | The author and the second reader | The test has one provider with two endpoints. After three calls on the endpoint that the filter does not name, the named endpoint still gets the mode for its first call. |
| No test read the status of the drop points `after_headers` and `mid_body` with a real client. The older tests accept two statuses. | The author and the second reader | `test_a_grpc_client_reads_the_same_status_for_each_drop_point`, three cases. |
| No case set a fault of the provider with a per-method override. In `plan` the fault comes first. | The second reader | Three cases in the grid. |
| The behaviour that 2b changes on purpose had no test: `latency_ms` under a filter that does not name the endpoint (section 9.2 of the design, row 2). Choice 3 of this plan left it out. The body of the pull request said that each change of the move shows as an edited expected value. | The second reader | `test_latency_ms_is_paid_today_under_a_filter_that_does_not_name_the_endpoint`, beside the grid. Pull request 2b edits its two expected values to 0. |
| A `missing_field` that names no field of the reply leaves the reply whole (`server.py`, the guard in `_perform`). No test held it. | The second reader | One test in `TestGrpcReplyFields`. |
| A `result` override under a filter that does not name the endpoint was read in the grid only, as data. | The author, after the first fault | One socket test in `TestGrpcReplyFields`. |
| The cache test called the cache port with no wait. `wait_ready()` does not wait for that port, and the cache thread starts last. It also counted calls with no reset before it. | The second reader, by a read. Both Linux jobs had passed. | The call waits for its channel (`wait_for_ready=True`), and the test resets the call log before it counts. |
| The docstring of the wiring file said that no other test starts these parts through `start()`. `tests/test_resp_wiring.py` does it for the two RESP listeners. | The author and the second reader | The docstring names what was missing: the forward to a store. The message of commit `c5fd800` keeps the wrong sentence. |
| The docstring of `TestGrpcWhichCallsWait` said "each status but one". A hung call does not wait for `latency_ms` too. | The author and the second reader | The docstring names both. |
| The test with no `grpcio` states the 503 of `/ready` as a fact. `server.py` says of the cache port that "a cluster without it would never become ready". | The second reader | The docstring says that the test records the answer and does not judge it. |
| The list "Coverage" at the top of `tests/test_simulator_grpc.py` did not name the six new classes. | The author | Six entries. |

- [x] **Step 2: Prove each changed or new case, on the files of the commit**

The three mutation sets ran again on the committed files. The section "The mutations" has each run.

- [x] **Step 3: Three things that the review found and that this pull request does not decide**

1. **The recorded fault and pull request 2b.** `test_a_result_override_of_a_wrong_type_ends_the_call_after_the_row_says_success` holds the row `success` of a call whose reply was not built. `Listener.serve` builds a reply before it finishes the row (`provider_simulator/listeners/base.py`, lines 237 and 254 to 260 at `6f4e933`). So in 2b the row of that call changes, unless 2b builds the fault again on purpose. Three ways: fix the two builders before 2b, in a small pull request of its own; make it a new row of section 9.2 of the design; or keep today's order in 2b. Victoria decides. By a read, `build_node_info` has the same fault, and no test records it.
2. **`test_an_endpoint_that_is_refused_leaves_the_simulator_up_and_not_ready`** makes its refused endpoint with a patch of `SERVED_METHODS` against servicers that are written by hand. The older test of `main` beside it does the same. If 2b makes the servicers from the table, the two tests need another way to make the refusal. The plan of 2b must say which.
3. **The answer of `/ready` with no `grpcio`.** It is 503 today. The test records it. Whether 503 is right for a simulator that serves HTTP only is a question for Victoria, and it is not part of the design.

## The mutations

Each row is one run. The source file was changed in one place, the test file ran, and the source file was put back. "Tests that failed" is the count of failed tests and the count of tests that ran. A run of one slow test has a small second number.

These are the runs of 2026-10-08 after the review of the pull request (Task 6), on the files of the commit `e1b4cb0`: Task 1 at 17:47 IDT, Task 2 from 17:50 to 18:02, Task 3 from 18:02 to 18:04. The first proof runs of the same day (73, 53 and 12 runs) were made before the pull request was opened. Two of those proofs were older than their test: one time bound of Task 2 changed after its failing run, and the 12 runs of Task 3 were made before the file moved to `tests/integration/`. One of the 53 runs made no test fail, and the author called its break "equivalent". That was wrong: it showed a missing test for the defaults of the reply builders (Task 6).

151 runs in all: 74 for Task 1, 65 for Task 2 and 12 for Task 3. Each of the 151 made one test or more fail. Each new case failed in one run or more: 81 of 81 in Task 1, 79 of 79 in Task 2 and 3 of 3 in Task 3.

**Task 1, `tests/test_listener_grpc.py`.** 74 runs.

| # | What was broken | File | Tests that failed |
|---|---|---|---|
| 1 | down: another text | `provider_simulator/listeners/grpc.py` | 5 of 114 |
| 2 | down: another status | `provider_simulator/listeners/grpc.py` | 8 of 114 |
| 3 | down: the row records the configured latency | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 4 | down: the row has another status label | `provider_simulator/listeners/grpc.py` | 8 of 114 |
| 5 | hang: the row records the configured latency | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 6 | hang: the adapter is not told to hang | `provider_simulator/listeners/grpc.py` | 2 of 114 |
| 7 | hang: another text | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 8 | hang: another status | `provider_simulator/listeners/grpc.py` | 2 of 114 |
| 9 | drop: the drop point is lost | `provider_simulator/listeners/grpc.py` | 4 of 114 |
| 10 | drop: every drop point becomes before_headers | `provider_simulator/listeners/grpc.py` | 2 of 114 |
| 11 | drop: another text | `provider_simulator/listeners/grpc.py` | 4 of 114 |
| 12 | drop: the row records no latency | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 13 | rate_limit: the text is the rate_limit_body | `provider_simulator/listeners/grpc.py` | 4 of 114 |
| 14 | rate_limit: another status | `provider_simulator/listeners/grpc.py` | 7 of 114 |
| 15 | rate_limit: the row has another status label | `provider_simulator/listeners/grpc.py` | 6 of 114 |
| 16 | error: the number wins over the name | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 17 | error: the number is not read | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 18 | error: the fallback is not UNKNOWN | `provider_simulator/listeners/grpc.py` | 5 of 114 |
| 19 | error: the text is lost | `provider_simulator/listeners/grpc.py` | 9 of 114 |
| 20 | error: http_status 503 changes the status | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 21 | override: the default entry is not read for an error | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 22 | override: the default entry wins over the entry of the method | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 23 | override: the entry of another method applies | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 24 | override: a filter that does not name the endpoint holds an error override back | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 25 | override: a corruption comes before an error override | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 26 | error_stub: a name that is no status gives INTERNAL | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 27 | error_stub: the key message is not read | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 28 | error_stub: the text is not the name of the stub | `provider_simulator/listeners/grpc.py` | 8 of 114 |
| 29 | error_stub: an error override in the same entry wins | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 30 | error_stub: a result override in the same entry wins | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 31 | error override: the name of a status is not read | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 32 | error override: the number of a status is not read | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 33 | error override: the fallback is not UNKNOWN | `provider_simulator/listeners/grpc.py` | 2 of 114 |
| 34 | error override: the default text is lost | `provider_simulator/listeners/grpc.py` | 2 of 114 |
| 35 | error override: the text is lost | `provider_simulator/listeners/grpc.py` | 2 of 114 |
| 36 | fault keys of a per-method override are merged (mode, error_probability) | `provider_simulator/listeners/grpc.py` | 4 of 114 |
| 37 | the latency of a per-method override is read | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 38 | the filters and the fail_first_n window are not asked | `provider_simulator/listeners/grpc.py` | 6 of 114 |
| 39 | corruption: the filters are not asked | `provider_simulator/listeners/grpc.py` | 3 of 114 |
| 40 | wrong_type: another status | `provider_simulator/listeners/grpc.py` | 4 of 114 |
| 41 | wrong_type: the text does not name the field | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 42 | wrong_type: another text | `provider_simulator/listeners/grpc.py` | 3 of 114 |
| 43 | corruption: invalid_json turns the reply into a status | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 44 | corruption: four modes fall through to a clean reply | `provider_simulator/listeners/grpc.py` | 7 of 114 |
| 45 | corruption: another status for the four modes | `provider_simulator/listeners/grpc.py` | 6 of 114 |
| 46 | corruption: the text does not name the mode | `provider_simulator/listeners/grpc.py` | 4 of 114 |
| 47 | corruption: the row of the four modes says success | `provider_simulator/listeners/grpc.py` | 5 of 114 |
| 48 | corruption: the row of wrong_type says success | `provider_simulator/listeners/grpc.py` | 4 of 114 |
| 49 | missing_field: the adapter is not told to clear the field | `provider_simulator/listeners/grpc.py` | 2 of 114 |
| 50 | missing_field: a field alone clears the field | `provider_simulator/listeners/grpc.py` | 2 of 114 |
| 51 | missing_field: a default field is cleared when the scenario names none | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 52 | a status of a fault gives way to a corruption (rate_limit) | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 53 | a status of a fault gives way to a corruption (error) | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 54 | a status of a fault gives way to a corruption (down) | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 55 | success: the row records no latency | `provider_simulator/listeners/grpc.py` | 5 of 114 |
| 56 | success: the row is not finished | `provider_simulator/listeners/grpc.py` | 28 of 114 |
| 57 | success: the adapter is told no latency | `provider_simulator/listeners/grpc.py` | 4 of 114 |
| 58 | a status waits for no latency (per-method error_stub) | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 59 | a status waits for no latency (per-method error) | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 60 | a status waits for no latency (rate_limit) | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 61 | a status waits for no latency (error) | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 62 | a status waits for no latency (the four corruption modes) | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 63 | a status waits for no latency (wrong_type) | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 64 | a status waits for no latency (drop_connection) | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 65 | a down provider waits for the latency | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 66 | a hung provider waits for the latency too | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 67 | the lava headers are not kept on the row | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 68 | the row names another port | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 69 | the latency obeys the filters (the rule of the other interfaces) | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 70 | a per-method override comes before rate_limit | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 71 | a per-method override comes before an error | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 72 | a per-method override comes before a drop | `provider_simulator/listeners/grpc.py` | 1 of 114 |
| 73 | the row records another method | `provider_simulator/listeners/grpc.py` | 3 of 114 |
| 74 | fault policy: an endpoint that the filter does not name uses up the fail_first_n window | `provider_simulator/fault_policy.py` | 1 of 114 |

**Task 2, `tests/test_simulator_grpc.py`.** 65 runs. The four runs with one test are runs of the 30-second test or of the test of the held row.

| # | What was broken | File | Tests that failed |
|---|---|---|---|
| 1 | adapter: the text of a status is lost | `server.py` | 39 of 138 |
| 2 | adapter: a status does not wait for the latency | `server.py` | 5 of 138 |
| 3 | adapter: a reply message does not wait for the latency | `server.py` | 2 of 138 |
| 4 | adapter: no drop point sends the initial metadata | `server.py` | 2 of 138 |
| 5 | adapter: each status sends the initial metadata first | `server.py` | 2 of 138 |
| 6 | adapter: a reply message is replaced by a status | `server.py` | 57 of 138 |
| 7 | adapter: a pause is performed | `server.py` | 1 of 138 |
| 8 | adapter: the row is written 1.8 s after the call arrives | `server.py` | 1 of 1 |
| 9 | adapter: a hung call ends after 3 seconds | `server.py` | 1 of 1 |
| 10 | adapter: a hung call is not held | `server.py` | 1 of 1 |
| 11 | listener: a hung call has another text | `provider_simulator/listeners/grpc.py` | 1 of 1 |
| 12 | listener: the row of a hung call records the configured latency | `provider_simulator/listeners/grpc.py` | 1 of 1 |
| 13 | chain: the node info has another moniker | `provider_simulator/chains/lava.py` | 1 of 138 |
| 14 | builder: the moniker of the reply is not the moniker of the chain | `server.py` | 4 of 138 |
| 15 | builder: the node info has another application name | `server.py` | 5 of 138 |
| 16 | builder: a result override is not merged into the reply | `server.py` | 11 of 138 |
| 17 | builder: the chain id of a result override is not read | `server.py` | 2 of 138 |
| 18 | builder: the version of a result override is not read | `server.py` | 1 of 138 |
| 19 | builder: a height of a wrong type gives a reply | `server.py` | 1 of 138 |
| 20 | registration: GetSyncing is served | `provider_simulator/listeners/grpc.py`, `server.py` | 2 of 138 |
| 21 | registration: each method of the tendermint service is served | `provider_simulator/listeners/grpc.py`, `server.py` | 6 of 138 |
| 22 | registration: the staking service is registered | `server.py` | 3 of 138 |
| 23 | registration: the bank service is registered by hand, and each request parses | `server.py` | 7 of 138 |
| 24 | registration: the tendermint service is registered by hand, and each request parses | `server.py` | 6 of 138 |
| 25 | the check of the servicers does not stop the endpoint | `server.py` | 2 of 138 |
| 26 | /ready does not report a port that does not accept | `server.py` | 1 of 138 |
| 27 | listener: the text of rate_limit is the rate_limit_body | `provider_simulator/listeners/grpc.py` | 3 of 138 |
| 28 | listener: the text of an error is lost | `provider_simulator/listeners/grpc.py` | 4 of 138 |
| 29 | listener: the number of an error is not read | `provider_simulator/listeners/grpc.py` | 2 of 138 |
| 30 | listener: wrong_type does not name the field | `provider_simulator/listeners/grpc.py` | 1 of 138 |
| 31 | listener: wrong_type has another text | `provider_simulator/listeners/grpc.py` | 2 of 138 |
| 32 | listener: the text of a corruption does not name the mode | `provider_simulator/listeners/grpc.py` | 4 of 138 |
| 33 | listener: invalid_json turns the reply into a status | `provider_simulator/listeners/grpc.py` | 1 of 138 |
| 34 | listener: rate_limit gives way to a corruption | `provider_simulator/listeners/grpc.py` | 1 of 138 |
| 35 | listener: an error gives way to a corruption | `provider_simulator/listeners/grpc.py` | 1 of 138 |
| 36 | listener: the row of a corruption says success | `provider_simulator/listeners/grpc.py` | 4 of 138 |
| 37 | listener: the row of wrong_type says success | `provider_simulator/listeners/grpc.py` | 2 of 138 |
| 38 | listener: the row of rate_limit says error | `provider_simulator/listeners/grpc.py` | 3 of 138 |
| 39 | listener: a down provider waits for the latency | `provider_simulator/listeners/grpc.py` | 1 of 138 |
| 40 | listener: the text of an error_stub is not its name | `provider_simulator/listeners/grpc.py` | 19 of 138 |
| 41 | listener: the key message of an error_stub is not read | `provider_simulator/listeners/grpc.py` | 1 of 138 |
| 42 | listener: an error_stub whose name is no status gives INTERNAL | `provider_simulator/listeners/grpc.py` | 1 of 138 |
| 43 | listener: the default entry is not read for an error | `provider_simulator/listeners/grpc.py` | 1 of 138 |
| 44 | listener: the entry of another method applies | `provider_simulator/listeners/grpc.py` | 1 of 138 |
| 45 | listener: the name of a status in an error override is not read | `provider_simulator/listeners/grpc.py` | 1 of 138 |
| 46 | listener: the number of a status in an error override is not read | `provider_simulator/listeners/grpc.py` | 1 of 138 |
| 47 | listener: the default text of an error override is lost | `provider_simulator/listeners/grpc.py` | 1 of 138 |
| 48 | listener: the row of an error override says success | `provider_simulator/listeners/grpc.py` | 3 of 138 |
| 49 | listener: the fault keys of a per-method override are merged | `provider_simulator/listeners/grpc.py` | 1 of 138 |
| 50 | listener: the fail_first_n window is not asked | `provider_simulator/listeners/grpc.py` | 1 of 138 |
| 51 | chain: the default entry is not read for a result | `provider_simulator/chains/lava.py` | 1 of 138 |
| 52 | builder: the default chain id of the latest block is another one | `server.py` | 3 of 138 |
| 53 | builder: the default height of the latest block is another one | `server.py` | 3 of 138 |
| 54 | builder: the default network of the node info is another one | `server.py` | 1 of 138 |
| 55 | builder: the default moniker of the node info is another one | `server.py` | 2 of 138 |
| 56 | builder: the default version of the node info is another one | `server.py` | 2 of 138 |
| 57 | builder: the default application name of the node info is another one | `server.py` | 2 of 138 |
| 58 | builder: the default application version of the node info is another one | `server.py` | 2 of 138 |
| 59 | builder: a result that is no object ends the call | `server.py` | 2 of 138 |
| 60 | adapter: a missing_field that names no field of the reply is cleared | `server.py` | 1 of 138 |
| 61 | listener: a filter that does not name the endpoint holds a result override back | `provider_simulator/listeners/grpc.py` | 1 of 138 |
| 62 | chain: a result override is not read | `provider_simulator/chains/lava.py` | 13 of 138 |
| 63 | chain: the height of the latest block is one more | `provider_simulator/chains/lava.py` | 10 of 138 |
| 64 | listener: a dropped connection has another text | `provider_simulator/listeners/grpc.py` | 3 of 138 |
| 65 | adapter: after the initial metadata the call ends with CANCELLED | `server.py` | 2 of 138 |

**Task 3, `tests/integration/test_server_start_wiring.py`.** 12 runs.

| # | What was broken | File | Tests that failed |
|---|---|---|---|
| 1 | start: the cache simulator listener is not started | `server.py` | 1 of 3 |
| 2 | start: the cache listener gets another cache simulator than the control routes | `server.py` | 1 of 3 |
| 3 | control: the /cache/ routes are not served over HTTP (POST) | `server.py` | 1 of 3 |
| 4 | control: the /cache/ routes are not served over HTTP (GET) | `server.py` | 1 of 3 |
| 5 | start: the RESP proxy server is not started | `server.py` | 2 of 3 |
| 6 | start: the RESP control listener is not started | `server.py` | 2 of 3 |
| 7 | init: the RESP proxy gets another target than the store it was given | `server.py` | 1 of 3 |
| 8 | start: an import error of grpcio is not caught for the gRPC endpoints | `server.py` | 1 of 3 |
| 9 | start: an import error of grpcio is not caught for the cache simulator | `server.py` | 1 of 3 |
| 10 | start: gRPC is reported as enabled with no grpcio | `server.py` | 1 of 3 |
| 11 | server.py imports grpc when it is loaded | `server.py` | 1 of 3 |
| 12 | /ready does not report a port that does not accept | `server.py` | 1 of 3 |

## Not in this plan

1. Pull request 2b, the move of gRPC into `Listener.serve`. It has a plan of its own. In 2b the function `_decide` of Task 1 changes from `plan` to `serve`, and the thirteen older tests of `tests/test_listener_grpc.py` that read a `GrpcPlan` are rewritten.
2. The fix of the two older reply builders (choice 2 above).
3. A change of the automation repository. Pull request 2a needs none.
4. The automation suites on a k3d cluster with the branch build. Section 12 of the design asks for that run before each merge of 1, 2b, 2c, 3b and 4a. Pull request 2a changes no source file, so the simulator image of the branch is the image of `main`.
5. A test for the router bug MAG-4217, which Victoria named on 2026-10-08. The router's gRPC block parser asks for the descriptor of a method with a `ServerReflectionInfo` call through the customer relay path, and the streaming guard refuses that call. The parser needs a descriptor only for a method whose block is parsed from a field of the request. READ on 2026-10-08 in `cosmossdk.json` of the repository `magma-devs/lava-specs`: `GetBlockByHeight` is such a method (`PARSE_DICTIONARY_OR_ORDERED` on `height`), and the three methods that the simulator serves are not (`GetLatestBlock` and `AllBalances` have `DEFAULT`, `GetNodeInfo` has `EMPTY`). The simulator does not serve `GetBlockByHeight`: Task 2 records that the call answers `UNIMPLEMENTED`. So a simulator test of that bug needs one new served gRPC method. After pull request 2b that is one row of the method table and its content in the chain.
6. One sentence for the skill `add-simulator-entity` of the automation repository: a new test file of the simulator goes into a folder below `tests/`, and `tests/integration/` holds the integration tests. The paired automation pull request of 2b changes that page, and it takes the sentence.
7. Four items of section 12.1 need no new test, because `main` pins them. The section "Spec coverage" names the test of each one: the reflection lookup of a symbol (item 8), the name `server._GRPC_PORT_POLL_S` (item 9), the status `UNAVAILABLE` for `down` and for `drop_connection` (item 4), and the texts "provider down" and "connection dropped" (half of item 5).

## Spec coverage

"On `main`" means: a test of `main` at `6f4e933` pins it, and this plan adds no test for it. `port_closed.py` stands for `tests/test_simulator_grpc_port_closed.py`.

| Requirement of the design | Where it is pinned |
|---|---|
| Section 12, 2a, the grid: each mode (`success`, `down`, `hang`, `drop_connection`, `rate_limit`, `error`) | Task 1: `test_what_plan_decides_for_one_call`, the cases `success` to `error-probability`, and the three cases `...-comes-before-...` for a fault with a per-method override |
| The grid: each corruption mode | Task 1: the cases `wrong-type` to `invalid-json-does-nothing` |
| The grid: each per-method override (`error_stub`, `error`, `result`, the entry `default`) | Task 1: the cases `error-stub` to `result-override-in-the-default-entry` |
| The grid: the status code, the text, and the row's method, status, `latency_ms` and request id | Task 1: each case asserts the code, the text and the row. `test_which_calls_wait_for_latency_ms` has the latency. On `main`: the request id of an `AllBalances` row, in `test_a_fault_row_of_all_balances_keeps_the_request_id` and `test_a_provider_wide_down_row_has_no_request_id` |
| The grid: a fault with a corruption (row 4 of section 9.3) | Task 1: the five cases `...-with-a-corruption-...`. Task 2: two cases of `test_a_fault_is_a_status_with_a_text_and_one_row` |
| Section 12, 2a, a real gRPC client: the status text of each fault | Task 2: `TestGrpcStatusTexts`. On `main`: "provider down" and "connection dropped", in `port_closed.py::TestThePortIsClosed::test_the_two_status_reply_modes_do_send_the_simulators_message` |
| A real gRPC client: `rate_limit` on a gRPC provider | Task 2: the cases `rate-limit` and `rate-limit-body-does-not-change-the-text` |
| A real gRPC client: a per-method `error_stub` | Task 2: `TestGrpcPerMethodErrors` |
| A real gRPC client: a method that is not served, with the status `UNIMPLEMENTED` and its text | On `main`: `TestGrpcAllBalances::test_other_bank_methods_answer_unimplemented`. Task 2: `TestGrpcCallsThatReachNoListener`, for a method of the tendermint service, for a service that is not registered, and for the row and the count |
| A real gRPC client: a message that does not parse, with the status `UNKNOWN` and no history row | Task 2: `test_a_request_that_is_no_message_answers_unknown` and `test_none_of_these_calls_writes_a_row_or_a_count` |
| Section 12.1, item 1: a per-method `error_stub`, its code and its text; a per-method `error`; the `default` entry | Task 1: nine cases. Task 2: `TestGrpcPerMethodErrors`, with each of the sixteen status names |
| Item 2: the row holds its method before the latency wait | Task 1: `test_the_row_is_complete_when_plan_returns`. Task 2: `test_the_row_of_a_call_is_complete_while_the_provider_still_waits` |
| Item 3: `fail_first_n=3` with `then_mode="success"` under `down` gives exactly three `down` rows | Task 1: `test_fail_first_n_gives_the_mode_to_the_first_calls_only`. Task 2: `test_fail_first_n_gives_exactly_that_many_down_rows` |
| Item 4: the status `UNAVAILABLE` for `down` and for `drop_connection` | On `main`: `port_closed.py`, the test of the row above, and `TestGrpcCrossPoolIsolation::test_grpc_fault_still_fires_on_its_own_pool`. Task 1: the cases `down` and `drop-...`. Task 2: `test_a_grpc_client_reads_the_same_status_for_each_drop_point` |
| Item 5: the four status texts | "provider down" and "connection dropped": on `main`. "hang timeout": Task 1, the case `hang`; Task 2, the 30-second test. "Too many requests": Task 1 and Task 2, the cases `rate-limit` |
| Item 6: the row fields of a gRPC call: `latency_ms`, and the status labels `hang`, `drop_connection` and `rate_limit` | Task 1: the row of each case, and `test_which_calls_wait_for_latency_ms`. Task 2: `TestGrpcWhichCallsWait` and the row of the 30-second test |
| Item 7: `rate_limit` and `error_stub` over a socket; `UNIMPLEMENTED`; a message that does not parse; the `UNKNOWN` fallback; the corruption codes | Task 2, as above. On `main`: the `UNKNOWN` fallback (`TestGrpcFaultStatus::test_unknown_fallback`) and the corruption codes (`TestGrpcFaultCorrupt`). Task 2 adds the text of each corruption and the code of `null_body` |
| Item 8: reflection finds the symbol of a served service | On `main`: `TestGrpcReflection::test_reflection_finds_the_bank_symbol_because_the_server_loads_it` |
| Item 9: the name `server._GRPC_PORT_POLL_S` stays importable | On `main`: `port_closed.py::TestThePortOpensAgain::test_a_sweep_inside_the_time_to_live_leaves_it_closed` and `test_ready_asked_part_way_through_a_reopen_waits_for_it` use the name |
| Item 10: `drop_at`: for `after_headers` and `mid_body` the initial metadata arrives before the call ends | Task 1: the three `drop-...` cases hold the drop point. Task 2: `TestGrpcDropPoint` reads the frames |
| Item 11: an `error_stub` with a name that is no status gives `UNKNOWN`, and the key `message` gives the text | Task 1: two cases. Task 2: `test_an_error_stub_whose_name_is_no_status_gives_unknown` and `test_an_error_stub_takes_its_text_from_the_key_message` |
| Item 12: a gRPC call under `pause_at` is not delayed; `rate_limit` gives `RESOURCE_EXHAUSTED` with "Too many requests" and no body text | Task 2: `test_a_pause_does_not_delay_a_grpc_call`. Task 1 and Task 2: the case `rate-limit-body-does-not-change-the-text` |
| Section 12.1, the shared wiring: `start` binds the cache simulator port and serves the `/cache/` routes over HTTP | Task 3: `test_start_serves_the_cache_simulator_that_the_control_routes_stage` |
| The shared wiring: the RESP proxy forwards | Task 3: `test_start_wires_the_resp_proxy_to_the_store_that_it_was_given`. On `main`: `tests/test_resp_wiring.py` shows that `start` binds the two RESP ports of the shared simulator |
| The shared wiring: the simulator starts with no `grpcio` | Task 3: `test_the_simulator_starts_with_no_grpcio_and_serves_its_http_endpoints` |
| Section 8.3: a request that does not parse is refused before the listener; a method that is not served answers `UNIMPLEMENTED`; `port_closed` and the thread name stay | Task 2: `TestGrpcCallsThatReachNoListener`. On `main`: `port_closed.py`, and the thread name in `test_a_port_that_does_not_close_in_time_is_an_error_naming_it` |
| Section 9.3, differences 1 to 6, which 2b must keep | 1: on `main`, `test_down_aborts_unavailable_and_records_method_not_star`. 2: Task 1, the cases `down` and `hang` of `test_which_calls_wait_for_latency_ms`; Task 2, `down-answers-at-once` and the row of the 30-second test. 3: the row status `error` of each corruption case. 4: the cases with a corruption. 5: the case `invalid-json-does-nothing`, and `test_invalid_json_corruption_does_nothing_on_grpc`. 6: the four cases `per-method-...-is-not-read`, and `test_a_fault_key_in_a_per_method_override_is_not_read` |
| Section 14.6, point 5: `blocks_behind`, `fail_first_n`, `then_mode` and `missing_field` pass through | On `main`: `test_blocks_behind_shifts_head` and `TestGrpcFaultStale`. Task 1: the three `fail_first_n` tests and the four `missing-field` cases |
| The handoff: a method that is not served writes no row and no count | Task 2: `test_none_of_these_calls_writes_a_row_or_a_count` |
| The handoff: a failed check ends one thread, the process stays up, and `/ready` answers 503 | On `main`: `TestGrpcServedMethods::test_the_adapter_refuses_to_start_when_a_served_method_has_no_row`. Task 2: `test_an_endpoint_that_is_refused_leaves_the_simulator_up_and_not_ready` |
| The handoff: the two older reply builders raise on a wrong shape after the row is written | Task 2: `TestGrpcReplyFields`, which holds the fields of each reply, the default of each field under a `result` override that does not set it, and the known fault of `build_latest_block` (choice 2). The same fault of `build_node_info` has no test |
| Section 9.2, row 2: `latency_ms` on gRPC under a filter that does not name the endpoint, as it is today | Task 1: `test_latency_ms_is_paid_today_under_a_filter_that_does_not_name_the_endpoint` |
