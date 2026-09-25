"""Two providers asked about one block height must report the same hash.

This guard is not fixing a defect. It is stopping one from being introduced
quietly, because the failure it prevents is silent.

The router watches for forks. Two providers reporting different hashes at one
height is how a real chain announces it has split, and the router changes what
it does when it sees that. If OUR simulator ever let two providers disagree,
the router would react to the test harness instead of to the behaviour under
test. Nothing would turn red. The test would pass, having measured something
else, and nobody would look.

Today the property holds for a structural reason rather than a decided one.
Chain objects are process singletons (``provider_simulator/chains/__init__.py``)
and the only per-provider inputs that reach ``build_success`` are the scenario
and quirks snapshots — a provider's NAME never arrives, so nothing can salt a
hash with it. That is not a guarantee. ``slot_offset`` already travels in
quirks and is per-provider, so a hash salted from a quirk would compile, ship
and read plausibly.

What is asserted, and what is deliberately not:

- Asserted: a HEIGHT-ADDRESSED request — one naming an absolute height, or a
  block hash — returns the same hashes to two providers sitting at different
  heads. Being behind must not change what a provider says about a block it
  has already reached.
- NOT asserted: that a HEAD-addressed reply agrees. ``getbestblockhash`` names
  no height, so two providers at different heads SHOULD answer differently and
  btc correctly does. Asserting agreement there would assert a bug.
- NOT asserted: that two different heights give two different hashes. Eth
  returns one constant hash for every height today. That is a separate
  question from forks and is tracked on its own.

Adding a chain? ``PER_HEIGHT_HASH_PROBES`` must gain an entry, even if that
entry is an empty tuple. A missing key fails
``test_every_known_chain_declares_whether_it_reports_a_hash_per_height``
rather than passing in silence.
"""

import pytest

from provider_simulator.chains import CHAINS, chain_for
from provider_simulator.chains.base import Chain
from provider_simulator.domain.quirks import known_chains, quirks_for
from provider_simulator.domain.scenario import ScenarioConfig
from stubs_btc import btc_block_hash

#: One height both providers have reached. Far below every chain's base head,
#: so a provider lagged by ``LAG`` blocks has still seen it.
HEIGHT = 700_000

#: How far the second provider is behind the first. Any non-zero number works;
#: this one is large enough that a hash quietly computed from the head instead
#: of from the requested height cannot land on the same value by luck.
LAG = 250


def _scenario(**kw) -> dict:
    sc = ScenarioConfig()
    if kw:
        sc.update(kw)
    return sc.snapshot()


def _quirks(chain: str, **kw) -> dict:
    q = quirks_for(chain)()
    if kw:
        q.update(kw)
    return q.snapshot()


# ── The population ────────────────────────────────────────────────────────────
# Per chain: the requests that name an absolute height (or a block hash, which
# names one just as absolutely) and come back carrying a hash.
#
# A request is (interface, method, params, result_is_itself_a_hash). The last
# flag exists because btc's ``getblockhash`` answers with a BARE STRING rather
# than an object with a ``hash`` key, so a walk keyed on field names would step
# straight over the one reply that is nothing but a hash.
PER_HEIGHT_HASH_PROBES: dict[str, tuple[tuple[str, str, object, bool], ...]] = {
    "btc": (
        ("", "getblockhash", [HEIGHT], True),
        ("", "getblock", [btc_block_hash(HEIGHT)], False),
        ("", "getblockheader", [btc_block_hash(HEIGHT)], False),
    ),
    "eth": (
        ("", "eth_getBlockByNumber", [hex(HEIGHT), False], False),
        ("", "eth_getBlockByHash", ["0x" + f"{HEIGHT:064x}", False], False),
    ),
    "lava": (("tendermintrpc", "block", {"height": str(HEIGHT)}, False),),
    # Solana has no per-height hash to ask for. Its one hash-bearing method,
    # ``getLatestBlockhash``, names the head rather than a height, and answers
    # a fixed constant (stubs_solana.SOLANA_BLOCKHASH). It is checked instead
    # by test_a_chain_whose_only_hash_is_a_constant_still_agrees_across_providers.
    "solana": (),
    # LN reports the height of the bitcoin chain beneath it and no block hash
    # at all — ``getinfo`` carries an identity pubkey, which is not one.
    "ln": (),
}

