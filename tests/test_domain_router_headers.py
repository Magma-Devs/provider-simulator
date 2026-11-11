"""The two spellings of the headers of the smart-router.

A router from before the rename sends and reads ``Lava-<X>`` and ``lava-<x>``.
A router from after it sends and reads ``Smart-Router-<X>`` and
``smartrouter-<x>``. The simulator runs behind both.
"""

import pytest

from provider_simulator.domain.router_headers import (
    LEGACY_REPLY_NAMES,
    PROVIDER_ADDRESS_NAMES,
    current_name,
    family_of,
    is_router_header,
    legacy_name,
    router_headers_of,
    under_current_names,
    under_legacy_names,
)

# Each header that the router renamed, as (old name, new name), with the letter
# case of the router's own constants. The list is the router's, not this
# repository's: a pair that the functions do not give is a fault of the functions.
_RENAMED = [
    ("Lava-Cache-Backend", "Smart-Router-Cache-Backend"),
    ("Lava-Cache-Outcome", "Smart-Router-Cache-Outcome"),
    ("Lava-Cache-Tier", "Smart-Router-Cache-Tier"),
    ("Lava-Errored-Providers", "Smart-Router-Errored-Providers"),
    ("Lava-Extension-Unavailable", "Smart-Router-Extension-Unavailable"),
    ("Lava-Guid", "Smart-Router-Guid"),
    ("Lava-Node-Errors-providers", "Smart-Router-Node-Errors-Providers"),
    ("Lava-Provider-Address", "Smart-Router-Provider-Address"),
    ("Lava-Provider-Node-Address-Hash", "Smart-Router-Provider-Node-Address-Hash"),
    ("Lava-Provider-Node-Extension", "Smart-Router-Provider-Node-Extension"),
    ("Lava-Relay-Protocol", "Smart-Router-Relay-Protocol"),
    ("Lava-Relay-Timeout-Applied", "Smart-Router-Relay-Timeout-Applied"),
    ("Lava-Reported-Providers", "Smart-Router-Reported-Providers"),
    ("Lava-Retries", "Smart-Router-Retries"),
    ("lava-cross-validation-agreeing-providers", "smartrouter-cross-validation-agreeing-providers"),
    ("lava-cross-validation-agreement-threshold", "smartrouter-cross-validation-agreement-threshold"),
    ("lava-cross-validation-all-providers", "smartrouter-cross-validation-all-providers"),
    ("lava-cross-validation-disagreeing-providers", "smartrouter-cross-validation-disagreeing-providers"),
    ("lava-cross-validation-failure-reason", "smartrouter-cross-validation-failure-reason"),
    ("lava-cross-validation-max-participants", "smartrouter-cross-validation-max-participants"),
    ("lava-cross-validation-pending-providers", "smartrouter-cross-validation-pending-providers"),
    ("lava-cross-validation-plurality-providers", "smartrouter-cross-validation-plurality-providers"),
    ("lava-cross-validation-plurality-size", "smartrouter-cross-validation-plurality-size"),
    ("lava-cross-validation-status", "smartrouter-cross-validation-status"),
    ("lava-debug-relay", "smartrouter-debug-relay"),
    ("lava-extension", "smartrouter-extension"),
    ("lava-fast-tx-participants", "smartrouter-fast-tx-participants"),
    ("lava-force-cache-refresh", "smartrouter-force-cache-refresh"),
    ("lava-hedge-triggered", "smartrouter-hedge-triggered"),
    ("lava-identified-node-error", "smartrouter-identified-node-error"),
    ("lava-lb-unique-id", "smartrouter-lb-unique-id"),
    ("lava-parsed-requested-block", "smartrouter-parsed-requested-block"),
    ("lava-providers-block", "smartrouter-providers-block"),
    ("lava-relay-timeout", "smartrouter-relay-timeout"),
    ("lava-select-provider", "smartrouter-select-provider"),
    ("lava-select-provider-strict", "smartrouter-select-provider-strict"),
    ("lava-selection-stats", "smartrouter-selection-stats"),
    ("lava-stateful-api", "smartrouter-stateful-api"),
    ("lava-stickiness", "smartrouter-stickiness"),
    ("lava-user-request-type", "smartrouter-user-request-type"),
]


@pytest.mark.parametrize(("old", "new"), _RENAMED, ids=[old for old, _ in _RENAMED])
def test_each_renamed_header_is_known_under_both_names(old, new):
    # Names have no letter case on the wire, so they are compared in lower case.
    assert current_name(old).lower() == new.lower()
    assert legacy_name(new).lower() == old.lower()
    assert family_of(old) == "legacy"
    assert family_of(new) == "current"


def test_the_reply_names_are_the_ones_whose_new_name_starts_with_smart_router():
    from_the_list = {old.lower() for old, new in _RENAMED if new.lower().startswith("smart-router-")}
    assert LEGACY_REPLY_NAMES == from_the_list


