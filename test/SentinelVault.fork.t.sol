// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test, console} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {SentinelVault} from "../src/SentinelVault.sol";
import {TwapLib} from "../src/libraries/TwapLib.sol";
import {IV3SwapRouter02} from "../src/interfaces/IHskSwap.sol";

/// @notice Integration tests against the **real** HSK Chain mainnet (chainId 177): real HSKSwap
///         router, real WHSK/USDT pool, real TWAP oracle. Run with:
///           forge test --match-contract SentinelVaultForkTest --fork-url https://mainnet.hsk.xyz
/// @dev No state is written to mainnet: forge forges a fork in memory.
///
///      The live pool usually sits exactly on its own TWAP (measured 2026-11-24: dislocation 0 ticks), and
///      a vault that refuses to trade a market with no dislocation is behaving correctly. So the tradable
///      tests first *create* a dislocation the only honest way available: a real swap through the real
///      router, from a whale funded on the fork. The vault then trades that dislocation, and its accounting
///      is checked against the pool's own TWAP.
contract SentinelVaultForkTest is Test {
    address constant USDT = 0xF1B50eD67A9e2CC94Ad3c477779E2d4cBfFf9029;
    address constant WHSK = 0xB210D2120d57b758EE163cFfb43e73728c471Cf1;
    address constant ROUTER = 0x2c16f75b95Cf1390c328aB70e2CEE7f4b80bD8F3;
    address constant FACTORY = 0x972cA9D1662F5e029cD18327D29026532E84c742;
    address constant POOL = 0x4ff515761864e7838B0AdB8b5b2B8B7A068905dC; // WHSK/USDT 0.3%
    uint24 constant FEE = 3000;
    address constant WHALE = address(0xBEEF);

    SentinelVault vault;
    uint256 strategyPk = 0xA11CE;
    uint256 riskPk = 0xB0B;
    address owner = address(0xFEE);

    uint32 twapWindow;
    bytes32 strategyHash = keccak256("sentinel/momentum-twap-reversion/v1");

    function setUp() public {
        vm.createSelectFork(vm.envOr("HSK_RPC", string("https://mainnet.hsk.xyz")));
        assertEq(block.chainid, 177, "must run against HSK Chain mainnet");

        // The oracle's usable window is bounded by observationCardinality * block time. Probe it
        // instead of assuming: a window the pool cannot serve must fail closed, not silently widen.
        twapWindow = _probeWindow();

        SentinelVault.Policy memory p = SentinelVault.Policy({
            maxSlippageBps: 150, // 1.5% vs TWAP, includes the 0.3% pool fee
            minEdgeBps: 80, // declared edge must clear the policy floor
            maxSpotTwapDeviationBps: 500,
            maxDrawdownBps: 1500,
            twapWindow: twapWindow,
            cooldown: 1,
            maxSlices: 4,
            maxTradeNotional: 200e6, // 200 USDT
            maxDailyNotional: 800e6
        });

        vault = new SentinelVault(owner, ROUTER, FACTORY, USDT, vm.addr(strategyPk), vm.addr(riskPk), p);

        vm.startPrank(owner);
        vault.setMarket(WHSK, FEE, true);
        vault.registerStrategy(strategyHash);
        vault.unpause();
        deal(USDT, owner, 5_000e6);
        IERC20(USDT).approve(address(vault), type(uint256).max);
        vault.deposit(USDT, 1_000e6);
        vm.stopPrank();
    }

    function _probeWindow() internal view returns (uint32) {
        uint32[4] memory candidates = [uint32(600), 420, 300, 240];
        for (uint256 i; i < candidates.length; ++i) {
            uint32[] memory sa = new uint32[](2);
            sa[0] = candidates[i];
            sa[1] = 0;
            try this.externalObserve(sa) {
                return candidates[i];
            } catch {}
        }
        revert("pool oracle has no usable window");
    }

    function externalObserve(uint32[] calldata sa) external view returns (int56[] memory a, uint160[] memory b) {
        return SentinelVaultForkTest(this).observeOnPool(sa);
    }

    function observeOnPool(uint32[] calldata sa) external view returns (int56[] memory, uint160[] memory) {
        return _observe(sa);
    }

    function _observe(uint32[] memory sa) internal view returns (int56[] memory, uint160[] memory) {
        (bool ok, bytes memory data) = POOL.staticcall(abi.encodeWithSignature("observe(uint32[])", sa));
        require(ok, "observe failed");
        return abi.decode(data, (int56[], uint160[]));
    }

    function _intent(uint128 amountIn, uint256 minOut, uint16 declaredEdge, uint64 nonce)
        internal
        view
        returns (SentinelVault.Intent memory)
    {
        return SentinelVault.Intent({
            tokenIn: USDT,
            tokenOut: WHSK,
            fee: FEE,
            amountIn: amountIn,
            minAmountOut: uint128(minOut),
            declaredEdgeBps: declaredEdge,
            strategyHash: strategyHash,
            parentId: bytes32(0),
            nonce: nonce,
            deadline: uint64(block.timestamp + 300),
            maxNotionalQuote: 200e6
        });
    }

    function _sig(uint256 pk, SentinelVault.Intent memory it) internal view returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, vault.hashIntent(it));
        return abi.encodePacked(r, s, v);
    }

    /// @dev A real swap through the real router, pushing the pool's spot away from its own TWAP. This is
    ///      the only honest way to have a dislocation to trade against on a calm venue: forge it with
    ///      volume, not by rewriting the oracle.
    function _whaleSell(uint256 whskIn) internal returns (uint256 usdtOut) {
        deal(WHSK, WHALE, whskIn);
        vm.startPrank(WHALE);
        IERC20(WHSK).approve(ROUTER, whskIn);
        bytes[] memory calls = new bytes[](1);
        calls[0] = abi.encodeCall(
            IV3SwapRouter02.exactInputSingle,
            IV3SwapRouter02.ExactInputSingleParams({
                tokenIn: WHSK,
                tokenOut: USDT,
                fee: FEE,
                recipient: WHALE,
                amountIn: whskIn,
                amountOutMinimum: 0,
                sqrtPriceLimitX96: 0
            })
        );
        bytes[] memory res = IV3SwapRouter02(ROUTER).multicall(block.timestamp + 60, calls);
        vm.stopPrank();
        usdtOut = abi.decode(res[0], (uint256));
    }

    /// @dev Sell increasing size through the pool until spot is at least `targetBps` away from the TWAP.
    function _pushDislocation(uint256 targetBps) internal returns (uint256 dislocation) {
        uint256[5] memory sizes = [uint256(2_000e18), 6_000e18, 20_000e18, 60_000e18, 180_000e18];
        for (uint256 i; i < sizes.length; ++i) {
            _whaleSell(sizes[i]);
            dislocation = _dislocation();
            if (dislocation >= targetBps) return dislocation;
        }
    }

    function _dislocation() internal view returns (uint256) {
        (int24 twapTick, int24 spotTick) = _twapAndSpot();
        return TwapLib.absDiff(spotTick, twapTick);
    }

    function _twapAndSpot() internal view returns (int24 twapTick, int24 spotTick) {
        int56[] memory tc;
        (bool ok, bytes memory data) = POOL.staticcall(abi.encodeWithSignature("observe(uint32[])", _windowArray()));
        require(ok, "observe failed");
        (tc,) = abi.decode(data, (int56[], uint160[]));
        twapTick = int24((tc[1] - tc[0]) / int56(uint56(twapWindow)));

        (bool ok2, bytes memory d2) = POOL.staticcall(abi.encodeWithSignature("slot0()"));
        require(ok2, "slot0 failed");
        (, spotTick,,,,,) = abi.decode(d2, (uint160, int24, uint16, uint16, uint16, uint8, bool));
    }

    function _windowArray() internal view returns (uint32[] memory sa) {
        sa = new uint32[](2);
        sa[0] = twapWindow;
        sa[1] = 0;
    }

    /// @notice The whole point, proved on the live chain: a real dislocation is created with a real swap,
    ///         the vault prices it from the pool's own TWAP, executes through the real router, and comes out
    ///         **ahead of TWAP** — because it bought the dip the venue was showing, not a number the agent
    ///         invented.
    function test_Fork_RealDislocationRealSwap() public {
        uint256 dislocation = _pushDislocation(100);
        (int24 twapTick, int24 spotTick) = _twapAndSpot();
        console.log("twapWindow(s)", twapWindow);
        console.log("twapTick", int256(twapTick));
        console.log("spotTick after the push", int256(spotTick));
        console.log("dislocation (bp ~ ticks)", dislocation);

        uint256 fair = TwapLib.quoteAtTick(twapTick, 100e6, USDT, WHSK); // 100 USDT -> WHSK at TWAP
        console.log("fairOut at TWAP (WHSK raw) for 100 USDT", fair);
        assertGt(fair, 0, "the oracle must produce a fair value");

        uint16 claim = uint16(dislocation < 120 ? dislocation : 120);
        assertGe(claim, 80, "the created dislocation must clear the policy floor");
        uint256 minOut = (fair * 9_970) / 10_000; // 0.3% below TWAP: the pool fee, nothing more
        SentinelVault.Intent memory it = _intent(100e6, minOut, claim, 1);

        uint256 out = vault.executeIntent(it, _sig(strategyPk, it), _sig(riskPk, it));
        console.log("amountOut (WHSK raw)", out);
        console.log("discount vs TWAP (bp, + = bought below TWAP)", int256(out) - int256(fair) > 0 ? "positive" : "negative");

        assertGe(out, minOut, "router must respect our floor");
        assertGe(out, fair, "a bought dislocation must fill at least at TWAP");
        assertEq(vault.avgExecutionEdgeBps() > 0, true, "the leg beat TWAP: the cost of the fee was paid by the dislocation");

        (uint256 qty, uint256 cost) = vault.positions(WHSK);
        assertEq(qty, out);
        assertEq(cost, 100e6);
        assertEq(IERC20(USDT).allowance(address(vault), ROUTER), 0, "allowance reset after trade");

        SentinelVault.Stats memory s = vault.stats();
        console.log("equity (USDT raw)", s.equity);
        console.log("feesPaid (USDT raw)", s.feesPaidQuote);
        console.log("executionEdgeSumBps", s.executionEdgeSumBps);
        console.log("oracleHealthy", s.oracleHealthy);
        assertTrue(s.oracleHealthy);
        assertEq(s.declaredEdgeSumBps, int256(uint256(claim)), "the claim is registered on entry");
        assertEq(s.realizedEdgeSumBps, 0, "nothing is settled until the position is exited");
        assertApproxEqAbs(s.equity, 1_000e6, 2e6, "mark-to-market of the vault");
    }

    /// @notice An exit is never gated by the claim (it declares no edge), and it settles the entry claim.
    function test_Fork_ExitSettlesTheClaim() public {
        uint256 dislocation = _pushDislocation(100);
        (int24 twapAfter,) = _twapAndSpot();
        uint256 fair = TwapLib.quoteAtTick(twapAfter, 100e6, USDT, WHSK);

        uint16 claim = uint16(dislocation < 120 ? dislocation : 120);
        SentinelVault.Intent memory buy = _intent(100e6, (fair * 9_970) / 10_000, claim, 1);
        uint256 bought = vault.executeIntent(buy, _sig(strategyPk, buy), _sig(riskPk, buy));

        vm.warp(block.timestamp + 2); // clear the 1s cooldown
        (int24 twapTick,) = _twapAndSpot();
        uint256 fairSell = TwapLib.quoteAtTick(twapTick, bought, WHSK, USDT);
        SentinelVault.Intent memory sell = SentinelVault.Intent({
            tokenIn: WHSK,
            tokenOut: USDT,
            fee: FEE,
            amountIn: uint128(bought),
            minAmountOut: uint128((fairSell * 9_850) / 10_000),
            declaredEdgeBps: 0, // an exit claims nothing
            strategyHash: strategyHash,
            parentId: bytes32(0),
            nonce: 2,
            deadline: uint64(block.timestamp + 300),
            maxNotionalQuote: type(uint64).max
        });
        uint256 proceeds = vault.executeIntent(sell, _sig(strategyPk, sell), _sig(riskPk, sell));

        (uint256 qty,) = vault.positions(WHSK);
        assertEq(qty, 0, "position closed");
        SentinelVault.Stats memory s = vault.stats();
        console.log("bought (WHSK raw)", bought);
        console.log("proceeds (USDT raw)", proceeds);
        console.log("realizedPnlQuote", s.realizedPnlQuote);
        console.log("realizedEdgeSumBps (round trip)", s.realizedEdgeSumBps);
        console.log("credibility bps of the claim", vault.edgeCredibilityBps());
        assertEq(s.declaredEdgeSumBps, int256(uint256(claim)));
        assertEq(s.tradeCount, 2);
    }

    /// @notice A floor further than `maxSlippageBps` below TWAP must be refused on the live pool too.
    function test_Fork_RevertsWhenFloorTooWide() public {
        uint256 dislocation = _pushDislocation(100);
        (int24 twapAfter,) = _twapAndSpot();
        uint256 fair = TwapLib.quoteAtTick(twapAfter, 100e6, USDT, WHSK);
        uint16 claim = uint16(dislocation < 120 ? dislocation : 120);
        SentinelVault.Intent memory it = _intent(100e6, (fair * 90) / 100, claim, 2);
        bytes memory s1 = _sig(strategyPk, it);
        bytes memory s2 = _sig(riskPk, it);
        vm.expectRevert(SentinelVault.SlippageTooWide.selector);
        vault.executeIntent(it, s1, s2);
    }

    /// @notice A claim larger than the venue's actual dislocation is refused: the anti-embellishment rule
    ///         holds against the real pool, not just the mock.
    function test_Fork_RevertsWhenClaimExceedsDislocation() public {
        (int24 twapTick, int24 spotTick) = _twapAndSpot();
        uint256 dislocation = TwapLib.absDiff(spotTick, twapTick);
        // A claim well above the dislocation, still under the policy's own deviation cap (a pool already
        // beyond that cap would be refused for a different reason, so skip instead of mis-asserting).
        uint256 target = dislocation + 80;
        if (target > 500) vm.skip(true);
        uint16 claim = uint16(target);
        uint256 fair = TwapLib.quoteAtTick(twapTick, 10e6, USDT, WHSK);
        SentinelVault.Intent memory it = _intent(10e6, (fair * 9_970) / 10_000, claim, 3);
        bytes memory s1 = _sig(strategyPk, it);
        bytes memory s2 = _sig(riskPk, it);
        vm.expectRevert(SentinelVault.EdgeClaimAboveDislocation.selector);
        vault.executeIntent(it, s1, s2);
    }

    /// @notice The unregistered-strategy rule also holds against the real contracts.
    function test_Fork_RevertsUnregisteredStrategy() public {
        SentinelVault.Intent memory it = _intent(10e6, 1, 80, 4);
        it.strategyHash = keccak256("shadow-strategy");
        bytes memory s1 = _sig(strategyPk, it);
        bytes memory s2 = _sig(riskPk, it);
        vm.expectRevert(SentinelVault.StrategyNotRegistered.selector);
        vault.executeIntent(it, s1, s2);
    }
}