#: How two providers of this chain are made to sit at different heads. Each
#: chain's real knob: most lag through the scenario, solana through a quirk.
SECOND_PROVIDER_IS_BEHIND: dict[str, tuple[dict, dict]] = {
    "btc": ({}, {"blocks_behind": LAG}),
    "eth": ({}, {"blocks_behind": LAG}),
    "lava": ({}, {"blocks_behind": LAG}),
    "ln": ({}, {"blocks_behind": LAG}),
    "solana": ({}, {}),  # solana lags through quirks, below
}

SECOND_PROVIDER_QUIRKS: dict[str, dict] = {
    "solana": {"slot_offset": -LAG},
}

#: A request whose whole answer MUST differ between the two providers.
#:
#: Without this the guard has a hole big enough to walk through. A
#: height-addressed reply is identical for both providers by design — that is
#: the property being checked — so it would look exactly the same if the lag
#: never reached the chain at all, or if both "providers" were one provider
#: asked twice. Then two identical replies would agree, the guard would go
#: green, and it would have compared a provider with itself.
#:
#: These are head-addressed on purpose: naming no height, they are the replies
#: that SHOULD move when a provider falls behind.
LAG_IS_VISIBLE_AT: dict[str, tuple[str, str, object]] = {
    "btc": ("", "getblockcount", []),
    "eth": ("", "eth_blockNumber", []),
    "lava": ("tendermintrpc", "status", {}),
}


def hashes_in(result: object, result_is_itself_a_hash: bool) -> dict[str, str]:
    """Every block hash in one reply, as ``{where it sat: the hash}``.

    The rule is the field's NAME: it counts when the name ends in ``hash``,
    in any case. Detecting by the shape of the VALUE was tried first and was
    worse in both directions — it claimed LN's ``identity_pubkey`` as a block
    hash, and it would have read a hash written in some new shape as prose.

    Two consequences worth knowing before reading the counts, because the rule
    is blunt on purpose and each chain names things its own way:

    - Tendermint's header roots (``data_hash``, ``validators_hash``,
      ``app_hash`` …) end in ``hash``, so lava contributes 14 fields where btc
      contributes 4. Eth's equivalents are named ``stateRoot`` and
      ``transactionsRoot``, so they do not count.
    - That asymmetry is safe in the direction it errs. Every extra field is
      still something two providers must agree on for one height, so
      over-including makes the guard stricter, never blinder.
    """
    if result_is_itself_a_hash:
        return {"<result>": result} if isinstance(result, str) else {}

    found: dict[str, str] = {}

    def walk(value: object, path: str) -> None:
        if isinstance(value, dict):
            for key, sub in value.items():
                here = f"{path}.{key}" if path else str(key)
                if isinstance(sub, str) and str(key).lower().endswith("hash"):
                    found[here] = sub
                else:
                    walk(sub, here)
        elif isinstance(value, list):
            for i, sub in enumerate(value):
                walk(sub, f"{path}[{i}]")

    walk(result, "")
    return found


def ask(chain: Chain, chain_name: str, probe: tuple, scenario_kw: dict, quirks_kw: dict) -> dict[str, str]:
    """Put one probe to one provider and return the hashes it answered with."""
    interface, method, params, result_is_hash = probe
    request = {"id": 1, "method": method, "params": params}
    _status, body = chain.build_success(request, _scenario(**scenario_kw), _quirks(chain_name, **quirks_kw), interface)
    return hashes_in(body.get("result"), result_is_hash)


def disagreements(leader: dict[str, str], laggard: dict[str, str]) -> dict[str, tuple[str, str]]:
    """Where the two providers gave different answers, as ``{field: (a, b)}``.

    The guard and the control that proves the guard can fail both call this,
    so the control exercises the comparison that actually ships rather than a
    copy of it written beside it. A field missing from one reply counts as a
    disagreement, not as a match.
    """
    return {
        field: (leader.get(field, "<absent>"), laggard.get(field, "<absent>"))
        for field in sorted(set(leader) | set(laggard))
        if leader.get(field) != laggard.get(field)
    }


