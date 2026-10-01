"""Addresses that can never be a listing's `submitted_by`, for two different reasons:

  * HARDHAT_ACCOUNT_0: its private key is PUBLIC, so anyone could sign for it - a
    listing owned by it could be edited, deactivated or taken over by anybody. The
    board's own signing spec publishes it as its worked-example signer
    (app/core/signing_spec.py), which is exactly why it must be refused as a real
    `submitted_by`.
  * UNCLAIMED_IMPORT_SUBMITTED_BY: the opposite problem - NO private key can ever sign
    for it (it is not a point reachable by ECDSA key recovery), which is exactly why
    it is used as the placeholder `submitted_by` for an imported listing before anyone
    claims it (app/core/imports.py): nobody (not even the operator) can forge a normal
    signed PATCH/DELETE/heartbeat against an unclaimed import. A human given this
    address as their own submitted_by would simply lock themselves out, so it is
    refused there too.
"""

HARDHAT_ACCOUNT_0 = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
UNCLAIMED_IMPORT_SUBMITTED_BY = "0x0000000000000000000000000000000000000000"

RESERVED_ADDRESSES: tuple[str, ...] = (HARDHAT_ACCOUNT_0, UNCLAIMED_IMPORT_SUBMITTED_BY)

_RESERVED_LOWER = frozenset(a.lower() for a in RESERVED_ADDRESSES)


def is_reserved_address(address: str) -> bool:
    return address.lower() in _RESERVED_LOWER
