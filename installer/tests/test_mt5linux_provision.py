"""Unit tests for MT5Linux provisioner (default-on, no live container mutation)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_INSTALLER = _HERE.parent
if str(_INSTALLER) not in sys.path:
    sys.path.insert(0, str(_INSTALLER))


class DiscoverAndEnableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="mt5linux_prov_"))
        (self.home / ".env").write_text(
            "mt5linux_LITE7486706_ACCOUNT=7486706\n"
            "mt5linux_LITE7486706_SERVER=LiteFinance-MT5-Live\n"
            "mt5linux_LITE7486706_PASSWORD=secret-lite\n"
            "mt5linux_PO5701520_ACCOUNT=5701520\n"
            "mt5linux_PO5701520_SERVER=PoTrade-MT5\n"
            "mt5linux_PO5701520_PASSWORD=secret-po\n"
            "mt5linux_PUPRIME33849951_ACCOUNT=33849951\n"
            "mt5linux_PUPRIME33849951_SERVER=PUPrime-Live2\n"
            "mt5linux_PUPRIME33849951_PASSWORD=secret-pup\n"
            "MT5LINUX_POTRADE5701520_ACCOUNT=5701520\n"
            "MT5LINUX_POTRADE5701520_SERVER=PoTrade-MT5\n",
            encoding="utf-8",
        )

    def test_discover_dedupes_po_and_hides_passwords(self) -> None:
        import mt5linux_provision as p

        accounts = p.discover_configured_accounts(self.home)
        self.assertEqual(set(accounts), {"LITE7486706", "PO5701520", "PUPRIME33849951"})
        self.assertNotIn("POTRADE5701520", accounts)
        blob = json.dumps(accounts)
        self.assertNotIn("secret-", blob)
        self.assertTrue(accounts["LITE7486706"]["has_password"])

    def test_enabled_default_on_without_credentials(self) -> None:
        import mt5linux_provision as p

        empty = Path(tempfile.mkdtemp())
        (empty / ".env").write_text("", encoding="utf-8")
        with mock.patch.object(p, "combined_env", return_value={}):
            en, reason = p.mt5linux_enabled(hermes_home=empty, force=None)
        self.assertTrue(en, msg=reason)
        self.assertIn("default-on", reason)

        en2, reason2 = p.mt5linux_enabled(hermes_home=empty, force=False)
        self.assertFalse(en2)
        self.assertIn("no-mt5linux", reason2)

        en3, reason3 = p.mt5linux_enabled(hermes_home=empty, force=True)
        self.assertTrue(en3)
        self.assertIn("mt5linux", reason3)

    def test_install_succeeds_with_zero_accounts(self) -> None:
        import mt5linux_provision as p

        empty = Path(tempfile.mkdtemp())
        (empty / ".env").write_text("", encoding="utf-8")
        with mock.patch.object(p, "combined_env", return_value={}):
            with mock.patch.object(p, "ensure_docker", return_value={"ok": True, "action": "exists"}):
                with mock.patch.object(
                    p,
                    "provision_container",
                    return_value={"action": "reused", "ok": True, "container": "mt5", "credentials_in_docker": False},
                ):
                    with mock.patch.object(p, "ensure_client_venv", return_value={"ok": True, "action": "ready", "site_packages": str(empty)}):
                        with mock.patch.object(p, "ensure_gateway_import_path", return_value={"ok": True}):
                            with mock.patch.object(
                                p,
                                "verify_rpyc_initialize",
                                return_value={"ok": True, "needs_manual_login": False, "output": "INIT True"},
                            ):
                                rec = p.install_mt5linux(
                                    hermes_root=Path("/usr/local/lib/hermes-agent"),
                                    hermes_home=empty,
                                    dry_run=False,
                                    force=None,
                                )
        self.assertTrue(rec["enabled"])
        self.assertTrue(rec["ok"])
        self.assertEqual(rec["accounts_configured_count"], 0)
        self.assertEqual((rec.get("account_verify") or {}).get("accounts_configured"), [])

    def test_broker_auth_failure_does_not_fail_install(self) -> None:
        import mt5linux_provision as p

        with mock.patch.object(p, "ensure_docker", return_value={"ok": True, "action": "exists"}):
            with mock.patch.object(
                p,
                "provision_container",
                return_value={"action": "reused", "ok": True, "container": "mt5"},
            ):
                with mock.patch.object(p, "ensure_client_venv", return_value={"ok": True, "action": "ready", "site_packages": "/tmp"}):
                    with mock.patch.object(p, "ensure_gateway_import_path", return_value={"ok": True}):
                        with mock.patch.object(
                            p,
                            "verify_rpyc_initialize",
                            return_value={"ok": True, "needs_manual_login": False, "output": "INIT True"},
                        ):
                            with mock.patch.object(
                                p,
                                "verify_accounts_readonly",
                                return_value={"ok": False, "needs_manual_login": True, "error": "LOGIN_FAILED"},
                            ):
                                rec = p.install_mt5linux(
                                    hermes_root=Path("/usr/local/lib/hermes-agent"),
                                    hermes_home=self.home,
                                    dry_run=False,
                                    force=None,
                                )
        self.assertTrue(rec["ok"])
        self.assertTrue(rec.get("manual_login_required"))
        self.assertFalse(rec.get("broker_auth_ok"))

    def test_docker_run_cmd_has_no_broker_secrets(self) -> None:
        import mt5linux_provision as p

        with mock.patch.object(p, "container_is_healthy", return_value=(False, "missing", None)):
            with mock.patch.object(p, "ensure_volume", return_value={"name": "mt5-wine", "action": "exists"}):
                with mock.patch.object(p, "ensure_entrypoint_on_host", return_value=self.home / "entrypoint.sh"):
                    rec = p.provision_container(hermes_home=self.home, dry_run=True)
        self.assertEqual(rec["action"], "would-create")
        self.assertFalse(rec["credentials_in_docker"])
        cmd = " ".join(rec["docker_run_cmd"])
        self.assertNotIn("MT5_LOGIN", cmd)
        self.assertNotIn("MT5_PASSWORD", cmd)
        self.assertNotIn("secret-", cmd)
        self.assertNotIn("7486706", cmd)

    def test_reuses_healthy_container(self) -> None:
        import mt5linux_provision as p

        with mock.patch.object(
            p, "container_is_healthy", return_value=(True, "running and RPyC reachable", {"Id": "x"})
        ):
            rec = p.provision_container(hermes_home=self.home, dry_run=False)
        self.assertEqual(rec["action"], "reused")

    def test_uninstall_preserves_volume_by_default(self) -> None:
        import mt5linux_provision as p

        with mock.patch.object(p, "_container_inspect", return_value=None):
            rec = p.uninstall_mt5linux(hermes_home=self.home, dry_run=True, purge_volume=False)
        self.assertEqual(rec["volume"]["action"], "preserved")
        rec2 = p.uninstall_mt5linux(hermes_home=self.home, dry_run=True, purge_volume=True)
        self.assertEqual(rec2["volume"]["action"], "would-purge")

    def test_ensure_docker_reuses_existing(self) -> None:
        import mt5linux_provision as p

        with mock.patch.object(p, "docker_available", return_value=(True, "docker ok")):
            rec = p.ensure_docker(dry_run=False)
        self.assertEqual(rec["action"], "exists")
        self.assertTrue(rec["ok"])

    def test_ensure_docker_dry_run_install_plan(self) -> None:
        import mt5linux_provision as p

        with mock.patch.object(p, "docker_available", return_value=(False, "missing")):
            with mock.patch.object(p, "detect_docker_package_state", return_value={
                "installed": {},
                "has_docker_ce": False,
                "has_docker_io": False,
                "has_podman_docker": False,
            }):
                with mock.patch.object(p, "_read_os_release", return_value={"ID": "ubuntu", "ID_LIKE": "debian"}):
                    with mock.patch.object(p, "shutil") as sh:
                        # keep real which for nothing - patch which via shutil.which on module
                        pass
                with mock.patch("mt5linux_provision.shutil.which", side_effect=lambda x: "/usr/bin/apt-get" if x == "apt-get" else None):
                    with mock.patch.object(p, "_dpkg_status", return_value=None):
                        rec = p.ensure_docker(dry_run=True)
        self.assertTrue(rec["ok"])
        self.assertEqual(rec["action"], "would-install-docker.io")
        self.assertIn("docker.io", " ".join(rec["install_cmd"]))

    def test_ensure_docker_refuses_podman_conflict(self) -> None:
        import mt5linux_provision as p

        with mock.patch.object(p, "docker_available", return_value=(False, "info failed")):
            with mock.patch.object(p, "detect_docker_package_state", return_value={
                "installed": {"podman-docker": "install ok installed"},
                "has_docker_ce": False,
                "has_docker_io": False,
                "has_podman_docker": True,
            }):
                with mock.patch("mt5linux_provision.shutil.which", return_value="/usr/bin/docker"):
                    rec = p.ensure_docker(dry_run=False)
        self.assertFalse(rec["ok"])
        self.assertEqual(rec["action"], "blocked-conflict")

    def test_no_mt5linux_skips_entirely(self) -> None:
        import mt5linux_provision as p

        with mock.patch.object(p, "ensure_docker") as ed:
            rec = p.install_mt5linux(
                hermes_root=Path("/tmp"),
                hermes_home=self.home,
                dry_run=True,
                force=False,
            )
        self.assertFalse(rec["enabled"])
        self.assertEqual(rec["action"], "skipped")
        ed.assert_not_called()

    def test_verify_empty_accounts_is_ok(self) -> None:
        import mt5linux_provision as p

        empty = Path(tempfile.mkdtemp())
        (empty / ".env").write_text("", encoding="utf-8")
        with mock.patch.object(p, "combined_env", return_value={}):
            with mock.patch.object(p, "docker_available", return_value=(True, "docker ok")):
                with mock.patch.object(p, "container_is_healthy", return_value=(True, "healthy", {"Id": "x"})):
                    with mock.patch.object(p, "_run", return_value=type("C", (), {"returncode": 0, "stdout": "1.1.1\n", "stderr": ""})()):
                        # agent file must exist — use real hermes root if present else skip hard check
                        root = Path("/usr/local/lib/hermes-agent")
                        if not (root / "plugins/trade/agents/x_mt5linux_agent.py").is_file():
                            self.skipTest("no installed agent")
                        ok, msgs = p.verify_mt5linux(hermes_root=root, hermes_home=empty, force=None)
        self.assertTrue(ok)
        self.assertTrue(any("empty list" in m for m in msgs))


class InstallerFlagTests(unittest.TestCase):
    def test_help_mentions_default_on(self) -> None:
        from installer import installer as inst
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(SystemExit) as ctx:
                inst._parse_args(["--help"], "install")
        self.assertEqual(ctx.exception.code, 0)
        text = buf.getvalue()
        self.assertIn("default-on", text.lower() or text)
        self.assertIn("--no-mt5linux", text)

    def test_mt5linux_flag_still_parses(self) -> None:
        from installer import installer as inst

        ns = inst._parse_args(["--trade", "--mt5linux", "--dry-run", "--no-restart", "--skip-deps"], "install")
        self.assertTrue(ns.mt5linux)
        self.assertFalse(ns.no_mt5linux)
        self.assertEqual(inst._mt5linux_argv(ns), ["--mt5linux"])


if __name__ == "__main__":
    unittest.main()