# ── The guard ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("chain_name", sorted(k for k, v in PER_HEIGHT_HASH_PROBES.items() if v))
def test_two_providers_at_different_heads_agree_on_one_heights_hash(chain_name: str) -> None:
    """The whole point of the file.

    One provider at the head, one lagged by ``LAG`` blocks, both asked about
    the SAME absolute height. Every hash in the two replies must match. A
    difference here is what the router reads as a fork.
    """
    chain = chain_for(chain_name)
    at_head, behind = SECOND_PROVIDER_IS_BEHIND[chain_name]
    behind_quirks = SECOND_PROVIDER_QUIRKS.get(chain_name, {})

    # First prove these really are two providers in different places. If this
    # fails, everything below compares one provider with itself and proves
    # nothing, however green it looks.
    interface, method, params = LAG_IS_VISIBLE_AT[chain_name]
    request = {"id": 1, "method": method, "params": params}
    tip_at_head = chain.build_success(request, _scenario(**at_head), _quirks(chain_name), interface)[1]["result"]
    tip_behind = chain.build_success(request, _scenario(**behind), _quirks(chain_name, **behind_quirks), interface)[1][
        "result"
    ]
    assert tip_at_head != tip_behind, (
        f"{chain_name}: the two providers answered {method} identically "
        f"({tip_at_head!r}), so they are at the same head and the hash comparison "
        f"below would be a provider compared with itself. The lag is not reaching "
        f"this chain."
    )

    compared = 0
    for probe in PER_HEIGHT_HASH_PROBES[chain_name]:
        leader = ask(chain, chain_name, probe, at_head, {})
        laggard = ask(chain, chain_name, probe, behind, behind_quirks)
        assert leader, (
            f"{chain_name} {probe[1]} returned no hash at all, so this probe compared "
            f"nothing. Either the method or the field was renamed — fix the probe, "
            f"do not delete it."
        )
        split = disagreements(leader, laggard)
        assert not split, (
            f"{chain_name} {probe[1]} at height {HEIGHT}: the provider at the head and "
            f"the provider {LAG} blocks behind disagree about the same block.\n"
            + "".join(f"  {field}: {a} != {b}\n" for field, (a, b) in split.items())
            + "The router reads a disagreement like this as a chain fork."
        )
        compared += len(leader)

    assert compared, f"{chain_name} compared zero hash fields"


def test_a_chain_whose_only_hash_is_a_constant_still_agrees_across_providers() -> None:
    """Solana, which cannot be asked for a height.

    Its one hash-bearing method names the head, not a block, so it gets its own
    test rather than a pretend entry in the height table. Two providers at
    different slots must still quote the same blockhash.
    """
    chain = chain_for("solana")
    request = {"id": 1, "method": "getLatestBlockhash", "params": []}
    leader = chain.build_success(request, _scenario(), _quirks("solana"), "")[1]["result"]
    laggard = chain.build_success(request, _scenario(), _quirks("solana", slot_offset=-LAG), "")[1]["result"]

    assert leader["context"]["slot"] != laggard["context"]["slot"], (
        "the two providers were meant to be at different slots; if they are not, "
        "this test is comparing one provider with itself"
    )
    assert leader["value"]["blockhash"] == laggard["value"]["blockhash"]


# ── Guards on the guard ───────────────────────────────────────────────────────
def test_every_known_chain_declares_whether_it_reports_a_hash_per_height() -> None:
    """A chain added without an entry here would be checked by nothing.

    The entry is allowed to be empty — that is a chain saying it reports no
    per-height hash, which is a real answer. What is not allowed is the key
    being absent, because that reads exactly like a chain with nothing to
    check and is the silence this file exists to end.
    """
    assert set(PER_HEIGHT_HASH_PROBES) == set(known_chains())
    assert set(SECOND_PROVIDER_IS_BEHIND) == set(known_chains())
    assert set(CHAINS) == set(known_chains())
    # Every chain that IS checked must also say where its lag shows, or the
    # check has no way to tell two providers apart.
    has_probes = {k for k, v in PER_HEIGHT_HASH_PROBES.items() if v}
    assert has_probes <= set(LAG_IS_VISIBLE_AT), sorted(has_probes - set(LAG_IS_VISIBLE_AT))
    # And every checked chain must be in the control's table too. Without this a
    # chain added later with real probes gets silent zero coverage from the
    # control — which is this file's own failure mode, reintroduced.
    assert has_probes == set(DISAGREEMENTS_THE_CONTROL_MUST_SEE), sorted(
        has_probes.symmetric_difference(DISAGREEMENTS_THE_CONTROL_MUST_SEE)
    )


