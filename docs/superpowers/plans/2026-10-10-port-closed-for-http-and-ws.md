# Port closed for http and ws: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**The state of this plan.** Written on 2026-10-10 from a read, by a helper that only read. Nothing ran for it before Task 0. Victoria gave her go on 2026-10-10, with the decisions that section 2 names. **The base changed after the plan was written:** the WebSocket move merged into `main` on 2026-10-10 as the commit `f8086f2` (pull request 144, with the recorded WebSocket tests). So this branch starts from `main`, or from a small follow-up branch of that merge, and not from a branch of the WebSocket move. Each line number of `server.py` below is of `8ed08aa`, before that merge: Task 0, Step 3 reads the places again on the base, and the code of the base is right. Each measurement below is a reading of another session, with its date: most are of the spike of 2026-10-08. Simulator code: READ at `main` `8ed08aa` (the merge of pull request 143, "four differences become uniform"), in the worktree `pin-todays-websocket-rows`. Automation code: READ at `main` `045ab9e33f`. A line number with no other note is a line of `8ed08aa`.

**After the review (2026-10-10).** The branch differs from the tasks below in four places. The text of the pull request has the proof for each one.

1. The tests. The new test file has 28 functions and 37 cases, `tests/test_control_api_port_closed.py` has one more new function, and the suite has `N + 43` tests. The break proof and the review found source lines that no test held, and each one has a test now.
2. The loop of a port. `_HttpPortLoop` reports "open" only for a server that serves and that no `close_port()` touched. It finishes a `close_port()` that raised, and it closes a server that cannot start its serve thread. So the report to the gate is true after a pass that raised.
3. The serve thread of a provider port has the name `port-<port>-serve`.
4. The call that `_provider_server_factory` returns holds the host, and not the simulator. So a simulator that stopped does not keep the socket of its control port.

`CLAUDE.md` has no edit: it waits for Victoria's word.

## 1. Goal, architecture, tech stack

**Goal:** `mode="port_closed"` closes the port of an `http` endpoint and of a `ws` endpoint for real. A new connection is refused, on Linux too. An open connection ends. The port opens again by `success`, by another mode, by `POST /reset`, by `POST /reset/all` and by the time-to-live sweep. The mode `down` does not change. This is pull request 4a of the design.

**Architecture:** The three parts of a gRPC port stay as they are (`provider_simulator/port_gate.py:9-24`). The scenario holds the wish. A loop owns the server. The control API waits on a `PortGate`. A new server class, `_ProviderHTTPServer`, records its client sockets and can close its port. A new loop, `_HttpPortLoop`, keeps one port in the state that the scenario asks for. `SimulatorServer.start` gives each `http` endpoint and each `ws` endpoint one gate and one loop. The control API accepts the mode for each endpoint that has a gate. It waits only for a gate whose last report differs from the wish.

**Tech stack:** Python 3.12 with `socketserver`, `http.server`, `socket` and `threading` of the standard library. pytest 9.1.1 with pytest-timeout 2.4.0. black 26.5.1, ruff 0.15.20, mypy 2.2.0. Docker, for the run on Linux.

**Spec:** The design "One request flow for every interface", version 2.3. Read section 14.5, section 12 (the four rules), sections 13 and 13.1, the last paragraph of 9.2, and 14.6. ADR-001, decision 7. The model of the code is the spike, on the branch `spike-port-closed-for-http-and-ws`. Its commits are `93616d1` (the loop) and `459274f` (the fix for Linux). Both are NOT FOR MERGE.

### 1.1 The design against the code of today

The code is right in each row.

| The design says | The code of today |
|---|---|
| Line numbers of the commit `e80acbf` | They moved. The refusal for an endpoint that is not gRPC: `control_api.py:254-264` (the design: `:227-237`). The gates of a write and the wait: `:480` and `:489` (`:451`, `:460`). The reason for the reset rule: `:668-670` (`:639-641`). The pinned text: `:268` (`:241`). The gRPC loop: `server.py:1170-1219` (`:1212-1259`). The server list, the wiring and `stop`: `:1355`, `:1366-1369`, `:1454-1462`. `docs/using_grpc.md:94` (`:85`). `docs/using_the_simulator.md:199-207` (`:172-180`). The lines of the six tests that change did not move. |
| Points 7 and 9 list the texts that say "gRPC only" | More places have such a text: `CONTEXT.md:220-224` and `:237-254`, `provider_simulator/domain/scenario.py:9-12`, `provider_simulator/fault_policy.py:36-39`, `k8s/deployment.yml:628-630`, `server.py:13-18`, `:759-761`, `:1275-1280` and `:1402-1406`, and `control_api.py:29-30`, `:213-218`, `:365-375`, `:487-496` and `:539`. |
| "Rollback: … the tests of 4b then read the HTTP 400 of the simulator as a setup problem, through `a_refused_port_closed_reads_as_setup`" | The helper converts one refusal only: HTTP 400 with the text `invalid mode 'port_closed'` (`tests/simulator/_grpc_replies.py:70-77` and `:179-207` of the automation repository). A unit test requires that the refusal "only a gRPC endpoint can close its port" stays an `HTTPError`: `tests/infrastructure/unit/test_grpc_reply_readers_and_port_closed_refusal.py:142-155`, the case `400-from-a-simulator-that-knows-the-mode`. So the sentence holds only if pull request 4b changes that helper and that test (section 9). |
| The spike puts its second simulator on the ports 29721, 28721 and 28722 | `tests/test_simulator_grpc.py:89-90` uses that block today. The block 29751, 28751 and 28752 is free. |
| The spike puts its test file into `tests/` | Victoria's rule forbids it. `tests/integration/` exists, with `__init__.py` and one test file. |
| `Simulator.start`, `Simulator.stop()` | The class is `SimulatorServer` (`server.py:1274`). |
| Two tests of the classifier file carry the label "Priority 0 — DetectConnectionError (TCP refused)" | The label is "HTTP 503 — NODE_SERVICE_UNAVAILABLE (External)" today (`test_router_retry_error_classifier_tiers.py:643` and `:876` of the automation repository). Part B of section 10.5 looks done. Part B2 is open. |

## 2. The choices, and what Victoria decided

**Victoria's decisions of 2026-10-10.** She saw a short list of these choices after the merge of pull request 144 and answered "continue".

