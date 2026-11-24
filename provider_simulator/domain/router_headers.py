"""The two families of the router's own header names, in one table.

A router before v1.7.0 names its headers ``lava-<x>`` / ``Lava-<X>``; a router
of v1.7.0 or later names them ``router-<x>`` / ``Router-<X>``. The simulator
serves both families at once, with no switch. For each family:

- a history row holds its request headers in a field of their own
  (``router_headers``, ``lava_headers``), beside ``headers``, which holds
  every request header;
- ``GET /history`` filters by that field (``router_header_<name>=<value>``,
  ``lava_header_<name>=<value>``);
- the 101 reply to a WebSocket upgrade names the provider in a header of the
  family (``Router-Provider-Address``, ``Lava-Provider-Address``).

A request header name is matched in any letter case. A header of neither
family is in a row's ``headers`` only.
"""

from collections.abc import Mapping
from typing import NamedTuple, TypedDict, cast


class HeaderFamily(NamedTuple):
    request_prefix: str
    """The start of a request header name of the family, in lower case."""
    row_field: str
    """The field of a history row that holds the request headers of the family."""
    filter_prefix: str
    """The start of a ``GET /history`` query parameter that filters by that field."""
    reply_prefix: str
    """The start of the name of a reply header that the simulator writes for the family."""


ROUTER_HEADER_FAMILIES: tuple[HeaderFamily, ...] = (
    HeaderFamily("router-", "router_headers", "router_header_", "Router-"),
    HeaderFamily("lava-", "lava_headers", "lava_header_", "Lava-"),
)

ROW_FIELDS: frozenset[str] = frozenset(family.row_field for family in ROUTER_HEADER_FAMILIES)


class FamilyHeaders(TypedDict):
    """The request headers of each family, by the row field that holds them."""

    router_headers: dict
    lava_headers: dict


def headers_by_family(headers: Mapping | None) -> FamilyHeaders:
    """The headers of ``headers`` whose name starts with the prefix of each
    family, in any letter case. A family with no such header gets ``{}``."""
    return cast(
        FamilyHeaders,
        {
            family.row_field: {
                name: value for name, value in (headers or {}).items() if name.lower().startswith(family.request_prefix)
            }
            for family in ROUTER_HEADER_FAMILIES
        },
    )


def provider_address_headers(value: str) -> dict[str, str]:
    """The reply headers that name the provider, one for each family."""
    return {f"{family.reply_prefix}Provider-Address": value for family in ROUTER_HEADER_FAMILIES}
