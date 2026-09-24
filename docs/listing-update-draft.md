# Draft: update the JSON Schema Verifier listing (approved, NOT yet applied)

Status: description and pricing approved on 2026-09-23. Paused before signing; nothing on the live board has been changed.

- Listing: `19bd05c2-ba2d-491d-b3ad-be8734ec5e31` on https://agent-discovery-board.onrender.com
- Facts were taken from the verification service's live agent-card (v0.2.0) and `/.well-known/x402`. Re-check them before applying, since they may have changed.
- `submitted_by` and `payment_wallet` are both `0x42a3c399f83BCcC3b9eEf81e65954a715D54855E`. The PATCH must be signed by that address.
- Unchanged fields: `endpoint_url`, `task_categories`, `listing_type`, `pricing_model` (`per_call`), `payment_wallet`, `submitted_by`, `status`.

## PATCH body (exact)

```json
{
  "description": "Verification for AI agent work, for both sides of a delivery. Buyers: check an agent's output before you pay for or accept it. Sellers: check your own output before you submit it, then attach the signed receipt as proof of delivery. POST /verify/schema checks any JSON value against a caller-supplied JSON Schema (draft-04 through 2020-12) and returns pass/fail, every violation (not just the first), template-based fix hints for a verify-repair-verify loop, and the share of checks passed. Every response carries an Ed25519-signed receipt that binds the result to the exact output, schema and rules that were checked (SHA-256 output_hash, schema_hash and rules_hash), so it can be verified independently of the service. Optional bounds and cross-field rules become binding with enforce_rules. GET /score/{agent_id} returns an agent_id's signed verification history with a recency-weighted score and 95% confidence interval (a statistical signal about past structural checks, not a guarantee of future behavior; agent_id is an unauthenticated label). Checks structure and integrity, not whether content is correct. Read-only: never modifies submitted data. Also available as MCP tools (verify_schema, get_verification_record) at https://fastapi-service-5ag4.onrender.com/mcp, with 3 free calls per tool per client per day. Payment via x402 in USDC on Base (payTo 0x42a3c399f83BCcC3b9eEf81e65954a715D54855E) or Solana (payTo 38Fmaf3MWTR6AWPWrtrdXoqn6iqfVcUBHMFRhiUAEjFb).",
  "pricing_amount": "$0.02 per verification; $0.01 per score lookup"
}
```

Description is 1470 of 2000 characters (plain ASCII). `pricing_amount` is 46 of 100.

If this body is edited at all, `body_sha256` below changes and a new signature is needed.

`body_sha256` of this body (canonical JSON, per `app/core/canonical.py`):
`41d736aded9814c7f2b044e7e64947e38ed99e5c85309b695bbab22814a45b5e`

## Applying it later

1. Re-fetch the live listing and the verification service's agent-card, and confirm the draft is still accurate.
2. Generate a fresh message with `_build_message` from `app/core/wallet_auth.py`. The format is:

   ```
   Agent Discovery Board
   action: update-listing
   listing_id: 19bd05c2-ba2d-491d-b3ad-be8734ec5e31
   timestamp: <unix seconds>
   nonce: <random hex>
   body_sha256: <hash of the body above>
   ```

   Six lines, no trailing newline. A previously generated timestamp and nonce have expired and are single-use, so never reuse them.
3. Sign it as plain text with `personal_sign` (not a transaction or typed data) from `0x42a3...855E`. The address must be a key-based (EOA) wallet; smart-wallet (ERC-1271) signatures won't verify. The signature is valid for 5 minutes from the timestamp.
4. Send `PATCH /listings/19bd05c2-ba2d-491d-b3ad-be8734ec5e31` with the body above and the header `X-Wallet-Auth`: base64 of `{"signature": "0x...", "timestamp": <int>, "nonce": "<hex>"}`.
5. Confirm via `GET /listings/{id}` and the `search_listings` MCP tool.

Never share a private key or recovery phrase; only the signature is ever needed.
