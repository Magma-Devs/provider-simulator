# WebSocket Frames Use Listener.serve: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development to implement this plan task by task.

> **The state of this plan on 2026-10-10.** A helper that only read wrote it, from the code of the branch `pin-todays-websocket-rows` at `5657f71`. Nothing ran before Task 0. Victoria gave her go for this plan on 2026-10-10, with the decisions that section 2 names. The line numbers of `server.py` and of `tests/test_simulator_ws.py` below are of `5657f71`.

## 1. Goal, architecture and tech stack

**Goal:** Each WebSocket frame that is JSON goes through `Listener.serve`. The WebSocket listener decides the upgrade request. Only the package `provider_simulator/listeners/` writes a history row or asks `fault_policy` about a request. `server.py` imports no `fault_policy` and writes no history row, and a guard test holds that. No reply and no history row changes, but the values that section 5 names.

**Architecture:** `JsonRpcWsListener`, a new subclass of `JsonRpcListener` in `provider_simulator/listeners/ws.py`, serves each `ws` endpoint. `Listener.serve` gets one new hook, `build_content`, which gives the success content. Its default asks the chain. `JsonRpcWsListener` answers a subscribe frame and an unsubscribe frame from the registry `WsSubscriptions`. It asks the chain for each other frame. Its method `decide_upgrade` decides the upgrade request and writes the row of a refusal. The registry writes the row of a pushed event. A new method, `Listener.arrive`, writes the arrival row for each adapter. The adapter `_WsHandler` keeps the socket, the frame bytes, the reader thread and the writer thread. It still answers three cases with no listener: a wrong path, a bad upgrade request, and a frame that is not JSON text. Choice 2 can add a fourth case. The 9 `ws` ports and the thread model do not change.

**Tech Stack:** Python 3.12 and its standard library only: `http.server`, `socketserver`, `threading`, `queue`, `secrets`, `json`, and `ast` in the guard test. pytest 9.1.1 with pytest-timeout 2.4.0. The checks of CI: black 26.5.1, ruff 0.15.20 and mypy 2.2.0 (`.github/workflows/lint-and-test.yml`).

**Spec:** The design "One request flow for every interface", version 2.3 at `4c4b3af`. Its sections 4.2 to 4.4, 8.1, 8.4, 8.5, 9.2 (rows 3 to 6) and 9.3 (differences 8 to 12). Its sections 10 (step 3), 12, 12.1, 13, 13.1 and 14.6. The decision record ADR-001. This is pull request 3b of the design. Its specification is the recorded tests of pull request 144: 114 cases in five classes of `tests/test_simulator_ws.py`, from line 1402 to the end.

**Where the code of today differs from the design.** The design describes the code at `e80acbf`. The code of today is right.

1. The line numbers of `server.py` in sections 4.2, 4.3, 8.4 and 13.1 are old. Today: `_HttpListenerHandler._run` 130-148, `_WireSubscriptions` 377-417, `do_GET` 434-491, `_refuse_upgrade` 493-553, `_reader_loop` 555-595, `_serve_subscription_frame` 597-670, `_perform_frame` 672-698, `_HTTP_ADAPTERS` 1266-1271, the loop of `start` 1360-1369.
2. Section 4.3 names six places that write a history row. Today five do, because `GrpcListener.plan` is gone since pull request 141. They are `Listener.serve` and four places of `server.py`: line 136, line 407, line 501, and lines 612 to 663.
3. Section 9.3 (difference 2) says that a `down` row and a `hang` row of `serve` record the configured latency. Pull request 143 changed that, as section 10.5 planned: they record 0 (`base.py:185`, `:222`, `:273`). Section 9.2 has no word about it. So the `down` row of its row 6 also gets `latency_ms` 0. The `hang` row of a subscribe frame changes too, and section 9.2 does not name it: choice 1.
4. Section 12.1 says that three tests build `WsSubscriptions()` with no argument. Today 22 call sites in 8 test files do: `test_ws_subscriptions.py`, `test_control_api.py`, `test_control_api_cache.py`, `test_control_api_port_closed.py`, `test_control_api_providers.py`, `test_cache_sim_wire.py`, `test_resp_control.py` and `test_provider_name_on_every_reply.py`. `tests/test_ws_subscriptions.py:18-22` also needs the plain event on the queue.
5. Section 8.4 adds one hook and one method. The move needs three more things that the design does not name: the result of the upgrade decision, the removal of the subscriptions of a connection that ended, and a split of `emit`. Section 4 names them.
6. Section 10 names one document, the WebSocket paragraph of `CLAUDE.md`. Pull request 143 added two sentences about a subscribe frame to `docs/using_the_simulator.md` (lines 158 and 253). The move makes them false: section 8.

## 2. The choices, and what Victoria decided

**Victoria's decisions of 2026-10-10.** She said "go" to this list.

| Choice | Her decision |
|---|---|
| 1. The wait time in the row of a frame that hangs | A: the row shows 0 |
| 2. A frame whose `method` is a list or an object | A: the adapter closes the connection as today, with no row and no Python error |
| 3. An unsubscribe frame whose `params` has a wrong shape | A: record it first, and keep it. A repair is a separate change. |
| 4. The file of the guard test | A: `tests/integration/test_server_writes_no_history_row.py` |
| 5. The start of the branch | A: from the head of the branch of pull request 144, `5657f71` |
| 6. Records first | A: Task 1 records four behaviours of today, 8 cases |
| 7. The names | The names of section 4. She saw the branch, the plan file and the guard file by name. She did not see the six names that this plan adds, so the text of the pull request lists them, and she can change each one. |
| 8. The run of the automation suites before the merge | Not decided. It is a gate before the merge, and no task of this plan needs it. |
| The count of runs in a row | Two, as for pull request 144 |

The text of each choice follows, as the record of the options.

The four changes on purpose of section 9.2 of the design are not choices of this plan. A corruption reaches the reply of a subscribe frame (row 3). A per-method `error_probability` reaches a subscribe frame (row 4). A canned `body` answers a subscribe frame (row 5). A provider-wide `down` comes before a per-method override (row 6). Section 15.1 of the design says that the design session closed them, and that Victoria did not say yes to each one. Her go for this plan is that yes. Section 5 has each value that changes.