@pytest.mark.parametrize(
    ("name", "old", "new"),
    [
        # As Go writes a header on HTTP/1.1.
        ("Lava-Guid", "Lava-Guid", "Smart-Router-Guid"),
        ("Smart-Router-Guid", "Lava-Guid", "Smart-Router-Guid"),
        ("Lava-Stateful-Api", "Lava-Stateful-Api", "Smartrouter-Stateful-Api"),
        ("Smartrouter-Stateful-Api", "Lava-Stateful-Api", "Smartrouter-Stateful-Api"),
        # As gRPC metadata and HTTP/2 carry it.
        ("lava-guid", "lava-guid", "smart-router-guid"),
        ("smart-router-guid", "lava-guid", "smart-router-guid"),
        ("lava-stateful-api", "lava-stateful-api", "smartrouter-stateful-api"),
        ("smartrouter-stateful-api", "lava-stateful-api", "smartrouter-stateful-api"),
        # Any other case gives the case of an HTTP/1.1 header.
        ("LAVA-GUID", "LAVA-GUID", "Smart-Router-Guid"),
        ("SMART-ROUTER-GUID", "Lava-Guid", "SMART-ROUTER-GUID"),
    ],
)
def test_a_made_name_has_the_case_of_the_name_it_was_made_from(name, old, new):
    assert legacy_name(name) == old
    assert current_name(name) == new


def test_a_router_name_that_is_not_on_the_list_follows_the_rule_of_the_other_headers():
    assert current_name("lava-consumer-relay") == "smartrouter-consumer-relay"
    assert legacy_name("smartrouter-consumer-relay") == "lava-consumer-relay"
    assert legacy_name("smart-router-new-reply-header") == "lava-new-reply-header"


@pytest.mark.parametrize(
    "name",
    ["Content-Type", "X-Request-Id", "X-LAVA-Provider", "X-Smart-Router-Provider", "lavalamp", "smartrouter", "lava"],
)
def test_a_name_that_is_not_the_routers_stays_as_it_is(name):
    assert family_of(name) is None
    assert not is_router_header(name)
    assert legacy_name(name) == name
    assert current_name(name) == name


def test_headers_of_an_old_router_are_given_under_both_names():
    sent = router_headers_of({"Content-Type": "application/json", "Lava-Guid": "g1", "lava-stateful-api": "true"})

    # Under the old names they are the headers as they came.
    assert sent == {"Lava-Guid": "g1", "lava-stateful-api": "true"}
    assert under_legacy_names(sent) == sent
    assert under_current_names(sent) == {"Smart-Router-Guid": "g1", "smartrouter-stateful-api": "true"}


def test_headers_of_a_new_router_are_given_under_both_names():
    sent = router_headers_of(
        {"Content-Type": "application/json", "Smart-Router-Guid": "g1", "smartrouter-stateful-api": "true"}
    )

    assert under_legacy_names(sent) == {"Lava-Guid": "g1", "lava-stateful-api": "true"}
    # Under the new names they are the headers as they came.
    assert sent == {"Smart-Router-Guid": "g1", "smartrouter-stateful-api": "true"}
    assert under_current_names(sent) == sent


def test_a_request_with_no_header_of_the_router_gives_nothing():
    assert router_headers_of({"Content-Type": "application/json", "X-LAVA-Provider": "node-1"}) == {}
    assert router_headers_of({}) == {}
    assert router_headers_of(None) == {}
    assert under_legacy_names({}) == {}
    assert under_current_names({}) == {}


def test_a_name_that_is_not_the_routers_is_kept_under_both_names():
    # The call log takes the headers that a caller gives it, and it drops none.
    assert under_legacy_names({"X-Other": "1"}) == {"X-Other": "1"}
    assert under_current_names({"X-Other": "1"}) == {"X-Other": "1"}


def test_a_header_that_comes_in_both_spellings_keeps_each_value_under_its_own_name():
    sent = {"lava-stickiness": "old", "Smartrouter-Stickiness": "new"}

    assert under_legacy_names(sent) == {"lava-stickiness": "old"}
    assert under_current_names(sent) == {"Smartrouter-Stickiness": "new"}


def test_the_values_are_not_changed():
    sent = {"smartrouter-x-bin": b"\x00\x01", "Smart-Router-Retries": 2}

    assert under_legacy_names(sent) == {"lava-x-bin": b"\x00\x01", "Lava-Retries": 2}


def test_the_provider_address_has_its_two_names():
    old, new = PROVIDER_ADDRESS_NAMES
    assert (old, new) == ("Lava-Provider-Address", "Smart-Router-Provider-Address")
    assert current_name(old) == new
    assert legacy_name(new) == old
