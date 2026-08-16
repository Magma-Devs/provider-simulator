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
Not a simulator concept. The simulator never sees one. A name such as
`SimProvider1` is chosen by the router's values file, and the router reports it
in the `Lava-Provider-Address` response header.
_Avoid_: using a name to address a provider through the control API

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
exists — every pool, every provider, every endpoint.
_Avoid_: config, port map, provider list

**Registry**:
The live objects built from the topology when the server starts. It refuses a
table that would break at run time, such as one port used twice.
_Avoid_: container, factory, world

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
The gate that decides whether a content fault fires on an endpoint. `down` is
the one fault that ignores it and reaches every endpoint of a provider.
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
_Avoid_: level, priority, rank
