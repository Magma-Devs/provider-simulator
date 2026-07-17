"""Redesigned provider-simulator core.

This package holds the new domain model (provider identity, config, telemetry,
topology, registry). It is additive: the existing flat modules (server.py,
handlers_*.py, ...) are untouched until later migration stories wire this
package into the running server and retire them.
"""