**Choice 1. The wait time in the history row of a frame that hangs.** A provider in the mode `hang` gives no reply to a subscribe frame. Today the row of that frame shows the `latency_ms` of the scenario: 250 in the recorded test. The provider did not wait that time. Each other frame and each other interface shows 0 since pull request 143.
- A: the row shows 0, as everywhere. One recorded value changes.
- B: the row keeps 250. That needs one more hook, which only this frame uses.
- Recommendation: A. Pull request 143 made this rule the same on every interface, and it deleted the hook that B brings back.

**Choice 2. A frame whose `method` is a list or an object.** Today the simulator closes the connection with no reply, and the history gets no row. It is a defect, and a recorded test holds it as it is. After the move such a frame reaches the request flow. The flow writes the arrival row, and then it stops with an error. So one unfinished row (`in_flight`) stays in `GET /history` and in `GET /stats`. It stays until the next `POST /history/clear` or `POST /reset/all`.
- A: the adapter closes the connection for such a frame before the request flow sees it. Nothing changes for a caller or for the history. The recorded test passes with no edit.
- B: the frame reaches the request flow. The caller sees the same close. The history gets the unfinished row, and the recorded test changes from no row to one row `in_flight`.
- C: repair the defect: the caller gets a JSON-RPC error, and the row is complete. That also changes each `http` JSON-RPC endpoint, where the same frame stops the flow today.
- Recommendation: A in this pull request. C is a pull request of its own, because it changes endpoints that this move does not touch.

**Choice 3. An unsubscribe frame whose `params` has a wrong shape.** The shapes are an object, a number, and a list whose first item is a list or an object. Today the simulator closes the connection with no reply. One unfinished row (`in_flight`) stays in the history. No test holds it.
- A: record it first, and keep it. The same lines move, so the result stays the same.
- B: repair it in this pull request: the reply is `false` (nothing removed), and the row is complete. Four values of Task 1 then change on purpose.
- Recommendation: A. The repair goes into the pull request that repairs the first defect (choice 2, C). Then the move changes nothing for a malformed frame, and one pull request repairs both defects with one rule.

**Choice 4. The file of the guard test.** The guard is the one new test file of this plan. Each other new test goes at the end of a test file that exists.
- A: `tests/integration/test_server_writes_no_history_row.py`. The folder exists.
- B: a new folder for tests that read source text.
- Recommendation: A.

**Choice 5. The start of the branch.** Pull request 144 holds the recorded tests. It is open, and its branch head is `5657f71`.
- A: start now from the head of that branch. Rebase onto `main` when 144 merges. Open this pull request after that.
- B: wait for the merge of 144.
- Recommendation: A. A new branch needs her go in both cases.

**Choice 6. Records first.** Four behaviours of today have no test, and the move touches each one. Task 1 names them.
- A: Task 1 records them on today's code, 8 cases. Then each change shows as a changed expected value.
- B: no records. The text of the pull request names the changes.
- Recommendation: A. It is the method of pull requests 140 and 143.

**Choice 7. The names.** The design proposes three names: the class `JsonRpcWsListener`, the hook `build_content` and the method `arrive`. This plan keeps them and adds six: `decide_upgrade`, `release`, `WsConnection`, `RawRequest.connection`, `WsSubscriptions.frame_of` and `event_message`. The upgrade decision is a `ServeResult`, with the status 101 for "complete the handshake". So the plan adds no result class. Victoria can change each name.

**Choice 8. The run of the automation suites before the merge.** Section 12 of the design asks for it, with the branch build. The facts below are from the handoff of 2026-10-09. I checked one of them by a read: the four workflow files take the input `provider_simulator_ref`.
- A: the four workflows on GitHub that take a simulator commit, as for pull request 143: `nightly-sim-suite-k3d-by-feature.yml`, `cache-regression-k3d.yml`, `failover-regression-k3d.yml` and `weekly-slow-suite-k3d.yml`. A manual run writes to Jira, to the issue of the day, to GitHub Pages and to Slack. So it needs the temporary branch that switches those writes off.
- B: the local k3d cluster. The harness refused the image swap on 2026-10-09. So Victoria makes the swap herself, or she adds a permission rule.
- The base of the comparison: the runs of 2026-10-09 with the simulator commit `6f23faa`, if the automation commit and the router tag are the same. If not, new runs with the image of `main`.
- Recommendation: A, with the base of 2026-10-09.

## 3. Global constraints

1. The recorded tests are the specification. An expected value changes only when section 5 names it. A value that fails and that section 5 does not name is a stop.
2. No test of `main` is edited: lines 1 to 1400 of `tests/test_simulator_ws.py`, and each other test file. New tests go at the end of a file. One text of such a file changes: the module docstring of `tests/test_ws_subscriptions.py`.
3. A new test file never goes directly into `tests/`. This plan creates one test file (choice 4).
4. Each new test fails first. A record of Task 1 is the one exception: it passes on today's code. Each new or changed test gets one break of the source that makes it fail (Task 8).
5. One pytest process at a time on the machine. Before a run, `lsof -nP -iTCP:19000 -sTCP:LISTEN` must print nothing.
6. Linux decides. The WebSocket tests run one time in a container before the push, and CI runs on `ubuntu-latest`.
7. Each text is in Simplified Technical English: this plan, each comment, each docstring, each commit message.
8. A commit has a conventional subject and no trailer. It does not name an agent configuration file. Stage each file by its name.
9. The branch is `websocket-frames-use-listener-serve`. The pull request has the label `no-ticket`. No ticket key is in the branch name, the title or the body.
10. The checks of CI are clean at each commit: `black --check .`, `ruff check .`, `mypy .`. Line length 120.
11. The topology does not change. The four topology tests pass with no edit: `test_domain_topology.py`, `test_control_api_providers.py`, `test_service_publishes_every_port.py` and `test_values_sim_matches_topology.py`. No new library.
12. The five rules of the upgrade stay (differences 8 to 12 of section 9.3 of the design). The upgrade applies no `latency_ms`. It applies no corruption and no per-method override. A refusal writes one complete row, and a success writes none. The upgrade has its own reply bodies. Each upgrade uses one count of the `fail_first_n` window.
13. `WsSubscriptions()` with no argument stays possible, and it puts the plain event on the queue.
14. A worktree has no virtual environment. `python` below is `.venv/bin/python` of the main checkout.
15. A step that does not go as this plan says is a stop: tell Victoria, and do not work around it.

