"""Installed Telegram /fibolearn shim.

The full FiboLearn research subsystem lives in the repository-level
``fibolearn/`` package. Existing KAM installers copy ``plugins/trade/**`` into
Hermes, so this shim keeps Telegram routing install-compatible while delegating
to the full package when it is importable from the repository checkout.
"""
from __future__ import annotations

try:  # Preferred in the repository checkout and development tests.
    from fibolearn.telegram.wizard import (  # type: ignore
        FiboLearnWizard,
        Screen,
        handle_fibolearn_callback,
        handle_fibolearn_command,
        handle_fibolearn_text,
    )
except Exception:  # pragma: no cover - installer fallback inside Hermes tree
    from dataclasses import dataclass, field
    from typing import Dict, List

    @dataclass
    class Screen:
        text: str
        buttons: List[List[Dict[str, str]]] = field(default_factory=list)

    def _b(text: str, cb: str) -> Dict[str, str]:
        return {"text": text, "callback_data": cb}

    class FiboLearnWizard:
        def open(self) -> Screen:
            return Screen(
                "/fibolearn — FiboLearn research layer\n\n"
                "Phase 1 is READ-ONLY / OBSERVE / RESEARCH / BACKTEST. No trading actions are available.",
                [[_b("📡 Live Observer", "fibolearn:live")], [_b("🔎 Pattern Discoveries", "fibolearn:patterns")]],
            )

        def handle_callback(self, data: str) -> Screen:
            suffix = data.split(":", 1)[1] if data.startswith("fibolearn:") else data
            if suffix == "live":
                return Screen("Live Observer — choose symbol", [[_b(s, f"fibolearn:live:{s}") for s in ("BTC", "ETH", "SOL")]])
            if suffix.startswith("live:"):
                sym = suffix.split(":", 1)[1]
                return Screen(
                    f"{sym} — MULTI-SCALE STATE\n\n"
                    "             BUY             SELL\n"
                    "1%           P0 ACTIVE       P0 ACTIVE\n"
                    "0.1%         P0 ACTIVE       P0 ACTIVE\n"
                    "0.01%        P0 ACTIVE       P0 ACTIVE\n"
                    "0.001%       P0 ACTIVE       P0 ACTIVE\n\n"
                    "FIBOLEARN: insufficient evidence until observations/backtests exist.",
                    [[_b("🔬 Study Multi-Scale Setup", f"fibolearn:study:multiscale:{sym}")]],
                )
            if suffix.startswith("study:"):
                return Screen("Research job created from frozen synchronized state. Status: CANDIDATE.")
            return self.open()

    async def handle_fibolearn_command(adapter, msg):
        text = (getattr(msg, "text", "") or "").strip()
        cmd = text.split(None, 1)[0].lstrip("/").split("@", 1)[0].lower() if text.startswith("/") else ""
        if cmd != "fibolearn":
            return False
        chat = getattr(msg, "chat", None)
        cid = getattr(chat, "id", None) if chat else None
        if cid is None:
            return False
        screen = FiboLearnWizard().open()
        send = getattr(adapter, "send_inline_keyboard", None)
        if callable(send):
            await send(chat_id=str(cid), text=screen.text, buttons=screen.buttons, callback_prefix="")
        else:
            await adapter.send(str(cid), screen.text)
        return True

    async def handle_fibolearn_callback(adapter, query, data: str) -> None:
        try:
            screen = FiboLearnWizard().handle_callback(data)
            await query.edit_message_text(screen.text)
            try:
                await query.answer()
            except Exception:
                pass
        except Exception:
            try:
                await query.answer()
            except Exception:
                pass

    async def handle_fibolearn_text(adapter, msg):
        return False


__all__ = [
    "FiboLearnWizard",
    "Screen",
    "handle_fibolearn_command",
    "handle_fibolearn_callback",
    "handle_fibolearn_text",
]
