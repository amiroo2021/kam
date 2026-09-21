# Secret / credential checklist (NAMES ONLY)

Never put values in Git, migration bundles, or HANDOFF docs.

## Classification legend

- **SECRET_MANUAL_RESTORE** — supply on new server out-of-band
- **RECREATE_ON_NEW_SERVER** — regenerate / re-auth
- **SAFE_TO_PACKAGE** — non-secret structure only

## Hermes

| Name | Class | Notes |
|------|-------|-------|
| Hermes provider API keys / OAuth tokens | SECRET_MANUAL_RESTORE | `/root/.hermes/auth.json` |
| `OPENROUTER_API_KEY` | SECRET_MANUAL_RESTORE | if used |
| `KIMI_API_KEY` | SECRET_MANUAL_RESTORE | if used |
| xAI / OpenAI / Anthropic credentials | SECRET_MANUAL_RESTORE | provider-dependent |
| `/root/.hermes/config.yaml` non-secret structure | SAFE_TO_PACKAGE (redacted) | recreate; strip secrets |
| `/root/.hermes/.env` | SECRET_MANUAL_RESTORE | env file |

## Telegram / Photon messaging

| Name | Class |
|------|-------|
| `TELEGRAM_BOT_TOKEN` | SECRET_MANUAL_RESTORE |
| `TELEGRAM_ALLOWED_USERS` | SECRET_MANUAL_RESTORE (PII/ids) |
| `TELEGRAM_HOME_CHANNEL` | RECREATE_ON_NEW_SERVER / SECRET_MANUAL_RESTORE |
| `MT4_READER_BOT_TOKEN` | SECRET_MANUAL_RESTORE |
| `MT4_READER_CHAT_ID` | SECRET_MANUAL_RESTORE |
| `PHOTON_PROJECT_ID` | SECRET_MANUAL_RESTORE |
| `PHOTON_PROJECT_SECRET` | SECRET_MANUAL_RESTORE |
| `PHOTON_ALLOWED_USERS` | SECRET_MANUAL_RESTORE |
| `PHOTON_HOME_CHANNEL` | SECRET_MANUAL_RESTORE |

## Exchange / trading (only if new server trades)

| Name | Class |
|------|-------|
| `APEX_FIBO_APIKEY` / `APEX_FIBO_APIKEYSECRET` / passphrase / seeds | SECRET_MANUAL_RESTORE |
| `HYPERLIQUID_FIBO_SECRET` / `HYPERLIQUID_FIBO_WALLET` | SECRET_MANUAL_RESTORE |
| `QFEX_AMIROO_SECRET_KEY` | SECRET_MANUAL_RESTORE |
| `MEXC_AMIROO_SECRETKEY` | SECRET_MANUAL_RESTORE |
| `NADO_BITGET_PRIVATE_KEY` | SECRET_MANUAL_RESTORE |
| `PERPL_BITGET_API_KEY` | SECRET_MANUAL_RESTORE |
| `RAYDIUM_PHANTOM_API_KEY` | SECRET_MANUAL_RESTORE |

## SSH / host

| Name | Class |
|------|-------|
| GitHub deploy keys / user SSH private keys | SECRET_MANUAL_RESTORE — never package |
| `id_rsa*` / `id_ed25519*` | SECRET_MANUAL_RESTORE |

## FiboLearn research data

| Name | Class |
|------|-------|
| `fibolearn.sqlite` | SAFE_TO_PACKAGE (no API secrets; research observations) |
| FL-VWAP-005 validation candle DB | SAFE_TO_PACKAGE |
| Report JSON under `fibolearn/reports/` | SAFE_TO_PACKAGE (already mostly in Git) |

## Rule

If unsure whether a file contains a secret: **do not package it**. List it here and restore manually.
