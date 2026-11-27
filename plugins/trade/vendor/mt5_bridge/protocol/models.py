"""Protocol models for MT5-Python socket communication."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
import uuid


class Action(Enum):
    """Supported command actions."""
    TRADE = "TRADE"
    GET_DATA = "GET_DATA"
    GET_TICK = "GET_TICK"
    GET_ACCOUNT = "GET_ACCOUNT"
    GET_POSITIONS = "GET_POSITIONS"
    CLOSE_POSITION = "CLOSE_POSITION"
    GET_SYMBOLS = "GET_SYMBOLS"
    GET_SYMBOL_INFO = "GET_SYMBOL_INFO"
    GET_ORDERS = "GET_ORDERS"
    MODIFY_POSITION = "MODIFY_POSITION"
    CANCEL_ORDER = "CANCEL_ORDER"
    MODIFY_ORDER = "MODIFY_ORDER"
    HEARTBEAT = "HEARTBEAT"


class OrderType(Enum):
    """Order types for trading."""
    BUY = "BUY"
    SELL = "SELL"
    BUY_LIMIT = "BUY_LIMIT"
    SELL_LIMIT = "SELL_LIMIT"
    BUY_STOP = "BUY_STOP"
    SELL_STOP = "SELL_STOP"


class Timeframe(Enum):
    """Chart timeframes."""
    M1 = "M1"
    M5 = "M5"
    M15 = "M15"
    M30 = "M30"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"
    W1 = "W1"
    MN1 = "MN1"


@dataclass
class Request:
    """Command request sent to MT5 EA."""
    action: Action
    params: dict[str, Any] = field(default_factory=dict)
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_json_dict(self) -> dict:
        return {
            "id": self.request_id,
            "action": self.action.value,
            "params": self.params
        }


@dataclass
class EATiming:
    """Timing data from EA for latency measurement."""
    t2_receive: int = 0  # EA receive timestamp (microseconds, relative)
    t3_execute: int = 0  # EA execute complete timestamp (microseconds, relative)

    @classmethod
    def from_json_dict(cls, data: dict) -> "EATiming":
        return cls(
            t2_receive=data.get("t2_receive", 0),
            t3_execute=data.get("t3_execute", 0)
        )


@dataclass
class Response:
    """Response received from MT5 EA."""
    request_id: str
    success: bool
    error_code: int
    error_message: str
    data: dict[str, Any]
    timing: Optional[EATiming] = None

    @classmethod
    def from_json_dict(cls, data: dict) -> "Response":
        timing_data = data.get("timing")
        timing = EATiming.from_json_dict(timing_data) if timing_data else None

        return cls(
            request_id=data.get("id", ""),
            success=data.get("success", False),
            error_code=data.get("error_code", -1),
            error_message=data.get("error_message", ""),
            data=data.get("data", {}),
            timing=timing
        )


@dataclass
class TradeParams:
    """Parameters for TRADE command."""
    symbol: str
    order_type: OrderType
    volume: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    price: Optional[float] = None
    magic: int = 0
    comment: str = ""

    def to_dict(self) -> dict:
        params = {
            "symbol": self.symbol,
            "type": self.order_type.value,
            "volume": self.volume
        }
        if self.stop_loss is not None:
            params["sl"] = self.stop_loss
        if self.take_profit is not None:
            params["tp"] = self.take_profit
        if self.price is not None:
            params["price"] = self.price
        if self.magic:
            params["magic"] = self.magic
        if self.comment:
            params["comment"] = self.comment
        return params


@dataclass
class DataParams:
    """Parameters for GET_DATA command."""
    symbol: str
    timeframe: Timeframe
    count: int

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe.value,
            "count": self.count
        }


@dataclass
class OrderResult:
    """Result of a trade execution."""
    ticket: int
    price_executed: float
    volume_executed: float

    @classmethod
    def from_response_data(cls, data: dict) -> "OrderResult":
        return cls(
            ticket=data.get("ticket", 0),
            price_executed=data.get("price_executed", 0.0),
            volume_executed=data.get("volume_executed", 0.0)
        )


@dataclass
class TickData:
    """Current price tick."""
    bid: float
    ask: float
    last: float
    volume: int
    time: str

    @classmethod
    def from_response_data(cls, data: dict) -> "TickData":
        return cls(
            bid=data.get("bid", 0.0),
            ask=data.get("ask", 0.0),
            last=data.get("last", 0.0),
            volume=data.get("volume", 0),
            time=data.get("time", "")
        )


@dataclass
class AccountInfo:
    """Trading account information."""
    balance: float
    equity: float
    margin: float
    free_margin: float
    leverage: int

    @classmethod
    def from_response_data(cls, data: dict) -> "AccountInfo":
        return cls(
            balance=data.get("balance", 0.0),
            equity=data.get("equity", 0.0),
            margin=data.get("margin", 0.0),
            free_margin=data.get("free_margin", 0.0),
            leverage=data.get("leverage", 0)
        )


@dataclass
class Position:
    """Open trading position."""
    ticket: int
    symbol: str
    order_type: str
    volume: float
    open_price: float
    stop_loss: float
    take_profit: float
    profit: float

    @classmethod
    def from_response_data(cls, data: dict) -> "Position":
        return cls(
            ticket=data.get("ticket", 0),
            symbol=data.get("symbol", ""),
            order_type=data.get("type", ""),
            volume=data.get("volume", 0.0),
            open_price=data.get("open_price", 0.0),
            stop_loss=data.get("sl", 0.0),
            take_profit=data.get("tp", 0.0),
            profit=data.get("profit", 0.0)
        )


@dataclass
class CloseResult:
    """Result of closing a position."""
    close_price: float
    profit: float

    @classmethod
    def from_response_data(cls, data: dict) -> "CloseResult":
        return cls(
            close_price=data.get("close_price", 0.0),
            profit=data.get("profit", 0.0)
        )