| Choice | Decision |
|---|---|
| 1. When a control call waits for a port | It waits only for a port that is not yet in the asked state. It also waits when an earlier call got no answer for that port (the form C of this plan). She saw the first sentence; the second is the recommendation of the plan. |
| 2. Where the new tests go | One new file, `tests/integration/test_http_and_ws_port_closed.py`. |
| 3. A request that the provider holds when its port closes | The connection ends, and its history row stays as it was written. A connection that arrives in the moment of the close ends too (the recommendation of the plan; she did not see this second sentence). |
| 4. A WebSocket client of a closed port | The connection ends with no close frame. The control call does not wait for the subscription registry. |
| 5. Loop threads | One loop thread for each port: 144 more threads. |
| 6. Where the new source goes | Into `server.py`, next to the gRPC loop. |
| 7. The base of the branch | `main` after the merge of the WebSocket move (see "The state of this plan"). |
| 8. The plan file | It merges with the pull request, as its first commit. |
| 9. The rule for "slower" of a scenario write | More than 1 ms and more than 100 percent above the base (the recommendation of the plan; she did not see it). |
| The count of runs in a row | Two, as for pull request 144. The definition of done of the design names three. |

The text of each choice follows, as the record of the options. The tasks of section 5 are written for these decisions.

1. **When does a control call wait for a port?**
   - A: Each call waits for each port of each provider that it writes, and it checks each one with a connection. This is the rule of today, for 12 gRPC ports. Nobody measured it with 156 ports.
   - B: A call waits only for a port that is not yet in the state that the call asks for. The design names this rule, and the spike measured it. The median of ten `down` writes on the cluster was 1.47 ms and 1.19 ms, against 1.20 ms and 0.93 ms on `main`.
   - C: B, and a call also waits when an earlier call got no answer for that port. After an error for a port, the next call then does not say "ok" too early.
   - B and C change one thing for a caller. A call that asks for the state that the port already has makes no new connection check. That holds for the gRPC ports too. `GET /ready` still checks each port.
   - **Recommendation: C.** It costs one condition and one test. While no call fails, C and B do the same thing, so the readings of the spike apply by a read. The gate on the cluster measures the code of the pull request in each case.
2. **Where do the new tests go?** A: one new file, `tests/integration/test_http_and_ws_port_closed.py`. B: no new file; the tests go into `tests/test_simulator.py` and `tests/test_simulator_ws.py`. Pull request 3a changes the second file, and the WebSocket move uses it as its specification. **Recommendation: A.** The file name needs your approval.
3. **What happens to a request that the provider holds when the port closes?** The design fixes one part: the connection ends. Two parts are open.
   - The history row. A: it stays as the simulator wrote it when the request arrived (`hang`, or `success` for a slow reply). B: it gets a new status. B changes the history that automation tests read. **Recommendation: A.**
   - A connection that arrives in the same moment as the close. By a read of the code of the spike, one such request can still get a reply after the control call returned. **Recommendation: end that connection too.** It costs three lines and one test.
4. **What does a WebSocket client of a closed port see?**
   - A: the connection ends with no close frame, as when a node dies. The spike does this. B: the simulator sends a close frame first, as when a node stops in good order. **Recommendation: A.** The mode means "this provider cannot be reached".
   - The entry in `GET /ws/subscriptions` goes when the thread of the connection ends. That is a moment after the control call returns. On the cluster the spike read 0 entries right after the call. The other way is a new wait inside the control call. **Recommendation: no new wait.** A test polls for at most 2 s.
5. **One loop thread for each port, or one for all ports?** A: one for each port, 144 more threads. The spike measured 179 threads on `main` and 323 with this form, and each reading of the spike used it. B: one thread for all 144 ports. Nobody measured B. **Recommendation: A.**
6. **Where does the new source code go?** A: into `server.py`, next to the gRPC loop. No new source file. B: into a new module of the package. B must first move the server class that all listeners share, and the WebSocket move reads that class. **Recommendation: A.**
7. **The base of the branch.** The brief fixes it: the branch of the WebSocket move. The brief of that plan names it `websocket-frames-use-listener-serve`. By a read, the two changes meet in four places of `server.py` (section 4). A start from `main` is possible, and the second one to merge rebases. **Recommendation: keep the base, if the WebSocket move is not late.**
8. **The plan file merges with the pull request**, as its first commit, as in pull requests 2c and 3a. It is a new file, so it needs your go.
9. **The rule for "slower" of a scenario write** (Task 6, Step 6). The design asks for the measurement and gives no rule. My rule: more than 1 ms and more than 100 percent above the base.

## 3. Global constraints

1. The mode `down` does not change: HTTP 503 with no body on an open connection, and `UNAVAILABLE` on gRPC.
2. No scenario closes the control port, a cache simulator port, a RESP proxy port or the RESP control port.
3. A new test file goes below `tests/`, into `tests/integration/`. A test of a file that exists goes into that file.
4. Linux decides. On a Mac `socket.close()` alone refuses a connection, so a Mac does not show a wrong close. The port tests run in a container before the push (Task 6, Step 5).
5. One pytest process at a time on the machine. Before a run, `lsof -nP -iTCP:19000 -sTCP:LISTEN` must print nothing.
6. Each new test fails first. Each new test has one break of the source that makes it fail (Task 6, Step 2).
7. Every comment, docstring, document line and commit message is in ASD-STE100 Simplified Technical English.
8. A commit has a conventional subject and no trailer. It names no agent configuration file. Stage each file by its name. Never use `git add -A`.
9. The branch is `port-closed-for-http-and-ws`. No ticket key is in the branch name, the title or the body. The pull request has the label `no-ticket`.
10. The checks of CI: `black --check .`, `ruff check .`, `mypy .`. Line length 120.
11. `server.py` gets no `fault_policy` import and writes no history row: the guard test of the WebSocket move forbids both. The new code reads the wish only through `PortGate.wants_closed()`.
12. Six expected values of tests of today change (Task 4), and no other one. The tests of the cache simulator, of the RESP proxy and the four topology tests pass with no edit.
13. A loop thread and a serve thread of a provider port never raise. `tests/test_simulator_grpc.py::TestGrpcServedMethods::test_an_endpoint_that_is_refused_leaves_the_simulator_up_and_not_ready` counts the errors of all threads.
14. A worktree has no virtual environment. Use `.venv/bin/python` of the main checkout. The commands below write `python` for it.
15. A step that does not go as this plan says is a stop: tell Victoria, and do not work around it.

