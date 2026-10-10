"""The guard of the request flow: ``server.py`` asks no fault policy and writes
no history row.

Only the package ``provider_simulator/listeners/`` asks ``fault_policy`` about
a request, and only that package writes a history row. ``server.py`` owns the
sockets: it gives each request to a listener, and it performs the plan that
the listener returns. The tests below read the source of ``server.py``. They
fail when a line that decides a fault or writes a row comes back.

The guard reads the syntax tree and not the text. So a comment, a docstring or
a text that names a forbidden thing does not count.

The guard has limits. It reads ``server.py`` only. It finds a forbidden name
that the code writes, and it does not find one that the code reaches in
another way: a row writer behind an alias or a callback, a ``getattr`` call,
or an import through a text.
"""

import ast
from pathlib import Path

# This file is two folders below the root of the repository.
_SERVER_PY = Path(__file__).resolve().parents[2] / "server.py"

# A call of one of these methods writes a history row, or completes one.
_ROW_WRITERS = ("record_arrival", "finalize", "push")


def _forbidden(source: str) -> list[str]:
    """Each forbidden place of a source text, as ``line <number>: <what>``.

    Forbidden: an import, a name or an attribute ``fault_policy``, and a call
    of a method that writes a history row."""
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = [alias.name for alias in node.names]
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            if any("fault_policy" in module.split(".") for module in modules):
                found.add((node.lineno, "fault_policy"))
        elif isinstance(node, ast.Name) and node.id == "fault_policy":
            found.add((node.lineno, "fault_policy"))
        elif isinstance(node, ast.Attribute) and node.attr == "fault_policy":
            found.add((node.lineno, "fault_policy"))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _ROW_WRITERS:
            found.add((node.lineno, f"{node.func.attr}()"))
    return [f"line {lineno}: {what}" for lineno, what in sorted(found)]


_SAMPLE = '''\
"""A docstring that names fault_policy and provider.log.push() does not count."""

from provider_simulator import fault_policy


def serve_frame(provider, endpoint, out_queue):
    # A comment that names fault_policy and log.finalize() does not count.
    entry = provider.log.record_arrival(endpoint.interface, endpoint.transport, endpoint.port)
    provider.log.finalize(entry, method="*", status="down", latency_ms=0)
    provider.log.push("*", "down", 0, interface="jsonrpc", transport="ws", port=endpoint.port)
    out_queue.put_nowait("a text that names fault_policy does not count")
'''


def test_server_py_imports_no_fault_policy():
    """``server.py`` has no import, no name and no attribute ``fault_policy``.
    A listener asks the fault policy, and ``server.py`` performs the result."""
    found = [place for place in _forbidden(_SERVER_PY.read_text(encoding="utf-8")) if place.endswith("fault_policy")]
    assert found == [], f"server.py uses fault_policy: {found}"


def test_server_py_calls_no_method_that_writes_a_history_row():
    """``server.py`` has no call of ``record_arrival``, ``finalize`` or
    ``push``. A listener, or the subscription registry of the listeners,
    writes each history row."""
    found = [place for place in _forbidden(_SERVER_PY.read_text(encoding="utf-8")) if place.endswith("()")]
    assert found == [], f"server.py writes a history row: {found}"


def test_the_guard_finds_each_forbidden_line_of_a_sample_source():
    """The sample has four forbidden lines: the import of ``fault_policy`` and
    one call of each method that writes a row. The guard also finds four more
    forms of a line that uses ``fault_policy``."""
    assert _forbidden(_SAMPLE) == [
        "line 3: fault_policy",
        "line 8: record_arrival()",
        "line 9: finalize()",
        "line 10: push()",
    ]
    assert _forbidden("import provider_simulator.fault_policy as policy\n") == ["line 1: fault_policy"]
    assert _forbidden("from provider_simulator.fault_policy import decide\n") == ["line 1: fault_policy"]
    assert _forbidden("verdict = fault_policy.decide(scenario, endpoint, provider)\n") == ["line 1: fault_policy"]
    assert _forbidden("verdict = provider_simulator.fault_policy.decide(scenario)\n") == ["line 1: fault_policy"]
