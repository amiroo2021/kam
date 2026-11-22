"""Tests for the Aftermath Agent's Sui Ed25519 signer.

These tests exercise the signer in isolation — they do NOT call Aftermath and
they do NOT touch the gateway service. They cover:

1. The signer decodes the configured ``suiprivkey`` to the same public key
   that the canonical ``pysui_fastcrypto.decode_bech32`` reference produces.
2. ``user_signature`` produces a 65-byte Sui ``UserSignature`` (1 flag byte
   + 64-byte Ed25519 signature) that verifies against the public key.
3. The agent's ``_sign_and_submit`` builds a well-formed
   ``SubmitTransactionRequest`` body (``{transactionBytes, signatures}``)
   when the build response is non-sponsored and when it is sponsored.

We use a deterministic, freshly generated keystring rather than the live
``AFTERMATH_BITGET_AGENT_PRIVATEKEY`` so the test never sees a real secret.
"""

from __future__ import annotations

import base64
import json
import os
import unittest
from pathlib import Path
from unittest import mock

import pysui_fastcrypto as pfc

from plugins.trade.agents._sui_signer import (
    SuiSigner,
    SuiSigningError,
    decode_suiprivkey,
)
from plugins.trade.agents import x_aftermath_agent as agent


def _fresh_suiprivkey() -> str:
    """Generate a fresh suiprivkey string via the reference encoder.

    Uses ``pysui_fastcrypto.encode_bech32`` so the test always exercises a
    string shape that Aftermath's runtime will accept.
    """
    import secrets

    seed = secrets.token_bytes(32)
    return pfc.encode_bech32(b"\x00" + seed, "suiprivkey")


class SuiSignerTests(unittest.TestCase):
    def test_decode_matches_reference(self) -> None:
        priv = _fresh_suiprivkey()
        scheme, pub_pfc, prv_pfc = pfc.decode_bech32(priv, "suiprivkey")
        seed = decode_suiprivkey(priv)
        self.assertEqual(len(seed), 32)
        self.assertEqual(seed, bytes(prv_pfc))
        signer = SuiSigner(priv)
        self.assertEqual(signer.public_key_base64(), base64.b64encode(bytes(pub_pfc)).decode())

    def test_user_signature_shape_and_verify(self) -> None:
        priv = _fresh_suiprivkey()
        signer = SuiSigner(priv)
        tx = b"some-tx-bytes"
        sig_b64 = signer.user_signature(tx)
        sig = base64.b64decode(sig_b64)
        self.assertEqual(len(sig), 65, "Sui UserSignature (sig-only) must be 1 flag + 64 sig")
        self.assertEqual(sig[0], 0, "flag byte must be 0x00 for ed25519")
        # Verify against PyNaCl directly using the derived public key.
        from nacl.signing import VerifyKey

        vk = VerifyKey(base64.b64decode(signer.public_key_base64()))
        vk.verify(tx, sig[1:])

    def test_serialized_user_signature_shape(self) -> None:
        """Aftermath's /api/ccxt/submit/* requires the FULL Sui UserSignature,
        which is ``[flag(1)] || [sig(64)] || [pubkey(32)]`` = 97 bytes for
        ed25519 (per ``sui_sdk_types::SimpleSignature`` BCS).
        """
        priv = _fresh_suiprivkey()
        signer = SuiSigner(priv)
        digest = b"0123456789abcdef0123456789abcdef"  # 32-byte signingDigest
        sig_b64 = signer.serialized_user_signature(digest)
        sig = base64.b64decode(sig_b64)
        self.assertEqual(len(sig), 97, "Sui ed25519 UserSignature must be 1 + 64 + 32 = 97 bytes")
        self.assertEqual(sig[0], 0, "flag byte must be 0x00 for ed25519")
        embedded_pubkey = sig[65:]  # last 32 bytes
        self.assertEqual(embedded_pubkey, base64.b64decode(signer.public_key_base64()),
                         "embedded pubkey must equal the signer's pubkey")
        # Verify the 64-byte signature against the digest + embedded pubkey.
        from nacl.signing import VerifyKey

        VerifyKey(embedded_pubkey).verify(digest, sig[1:65])

    def test_tampered_tx_fails_verification(self) -> None:
        priv = _fresh_suiprivkey()
        signer = SuiSigner(priv)
        tx = b"original-bytes"
        sig_b64 = signer.user_signature(tx)
        sig = base64.b64decode(sig_b64)
        from nacl.signing import VerifyKey

        vk = VerifyKey(base64.b64decode(signer.public_key_base64()))
        bad = bytearray(tx)
        bad[0] ^= 1
        with self.assertRaises(Exception):
            vk.verify(bytes(bad), sig[1:])


