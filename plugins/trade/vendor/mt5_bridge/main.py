"""Main entry point for MT5 Bridge server."""

import asyncio
import logging
import signal
from argparse import ArgumentParser

from . import MT5Bridge, Timeframe

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


async def demo_trading_session(bridge: MT5Bridge) -> None:
    """Demonstrate bridge functionality after connection."""
    logger.info("Starting demo trading session...")

    try:
        account = await bridge.get_account()
        logger.info(f"Account: Balance={account.balance}, Equity={account.equity}")

        tick = await bridge.get_tick("EURUSD")
        logger.info(f"EURUSD: Bid={tick.bid}, Ask={tick.ask}")

        rates = await bridge.get_rates("EURUSD", Timeframe.H1, 10)
        logger.info(f"Got {len(rates)} H1 candles for EURUSD")

        positions = await bridge.get_positions()
        logger.info(f"Open positions: {len(positions)}")

    except Exception as error:
        logger.error(f"Demo session error: {error}")


async def main(host: str, port: int) -> None:
    """Run the MT5 bridge server."""
    bridge = MT5Bridge(host=host, port=port)
    shutdown_event = asyncio.Event()

    def signal_handler():
        logger.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    await bridge.start()
    logger.info(f"MT5 Bridge server started on {host}:{port}")
    logger.info("Waiting for MT5 EA to connect...")

    try:
        while not shutdown_event.is_set():
            if not bridge.is_connected:
                try:
                    await asyncio.wait_for(
                        bridge.wait_for_connection(),
                        timeout=5.0
                    )
                    await demo_trading_session(bridge)
                except asyncio.TimeoutError:
                    continue

            await asyncio.sleep(1.0)

            if bridge.is_connected:
                alive = await bridge.heartbeat()
                if not alive:
                    logger.warning("Lost connection to MT5 EA")

    except asyncio.CancelledError:
        pass
    finally:
        await bridge.stop()
        logger.info("MT5 Bridge server stopped")


def run() -> None:
    """CLI entry point."""
    parser = ArgumentParser(description="MT5-Python Socket Bridge Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=1111, help="Port to listen on")
    args = parser.parse_args()

    asyncio.run(main(args.host, args.port))


if __name__ == "__main__":
    run()
