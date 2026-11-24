// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {TickMath} from "v3-core-08/TickMath.sol";

/// @notice Minimal stand-in for a Uniswap-V3-style pool, sufficient to exercise the vault's
///         TWAP/spot logic deterministically. The real pool behaviour is covered by the fork tests.
contract MockV3Pool {
    int24 public spot;
    int24 public twap;
    uint160 public sqrtPrice;
    bool public observeReverts;
    uint128 public liquidity = 1e18;

    constructor(int24 spotTick, int24 twapTick) {
        spot = spotTick;
        twap = twapTick;
        sqrtPrice = TickMath.getSqrtRatioAtTick(spotTick);
    }

    function setSpot(int24 newSpot) external {
        spot = newSpot;
        sqrtPrice = TickMath.getSqrtRatioAtTick(newSpot);
    }

    function setTwap(int24 newTwap) external {
        twap = newTwap;
    }

    function setObserveReverts(bool v) external {
        observeReverts = v;
    }

    function slot0()
        external
        view
        returns (uint160, int24, uint16, uint16, uint16, uint8, bool)
    {
        return (sqrtPrice, spot, 0, 240, 240, 0, true);
    }

    function observe(uint32[] calldata secondsAgos)
        external
        view
        returns (int56[] memory tickCumulatives, uint160[] memory secondsPerLiquidity)
    {
        if (observeReverts) revert("OLD");
        tickCumulatives = new int56[](2);
        secondsPerLiquidity = new uint160[](2);
        // cumulative ticks such that the mean over the window equals `twap`
        tickCumulatives[0] = 0;
        tickCumulatives[1] = int56(twap) * int56(uint56(secondsAgos[0]));
    }
}
