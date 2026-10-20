"""stubs_sui_graphql.py — constants and error replies of the simulated Sui GraphQL provider.

Companion to ``stubs_btc.py``. The reply of each root field is built by
``provider_simulator/chains/sui.py``; this file holds what that chain needs
beside the code: the chain ids, the first checkpoint, the epoch, and the error
replies that a test can ask for by name.

The chain id and the epoch are what the public Sui server answered when they
were measured. The default chain id is the id of Sui mainnet, because the
routers of the simulator imitate mainnet. A test sets another id through the
``chain_id`` setting of the provider.
"""

from provider_simulator.graphql_document import graphql_error

SUI_CHAIN_ID = "4btiuiMPvEENsttpZC7CZ53DruC3MAgfznDbASZ7DR6S"
SUI_TESTNET_CHAIN_ID = "69WiPg3DAQiwdxfncX6wYQ2siKwAe6L9BZthQea3JNMD"

# The checkpoint that a provider at the chain head reports before a test moves the head.
SUI_BASE_CHECKPOINT = 200_000_000

SUI_EPOCH_ID = 1286

# The seven codes that the real Sui server puts in ``extensions.code``.
SUI_ERROR_CODES: tuple[str, ...] = (
    "BAD_USER_INPUT",
    "FEATURE_UNAVAILABLE",
    "GRAPHQL_PARSE_FAILED",
    "GRAPHQL_VALIDATION_FAILED",
    "INTERNAL_SERVER_ERROR",
    "REQUEST_TIMEOUT",
    "RESOURCE_EXHAUSTED",
)

_ERROR_MESSAGES = {
    "BAD_USER_INPUT": "Invalid value for argument",
    "FEATURE_UNAVAILABLE": "This feature is not available on this server",
    "GRAPHQL_PARSE_FAILED": "The query could not be parsed",
    "GRAPHQL_VALIDATION_FAILED": "The query is not valid",
    "INTERNAL_SERVER_ERROR": "Internal error",
    "REQUEST_TIMEOUT": "Request timed out",
    "RESOURCE_EXHAUSTED": "Too many requests",
}

# The key is the lower-case code, the name that a test gives in ``error_stub``.
SUI_ERROR_STUBS: dict[str, dict] = {
    code.lower(): graphql_error(_ERROR_MESSAGES[code], code) for code in SUI_ERROR_CODES
}