## 4. The file structure

| File | Its one job in this change |
|---|---|
| `provider_simulator/listeners/base.py` | `RawRequest.connection`, `Listener.arrive`, the hook `Listener.build_content`, and two lines of `serve` that call them |
| `provider_simulator/listeners/ws.py` | `JsonRpcWsListener`, `WsConnection`, `event_message`; `WsSubscriptions` gets a registry and writes the row of a pushed event |
| `provider_simulator/listeners/__init__.py` | Export `JsonRpcWsListener` |
| `server.py` | `_run` asks `listener.arrive`. `_WsHandler` performs the upgrade decision and gives each JSON text frame to `serve`. `_WireSubscriptions` keeps the frame bytes only. `_serve_subscription_frame`, `_STATUS_LABEL` and three imports go: `fault_policy`, `secrets` and `stubs_ws`. |
| `stubs_ws.py` | One sentence of its docstring: the WebSocket listener imports the tables |
| `tests/integration/test_server_writes_no_history_row.py` (new) | The guard: it reads `server.py` with `ast` |
| `tests/test_simulator_ws.py` | The records of Task 1 at its end. The changed values of section 5. Comments and docstrings that name code that is gone. |
| `tests/test_ws_subscriptions.py`, `tests/test_listener_jsonrpc.py` | New tests with no socket, at the end of each file |
| `CLAUDE.md`, `README.md`, `docs/using_the_simulator.md` | The texts of section 8 |
| `docs/superpowers/plans/2026-10-10-websocket-frames-use-listener-serve.md` (new) | The final plan. It is the first commit of the branch, as in pull requests 143 and 144. |

**The new names and their signatures.**

- The class: `JsonRpcWsListener(JsonRpcListener)`, with `__init__(self, provider, endpoint, subscriptions: WsSubscriptions)`.
- The hook: `Listener.build_content(self, parsed: dict, scenario: dict, request: RawRequest) -> tuple[int, object]`. The default returns `chain.build_success(...)`, as lines 249-252 of `base.py` do today.
- The method: `Listener.arrive(self, headers: dict | None = None) -> dict`. It keeps the `lava-` headers and returns the row for `serve(request, entry=...)`.
- `RawRequest.connection: object = None`: the `WsConnection` of a frame. Each other adapter leaves it empty.
- `WsConnection(out_queue: queue.Queue, subscription_ids: set[str])`: a dataclass. One reader thread owns it.
- `JsonRpcWsListener.decide_upgrade(self, headers: dict) -> ServeResult` and `JsonRpcWsListener.release(self, connection: WsConnection) -> None`.
- `WsSubscriptions.__init__(self, registry: Registry | None = None)` and `WsSubscriptions.frame_of(self, sub: Subscription, event: object) -> object`. The default of `frame_of` returns the event as it is.
- `event_message(sub: Subscription, event: object) -> dict`: the JSON message of one pushed event, in the envelope of its subscribe method.

**For the plan of the real down, which starts from this branch.** This plan changes these places of `server.py`.

- At its head: the module docstring (19-22), four imports (36, 44, 56, 60-71), `_STATUS_LABEL` (78-85), and the attribute `subscriptions` of `_SimThreadingHTTPServer` (105).
- In the adapters: `_run` (130-148), `_WireSubscriptions` (377-417), and the four methods `do_GET`, `_refuse_upgrade`, `_reader_loop` and `_serve_subscription_frame` (434-670).
- In the bootstrap: the `ws` row of `_HTTP_ADAPTERS` (1268), and two lines of `start` (1367-1368).

It does not change `_ws_writer_loop`, `_ws_text_frame`, `_perform_frame`, `_send_simple_error`, `stop` or `wait_ready`.

## 5. The recorded tests whose expected value changes

All six tests are in the class `TestSubscribeAndUnsubscribeFrames`. Each one also gets a name and a docstring that are true after the move. Eleven cases change: 1, 1, 6, 1, 1 and 1.

| Test today, and its new name | Today | After the move | Allowed by |
|---|---|---|---|
| `test_a_subscribe_frame_of_a_down_provider_closes_the_connection_and_its_row_names_the_frame` (line 2030). New: `test_a_subscribe_frame_of_a_down_provider_closes_the_connection_and_its_row_names_no_frame` | Row `("eth_subscribe", "down", 1500, 7)` | Row `("*", "down", 0, None)`. The close, the endpoint and the lava headers stay. | Row 6 of section 9.2: `down` comes first |
| `test_a_per_method_success_comes_before_a_provider_wide_down_for_a_subscribe_frame` (2491). New: `test_a_provider_wide_down_comes_before_a_per_method_success_for_a_subscribe_frame` | The subscribe succeeds, with one subscription. Rows `("eth_subscribe", "success", 0, 7)` and `("*", "down", 0, None)` of the next frame | No reply, and the connection closes. One row, `("*", "down", 0, None)`. No subscription. | Row 6: `down` comes first |
| `test_a_corruption_mode_does_not_change_the_reply_of_a_subscribe_or_an_unsubscribe_frame` (2190), six cases. New: `test_a_corruption_mode_changes_the_reply_of_a_subscribe_and_of_an_unsubscribe_frame` | Both replies are whole | The table below. The subscription is registered, and both rows stay `success`. | Row 3: the corruption applies |
| `test_a_per_method_error_probability_is_not_read_for_a_subscribe_frame` (2372). New: `test_a_per_method_error_probability_reaches_a_subscribe_frame` | A subscription id. Row `("eth_subscribe", "success", 0, 7)`. One subscription. | The error reply with the code -32000 and the id 7. Row `("eth_subscribe", "error", 0, 7)`. No subscription. | Row 4: the per-method `error_probability` applies |
| `test_a_canned_body_is_not_read_for_a_subscribe_frame` (2401). New: `test_a_canned_body_answers_a_subscribe_frame_and_registers_no_subscription` | A reply with the id 7 and a subscription id. One subscription. | The reply is the canned body, with its id 1. No subscription. New assert: row `("eth_subscribe", "success", 0, 7)`. | Row 5: the canned `body` applies |
| `test_a_subscribe_frame_of_a_hung_provider_gets_no_reply_and_the_connection_stays_open` (2050) | Row `("eth_subscribe", "hang", 250, 7)` | Row `("eth_subscribe", "hang", 0, 7)` | Choice 1, A: the row shows 0 |

