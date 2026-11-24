"""Agent configuration: chain facts, pinned contract addresses, and the default risk policy.

Every address here was discovered from the official HSKSwap SDK (@hskswap/sdk-core@1.0.3) and then
verified on HSK Chain mainnet by reading code size / factory state. See docs/00_FACTS.md §7 and
docs/10_OSS_RESEARCH.md §5. Nothing is guessed, and nothing is read from a live API at import time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Dict


@dataclass(frozen=True)
class Chain:
    name: str = "HSK Chain"
    chain_id: int = 177
    rpc_url: str = "https://mainnet.hsk.xyz"
    explorer: str = "https://hsk.blockscout.com"
    native_symbol: str = "HSK"
    block_time_s: int = 2


@dataclass(frozen=True)
class Addresses:
    quote: str = "0xF1B50eD67A9e2CC94Ad3c477779E2d4cBfFf9029"  # USDT (6dp)
    factory: str = "0x972cA9D1662F5e029cD18327D29026532E84c742"
    router: str = "0x2c16f75b95Cf1390c328aB70e2CEE7f4b80bD8F3"
    quoter: str = "0x603f70466fDdbE3F238220B9a74FFF419a2BbFDD"
    npm: str = "0xDF0A9b833Db0E4CeEa103A4408f3B68e7FC0cad5"
    whsk: str = "0xB210D2120d57b758EE163cFfb43e73728c471Cf1"  # 18dp
    weth: str = "0xefd4bC9afD210517803f293ABABd701CaeeCdfd0"  # 18dp
    wbtc: str = "0x6119CA49a79f5825C8B345F8d7aC36B272565b14"  # 8dp
    usdc: str = "0x054ed45810DbBAb8B27668922D110669c9D88D0a"  # 6dp (USDC.e)


@dataclass(frozen=True)
class Market:
    asset: str
    symbol: str
    decimals: int
    fee: int


MARKETS: Dict[str, Market] = {
    "WHSK": Market(Addresses.whsk, "WHSK", 18, 3000),
    "WETH": Market(Addresses.weth, "WETH", 18, 3000),  # traded via USDT/WETH pool
    "WBTC": Market(Addresses.wbtc, "WBTC", 8, 3000),
}

# Measured 2026-11-24 (block 30,577,582) by reading each pool's liquidity() and slot0() and by quoting a
# 120 USDT buy through QuoterV2. Depth is the quote-side value of the in-range liquidity; `both_sides`
# counts the asset side too. Kept as data, not prose, so the agent can rank markets honestly instead of
# pretending all three are equally tradable.
MARKET_STATS: Dict[str, dict] = {
    "WHSK": {"pool": "0x4ff515761864e7838B0AdB8b5b2B8B7A068905dC", "quote_depth_usd": 419_423,
             "both_sides_usd": 838_845, "impact_bps_at_120": 33.0, "price_usd": 0.080768},
    "WETH": {"pool": "0xb92b79ed6b370D5cFee2E14B1D0983C258304C16", "quote_depth_usd": 41_814,
             "both_sides_usd": 83_629, "impact_bps_at_120": 58.7, "price_usd": 1703.99},
    "WBTC": {"pool": "0xd9EA2cE468957Ba49cCC38C4758Ed6ecD561449A", "quote_depth_usd": 45_339,
             "both_sides_usd": 90_677, "impact_bps_at_120": 56.3, "price_usd": 61_830.30},
}

# Only pools that exist on HSKSwap today are listed. WHSK/USDC 0.3% holds ~$0.08 of liquidity and
# WHSK/USDC 1% is empty, so both are excluded on purpose. WETH and WBTC *are* tradable — they are simply
# thinner and pricier to cross (see MARKET_STATS), which the strategy accounts for rather than hides.
LIQUID_MARKETS = ("WHSK", "WETH", "WBTC")
DEFAULT_MARKET = "WHSK"


@dataclass(frozen=True)
class RiskPolicy:
    """Mirror of SentinelVault.Policy. Kept in sync by the shared table in test/fixtures/."""

    # min_edge_bps is the on-chain floor for a *claim on entry*. It is set to the measured round-trip cost
    # (2 x 30bp fee + 33bp impact in + 33bp impact out = 126bp) plus a 20bp margin, so "the vault refuses
    # anything claiming less than the cost of crossing" is true on-chain, not just in the agent's head.
    min_edge_bps: int = 146
    max_slippage_bps: int = 150
    max_spot_twap_deviation_bps: int = 500
    max_drawdown_bps: int = 1500
    twap_window: int = 600
    cooldown_s: int = 60
    max_slices: int = 4
    max_trade_notional: int = 200_000_000  # 200 USDT (6dp)
    max_daily_notional: int = 800_000_000  # 800 USDT

    def with_overrides(self, **kw) -> "RiskPolicy":
        return replace(self, **kw)

    def as_dict(self) -> dict:
        return {
            "min_edge_bps": self.min_edge_bps,
            "max_slippage_bps": self.max_slippage_bps,
            "max_spot_twap_deviation_bps": self.max_spot_twap_deviation_bps,
            "max_drawdown_bps": self.max_drawdown_bps,
            "twap_window": self.twap_window,
            "cooldown_s": self.cooldown_s,
            "max_slices": self.max_slices,
            "max_trade_notional": self.max_trade_notional,
            "max_daily_notional": self.max_daily_notional,
        }


@dataclass(frozen=True)
class Strategy:
    name: str = "sentinel/momentum-twap-reversion/v1"
    # parameters are committed by hash; changing any of them changes the registered strategy id
    ema_fast: int = 5
    ema_slow: int = 20
    rsi_period: int = 14
    rsi_oversold: int = 35
    rsi_overbought: int = 65
    min_conviction: float = 0.55
    target_position_frac: float = 0.6  # of max_trade_notional
    horizon_bars: int = 3

    def spec(self) -> dict:
        return {
            "name": self.name,
            "ema_fast": self.ema_fast,
            "ema_slow": self.ema_slow,
            "rsi_period": self.rsi_period,
            "rsi_oversold": self.rsi_oversold,
            "rsi_overbought": self.rsi_overbought,
            "min_conviction": self.min_conviction,
            "target_position_frac": self.target_position_frac,
            "horizon_bars": self.horizon_bars,
            "policy": {
                "min_edge_bps": RiskPolicy().min_edge_bps,
                "max_slippage_bps": RiskPolicy().max_slippage_bps,
                "twap_window": RiskPolicy().twap_window,
            },
        }


@dataclass
class Settings:
    chain: Chain = field(default_factory=Chain)
    addresses: Addresses = field(default_factory=Addresses)
    policy: RiskPolicy = field(default_factory=RiskPolicy)
    strategy: Strategy = field(default_factory=Strategy)
    live: bool = False
    vault: str = ""
    state_dir: str = "state"
    # Live refusals are written on-chain as well, so "did nothing because the hurdle was not met" is
    # evidence and not an absence. Off by default for paper runs (there is nothing to sign against).
    record_no_trade: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            chain=Chain(rpc_url=os.environ.get("HSK_RPC", Chain.rpc_url)),
            live=os.environ.get("SENTINEL_LIVE", "0") == "1",
            vault=os.environ.get("SENTINEL_VAULT", ""),
            state_dir=os.environ.get("SENTINEL_STATE_DIR", "state"),
            record_no_trade=os.environ.get("SENTINEL_RECORD_NO_TRADE", "1") != "0",
        )


BPS = 10_000
