// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {SentinelVault} from "../src/SentinelVault.sol";
import {MockERC20} from "../src/mocks/MockERC20.sol";
import {MockV3Pool} from "../src/mocks/MockV3Pool.sol";
import {MockSwapRouter02} from "../src/mocks/MockSwapRouter02.sol";
import {MockFactory} from "../src/mocks/MockFactory.sol";
import {TwapLib} from "../src/libraries/TwapLib.sol";

/// @notice Unit / fuzz / invariant tests for the policy kernel, against controllable mocks.
///         Real HSKSwap behaviour is covered by SentinelVault.fork.t.sol.
contract SentinelVaultTest is Test {
    // 1.0001^-301487 ≈ 8.08e-14 → 1 WHSK ≈ 0.0808 USDT, the price measured on HSKSwap mainnet.
    int24 internal constant TICK = -301_487;
    uint24 internal constant FEE = 3000;
    uint64 internal constant MAX_TRADE = 200e6; // 200 USDT
    uint64 internal constant MAX_DAILY = 1000e6; // 1,000 USDT

    SentinelVault internal vault;
    MockERC20 internal usdt;
    MockERC20 internal whsk;
    MockV3Pool internal pool;
    MockSwapRouter02 internal router;
    MockFactory internal factory;

    uint256 internal strategyPk = 0xA11CE;
    uint256 internal riskPk = 0xB0B;
    address internal strategy;
    address internal risk;
    address internal human = address(0x4011);
    address internal owner = address(0xFEE);

    bytes32 internal constant STRATEGY = keccak256("sentinel/momentum-twap-reversion/v1");

    function setUp() public {
        usdt = new MockERC20("Tether USD", "USDT", 6);
        whsk = new MockERC20("Wrapped HSK", "WHSK", 18);
        pool = new MockV3Pool(TICK, TICK);
        router = new MockSwapRouter02();
        factory = new MockFactory();
        factory.setPool(address(whsk), address(usdt), FEE, address(pool));

        strategy = vm.addr(strategyPk);
        risk = vm.addr(riskPk);

        SentinelVault.Policy memory p = SentinelVault.Policy({
            maxSlippageBps: 100, // 1% vs TWAP
            minEdgeBps: 80, // declared edge must clear 80 bps
            maxSpotTwapDeviationBps: 300,
            maxDrawdownBps: 1500,
            twapWindow: 600,
            cooldown: 60,
            maxSlices: 4,
            maxTradeNotional: MAX_TRADE,
            maxDailyNotional: MAX_DAILY
        });

        vault = new SentinelVault(owner, address(router), address(factory), address(usdt), strategy, risk, p);

        vm.prank(owner);
        vault.setMarket(address(whsk), FEE, true);
        vm.prank(owner);
        vault.registerStrategy(STRATEGY);
        vm.prank(owner);
        vault.unpause();

        // fund the vault with 10,000 USDT worth of liquidity
        usdt.mint(owner, 10_000e6);
        vm.startPrank(owner);
        usdt.approve(address(vault), type(uint256).max);
        vault.deposit(address(usdt), 10_000e6);
        vm.stopPrank();

        // the router needs inventory to pay out
        whsk.mint(address(router), 1_000_000e18);
        usdt.mint(address(router), 10_000e6);
    }

    // ---------------------------------------------------------------------------------------------
    // helpers
    // ---------------------------------------------------------------------------------------------

    function _fairOut(uint256 amountIn, bool buy) internal view returns (uint256) {
        return TwapLib.quoteAtTick(
            TICK, amountIn, buy ? address(usdt) : address(whsk), buy ? address(whsk) : address(usdt)
        );
    }

    function _intent(uint128 amountIn, uint256 minOut, uint16 declaredEdge, uint64 nonce)
        internal
        view
        returns (SentinelVault.Intent memory it)
    {
        it = SentinelVault.Intent({
            tokenIn: address(usdt),
            tokenOut: address(whsk),
            fee: FEE,
            amountIn: amountIn,
            minAmountOut: uint128(minOut),
            declaredEdgeBps: declaredEdge,
            strategyHash: STRATEGY,
            parentId: bytes32(0),
            nonce: nonce,
            deadline: uint64(block.timestamp + 300),
            maxNotionalQuote: MAX_TRADE
        });
    }

    function _sellIntent(uint128 amountIn, uint256 minOut, uint16 declaredEdge, uint64 nonce)
        internal
        view
        returns (SentinelVault.Intent memory it)
    {
        it = _intent(amountIn, minOut, declaredEdge, nonce);
        it.tokenIn = address(whsk);
        it.tokenOut = address(usdt);
        it.maxNotionalQuote = type(uint64).max; // an exit is sized by the position, not by the entry cap
    }

    function _sig(uint256 pk, SentinelVault.Intent memory it) internal view returns (bytes memory) {
        bytes32 digest = vault.hashIntent(it);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, digest);
        return abi.encodePacked(r, s, v);
    }

    function _expectRevert(bytes4 selector, SentinelVault.Intent memory it) internal {
        bytes memory s1 = _sig(strategyPk, it);
        bytes memory s2 = _sig(riskPk, it);
        vm.expectRevert(selector);
        vault.executeIntent(it, s1, s2);
    }

    function _exec(SentinelVault.Intent memory it) internal returns (uint256 out) {
        bytes memory s1 = _sig(strategyPk, it);
        bytes memory s2 = _sig(riskPk, it);
        return vault.executeIntent(it, s1, s2);
    }

    // ---------------------------------------------------------------------------------------------
    // happy paths
    // ---------------------------------------------------------------------------------------------

    function test_Buy_ExecutesAndAccounts() public {
        uint64 amountIn = 50e6; // 50 USDT
        uint256 fair = _fairOut(amountIn, true);
        router.setNextAmountOut(fair);

        SentinelVault.Intent memory it = _intent(amountIn, fair, 120, 1);
        uint256 out = _exec(it);

        assertEq(out, fair, "fill equals fair value");
        assertEq(vault.tradeCount(), 1);
        assertEq(vault.feesPaidQuote(), uint256(amountIn) * FEE / 1_000_000, "fee booked in quote units");

        (uint256 qty, uint256 cost) = vault.positions(address(whsk));
        assertEq(qty, fair);
        assertEq(cost, amountIn);

        assertEq(vault.dailyNotional(), amountIn);
        assertEq(IERC20(address(usdt)).allowance(address(vault), address(router)), 0, "allowance reset");

        SentinelVault.Stats memory s = vault.stats();
        assertEq(s.declaredEdgeSumBps, 120);
        assertEq(s.realizedEdgeSumBps, 0, "filled at TWAP: zero realized edge");
        assertApproxEqAbs(s.equity, 10_000e6, 2, "no value leaked beyond rounding");
    }

    function test_Sell_RealisesPnlAndCountsWins() public {
        // buy at TWAP
        uint256 fairBuy = _fairOut(50e6, true);
        router.setNextAmountOut(fairBuy);
        _exec(_intent(50e6, fairBuy, 100, 1));

        // sell the whole position 5% above the price paid (simulated favourable fill)
        uint256 fairSell = _fairOut(fairBuy, false);
        uint256 better = fairSell + (fairSell * 5) / 100;
        router.setNextAmountOut(better);

        vm.warp(block.timestamp + 120); // clear the cooldown
        uint256 minOut = (fairSell * 99) / 100;
        uint256 out = _exec(_sellIntent(uint128(fairBuy), minOut, 100, 2));

        assertEq(out, better);
        assertEq(vault.tradeCount(), 2);
        assertGt(vault.realizedPnlQuote(), 0, "profit booked");
        assertEq(vault.winCount(), 1);

        (uint256 qty,) = vault.positions(address(whsk));
        assertEq(qty, 0, "position closed");

        // realized edge is measured against TWAP, so a better-than-TWAP fill must be positive
        assertGt(vault.realizedEdgeSumBps(), 0);
        // credibility = realized / declared, in bps of the claim
        assertGt(vault.edgeCredibilityBps(), 0);
    }

    function test_NoTradeDecisionIsRecorded() public {
        vm.expectEmit(true, false, false, true, address(vault));
        emit SentinelVault.NoTradeDecision(STRATEGY, 30, keccak256("hurdle"), 7);
        vm.prank(strategy);
        vault.recordNoTrade(STRATEGY, 30, keccak256("hurdle"), 7);
    }

    // ---------------------------------------------------------------------------------------------
    // policy rejections
    // ---------------------------------------------------------------------------------------------

    function test_Revert_WhenPaused() public {
        vm.prank(owner);
        vault.pause();
        SentinelVault.Intent memory it = _intent(50e6, _fairOut(50e6, true), 100, 1);
        _expectRevert(SentinelVault.VaultPaused.selector, it);
    }

    function test_Revert_WhenStrategyNotRegistered() public {
        SentinelVault.Intent memory it = _intent(50e6, _fairOut(50e6, true), 100, 1);
        it.strategyHash = keccak256("unregistered");
        _expectRevert(SentinelVault.StrategyNotRegistered.selector, it);
    }

    function test_Revert_WhenEdgeBelowHurdle() public {
        SentinelVault.Intent memory it = _intent(50e6, _fairOut(50e6, true), 79, 1); // hurdle is 80
        _expectRevert(SentinelVault.EdgeBelowHurdle.selector, it);
    }

    function test_Revert_WhenMinOutAboveFair() public {
        uint256 fair = _fairOut(50e6, true);
        SentinelVault.Intent memory it = _intent(50e6, fair + 1, 100, 1);
        _expectRevert(SentinelVault.MinOutAboveFair.selector, it);
    }

    function test_Revert_WhenSlippageTooWide() public {
        uint256 fair = _fairOut(50e6, true);
        SentinelVault.Intent memory it = _intent(50e6, (fair * 98) / 100, 100, 1); // 2% > 1% cap
        _expectRevert(SentinelVault.SlippageTooWide.selector, it);
    }

    function test_Revert_WhenNonceReplayed() public {
        uint256 fair = _fairOut(50e6, true);
        router.setNextAmountOut(fair);
        _exec(_intent(50e6, fair, 100, 42));
        vm.warp(block.timestamp + 120);
        SentinelVault.Intent memory it = _intent(10e6, _fairOut(10e6, true), 100, 42);
        _expectRevert(SentinelVault.NonceUsed.selector, it);
    }

    function test_Revert_WhenExpired() public {
        SentinelVault.Intent memory it = _intent(50e6, _fairOut(50e6, true), 100, 1);
        it.deadline = uint64(block.timestamp - 1);
        _expectRevert(SentinelVault.Expired.selector, it);
    }

    function test_Revert_WhenRiskSignatureMissing() public {
        uint256 fair = _fairOut(50e6, true);
        SentinelVault.Intent memory it = _intent(50e6, fair, 100, 1);
        bytes memory s1 = _sig(strategyPk, it);
        bytes memory wrong = _sig(0xDEAD, it);
        vm.expectRevert(SentinelVault.BadRiskSignature.selector);
        vault.executeIntent(it, s1, wrong);
    }

    function test_Revert_WhenSignatureCoversDifferentAmount() public {
        // the agent signs 50 USDT but executes 500: the digest must not match
        SentinelVault.Intent memory signed = _intent(50e6, _fairOut(50e6, true), 100, 1);
        SentinelVault.Intent memory claimed = _intent(500e6, _fairOut(500e6, true), 100, 1);
        bytes memory s1 = _sig(strategyPk, signed);
        bytes memory s2 = _sig(riskPk, signed);
        vm.expectRevert(SentinelVault.BadStrategySignature.selector);
        vault.executeIntent(claimed, s1, s2);
    }

    function test_Revert_WhenTradeTooLarge() public {
        uint128 amountIn = uint128(MAX_TRADE) + 1;
        SentinelVault.Intent memory it = _intent(amountIn, _fairOut(amountIn, true), 100, 1);
        _expectRevert(SentinelVault.TradeTooLarge.selector, it);
    }

    function test_Revert_WhenDailyLimitExceeded() public {
        // 5 × 200 USDT = 1,000 (the cap); the 6th must fail even though each is legal
        bytes32 parent = keccak256("parent");
        vm.prank(strategy);
        vault.openParent(parent, MAX_TRADE * 4);
        for (uint64 i = 1; i <= 5; ++i) {
            if (i > 4) {
                // 5th slice: a new parent-free order would hit the cooldown, so use a second parent
                bytes32 p2 = keccak256("parent2");
                vm.prank(strategy);
                vault.openParent(p2, MAX_TRADE);
            }
            uint256 fair = _fairOut(MAX_TRADE, true);
            router.setNextAmountOut(fair);
            SentinelVault.Intent memory it = _intent(uint128(MAX_TRADE), fair, 100, i);
            it.parentId = i > 4 ? keccak256("parent2") : parent;
            _exec(it);
        }
        uint256 fair = _fairOut(MAX_TRADE, true);
        router.setNextAmountOut(fair);
        SentinelVault.Intent memory it = _intent(uint128(MAX_TRADE), fair, 100, 99);
        it.parentId = bytes32(0);
        _expectRevert(SentinelVault.DailyLimitExceeded.selector, it);
    }

    function test_Revert_WhenSpotDeviatesFromTwap() public {
        pool.setSpot(TICK + 400); // 400 "bps" away, cap is 300
        SentinelVault.Intent memory it = _intent(50e6, _fairOut(50e6, true), 100, 1);
        _expectRevert(SentinelVault.PriceDeviated.selector, it);
    }

    function test_Revert_WhenOracleNotReady() public {
        pool.setObserveReverts(true);
        SentinelVault.Intent memory it = _intent(50e6, _fairOut(50e6, true), 100, 1);
        _expectRevert(TwapLib.OracleNotReady.selector, it);
    }

    function test_Revert_WhenCooldownActive() public {
        uint256 fair = _fairOut(20e6, true);
        router.setNextAmountOut(fair);
        _exec(_intent(20e6, fair, 100, 1));
        // second independent order one second later
        SentinelVault.Intent memory it = _intent(20e6, fair, 100, 2);
        _expectRevert(SentinelVault.CooldownActive.selector, it);
    }

    /// @dev Regression: an exit bigger than the per-trade cap must still be executable (bounded by
    ///      daily cap and TWAP slippage), otherwise a drawdown could be impossible to exit.
    function test_ExitLargerThanTradeCapIsAllowed() public {
        bytes32 parent = keccak256("big");
        vm.prank(strategy);
        vault.openParent(parent, 600e6);
        uint256 fair = _fairOut(200e6, true);
        router.setNextAmountOut(fair);
        for (uint64 i = 1; i <= 3; ++i) {
            SentinelVault.Intent memory it = _intent(200e6, fair, 100, i);
            it.parentId = parent;
            _exec(it);
        }
        (uint256 qty,) = vault.positions(address(whsk));
        assertGt(qty, 0);

        vm.warp(block.timestamp + 120);
        uint256 fairSell = _fairOut(qty, false);
        assertGt(fairSell, MAX_TRADE, "exit notional exceeds the per-trade cap");
        router.setNextAmountOut(fairSell);
        _exec(_sellIntent(uint128(qty), (fairSell * 99) / 100, 100, 9));
        (, uint256 cost) = vault.positions(address(whsk));
        assertEq(cost, 0);
    }

    // ---------------------------------------------------------------------------------------------
    // drawdown
    // ---------------------------------------------------------------------------------------------

    function test_DrawdownBlocksBuysButAllowsExits() public {
        // Make the position dominate the book, otherwise a 20% price move is noise on a large balance.
        vm.prank(owner);
        vault.withdraw(address(usdt), owner, 9_500e6); // vault keeps 500 USDT

        bytes32 parent = keccak256("drawdown");
        vm.prank(strategy);
        vault.openParent(parent, 400e6);

        uint256 fairBuy = _fairOut(200e6, true);
        router.setNextAmountOut(fairBuy);
        for (uint64 i = 1; i <= 2; ++i) {
            SentinelVault.Intent memory it = _intent(200e6, fairBuy, 100, i);
            it.parentId = parent;
            _exec(it);
        }
        (uint256 qty,) = vault.positions(address(whsk));
        assertEq(qty, fairBuy * 2);
        assertApproxEqAbs(vault.highWater(), 500e6, 2);

        // price collapses 20% (cap is 15%)
        int24 crashed = TICK - 2231; // ln(0.8)/ln(1.0001)
        pool.setTwap(crashed);
        pool.setSpot(crashed);

        uint256 eq = vault.equity();
        assertLt(eq, (vault.highWater() * 8500) / 10_000, "drawdown beyond 15%");

        vm.warp(block.timestamp + 120);
        uint256 fairBuyCrashed = TwapLib.quoteAtTick(crashed, 10e6, address(usdt), address(whsk));
        SentinelVault.Intent memory buy = _intent(10e6, fairBuyCrashed, 100, 3);
        _expectRevert(SentinelVault.DrawdownBreach.selector, buy);

        // exits are still allowed: sell the position into the quote token
        uint256 fairSell = TwapLib.quoteAtTick(crashed, qty, address(whsk), address(usdt));
        router.setNextAmountOut(fairSell);
        SentinelVault.Intent memory sell = _sellIntent(uint128(qty), (fairSell * 99) / 100, 100, 4);
        _exec(sell);
        (, uint256 cost) = vault.positions(address(whsk));
        assertEq(cost, 0, "position exited");
    }

    // ---------------------------------------------------------------------------------------------
    // impact-budgeted slicing
    // ---------------------------------------------------------------------------------------------

    function test_ParentBudgetAndSliceCount() public {
        bytes32 parent = keccak256("p1");
        vm.prank(strategy);
        vault.openParent(parent, 300e6); // budget 300 USDT

        uint256 fair = _fairOut(100e6, true);
        router.setNextAmountOut(fair);
        for (uint64 i = 1; i <= 3; ++i) {
            SentinelVault.Intent memory it = _intent(100e6, fair, 100, i);
            it.parentId = parent;
            _exec(it);
        }
        (uint64 budget, uint64 spent, uint8 slices, bool open) = vault.parents(parent);
        assertEq(budget, 300e6);
        assertEq(spent, 300e6);
        assertEq(slices, 3);
        assertTrue(open);

        // 4th slice would exceed the budget
        vm.warp(block.timestamp + 120);
        SentinelVault.Intent memory over = _intent(100e6, fair, 100, 11);
        over.parentId = parent;
        _expectRevert(SentinelVault.ParentBudgetExceeded.selector, over);
    }

    function test_ParentSliceCountCap() public {
        bytes32 parent = keccak256("p2");
        vm.prank(strategy);
        vault.openParent(parent, MAX_TRADE * 4);

        uint256 fair = _fairOut(10e6, true);
        router.setNextAmountOut(fair);
        for (uint64 i = 1; i <= 4; ++i) {
            SentinelVault.Intent memory it = _intent(10e6, fair, 100, i);
            it.parentId = parent;
            _exec(it);
        }
        vm.warp(block.timestamp + 120);
        SentinelVault.Intent memory fifth = _intent(10e6, fair, 100, 5);
        fifth.parentId = parent;
        _expectRevert(SentinelVault.ParentSlicesExceeded.selector, fifth);
    }

    // ---------------------------------------------------------------------------------------------
    // access control
    // ---------------------------------------------------------------------------------------------

    function test_OnlyOwnerCanRegisterStrategy() public {
        vm.prank(strategy);
        vm.expectRevert(SentinelVault.NotOwner.selector);
        vault.registerStrategy(keccak256("self-certified"));
    }

    function test_RiskKeyCanStopButNotRestart() public {
        vm.prank(risk);
        vault.pause();
        assertTrue(vault.paused());

        vm.prank(risk);
        vm.expectRevert(SentinelVault.NotOwner.selector);
        vault.unpause();
    }

    function test_StrategyKeyCannotWithdraw() public {
        vm.prank(strategy);
        vm.expectRevert(SentinelVault.NotOwner.selector);
        vault.withdraw(address(usdt), strategy, 1);
    }

    // ---------------------------------------------------------------------------------------------
    // fuzz & invariants
    // ---------------------------------------------------------------------------------------------

    /// @dev Any legal size within the per-trade cap must execute and leave the vault consistent.
    function testFuzz_LegalSizesExecuteCleanly(uint64 seed) public {
        uint128 amountIn = uint128(bound(seed, 1e6, MAX_TRADE)); // 1 USDT .. 200 USDT
        uint256 fair = _fairOut(amountIn, true);
        vm.assume(fair > 0);
        router.setNextAmountOut(fair);

        uint256 before = vault.equity();
        _exec(_intent(amountIn, fair, 100, 1));

        assertEq(vault.tradeCount(), 1);
        assertLe(vault.dailyNotional(), MAX_DAILY);
        assertEq(IERC20(address(usdt)).allowance(address(vault), address(router)), 0);
        assertApproxEqAbs(vault.equity(), before, 2, "swap at TWAP must not destroy value");
    }

    /// @dev The declared-edge rule is a hard predicate: below the hurdle always reverts.
    function testFuzz_EdgeHurdleIsMonotonic(uint16 declared) public {
        declared = uint16(bound(declared, 0, 5000));
        uint256 fair = _fairOut(10e6, true);
        router.setNextAmountOut(fair);
        SentinelVault.Intent memory it = _intent(10e6, fair, declared, 1);

        if (declared < 80) {
            _expectRevert(SentinelVault.EdgeBelowHurdle.selector, it);
        } else {
            _exec(it);
            assertEq(vault.tradeCount(), 1);
        }
    }

    /// @dev A router that pays less than the signed minimum must abort the whole trade.
    function test_Revert_WhenRouterUnderpays() public {
        uint256 fair = _fairOut(50e6, true);
        router.setNextAmountOut(fair - 1);
        router.setIgnoreMinOut(true); // simulate a malicious/broken router
        SentinelVault.Intent memory it = _intent(50e6, fair, 100, 1);
        _expectRevert(SentinelVault.InsufficientOutput.selector, it);
    }
}