The payload of each reply under a corruption, with `missing_field: "result"`. The test reads the subscription id from `GET /ws/subscriptions`.

| `corruption_mode` | The subscribe reply | The unsubscribe reply |
|---|---|---|
| `truncated` | `{"jsonrpc": "2.0", "id": 7, "result": "0x` and 24 hex digits: 65 bytes | `{"jsonrpc": "2.0", "id": 9, "resu` |
| `invalid_json` | `}{ {{ not valid json` | The same |
| `empty_response` | An empty text frame | The same |
| `null_body` | `null` | `null` |
| `missing_field` | `{"jsonrpc": "2.0", "id": 7}` | `{"jsonrpc": "2.0", "id": 9}` |
| `wrong_type` | `{"jsonrpc": "2.0", "id": 7, "result": 12345}` | `{"jsonrpc": "2.0", "id": 9, "result": 1}` |

Three records of Task 1 change too. The `down` row of an unsubscribe frame: `("eth_unsubscribe", "down", 250, 9)` becomes `("*", "down", 0, None)`, because `down` comes first (row 6). Its `hang` row: 250 becomes 0 (choice 1, A). The canned body under a provider-wide `rate_limit`: the rate-limit text becomes the canned body (row 5). Its row `rate_limit` becomes `("eth_subscribe", "success", 0, 7)`.

No other value changes when Victoria takes the recommendations of choices 2 and 3. Then the adapter closes for a `method` that is a list or an object, and the `params` lines move as they are. `test_a_frame_whose_method_is_a_list_or_an_object_closes_the_connection` (line 2878) passes with no edit.

## 6. The tasks

Each source task ends with the whole suite, the three checks and one commit. The command for the whole suite is `python -m pytest tests -q -p no:cacheprovider`. Each count below is computed from the lists of this plan. A module docstring changes in the task that changes its code.

### Task 0: The worktree, the baseline and the plan file

**Files:** Create the final plan (section 4).
**Interfaces:** Consumes the head of the branch of pull request 144, `5657f71`. Produces the worktree and the branch `websocket-frames-use-listener-serve`.
**Steps:** 1. Make the worktree with the worktree tool of the harness. The tool starts a branch at `origin/main`, so move the branch to `5657f71` with a fast-forward, and give it its name. 2. Run the whole suite. 3. Commit the plan.
**Verify:** `1977 passed`: the plan of pull request 144 states this count, and the job `test` of that pull request printed it on Linux. Another count is a stop.
**Commit:** `docs(plan): the implementation plan of pull request 3b of the request-flow design`

### Task 1: Records of today's behaviour that no test holds

**Files:** Modify `tests/test_simulator_ws.py`: one new class at its end, `TestMoreOfWhatWebSocketDoesToday`. No source file.
**Interfaces:** Consumes the helpers of the recorded tests: `_websocket`, `_send_frame`, `_reply`, `_payload`, `_rows`, `_rows_when_complete`, `_emit`, `_subscribe`, `_read_until_the_close`. Produces 8 cases that pass on today's code. Task 6 changes three of them on purpose.
**Tests:**
- `test_the_row_of_an_unsubscribe_frame_of_a_down_or_a_hung_provider`, cases `down` and `hang`. Its row is `("eth_unsubscribe", <mode>, 250, 9)`, as `server.py:631-646` writes it.
- `test_a_canned_body_for_a_subscribe_method_under_a_provider_wide_rate_limit`. The frame gets the rate-limit text, its row is `("eth_subscribe", "rate_limit", 0, 7)`, and no subscription exists.
- `test_an_event_for_a_full_queue_gets_503_and_writes_no_row`. It registers a subscription with `queue.Queue(maxsize=1)` on `sim["server"].subscriptions`. The first event gets 200 and one row. The second gets 503 with `queue full` and no second row.
- `test_an_unsubscribe_frame_with_params_of_a_wrong_shape_closes_the_connection_and_leaves_its_row_in_flight`, four cases: `{"id": "0x00"}`, `5`, `[["0x00"]]` and `[{"id": "0x00"}]`. No byte arrives, the connection closes, and the rows are `[("*", "in_flight", 0, None)]`. The docstring says that the test records a defect and does not judge it.
**Verify:** `python -m pytest tests/test_simulator_ws.py -q -p no:cacheprovider` gives `181 passed`. A record that fails is a stop: this plan read today's code wrong.
**Commit:** `test(ws): record four behaviours of today before the move`

### Task 2: An adapter asks the listener to record the arrival

**Files:** Modify `provider_simulator/listeners/base.py`, `server.py` (`_run`), `tests/test_listener_jsonrpc.py`.
**Interfaces:** Produces `Listener.arrive`. `serve(request, entry=...)` keeps its signature.
**Tests first:**
- `test_arrive_writes_one_row_in_flight_with_the_lava_headers_of_the_call`. After `arrive` with a lava header and another header, the history has one `in_flight` row with the lava header only and the endpoint.
- `test_serve_finishes_the_row_that_arrive_wrote`. `serve` with that row gives one `success` row, and `stats()` counts no `in_flight`.
**Steps:** 1. Write the tests. They fail: `Listener` has no `arrive`. 2. Add `arrive`. In `serve`, the body of `if entry is None:` (lines 168-174) becomes `entry = self.arrive(request.headers)`. 3. In `_run`, call `entry = listener.arrive(headers)` **before** `self.rfile.read(length)`. `headers` is `dict(self.headers.items())`, and the `RawRequest` gets the same object.
**Verify:** The two tests pass. `tests/test_simulator.py::TestCancelDuringResponseRecordsArrival` passes with no edit: its three socket tests hold the row before the body read. The whole suite: `1987 passed`. `grep -n "record_arrival" server.py` prints one line, in `_serve_subscription_frame`.
**Commit:** `refactor(listeners): an adapter asks the listener to record the arrival`

### Task 3: The hook `build_content`

