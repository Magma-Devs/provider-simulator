"""Build identity: the three states GET /version can report, and the values it
refuses to report.

The refusals matter as much as the happy path. This route exists because a
version that is confidently wrong sends everyone downstream in the wrong
direction; every test below that expects ``None`` is pinning a case where the
honest answer is "I do not know".
"""

from provider_simulator.build_info import build_info

_SHA = "cd0c74c2ad8e9ff36d24ce19e29e6db2aba6ec3e"


# ── the three states ──────────────────────────────────────────────────────────
def test_nothing_stamped_is_unknown():
    """Someone running `python run.py` from a checkout, or an image built before
    this route existed. Every field is null and the state says so."""
    assert build_info({}) == {"version": None, "commit": None, "git_describe": None, "state": "unknown"}


def test_tagged_build_reports_the_release():
    info = build_info({"SIM_GIT_COMMIT": _SHA, "SIM_GIT_VERSION": "v1.4.0", "SIM_GIT_DESCRIBE": "v1.4.0"})
    assert info == {"version": "v1.4.0", "commit": _SHA, "git_describe": "v1.4.0", "state": "release"}


def test_build_past_a_release_reports_no_version():
    """Three commits past v1.4.0 is not v1.4.0. The version is null and the
    describe string is what says which release it sits after."""
    info = build_info({"SIM_GIT_COMMIT": _SHA, "SIM_GIT_VERSION": "", "SIM_GIT_DESCRIBE": "v1.4.0-3-gcd0c74c"})
    assert info["version"] is None
    assert info["state"] == "untagged"
    assert info["commit"] == _SHA
    assert info["git_describe"] == "v1.4.0-3-gcd0c74c"


def test_repo_with_no_tags_at_all_is_untagged_not_unknown():
    """`git describe --always` falls back to a bare short sha when no tag exists
    anywhere. The commit is known, so this is untagged, not unknown."""
    info = build_info({"SIM_GIT_COMMIT": _SHA, "SIM_GIT_DESCRIBE": "cd0c74c"})
    assert info["state"] == "untagged"
    assert info["version"] is None
    assert info["commit"] == _SHA


# ── values that must be refused ───────────────────────────────────────────────
def test_dirty_tag_is_not_that_release():
    """The tag was on the commit but the tree had uncommitted changes, so the
    code built is not the code the tag points at. The version is dropped; the
    describe string keeps -dirty visible so the reason is readable."""
    info = build_info({"SIM_GIT_COMMIT": _SHA, "SIM_GIT_VERSION": "v1.4.0-dirty", "SIM_GIT_DESCRIBE": "v1.4.0-dirty"})
    assert info["version"] is None
    assert info["state"] == "untagged"
    assert info["git_describe"] == "v1.4.0-dirty"


def test_version_without_a_commit_is_dropped():
    """A release claim with no commit behind it cannot be checked, so it is not
    served. Reporting v1.4.0 here would be the exact failure this route replaces."""
    info = build_info({"SIM_GIT_VERSION": "v1.4.0", "SIM_GIT_DESCRIBE": "v1.4.0"})
    assert info == {"version": None, "commit": None, "git_describe": None, "state": "unknown"}


def test_unset_build_args_arrive_as_empty_strings():
    """`docker build` with no --build-arg leaves the variables set but empty.
    Empty must read the same as absent, not as a commit named ''."""
    info = build_info({"SIM_GIT_COMMIT": "", "SIM_GIT_VERSION": "", "SIM_GIT_DESCRIBE": ""})
    assert info["state"] == "unknown"
    assert info["commit"] is None


def test_whitespace_only_commit_is_unknown():
    assert build_info({"SIM_GIT_COMMIT": "   \n"})["state"] == "unknown"


def test_literal_word_unknown_is_not_served_as_a_commit():
    """A build script that fills the blank with the word "unknown" must not have
    that echoed back as though it were a real commit."""
    info = build_info({"SIM_GIT_COMMIT": "unknown"})
    assert info["commit"] is None
    assert info["state"] == "unknown"


def test_unexpanded_placeholder_is_not_served_as_a_commit():
    """A build argument wired up wrong: the shell never ran the substitution."""
    info = build_info({"SIM_GIT_COMMIT": "$(git rev-parse HEAD)"})
    assert info["commit"] is None
    assert info["state"] == "unknown"


def test_too_short_to_be_a_commit_is_refused():
    assert build_info({"SIM_GIT_COMMIT": "cd0c74"})["commit"] is None


def test_version_with_whitespace_is_refused():
    """Not a tag name. Something went wrong upstream of the build."""
    info = build_info({"SIM_GIT_COMMIT": _SHA, "SIM_GIT_VERSION": "fatal: no tag exactly matches"})
    assert info["version"] is None
    assert info["state"] == "untagged"


# ── shapes ────────────────────────────────────────────────────────────────────
def test_short_commit_is_accepted():
    assert build_info({"SIM_GIT_COMMIT": "cd0c74c"})["commit"] == "cd0c74c"


def test_commit_is_lowercased():
    """So a caller can compare it against `git rev-parse HEAD` with ==."""
    assert build_info({"SIM_GIT_COMMIT": _SHA.upper()})["commit"] == _SHA


def test_surrounding_whitespace_is_stripped():
    """A trailing newline is what `git rev-parse` prints; it must not survive
    into the response and break an equality check."""
    assert build_info({"SIM_GIT_COMMIT": f"  {_SHA}\n"})["commit"] == _SHA


def test_every_state_is_one_of_the_three():
    states = {
        build_info({})["state"],
        build_info({"SIM_GIT_COMMIT": _SHA})["state"],
        build_info({"SIM_GIT_COMMIT": _SHA, "SIM_GIT_VERSION": "v1.4.0"})["state"],
    }
    assert states == {"unknown", "untagged", "release"}


# ── the default source is the process environment ─────────────────────────────
def test_reads_the_process_environment_when_no_mapping_is_given(monkeypatch):
    """The image stamps these as environment variables, so this is the path that
    actually runs in a pod."""
    monkeypatch.setenv("SIM_GIT_COMMIT", _SHA)
    monkeypatch.setenv("SIM_GIT_VERSION", "v1.4.0")
    monkeypatch.setenv("SIM_GIT_DESCRIBE", "v1.4.0")
    assert build_info() == {"version": "v1.4.0", "commit": _SHA, "git_describe": "v1.4.0", "state": "release"}


def test_process_environment_without_the_variables_is_unknown(monkeypatch):
    for name in ("SIM_GIT_COMMIT", "SIM_GIT_VERSION", "SIM_GIT_DESCRIBE"):
        monkeypatch.delenv(name, raising=False)
    assert build_info() == {"version": None, "commit": None, "git_describe": None, "state": "unknown"}
