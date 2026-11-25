// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";

import {GuardHarness, SwapGuardLib} from "../src/libraries/SwapGuardLib.sol";

/// @notice The EVM side of the guard parity table. `tests/test_guards.py` reads the same JSON and runs
///         the Python mirror over the same scenarios. Neither side may pass with a different verdict.
contract SwapGuardTest is Test {
    GuardHarness internal harness;
    string internal json;

    struct Limits {
        uint16 maxSlippageBps;
        uint16 maxPriceImpactBps;
        uint16 maxDepthShareBps;
        uint256 minDepthQuote;
        uint32 maxQuoteAge;
    }

    struct Scenario {
        string id;
        address tokenIn;
        address tokenOut;
        bool allowedIn;
        bool allowedOut;
        uint256 amountIn;
        uint256 notionalQuote;
        uint256 quotedOut;
        uint256 fairOut;
        uint256 quoteDepthQuote;
        uint256 sellQuote;
        uint64 quotedAt;
        uint64 nowTs;
        uint256 periodSpentQuote;
        uint16 slippageBps;
        bool isBuy;
        bool killSwitch;
        bool autonomous;
        bool dryRunDone;
        uint256 perRunCap;
        uint256 perPeriodCap;
        bool hasPerRunCap;
        bool hasPerPeriodCap;
        bool hasAllowlist;
        string expect;
    }

    function setUp() public {
        harness = new GuardHarness();
        json = vm.readFile("test/fixtures/guard_scenarios.json");
    }

    function _input(Scenario memory s, Limits memory l) internal pure returns (SwapGuardLib.GuardInput memory) {
        return SwapGuardLib.GuardInput({
            tokenIn: s.tokenIn,
            tokenOut: s.tokenOut,
            allowedIn: s.allowedIn,
            allowedOut: s.allowedOut,
            amountIn: s.amountIn,
            notionalQuote: s.notionalQuote,
            quotedOut: s.quotedOut,
            fairOut: s.fairOut,
            quoteDepthQuote: s.quoteDepthQuote,
            sellQuote: s.sellQuote,
            quotedAt: s.quotedAt,
            nowTs: s.nowTs,
            periodSpentQuote: s.periodSpentQuote,
            slippageBps: s.slippageBps,
            isBuy: s.isBuy,
            killSwitch: s.killSwitch,
            autonomous: s.autonomous,
            dryRunDone: s.dryRunDone,
            maxSlippageBps: l.maxSlippageBps,
            maxPriceImpactBps: l.maxPriceImpactBps,
            maxDepthShareBps: l.maxDepthShareBps,
            minDepthQuote: l.minDepthQuote,
            maxQuoteAge: l.maxQuoteAge,
            perRunCap: s.perRunCap,
            perPeriodCap: s.perPeriodCap,
            hasPerRunCap: s.hasPerRunCap,
            hasPerPeriodCap: s.hasPerPeriodCap,
            hasAllowlist: s.hasAllowlist
        });
    }

    function test_SharedGuardTable() public {
        Limits memory l = abi.decode(vm.parseJson(json, ".limits"), (Limits));
        uint256 n = vm.parseJsonUint(json, ".count");
        assertGt(n, 10, "fixture must cover every guard");

        for (uint256 i = 0; i < n; i++) {
            string memory key = string.concat(".scenarios[", vm.toString(i), "]");
            Scenario memory s = abi.decode(vm.parseJson(json, key), (Scenario));
            (bytes32 code,,) = harness.evaluate(_input(s, l));
            emit log_named_string("", s.id);
            assertEq(
                _shortString(code),
                s.expect,
                string.concat("guard verdict drifted for ", s.id)
            );
        }
    }

    function test_PriceImpactIsZeroWhenQuoteBeatsTwap() public {
        (,, uint256 share) = harness.evaluate(_input(_legal(), _defaultLimits()));
        assertEq(share, 2, "120/419,423 USDT is 2bp of depth");
        assertEq(SwapGuardLib.priceImpactBps(1000, 1100), 0);
        assertEq(SwapGuardLib.priceImpactBps(1000, 990), 100);
    }

    function test_DepthShareOfAnEmptySideIsEverything() public pure {
        assertEq(SwapGuardLib.depthShareBps(1, 0), 10_000);
    }

    function test_SlippageFloorNeverBreaksTheCeiling() public {
        // Two floors, tighter one wins: the tolerance floor (1 - 150bp) and a 20bp safety margin
        // below the quote. This mirrors `agent/policy.py::build_min_out` exactly.
        assertEq(harness.floor(1_000_000, 150, 20), 998_000, "safety margin is tighter than 150bp here");
        assertEq(harness.floor(1_000_000, 0, 20), 1_000_000, "zero tolerance asks for the quote itself");
        assertEq(harness.floor(1_000_000, 400, 20), 998_000, "within 400bp, the 20bp safety margin binds");
        assertEq(harness.floor(1_000_000, 20, 100), 998_000, "within 20bp, the tolerance binds");
    }

    /// @dev `bytes32("OK")` is right-padded, so trim the zeros before comparing it with JSON text.
    function _shortString(bytes32 v) internal pure returns (string memory) {
        uint256 len;
        while (len < 32 && v[len] != 0) len++;
        bytes memory out = new bytes(len);
        for (uint256 i = 0; i < len; i++) out[i] = v[i];
        return string(out);
    }

    function _defaultLimits() internal pure returns (Limits memory) {
        return Limits({
            maxSlippageBps: 500,
            maxPriceImpactBps: 100,
            maxDepthShareBps: 2000,
            minDepthQuote: 100_000e6,
            maxQuoteAge: 30
        });
    }

    function _legal() internal pure returns (Scenario memory s) {
        s.tokenIn = 0xF1B50eD67A9e2CC94Ad3c477779E2d4cBfFf9029;
        s.tokenOut = 0xB210D2120d57b758EE163cFfb43e73728c471Cf1;
        s.allowedIn = true;
        s.allowedOut = true;
        s.amountIn = 120e6;
        s.notionalQuote = 120e6;
        s.quotedOut = 1485e18;
        s.fairOut = 1486e18;
        s.quoteDepthQuote = 419_423e6;
        s.sellQuote = 118e6;
        s.quotedAt = 1000;
        s.nowTs = 1010;
        s.slippageBps = 150;
        s.isBuy = true;
        s.hasAllowlist = true;
    }
}
