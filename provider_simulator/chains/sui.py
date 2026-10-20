"""Sui chain — the success-path reply of a simulated Sui GraphQL provider.

The reply of a GraphQL request is built one root field at a time, and the shape
of the reply follows the selection set of the request: only the fields that the
caller asked for, under the aliases that it gave (``graphql_document.project``).
Six root fields have a stub: ``chainIdentifier``, ``serviceConfig`` (the head poll
of the router), ``checkpoint`` (the block lookup), ``epoch``, ``transaction`` and
``executeTransaction``. Any other root field answers null.

A per-root entry of the ``responses`` map of the scenario, keyed by the root
field name, changes one root: ``result`` replaces the stub, and ``error_stub``
(the name of a Sui error) or ``error`` (an error entry) fails it. A failed root is
null with its error beside it, so the reply is partial when the request had
other roots.

Checkpoints. The head starts at ``SUI_BASE_CHECKPOINT`` and a test moves it with
``POST /advance``. A provider that is ``blocks_behind`` blocks late reports the
head minus that number as its tip, and a checkpoint above the tip does not exist
for it. The digest of a checkpoint derives from its sequence number and from the
salt of the provider and from nothing else, so two providers at different tips
agree on a checkpoint that both have (the rule of ``chains/base.py``).

Transactions. ``executeTransaction`` answers the real digest of the bytes that it
received, and it reports the digest in ``request["executed_now"]``. The listener
remembers it on the provider. With ``transaction_index_lag_ms`` above 0, the
lookup of a transaction finds it only after the provider ran it and the delay
passed, as the GraphQL index of a real server does.
"""

import base64
import hashlib
import re
import time

from provider_simulator.chains.base import AdvancingHead, Chain
from provider_simulator.chains.sui_digest import base58_encode, transaction_digest
from provider_simulator.domain.quirks import SuiQuirks
from provider_simulator.graphql_document import GraphQLRequest, RootField, project
from stubs_sui_graphql import SUI_BASE_CHECKPOINT, SUI_EPOCH_ID, SUI_ERROR_STUBS, SUI_MAX_CHECKPOINT

# The digest of a checkpoint is not the digest that a real checkpoint has. The
# prefix keeps the simulated digests apart from real ones, so that no test can
# take a simulated digest for a measurement.
_CHECKPOINT_PREFIX = b"SimCheckpoint::"
_WHOLE_NUMBER = re.compile(r"-?[0-9]+")