def test_the_probes_actually_find_hashes() -> None:
    """Proof the comparison had something to compare.

    A probe whose method was renamed, or whose hash field moved, returns an
    empty dict. Two empty dicts are equal, so the guard above would pass
    having read nothing. This counts what the probes find and requires the
    number to be the one recorded below.
    """
    per_chain = {}
    for chain_name, probes in PER_HEIGHT_HASH_PROBES.items():
        found = 0
        for probe in probes:
            found += len(ask(chain_for(chain_name), chain_name, probe, {}, {}))
        per_chain[chain_name] = found

    # Recorded, not derived: deriving these from the same call they check would
    # compare the probes with themselves. btc 1 + 2 + 1; eth 2 + 2; lava 14,
    # which is every hash in a Tendermint block header (see ``hashes_in`` for
    # why that chain contributes so many more than the others).
    assert per_chain == {"btc": 4, "eth": 4, "lava": 14, "solana": 0, "ln": 0}, per_chain
    assert sum(per_chain.values()) == 22


def test_the_guard_notices_a_hash_that_changes_with_the_provider() -> None:
    """The control. A guard only ever seen passing is not a guard.

    This builds the exact defect the file exists to catch — a chain whose hash
    is salted with a per-provider value — and requires the comparison to go
    red against it. The salt is read from quirks because that is the reachable
    route: quirks are per-provider and already carry ``slot_offset``, so this
    is the shape the mistake would really take.

    Note what this control is NOT. It is not a chain that went missing, and
    not a method that stopped existing. Either of those would prove the guard
    notices the neighbourhood rather than the thing.
    """

    class ForkedChain(Chain):
        name = "forked"

        def build_success(self, request, scenario, quirks, interface=""):
            salt = quirks.get("slot_offset", 0)
            return 200, {"result": {"hash": f"{HEIGHT + salt:064x}"}}

    forked = ForkedChain()
    probe = ("", "anything", [HEIGHT], False)
    leader = ask(forked, "solana", probe, {}, {})
    laggard = ask(forked, "solana", probe, {}, {"slot_offset": -LAG})

    # The control has to carry a readable hash before it can prove anything
    # about one that disagrees.
    assert leader == {"hash": f"{HEIGHT:064x}"}

    # The comparison the guard itself runs, on the salted pair. It must name
    # the field and both values, which is what makes a real failure diagnosable.
    split = disagreements(leader, laggard)
    assert split == {"hash": (f"{HEIGHT:064x}", f"{HEIGHT - LAG:064x}")}

    # And the same pair put through an unsalted chain must come back clean, so
    # this proves the comparison discriminates rather than always reporting a
    # difference.
    unsalted = ask(chain_for("btc"), "btc", ("", "getblockhash", [HEIGHT], True), {}, {})
    assert disagreements(unsalted, unsalted) == {}


#: How many hash fields the control below must see disagree, per chain. The same
#: numbers ``test_the_probes_actually_find_hashes`` records, for the same reason:
#: derived from the call they check, they would compare the walk with itself.
DISAGREEMENTS_THE_CONTROL_MUST_SEE = {"btc": 4, "eth": 4, "lava": 14}


