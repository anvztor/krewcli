"""ERC-8004 Identity Registry interaction for GOAT Testnet3.

Handles:
  - Minting agent NFTs (register)
  - Checking if an agent is already registered under a wallet
  - Reading agent metadata
"""

from __future__ import annotations

import json
import logging
from typing import Any

from eth_account import Account
from web3 import Web3

logger = logging.getLogger(__name__)

# Minimal ABI — only the functions we need
_IDENTITY_REGISTRY_ABI: list[dict[str, Any]] = [
    {
        "inputs": [{"name": "agentURI", "type": "string"}],
        "name": "register",
        "outputs": [{"name": "agentId", "type": "uint256"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "agentId", "type": "uint256"}],
        "name": "ownerOf",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "agentId", "type": "uint256"}],
        "name": "tokenURI",
        "outputs": [{"name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "agentId", "type": "uint256"},
            {"indexed": False, "name": "agentURI", "type": "string"},
            {"indexed": True, "name": "owner", "type": "address"},
        ],
        "name": "Registered",
        "type": "event",
    },
]


def _get_contract(rpc_url: str, registry_address: str):
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    return w3, w3.eth.contract(
        address=Web3.to_checksum_address(registry_address),
        abi=_IDENTITY_REGISTRY_ABI,
    )


def build_agent_uri(
    agent_name: str,
    display_name: str,
    capabilities: list[str],
    endpoint_url: str | None = None,
) -> str:
    """Build an ERC-8004 agent registration JSON (returned as data: URI)."""
    registration = {
        "type": "https://eips.ethereum.org/EIPS/eip-8004#registration-v1",
        "name": display_name,
        "description": f"{display_name} agent managed by krewcli",
        "active": True,
        "services": [],
    }
    if endpoint_url:
        registration["services"].append({
            "name": "A2A",
            "endpoint": endpoint_url,
        })
    # Encode as data: URI so it's self-contained on-chain
    payload = json.dumps(registration, separators=(",", ":"))
    return f"data:application/json;base64,{__import__('base64').b64encode(payload.encode()).decode()}"


def get_agents_owned_by(
    rpc_url: str,
    registry_address: str,
    wallet_address: str,
) -> int:
    """Return how many agent NFTs this wallet owns."""
    w3, contract = _get_contract(rpc_url, registry_address)
    return contract.functions.balanceOf(
        Web3.to_checksum_address(wallet_address)
    ).call()


def list_agent_ids_for_wallet(
    rpc_url: str,
    registry_address: str,
    wallet_address: str,
) -> list[int]:
    """Return all agentIds owned by this wallet (from Registered events)."""
    w3, contract = _get_contract(rpc_url, registry_address)
    checksummed = Web3.to_checksum_address(wallet_address)

    # Scan Registered events filtered by owner (indexed param)
    logs = contract.events.Registered().get_logs(
        from_block=0,
        argument_filters={"owner": checksummed},
    )

    # Verify current ownership (NFTs could have been transferred)
    agent_ids = []
    for log in logs:
        aid = log["args"]["agentId"]
        try:
            current_owner = contract.functions.ownerOf(aid).call()
            if current_owner.lower() == checksummed.lower():
                agent_ids.append(aid)
        except Exception:
            pass  # token may have been burned

    return agent_ids


def register_agent(
    rpc_url: str,
    registry_address: str,
    private_key: str,
    agent_uri: str,
    chain_id: int = 48816,
) -> int:
    """Mint a new ERC-8004 agent NFT.

    Returns the agentId from the Registered event.
    """
    w3, contract = _get_contract(rpc_url, registry_address)
    acct = Account.from_key(private_key)
    address = Web3.to_checksum_address(acct.address)

    # Build transaction
    tx = contract.functions.register(agent_uri).build_transaction({
        "from": address,
        "nonce": w3.eth.get_transaction_count(address),
        "chainId": chain_id,
        "gas": 300_000,
        "gasPrice": w3.eth.gas_price,
    })

    # Sign and send
    signed = w3.eth.account.sign_transaction(tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    logger.info("ERC-8004 register tx sent: %s", tx_hash.hex())

    # Wait for receipt
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    if receipt["status"] != 1:
        raise RuntimeError(f"ERC-8004 register tx failed: {tx_hash.hex()}")

    # Extract agentId from Registered event
    logs = contract.events.Registered().process_receipt(receipt)
    if not logs:
        raise RuntimeError("No Registered event found in tx receipt")

    agent_id = logs[0]["args"]["agentId"]
    logger.info("Agent registered: agentId=%d, owner=%s", agent_id, address)
    return agent_id
