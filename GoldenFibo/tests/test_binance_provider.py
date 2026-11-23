from __future__ import annotations

import unittest
from unittest import mock

from goldenfibo.marketdata.binance_provider import BinanceHistoricalProvider
from goldenfibo.marketdata.historical import HistoricalRequest


class BinanceProviderTests(unittest.TestCase):
    def test_adapter_preserves_market_and_row_shape_without_changing_engine(self) -> None:
        source = mock.Mock()
        source.fetch_range.return_value = [[1, 2, 3, 1, 2, 5, 60, 10, 1, 2, 3, "0"]]
        provider = BinanceHistoricalProvider(mock.Mock(), source=source)
        request = HistoricalRequest("BTCUSDT", 1, 61_000)
        rows = provider.fetch_range(request, market="futures")
        self.assertEqual(1, len(rows))
        source.fetch_range.assert_called_once()
        self.assertEqual("BTCUSDT", source.fetch_range.call_args.kwargs["symbol"])
        self.assertEqual("futures", source.fetch_range.call_args.kwargs["market"])
        self.assertEqual("1m", source.fetch_range.call_args.kwargs["interval"])


if __name__ == "__main__":
    unittest.main()
