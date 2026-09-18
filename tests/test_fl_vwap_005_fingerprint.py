from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from fibolearn.devtools.fl_vwap_005_fingerprint import canonical_validation_fingerprint


def _make_db(path: Path, rows: list[tuple]) -> None:
    con = sqlite3.connect(str(path))
    con.execute(
        '''
        create table candles(
            symbol text not null,
            open_time integer not null,
            open text not null,
            high text not null,
            low text not null,
            close text not null,
            volume text not null,
            close_time integer not null,
            quote_volume text not null,
            trades integer not null,
            taker_buy_base text not null,
            taker_buy_quote text not null,
            source text not null,
            market text not null,
            timeframe text not null
        )
        '''
    )
    con.executemany(
        'insert into candles values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        rows,
    )
    con.commit()
    con.close()


def _rows():
    return [
        ('BTC', 1, '1', '2', '0.5', '1.5', '10', 60, '15', 1, '5', '7', 'binance', 'spot', '1m'),
        ('BTC', 61, '1.5', '2.5', '1', '2', '20', 120, '30', 1, '10', '15', 'binance', 'spot', '1m'),
        ('ETH', 1, '10', '11', '9', '10.5', '3', 60, '31.5', 1, '1', '2', 'binance', 'spot', '1m'),
    ]


def test_identical_logical_rows_same_fingerprint_across_physical_layouts(tmp_path):
    a = tmp_path / 'a.sqlite'
    b = tmp_path / 'b.sqlite'
    rows = _rows()
    _make_db(a, rows)
    _make_db(b, rows)
    # Create a physically different file by enabling WAL and doing a VACUUM into a new copy.
    con = sqlite3.connect(str(b))
    con.execute('pragma journal_mode=WAL')
    con.execute('pragma synchronous=NORMAL')
    con.execute('vacuum')
    con.close()
    assert canonical_validation_fingerprint(a) == canonical_validation_fingerprint(b)


def test_single_primitive_field_change_changes_fingerprint(tmp_path):
    a = tmp_path / 'a.sqlite'
    b = tmp_path / 'b.sqlite'
    rows = _rows()
    _make_db(a, rows)
    changed = []
    for i, r in enumerate(rows):
        if i == 0:
            rr = list(r)
            rr[1] = 2  # open_time change
            changed.append(tuple(rr))
        else:
            changed.append(r)
    _make_db(b, changed)
    assert canonical_validation_fingerprint(a) != canonical_validation_fingerprint(b)


def test_validation_db_reproduces_frozen_fingerprint():
    db = Path('/root/kam/fibolearn/data/fl_vwap_005_binance_validation.sqlite')
    assert canonical_validation_fingerprint(db) == '75a7af08a21d9ffe1731079e37f198b1663fcd5c4da4d3dcedcb9d7d7480cd60'