**Files:** Modify `provider_simulator/listeners/base.py`, `tests/test_listener_jsonrpc.py`.
**Interfaces:** Produces `RawRequest.connection` and `Listener.build_content`. No listener overrides the hook in this task.
**Tests first:**
- `test_the_flow_asks_build_content_for_the_success_content_and_gives_it_the_request`. A subclass returns a body with the id 5. `serve` returns that body, the row has the request id 5, and the hook got the `RawRequest` with its `connection`.
- `test_the_flow_asks_build_content_for_a_success_and_not_for_a_fault_or_a_canned_body`, six cases: `error`, `rate_limit`, `hang`, `drop_connection`, a per-method `down`, and a canned body. The hook counts one call after a success request, and still one call after the request of the case.
**Steps:** 1. Write the tests. They fail: the flow asks the chain, so the hook counts no call. 2. Add the field and the hook. In `serve`, lines 249-252 become `status, body = self.build_content(parsed, scenario, request)`.
**Verify:** The whole suite: `1994 passed`, with no test edited.
**Commit:** `refactor(listeners): the hook build_content gives the success content of a request`

### Task 4: The WebSocket listener decides the upgrade

**Files:** Modify `provider_simulator/listeners/ws.py`, `provider_simulator/listeners/__init__.py`, `server.py` (`do_GET`, `_refuse_upgrade`, `_HTTP_ADAPTERS`, `start`), `tests/test_ws_subscriptions.py`.
**Interfaces:** Produces `JsonRpcWsListener` and `decide_upgrade`. The frames still use `_serve_subscription_frame`, which needs only `provider`, `endpoint` and `build_fault` of the listener.

| Verdict of `fault_policy.decide` | Row that `decide_upgrade` writes | The `ServeResult` |
|---|---|---|
| `none` | No row | `action="respond"`, `status=101` |
| `down` | `*`, `down` | `respond`, 503, `{"error": "provider down"}` |
| `rate_limit` | `ws_upgrade`, `rate_limit` | `respond`, 429, `{"error": "rate limited"}` |
| `error` | `ws_upgrade`, `error` | `respond`, 400, `{"error": <the error_message of the scenario>}` |
| `hang` | `ws_upgrade`, `hang` | `action="hang"` |
| `drop` | `ws_upgrade`, `drop_connection` | `action="drop"`, with the `drop_at` of the verdict |

Each row has `latency_ms` 0, no request id and the lava headers of the upgrade request. The method reads `error_message` and the verdict only. It writes the row with `log.push` before it returns, so the row exists while the adapter holds a `hang`.
**Tests first:**
- `test_what_the_listener_decides_for_an_upgrade`, nine cases. Six are the rows of the table. One is a `down` with `transports: ["http"]`: 101 and no row. One is an `error_probability` of 1.0: 400. One is a `success` with a latency, a corruption and a `responses` entry: 101 and no row.
- `test_the_row_of_a_refused_upgrade_names_the_ws_endpoint_and_carries_the_lava_headers_only`. The row has the interface, the transport and the port of the `ws` endpoint, and no other header.
- `test_each_upgrade_decision_uses_one_count_of_the_fail_first_n_window`. With `fail_first_n` 2, three decisions give 429, 429 and 101, and two rows.
**Steps:** 1. Write the tests. They fail: `ws.py` has no `JsonRpcWsListener`. 2. Add the class and the method. 3. In `do_GET`, keep the 404 and the 400 above the decision. Then `decision = listener.decide_upgrade(lava)`. 4. `_refuse_upgrade(decision, client_key)` performs it: the JSON refusal, the 30 seconds of a `hang`, or the bytes of a drop point. Its bytes do not change. 5. The `ws` row of `_HTTP_ADAPTERS` names `JsonRpcWsListener`. `start` builds it with `self.subscriptions`.
**Verify:** The 31 cases of `TestTheUpgradeRequest` pass with no edit, and so do `TestPreHandshakeFaults`, `TestWsCrossPoolIsolation`, `TestWsSequencedFaults` and `tests/test_simulator_backup_listeners.py`. The whole suite: `2005 passed`. `grep -n "log\.push" server.py` prints one line, in `_WireSubscriptions.emit`.
**Commit:** `refactor(ws): the WebSocket listener decides the upgrade and writes its row`

### Task 5: The registry writes the row of a pushed event

**Files:** Modify `provider_simulator/listeners/ws.py`, `server.py` (`_WireSubscriptions`), `stubs_ws.py` (one sentence of its docstring), `tests/test_ws_subscriptions.py`.
**Interfaces:** Produces `WsSubscriptions(registry)`, `frame_of` and `event_message`. `ControlApi.ws_emit` does not change. `SimulatorServer.subscriptions` stays: the fixture `clean_state` calls its `clear()`.
**Tests first:**
- `test_emit_with_a_registry_writes_one_push_row_for_the_ws_endpoint_of_the_provider`. A subscription of `eth-sim:2` with `accountSubscribe` gives the row `("solana_account push", "success", 0, <id>)`, with the `ws` port of provider 2 and no lava header.
- `test_emit_on_a_full_queue_writes_no_row`. The result is `full`, and the log of the provider has no row.
- `test_emit_for_a_provider_that_the_registry_does_not_hold_writes_no_row`. The result is `emitted`, and no log gets a row.
- `test_frame_of_gives_what_goes_on_the_queue`. A subclass returns bytes, and the queue holds those bytes.
**Steps:** 1. Write the tests. They fail: `WsSubscriptions` takes no registry and asks no `frame_of`. 2. `WsSubscriptions.emit` puts `self.frame_of(sub, event)` on the queue **first**. `queue.Full` returns `"full"` before any row. Then it writes the row, with the code of `server.py:402-416`, only when it has a registry. It asks no fault policy. 3. `_WireSubscriptions` keeps one method: `frame_of` returns `_ws_text_frame(event_message(sub, event))`. `server.py` keeps `import stubs_ws` until Task 6.
**Verify:** The 16 cases of `TestAPushedEvent`, the full-queue record of Task 1 and the six tests of `main` in `tests/test_ws_subscriptions.py` pass with no edit. The whole suite: `2009 passed`. `grep -n "log\.push" server.py` prints nothing.
**Commit:** `refactor(ws): the subscription registry writes the row of a pushed event`

### Task 6: Each JSON frame goes through `Listener.serve`, and the guard

