// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title SwapGuardLib — the Uniswap-skill swap guards, as predicates
/// @author Built for HSK Chain Horizon Hackathon · Seoul (Track 1: AI Agent Trading)
///
/// @notice These are the pre-trade guards the `Uniswap/uniswap-ai` skills apply before a swap is
///         built: is this pool deep enough to absorb the size, how much of the pool would this order
///         be, how much worse than the pool's own price is the quote, is the slippage setting inside
///         the ceiling, is the quote still fresh, is the size inside the operator's caps, and is the
///         exit leg quotable at all (a buy you cannot sell is a honeypot).
///
/// @dev Why a library and not a change to `SentinelVault`: the vault is the fund-moving kernel and is
///      deliberately immutable and audited. The guard layer is stricter than the vault by design
///      (e.g. a $100k depth floor the vault does not have), so it belongs in front of the intent, not
///      inside it. The off-chain agent applies the identical predicate set in `agent/guards.py`; the
///      shared table `test/fixtures/guard_scenarios.json` is read by both implementations, so the two
///      cannot drift apart silently.
library SwapGuardLib {
    uint256 internal constant BPS = 10_000;

    /// @dev Reason codes are `bytes32` so they can be logged, compared and mirrored by name in Python.
    bytes32 internal constant OK = "OK";
    bytes32 internal constant ADDRESS_INVALID = "ADDRESS_INVALID";
    bytes32 internal constant AMOUNT_INVALID = "AMOUNT_INVALID";
    bytes32 internal constant KILL_SWITCH = "KILL_SWITCH";
    bytes32 internal constant TOKEN_NOT_ALLOWED = "TOKEN_NOT_ALLOWED";
    bytes32 internal constant SELL_SIDE_MISSING = "SELL_SIDE_MISSING";
    bytes32 internal constant DEPTH_FLOOR = "DEPTH_FLOOR";
    bytes32 internal constant DEPTH_SHARE = "DEPTH_SHARE";
    bytes32 internal constant PRICE_IMPACT = "PRICE_IMPACT";
    bytes32 internal constant SLIPPAGE_TOLERANCE = "SLIPPAGE_TOLERANCE";
    bytes32 internal constant PER_RUN_CAP = "PER_RUN_CAP";
    bytes32 internal constant PER_PERIOD_CAP = "PER_PERIOD_CAP";
    bytes32 internal constant QUOTE_STALE = "QUOTE_STALE";
    bytes32 internal constant AUTONOMY_UNARMED = "AUTONOMY_UNARMED";

    struct GuardInput {
        address tokenIn;
        address tokenOut;
        bool allowedIn;
        bool allowedOut;
        uint256 amountIn; // raw token units
        uint256 notionalQuote; // raw quote units
        uint256 quotedOut; // what the pool would pay now
        uint256 fairOut; // what the pool's TWAP says it should pay
        uint256 quoteDepthQuote; // quote-side depth, raw quote units
        uint256 sellQuote; // quote received if the position were exited now (0 = no exit)
        uint64 quotedAt;
        uint64 nowTs;
        uint256 periodSpentQuote; // raw quote units already spent in this period
        uint16 slippageBps; // tolerance the agent is asking for
        bool isBuy;
        bool killSwitch;
        bool autonomous;
        bool dryRunDone;
        uint16 maxSlippageBps;
        uint16 maxPriceImpactBps;
        uint16 maxDepthShareBps;
        uint256 minDepthQuote; // raw quote units
        uint32 maxQuoteAge;
        uint256 perRunCap; // raw quote units, 0 = unset
        uint256 perPeriodCap; // raw quote units, 0 = unset
        bool hasPerRunCap;
        bool hasPerPeriodCap;
        bool hasAllowlist;
    }

    /// @notice How much worse than the pool's TWAP the quote is. A better-than-TWAP quote is 0.
    function priceImpactBps(uint256 fairOut, uint256 quotedOut) internal pure returns (uint256) {
        if (fairOut == 0 || quotedOut == 0 || quotedOut >= fairOut) return 0;
        return ((fairOut - quotedOut) * BPS) / fairOut;
    }

    /// @notice What fraction of the quoted depth one order would be.
    function depthShareBps(uint256 notionalQuote, uint256 depthQuote) internal pure returns (uint256) {
        if (depthQuote == 0) return BPS; // any order consumes an empty side entirely
        return (notionalQuote * BPS) / depthQuote;
    }

    /// @notice Slippage floor for `minAmountOut`: never above the quote, never further than the tolerance.
    function slippageFloor(uint256 amountOut, uint16 slippageBps, uint16 safetyBps)
        internal
        pure
        returns (uint256)
    {
        if (slippageBps > BPS) return 0;
        uint256 floor = (amountOut * (BPS - slippageBps)) / BPS;
        uint256 safer = amountOut - (amountOut * safetyBps) / BPS;
        return floor > safer ? floor : safer;
    }

    /// @notice The whole predicate set, in the same order as `agent/guards.py::preflight`.
    function check(GuardInput memory g) internal pure returns (bytes32) {
        // 1. input validation
        if (g.tokenIn == address(0) || g.tokenOut == address(0) || g.tokenIn == g.tokenOut) {
            return ADDRESS_INVALID;
        }
        if (g.amountIn == 0) return AMOUNT_INVALID;

        // 2. the operator's brake
        if (g.killSwitch) return KILL_SWITCH;

        // 3. both legs must be allowlisted
        if (!g.allowedIn || !g.allowedOut) return TOKEN_NOT_ALLOWED;

        // 4. a buy whose exit cannot quote is a honeypot
        if (g.isBuy && g.sellQuote == 0) return SELL_SIDE_MISSING;

        // 5. pool-depth floor
        if (g.quoteDepthQuote < g.minDepthQuote) return DEPTH_FLOOR;

        // 6. one order must not be the pool
        if (depthShareBps(g.notionalQuote, g.quoteDepthQuote) > g.maxDepthShareBps) return DEPTH_SHARE;

        // 7. price impact against the pool's own TWAP
        if (priceImpactBps(g.fairOut, g.quotedOut) > g.maxPriceImpactBps) return PRICE_IMPACT;

        // 8. slippage tolerance ceiling
        if (g.slippageBps > g.maxSlippageBps) return SLIPPAGE_TOLERANCE;

        // 9-10. spend caps, entries only
        if (g.isBuy) {
            if (g.hasPerRunCap && g.notionalQuote > g.perRunCap) return PER_RUN_CAP;
            if (g.hasPerPeriodCap && g.periodSpentQuote + g.notionalQuote > g.perPeriodCap) return PER_PERIOD_CAP;
        }

        // 11. quote freshness
        if (g.nowTs > g.quotedAt && g.nowTs - g.quotedAt > g.maxQuoteAge) return QUOTE_STALE;

        // 12. autonomous mode is earned by having every guardrail
        if (g.autonomous) {
            bool armed = g.hasPerRunCap && g.hasPerPeriodCap && g.hasAllowlist && g.dryRunDone;
            if (!armed) return AUTONOMY_UNARMED;
        }

        return OK;
    }
}

/// @notice Thin harness the test suite calls directly. Not deployed by anything.
contract GuardHarness {
    function evaluate(SwapGuardLib.GuardInput memory g) external pure returns (bytes32 code, uint256 impact, uint256 share) {
        code = SwapGuardLib.check(g);
        impact = SwapGuardLib.priceImpactBps(g.fairOut, g.quotedOut);
        share = SwapGuardLib.depthShareBps(g.notionalQuote, g.quoteDepthQuote);
    }

    function floor(uint256 amountOut, uint16 slippageBps, uint16 safetyBps) external pure returns (uint256) {
        return SwapGuardLib.slippageFloor(amountOut, slippageBps, safetyBps);
    }
}
