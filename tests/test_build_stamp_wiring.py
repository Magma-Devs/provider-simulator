"""The build path must stamp in exactly the variables GET /version reads.

Three files have to agree on three names: build_info.py reads them, the
Dockerfile declares and carries them, and scripts/deploy.sh supplies them. Break
that agreement (rename one, drop a --build-arg) and nothing fails. The image
builds, the pod starts, /health says ok, and /version answers "unknown" forever.
The route degrades honestly by design, which is exactly what makes this
particular breakage invisible without a test.

So the names are taken from build_info.py and looked for in the other two files,
which makes a rename there a failing test rather than a silent regression.
"""

import pathlib
import re

from provider_simulator.build_info import COMMIT_ENV, DESCRIBE_ENV, VERSION_ENV

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_DOCKERFILE = (_ROOT / "Dockerfile").read_text()
_DEPLOY_SH = (_ROOT / "scripts" / "deploy.sh").read_text()

_STAMPED = (COMMIT_ENV, VERSION_ENV, DESCRIBE_ENV)


def test_the_three_names_are_distinct_and_present():
    """A guard on this file's own premise. If build_info stopped exporting three
    separate names, every check below would still pass while testing one thing
    three times."""
    assert len(set(_STAMPED)) == 3
    assert all(name.startswith("SIM_GIT_") for name in _STAMPED)


def test_dockerfile_declares_a_build_arg_for_each():
    """Without the ARG line, --build-arg is accepted and then discarded, and
    docker only warns."""
    for name in _STAMPED:
        assert re.search(rf"^ARG\s+{name}=", _DOCKERFILE, re.M), f"Dockerfile declares no ARG {name}"


def test_dockerfile_carries_each_arg_into_the_image_environment():
    """A build argument exists only during the build. Without the matching ENV
    assignment the value never reaches the running process.

    The pattern allows an optional leading ``ENV`` because a multi-name ENV is
    one instruction continued over several lines: only the first name has the
    keyword in front of it, the rest are indented continuations."""
    for name in _STAMPED:
        assert re.search(rf"^\s*(?:ENV\s+)?{name}=\${name}\b", _DOCKERFILE, re.M), f"Dockerfile never sets ENV {name}"


def test_deploy_script_passes_each_build_arg():
    for name in _STAMPED:
        assert f"--build-arg {name}=" in _DEPLOY_SH, f"scripts/deploy.sh does not pass --build-arg {name}"


def test_the_searches_can_tell_absence_from_presence():
    """Positive control. A name that is deliberately not wired up must not be
    found by any of the three searches above. Otherwise they match everything
    and prove nothing."""
    absent = "SIM_GIT_NOT_STAMPED_ANYWHERE"
    assert not re.search(rf"^ARG\s+{absent}=", _DOCKERFILE, re.M)
    assert not re.search(rf"^\s*(?:ENV\s+){absent}=\${absent}\b", _DOCKERFILE, re.M)
    assert f"--build-arg {absent}=" not in _DEPLOY_SH


def test_deploy_script_reads_the_release_tag_with_exact_match():
    """--exact-match is what keeps a build between releases from claiming the
    previous release as its version: it prints a tag only when this commit IS
    that tag, and prints nothing otherwise. Plain `git describe` would print
    v1.4.0-3-gabc1234 and a later change could parse a version out of it."""
    assert "git describe --tags --exact-match" in _DEPLOY_SH
