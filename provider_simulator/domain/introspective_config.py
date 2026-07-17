"""Shared update/snapshot/reset for dataclass-based config objects.

A config object exposes exactly three operations:

  update(cfg)  — apply a partial dict of field -> value; unknown keys raise.
  snapshot()   — return a thread-safe copy of every field's current value.
  reset()      — restore every field to its declared default.

Implementing these once, by reflecting over the dataclass fields, removes the
bug where adding a field meant editing update, snapshot, and reset by hand and
forgetting one. Subclasses just declare fields; this base does the rest.
"""

import threading
from dataclasses import MISSING, dataclass, fields


@dataclass
class IntrospectiveConfig:
    """Field-less dataclass base. Being a dataclass is what lets ``fields(self)``
    below type-check — subclasses add the real fields."""

    def __post_init__(self) -> None:
        # Not a dataclass field, so it never appears in fields()/snapshot().
        self._lock = threading.Lock()

    def _field_names(self) -> set[str]:
        return {f.name for f in fields(self)}

    def update(self, cfg: dict) -> None:
        """Apply only the keys present in cfg. Validate every key first so an
        unknown key aborts the whole update instead of half-applying it."""
        names = self._field_names()
        unknown = [k for k in cfg if k not in names]
        if unknown:
            raise ValueError(f"unknown config field(s) {unknown}; valid fields are {sorted(names)}")
        with self._lock:
            for key, value in cfg.items():
                setattr(self, key, value)

    def snapshot(self) -> dict:
        with self._lock:
            return {f.name: getattr(self, f.name) for f in fields(self)}

    def reset(self) -> None:
        with self._lock:
            for f in fields(self):
                if f.default is not MISSING:
                    setattr(self, f.name, f.default)
                elif f.default_factory is not MISSING:  # type: ignore[misc]
                    setattr(self, f.name, f.default_factory())  # type: ignore[misc]
