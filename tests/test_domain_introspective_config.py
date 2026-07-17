from dataclasses import dataclass, field

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


def test_update_rejects_unknown_key():
    t = _Toy()
    try:
        t.update({"a": 5, "nope": 1})
    except ValueError as exc:
        assert "nope" in str(exc)
    else:
        raise AssertionError("unknown key must raise ValueError")
    # partial-apply guard: nothing changed because validation runs before writes
    assert t.a == 1


def test_reset_restores_defaults_including_factory():
    t = _Toy()
    t.update({"a": 9})
    t.items.append("dirty")
    t.reset()
    assert t.snapshot() == {"a": 1, "b": "x", "items": []}


def test_snapshot_is_a_copy_not_a_live_view():
    t = _Toy()
    snap = t.snapshot()
    snap["a"] = 999
    assert t.a == 1