## 4. File structure

| File | Its one job in this change |
|---|---|
| `server.py` | `_ProviderHTTPServer`, `_HTTP_PORT_POLL_S`, `_HttpPortLoop`, and the wiring in `SimulatorServer` |
| `provider_simulator/port_gate.py` | `PortGate.last_report()` |
| `provider_simulator/control_api.py` | The rule for the wait. The mode is accepted for each endpoint that has a gate. |
| `provider_simulator/fault_policy.py`, `provider_simulator/domain/scenario.py` | Texts only |
| `tests/integration/test_http_and_ws_port_closed.py` (new) | The tests of the mode on an `http` port and on a `ws` port: 21 functions, 30 cases |
| `tests/test_port_gate.py`, `tests/test_control_api_port_closed.py` | Four new functions. Three tests change. |
| `tests/test_simulator_grpc_port_closed.py` | Three tests change. |
| `README.md`, `docs/using_grpc.md`, `docs/using_the_simulator.md`, `CLAUDE.md`, `CONTEXT.md`, `k8s/deployment.yml` (one comment) | Texts only |
| `docs/superpowers/plans/2026-10-10-port-closed-for-http-and-ws.md` (new) | This plan |

**The places of `server.py`.** The last column is a read of the brief of the WebSocket move. That code is not written.

| Place (line of `8ed08aa`) | This plan | The WebSocket move, by its brief |
|---|---|---|
| The module docstring, `:13-18` | Adds the loop of an `http` or `ws` port | Edits the WebSocket item, `:19-22`: the next lines |
| The imports, `:32-74` | Adds `errno` and `Callable` | Removes `fault_policy`: the same block |
| Below `_SimThreadingHTTPServer`, `:88-108` | New class `_ProviderHTTPServer` | Reads `:76-150`, and can edit the class: the next lines |
| The comment of `_READY_LOCK_WAIT_S`, `:759-761` | Text | No |
| Below `_run_grpc_in_thread`, `:1100-1219` | New `_HTTP_PORT_POLL_S` and `_HttpPortLoop` | No |
| `SimulatorServer`: the docstring `:1275-1280`, and `__init__` `:1353-1356` | Text, and `_port_loops` | Can edit `__init__` at `:1299`: other lines |
| The loop of `SimulatorServer.start`, `:1358-1369` | A gate, a factory and a loop for each endpoint | **Yes**: the row `("jsonrpc", "ws")` of `_HTTP_ADAPTERS` (`:1268`) changes what these lines build |
| `start`, `:1392` and `:1400-1408`; `stop`, `:1454-1462` | The loop threads, `probe_host`, the stop of the loops | No |

This plan edits no line of `_HttpListenerHandler`, of `_WsHandler`, of `_WireSubscriptions` or of `_HTTP_ADAPTERS`. It depends on two things that the WebSocket move must keep. The reader of a WebSocket connection ends at end of stream (`:561-563`). The subscriptions of the connection go at its end (`:592-595`). The test `TestASubscriptionBelongsToOneConnection::test_a_connection_that_ends_with_no_close_frame_loses_its_subscriptions` of pull request 3a holds both.

## 5. The tasks

**Review focus.** A plain test of the mode does not reach these five conditions. Each one has its test. (1) The listening socket on Linux: U1. (2) A connection that is accepted in the moment of the close: U4. (3) A serve thread that starts on a closed socket: U5. (4) The wiring of a server that opened again: `test_a_port_that_opened_again_serves_the_same_provider`. (5) `stop()` for such a server: `test_stop_reaches_a_server_that_opened_again`.

### Task 0: The worktree, the base and the baseline

**Files:** Create `docs/superpowers/plans/2026-10-10-port-closed-for-http-and-ws.md`.

**Interfaces:** Consumes `main` after the merge of the WebSocket move (`f8086f2`), or the follow-up branch of that merge. Produces the worktree `.claude/worktrees/port-closed-for-http-and-ws`, the branch `port-closed-for-http-and-ws`, the count `N` of the suite on the base, and the reading "before" of a scenario write.

- [ ] **Step 1.** Get Victoria's go for the worktree, the branch, the plan file and the new test file.
- [ ] **Step 2.** Make the worktree from the base that "The state of this plan" names.
- [ ] **Step 3.** Read four things on the base before any edit. (a) The loop of `SimulatorServer.start` and `_HTTP_ADAPTERS`. (b) The two places of `_WsHandler` that section 4 names. (c) The guard test of the WebSocket move. (d) The line numbers of section 4, and that no test uses the ports 29751, 28751 and 28752. The line numbers of section 4 are of `8ed08aa`, so they differ on the base: write the lines of the base into the report. A port that a test uses, or a place of section 4 that does not exist on the base, is a stop.
- [ ] **Step 4.** Run `lsof -nP -iTCP:19000 -sTCP:LISTEN`. Expected: no output. Run `python -m pytest tests -q -p no:cacheprovider`. Expected: `N passed`, no failure, no skip. Record `N` and the run time.
- [ ] **Step 5.** Run two times, from the root of the worktree: `python <automation checkout>/handoffs/evidence-2026-10-07-one-request-flow-design/real-down-spike/scripts/time_set_scenario.py`. Expected: the line `gates: 12 | provider ports: 156`. Record the medians: this is "before".
- [ ] **Step 6.** Commit the plan file: `docs(plan): the implementation plan of port_closed for http and ws`.

### Task 1: The last report of a gate, and the rule for the wait

**Files:** Modify `provider_simulator/port_gate.py` (`PortGate`, `:39-111`) and `provider_simulator/control_api.py` (`_settle_ports`, `:510-536`). Test: `tests/test_port_gate.py`, `tests/test_control_api_port_closed.py`.

**Interfaces:** Produces `PortGate.last_report(self) -> bool | None`. `True` is open and `False` is closed. `None` means: no pass has finished, or an ask has no answer yet. Consumes `begin_pass`, `end_pass` and `ask` of `PortGate`.

