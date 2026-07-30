"""Exchange agents directory.

TradeDesk discovers agents by scanning this directory for files matching
``x_<exchange>_agent.py``. Each agent module exposes:

- ``name`` (str): canonical exchange name (e.g. ``"hyperliquid"``)
- ``list_accounts()`` -> list[str | dict]
- ``capabilities()`` -> list[str]
- ``execute(request: dict) -> CanonicalResponse``

This package is intentionally empty. The ``agents/`` directory exists
solely to be scanned by ``TradeDesk``.
"""
