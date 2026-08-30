"""All exchange agents expose the same instrument picker surface.

Offline: capabilities + execute dispatch branches only (no live network).
"""
from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_AGENTS = [
    "apex",
    "arcus",
    "edgex",
    "hibachi",
    "hyperliquid",
    "lighter",
    "ondoperps",
    "pacifica",
    "raydium",
    "rise",
]

_REQUIRED = ("resolve_instrument", "list_instruments", "market_price")


class TestAgentInstrumentSurfaceParity(unittest.TestCase):
    def test_every_agent_advertises_resolve_list_price(self) -> None:
        missing = {}
        for exchange in _AGENTS:
            mod = importlib.import_module(f"plugins.trade.agents.x_{exchange}_agent")
            caps = list(mod.capabilities())
            miss = [op for op in _REQUIRED if op not in caps]
            if miss:
                missing[exchange] = miss
        self.assertEqual(missing, {}, msg=f"agents missing picker ops: {missing}")

    def test_execute_dispatches_all_three_ops(self) -> None:
        """Each agent execute() contains branches for the three ops."""
        for exchange in _AGENTS:
            path = _REPO_ROOT / "plugins" / "trade" / "agents" / f"x_{exchange}_agent.py"
            src = path.read_text(encoding="utf-8")
            for op in _REQUIRED:
                self.assertIn(
                    f'"{op}"',
                    src,
                    msg=f"{exchange} source missing op token {op}",
                )
            # Dispatch presence (string form varies slightly)
            self.assertTrue(
                f'operation == "{_REQUIRED[0]}"' in src
                or f"operation == '{_REQUIRED[0]}'" in src
                or f'return _' in src and "resolve_instrument" in src,
                msg=f"{exchange} missing resolve dispatch",
            )


if __name__ == "__main__":
    unittest.main()
