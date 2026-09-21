from goldenfibo.marketdata.aggtrade_cache import AggTradeCache


def test_archive_csv_preserves_buyer_is_maker_side(tmp_path):
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    rows = cache._parse_aggtrade_csv(b"10,100.5,0.25,1,1,1234,true\n11,101.5,0.75,2,2,1235,false\n")

    assert rows[0].buyer_is_maker is True
    assert rows[1].buyer_is_maker is False
