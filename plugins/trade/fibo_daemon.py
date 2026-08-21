from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
from pathlib import Path
from typing import List

from .fibo_service import (
    FiboCycleLedger,
    FiboSocketServiceHost,
    PersistentFiboService,
    resolve_fibo_event_log_path,
    resolve_fibo_ledger_path,
    resolve_fibo_socket_path,
    resolve_fibo_state_path,
)

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the persistent KAM Fibo service daemon")
    parser.add_argument("--socket-path", default=str(resolve_fibo_socket_path()))
    parser.add_argument("--state-path", default=str(resolve_fibo_state_path()))
    parser.add_argument("--ledger-path", default=str(resolve_fibo_ledger_path()))
    parser.add_argument("--event-log-path", default=str(resolve_fibo_event_log_path()))
    parser.add_argument(
        "--check",
        action="store_true",
        help="Instantiate the service offline, emit a startup JSON summary, and exit.",
    )
    return parser


def main(argv: List[str] | None = None) -> int:
    _configure_logging()
    args = build_arg_parser().parse_args(argv)

    service = PersistentFiboService(
        state_path=Path(args.state_path),
        ledger=FiboCycleLedger(Path(args.ledger_path)),
        event_log_path=Path(args.event_log_path),
        start_thread=not args.check,
    )
    if args.check:
        payload = service.execute_command({"op": "list"})
        print(
            json.dumps(
                {
                    "ok": True,
                    "socket_path": str(Path(args.socket_path)),
                    "state_path": str(Path(args.state_path)),
                    "ledger_path": str(Path(args.ledger_path)),
                    "event_log_path": str(Path(args.event_log_path)),
                    "registrations": payload.get("registrations") or [],
                },
                ensure_ascii=False,
            )
        )
        service.shutdown()
        return 0

    host = FiboSocketServiceHost(service=service, socket_path=Path(args.socket_path))

    def _stop(_signum: int, _frame) -> None:  # type: ignore[no-untyped-def]
        logger.info("Stopping fibo daemon")
        host.request_stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    logger.info("Starting fibo daemon on %s", args.socket_path)
    try:
        host.serve_forever()
    finally:
        host.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