| Test to write first | It asserts |
|---|---|
| `tests/test_port_gate.py::test_a_gate_with_no_finished_pass_has_no_report` | A new gate gives `None`, and not `False`. |
| `::test_the_last_report_is_what_the_newest_pass_left[open\|closed]` | After `end_pass(begin_pass(), is_open=X)` the report is `X`. |
| `::test_a_gate_with_an_ask_that_no_pass_answered_has_no_report` | After a report and one `ask()` the report is `None`. A pass that began after the ask gives the report back. |
| `tests/test_control_api_port_closed.py::test_a_write_that_moves_no_port_waits_for_no_gate` | A gate with no listener reported "open". With `_PORT_SETTLE_S` at 0.2, `mode="down"` answers 200. |

The controls are tests of today, and they pass with no edit. A gate with no report still waits: `test_a_listener_that_never_acts_is_an_error_naming_pool_provider_and_port` and `test_a_reset_that_cannot_reopen_the_port_is_an_error_too`. A port that must move still waits: `tests/test_simulator_grpc_port_closed.py`.

- [ ] **Step 1.** Write the four tests.
- [ ] **Step 2.** Run `python -m pytest tests/test_port_gate.py tests/test_control_api_port_closed.py -q -p no:cacheprovider`. Expected: the three gate tests fail with `AttributeError: 'PortGate' object has no attribute 'last_report'`, and the control test fails with `assert 500 == 200`.
- [ ] **Step 3.** `PortGate`: add the field `_reported`, set it in `end_pass`, and add `last_report`. It returns `None` when `not self._reported or self._done < self._asked`, else `self._open`. In `_settle_ports`, line `:519` asks a gate only `if gate.last_report() != want_open`. Update the docstring of `_settle_ports` and the item "The control API waits" of `port_gate.py:20-24`.
- [ ] **Step 4.** Run the command of Step 2. Expected: no failure, five cases more than on the base. Run `python -m pytest tests/test_simulator_grpc_port_closed.py -q -p no:cacheprovider`. Expected: no failure, the count of the base.
- [ ] **Step 5.** Commit: `feat(control): a control call waits only for a port that is not where its scenario puts it`.

### Task 2: The server of one provider port

**Files:** Modify `server.py`: `import errno`, and the new class below `_SimThreadingHTTPServer`. Create `tests/integration/test_http_and_ws_port_closed.py`.

**Interfaces:** Produces `_ProviderHTTPServer(_SimThreadingHTTPServer)` with the attributes `closed_by_scenario: bool` and `serve_started: bool`, and with:
- `serve(self) -> threading.Thread`: it starts `serve_forever` on a daemon thread and returns the thread.
- `close_port(self) -> None`: it refuses new connections, ends the open ones, and tells the serve loop to stop. It waits for nothing.
- The overrides `get_request`, `shutdown_request` and `handle_error`.

Class `TestTheServerOfOneProviderPort`: a server on a port that the system gives, with a small handler class of the test. It uses no simulator. Each test names the class as `server_module._ProviderHTTPServer`, so a missing class fails each test and not the collection.

| Test to write first | It asserts |
|---|---|
| U1 `test_close_port_refuses_a_new_connection_at_once` | After one served connection the serve loop sits in its poll. Right after `close_port()` one connection gets `ECONNREFUSED`, and a new server binds the port at once. Twenty rounds. |
| U2 `test_close_port_ends_an_open_connection` | A client that got one byte from the handler reads end of stream or a reset in under 2 s, and no timeout. |
| U3 `test_close_port_returns_while_the_serve_loop_still_runs` | With `shutdown` of the server held back, `close_port()` returns in under 1 s and the port refuses. The test lets the real `shutdown` run at its end. |
| U4 `test_a_connection_accepted_after_the_close_began_is_ended` | With `closed_by_scenario` set, `get_request()` raises `OSError`, and the client reads end of stream. |
| U5 `test_a_server_closed_before_its_first_poll_ends_its_serve_thread_with_no_error` | After `close_port()`, the thread of `serve()` ends in under 2 s, and `threading.excepthook` gets no call. |
| U6 `test_only_an_os_error_of_a_closed_server_is_dropped` | `handle_error` prints nothing for an `OSError` after `close_port()`. It prints for a `TypeError` after it, and for an `OSError` before it. |

The tricky lines of `close_port`, in this order:

```python
with self._clients_lock:                      # the flag and the copy change together
    self.closed_by_scenario = True
    clients = list(self._clients)
try:
    self.socket.shutdown(socket.SHUT_RDWR)    # Linux: the socket leaves the listening state at once
except OSError as exc:
    if exc.errno != errno.ENOTCONN:           # a Mac answers ENOTCONN here, and that is expected
        _log.warning("provider port %s: the listening socket did not shut down: %s", self.server_port, exc)
self.socket.close()
for client in clients:
    try:
        client.shutdown(socket.SHUT_RDWR)     # not close(): the worker thread owns the socket
    except OSError:
        pass                                  # the client is gone
if self.serve_started:
    threading.Thread(target=self.shutdown, daemon=True).start()   # nothing waits for the old serve loop
```

- `socket.shutdown` comes before `socket.close`. On Linux a listening socket accepts for as long as `serve_forever` sits in its poll, if only `close()` runs. Measured by the session `worker` on 2026-10-08: 20 of 20 connections accepted with `close()` only, and 20 of 20 refused with `shutdown` first.
- `get_request` adds the accepted socket to `_clients` under `_clients_lock`. If `closed_by_scenario` is set, it closes the socket and raises `OSError`. `BaseServer._handle_request_noblock` drops that error. `shutdown_request` removes the socket from `_clients`.
- The thread function of `serve` catches `OSError` and `ValueError` from `serve_forever`. It raises again only when `closed_by_scenario` is not set. READ in `selectors._fileobj_to_fd`: a closed socket gives `ValueError`.
- `handle_error` drops an error only when `closed_by_scenario` is set and the error is an `OSError`. Each other error prints its traceback, as today.

- [ ] **Step 1.** Write the six tests.
- [ ] **Step 2.** Run `python -m pytest tests/integration/test_http_and_ws_port_closed.py -q -p no:cacheprovider`. Expected: six failures with `AttributeError: module 'server' has no attribute '_ProviderHTTPServer'`.
- [ ] **Step 3.** Write the class.
- [ ] **Step 4.** Run the command of Step 2. Expected: `6 passed`. Run the same file on Linux with the command of Task 6, Step 5. Expected: `6 passed`.
- [ ] **Step 5.** Commit: `feat(server): the server of a provider port can refuse new connections and end the open ones`.

