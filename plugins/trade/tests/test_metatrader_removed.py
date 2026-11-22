from __future__ import annotations

from pathlib import Path
import tempfile
from typing import cast

from plugins.trade.tradedesk import TradeDesk
from plugins.trade.wizard import TradeWizard


ROOT = Path(__file__).resolve().parents[3]
FORBIDDEN_EXCHANGES = {"metatrader", "mt5linux", "mt5", "mt4"}


class _Desk:
    def __init__(self) -> None:
        self.account_lookups: list[str] = []

    def list_exchanges(self) -> list[str]:
        return ["apex", "hyperliquid"]

    def list_accounts(self, exchange: str) -> list[str]:
        self.account_lookups.append(exchange)
        return ["live"]


def _callback_values(screen) -> set[str]:
    return {
        button["callback_data"]
        for row in screen.buttons
        for button in row
        if "callback_data" in button
    }


def test_metatrader_agent_modules_are_absent_and_undiscoverable() -> None:
    agent_dir = ROOT / "plugins" / "trade" / "agents"
    forbidden_files = {
        "x_metatrader_agent.py",
        "x_mt5linux_agent.py",
        "x_mt5_agent.py",
        "x_mt4_agent.py",
    }
    assert forbidden_files.isdisjoint({path.name for path in agent_dir.glob("x_*_agent.py")})
    assert FORBIDDEN_EXCHANGES.isdisjoint(set(TradeDesk().list_exchanges()))


def test_stale_deployed_agent_files_are_never_discovered() -> None:
    """A stale agent file on disk must not expose a retired venue.

    The installer prunes the retired agents, but discovery also refuses
    them so a deployment whose prune was skipped cannot route to them.
    """
    from plugins.trade import tradedesk

    with tempfile.TemporaryDirectory(prefix="stale-agent-") as tmp:
        agents_dir = Path(tmp)
        for name in (
            "x_metatrader_agent.py",
            "x_mt5linux_agent.py",
            "x_mt5_agent.py",
            "x_mt4_agent.py",
        ):
            (agents_dir / name).write_text("# stale\n", encoding="utf-8")
        # A live agent must still be discovered.
        (agents_dir / "x_apex_agent.py").write_text("# live\n", encoding="utf-8")

        found = [p.name for p in tradedesk._iter_agent_files(agents_dir)]

    assert "x_metatrader_agent.py" not in found
    assert "x_mt5linux_agent.py" not in found
    assert "x_mt5_agent.py" not in found
    assert "x_mt4_agent.py" not in found
    assert "x_apex_agent.py" in found


def test_trade_menu_is_dynamic_and_stale_metatrader_callbacks_fail_closed() -> None:
    desk = _Desk()
    wizard = TradeWizard(cast(TradeDesk, desk))
    chat_key = (123,)

    first = wizard.open(chat_key)
    callbacks = _callback_values(first)
    assert "exchange:apex" in callbacks
    assert "exchange:hyperliquid" in callbacks
    assert all(not any(name in callback.lower() for name in FORBIDDEN_EXCHANGES) for callback in callbacks)

    stale = wizard.handle_callback(chat_key, "exchange:metatrader")
    assert stale.state == "select_exchange"
    assert _callback_values(stale) == callbacks
    assert desk.account_lookups == []


def test_stale_mt5_callback_is_rejected_without_touching_account_lookup() -> None:
    """A stale ``exchange:mt5`` callback must fail closed.

    The MT5 venue was removed from the repository, but a deployment that
    still carries ``x_mt5_agent.py`` could otherwise be addressed by a
    callback left over from an earlier session.
    """
    desk = _Desk()
    wizard = TradeWizard(cast(TradeDesk, desk))
    chat_key = (456,)

    first = wizard.open(chat_key)
    callbacks = _callback_values(first)

    stale = wizard.handle_callback(chat_key, "exchange:mt5")
    assert stale.state == "select_exchange"
    assert _callback_values(stale) == callbacks
    assert desk.account_lookups == []


def test_mt5_agent_and_bridge_are_absent_from_the_repository() -> None:
    """The MT5 venue must leave no agent module or bridge payload behind."""
    assert not (ROOT / "plugins" / "trade" / "agents" / "x_mt5_agent.py").exists()
    assert not (ROOT / "plugins" / "trade" / "mt5_bridge").exists()


