"""TCP server for MT5 EA connections."""

import asyncio
import logging
from typing import Callable, List, Optional

from ..protocol import (
    Request,
    Response,
    serialize_request,
    parse_response,
    MessageBuffer,
)

logger = logging.getLogger(__name__)

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 5555
READ_TIMEOUT_SECONDS = 1.0
COMMAND_TIMEOUT_SECONDS = 30.0


class MT5Connection:
    """Handles a single MT5 EA connection."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter
    ):
        self._reader = reader
        self._writer = writer
        self._buffer = MessageBuffer()
        self._pending_requests: dict[str, asyncio.Future] = {}
        self._connected = True
        self._client_address = writer.get_extra_info("peername")
        self.login = ""

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def client_address(self) -> tuple:
        return self._client_address

    async def send_request(self, request: Request) -> Response:
        """Send a request and wait for response."""
        if not self._connected:
            raise ConnectionError("Not connected to MT5 EA")

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_requests[request.request_id] = future

        try:
            data = serialize_request(request)
            self._writer.write(data)
            await self._writer.drain()
            logger.debug(f"Sent request: {request.action.value} [{request.request_id}]")

            response = await asyncio.wait_for(future, timeout=COMMAND_TIMEOUT_SECONDS)
            return response
        except asyncio.TimeoutError:
            logger.error(f"Request timed out: {request.request_id}")
            raise TimeoutError(f"Request {request.action.value} timed out")
        finally:
            self._pending_requests.pop(request.request_id, None)

    async def receive_loop(self) -> None:
        """Continuously read responses from MT5 EA."""
        while self._connected:
            try:
                data = await asyncio.wait_for(
                    self._reader.read(4096),
                    timeout=READ_TIMEOUT_SECONDS
                )
                if not data:
                    logger.info("Connection closed by MT5 EA")
                    self._connected = False
                    break

                self._buffer.append(data)

                while True:
                    message = self._buffer.extract_message()
                    if message is None:
                        break
                    await self._handle_message(message)

            except asyncio.TimeoutError:
                continue
            except Exception as error:
                logger.error(f"Error in receive loop: {error}")
                self._connected = False
                break

        self._cancel_pending_requests()

    async def _handle_message(self, data: bytes) -> None:
        """Process a complete message from MT5 EA."""
        response = parse_response(data)
        if response is None:
            logger.warning("Failed to parse response")
            return

        future = self._pending_requests.get(response.request_id)
        if future and not future.done():
            future.set_result(response)
        else:
            logger.warning(f"Received response for unknown request: {response.request_id}")

    def _cancel_pending_requests(self) -> None:
        """Cancel all pending requests on disconnect."""
        for request_id, future in self._pending_requests.items():
            if not future.done():
                future.set_exception(ConnectionError("Connection lost"))
        self._pending_requests.clear()

    async def close(self) -> None:
        """Close the connection."""
        self._connected = False
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:
            logger.debug("connection close failed", exc_info=True)


class MT5Server:
    """TCP server that accepts multiple MT5 EA connections."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        on_connect: Optional[Callable[[MT5Connection], None]] = None,
        on_disconnect: Optional[Callable[[MT5Connection], None]] = None
    ):
        self._host = host
        self._port = port
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._server: Optional[asyncio.Server] = None
        self._connections: List[MT5Connection] = []
        self._receive_tasks: List[asyncio.Task] = []

    @property
    def connection(self) -> Optional[MT5Connection]:
        live = self.connections
        return live[0] if live else None

    @property
    def connections(self) -> List[MT5Connection]:
        return [c for c in self._connections if c.is_connected]

    @property
    def is_connected(self) -> bool:
        return bool(self.connections)

    async def start(self) -> None:
        """Start the TCP server."""
        self._server = await asyncio.start_server(
            self._handle_client,
            self._host,
            self._port
        )
        logger.info(f"MT5 Server listening on {self._host}:{self._port}")

    async def stop(self) -> None:
        """Stop the server and close connections."""
        for task in list(self._receive_tasks):
            task.cancel()
        for task in list(self._receive_tasks):
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._receive_tasks.clear()

        for connection in list(self._connections):
            await connection.close()
        self._connections.clear()

        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            logger.info("MT5 Server stopped")

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter
    ) -> None:
        """Handle a new client connection."""
        connection = MT5Connection(reader, writer)
        self._connections.append(connection)
        logger.info(f"MT5 EA connected from {connection.client_address}")

        if self._on_connect:
            self._on_connect(connection)

        task = asyncio.create_task(connection.receive_loop())
        self._receive_tasks.append(task)

        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            if self._on_disconnect:
                self._on_disconnect(connection)
            if connection in self._connections:
                self._connections.remove(connection)
            if task in self._receive_tasks:
                self._receive_tasks.remove(task)
            logger.info("MT5 EA disconnected")

    async def wait_for_connection(self, timeout: float = 60.0) -> MT5Connection:
        """Wait until at least one MT5 EA connects."""
        start = asyncio.get_event_loop().time()
        while not self.is_connected:
            if asyncio.get_event_loop().time() - start > timeout:
                raise TimeoutError("Timed out waiting for MT5 connection")
            await asyncio.sleep(0.1)
        live = self.connections
        if not live:
            raise TimeoutError("Timed out waiting for MT5 connection")
        return live[0]