### Task 3: The loop of a port, the gates and `stop()`

**Files:** Modify `server.py`: the new constant and the new class below `_run_grpc_in_thread`; `SimulatorServer.__init__`, `start` and `stop`; the texts at `:13-18`, `:759-761`, `:1275-1280` and `:1402-1406`. Modify `provider_simulator/port_gate.py:1-28`: the text names both loops. Test: the new file.

**Interfaces:** Consumes `serve` and `close_port` of Task 2, and `attach`, `begin_pass`, `wants_closed` and `end_pass` of `PortGate`. Produces:
- `_HTTP_PORT_POLL_S = 1.0`: the time between two passes that nobody asked for.
- `_HttpPortLoop(gate: PortGate, first: _ProviderHTTPServer, new_server: Callable[[], _ProviderHTTPServer])`, with `thread: threading.Thread` (a daemon, with the name `port-<port>`) and `stop(self) -> None`.
- `SimulatorServer._provider_server_factory(self, port: int, handler_cls, listener: Listener) -> Callable[[], _ProviderHTTPServer]`, and `SimulatorServer._port_loops: list[_HttpPortLoop]`.
- One `PortGate` in `ControlApi.port_gates` for each `http` endpoint and each `ws` endpoint.

| Test to write first | It asserts |
|---|---|
| `TestWhatAScenarioCannotClose::test_only_provider_ports_have_a_gate` | The ports with a gate are exactly `registry.ports()`. The control port, each cache simulator port, each RESP proxy port and the RESP control port have no gate. |

The rules for the source:
- One pass has the order of the gRPC loop (`server.py:1189-1217`). First `begin_pass()`, then `wants_closed()`, then `close_port()` or a new server with `serve()`, then `end_pass(seen, is_open=...)`. Then the loop waits for `_HTTP_PORT_POLL_S`, and an ask ends that wait.
- A pass and `stop()` hold one lock of the loop. A pass catches `Exception`, logs each new text one time, and the next pass tries again.
- The first server binds in `start()`. So a port that cannot bind fails the start, as today.
- The factory gives each new server the attributes that the loop of `start` sets on the base branch. Today they are `listener` and `subscriptions` (`:1367-1368`). Each server of one port gets the same listener object.
- `self._servers` keeps the control server, the RESP proxies and the RESP control server. They get no gate and no loop.
- `stop()` also runs `_HttpPortLoop.stop()` for each loop, each on its own thread. `_HttpPortLoop.stop()` calls `shutdown()` only when `serve_started` is set, and then `server_close()`.
- The gates of the `http` and `ws` ports need no `grpcio`. Make them outside the `try` of the gRPC import (`:1396-1419`).
- After this task the control API still refuses the mode for these endpoints. Task 4 removes the refusal.

- [ ] **Step 1.** Write the test.
- [ ] **Step 2.** Run `python -m pytest tests/integration/test_http_and_ws_port_closed.py -q -p no:cacheprovider`. Expected: one failure, with 12 gated ports against 156.
- [ ] **Step 3.** Write the loop and the wiring.
- [ ] **Step 4.** Run the whole suite: `python -m pytest tests -q -p no:cacheprovider`. Expected: `N + 12 passed`. A run time more than 25 percent above the time of Task 0 is a stop. That is the rule of the spike for "slower".
- [ ] **Step 5.** Commit: `feat(server): each http port and each ws port gets a gate and a loop that can close it`.

### Task 4: The control API accepts the mode

**Files:** Modify `provider_simulator/control_api.py`. Delete `:254-264` of `_a_port_closed_that_cannot_happen`. The text of the HTTP 409 (`:265-271`) says "runs no listener there". The text of the HTTP 500 (`:532-536`) says "The listener". Change the texts at `:29-30`, `:205-227`, `:365-375`, `:487-496` and `:539`. Test: the new file, and the two port test files of today.

**Interfaces:** Consumes Tasks 1 to 3. Produces the behaviour of the goal.

The tests use the shared simulator of the session and the provider `eth-sim:1`, with its `http` port and its `ws` port. Each test reads the port with a raw TCP connection on the line after the control call. An autouse fixture sends `POST /reset/all` before and after each test, and it requires HTTP 200.

| Test to write first | It asserts |
|---|---|
| `TestWhatAScenarioCannotClose::test_a_field_that_acts_on_one_request_is_still_refused_with_the_mode` | `latency_ms=300` with the mode gets HTTP 400 with the reason of that field. Nothing is stored, and the port accepts. |
| `TestAnHttpPortIsClosed::test_a_new_connection_is_refused_on_the_line_after_the_call[jsonrpc\|rest\|tendermintrpc]` | HTTP 200, then `ECONNREFUSED` with no sleep, for one provider of each `http` handler class. |
| `::test_the_closed_provider_stores_no_row` | A refused connection adds no row. The same request to a `down` provider adds one row. |
| `::test_a_filter_closes_only_the_endpoint_that_it_names[transports-http\|ports-http\|transports-ws]` | The named port refuses. The other port of the provider accepts. Provider 2 answers a request. |
| `::test_with_no_filter_the_http_port_and_the_ws_port_close` | Both ports of the provider refuse. |
| `::test_a_request_in_flight_ends_and_the_control_call_does_not_wait_for_it` | A `hang` provider holds one request, and its row is in the history. The control call returns 200 in under 2 s. The client gets a connection error in under 5 s, and no reply. The row stays `hang`, and no row is added. |
| `TestAWsConnectionOfAClosedPort::test_a_connection_with_a_subscription_ends_and_its_entry_leaves_the_registry` | The read of the client ends with end of stream or a reset: no close frame and no timeout. `GET /ws/subscriptions` has no entry in under 2 s. A new connection is refused. |
| `::test_a_connection_with_no_subscription_ends_too` | The read of the client ends in the same way. |
| `TestThePortOpensAgain::test_each_way_opens_both_ports_on_the_line_after_the_call[mode=success\|mode=down\|POST /reset\|POST /reset with the pool\|POST /reset/all\|the time-to-live sweep]` | Both ports accept with no sleep. With `down` the `http` port answers HTTP 503. With each other way it answers HTTP 200. |
| `::test_a_port_that_opened_again_serves_the_same_provider` | One request adds one row for `eth-sim:1`. One `eth_subscribe` on the `ws` port adds one entry with the pool `eth-sim` and the provider id `1`. |
| `::test_closing_and_opening_ten_times_keeps_working` | Each control call answers 200, and each state holds on the line after the call. |
| `::test_a_close_and_an_open_at_the_same_moment_both_answer_200` | Twenty rounds with two callers. Each caller gets 200. The port is in the state of the stored mode. |
| `::test_ready_leaves_the_closed_ports_out_and_stays_200` | `GET /ready` answers 200, names both ports in `closed_by_scenario`, and `expected` is two less. After a reset it is the baseline. |
| `test_stop_reaches_a_server_that_opened_again` | A simulator of its own on the ports 29751, 28751 and 28752. After a close, an open and `stop()`, a request to the `http` port gets no reply. |

