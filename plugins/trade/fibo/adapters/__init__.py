"""Fibo exchange adapters — thin translation layer over KAM x_<exchange>_agent.

The strategy engine lives in ``plugins/trade/fibo/engine.py`` and is
exchange-agnostic. It talks to a ``ExchangeAdapter`` Protocol (declared in
``engine``). This package provides the concrete implementations.

ARCHITECTURE RULE (per locked spec section 4):

  Fibo owns STRATEGY LOGIC only.
  ALL exchange communication goes through x_<exchange>_agent.py.
  The Fibo adapter is a translation layer — it MUST NOT duplicate:
    - authentication / signing
    - credential handling
    - account alias handling
    - symbol / instrument resolution
    - HTTP request implementation
    - exchange endpoint implementation
    - quantity / price formatting
    - market-order / TP / SL API implementation

If Fibo requires a capability the agent does not expose, the MINIMUM
reusable capability is added to the agent itself.

Currently OndoPerps is the only supported exchange. Pacifica, Hyperliquid,
Arcus, etc. will follow the same pattern when their Phase 2 work begins.
"""

from .ondoperps import OndoPerpsFiboAdapter

__all__ = ["OndoPerpsFiboAdapter"]