@pytest.mark.parametrize("chain_name", sorted(DISAGREEMENTS_THE_CONTROL_MUST_SEE))
def test_the_guard_names_every_hash_field_in_a_real_chains_reply_when_one_differs(
    chain_name: str,
) -> None:
    """The control above, run against each real chain instead of one synthetic one.

    The control beside it proves the comparison works on a reply with a single
    top-level ``hash`` key. That is not the shape any real chain answers with.
    Lava's fourteen fields sit nested — ``block_id.parts.hash``,
    ``last_commit.block_id.hash`` — and btc's ``getblockhash`` is a bare string
    with no key at all. A walk that reached the top level and stopped would pass
    the synthetic control and find nothing here.

    So this asks each real chain its real probes, salts every hash in the reply,
    and requires the comparison to name all of them. It is the difference
    between "the comparison can report a difference" and "the comparison would
    report a difference in the replies this chain actually sends".

    What makes this trustworthy is that ``hashes_in`` runs on BOTH sides and the
    two results are required to name the same fields. Nothing else. ``_salted``
    below restates the field rule for readability, and that restatement is NOT a
    second opinion that would expose drift: ``hashes_in`` filters by key name
    downstream, so deleting the rule from ``_salted`` changes no result here.
    Verified by deleting it — 11 passed either way. Said plainly because the
    first version of this test claimed the independence it does not have.
    """
    chain = chain_for(chain_name)
    at_head, _behind = SECOND_PROVIDER_IS_BEHIND[chain_name]

    named = 0
    for probe in PER_HEIGHT_HASH_PROBES[chain_name]:
        interface, method, params, result_is_hash = probe
        request = {"id": 1, "method": method, "params": params}
        _status, body = chain.build_success(request, _scenario(**at_head), _quirks(chain_name), interface)
        result = body.get("result")

        leader = hashes_in(result, result_is_hash)
        forked = hashes_in(_salted(result, bare_hash=result_is_hash), result_is_hash)

        assert leader, f"{chain_name} {method} returned no hash, so this probe salted nothing"
        split = disagreements(leader, forked)
        assert set(split) == set(leader), (
            f"{chain_name} {method}: the comparison named {sorted(split)} but the reply "
            f"carries {sorted(leader)}. A hash the walk finds must be one a disagreement "
            f"reports, or a fork in that field would pass unseen."
        )
        named += len(split)

    assert named == DISAGREEMENTS_THE_CONTROL_MUST_SEE[chain_name], (
        f"{chain_name}: the comparison named {named} differing hash fields, expected "
        f"{DISAGREEMENTS_THE_CONTROL_MUST_SEE[chain_name]}. If a probe or a reply shape "
        f"changed, fix the probe and the recorded number together."
    )


def _changed(text: str) -> str:
    """One string, definitely different. Raises rather than returning the input.

    A salt that returns what it was given is the worst outcome here: the two
    sides then compare equal, the comparison reports agreement, and the control
    passes having tested nothing. Replacing the first character with ``f`` does
    that whenever the value already starts with ``f``, and every value is hex or
    base64, so it is a question of which constant somebody writes next rather
    than of whether it can happen.

    So the character is chosen against the value, and the result is checked.
    """
    swapped = ("e" if text[:1] == "f" else "f") + text[1:] if text else "salt"
    if swapped == text:
        raise AssertionError(f"the salt did not change {text!r}, so nothing would be compared")
    return swapped


def _salted(value: object, *, bare_hash: bool = False) -> object:
    """A copy of one reply with every block hash changed, and nothing else.

    The rule is ``hashes_in``'s rule restated: a field whose name ends in
    ``hash``, in any case. The restatement buys readability, not a second
    opinion — ``hashes_in`` filters by the same rule downstream, so no test here
    can tell whether this one is applied. Do not add a comment claiming it can.

    ``bare_hash`` is for btc's ``getblockhash``, whose whole reply is a hash with
    no key to read. The caller names that case because it is the only place that
    knows it. The first version of this helper instead salted every string it
    met, which made the docstring above false — ``merkleroot`` and ``chain`` were
    changed too — and would silently corrupt unrelated fields if anything reused
    it on a full response body.
    """
    if bare_hash:
        return _changed(value) if isinstance(value, str) else value
    if isinstance(value, dict):
        out: dict = {}
        for key, sub in value.items():
            if isinstance(sub, str) and str(key).lower().endswith("hash"):
                out[key] = _changed(sub)
            else:
                out[key] = _salted(sub)
        return out
    if isinstance(value, list):
        return [_salted(sub) for sub in value]
    return value


def test_a_reply_carrying_no_hash_is_not_mistaken_for_agreement() -> None:
    """The other way a green tick lies: nothing was there to compare.

    ``hashes_in`` must come back empty for a reply with no hash, so the guard's
    own emptiness check fires instead of two empty dicts comparing equal.
    """
    assert hashes_in({"block_height": 1, "identity_pubkey": "02ab" * 16}, False) == {}
    assert hashes_in(None, True) == {}
    assert hashes_in({"merkleroot": "ab" * 32, "stateRoot": "0x" + "cd" * 32}, False) == {}
