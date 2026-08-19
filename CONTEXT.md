# Provider Simulator

The provider simulator pretends to be the blockchain nodes that a smart router
talks to. Tests script its behaviour, then watch what the router does. This file
is the glossary for that work: one word, one meaning.

## Identity

**Pool**:
One router's own set of providers. Its name equals the router id, such as
`eth-sim` or `lava-sim-rest`.
_Avoid_: provider group, chain, router, cluster

**Provider**:
One named node entry in one router's config — the same unit the router scores
and blocks. It owns its own fault settings, quirks, call log and endpoints.
_Avoid_: node, upstream, backend, server, instance

**Pool slot**:
A provider's position inside its own pool. It starts at `1` in every pool. It is
the second half of the provider key, and it is the only number the control API
accepts.
_Avoid_: provider id, global id, provider number, index

**Provider key**:
The address of one provider, written `pool:slot` — for example `eth-sim:1`. Two
pools may both hold slot `1`; the pool name is what keeps them apart.
_Avoid_: provider address, provider name, pid string

**Provider name**:
What the router calls one provider. It lives in the topology table now, not
only in a values file. The router reports it in the `Lava-Provider-Address`
response header. It is not an address: the control API will not accept it.
Use the provider key for that.

The form is `<Pool><Role>Provider<slot>`, for example `EthPrimaryProvider1` and
`LavaRestBackupProvider4`. Three rules go with it:

- **The number is the pool slot**, so the number in the name is the same number
  the control API accepts. A test that reads a name from a header already knows
  the address to send a fault to.
- **No word is said twice.** Some pool names already contain their own role
  word. `eth-solo-sim` plus the role `Solo` would read `EthSoloSolo`, so the
  pool word is dropped and the name is `EthSoloProvider1`.
- **A name must be unique per chain and api-interface.** Two providers on one
  chain and interface cannot share a name or the router exits at startup. The
  chart lowercases every name first, so two names that differ only in case
  arrive identical and collide.

The chart lowercases the name and turns spaces into hyphens before the router
reads it, so `EthPrimaryProvider1` reaches a test as `ethprimaryprovider1`.
Compare lowercased, always.
_Avoid_: using a name to address a provider through the control API

**Role**:
The middle part of a provider's name, between the pool and the word `Provider`.
The full list: `Primary`, `Backup`, `Solo`, `DuoHigh`, `DuoLow`, `Best`,
`Priority`, `Precedence`.

It exists to tell a person what that provider is for, so it is chosen rather
than derived. That it cannot be worked out from the slot number is the point of
the field, not a gap in it.

**Every provider carries one, and an empty role is not allowed.** A pool with
nothing to tell its providers apart, such as `btc-sim`, uses `Primary`. A row
with no role is a row someone did not finish, so it is rejected rather than
quietly building a name with a hole in it.

The role is the middle part of the name. It is not a column of its own. It is
visible inside the `name` column of the topology table. In `EthBackupProvider4`,
the role is Backup, but nothing stores that word separately.
_Avoid_: type, kind, label, category

**Group**:
The cross-validation group a provider was put in, held in the `group_label`
column of the topology table. Cross-validation is the router asking several
providers the same question and only answering when enough of them agree. A
router policy can also demand that the agreeing answers come from different
groups, so a test that checks group diversity has to know which provider sits
in which group.

The deployment chooses it, not the simulator. The router's values file gives
each node a `group_label` and the table copies it. No simulator behaviour
depends on it: the only code here that reads it serves it on `GET /providers`,
so a caller can ask instead of writing it down.

The label is written `voting-group-<n>`. Cross-validation is a vote: providers
answer, matching answers are counted, and a threshold wins. Providers sharing a
label are one bloc that would cast the same wrong vote together, because they
share a source, so the router only counts agreement that spans different blocs.
The number identifies the bloc and means nothing else. A cross-validation policy
never names a label. It only counts groups, through `min_groups` and
`per_group_quorum` in the router's values file. One test does write the labels
out, `tests/test_control_api_providers.py`, and it does that to check that
`GET /providers` serves the table as written.

Only two pools carry a label today. `eth-sim` puts its first two primaries in
one bloc and its third in another. `eth-cv-sim` puts its six providers in three
blocs of two. Every other row carries the empty string, which means the
deployment put that provider in no group at all. It is an empty string and not
a missing value, so a caller grouping by label gets one bucket of unlabelled
providers instead of a key that is not there.
_Avoid_: tier, class, provider set