def test_installer_prunes_the_mt5_agent_and_bridge_on_upgrade() -> None:
    """The install path must remove a stale MT5 agent and bridge payload."""
    import sys

    installer_dir_path = str(ROOT / "installer")
    if installer_dir_path not in sys.path:
        sys.path.insert(0, installer_dir_path)
    from retired_payload import RETIRED_PAYLOAD_DIRS, RETIRED_PAYLOAD_FILES, prune_retired_payload

    assert Path("agents/x_mt5_agent.py") in RETIRED_PAYLOAD_FILES
    assert Path("mt5_bridge") in RETIRED_PAYLOAD_DIRS

    with tempfile.TemporaryDirectory(prefix="mt5-prune-") as tmp:
        plugin_root = Path(tmp)
        (plugin_root / "agents").mkdir()
        (plugin_root / "agents" / "x_mt5_agent.py").write_text("# stale\n", encoding="utf-8")
        live_agent = plugin_root / "agents" / "x_apex_agent.py"
        live_agent.write_text("# live\n", encoding="utf-8")
        (plugin_root / "mt5_bridge").mkdir()
        (plugin_root / "mt5_bridge" / "HermesBridge.mq5").write_text("// ea\n", encoding="utf-8")

        actions = {entry["path"]: entry["action"] for entry in prune_retired_payload(plugin_root)}

        # Only retired paths are reported; a live agent is never touched.
        assert "agents/x_apex_agent.py" not in actions
        assert live_agent.exists()

    assert actions["agents/x_mt5_agent.py"] == "removed"
    assert actions["mt5_bridge"] == "removed"


def test_installer_has_no_metatrader_or_mt4_capability_surface() -> None:
    installer_dir = ROOT / "installer"
    active_files = [
        installer_dir / "installer.py",
        installer_dir / "install_trade_capability.py",
        installer_dir / "uninstall_trade_capability.py",
        installer_dir / "verify_trade_capability.py",
        installer_dir / "adapter_wiring.py",
        installer_dir / "patchspecs.py",
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in active_files)
    # The live installer modules must carry no reference to the removed
    # feature. The removal-only specs that strip an orphaned seam from an
    # already-deployed adapter live in their own module (retired_seams.py).
    lowered = text.lower()
    for token in ("mt5linux", "metatrader", "x_mt5", "x_metatrader", "handle_fibo_"):
        assert token not in lowered

    # "fibo_wizard" must not appear in any live installer module.
    assert "fibo_wizard" not in lowered

    # The retired /fibo Telegram seams are still referenced by the
    # removal-only specs (as inert text-matching sentinels, never as an
    # import) so an already-deployed adapter can be stripped of a block
    # that would otherwise import the deleted plugins.trade.fibo_wizard.
    # Those specs must never be part of an install path.
    import sys
    installer_dir_path = str(installer_dir)
    if installer_dir_path not in sys.path:
        sys.path.insert(0, installer_dir_path)
    import patchspecs
    import retired_seams
    install_specs = patchspecs.specs_for_capabilities(["trade"])
    retired_specs = retired_seams.retired_fibo_adapter_specs()
    install_seams = {spec.seam for spec in install_specs}
    retired_seam_names = {spec.seam for spec in retired_specs}
    assert retired_seam_names.isdisjoint(install_seams)
    assert not install_specs == retired_specs

    assert not (installer_dir / "mt5linux_provision.py").exists()
    assert not (installer_dir / "install_fibo_capability.py").exists()
    assert not (installer_dir / "verify_fibo_capability.py").exists()
    assert not (installer_dir / "uninstall_fibo_capability.py").exists()
    assert not (ROOT / "plugins" / "trade" / "fibo_wizard.py").exists()
    assert not (ROOT / "plugins" / "trade" / "fibo").exists()


def test_fibolearn_remains_wired_to_trade_without_fibo_callback_namespace() -> None:
    import sys

    sys.path.insert(0, str(ROOT / "installer"))
    from patchspecs import specs_for_capabilities

    sentinels = {spec.native_sentinel for spec in specs_for_capabilities(["trade"])}
    assert "from plugins.trade.fibolearn_wizard import handle_fibolearn_command" in sentinels
    assert "from plugins.trade.fibolearn_wizard import handle_fibolearn_callback" in sentinels
    assert "from plugins.trade.fibo_wizard import handle_fibo_command" not in sentinels
    assert "from plugins.trade.fibo_wizard import handle_fibo_callback" not in sentinels
