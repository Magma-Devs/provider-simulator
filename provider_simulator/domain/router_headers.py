"""The two spellings of the headers that the smart-router owns.

The router gave each of its headers a new name. A router from before that
change knows only the old names, and a router from after it knows only the new
ones. A test runs the simulator behind a router of either kind, so the
simulator knows both.

    old name (legacy)          new name (current)
    Lava-<X>                   Smart-Router-<X>     the headers of a reply
    lava-<x>                   smartrouter-<x>      every other header

``LEGACY_REPLY_NAMES`` lists the old names of the first row. Each other name
that starts with ``lava-`` follows the second row. A name of the router that
this module was never told of follows the second row too.

A history row holds the headers of the router that a request carried two
times: in ``lava_headers`` under the old names, and in ``router_headers``
under the new names. So a test that reads one of the two names finds the
header, whatever the kind of the router. ``headers`` of the row holds each
header as it came, and is the place to read which spelling the router sent.

Header names have no letter case on the wire. A name that this module makes
has the case of the name it was made from: lower case for a name in lower
case, and the case of ``Smart-Router-Guid`` for each other name.
"""

from collections.abc import Callable, Mapping

_LEGACY_PREFIX = "lava-"
_CURRENT_REPLY_PREFIX = "smart-router-"
_CURRENT_PREFIX = "smartrouter-"

#: The old names whose new name starts with ``Smart-Router-``, in lower case.
#: Each of them names a header that the router puts on a reply.
LEGACY_REPLY_NAMES = frozenset(
    {
        "lava-cache-backend",
        "lava-cache-outcome",
        "lava-cache-tier",
        "lava-errored-providers",
        "lava-extension-unavailable",
        "lava-guid",
        "lava-node-errors-providers",
        "lava-provider-address",
        "lava-provider-node-address-hash",
        "lava-provider-node-extension",
        "lava-relay-protocol",
        "lava-relay-timeout-applied",
        "lava-reported-providers",
        "lava-retries",
    }
)

#: The header in which a router names the provider that answered. The
#: simulator sends it under both names on the reply to a WebSocket upgrade.
PROVIDER_ADDRESS_NAMES = ("Lava-Provider-Address", "Smart-Router-Provider-Address")


def family_of(name: str) -> str | None:
    """``"legacy"`` for an old name of the router, ``"current"`` for a new
    one, and None for a name that is not the router's."""
    lower = name.lower()
    if lower.startswith(_LEGACY_PREFIX):
        return "legacy"
    if lower.startswith((_CURRENT_PREFIX, _CURRENT_REPLY_PREFIX)):
        return "current"
    return None


def is_router_header(name: str) -> bool:
    """True for a header name of the router, in either spelling."""
    return family_of(name) is not None


def _in_the_case_of(lower_name: str, model: str) -> str:
    if model == model.lower():
        return lower_name
    return "-".join(part.capitalize() for part in lower_name.split("-"))


def legacy_name(name: str) -> str:
    """The old name of a header of the router. An old name and a name that is
    not the router's come back as they are."""
    lower = name.lower()
    for prefix in (_CURRENT_REPLY_PREFIX, _CURRENT_PREFIX):
        if lower.startswith(prefix):
            return _in_the_case_of(_LEGACY_PREFIX + lower[len(prefix) :], name)
    return name


def current_name(name: str) -> str:
    """The new name of a header of the router. A new name and a name that is
    not the router's come back as they are."""
    lower = name.lower()
    if not lower.startswith(_LEGACY_PREFIX):
        return name
    prefix = _CURRENT_REPLY_PREFIX if lower in LEGACY_REPLY_NAMES else _CURRENT_PREFIX
    return _in_the_case_of(prefix + lower[len(_LEGACY_PREFIX) :], name)


def router_headers_of(headers: Mapping | None) -> dict:
    """The headers of ``headers`` whose name is the router's, in either
    spelling, each as it came."""
    return {name: value for name, value in (headers or {}).items() if is_router_header(name)}


def _under(headers: Mapping, rename: Callable[[str], str]) -> dict:
    """``headers``, each under the name that ``rename`` gives it. When a
    request carries one header in both spellings, the one that came under the
    wanted name stays."""
    renamed: dict = {}
    as_it_came: dict = {}
    for name, value in headers.items():
        wanted = rename(name)
        if wanted == name:
            as_it_came[name] = value
        else:
            renamed[wanted] = value
    taken = {name.lower() for name in as_it_came}
    return {**{name: value for name, value in renamed.items() if name.lower() not in taken}, **as_it_came}


def under_legacy_names(headers: Mapping) -> dict:
    """Headers of the router, each under its old name. Headers that came with
    old names only are given as they came."""
    return _under(headers, legacy_name)


def under_current_names(headers: Mapping) -> dict:
    """Headers of the router, each under its new name. Headers that came with
    new names only are given as they came."""
    return _under(headers, current_name)