**Files:** Modify `provider_simulator/listeners/ws.py`, `server.py` (`_reader_loop`; deletions), `tests/test_ws_subscriptions.py`, `tests/test_simulator_ws.py` (section 5). Create `tests/integration/test_server_writes_no_history_row.py`.
**Interfaces:** Consumes `build_content`, `RawRequest.connection` and `JsonRpcWsListener`. Produces `WsConnection`, `JsonRpcWsListener.build_content`, `release`, and the guard.
**Tests first, with no socket** (12 cases):
- `test_a_subscribe_frame_registers_a_subscription_on_the_connection_of_the_request`. The registry entry has the pool, the provider id, the method and the queue of that connection. The row is `(method, "success", 0, id)`.
- `test_an_unsubscribe_frame_removes_a_subscription_of_its_own_connection_only`. Another connection of the same listener gets `false`, and the owner gets `true`.
- `test_a_fault_or_a_canned_body_on_a_subscribe_frame_registers_nothing`, seven cases: `down`, `hang`, `rate_limit`, `error`, `drop_connection`, a per-method `down`, a canned body.
- `test_release_removes_each_subscription_of_one_connection_and_no_other`. The registry keeps the subscription of a second connection.
- `test_a_frame_of_another_method_gets_the_content_of_the_chain`. `eth_blockNumber` gets the head, and a frame with no method gets `0x1` with the row method `unknown`.
- `test_a_subscribe_frame_with_no_connection_raises_a_clear_error`. A request with no `WsConnection` raises `ValueError`.
**Tests first, the guard** (3 cases). One function, `_forbidden(source: str) -> list[str]`, reads a source text with `ast`. So a comment or a docstring does not count.
- `test_server_py_imports_no_fault_policy`: `server.py` has no import, no name and no attribute `fault_policy`.
- `test_server_py_calls_no_method_that_writes_a_history_row`: it has no call of an attribute `record_arrival`, `finalize` or `push`.
- `test_the_guard_finds_each_forbidden_line_of_a_sample_source`: the function finds the four forbidden lines of a sample text.
**Tests first, through a socket:** edit the 11 cases of section 5 and the three records of Task 1 to their values after the move.
**Steps:**
1. Write and edit the tests. Run them. The 14 edited cases fail with the values of today. The new tests fail on the import of `WsConnection`. The two guard tests of `server.py` fail: they name the import of `fault_policy` and each line of `_serve_subscription_frame` that uses it or writes a row.
2. `build_content` of the listener. A method of `stubs_ws.SUBSCRIBE_METHODS` registers `"0x" + secrets.token_hex(16)` with `connection.out_queue`, and adds the id to `connection.subscription_ids`. A method of `stubs_ws.UNSUBSCRIBE_METHODS` removes an id only when `connection.subscription_ids` holds it. Each other frame goes to `super().build_content(...)`. The tricky lines:
   - The reply is `{"jsonrpc": "2.0", "id": parsed.get("id"), "result": ...}`, in this key order. The id is `None` for a frame with no id: the chain gives 1 there. `serve` reads `response_id(body) or request_id(parsed)`, so the row keeps the id of the frame.
   - `request.connection` has the type `object`. Narrow it with `isinstance(connection, WsConnection)`, and raise `ValueError` if it is not one.
   - The lines that read `params` move as they are (`server.py:655-658`). That keeps the defect of choice 3, as its option A says.
   - Ask no chain for a subscribe method: the content keys of `responses` must not reach it.
3. `release` removes each id of the connection from the registry.
4. `_reader_loop`: make `connection = WsConnection(out_queue)` one time. Keep the JSON parse that drops a frame which is not JSON. Give each frame to `listener.serve(RawRequest(body=frame.payload, headers=lava, connection=connection))`. The `finally` calls `listener.release(connection)`.
5. To keep today's close for a `method` that is a list or an object (choice 2, A), add one line above that call: `if isinstance(body, dict) and isinstance(body.get("method"), (list, dict)): return`. Its comment names the recorded test.
6. Do not change `_perform_frame`. It closes for `no_body` before it reads `latency_ms`, so a per-method `down` with a latency still closes with no wait.
7. Delete `_serve_subscription_frame`, `_STATUS_LABEL`, the three imports `fault_policy`, `secrets` and `stubs_ws`, the attribute `subscriptions` of `_SimThreadingHTTPServer`, and the line `srv.subscriptions = ...` of `start`.
**Verify:** `tests/test_simulator_ws.py` gives `181 passed`. The whole suite: `2024 passed`. `grep -n "fault_policy\|log\.record_arrival\|log\.finalize\|log\.push" server.py` prints nothing: the success metric of ADR-001.
**Commit:** `refactor(ws): each JSON frame goes through Listener.serve, and a guard reads server.py`

### Task 7: The texts

**Files:** The documents of section 8, and the comments and docstrings of `tests/test_simulator_ws.py` that name code that is gone. No assertion changes.
**Verify:** `git grep -n "_serve_subscription_frame\|_WireSubscriptions\.emit\|_WsHandler\.do_GET\|subscribe code" -- tests provider_simulator server.py stubs_ws.py` prints nothing. `grep -n "handlers_ws" CLAUDE.md` prints nothing. The whole suite: `2024 passed`.
**Commit:** `docs: the WebSocket listener, and the texts that state the old behaviour of a subscribe frame`

### Task 8: Show that each new or changed test can fail, and run two times

**Files:** None in the repository. The reports go into the evidence folder of the design, next to the day's handoff file.
**Steps:** Break one place of one source file. Run the tests. Put the file back from a copy, and compare the checksum.

