from __future__ import annotations

import ast
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
CANONICAL_REL = "plugins/trade/canonical.py"
AGENT_DIR = ROOT / "plugins" / "trade" / "agents"


EXPECTED_LADDER_AGENTS = {
    "x_aftermath_agent.py",  # Aftermath
    "x_apex_agent.py",  # Apex
    "x_arcus_agent.py",  # Arcus
    "x_bulk_agent.py",  # Bulk
    "x_edgex_agent.py",  # EdgeX
    "x_hibachi_agent.py",  # Hibachi
    "x_hyperliquid_agent.py",  # Hyperliquid
    "x_lighter_agent.py",  # Lighter
    "x_metatrader_agent.py",  # MetaTrader
    "x_mexc_agent.py",  # MEXC perp
    "x_mexc_agent_spot.py",  # MEXC spot
    "x_nado_agent.py",  # Nado
    "x_ondoperps_agent.py",  # OndoPerps
    "x_pacifica_agent.py",  # Pacifica
    "x_perpl_agent.py",  # Perpl
    "x_phemex_agent.py",  # Phemex
    "x_qfex_agent.py",  # QFEX
    "x_raydium_agent.py",  # Raydium
    "x_rise_agent.py",  # Rise
}


def _head_canonical_source() -> str:
    return subprocess.check_output(
        ["git", "show", f"HEAD:{CANONICAL_REL}"],
        cwd=ROOT,
        text=True,
    )


def _head_canonical_ladder_fields() -> set[str]:
    tree = ast.parse(_head_canonical_source(), filename=f"HEAD:{CANONICAL_REL}")
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "CanonicalLadderResult":
            return {
                stmt.target.id
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            }
    raise AssertionError("HEAD CanonicalLadderResult class not found")


def _constructor_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "CanonicalLadderResult":
            calls.append(node)
        elif isinstance(func, ast.Attribute) and func.attr == "CanonicalLadderResult":
            calls.append(node)
    return calls


class CanonicalLadderContractAuditTests(unittest.TestCase):
    def test_every_agent_constructor_uses_only_head_canonical_kwargs(self) -> None:
        """Audit against committed HEAD canonical.py, not dirty worktree imports."""
        allowed = _head_canonical_ladder_fields()
        self.assertIn("requested_order_count", allowed)
        self.assertNotIn("batch_plan", allowed)
        self.assertNotIn("expected_children", allowed)

        violations: list[str] = []
        files_with_ladder_constructors: set[str] = set()
        for path in sorted(AGENT_DIR.glob("x_*_agent*.py")):
            calls = _constructor_calls(path)
            if not calls:
                continue
            files_with_ladder_constructors.add(path.name)
            for call in calls:
                for keyword in call.keywords:
                    if keyword.arg is not None and keyword.arg not in allowed:
                        violations.append(
                            f"{path.relative_to(ROOT)}:{call.lineno} unsupported kwarg {keyword.arg!r}"
                        )

        missing = EXPECTED_LADDER_AGENTS - files_with_ladder_constructors
        self.assertFalse(missing, f"expected ladder constructors were not audited: {sorted(missing)}")
        self.assertEqual([], violations)


if __name__ == "__main__":
    unittest.main()
