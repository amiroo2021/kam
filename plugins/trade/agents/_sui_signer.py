"""Sui Ed25519 signer for the Aftermath Agent Wallet.

Decodes the configured ``suiprivkey...`` bech32 secret and signs Sui
``Transaction`` bytes with the Ed25519 scheme flag byte (0x00) expected by
Sui's ``UserSignature`` enum.

The signer is intentionally minimal:
- Decodes bech32 ``suiprivkey`` (HRP ``suiprivkey``, witness version 0).
- Derives the 32-byte ed25519 secret key from the 33-byte ``flag || seed``.
- Signs with ``nacl.signing.SigningKey`` and prepends the Sui Ed25519 flag.
- Never logs the seed, secret key, or signed bytes. Errors are sanitized.
"""

from __future__ import annotations

import base64
from typing import Tuple

import nacl.signing
from nacl.encoding import RawEncoder

try:  # Use Sui's reference decoder when available to avoid bech32 drift.
    import pysui_fastcrypto as _pfc

    def _decode_sui_keystring(secret: str) -> Tuple[int, bytes, bytes]:
        return _pfc.decode_bech32(secret, "suiprivkey")

except Exception:  # pragma: no cover - fallback only when pysui is missing.
    _pfc = None

    def _decode_sui_keystring(secret: str) -> Tuple[int, bytes, bytes]:  # type: ignore[no-redef]
        raise RuntimeError("pysui_fastcrypto is required to decode suiprivkey strings")


_AFTERMATH_SUIPRIVKEY_HRP = "suiprivkey"
_ED25519_FLAG = b"\x00"


class SuiSigningError(Exception):
    """Raised when the configured key cannot be decoded or signing fails."""


def _secret_redacted(secret: str) -> str:
    text = str(secret or "")
    if not text:
        return "[EMPTY_SECRET]"
    return f"[REDACTED:{len(text)}chars]"


def decode_suiprivkey(secret: str) -> bytes:
    """Decode a Sui ``suiprivkey...`` string to a 32-byte ed25519 seed.

    Delegates to ``pysui_fastcrypto.decode_bech32`` so the implementation
    always matches the Aftermath-supplied wallet format.
    """
    text = str(secret or "").strip()
    if not text:
        raise SuiSigningError("empty secret")
    try:
        scheme, pub, prv = _decode_sui_keystring(text)
    except Exception as exc:  # noqa: BLE001
        raise SuiSigningError(f"failed to decode suiprivkey: {type(exc).__name__}: {exc}") from exc
    if scheme != 0:
        raise SuiSigningError(f"unsupported key scheme {scheme}; only ed25519 (0) is supported")
    if not pub or not prv:
        raise SuiSigningError("suiprivkey decoder returned empty key material")
    # pfc returns the 32-byte raw private seed (no flag prefix); we still
    # confirm the public key is present so callers can verify the address.
    _ = pub
    return bytes(prv)[:32]


class SuiSigner:
    """Ed25519 signer bound to the Aftermath Agent Wallet."""

    def __init__(self, secret: str) -> None:
        try:
            seed = decode_suiprivkey(secret)
        except SuiSigningError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SuiSigningError(f"failed to decode suiprivkey: {type(exc).__name__}") from exc
        self._signing_key = nacl.signing.SigningKey(seed, encoder=RawEncoder)
        self._verify_key = self._signing_key.verify_key

    @property
    def address(self) -> str:
        return base64.b64encode(bytes(self._verify_key)).decode()

    def user_signature(self, tx_bytes: bytes) -> str:
        """Sign ``tx_bytes`` and return the Sui ``UserSignature`` base64 string.

        Sui's ``UserSignature`` for ed25519 is the scheme flag byte ``0x00``
        followed by the 64-byte ed25519 signature over the TX bytes.
        """
        if not isinstance(tx_bytes, (bytes, bytearray)):
            raise SuiSigningError("tx_bytes must be bytes")
        signature = self._signing_key.sign(bytes(tx_bytes), encoder=RawEncoder).signature
        return base64.b64encode(_ED25519_FLAG + signature).decode()

    def sign_digest_b64(self, digest: bytes) -> str:
        """Sign a 32-byte Sui ``signingDigest`` and return the base64-encoded
        ed25519 ``UserSignature`` (``0x00`` flag + 64-byte signature).

        This is what Aftermath's ``/api/ccxt/build/*`` paths return when they
        want the client to sign the digest of the assembled Transaction. The
        signing operation is identical to ``user_signature``; the method is
        named separately to make the call site explicit.
        """
        return self.user_signature(digest)

    def serialized_user_signature(self, msg_or_digest: bytes) -> str:
        """Sign ``msg_or_digest`` and return the full Sui ``UserSignature``
        base64 string for an ed25519 signer.

        Layout (per ``sui_sdk_types::SimpleSignature`` BCS for ed25519):

        ``[0x00] || ed25519_signature(64 bytes) || ed25519_public_key(32 bytes)``

        Total 97 bytes, base64-encoded.

        Aftermath's ``/api/ccxt/submit/*`` requires this full 97-byte form;
        the Sui fullnode reconstructs the sender address from the embedded
        public key and verifies the 64-byte ed25519 signature against the
        32-byte digest (or, equivalently, against the 32-byte digest of
        ``intent || bcs(transaction)``).
        """
        if not isinstance(msg_or_digest, (bytes, bytearray)):
            raise SuiSigningError("msg_or_digest must be bytes")
        signature = self._signing_key.sign(bytes(msg_or_digest), encoder=RawEncoder).signature
        pubkey = bytes(self._verify_key)
        return base64.b64encode(_ED25519_FLAG + signature + pubkey).decode()

    def public_key_base64(self) -> str:
        return base64.b64encode(bytes(self._verify_key)).decode()


__all__ = ["SuiSigner", "SuiSigningError", "decode_suiprivkey"]