The six tests of today that change:

| Test | Today | After |
|---|---|---|
| `tests/test_control_api_port_closed.py:42-48` | The text `runs no gRPC listener there` | `runs no listener there`. New name: `test_with_no_listener_it_is_refused_and_not_stored`. |
| `:51-59` | HTTP 400, `only a gRPC endpoint can close its port` | HTTP 409, `runs no listener there`, and both ports named. New name: `test_an_http_and_ws_provider_with_no_listener_is_refused_and_not_stored`. |
| `:62-68` | HTTP 400 | HTTP 409. The name stays. |
| `tests/test_simulator_grpc_port_closed.py:421-431` | HTTP 400, and the provider answers | HTTP 200, and both ports of `eth-sim:1` refuse. New name: `test_an_endpoint_that_is_not_grpc_closes_too`, in the class `TestThePortIsClosed`. |
| `:818-823` | HTTP 400, and no port closes | HTTP 200. The two gRPC ports and the REST port refuse, and the port of provider 2 accepts. New name: `test_with_no_filter_every_port_of_the_provider_closes`. |
| `:856-863` | HTTP 400 | HTTP 200. `_MIXED_GRPC_A` and `_MIXED_REST` refuse, and `_MIXED_GRPC_B` accepts. New name: `test_widening_a_stored_filter_onto_the_rest_port_closes_it_too`. |

Three refusals stay, and their tests of today pass with no edit: `TestRefusals::test_a_per_method_override_is_refused`, `::test_a_per_route_override_on_rest_is_refused` and `::test_then_mode_is_refused`.

- [ ] **Step 1.** Write the 14 new functions, and change the six tests.
- [ ] **Step 2.** Run `python -m pytest tests/integration/test_http_and_ws_port_closed.py tests/test_simulator_grpc_port_closed.py tests/test_control_api_port_closed.py tests/test_port_gate.py -q -p no:cacheprovider`. Expected: each new test and each changed test fails. The control API answers HTTP 400 with `only a gRPC endpoint can close its port`. The test at `:42-48` fails on the old text of the HTTP 409.
- [ ] **Step 3.** Change `control_api.py`.
- [ ] **Step 4.** Run the command of Step 2. Expected: no failure. Run the whole suite. Expected: `N + 35 passed`.
- [ ] **Step 5.** Commit: `feat(scenario): port_closed closes an http port and a ws port for real`.

### Task 5: The texts and the documents

**Files:** Modify `README.md:109` and `:122-163`; `docs/using_grpc.md:94`; `docs/using_the_simulator.md:199-207`; `CLAUDE.md:44`; `CONTEXT.md:220-224` and `:237-254`; `provider_simulator/fault_policy.py:36-39` and `:88-91`; `provider_simulator/domain/scenario.py:9-12`; `k8s/deployment.yml:628-630` (a comment); the module docstrings of `tests/test_control_api_port_closed.py` and `tests/test_port_gate.py`.

**Interfaces:** Consumes the behaviour of Task 4. Produces documents that state it.

Each document states the facts that it has room for, as the current fact and with no history:
1. The mode works on each provider endpoint: `http`, `ws` and `http2`. With no filter each port of the provider closes. `transports` and `ports` name fewer.
2. A new connection is refused. An open connection ends with no reply, and a WebSocket connection ends with no close frame.
3. A closed port stores no row. The row of a request in flight stays as it was written.
4. The subscriptions of a closed `ws` port leave `GET /ws/subscriptions` when their connection ends.
5. `down` answers HTTP 503 on an open connection. `port_closed` refuses the connection. A test chooses.
6. The refusals with HTTP 400: a block that targets no endpoint, a per-method override, `then_mode`, and a field that acts on one request. The refusal with HTTP 409: a port with no listener.
7. A control call that asks for the state that the port already has makes no new connection check.
8. The control port, the cache simulator ports and the RESP ports have no gate.

- [ ] **Step 1.** Run `git grep -n -i -e "port_closed" -e "closed port" -e "gRPC port" -- README.md CLAUDE.md CONTEXT.md docs k8s provider_simulator server.py ':!docs/superpowers'`. Expected: hits in each file of this task. This is the control of the search.
- [ ] **Step 2.** Edit each file.
- [ ] **Step 3.** Run the search of Step 1 and read each hit. Expected: no hit says that the mode is for gRPC only, or that another endpoint refuses it. Run `python -m pytest tests/test_service_publishes_every_port.py tests/test_values_sim_matches_topology.py -q -p no:cacheprovider`. Expected: no failure.
- [ ] **Step 4.** Commit: `docs: port_closed closes an http port and a ws port too`.

### Task 6: The proof

**Files:** None in the repository. The outputs go into `handoffs/evidence-2026-10-07-one-request-flow-design/proof-runs-4a-port-closed-for-http-and-ws/` of the automation checkout. Git ignores that folder.

**Interfaces:** Consumes the branch after Task 5. Produces the counts and the list of breaks for the pull request.

- [ ] **Step 1: Two runs in a row.** Run the command of Task 4, Step 2 two times. Expected: the same count each time, and no failure.
- [ ] **Step 2: The breaks.** Break one place of the source on a copy of the worktree in a temporary folder. Run with `PYTHONDONTWRITEBYTECODE=1`. Put the file back from a copy, and compare the checksum. The skill `add-simulator-entity` has the rules. Expected: each break fails the tests of its row, and each of the 25 new functions fails under one break or more.

