"""Which (network, asset) pairs in a payment_option are USD stablecoins - the only ones
`max_price` (app/core/db.py) can compare against a USD figure, since a payment_option's
`amount` is a decimal string in that ASSET's own whole-token units (see
app/core/models.py's PaymentOption - e.g. "0.02" means 0.02 of the asset, not 0.02 of a
smallest/atomic unit), and this board has no price oracle to convert anything else
(ETH, SOL, an arbitrary token) to USD. A stablecoin's peg makes that conversion trivial
(1 token ~= $1), so its amount can be compared to max_price directly, unconverted; a
listing whose only payment_options are in a non-stablecoin asset is excluded from
max_price filtering entirely rather than guessed at.

Deliberately a short, explicit, manually-curated list - exactly the assets this board's
own worked examples and real listings actually use - not an attempt at a general
token/price registry. Add an entry here (network, asset address, case-insensitive) only
for a token actually pegged ~1:1 to the US dollar.
"""

# (network, asset) - asset compared case-insensitively (EVM addresses may be checksummed
# with mixed case; the Solana address is case-sensitive base58 but copied here exactly).
_USD_STABLECOINS: tuple[tuple[str, str], ...] = (
    ("eip155:8453", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"),  # USDC on Base
    ("eip155:1", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"),  # USDC on Ethereum mainnet
    ("solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"),  # USDC on Solana
)


def stablecoin_pairs() -> tuple[tuple[str, str], ...]:
    """The raw (network, asset) list, for publishing in the discovery manifest."""
    return _USD_STABLECOINS


def stablecoin_keys() -> list[str]:
    """One "network|asset" string per recognized stablecoin, asset lowercased - matches
    the SQL in app/core/db.py's _filter_conditions, which builds the same key from each
    payment_option's own network/asset at query time."""
    return [f"{network}|{asset.lower()}" for network, asset in _USD_STABLECOINS]


def is_usd_stablecoin(network: str, asset: str) -> bool:
    return f"{network}|{asset.lower()}" in stablecoin_keys()
