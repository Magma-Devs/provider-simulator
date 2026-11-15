"""Every module of the package imports on its own, whichever module an importer reaches first.

The chains package and the domain package import each other. An import at the top of a module can
make that a cycle that fails only when one particular module is imported first. A test that imports
the package in the usual order never sees it. So each module is imported first, in an interpreter of
its own.
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _modules() -> list[str]:
    names = []
    for path in sorted((ROOT / "provider_simulator").rglob("*.py")):
        parts = path.relative_to(ROOT).with_suffix("").parts
        names.append(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
    return names


MODULES = _modules()


def test_the_search_finds_the_modules_of_the_package():
    assert len(MODULES) > 30, MODULES
    assert "provider_simulator.chains" in MODULES and "provider_simulator.domain.registry" in MODULES


@pytest.mark.parametrize("module", MODULES)
def test_a_module_imports_first_in_a_fresh_interpreter(module):
    ran = subprocess.run([sys.executable, "-c", f"import {module}"], cwd=ROOT, capture_output=True, text=True)

    assert ran.returncode == 0, f"importing {module} first failed:\n{ran.stderr}"
