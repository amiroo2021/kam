from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from plugins.trade.fibo_service import FiboSocketServiceHost, PersistentFiboService, SocketFiboServiceClient  # noqa: E402
from plugins.trade.tests.test_fibo_service import FakeAgent, FakeRunner, FakeTradeDesk  # noqa: E402


class DummyManager:
    def __init__(self) -> None:
        self._engines: Dict[str, Any] = {}

    def list_running(self) -> List[Any]:
        return []

    def poll_once(self) -> None:
        return None

    def is_running(self, key: str) -> bool:
        return False


class DummyRunner:
    def __init__(self) -> None:
        self.manager = DummyManager()
        self.stop_requested = False

    def _log(self, event: str, **fields: Any) -> None:
        return None


class FiboIpcTests(unittest.TestCase):
    def _wait_for_socket(self, socket_path: Path) -> None:
        deadline = time.time() + 3
        while time.time() < deadline and not socket_path.exists():
            time.sleep(0.05)
        self.assertTrue(socket_path.exists(), socket_path)

    def test_socket_client_can_query_empty_service(self) -> None:
        with TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            socket_path = tmpdir / "service.sock"
            service = PersistentFiboService(
                runner=DummyRunner(),  # type: ignore[arg-type]
                state_path=tmpdir / "service_state.json",
                event_log_path=tmpdir / "events.jsonl",
                start_thread=False,
            )
            host = FiboSocketServiceHost(service=service, socket_path=socket_path)
            thread = threading.Thread(target=host.serve_forever, daemon=True)
            thread.start()
            self._wait_for_socket(socket_path)
            client = SocketFiboServiceClient(socket_path=socket_path, timeout=2)
            deadline = time.time() + 3
            result = {"ok": False, "error": "SERVICE_UNAVAILABLE"}
            while time.time() < deadline:
                result = client.execute_command({"op": "list"})
                if result.get("ok"):
                    break
                time.sleep(0.05)
            self.assertTrue(result["ok"])
            self.assertEqual(result["registrations"], [])
            host.shutdown()
            thread.join(timeout=2)

    def test_start_list_detail_over_ipc_and_client_restart(self) -> None:
        with TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            socket_path = tmpdir / "service.sock"
            agent = FakeAgent()
            runner = FakeRunner(agent)
            service = PersistentFiboService(
                tradedesk=FakeTradeDesk(),  # type: ignore[arg-type]
                runner=runner,  # type: ignore[arg-type]
                state_path=tmpdir / "service_state.json",
                event_log_path=tmpdir / "events.jsonl",
                start_thread=False,
            )
            host = FiboSocketServiceHost(service=service, socket_path=socket_path)
            thread = threading.Thread(target=host.serve_forever, daemon=True)
            thread.start()
            self._wait_for_socket(socket_path)

            client_a = SocketFiboServiceClient(socket_path=socket_path, timeout=2)
            start = client_a.execute_command(
                {
                    "op": "start",
                    "exchange": "ondoperps",
                    "account": "amiroo",
                    "instrument": "ONDO",
                    "counter_side": "counterSELL",
                    "divide_percent": 100.0,
                    "counter1": 1.0,
                    "counter2": 0.0,
                    "counter3": 0.0,
                    "counter4": 0.0,
                    "poll_seconds": 2.0,
                }
            )
            self.assertTrue(start["ok"])
            key = start["registration_key"]
            listed = client_a.execute_command({"op": "list"})
            self.assertEqual({row["registration_key"] for row in listed["registrations"]}, {key})

            client_b = SocketFiboServiceClient(socket_path=socket_path, timeout=2)
            detail = client_b.execute_command({"op": "detail", "registration_key": key})
            self.assertTrue(detail["ok"])
            self.assertEqual(detail["detail"]["registration_key"], key)
            self.assertEqual(detail["detail"]["status"], "running")
            self.assertEqual(detail["detail"]["counter_side"], "counterSELL")
            host.shutdown()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