| Break | A test that must fail |
|---|---|
| `arrive` passes no lava header | `test_arrive_writes_one_row_in_flight_with_the_lava_headers_of_the_call` |
| `_run` calls `arrive` after the body read | `TestCancelDuringResponseRecordsArrival::test_rst_before_body_arrives_still_records_history` |
| `serve` asks the chain and not `build_content` | The two tests of Task 3, and each subscribe test |
| `serve` asks `build_content` above the fault check | The six cases of the second test of Task 3, and `test_a_fault_or_a_canned_body_on_a_subscribe_frame_registers_nothing` |
| In `serve`, `if targeted and mode == "down":` becomes `if False:` | The two tests of "`down` comes first", and the `down` record of Task 1 |
| In `serve`, `latency if waited else 0` becomes `latency` | The `hang` test of a subscribe frame, and the `hang` record of Task 1 |
| `decide_upgrade` writes no row; then: it gives a `down` row the method `ws_upgrade` | `test_what_the_listener_decides_for_an_upgrade`, and the row tests of `TestTheUpgradeRequest` |
| `emit` writes the row before `put_nowait`; then: it writes no row | The two full-queue tests; the push tests |
| `build_content` does not add the id to `connection.subscription_ids` | The unsubscribe test, the `release` test, and `test_a_close_removes_the_subscriptions_of_its_own_connection_only` |
| `build_content` removes an id with no check of the connection | `test_an_unsubscribe_from_another_connection_answers_false_and_removes_nothing` |
| `build_content` asks the chain for a subscribe method | `test_a_content_key_of_responses_is_not_read_for_a_subscribe_frame` |
| `JsonRpcWsListener.corrupt` leaves the reply of a subscribe method whole | The six cases of the corruption test |
| `JsonRpcWsListener.method_key` returns `None` for a subscribe method | `test_a_per_method_error_probability_reaches_a_subscribe_frame` |
| `JsonRpcWsListener.build_body_override` returns `None` for a subscribe method | The canned-body test, and the canned-body record of Task 1 |
| `_perform_frame` waits for `latency_ms` before it closes for `no_body` | The two tests of a per-method `down` with a latency |
| `_reader_loop` does not call `release` | `test_a_connection_that_ends_with_no_close_frame_loses_its_subscriptions` |
| The line that closes for a `method` that is a list or an object is deleted | `test_a_frame_whose_method_is_a_list_or_an_object_closes_the_connection` |
| `build_content` reads `params` only when it is a list | Two cases of the `params` record of Task 1 |
| The import of `fault_policy` comes back; then: one call of `provider.log.push` comes back | The first guard test; the second guard test |

**Verify:** Each break fails its test. Then run two times in a row: `python -m pytest tests/test_simulator_ws.py tests/test_ws_subscriptions.py tests/test_listener_jsonrpc.py tests/integration/test_server_writes_no_history_row.py -q -p no:cacheprovider`. The count is the same each time, with no failure.

### Task 9: The pull request

**Steps:** 1. The Linux run, before the push: `docker run --rm --user 0 -v "$PWD":/src:ro provider-simulator:local sh -c "cp -r /src /tmp/work && cd /tmp/work && pip install -q pytest==9.1.1 pytest-timeout==2.4.0 && python -m pytest tests/test_simulator_ws.py tests/test_ws_subscriptions.py tests/test_listener_jsonrpc.py tests/integration/test_server_writes_no_history_row.py -q -p no:cacheprovider"`. It is the command of pull request 144, with three more files. 2. Ask "push and open the PR?". Use the skill `pr`. 3. The title: `refactor(ws): a WebSocket frame goes through Listener.serve, and server.py writes no history row`. The command has `--label no-ticket`. 4. The body names each changed value of section 5 by its test, each choice that Victoria made, and each break of Task 8. It holds no ticket key and no path of one machine.
**Verify:** `gh pr checks <number>`: `lint` and `test` pass, and the log of `test` says `2024 passed`.

**Not in this plan.** The repair of the two malformed frames, which choices 2 and 3 offer. The 30 seconds after which the simulator closes a WebSocket that sends no frame. The mode `port_closed` for a `ws` port: that is the next pull request.

## 7. The gates before the merge

| Gate | It passes when | What it needs from Victoria |
|---|---|---|
| The worktree and the branch (Task 0) | They exist | Done: her go of 2026-10-10 |
| Two runs in a row of the new and changed tests | The same count, no failure | Done: she chose two runs, as for pull request 144 |
| One break for each new or changed test (Task 8) | Each break fails its test | Nothing |
| black, ruff and mypy in the versions of CI | Clean | Nothing |
| The whole suite on the Mac | `2024 passed` | Nothing |
| Linux, one time, before the push (Task 9) | The container run passes | Docker must run. Her go if it must be started. |
| The push and the pull request | The pull request is open | Her go, in one question |
| The checks of the pull request | `lint` and `test` pass | Nothing |
| A Copilot review | It is clean | Her go for the review |
| The skill `can-i-merge` | It is clean | Nothing |
| The automation suites with the branch build | Each test id has the same outcome as in the base | Her choice of the place and of the base (choice 8). Her go for each run and for the temporary branch. |
| The merge | — | Her own go |
| The paired pull request of the automation repository (section 8) | After the merge | Her go for its push |

Two automation tests reach the upgrade decision, by a read. `websocket/test_ws_subscriptions_recover_without_restart.py` sets `down` on the providers that have a `ws` endpoint (line 373). `websocket/test_ws_upgrade_rate_limit_holdoff.py` reads the row `ws_upgrade` with `rate_limit` (line 657). Section 11 of the design says that no automation test sets a corruption or a per-method override for a subscribe method. I did not repeat that search.

## 8. The documents that change

In the simulator repository:

1. `CLAUDE.md:45`, the paragraph `WsHandler (handlers_ws.py)`. It names a module and a registry that do not exist. The new text names `_WsHandler` of `server.py`, `JsonRpcWsListener`, `WsSubscriptions`, and who writes each row. Line 14 names `handlers_ws` too: correct it in the same edit.
2. The head of `server.py` (lines 19-22): the listener decides the upgrade and answers a subscribe frame. The adapter owns the socket, the frame bytes and the two threads.
3. The head of `provider_simulator/listeners/ws.py` (lines 1-15): the module holds the WebSocket listener, the connection object and the registry.
4. The head of `provider_simulator/listeners/base.py`: three hooks have a default (`build_down`, `corrupt`, `build_content`), and an adapter calls `arrive` before it reads the body (lines 36-38 and 47-50).
5. `stubs_ws.py:18`: "No other modules should depend on stubs_ws" becomes false. The WebSocket listener imports the tables.
6. `docs/using_the_simulator.md:158`: delete the two sentences that start "One case differs: a WebSocket subscribe frame." If the `hang` row keeps the latency (choice 1, B), the half about the `hang` row stays.
7. `docs/using_the_simulator.md:253`: delete "A WebSocket subscribe frame reads five of the keys only". Add that `http_status` has no effect on a WebSocket frame. Add two facts. A corruption reaches the reply of a subscribe frame, and `GET /ws/subscriptions` still shows the subscription. A canned `body` for a subscribe method answers that body and registers no subscription.
8. `README.md:269-270`: "a WS subscription registry" becomes "the WebSocket listener and its subscription registry".
9. `tests/test_simulator_ws.py`: the comment at 1402-1413, the class docstrings at 1624-1630, 1908-1912, 2558-2562 and 2755-2756, and the docstrings at 2453-2456 and 2837-2840. They name code that is gone, or they say "the subscribe code". The class name `TestFramesOutsideTheSubscribeCode` stays: a new name changes the id of 13 recorded cases. `tests/test_ws_subscriptions.py:1-2`.

