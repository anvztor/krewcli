"""Device authorization flow client for krewauth.

Inverted RFC 8628: krewcli generates the user_code, the human types it
into cookrew under "Hire Agent". cookrew relays approval to krewauth
via krewhub /bundles/{id}/pair-agent.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int


@dataclass(frozen=True)
class DeviceToken:
    token: str
    account_id: str
    expires_at: str


async def request(krewauth_url: str) -> DeviceCode:
    """Ask krewauth for a fresh device_code + user_code."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(f"{krewauth_url}/auth/device/request")
        r.raise_for_status()
        body = r.json()
    return DeviceCode(
        device_code=body["device_code"],
        user_code=body["user_code"],
        verification_uri=body["verification_uri"],
        expires_in=int(body["expires_in"]),
    )


def display_code(dc: DeviceCode) -> None:
    """Print the user_code instructions to the operator."""
    print()
    print(f"  Code: {dc.user_code}")
    print("  Enter this code in cookrew under Hire Agent.")
    print(f"  (Or open {dc.verification_uri})")
    print()


async def poll(
    krewauth_url: str,
    device_code: str,
    *,
    interval: float = 3.0,
    timeout: float = 600.0,
) -> DeviceToken:
    """Poll krewauth /auth/device/token until approved or timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            if asyncio.get_event_loop().time() > deadline:
                raise TimeoutError("device_code_expired")
            r = await client.post(
                f"{krewauth_url}/auth/device/token",
                json={"device_code": device_code},
            )
            if r.status_code == 200:
                body = r.json()
                if body.get("status") == "approved" and body.get("token"):
                    return DeviceToken(
                        token=body["token"],
                        account_id=body["account_id"],
                        expires_at=body["expires_at"],
                    )
            await asyncio.sleep(interval)
