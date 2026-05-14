"""Vendored Cosmos / Tendermint protobuf stubs (MAG-1780).

Snapshot-copy of ``src/clients/grpc/cosmos_pb2/`` from the
``smart-router-automation`` repo (vendored there under MAG-1779). The original
``.proto`` sources are not carried here — to regenerate, see the protoc
command documented in that repo's ``proto/README.md``. Updating these stubs
means re-copying the directory; do **not** edit ``*_pb2.py`` /
``*_pb2_grpc.py`` files by hand.

The generated files use absolute imports rooted at this package directory
(e.g. ``from cosmos.base.tendermint.v1beta1 import query_pb2``), which is how
``protoc`` emits them when ``--python_out`` points at a single root. To make
those absolute imports resolve in the provider-simulator process without
re-writing the generated code, this package prepends its own filesystem path
to ``sys.path`` at import time. After ``import cosmos_pb2`` runs once,
``handlers_grpc.py`` and ``grpc_server.py`` can do::

    from cosmos.base.tendermint.v1beta1 import query_pb2
    from cosmos.base.tendermint.v1beta1 import query_pb2_grpc
    from tendermint.types import block_pb2, types_pb2

The transitive subpackages (``cosmos``, ``tendermint``, ``gogoproto``,
``cosmos_proto``, ``google``, ``amino``) are populated by ``protoc``.
"""

from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))

# Splice ourselves onto sys.path so the generated absolute imports resolve.
# Prepend so vendored top-levels (e.g. ``google/``) win over any stray
# namespace package on the system path. Dedupe so repeated imports stay
# idempotent.
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

__all__: list[str] = []