**Endpoint**:
One door a provider listens on: an interface, the transport that carries it, and
a TCP port. A JSON-RPC provider has two, one for http and one for ws.
_Avoid_: port, listener, socket, URL

**Interface**:
The application protocol an endpoint speaks: `jsonrpc`, `rest`, `grpc` or
`tendermintrpc`.
_Avoid_: protocol, API type, surface

**Transport**:
What carries the interface: `http`, `ws` or `http2`.
_Avoid_: scheme, connection type

**Chain**:
The blockchain a pool imitates: `eth`, `btc`, `ln`, `solana` or `lava`. One pool
serves exactly one chain.
_Avoid_: network, chain id, chain family

## The table and the live objects

**Topology**:
The table in `provider_simulator/topology.py`. It is the one place that says what
exists — every pool, every provider, every endpoint. Each row also carries the
provider's `name`, its `is_backup` flag and its `group_label`. That makes seven
fields per row. Four of them, pool, chain, pid and endpoints, decide what the
simulator runs. The other three, `name`, `is_backup` and `group_label`, are only
recorded. A caller reads them instead of guessing.
_Avoid_: config, port map, provider list

**Registry**:
The live objects built from the topology when the server starts. It refuses a
table that would break at run time, such as one port used twice.
_Avoid_: container, factory, world

**Provider record**:
Everything the simulator knows about one provider, served by `GET /providers`
and keyed by the provider key: pool, pool slot, chain, name, backup flag, group,
and every endpoint it listens on. Four filters narrow the set, `pool`, `pid`,
`name` and `is_backup`. The `name` filter ignores case, because the chart
lowercases every name before the router sees it. An unknown pool is a 400 and
never an empty set.

It exists so a test can ask instead of writing the answer down. A name written
into test code can be wrong, and a wrong name raises nothing: the router reports
the name the deployment gave the provider, the test looks for the name it wrote
down, and the two never meet. The test then compares two empty sets and passes
green while checking nothing.
_Avoid_: provider info, provider details, the /topology reply

## Faults

**Scenario**:
The fault settings that every provider understands, whatever its chain: outage,
latency, errors, rate limits, corruption, dropped connections, and the
fail-first-N sequence.
_Avoid_: config, state, fault config

**Quirks**:
The extra settings only some chains need, kept out of the scenario. Ethereum
models a lagging log index; Solana models the slot gap.
_Avoid_: chain config, extras, options

**Mode**:
The one fault shape a provider is in. Exactly one applies at a time: `success`,
`error`, `rate_limit`, `down`, `drop_connection` or `hang`.
_Avoid_: state, status, behaviour

**Orthogonal field**:
A setting that composes with whichever mode is active, because it changes the
content or the timing rather than the shape: `latency_ms`, `responses`,
`blocks_behind`, `corruption_mode`, `http_status`.
_Avoid_: modifier, option, extra

**Chain family**:
A retired word. It used to be a field on a scenario, and it decided whether a
content fault fired on an endpoint. The control API no longer takes it: a
scenario block carrying `chain_family` gets a 400 that points at the pool and
the `transports` filter instead.

Two things replaced it. Which chain's handler serves a call is decided by the
port the call arrived on, so the pool a provider sits in already says which
chain it speaks. Which of a provider's endpoints a fault reaches is decided by
the scenario's `transports` list, and `down` obeys that list like every other
mode. With no list set, `down` still reaches every endpoint the provider has.
_Avoid_: chain, chain type

## Telemetry

**Call log**:
One provider's record of the calls it served — a ring buffer of recent calls plus
counters that never reset.
_Avoid_: log, telemetry, journal

**History**:
The recent calls from the ring buffer. Read it per request, filtered by request
id.
_Avoid_: log, trace, calls

**Stats**:
The counters that never reset, covering the whole session. The router's own
background block-height probes land here too.
_Avoid_: metrics, counts, totals

## Router-side words that appear here

**Tier**:
Primary or backup. A router decides which providers it tries first. The simulator
treats every pool the same way, so a tier is never a simulator behaviour.
`Primary` and `Backup` are two of the values a Role can take.
_Avoid_: level, priority, rank
