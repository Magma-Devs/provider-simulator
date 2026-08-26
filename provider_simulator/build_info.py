"""Build identity: which commit and which release a running simulator came from.

Why this exists
---------------
A deployed simulator could not say what it was. The image tag is always
``:latest``, the repo carried no version field, and the container has no ``.git``
directory to ask at runtime. Identifying a pod meant hashing a source file inside
it and matching that hash against git by hand.

So the values are stamped into the image at BUILD time: ``scripts/deploy.sh``
reads them from the checkout it just built and passes them as Docker build
arguments; the Dockerfile turns them into environment variables; this module
reads those variables. Nothing here shells out to git, because in the place that
matters (a pod) there is no git repository to shell out to.

The three states
----------------
``state`` says which one a build is in, so a caller never has to infer it from
which fields happen to be null:

  ``release``   built from a commit that carries a release tag; ``version`` is
                that tag, e.g. ``v1.4.0``.
  ``untagged``  built from a commit with no release tag on it, the usual case
                between releases. ``version`` is null on purpose. A build three
                commits past ``v1.4.0`` is not ``v1.4.0``, and saying it is would
                be exactly the kind of wrong-but-confident version this route was
                added to replace. ``git_describe`` still names the nearest
                release (``v1.4.0-3-gabc1234``) so the build stays locatable.
  ``unknown``   nothing was stamped in. Someone is running ``python run.py``
                from a checkout, or the image predates this route. Every field is
                null. An absent version is honest; a wrong one is not.

A dirty tree is not a release either. ``git describe --dirty`` appends
``-dirty`` when the working tree had uncommitted changes at build time, and a
version ending that way is refused: the tag no longer describes the code that
was built. It stays visible in ``git_describe`` so the reason is readable.
"""

import os
import re
from typing import Mapping, Optional

# Environment variables the image carries. Set from Docker build arguments of
# the same name; see the Dockerfile and scripts/deploy.sh.
COMMIT_ENV = "SIM_GIT_COMMIT"
VERSION_ENV = "SIM_GIT_VERSION"
DESCRIBE_ENV = "SIM_GIT_DESCRIBE"

STATE_RELEASE = "release"
STATE_UNTAGGED = "untagged"
STATE_UNKNOWN = "unknown"

# A commit is 7 to 40 hex characters, a short sha through a full one. Checking
# the shape is what stops a placeholder from being served as though it were a
# real commit: an unexpanded ``$(git rev-parse HEAD)``, the literal word
# ``unknown``, or a build argument someone wired up wrong all fail this and are
# reported as unknown instead.
_COMMIT_RE = re.compile(r"\A[0-9a-fA-F]{7,40}\Z")

# git describe --dirty marks a build made from a tree with uncommitted changes.
_DIRTY_SUFFIX = "-dirty"


def _stamped(env: Mapping[str, str], name: str) -> Optional[str]:
    """Read one stamped value. Missing, empty, or whitespace-only all mean absent.

    An unset build argument arrives as an empty string, not as a missing
    variable, so both have to collapse to the same answer.
    """
    value = env.get(name, "").strip()
    return value or None


def _commit(raw: Optional[str]) -> Optional[str]:
    """The commit, lowercased, or None if it is not shaped like one."""
    if raw is None or not _COMMIT_RE.match(raw):
        return None
    return raw.lower()


def _version(raw: Optional[str]) -> Optional[str]:
    """The release tag, or None when this build is not that release.

    Refused: a blank value, anything with whitespace in it (never a tag name),
    and a ``-dirty`` value, because uncommitted changes mean the code built is
    not the code the tag points at.
    """
    if raw is None or raw.endswith(_DIRTY_SUFFIX) or raw.split() != [raw]:
        return None
    return raw


def build_info(env: Optional[Mapping[str, str]] = None) -> dict:
    """What this running simulator was built from.

    Returns ``version`` / ``commit`` / ``git_describe`` / ``state``. Pass ``env``
    to read somewhere other than the process environment; tests use it.
    """
    source = os.environ if env is None else env

    commit = _commit(_stamped(source, COMMIT_ENV))
    if commit is None:
        # No trustworthy commit means nothing else can be trusted either. A
        # release tag without a commit to anchor it is a claim we cannot check,
        # so it is dropped rather than reported.
        return {"version": None, "commit": None, "git_describe": None, "state": STATE_UNKNOWN}

    version = _version(_stamped(source, VERSION_ENV))
    return {
        "version": version,
        "commit": commit,
        "git_describe": _stamped(source, DESCRIBE_ENV),
        "state": STATE_RELEASE if version else STATE_UNTAGGED,
    }
