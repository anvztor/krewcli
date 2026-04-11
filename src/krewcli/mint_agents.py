"""ERC-8004 agent NFT minting via ERC-4337 UserOperations.

Builds register(agentURI) UserOps for each agent, signed with the
session key. The human submits via handleOps() in cookrew.

The agentURI includes the hub.cookrew.dev A2A endpoint so the
on-chain identity points to the publicly-reachable A2A gateway.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from eth_abi import encode
from web3 import Web3

from krewcli.userop import build_execute_calldata, build_userop, sign_userop_with_session_key, userop_to_json

logger = logging.getLogger(__name__)

# ERC-8004 register(string agentURI) selector
REGISTER_SELECTOR = Web3.keccak(text="register(string)")[:4]


def build_agent_uri(
    display_name: str,
    owner: str,
    agent_name: str,
    capabilities: list[str],
    hub_base_url: str = "https://hub.cookrew.dev",
) -> str:
    """Build an ERC-8004 registration JSON as a data: URI.

    The A2A endpoint points to the hub gateway so the agent
    is discoverable on-chain.
    """
    registration = {
        "type": "https://eips.ethereum.org/EIPS/eip-8004#registration-v1",
        "name": display_name,
        "description": f"{display_name} on Cookrew",
        "active": True,
        "services": [
            {
                "name": "A2A",
                "endpoint": f"{hub_base_url}/a2a/{owner}/{agent_name}",
            },
        ],
    }
    payload = json.dumps(registration, separators=(",", ":"))
    return f"data:application/json;base64,{base64.b64encode(payload.encode()).decode()}"


def build_register_calldata(agent_uri: str) -> bytes:
    """Encode register(string agentURI) calldata for ERC-8004."""
    encoded = encode(["string"], [agent_uri])
    return REGISTER_SELECTOR + encoded


def build_mint_userops(
    agents: list[dict[str, Any]],
    smart_account: str,
    session_key_private: str,
    owner: str,
    identity_registry: str,
    rpc_url: str,
    entrypoint_address: str,
    hub_base_url: str = "https://hub.cookrew.dev",
) -> list[dict[str, Any]]:
    """Build signed UserOps to mint ERC-8004 agent NFTs.

    Each agent gets a register() call via the smart account's execute().

    Args:
        agents: list of {name, display_name, capabilities}
        smart_account: ERC-4337 smart account address
        session_key_private: hex private key for signing
        owner: cookbook owner (for A2A path)
        identity_registry: ERC-8004 Identity Registry address
        rpc_url: GOAT Testnet3 RPC
        entrypoint_address: EntryPoint v0.6 address
        hub_base_url: base URL for A2A endpoints

    Returns:
        list of signed UserOps (JSON-serializable)
    """
    signed_ops = []

    for agent in agents:
        name = agent["name"]
        display_name = agent["display_name"]
        capabilities = agent.get("capabilities", [])

        # Build agentURI with A2A endpoint
        agent_uri = build_agent_uri(
            display_name=display_name,
            owner=owner,
            agent_name=name,
            capabilities=capabilities,
            hub_base_url=hub_base_url,
        )

        # Build inner calldata: register(agentURI) on Identity Registry
        inner_calldata = build_register_calldata(agent_uri)

        # Build outer calldata: execute(registry, 0, inner_calldata) on smart account
        execute_calldata = build_execute_calldata(
            dest=identity_registry,
            value=0,
            inner_data=inner_calldata,
        )

        # Build UserOp
        userop = build_userop(
            smart_account=smart_account,
            calldata=execute_calldata,
            rpc_url=rpc_url,
            entrypoint_address=entrypoint_address,
        )

        # Sign with session key
        signed = sign_userop_with_session_key(
            userop=userop,
            session_key_private=session_key_private,
            rpc_url=rpc_url,
            entrypoint_address=entrypoint_address,
        )

        signed_ops.append({
            "agent_name": name,
            "display_name": display_name,
            "agent_uri": agent_uri,
            "userop": userop_to_json(signed),
        })

        logger.info("Built mint UserOp for %s → %s/a2a/%s/%s", display_name, hub_base_url, owner, name)

    return signed_ops
