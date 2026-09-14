"""The reader that opens the router's store and says what is in it.

The thing this file is most careful about is the difference between **an empty
store and a store nobody asked**. A reader that answers "no keys" when it could
not connect passes every test ever written on it, because both answers look
identical to the caller and only one of them is a measurement.

So every test here that expects nothing is paired with one that expects
something, on the same reader, against the same kind of store.
"""

from __future__ import annotations

import pytest

from provider_simulator.resp_store import RespStore, RespStoreError
from tests.resp_fake_store import FakeRespStore


@pytest.fixture
def store():
    with FakeRespStore() as fake:
        yield fake


@pytest.fixture
def reader(store):
    return RespStore("127.0.0.1", store.port)


# ── the reader can fail, which is what makes its empty answer mean something ──


def test_an_empty_store_reads_as_no_entries(reader):
    assert reader.entries() == []


def test_a_store_holding_one_key_reads_as_one_entry(store, reader):
    """The other half of the test above, and the reason it counts.

    Without this, a reader hard-wired to answer nothing would pass the empty
    case and every test built on it. This is the positive control for the whole
    file.
    """
    store.put("sr:rel:f:ETH1:abc:1", '{"result":"0x1"}', ttl=3600)
    entries = reader.entries()
    assert len(entries) == 1
    assert entries[0].key == "sr:rel:f:ETH1:abc:1"
    assert entries[0].value == '{"result":"0x1"}'
    assert 0 < entries[0].ttl <= 3600


def test_an_unreachable_store_raises_rather_than_reading_as_empty():
    """The failure this module exists to prevent.

    A closed port and an empty store must not produce the same answer. Port 1 is
    reserved and nothing listens on it, so this is a connection that cannot
    succeed rather than one that happens not to.
    """
    with pytest.raises(RespStoreError) as caught:
        RespStore("127.0.0.1", 1, timeout=0.2).entries()
    assert "cannot reach" in str(caught.value)


def test_a_stopped_store_raises_rather_than_reading_as_empty(store, reader):
    """The same distinction, reached the way a test would actually reach it.

    The store answered a moment ago and now does not. An empty list here would
    tell a caller the router stored nothing, when what happened is that the
    store went away.
    """
    store.put("sr:chaintip:ETH1", "20000000")
    assert len(reader.entries()) == 1
    store.stop()
    with pytest.raises(RespStoreError):
        reader.entries()


# ── reading ───────────────────────────────────────────────────────────────────


def test_ping_says_whether_the_store_answers(store, reader):
    assert reader.ping() is True
    store.stop()
    assert reader.ping() is False


def test_scan_finds_every_key(store, reader):
    store.put("sr:chaintip:ETH1", "20000000")
    store.put("sr:rel:f:ETH1:aaa:1", "one")
    store.put("other:key", "two")
    assert reader.scan() == ["other:key", "sr:chaintip:ETH1", "sr:rel:f:ETH1:aaa:1"]


def test_a_pattern_narrows_the_scan(store, reader):
    store.put("sr:chaintip:ETH1", "20000000")
    store.put("other:key", "two")
    assert reader.scan("sr:*") == ["sr:chaintip:ETH1"]


def test_the_default_pattern_is_every_key_not_the_routers_prefix(store, reader):
    """A reader that silently filtered on ``sr:`` would answer "empty" for a
    store whose key-prefix was configured differently, and the caller would read
    that as the router having stored nothing."""
    store.put("elsewhere:key", "value")
    assert reader.scan() == ["elsewhere:key"]


def test_get_answers_none_for_a_key_that_is_not_there(reader):
    assert reader.get("sr:missing") is None


def test_ttl_reports_no_expiry_and_no_key_distinctly(store, reader):
    """-1 and -2 are the store's own convention and they are not durations.

    A caller reading either as "one second left" would wait for an entry to
    expire that never will, or read a key that is already gone as still present.
    """
    store.put("sr:forever", "value")
    assert reader.ttl("sr:forever") == -1
    assert reader.ttl("sr:never-existed") == -2


def test_an_expired_key_is_gone_from_the_scan(store, reader):
    store.put("sr:brief", "value", ttl=0)
    assert reader.scan() == []


# ── flush ─────────────────────────────────────────────────────────────────────


def test_flush_empties_the_store(store, reader):
    store.put("sr:one", "1")
    store.put("sr:two", "2")
    assert len(reader.entries()) == 2
    reader.flushdb()
    assert reader.entries() == []


def test_flush_on_an_unreachable_store_raises(store, reader):
    store.stop()
    with pytest.raises(RespStoreError):
        reader.flushdb()


# ── there is no write, and that is the design ─────────────────────────────────


def test_the_reader_offers_no_way_to_put_an_entry_in():
    """A test that can plant an entry will plant one instead of making the
    router store it, and then it measures the store rather than the router.

    Checked by name rather than by trying to call one: the point is that no such
    method exists to be found, whatever it might be called.
    """
    surface = {name for name in dir(RespStore) if not name.startswith("_")}
    assert surface == {"entries", "flushdb", "get", "ping", "scan", "target", "ttl"}


def test_the_reader_sends_scan_rather_than_keys(store, reader):
    """KEYS blocks a real server for the whole sweep; SCAN does not.

    The habit matters more than the size of our store: a command that is safe on
    a test store and dangerous on a customer's is the wrong one to leave in a
    repository people copy from.
    """
    store.put("sr:one", "1")
    reader.scan()
    sent = [parts[0].upper() for parts in store.commands]
    assert "SCAN" in sent
    assert "KEYS" not in sent


def test_a_key_that_expires_between_the_value_read_and_the_lifetime_read_is_dropped(store, reader):
    """The race one step later than the scan, and it needs the same answer.

    A key can go after its value is read and before its lifetime is. The store
    reports that as -2, "no such key", so an entry holding a value and a -2 is a
    key that left while we were looking at it. Reporting it would put a key in
    the answer that is not in the store.

    Driven by making the lifetime read answer -2 for a key whose value read
    succeeded, which is exactly what the store does in that window.
    """
    store.put("sr:vanishing", "value")
    real_ttl = reader.ttl

    def ttl_says_gone(key: str) -> int:
        return -2 if key == "sr:vanishing" else real_ttl(key)

    reader.ttl = ttl_says_gone  # type: ignore[method-assign]
    assert reader.entries() == []


def test_a_key_with_no_expiry_is_still_reported(store, reader):
    """The positive control for the test above. Dropping on -2 must not drop on
    -1, which means "this key never expires" and is a perfectly present key."""
    store.put("sr:forever", "value")
    entries = reader.entries()
    assert len(entries) == 1
    assert entries[0].ttl == -1


def test_rewriting_a_key_without_a_lifetime_clears_the_old_one(store, reader):
    """A property of the test double, checked because a test depends on it.

    Redis drops an existing expiry when a key is rewritten with no TTL. A double
    that kept the old deadline would make a test's permanent entry vanish
    partway through, and the reader would be blamed for a store that had quietly
    expired it.
    """
    store.put("sr:reused", "first", ttl=1)
    store.put("sr:reused", "second")
    assert reader.ttl("sr:reused") == -1
    assert reader.get("sr:reused") == "second"
