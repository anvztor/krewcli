"""SIWE login flow for krewcli → krewhub authentication.

Flow:
  1. Load private key from ~/.krewcli/wallet
  2. GET /api/v1/auth/nonce from krewhub
  3. Build SIWE message with nonce
  4. Sign with wallet key
  5. POST /api/v1/auth/siwe/verify → get JWT
  6. Save JWT to ~/.krewcli/token
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct
from siwe import SiweMessage

from krewcli.auth.token_store import save_token
from krewcli.auth.wallet import load_private_key


def siwe_login(krewhub_url: str, private_key: str | None = None) -> dict:
    """Perform SIWE login against krewhub and save the JWT.

    Args:
        krewhub_url: Base URL of krewhub (e.g. http://127.0.0.1:8420)
        private_key: Hex private key. If None, loads from ~/.krewcli/wallet.

    Returns:
        Dict with wallet_address, session_id, expires_at.

    Raises:
        ValueError: If no private key available or login fails.
    """
    key = private_key or load_private_key()
    if key is None:
        raise ValueError(
            "No wallet found. Run 'krewcli wallet create' or 'krewcli wallet import' first."
        )

    acct = Account.from_key(key)

    with httpx.Client(timeout=10) as http:
        # 1. Get nonce
        nonce_resp = http.get(f"{krewhub_url}/api/v1/auth/nonce")
        nonce_resp.raise_for_status()
        nonce = nonce_resp.json()["nonce"]

        # 2. Build SIWE message (GOAT Testnet3, chain_id=48816)
        msg = SiweMessage(
            domain="krewcli",
            address=acct.address,
            uri=krewhub_url,
            version="1",
            chain_id=48816,
            nonce=nonce,
            issued_at=datetime.now(timezone.utc).isoformat(),
        )
        message_text = msg.prepare_message()

        # 3. Sign
        signable = encode_defunct(text=message_text)
        signed = Account.sign_message(signable, private_key=key)
        signature = "0x" + signed.signature.hex()

        # 4. Verify with krewhub
        verify_resp = http.post(
            f"{krewhub_url}/api/v1/auth/siwe/verify",
            json={"message": message_text, "signature": signature},
        )
        if verify_resp.status_code != 200:
            detail = verify_resp.json().get("detail", "Login failed")
            raise ValueError(f"SIWE verification failed: {detail}")

        data = verify_resp.json()

        # 5. Save JWT
        save_token(data["token"])

        return {
            "wallet_address": data["wallet_address"],
            "session_id": data["session_id"],
            "expires_at": data["expires_at"],
        }