class SignAndSubmitTests(unittest.TestCase):
    """Verify ``_sign_and_submit`` against Aftermath's actual
    ``TransactionBuildResponse``/``SubmitTransactionRequest`` contract.

    Build responses are ``{transactionBytes, signingDigest}`` (no txKind).
    Submit payloads are ``{transactionBytes, signatures}``.
    """

    def _creds(self, priv: str) -> dict:
        scheme, pub, _ = pfc.decode_bech32(priv, "suiprivkey")
        return {
            "account": "bitget",
            "account_id": "0xACC",
            "address": base64.b64encode(bytes(pub)).decode(),
            "private_key": priv,
            "session": "deadbeef",
        }

    def _fake_build_response(self) -> dict:
        return {
            "transactionBytes": base64.b64encode(b"FAKE-TX-BYTES").decode(),
            "signingDigest": base64.b64encode(b"0123456789abcdef0123456789abcdef").decode(),
        }

    def test_nonsponsored_submit_body(self) -> None:
        priv = _fresh_suiprivkey()
        creds = self._creds(priv)
        captured: dict = {"calls": []}

        def fake_post(credentials, path, payload, timeout=15):
            captured["calls"].append((path, payload))
            if path == "/api/ccxt/build/createOrders":
                return self._fake_build_response()
            if path == "/api/ccxt/submit/createOrders":
                return {"digest": "0xSUBMITTEDDIGEST"}
            raise AssertionError(f"unexpected POST path: {path}")

        with mock.patch.object(agent, "_post_json", side_effect=fake_post):
            submit = agent._sign_and_submit(creds, "createOrders", {"anything": 1})
        # Two POSTs: build → submit.
        self.assertEqual([c[0] for c in captured["calls"]],
                         ["/api/ccxt/build/createOrders", "/api/ccxt/submit/createOrders"])
        body = captured["calls"][1][1]
        self.assertEqual(body["transactionBytes"], self._fake_build_response()["transactionBytes"])
        self.assertEqual(len(body["signatures"]), 1, "non-sponsored flow uses one sender signature")
        sig = base64.b64decode(body["signatures"][0])
        self.assertEqual(len(sig), 97, "Aftermath submit requires 97-byte Sui UserSignature (flag+sig+pubkey)")
        self.assertEqual(sig[0], 0, "ed25519 flag byte must be 0x00")
        self.assertEqual(submit["submit_response"]["digest"], "0xSUBMITTEDDIGEST")

    def test_sponsored_path_uses_sponsor_then_sender_signature(self) -> None:
        """Aftermath may attach a sponsor signature to the build response; the
        agent must preserve that signature at index 0 and append the sender
        signature at index 1.
        """
        priv = _fresh_suiprivkey()
        creds = self._creds(priv)
        sponsor_sig = base64.b64encode(b"\x00" + b"\x11" * 64).decode()
        captured: dict = {"body": None}

        def fake_post(credentials, path, payload, timeout=15):
            if path == "/api/ccxt/build/cancelOrders":
                return {
                    "transactionBytes": base64.b64encode(b"FAKE-TX").decode(),
                    "signingDigest": base64.b64encode(b"deadbeefdeadbeefdeadbeefdeadbeef").decode(),
                    "sponsorSignature": sponsor_sig,
                }
            captured["body"] = payload
            return {"digest": "0xSPONSOREDDIGEST"}

        with mock.patch.object(agent, "_post_json", side_effect=fake_post):
            submit = agent._sign_and_submit(creds, "cancelOrders", {})
        self.assertEqual(len(captured["body"]["signatures"]), 2, "sponsored flow → sponsor + sender")
        self.assertEqual(captured["body"]["signatures"][0], sponsor_sig)
        self.assertEqual(submit["submit_response"]["digest"], "0xSPONSOREDDIGEST")

    def test_address_mismatch_is_not_enforced_by_agent(self) -> None:
        """The agent trusts the configured ``AFTERMATH_<ALIAS>_AGENT_ADDRESS``
        and lets Aftermath's submit endpoint validate the signature.
        """
        priv = _fresh_suiprivkey()
        creds = self._creds(priv)
        creds["address"] = "0x" + "f" * 64  # unrelated Sui address
        with mock.patch.object(
            agent,
            "_post_json",
            side_effect=lambda c, p, b, timeout=15: (
                self._fake_build_response() if p.startswith("/api/ccxt/build/") else {"digest": "0xOK"}
            ),
        ):
            submit = agent._sign_and_submit(creds, "createOrders", {})
        self.assertEqual(submit["submit_response"]["digest"], "0xOK")

    def test_missing_signing_digest_raises(self) -> None:
        priv = _fresh_suiprivkey()
        creds = self._creds(priv)
        bad_resp = {"transactionBytes": base64.b64encode(b"x").decode()}  # no signingDigest
        with mock.patch.object(agent, "_post_json", return_value=bad_resp):
            with self.assertRaises(RuntimeError):
                agent._sign_and_submit(creds, "createOrders", {})


class DryRunDefaultTests(unittest.TestCase):
    def test_dry_run_default_without_env(self) -> None:
        # Strip env to confirm the function reads from os.environ AND the .env
        # fallback. We point _hermes_home() at an empty temp dir so a real
        # .env with AFTERMATH_DRY_RUN cannot leak into the test.
        env = {k: v for k, v in os.environ.items() if k != "AFTERMATH_DRY_RUN"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(agent, "_hermes_home", lambda: Path("/tmp/aftermath_dryrun_test_no_env")):
            Path("/tmp/aftermath_dryrun_test_no_env").mkdir(exist_ok=True)
            Path("/tmp/aftermath_dryrun_test_no_env/.env").write_text("")
            self.assertTrue(agent._is_dry_run({}))

    def test_dry_run_off_via_env(self) -> None:
        with mock.patch.dict(os.environ, {"AFTERMATH_DRY_RUN": "0"}, clear=False):
            self.assertFalse(agent._is_dry_run({}))

    def test_dry_run_override_in_request(self) -> None:
        with mock.patch.dict(os.environ, {"AFTERMATH_DRY_RUN": "0"}, clear=False):
            self.assertTrue(agent._is_dry_run({"dry_run": True}))


if __name__ == "__main__":
    unittest.main()
