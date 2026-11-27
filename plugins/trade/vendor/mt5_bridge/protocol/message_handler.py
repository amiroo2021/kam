"""Message serialization and parsing for MT5 protocol."""

import json
import logging
from typing import Optional

from .models import Request, Response

logger = logging.getLogger(__name__)

MESSAGE_TERMINATOR = b"\n"
MAX_MESSAGE_SIZE = 1024 * 1024  # 1MB
ENCODING = "utf-8"


def serialize_request(request: Request) -> bytes:
    """Convert a Request to bytes for socket transmission."""
    json_str = json.dumps(request.to_json_dict())
    return json_str.encode(ENCODING) + MESSAGE_TERMINATOR


def parse_response(data: bytes) -> Optional[Response]:
    """Parse bytes from socket into a Response object."""
    try:
        text = data.decode(ENCODING).strip()
        if not text:
            return None
        json_data = json.loads(text)
        return Response.from_json_dict(json_data)
    except json.JSONDecodeError as error:
        logger.error(f"Failed to parse JSON response: {error}")
        return None
    except UnicodeDecodeError as error:
        logger.error(f"Failed to decode response bytes: {error}")
        return None


class MessageBuffer:
    """Buffer for accumulating partial messages from socket reads."""

    def __init__(self):
        self._buffer = b""

    def append(self, data: bytes) -> None:
        """Add received bytes to buffer."""
        self._buffer += data
        if len(self._buffer) > MAX_MESSAGE_SIZE:
            logger.warning("Buffer exceeded max size, clearing")
            self._buffer = b""

    def extract_message(self) -> Optional[bytes]:
        """Extract a complete message if available."""
        if MESSAGE_TERMINATOR in self._buffer:
            message, self._buffer = self._buffer.split(MESSAGE_TERMINATOR, 1)
            return message
        return None

    def clear(self) -> None:
        """Clear the buffer."""
        self._buffer = b""
