# Aftermath native SL/TP transaction fixtures

Reference artifacts used by `test_aftermath_agent.py` to prove the agent's
pure-Python Sui transaction assembly matches the official SDK.

* `aftermath_native_sl_tp_kind.b64` - the base64 `TransactionKind` returned by
  Aftermath's `POST /api/perpetuals/account/transactions/place-sl-tp-orders`
  for the ZEC short position (account 672, market
  `0x435987c9e1b8f61a4cdfa220751b3324ebf5a7bfd1250b25c79412a71a20392f`,
  TP 500). The kind's last Move call is
  `stop_orders::create_stop_order_ticket`.
* `aftermath_native_sl_tp_reference_txdata.b64` - the BCS `TransactionData`
  bytes for that kind, assembled with the official `@mysten/sui` v3.0.1
  `Transaction.fromKind(...)` + `build()` path, using sender
  `0x38a460af94ba659bbcdd24e4043aa7f0e5e50a4ad55639c1bcae582571daf564`,
  chain identifier `4btiuiMPvEENsttpZC7CZ53DruC3MAgfznDbASZ7DR6S`,
  epoch 1318 (max 1319), gas price 1000, budget 100000000, address-balance
  gas mode (empty payment list + `ValidDuring` expiration), nonce 0.
* `aftermath_native_sl_tp_reference_digest.txt` - the Sui signing digest
  (`blake2b-256` over intent `0,0,0` || the transaction data) for those bytes.

These are captured artifacts, not live data: they contain no credentials and
no private key material.
