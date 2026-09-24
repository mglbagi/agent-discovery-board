"""Addresses that can never own a listing because their private keys are public.

Anyone can sign for these, so a listing owned by one could be edited, deactivated or
taken over by anybody. The board's own signing spec publishes the first Hardhat/Anvil
dev account as its worked-example signer (app/core/signing_spec.py) - which is exactly
why it must be refused as a real `submitted_by`.
"""

HARDHAT_ACCOUNT_0 = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"

RESERVED_ADDRESSES: tuple[str, ...] = (HARDHAT_ACCOUNT_0,)

_RESERVED_LOWER = frozenset(a.lower() for a in RESERVED_ADDRESSES)


def is_reserved_address(address: str) -> bool:
    return address.lower() in _RESERVED_LOWER
