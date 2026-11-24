// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {TickMath} from "v3-core-08/TickMath.sol";
import {IV3Pool} from "../interfaces/IHskSwap.sol";

/// @title TwapLib
/// @notice Time-weighted price reasoning for a Uniswap-V3-style pool (HSKSwap).
/// @dev Design rationale (docs/20_EXPERT_PANEL.md, session 2, P2/P1):
///      - `slot0` is manipulable inside a single transaction (flash swap), so it is never used as the
///        reference price for risk decisions. The reference is the pool's own TWAP oracle.
///      - QuoterV2 is *not* a view function, so it cannot be re-called on-chain to re-validate an
///        off-chain quote (see IQuoterV2's own notice). The vault therefore derives its own fair value
///        from the TWAP and bounds the acceptable execution price around it.
library TwapLib {
    error OracleNotReady();
    error WindowTooShort();
    error TickOutOfRange();

    /// @notice Arithmetic mean tick over `window` seconds, taken from the pool oracle.
    /// @dev Reverts instead of guessing when the oracle has no observation old enough. Failing closed
    ///      is intentional: a stale oracle must stop trading, not widen it.
    function consult(address pool, uint32 window) internal view returns (int24 meanTick) {
        if (window == 0) revert WindowTooShort();

        uint32[] memory secondsAgos = new uint32[](2);
        secondsAgos[0] = window;
        secondsAgos[1] = 0;

        (int56[] memory tickCumulatives,) = _observeOrRevert(pool, secondsAgos);

        int56 delta = tickCumulatives[1] - tickCumulatives[0];
        int56 avg = delta / int56(uint56(window));

        if (avg > type(int24).max || avg < type(int24).min) revert TickOutOfRange();
        meanTick = int24(avg);

        if (meanTick < TickMath.MIN_TICK || meanTick > TickMath.MAX_TICK) revert TickOutOfRange();
    }

    /// @dev The pool reverts with its own error (e.g. "OLD") when it cannot serve the window. We
    ///      translate that into a typed failure so callers can fail closed with a precise reason.
    function _observeOrRevert(address pool, uint32[] memory secondsAgos)
        private
        view
        returns (int56[] memory tickCumulatives, uint160[] memory secondsPerLiquidityCumulativeX128s)
    {
        try IV3Pool(pool).observe(secondsAgos) returns (int56[] memory tc, uint160[] memory sp) {
            return (tc, sp);
        } catch {
            revert OracleNotReady();
        }
    }

    /// @notice Spot tick, read only to measure how far the pool has been pushed from its own TWAP.
    function spotTick(address pool) internal view returns (int24 tick) {
        (, tick,,,,,) = IV3Pool(pool).slot0();
    }

    /// @notice Convert `baseAmount` of `baseToken` into `quoteToken` units at `tick`.
    /// @dev Same arithmetic as Uniswap's OracleLibrary.getQuoteAtTick, reimplemented on top of
    ///      OpenZeppelin Math.mulDiv so the vault does not depend on the whole v3-periphery package.
    ///      Amounts stay in raw token units, so decimals never need to be normalised here.
    function quoteAtTick(int24 tick, uint256 baseAmount, address baseToken, address quoteToken)
        internal
        pure
        returns (uint256 quoteAmount)
    {
        uint160 sqrtRatioX96 = TickMath.getSqrtRatioAtTick(tick);

        if (sqrtRatioX96 <= type(uint128).max) {
            uint256 ratioX192 = uint256(sqrtRatioX96) * sqrtRatioX96;
            quoteAmount = baseToken < quoteToken
                ? Math.mulDiv(ratioX192, baseAmount, 1 << 192)
                : Math.mulDiv(1 << 192, baseAmount, ratioX192);
        } else {
            uint256 ratioX128 = Math.mulDiv(sqrtRatioX96, sqrtRatioX96, 1 << 64);
            quoteAmount = baseToken < quoteToken
                ? Math.mulDiv(ratioX128, baseAmount, 1 << 128)
                : Math.mulDiv(1 << 128, baseAmount, ratioX128);
        }
    }

    /// @notice Absolute difference between two ticks, saturated to int256.
    function absDiff(int24 a, int24 b) internal pure returns (uint256) {
        int256 d = int256(a) - int256(b);
        return uint256(d < 0 ? -d : d);
    }
}
