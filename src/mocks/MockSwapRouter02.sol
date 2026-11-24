// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {IV3SwapRouter02} from "../interfaces/IHskSwap.sol";

/// @notice Router stand-in with SwapRouter02's exact external shape (struct without deadline,
///         deadline enforced by multicall). `nextAmountOut` lets a test script the fill.
contract MockSwapRouter02 is IV3SwapRouter02 {
    using SafeERC20 for IERC20;

    uint256 public nextAmountOut;
    uint256 public calls;
    bool public ignoreMinOut;

    function setNextAmountOut(uint256 v) external {
        nextAmountOut = v;
    }

    function available(address token) external view returns (uint256) {
        return IERC20(token).balanceOf(address(this));
    }

    function setIgnoreMinOut(bool v) external {
        ignoreMinOut = v;
    }

    function exactInputSingle(ExactInputSingleParams calldata p) external payable returns (uint256 amountOut) {
        require(IERC20(p.tokenOut).balanceOf(address(this)) >= nextAmountOut, "router: no liquidity");
        IERC20(p.tokenIn).safeTransferFrom(msg.sender, address(this), p.amountIn);
        amountOut = nextAmountOut;
        if (!ignoreMinOut) require(amountOut >= p.amountOutMinimum, "Too little received");
        IERC20(p.tokenOut).safeTransfer(p.recipient, amountOut);
        calls += 1;
    }

    function multicall(uint256 deadline, bytes[] calldata data) external payable returns (bytes[] memory results) {
        require(block.timestamp <= deadline, "Transaction too old");
        results = new bytes[](data.length);
        for (uint256 i; i < data.length; ++i) {
            (bool ok, bytes memory ret) = address(this).delegatecall(data[i]);
            require(ok, "multicall failed");
            results[i] = ret;
        }
    }
}
