"""ERC-8004 agent NFT minting — one-click per agent.

One UserOp per agent that does everything:
  1. Deploy smart account (via initCode, if not deployed)
  2. Add session key for future agent operations
  3. Mint ERC-8004 NFT with A2A endpoint

Signed by owner (human's EOA), not session key.
Human calls handleOps() in cookrew — one wallet popup per agent.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from eth_abi import encode
from eth_utils import to_checksum_address
from web3 import Web3

logger = logging.getLogger(__name__)

# Selectors
REGISTER_SELECTOR = Web3.keccak(text="register(string)")[:4]
ADD_SESSION_KEY_SELECTOR = Web3.keccak(text="addSessionKey(address,address[],bytes4[],uint48,uint128)")[:4]
EXECUTE_BATCH_SELECTOR = Web3.keccak(text="executeBatch(address[],uint256[],bytes[])")[:4]


def build_agent_uri(
    display_name: str, owner: str, agent_name: str,
    hub_base_url: str = "https://hub.cookrew.dev",
) -> str:
    registration = {
        "type": "https://eips.ethereum.org/EIPS/eip-8004#registration-v1",
        "name": display_name,
        "description": f"{display_name} on Cookrew",
        "active": True,
        "services": [{"name": "A2A", "endpoint": f"{hub_base_url}/a2a/{owner}/{agent_name}"}],
    }
    payload = json.dumps(registration, separators=(",", ":"))
    return f"data:application/json;base64,{base64.b64encode(payload.encode()).decode()}"


def _encode_register(agent_uri: str) -> bytes:
    return REGISTER_SELECTOR + encode(["string"], [agent_uri])


def _encode_add_session_key(
    session_key_addr: str, targets: list[str], selectors: list[bytes],
    valid_until: int, spend_limit: int,
) -> bytes:
    return ADD_SESSION_KEY_SELECTOR + encode(
        ["address", "address[]", "bytes4[]", "uint48", "uint128"],
        [to_checksum_address(session_key_addr), [to_checksum_address(t) for t in targets],
         selectors, valid_until, spend_limit],
    )


def _encode_execute_batch(dests: list[str], values: list[int], datas: list[bytes]) -> bytes:
    return EXECUTE_BATCH_SELECTOR + encode(
        ["address[]", "uint256[]", "bytes[]"],
        [[to_checksum_address(d) for d in dests], values, datas],
    )


def build_one_click_userop(
    agent_name: str,
    display_name: str,
    owner_address: str,
    session_key_addr: str,
    identity_registry: str,
    factory_address: str,
    entrypoint_address: str,
    rpc_url: str,
    hub_base_url: str = "https://hub.cookrew.dev",
    owner_name: str = "",
    valid_hours: int = 24,
) -> dict[str, Any]:
    """Build a single UserOp that deploys account + adds session key + mints NFT.

    Returns a JSON-serializable dict with the unsigned UserOp.
    Human signs with their wallet (owner key, 0x00 prefix).
    """
    import time

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    owner = to_checksum_address(owner_address)
    factory = to_checksum_address(factory_address)
    registry = to_checksum_address(identity_registry)
    ep_addr = to_checksum_address(entrypoint_address)

    # Compute smart account address (deterministic)
    factory_abi = [{"inputs": [{"name": "owner", "type": "address"}, {"name": "salt", "type": "uint256"}],
                    "name": "getAddress", "outputs": [{"name": "", "type": "address"}],
                    "stateMutability": "view", "type": "function"},
                   {"inputs": [{"name": "owner", "type": "address"}, {"name": "salt", "type": "uint256"}],
                    "name": "createAccount", "outputs": [{"name": "", "type": "address"}],
                    "stateMutability": "nonpayable", "type": "function"}]
    factory_contract = w3.eth.contract(address=factory, abi=factory_abi)
    smart_account = factory_contract.functions.getAddress(owner, 0).call()

    # Check if already deployed
    code = w3.eth.get_code(smart_account)
    already_deployed = len(code) > 2

    # Build initCode (only if not deployed)
    if already_deployed:
        init_code = b""
    else:
        create_calldata = factory_contract.encodeABI(fn_name="createAccount", args=[owner, 0])
        init_code = bytes.fromhex(factory[2:]) + bytes.fromhex(create_calldata[2:])

    # Build agentURI
    agent_uri = build_agent_uri(display_name, owner_name or owner, agent_name, hub_base_url)

    # Build executeBatch calldata:
    #   call 1: addSessionKey on self
    #   call 2: execute(registry, 0, register(agentURI))
    valid_until = int(time.time()) + (valid_hours * 3600)
    register_selector_bytes = REGISTER_SELECTOR  # register(string)

    add_key_data = _encode_add_session_key(
        session_key_addr=session_key_addr,
        targets=[registry],
        selectors=[register_selector_bytes],
        valid_until=valid_until,
        spend_limit=0,
    )

    register_data = _encode_register(agent_uri)

    # executeBatch([self, registry], [0, 0], [addSessionKey, register])
    batch_calldata = _encode_execute_batch(
        dests=[smart_account, registry],
        values=[0, 0],
        datas=[add_key_data, register_data],
    )

    # Get nonce
    ep_abi = [{"inputs": [{"name": "sender", "type": "address"}, {"name": "key", "type": "uint192"}],
               "name": "getNonce", "outputs": [{"name": "nonce", "type": "uint256"}],
               "stateMutability": "view", "type": "function"}]
    ep = w3.eth.contract(address=ep_addr, abi=ep_abi)
    nonce = ep.functions.getNonce(to_checksum_address(smart_account), 0).call()

    gas_price = max(w3.eth.gas_price, 200000)

    return {
        "agent_name": agent_name,
        "display_name": display_name,
        "agent_uri": agent_uri,
        "smart_account": smart_account,
        "userop": {
            "sender": smart_account,
            "nonce": hex(nonce),
            "initCode": "0x" + init_code.hex(),
            "callData": "0x" + batch_calldata.hex(),
            "callGasLimit": hex(800_000),
            "verificationGasLimit": hex(800_000),
            "preVerificationGas": hex(100_000),
            "maxFeePerGas": hex(gas_price * 2),
            "maxPriorityFeePerGas": hex(gas_price),
            "paymasterAndData": "0x",
            "signature": "0x",  # unsigned — human signs in cookrew
        },
    }
