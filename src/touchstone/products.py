"""What the research needs to know about each recorded product.

Ticks are Coinbase's quote increments, checked on 2026-11-11, scaled by 1e8 like recorded prices.
Order sizes are worth roughly $25 to $75, small next to what usually rests at the best price.

BTC and ETH trade at a spread of one to a few cents, a tiny fraction of their price, so any edge
from the spread is small next to adverse selection. ZEC and NEAR were added on 2026-11-11 because
their spreads are several ticks and a few basis points wide, to show how the edge depends on spread
width. They have too little development data to fit on, so they are not part of the held-out
evaluation.
"""

from __future__ import annotations

TICK_E8 = {"BTC-USD": 1_000_000, "ETH-USD": 1_000_000, "ZEC-USD": 1_000_000, "NEAR-USD": 10_000}
ORDER_SIZE_E8 = {
    "BTC-USD": 100_000,  # 0.001 BTC
    "ETH-USD": 1_000_000,  # 0.01 ETH
    "ZEC-USD": 5_000_000,  # 0.05 ZEC
    "NEAR-USD": 1_000_000_000,  # 10 NEAR
}
ORIGINAL = ("BTC-USD", "ETH-USD")
WIDER_SPREAD = ("ZEC-USD", "NEAR-USD")
ALL = ORIGINAL + WIDER_SPREAD

# Maker fee scenarios, in basis points of traded notional. Coinbase does not publish one schedule
# for every region, and the rate in an account's order preview is the one that applies. 50 bps is
# the US entry-tier maker fee reported after a change on 2026-09-16; 40 bps is the entry rate
# several sources gave before it.
FEE_SCENARIOS_BPS = {"no_fees": 0.0, "retail_maker_50bps": 50.0, "retail_maker_40bps": 40.0}