def _as_height(value: object) -> int | None:
    """A checkpoint number from an argument: a whole number, or a text of digits.
    Anything else is not a number, and the checkpoint does not exist."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _WHOLE_NUMBER.fullmatch(value.strip()):
        try:
            return int(value.strip())
        except ValueError:  # Python refuses to read an integer of more than 4300 digits
            return None
    return None


class SuiChain(Chain):
    name = "sui"
    quirks_type = SuiQuirks

    def __init__(self) -> None:
        self.head = AdvancingHead(SUI_BASE_CHECKPOINT)

    def error_stub(self, name: str) -> dict:
        """The error entry of a Sui error code. The name is the code in small letters
        (the key of ``SUI_ERROR_STUBS``) or in capitals, as ``error_code`` takes it."""
        try:
            return SUI_ERROR_STUBS[str(name).lower()]
        except KeyError:
            raise KeyError(f"unknown error_stub {name!r}; the Sui codes are {sorted(SUI_ERROR_STUBS)}") from None

    def tip(self, scenario: dict) -> int:
        """The latest checkpoint of one provider: the head, minus ``blocks_behind``
        (which is signed), never below 0 and never above ``SUI_MAX_CHECKPOINT``."""
        return min(SUI_MAX_CHECKPOINT, max(0, self.head.current() - scenario.get("blocks_behind", 0)))

    @staticmethod
    def checkpoint_digest(sequence_number: int, salt: str = "") -> str:
        raw = hashlib.blake2b(
            _CHECKPOINT_PREFIX + salt.encode() + sequence_number.to_bytes(8, "big"), digest_size=32
        ).digest()
        return base58_encode(raw)

    def build_success(
        self, request: dict, scenario: dict, quirks: dict, interface: str = "graphql"
    ) -> tuple[int, dict]:
        graphql: GraphQLRequest = request["graphql"]
        responses = scenario.get("responses") or {}
        data: dict = {}
        errors: list = []
        for root in graphql.roots:
            value, error = self._answer(root, responses.get(root.name) or {}, graphql, request, scenario, quirks)
            data[root.response_key] = value
            if error is not None:
                errors.append({**error, "path": [root.response_key]})
        body: dict = {"data": data}
        if errors:
            body["errors"] = errors
        return 200, body

    def _answer(
        self,
        root: RootField,
        entry: dict,
        graphql: GraphQLRequest,
        request: dict,
        scenario: dict,
        quirks: dict,
    ) -> tuple[object, dict | None]:
        """The value of one root field and the error beside it, if the root failed."""
        if "error_stub" in entry:
            return None, self.error_stub(entry["error_stub"])
        if "error" in entry:
            return None, entry["error"]
        if "result" in entry:
            return project(entry["result"], root.selection, graphql), None
        if root.name == "executeTransaction":
            return self._execute(root, graphql, request)
        return project(self._stub(root, graphql, request, scenario, quirks), root.selection, graphql), None

    def _stub(self, root: RootField, graphql: GraphQLRequest, request: dict, scenario: dict, quirks: dict) -> object:
        if root.name == "chainIdentifier":
            return quirks["chain_id"]
        if root.name == "serviceConfig":
            return {
                "availableRange": {
                    "first": {"sequenceNumber": 0},
                    "last": {"sequenceNumber": self.tip(scenario)},
                }
            }
        if root.name == "checkpoint":
            return self._checkpoint(root, scenario, quirks)
        if root.name == "epoch":
            return {"epochId": SUI_EPOCH_ID, "referenceGasPrice": "1000"}
        if root.name == "transaction":
            return self._transaction(root, request, scenario, quirks)
        if root.name == "__typename":
            return "Mutation" if graphql.operation == "mutation" else "Query"
        return None

    def _checkpoint(self, root: RootField, scenario: dict, quirks: dict) -> dict | None:
        tip = self.tip(scenario)
        requested = root.arguments.get("sequenceNumber")
        if requested is None:
            number = tip  # no number: the latest checkpoint
        else:
            found = _as_height(requested)
            if found is None or not 0 <= found <= tip:
                return None
            number = found
        salt = quirks["digest_salt"]
        return {
            "sequenceNumber": number,
            "digest": self.checkpoint_digest(number, salt),
            "previousCheckpointDigest": self.checkpoint_digest(number - 1, salt) if number > 0 else None,
            "epoch": {"epochId": SUI_EPOCH_ID},
        }

    def _transaction(self, root: RootField, request: dict, scenario: dict, quirks: dict) -> dict | None:
        digest = root.arguments.get("digest")
        if not isinstance(digest, str):
            return None
        lag_ms = quirks["transaction_index_lag_ms"]
        if lag_ms > 0:
            ran_at = (request.get("executed_at") or {}).get(digest)
            now = request.get("now", time.time())
            if ran_at is None or now - ran_at < lag_ms / 1000:
                return None
        return {
            "digest": digest,
            "effects": {"status": "SUCCESS", "checkpoint": {"sequenceNumber": self.tip(scenario)}},
        }

    def _execute(self, root: RootField, graphql: GraphQLRequest, request: dict) -> tuple[object, dict | None]:
        raw = root.arguments.get("transactionDataBcs")
        try:
            if not isinstance(raw, str):
                raise ValueError("transactionDataBcs is not a text")
            data = base64.b64decode(raw, validate=True)
        except ValueError:  # binascii.Error, the error of a text that is not base64, is a ValueError
            return None, self.error_stub("bad_user_input")
        digest = transaction_digest(data)
        request.setdefault("executed_now", []).append(digest)
        effects = {"effects": {"status": "SUCCESS", "digest": digest, "transaction": {"digest": digest}}}
        return project(effects, root.selection, graphql), None