| Break | The tests that must fail |
|---|---|
| `last_report` returns `self._open` with no finished pass | `test_a_gate_with_no_finished_pass_has_no_report` |
| `last_report` returns `not self._open` | `test_the_last_report_is_what_the_newest_pass_left` |
| `last_report` loses `self._done < self._asked` | `test_a_gate_with_an_ask_that_no_pass_answered_has_no_report` |
| `_settle_ports` asks each gate again | `test_a_write_that_moves_no_port_waits_for_no_gate` |
| `close_port` loses the line `self.socket.shutdown(...)`. **Run it on Linux.** | U1 and `test_a_new_connection_is_refused_on_the_line_after_the_call`. On a Mac both pass, and that is the reason for the Linux run. |
| `close_port` also loses `self.socket.close()` | U1, on both systems |
| `close_port` does not shut down the client sockets | U2, the test of the request in flight, the two tests of `TestAWsConnectionOfAClosedPort` |
| `close_port` calls `self.shutdown()` with no thread | U3 |
| `get_request` loses the check of `closed_by_scenario` | U4 |
| The thread function of `serve` loses its `except` | U5 |
| `handle_error` drops each error of a closed server | U6 |
| `start()` makes no gate for a `ws` endpoint; a second break gives the control port a gate | `test_only_provider_ports_have_a_gate`; the first break also fails `test_with_no_filter_the_http_port_and_the_ws_port_close` |
| The refusal for an endpoint that is not gRPC comes back | Each test of Task 4 |
| The loop of `_NEEDS_A_REQUEST` is deleted | `test_a_field_that_acts_on_one_request_is_still_refused_with_the_mode` |
| `fault_policy.port_closed` loses `and targets(scenario, endpoint)` | `test_a_filter_closes_only_the_endpoint_that_it_names` |
| A pass of `_HttpPortLoop` never closes a port | `test_the_closed_provider_stores_no_row`, and each test of `TestAnHttpPortIsClosed` |
| A pass of `_HttpPortLoop` never opens a port again | Each test of `TestThePortOpensAgain` |
| The factory sets no `listener` on a new server; a second break sets no `subscriptions` | `test_a_port_that_opened_again_serves_the_same_provider` |
| `SimulatorServer.stop()` does not stop the loops | `test_stop_reaches_a_server_that_opened_again` |
| `ControlApi.ports_closed_by_scenario` returns `[]` | `test_ready_leaves_the_closed_ports_out_and_stays_200` |

- [ ] **Step 3: The whole suite.** Run `python -m pytest tests -q -p no:cacheprovider`. Expected: `N + 35 passed`, with no test skipped.
- [ ] **Step 4: The checks of CI.** In a virtual environment of a temporary folder: `pip install 'black==26.5.1' 'ruff==0.15.20' 'mypy==2.2.0'`, then `black --check .`, `ruff check .` and `mypy .`. Expected: no finding.
- [ ] **Step 5: Linux.** Run from the root of the worktree. Docker must run, and no other pytest process.

```bash
docker run --rm --user 0 -v "$PWD":/src:ro provider-simulator:local sh -c "cp -r /src /tmp/work && cd /tmp/work && pip install -q pytest==9.1.1 pytest-timeout==2.4.0 && python -m pytest tests/integration/test_http_and_ws_port_closed.py tests/test_simulator_grpc_port_closed.py tests/test_control_api_port_closed.py tests/test_port_gate.py -q -p no:cacheprovider"
```

Expected: the count of Step 1, and no failure. Run it again with `tests` in the place of the four files. Expected: `N + 35 passed`. The image gives Python 3.12 and the requirements only: the code comes from the worktree. If the machine has no image `provider-simulator:local`, build one under a tag of its own. Do not move the tag `provider-simulator:local`: the bring-up of the local cluster uses it.
- [ ] **Step 6: The time of a scenario write, after.** Run the script of Task 0, Step 5 two times. Expected: the line `gates: 156 | provider ports: 156`. The rule for "slower" is fixed before the reading. A write is slower when its median is more than 1 ms and more than 100 percent above the median of Task 0. A slower write is a stop. Victoria can change this rule.

## 6. The eleven points of the design

| Point of section 14.5 | Where |
|---|---|
| 1. Each scenario write waits on the gates; limit the wait and measure the time | Task 1; Task 0, Step 5 and Task 6, Step 6; gate 4. `test_a_reset_that_cannot_reopen_the_port_is_an_error_too` passes with no edit. |
| 2. `/ready` waits at most 1.0 s for the lock | Task 2: `close_port` waits for nothing (U3). Task 4: `test_ready_leaves_the_closed_ports_out_and_stays_200`. |
| 3. `stop()` and a server that is a new object; its wiring | Task 3: `_HttpPortLoop.stop` and `_provider_server_factory`. Task 4: `test_stop_reaches_a_server_that_opened_again` and `test_a_port_that_opened_again_serves_the_same_provider`. |
| 4. Nothing records the open client sockets | Task 2: `get_request`, `shutdown_request`, U2 and U4. Task 4: the test of the request in flight and the two WebSocket tests. |
| 5. Five tests assert the refusal, and one pins its text | Task 4: the table of the six tests |
| 6. The filter of the automation client | Section 9. Not in this pull request. |
| 7. The documents | Task 5 for the simulator, with `CONTEXT.md` and the deployment comment, which the design does not name. Section 9 for the skill pages. |
| 8. Two more refusals stay: a per-method override and `then_mode` | No code change. Three tests of today hold them and pass with no edit (Task 4). One new test holds the refusal of a field that acts on one request, on an `http` provider. |
| 9. The texts in code that name gRPC | Tasks 1, 3 and 4 for the files that they change. Task 5 for `fault_policy.py` and `domain/scenario.py`. |
| 10. The acceptance is a change on purpose | Task 4. The body of the pull request names it (gate 2). |
| 11. The loop is for provider endpoints only | Task 3: `_ProviderHTTPServer` is made only in the loop of `start` over the provider endpoints. `test_only_provider_ports_have_a_gate`. |

## 7. The gates before the merge

