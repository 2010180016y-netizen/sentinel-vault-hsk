// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title Minimal HSKSwap (Uniswap V3 fork) interfaces
/// @notice Only the surface the vault needs. Struct layouts were verified against the
///         verified ABI published by HSK Blockscout for SwapRouter02 / QuoterV2 / the pool,
///         not copied from memory. See docs/10_OSS_RESEARCH.md §3.2.
interface IV3SwapRouter02 {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }

    /// @dev SwapRouter02 has NO deadline in ExactInputSingleParams; the deadline is enforced
    ///      by the multicall wrapper. Using the v1 struct here would compile and silently drop
    ///      the expiry, so we force callers through `multicall(deadline, ...)`.
    function exactInputSingle(ExactInputSingleParams calldata params) external payable returns (uint256 amountOut);

    function multicall(uint256 deadline, bytes[] calldata data) external payable returns (bytes[] memory results);
}

interface IV3Factory {
    function getPool(address tokenA, address tokenB, uint24 fee) external view returns (address pool);
}

interface IV3Pool {
    function slot0()
        external
        view
        returns (
            uint160 sqrtPriceX96,
            int24 tick,
            uint16 observationIndex,
            uint16 observationCardinality,
            uint16 observationCardinalityNext,
            uint8 feeProtocol,
            bool unlocked
        );

    function observe(uint32[] calldata secondsAgos)
        external
        view
        returns (int56[] memory tickCumulatives, uint160[] memory secondsPerLiquidityCumulativeX128s);

    function liquidity() external view returns (uint128);
}
