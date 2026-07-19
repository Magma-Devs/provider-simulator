from dataclasses import dataclass, field

import pytest

from provider_simulator.domain.introspective_config import IntrospectiveConfig


@dataclass
class _Toy(IntrospectiveConfig):
    a: int = 1
    b: str = "x"
    items: list = field(default_factory=list)


def test_snapshot_returns_all_fields():
    t = _Toy()
    assert t.snapshot() == {"a": 1, "b": "x", "items": []}


def test_update_applies_known_keys_only():
    t = _Toy()
    t.update({"a": 5, "b": "y"})
    assert t.a == 5
    assert t.b == "y"


def test_update_rejects_unknown_key_and_applies_nothing():
    t = _Toy()
    with pytest.raises(ValueError, match="nope"):
        t.update({"a": 5, "nope": 1})
    # partial-apply guard: nothing changed because validation runs before writes
    assert t.a == 1


def test_reset_restores_defaults_including_factory():
    t = _Toy()
    t.update({"a": 9})
    t.items.append("dirty")
    t.reset()
    assert t.snapshot() == {"a": 1, "b": "x", "items": []}


def test_reset_raises_on_field_without_default():
    @dataclass
    class _NoDefault(IntrospectiveConfig):
        required: int

    c = _NoDefault(required=5)
    c.update({"required": 99})
    with pytest.raises(TypeError, match="required"):
        c.reset()


def test_snapshot_is_a_copy_not_a_live_view():
    t = _Toy()
    snap = t.snapshot()
    snap["a"] = 999
    assert t.a == 1


def test_snapshot_copies_mutable_values_one_level():
    t = _Toy()
    t.update({"items": ["a"]})
    snap = t.snapshot()
    snap["items"].append("INJECTED")
    assert t.snapshot()["items"] == ["a"], "editing a snapshot list must not touch live config"


def test_update_copies_caller_owned_mutables():
    t = _Toy()
    mine = ["a"]
    t.update({"items": mine})
    mine.append("INJECTED")
    assert t.snapshot()["items"] == ["a"], "mutating the caller's list must not touch live config"


def test_subclass_with_own_post_init_still_gets_a_lock():
    @dataclass
    class _OwnPostInit(IntrospectiveConfig):
        a: int = 1

        def __post_init__(self):
            # Deliberately does NOT call super().__post_init__() — the lazy
            # lock must still work.
            self.a = self.a * 2

    c = _OwnPostInit()
    assert c.a == 2
    c.update({"a": 7})  # would raise AttributeError if the lock were eager-only
    assert c.snapshot() == {"a": 7}


def test_update_stamps_last_write_at():
    t = _Toy()
    before = t.last_write_at
    t.update({"a": 2})
    assert t.last_write_at >= before
