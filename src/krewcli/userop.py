"""ERC-4337 v0.6 UserOperation builder and signer.

Builds UserOps for the KrewAccount smart account, signs with session key
(0x01 prefix), and submits by calling EntryPoint.handleOps() directly.
The human's EOA pays gas when they call handleOps from cookrew.
"""

from __future__ import annotations

import logging
from typing import Any

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_abi import encode
from web3 import Web3

logger = logging.getLogger(__name__)

# ERC-4337 v0.6 EntryPoint ABI (minimal)
ENTRYPOINT_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"name": "sender", "type": "address"},
                    {"name": "nonce", "type": "uint256"},
                    {"name": "initCode", "type": "bytes"},
                    {"name": "callData", "type": "bytes"},
                    {"name": "callGasLimit", "type": "uint256"},
                    {"name": "verificationGasLimit", "type": "uint256"},
                    {"name": "preVerificationGas", "type": "uint256"},
                    {"name": "maxFeePerGas", "type": "uint256"},
                    {"name": "maxPriorityFeePerGas", "type": "uint256"},
                    {"name": "paymasterAndData", "type": "bytes"},
                    {"name": "signature", "type": "bytes"},
                ],
                "name": "ops",
                "type": "tuple[]",
            },
            {"name": "beneficiary", "type": "address"},
        ],
        "name": "handleOps",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {
                "components": [
                    {"name": "sender", "type": "address"},
                    {"name": "nonce", "type": "uint256"},
                    {"name": "initCode", "type": "bytes"},
                    {"name": "callData", "type": "bytes"},
                    {"name": "callGasLimit", "type": "uint256"},
                    {"name": "verificationGasLimit", "type": "uint256"},
                    {"name": "preVerificationGas", "type": "uint256"},
                    {"name": "maxFeePerGas", "type": "uint256"},
                    {"name": "maxPriorityFeePerGas", "type": "uint256"},
                    {"name": "paymasterAndData", "type": "bytes"},
                    {"name": "signature", "type": "bytes"},
                ],
                "name": "userOp",
                "type": "tuple",
            },
        ],
        "name": "getUserOpHash",
        "outputs": [{"name": "", "type": "bytes32"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "key", "type": "uint192"}],
        "name": "getNonce",
        "outputs": [{"name": "nonce", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# KrewAccount execute() selector
EXECUTE_SELECTOR = Web3.keccak(text="execute(address,uint256,bytes)")[:4]


def build_execute_calldata(dest: str, value: int, inner_data: bytes) -> bytes:
    """Encode execute(address dest, uint256 value, bytes data) calldata."""
    encoded_args = encode(
        ["address", "uint256", "bytes"],
        [Web3.to_checksum_address(dest), value, inner_data],
    )
    return EXECUTE_SELECTOR + encoded_args


def build_userop(
    smart_account: str,
    calldata: bytes,
    rpc_url: str,
    entrypoint_address: str,
    gas_price: int | None = None,
) -> dict[str, Any]:
    """Build an unsigned ERC-4337 v0.6 UserOperation."""
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    ep = w3.eth.contract(
        address=Web3.to_checksum_address(entrypoint_address),
        abi=ENTRYPOINT_ABI,
    )

    # Get nonce from EntryPoint
    nonce = ep.functions.getNonce(Web3.to_checksum_address(smart_account), 0).call()

    gp = gas_price or w3.eth.gas_price
    # Ensure minimum gas price for GOAT Testnet3
    if gp < 200000:
        gp = 200000

    return {
        "sender": Web3.to_checksum_address(smart_account),
        "nonce": nonce,
        "initCode": b"",
        "callData": calldata,
        "callGasLimit": 500_000,
        "verificationGasLimit": 500_000,
        "preVerificationGas": 50_000,
        "maxFeePerGas": gp * 2,
        "maxPriorityFeePerGas": gp,
        "paymasterAndData": b"",
        "signature": b"",  # unsigned
    }


def sign_userop_with_session_key(
    userop: dict[str, Any],
    session_key_private: str,
    rpc_url: str,
    entrypoint_address: str,
) -> dict[str, Any]:
    """Sign a UserOp with a session key (0x01 prefix encoding)."""
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    ep = w3.eth.contract(
        address=Web3.to_checksum_address(entrypoint_address),
        abi=ENTRYPOINT_ABI,
    )

    # Get UserOp hash
    userop_tuple = (
        userop["sender"],
        userop["nonce"],
        userop["initCode"],
        userop["callData"],
        userop["callGasLimit"],
        userop["verificationGasLimit"],
        userop["preVerificationGas"],
        userop["maxFeePerGas"],
        userop["maxPriorityFeePerGas"],
        userop["paymasterAndData"],
        b"",  # empty signature for hash computation
    )

    userop_hash = ep.functions.getUserOpHash(userop_tuple).call()

    # Sign with session key
    acct = Account.from_key(session_key_private)
    msg = encode_defunct(userop_hash)
    signed = acct.sign_message(msg)

    # Encode: 0x01 || session_key_address(20) || ecdsa_sig(65)
    session_addr_bytes = bytes.fromhex(acct.address[2:])
    sig_bytes = bytes([1]) + session_addr_bytes + signed.signature

    return {**userop, "signature": sig_bytes}


def userop_to_json(userop: dict[str, Any]) -> dict[str, Any]:
    """Convert UserOp to JSON-serializable format (for display/transmission)."""
    return {
        "sender": userop["sender"],
        "nonce": hex(userop["nonce"]),
        "initCode": "0x" + userop["initCode"].hex(),
        "callData": "0x" + userop["callData"].hex(),
        "callGasLimit": hex(userop["callGasLimit"]),
        "verificationGasLimit": hex(userop["verificationGasLimit"]),
        "preVerificationGas": hex(userop["preVerificationGas"]),
        "maxFeePerGas": hex(userop["maxFeePerGas"]),
        "maxPriorityFeePerGas": hex(userop["maxPriorityFeePerGas"]),
        "paymasterAndData": "0x" + userop["paymasterAndData"].hex(),
        "signature": "0x" + userop["signature"].hex() if userop["signature"] else "0x",
    }
