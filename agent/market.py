"""Market data: HSKSwap pool state, TWAP, real quotes, and trade bars.

Design notes that come straight from the on-chain facts (docs/10_OSS_RESEARCH.md §3):
  * TWAP comes from the pool's own oracle (`observe`), never from `slot0`, because spot can be
    moved inside one transaction.
  * Real price impact is measured with the QuoterV2 contract, not modelled: quoting a size returns
    exactly what the pool would pay, which is how we separate fee from impact.
  * HSKSwap is thin (about 10 swaps per 3 hours on WHSK/USDT), so time bars would be mostly empty.
    Bars are therefore built by *trade count*, which keeps every bar informative.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .config import Addresses, Market
from .rpc import ABI_ERC20, ABI_FACTORY, ABI_POOL, ABI_QUOTER, Rpc

SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"


def tick_to_price(tick: int) -> float:
    """Raw-token ratio token1/token0 at `tick` (no decimal normalisation)."""
    return 1.0001**tick


def price_human(tick: int, dec0: int, dec1: int) -> float:
    """Human price of token0 in token1 units."""
    return tick_to_price(tick) * (10**dec0) / (10**dec1)


@dataclass
class PoolState:
    address: str
    token0: str
    token1: str
    fee: int
    sqrt_price_x96: int
    tick: int
    liquidity: int
    twap_tick: int
    twap_window: int
    spot_quote_per_base: float  # human price of `base` in `quote`
    twap_quote_per_base: float
    base_is_token0: bool

    @property
    def twap_deviation_bps(self) -> float:
        """Spot vs TWAP in basis points, signed (positive = spot above TWAP)."""
        return (self.tick - self.twap_tick) * 100.0  # 1 tick ≈ 1 bp

    def depth_quote(self) -> float:
        """Rough pool depth in quote units, from L and the current price (single-range view)."""
        if self.liquidity == 0:
            return 0.0
        if self.base_is_token0:
            # quote is token1: amount1 = L * sqrtP
            return (self.liquidity * math.sqrt(tick_to_price(self.tick))) / 10**6
        return (self.liquidity / math.sqrt(tick_to_price(self.tick))) / 10**6


@dataclass
class Bar:
    t: int
    open: float
    high: float
    low: float
    close: float
    volume_quote: float
    buys: int
    sells: int

    @property
    def ret_bps(self) -> float:
        if self.open <= 0:
            return 0.0
        return (self.close - self.open) / self.open * 10_000


@dataclass
class Quote:
    amount_in: int
    amount_out: int
    fee: int


@dataclass
class DexClient:
    rpc: Rpc
    addresses: Addresses = field(default_factory=Addresses)
    _cache: dict = field(default_factory=dict, repr=False)

    # ---------------- pools ----------------

    def pool_address(self, market: Market) -> str:
        f = self.rpc.contract(self.addresses.factory, ABI_FACTORY)
        pool = f.functions.getPool(market.asset, self.addresses.quote, market.fee).call()
        return pool

    def pool_state(self, market: Market, twap_window: int = 600) -> PoolState:
        pool_addr = self.pool_address(market)
        pool = self.rpc.contract(pool_addr, ABI_POOL)
        slot0 = pool.functions.slot0().call()
        liquidity = pool.functions.liquidity().call()
        token0 = pool.functions.token0().call()
        token1 = pool.functions.token1().call()

        holding = self.rpc.block_number()
        window = twap_window
        # The oracle can only serve a window it has observations for; shrink until it answers, and
        # if it never does, fail loudly (the vault fails closed the same way).
        while window >= 60:
            try:
                cum = pool.functions.observe([window, 0]).call()
                twap_tick = int((cum[0][1] - cum[0][0]) / window)
                break
            except Exception:
                window //= 2
        else:
            raise RuntimeError("pool oracle cannot serve any window >= 60s")

        dec0 = self._decimals(token0)
        dec1 = self._decimals(token1)
        base_is_token0 = token0.lower() == market.asset.lower()
        spot = price_human(slot0[1], dec0, dec1)
        twap = price_human(twap_tick, dec0, dec1)
        if not base_is_token0:
            # price_human gives token1 per token0; invert for quote per base
            spot = 1 / spot if spot else 0.0
            twap = 1 / twap if twap else 0.0

        return PoolState(
            address=pool_addr,
            token0=token0,
            token1=token1,
            fee=pool.functions.fee().call(),
            sqrt_price_x96=slot0[0],
            tick=slot0[1],
            liquidity=liquidity,
            twap_tick=twap_tick,
            twap_window=window,
            spot_quote_per_base=spot,
            twap_quote_per_base=twap,
            base_is_token0=base_is_token0,
        )

    def _decimals(self, token: str) -> int:
        c = self.rpc.contract(token, ABI_ERC20)
        return int(c.functions.decimals().call())

    # ---------------- quoting ----------------

    def quote(self, token_in: str, token_out: str, amount_in: int, fee: int) -> Quote:
        q = self.rpc.contract(self.addresses.quoter, ABI_QUOTER)
        out = q.functions.quoteExactInputSingle((token_in, token_out, amount_in, fee, 0)).call()
        return Quote(amount_in=amount_in, amount_out=int(out[0]), fee=fee)

    def quote_buy(self, market: Market, amount_quote: int) -> Quote:
        return self.quote(self.addresses.quote, market.asset, amount_quote, market.fee)

    def quote_sell(self, market: Market, amount_asset: int) -> Quote:
        return self.quote(market.asset, self.addresses.quote, amount_asset, market.fee)

    def fair_out(self, market: Market, amount_in: int, buy: bool, twap_quote_per_base: float) -> int:
        """Amount the pool would pay at the TWAP price (the vault's own fair-value rule).

        Uses Decimal so the value is stable to the last unit; the vault recomputes the same number
        on-chain from the pool oracle and only accepts a floor inside [fair*(1-slip), fair].
        """
        from decimal import Decimal

        if twap_quote_per_base <= 0:
            return 0
        price = Decimal(str(twap_quote_per_base))
        base_unit = Decimal(10) ** market.decimals
        quote_unit = Decimal(10) ** 6
        if buy:
            base = (Decimal(amount_in) / quote_unit) / price * base_unit
            return int(base)
        quote = (Decimal(amount_in) / base_unit) * price * quote_unit
        return int(quote)

    # ---------------- trade history -> bars ----------------

    def swaps(self, market: Market, lookback_blocks: int = 40_000, chunk: int = 2_000) -> list[dict]:
        """Swap events over a range, paged. HSK's public RPC rejects wide eth_getLogs ranges (HTTP 400),
        so the range is walked in `chunk` blocks. Results are cached briefly: the demo loop calls this
        every cycle and the chain is thin, so re-downloading the same logs would be waste, not freshness.
        """
        pool_addr = self.pool_address(market)
        head = self.rpc.block_number()
        key = (market.asset.lower(), market.fee)
        cached = self._cache.get(key)
        if cached and abs(head - cached[0]) < 300 and (time.time() - cached[2]) < 120:
            return cached[1]

        start = max(0, head - lookback_blocks)
        raw: list[dict] = []
        b = start
        while b <= head:
            to = min(b + chunk - 1, head)
            raw.extend(self.rpc.logs(pool_addr, [SWAP_TOPIC], b, to))
            b = to + 1

        pool = self.rpc.contract(pool_addr, ABI_POOL)
        out = []
        for log in raw:
            ev = pool.events.Swap().process_log(log)
            a = ev["args"]
            out.append(
                {
                    "block": ev["blockNumber"],
                    "tx": ev["transactionHash"].hex(),
                    "amount0": int(a["amount0"]),
                    "amount1": int(a["amount1"]),
                    "tick": int(a["tick"]),
                    "sqrt_price_x96": int(a["sqrtPriceX96"]),
                }
            )
        self._cache[key] = (head, out, time.time())
        return out

    def bars(self, market: Market, n_bars: int = 30, swaps_per_bar: int = 2, lookback_blocks: int = 40_000) -> list[Bar]:
        """Equal-trade-count bars. Each bar aggregates `swaps_per_bar` swaps.

        Trade direction is read from the sign of the base-token delta: base *leaving* the pool means
        someone bought it. Prices are human units (quote per base), so decimals are handled once here.
        """
        swaps = [s for s in self.swaps(market, lookback_blocks) if s["amount0"] != 0 and s["amount1"] != 0]
        if len(swaps) < 2:
            return []
        token0 = self._token0(market)
        base_is_token0 = market.asset.lower() == token0
        base_unit = 10**market.decimals
        quote_unit = 10**6

        rows: list[tuple[dict, float, float, bool]] = []
        for s in swaps:
            base_signed = s["amount0"] if base_is_token0 else s["amount1"]
            quote_signed = s["amount1"] if base_is_token0 else s["amount0"]
            if base_signed == 0:
                continue
            base_raw, quote_raw = abs(base_signed), abs(quote_signed)
            price = (quote_raw / quote_unit) / (base_raw / base_unit)
            rows.append((s, price, quote_raw / quote_unit, base_signed < 0))  # base leaving pool = a buy

        chunk = max(1, swaps_per_bar)
        groups = [rows[i : i + chunk] for i in range(0, len(rows), chunk)][-n_bars:]
        out: list[Bar] = []
        for g in groups:
            prices = [p for _, p, _, _ in g]
            out.append(
                Bar(
                    t=g[-1][0]["block"],
                    open=prices[0],
                    high=max(prices),
                    low=min(prices),
                    close=prices[-1],
                    volume_quote=sum(v for _, _, v, _ in g),
                    buys=sum(1 for *_, is_buy in g if is_buy),
                    sells=sum(1 for *_, is_buy in g if not is_buy),
                )
            )
        return out

    def _token0(self, market: Market) -> str:
        key = ("token0", market.asset.lower(), market.fee)
        if key not in self._cache:
            pool = self.rpc.contract(self.pool_address(market), ABI_POOL)
            self._cache[key] = pool.functions.token0().call().lower()
        return self._cache[key]
