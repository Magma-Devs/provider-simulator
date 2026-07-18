"""Shared update/snapshot/reset for dataclass-based config objects.

A config object exposes exactly three operations:

  update(cfg)  — apply a partial dict of field -> value; unknown keys raise.
  snapshot()   — return a copy of every field's current value. Mutable values
                 are copied ONE level deep (a deep copy would serialize every
                 request behind the config lock): replacing keys/items in the
                 snapshot never touches live config, but structures nested
                 inside a value (e.g. one method's override body inside
                 ``responses``) are shared — treat them as read-only, and
                 copy at the use site if you must mutate (the handlers'
                 existing deepcopy-at-use pattern).
  reset()      — restore every field to its declared default; a field with no
                 default is a hard error, never silently skipped.

Implementing these once, by reflecting over the dataclass fields, removes the
bug where adding a field meant editing update, snapshot, and reset by hand and
forgetting one. Subclasses just declare fields; this base does the rest.

Read/write contract: WRITE via update(), READ via snapshot(). Direct attribute
reads are not synchronized — a handler that reads fields one at a time can
observe a half-applied update. Take one snapshot per request and read from it.

The lock is created lazily on first use (dict.setdefault is atomic under the
GIL), so a subclass that defines its own __post_init__ without calling super()
still gets a lock, and dataclasses.replace() copies work. deepcopy/pickle are
not supported — these are live in-process objects, not serializable state.

update() also stamps ``last_write_at`` (epoch seconds, readable as a plain
attribute) so a staleness sweep can revert scenarios that no test has touched
for a while.
"""

import copy
import threading
import time
from dataclasses import MISSING, dataclass, fields


@dataclass
class IntrospectiveConfig:
    """Field-less dataclass base. Being a dataclass is what lets ``fields(self)``
    below type-check — subclasses add the real fields."""

    @property
    def _lock(self) -> threading.Lock:
        # Lazily created so it exists even when a subclass overrides
        # __post_init__ without super(). Check first so a fresh Lock() isn't
        # built and immediately discarded on every access (snapshot/update/reset
        # are hot paths); setdefault is atomic under the GIL, so two racing
        # first-callers still share one lock object.
        lock = self.__dict__.get("_lock_obj")
        if lock is None:
            lock = self.__dict__.setdefault("_lock_obj", threading.Lock())
        return lock

    @property
    def last_write_at(self) -> float:
        """Epoch seconds of the last update(). Reading never stamps, so a
        staleness sweep that reads this can't accidentally refresh a config to
        "now". A never-updated config reads as ~now, but it is always at
        defaults and the sweep only reverts non-default scenarios, so it's
        skipped anyway."""
        return self.__dict__.get("_last_write_at", time.time())

    def _field_names(self) -> set[str]:
        return {f.name for f in fields(self)}

    def _validate(self, cfg: dict) -> None:
        """Hook for subclasses to reject bad VALUES (bad keys are handled here).
        Runs before any write; raise ValueError to abort the whole update."""

    def update(self, cfg: dict) -> None:
        """Apply only the keys present in cfg. Validate every key and value
        first so a bad entry aborts the whole update instead of half-applying
        it. Mutable values are copied in, so later mutation of the caller's
        dict/list cannot silently rewrite live config."""
        names = self._field_names()
        unknown = [k for k in cfg if k not in names]
        if unknown:
            raise ValueError(f"unknown config field(s) {unknown}; valid fields are {sorted(names)}")
        self._validate(cfg)
        with self._lock:
            for key, value in cfg.items():
                setattr(self, key, copy.copy(value))
            self.__dict__["_last_write_at"] = time.time()

    def snapshot(self) -> dict:
        with self._lock:
            return {f.name: copy.copy(getattr(self, f.name)) for f in fields(self)}

    def reset(self) -> None:
        with self._lock:
            for f in fields(self):
                if f.default is not MISSING:
                    setattr(self, f.name, f.default)
                elif f.default_factory is not MISSING:  # type: ignore[misc]
                    setattr(self, f.name, f.default_factory())  # type: ignore[misc]
                else:
                    raise TypeError(
                        f"cannot reset field {f.name!r}: it declares no default; "
                        "config fields must have a default or default_factory"
                    )
