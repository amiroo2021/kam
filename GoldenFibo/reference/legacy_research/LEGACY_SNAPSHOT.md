# Legacy research snapshot — DO NOT MODIFY

This directory is a **read-only archival copy** of the recovered `golden-fibo` **0.1.0**
research package (SHA256 of source archive:
`17f05d05b1d3bd3fe020bb536b6935e1aa37951b95765d98da7c2c04f7670eec`).

It preserves the original implementation used by historical backtests and the
Telegram backtest wizard imports (`replay_ohlc`, `ladder_step`, `levels_p0_to_pn`).

Rules:
- Do **not** silently edit files here to "fix" the new engine.
- Runtime production code must **not** import from this path.
- Parity tests may import it only as a frozen oracle.
- The canonical engine lives under `goldenfibo/`.