Not in this plan: `docs/ARCHITECTURE_GUIDE.md`, `docs/CLASS_REFERENCE.md`, `docs/DATA_FLOWS.md` and `docs/QUICK_REFERENCE.md`. They name `handlers_ws` and `chain_family`, and they were stale before this design.

Outside the simulator repository, after the merge:

1. The automation repository, one paired pull request. `.claude/skills/add-simulator-entity/SKILL.md` says in two places that `JsonRpcListener` serves WebSocket: lines 43 and 661 at `045ab9e33f`. A search of 2026-10-10 found no skill sentence about a subscribe frame under `down`, a corruption, `error_probability` or a canned body.
2. The design, on the branch of pull request 137: one new row of section 9.2 for each choice that changes a row. If the `hang` row shows 0 (choice 1, A), that is one row.

## 9. Rollback

- Before the merge: each task is one commit. Undo a task with `git revert` of its commit. Do not reset the branch.
- After the merge: revert the merge commit of this pull request with a new pull request, while it is the newest merged one. The edits of the recorded tests go back with it, so the suite is green again. If the real down merged after it, revert that one first (section 10 of the design).
- Pull request 144 can stay: it holds tests only.
- If the move cannot be finished: WebSocket keeps its places in `server.py`. Three settings that a subscribe frame ignores today can then be repaired where the code is: the corruption, the per-method `error_probability` and the canned `body` (section 11 of the design).
- A revert needs Victoria's go, as each push does.

## 10. What the writer of this plan could not verify by a read

The task that reaches an item is its proof. An item that does not hold is a stop.

1. Each pytest count. `1977` is stated by the plan of pull request 144. Each later count is that number and the cases of my lists.
2. That the four records of Task 1 pass on today's code. I read the lines: `server.py:612`, `:631-646`, `:655-658`, `:398-401`, and `control_api.py:969-970`. The first run of Task 1 is the proof.
3. The bytes of the two `truncated` replies in section 5. I counted them by hand: 75 bytes less 10, and 43 bytes less 10.
4. That `serve` stops at `base.py:210` for a `method` that is a list. `dict.get` hashes its key, also for an empty dict. The ledger of pull request 144 says that the controller ran this as a break. The two cases of the recorded test failed.
5. That a plain `return` of the reader gives a caller the same bytes as today's error: no byte, then a close. I read the two `finally` blocks of `do_GET` and `_reader_loop`. On Linux a close can come as a reset, and the helper `_read_until_the_close` accepts both.
6. That `mypy .` is clean. `pyproject.toml` switches `attr-defined` off for the module `server`, so `listener.decide_upgrade` passes there. In `ws.py` the `isinstance` check narrows `request.connection`.
7. That `ruff check .` is clean after each task. The three imports `fault_policy`, `secrets` and `stubs_ws` lose their last use in Task 6, so they go there.
8. That `ws.py` can import `stubs_ws` and `provider_simulator.domain.registry` with no import cycle. I read the imports of both. The package already imports flat modules: `constants`, `stubs` and `stubs_rest`.
9. That no automation test depends on a changed value. I searched the skill pages and five WebSocket test files only. The gate of the automation suites is the proof.
10. The facts of choice 8 about the automation run, and that the image `provider-simulator:local` exists on the Mac.
11. That each break of Task 8 fails the tests that its row names. No break ran.

## 11. The sixteen points of the brief, checked against the code

| Point | Result | Where |
|---|---|---|
| 1. `serve` checks a provider-wide `down` before the parse; the subscribe code merges first | Holds. The `down` row of an unsubscribe frame changes too, and no recorded test holds it. | `base.py:184-189`; `server.py:616-633` |
| 2. A `hang` row: `serve` records 0, the subscribe code the latency | Holds | `base.py:222`, `:247`, `:273`; `server.py:640-646` |
| 3. A per-method `down` with a latency closes with no wait | Holds | `base.py:230-242`; `server.py:675-681` |
| 4. Seven keys against five | Holds. `http_status` reaches no frame byte. | `base.py:73-81`; `server.py:624`, `:373` |
| 5. A canned `body` reaches a subscribe method | Holds. It also answers under a provider-wide fault other than `down`: the flow reads it before the fault. | `jsonrpc.py:77-84`; `base.py:223-229` |
| 6. A corruption reaches the success reply | Holds. It reaches the reply of an unsubscribe frame too. | `base.py:262-263` |
| 7. The content comes from the registry; the id `null` | Holds. The eth chain gives the id 1 to a frame with no id. | `base.py:255`; `chains/eth.py:102` |
| 8. One connection owns a subscription | Holds | `server.py:557`, `:592-595`, `:1367` |
| 9. A fault registers nothing | Holds | `base.py:229-255` |
| 10. The row and the subscription exist before the wait | Holds | `base.py:269-276`; `server.py:680-681` |
| 11. A `method` that is a list or an object | Holds. With a filter that does not name the endpoint, the stop is in the chain. | `server.py:583`; `base.py:208-210`; `chains/eth.py:106`; `call_log.py:184-189`; `control_api.py:626-627` |
| 12. `params` of a wrong shape raises after the arrival row | Holds. An empty object, the number 0 and an empty first item do not raise. | `server.py:612`, `:655-658` |
| 13. The upgrade keeps its rules; the adapter answers 404 and 400 first | Holds | `server.py:438-460`, `:493-553` |
| 14. A pushed event: the queue first, then the row | Holds, but one detail differs: 22 call sites in 8 test files build `WsSubscriptions()` with no argument, and not three. | `server.py:398-416`; section 1, item 4 |
| 15. The arrival row before the body read | Holds | `server.py:134-138`; `tests/test_simulator.py:2820`, `:2862`, `:2887` |
| 16. The guard is a new test file below `tests/` | Holds. `tests/integration/` exists, with an `__init__.py`. | Choice 4 |