| Gate | What it needs from Victoria |
|---|---|
| 0. The start | The answers to section 2. A go for the worktree, the branch, the plan file and the new test file. |
| 1. Linux, before the push | Nothing. Docker must run. The container downloads pytest. |
| 2. The push and the pull request | One go: "push and open the PR?". Use the skill `pr`, with `--label no-ticket` and `--assignee`. The body names four changes for a caller. (1) The mode is accepted for `http` and `ws`. (2) A control call that moves no port makes no connection check. (3) `SimulatorServer.stop()` closes the sockets of the provider ports. (4) The texts of the HTTP 409 and of the HTTP 500 name no gRPC. The body also names each break of Task 6. |
| 3. CI | Nothing. Read `gh pr checks <number>`: `lint`, `test` and each other check pass, and the log of `test` says `N + 35 passed`. In this repository no rule makes a red check block a merge (READ in the skill `shared-skill`), so read each check. |
| 4. The automation suites on the local k3d cluster, with the branch build | A free cluster: ask each open session. A go for each step that changes the cluster. The steps are the import of the image, the swap of the image of the deployment `provider-simulator`, and the way back. Her word for the test file with the marker `destructive`. The skill `local-cluster-testing`, section "Swapping the simulator's build", has the commands. |
| 5. The review | A go for the Copilot review. The skill `can-i-merge` and the tests must be clean. |
| 6. The merge | Its own go. |

**What gate 4 runs.** First the image of the base, then the branch build, each with a new simulator pod and one pairing reset on each router. Read `GET /version` before each measurement.
- Each suite that the skill `pick-suite` lists for a simulator change, the cache suites and the RESP suites too. The pass condition: the same outcome for each test id on both images.
- `time_set_scenario_remote.py http://localhost:31000 eth-sim:1` on both images (point 4 of the spike).
- Points 6 and 7 of the spike again, with their scripts of the evidence folder: `run2_router_on_a_refused_http_provider.py` and `run3b_ws_provider_that_holds_the_subscription.py`.
- The two open points of the design. One: the health of an `http` endpoint after many refused connections. Two: how a router gets a subscription again after a `ws` port opens.
- The thread count and the memory of the simulator process on both images.

## 8. Rollback

Revert the pull request while it is the newest merged one. One revert takes all of it back: the rule for the wait, the gates, the loops and the texts. After the revert the control API answers HTTP 400 for an `http` or `ws` endpoint again. Pull request 4b can be merged at that time. Its two new tests then fail with that HTTP 400 as a plain `HTTPError`, and not as a setup problem (section 1.1). So revert 4b first, or let 4b convert that refusal.

## 9. The pull request of the automation repository (4b)

It opens after 4a is on `main` of the simulator and its image is published. The CI of the automation repository checks out the default branch of the simulator. Another draft holds its full plan.

- **What changes.** `_resolve_transports` (`tests/simulator/sim_control.py:114-119`) returns `None` for `port_closed`, as for `down`. So each port of the provider closes: `http`, `ws` and `http2`. Today it returns `["http", "http2"]`, and the `ws` port stays open.
- **The texts.** `sim_control.py:169-174`, `:456-457` and `:535-537`. `tests/simulator/_grpc_replies.py:65-77` and the helper `a_refused_port_closed_reads_as_setup` at `:179-207`. Decide there what the refusal of a simulator with no 4a reads as (section 8).
- **The unit tests of these places**, in `tests/infrastructure/unit/`. The class `TestResolveTransports` of `test_sim_control_pool_translation.py:66-85` has no case for `port_closed` today. `test_grpc_reply_readers_and_port_closed_refusal.py:142-155` holds the refusals. `test_rest_and_grpc_failover_steps_case_ids_and_direct_answers.py:246-261` holds `set_fault` with `port_closed`.
- **Two new tests (part B2).** The router rule for a refused connection on an `http` provider. They are twins of `test_priority_0_connection_error` and `test_two_connection_failures_before_a_success_are_all_counted_in_lava_retries` of `test_router_retry_error_classifier_tiers.py` (`:653`, `:886`), with `mode="port_closed"`. Expected, as the session `worker` measured on 2026-10-08: the class `PROTOCOL_CONNECTION_REFUSED`, code 1002, `Internal`, and the text `connect: connection refused`. A closed port stores no row, so the tests count no `down` row.
- **The skill pages.** `failover/references/what-the-simulator-can-tell-you.md:20`, `:163-165` and `:296-356`; `writing-simulator-tests/SKILL.md:172`, `:229` and `:258-259`; `failover/references/the-routers.md:147-148`; `failover/references/what-the-router-tells-you.md:176`.

## 10. What I could not verify by a read

1. Nothing of this plan ran. The readings are of the spike, and the code of this plan is not the code of the spike.
2. Five places where the source of this plan differs from the spike have no reading. (a) The rule C of choice 1. (b) The end of a connection that is accepted during the close. (c) The `except` of the serve thread. (d) The narrow `handle_error`. (e) The warning for an error that is not `ENOTCONN`.
3. The branch of the WebSocket move is not written. The line numbers of section 4, the loop of `start`, and the two lines of `_WsHandler` that this plan depends on can change.
4. The count `N` of the suite on the base. `main` has 1863 tests (READ in the plan of pull request 3a, from a CI log).
5. The exact error that a client of a request in flight gets on Linux. The spike accepted each error. On macOS the design session read `RemoteDisconnected` on 2026-10-07.
6. That each test with a time bound is stable on Linux. The bounds are 2 s for the control call, 5 s for the request in flight and 2 s for the subscription entry.
7. The cost of the rule A of choice 1 with 156 gates. On the unchanged build the session `worker` read `POST /reset/all` at 1.99 ms and 2.38 ms, with 12 gates.
8. That the image `provider-simulator:local` is on the machine, and that the container can download pytest.
9. The memory of the simulator with 144 more threads. `k8s/deployment.yml:623-627` sets a limit of 256Mi and 500m. The chart of the local k3d cluster sets no limit, so the run of gate 4 does not show if the pod fits that limit.
10. Two points of the design about the router are open (gate 4). The first is the health of an `http` endpoint after many refused connections. The second is the subscription after a `ws` port opens. The spike read: the router did not subscribe again in 30 s.
11. That the list of unit tests in section 9 is complete. I found it with a search for `_resolve_transports` and `port_closed`.
