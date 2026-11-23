"""Fresh-install documentation and WebUI pin consistency checks.

The Hermes WebUI is deliberately kept in its own upstream repository. These
tests pin the contract that makes a fresh droplet reproducible:

  * the tested upstream revision is recorded in the repo;
  * the documented clone step actually uses that recorded revision;
  * the documented install step is the only entry point allowed to manage
    ``webchat.service``;
  * no KAM source file modifies or vendors the WebUI.
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_PIN = _REPO_ROOT / "installer" / "webui_revision.json"
_DOC = _REPO_ROOT / "docs" / "NEW_SERVER_INSTALL.md"


class WebUiRevisionPinTests(unittest.TestCase):
    def test_pin_file_exists_and_is_valid_json(self) -> None:
        self.assertTrue(_PIN.is_file(), f"missing {_PIN}")
        data = json.loads(_PIN.read_text(encoding="utf-8"))
        for key in ("component", "repository", "checkout_root", "revision"):
            self.assertIn(key, data)
        self.assertEqual(data["component"], "hermes-webui")
        self.assertRegex(data["revision"], r"^[0-9a-f]{40}$")

    def test_pin_points_at_the_upstream_webui_repository(self) -> None:
        data = json.loads(_PIN.read_text(encoding="utf-8"))
        self.assertIn("nesquena/hermes-webui", data["repository"])

    def test_documented_clone_step_references_the_pin_file(self) -> None:
        doc = _DOC.read_text(encoding="utf-8")
        self.assertIn("installer/webui_revision.json", doc)

    def test_documented_install_step_is_the_only_webchat_entry_point(self) -> None:
        doc = _DOC.read_text(encoding="utf-8")
        self.assertIn("installer/install_webchat.py", doc)
        self.assertIn("--dry-run", doc)

    def test_doc_states_the_webui_must_stay_pristine(self) -> None:
        doc = _DOC.read_text(encoding="utf-8")
        self.assertIn("git status --porcelain", doc)
        self.assertIn("pristine", doc.lower())

    def test_doc_requires_a_password_before_starting_webchat(self) -> None:
        doc = _DOC.read_text(encoding="utf-8")
        self.assertIn("WEBCHAT_PASSWORD", doc)


class WebUiIsNotVendoredTests(unittest.TestCase):
    def test_repo_does_not_contain_a_webui_checkout(self) -> None:
        for name in ("webui", "hermes-webui"):
            self.assertFalse(
                (_REPO_ROOT / name).exists(),
                f"KAM must not vendor the WebUI ({name}/ found)",
            )

    def test_no_kam_source_imports_the_webui_package(self) -> None:
        # The WebUI stays an upstream checkout: KAM may reference its path or
        # repo URL to render the systemd unit, but must never import its code
        # or vendor its files.
        allowed_path_refs = {"trade_web_unit.py", "install_webchat.py"}
        this_file = Path(__file__).resolve()
        offenders: list[str] = []
        for path in _REPO_ROOT.rglob("*.py"):
            parts = path.relative_to(_REPO_ROOT).parts
            if parts[0] in {".git", ".venv", "node_modules"} or "site-packages" in parts:
                continue
            if path.resolve() == this_file:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            # An actual code import of the WebUI package.
            if re.search(r"^\s*(?:from|import)\s+(?:hermes_)?webui\b", text, re.MULTILINE):
                offenders.append(str(path.relative_to(_REPO_ROOT)))
            elif "hermes-webui" in text and path.name not in allowed_path_refs:
                offenders.append(str(path.relative_to(_REPO_ROOT)))
        self.assertEqual(offenders, [], f"unexpected WebUI references: {offenders}")

    def test_trade_install_paths_do_not_manage_webchat(self) -> None:
        # The installer must keep webchat.service out of routine trade paths.
        installer = _REPO_ROOT / "installer"
        for name in ("install_trade.py", "install_trade_capability.py",
                     "uninstall_trade.py", "uninstall_trade_capability.py"):
            path = installer / name
            self.assertTrue(path.is_file(), f"missing {name}")
            text = path.read_text(encoding="utf-8", errors="replace")
            self.assertNotRegex(
                text,
                r"systemctl[^\n]{0,80}webchat",
                f"{name} must never run systemctl against webchat.service",
            )


if __name__ == "__main__":
    unittest.main()
