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
        pool = new MockV3Pool(TICK + 200, TICK); // spot 200bp above TWAP: a real dislocation to trade against
        router = new MockSwapRouter02();
        factory = new MockFactory();
        factory.setPool(address(whsk), address(usdt), FEE, address(pool));

        strategy = vm.addr(strategyPk);
        risk = vm.addr(riskPk);

        SentinelVault.Policy memory p = _policy();

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

    function _policy() internal pure returns (SentinelVault.Policy memory) {
        return SentinelVault.Policy({
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

    function _fairOutAt(int24 tick, uint256 amountIn, bool buy) internal view returns (uint256) {
        return TwapLib.quoteAtTick(
            tick, amountIn, buy ? address(usdt) : address(whsk), buy ? address(whsk) : address(usdt)
        );
    }

    function _sigOn(SentinelVault v, uint256 pk, SentinelVault.Intent memory it)
        internal
        view
        returns (bytes memory)
    {
        (uint8 v_, bytes32 r, bytes32 s) = vm.sign(pk, v.hashIntent(it));
        return abi.encodePacked(r, s, v_);
    }

    function _selectorFor(string memory expect) internal pure returns (bytes4) {
        bytes32 h = keccak256(bytes(expect));
        if (h == keccak256("EDGE_BELOW_HURDLE")) return SentinelVault.EdgeBelowHurdle.selector;
        if (h == keccak256("SLIPPAGE_WINDOW")) return SentinelVault.SlippageTooWide.selector;
        if (h == keccak256("MIN_OUT_ABOVE_FAIR")) return SentinelVault.MinOutAboveFair.selector;
        if (h == keccak256("TRADE_TOO_LARGE")) return SentinelVault.TradeTooLarge.selector;
        if (h == keccak256("EXCEEDS_INTENT_CAP")) return SentinelVault.ExceedsIntentCap.selector;
        if (h == keccak256("SPOT_DEVIATION")) return SentinelVault.PriceDeviated.selector;
        if (h == keccak256("DAILY_LIMIT")) return SentinelVault.DailyLimitExceeded.selector;
        revert("unknown expectation in the shared table");
    }

    // ---------------------------------------------------------------------------------------------
    // Cross-language parity: the same table, read by the contract here and by the Python mirror
    // ---------------------------------------------------------------------------------------------

    /// @dev `test/fixtures/policy_scenarios.json` is the single source of truth for what each policy
    ///      predicate must decide. The Python mirror (`tests/test_policy_mirror.py`) reads the same file
    ///      and must reach the same verdict, so the two implementations cannot drift apart silently.
    function test_SharedPolicyTable() public {
        string memory json = vm.readFile("test/fixtures/policy_scenarios.json");
        int24 baseTick = int24(vm.parseJsonInt(json, ".tick"));
        uint256 n = vm.parseJsonUint(json, ".count");
        assertGt(n, 0, "shared table must not be empty");

        for (uint256 i; i < n; ++i) {
            string memory at = string.concat(".scenarios[", vm.toString(i), "]");
            _runSharedScenario(
                vm.parseJsonString(json, string.concat(at, ".id")),
                baseTick,
                vm.parseJsonUint(json, string.concat(at, ".amountIn")),
                vm.parseJsonUint(json, string.concat(at, ".minOutBpsOfFair")),
                uint16(vm.parseJsonUint(json, string.concat(at, ".declaredEdgeBps"))),
                uint64(vm.parseJsonUint(json, string.concat(at, ".maxNotionalQuote"))),
                int24(int256(vm.parseJsonInt(json, string.concat(at, ".spotOffsetTicks")))),
                uint64(vm.parseJsonUint(json, string.concat(at, ".preSpendQuote"))),
                uint64(vm.parseJsonUint(json, string.concat(at, ".maxDailyNotional"))),
                vm.parseJsonString(json, string.concat(at, ".expect"))
            );
        }
    }

    function _runSharedScenario(
        string memory id,
        int24 baseTick,
        uint256 amountIn,
        uint256 minOutBps,
        uint16 declaredEdge,
        uint64 intentCap,
        int24 spotOffset,
        uint64 preSpend,
        uint64 dailyCap,
        string memory expect
    ) internal {
        SentinelVault.Policy memory p = _policy();
        p.maxDailyNotional = dailyCap;
        SentinelVault v =
            new SentinelVault(owner, address(router), address(factory), address(usdt), strategy, risk, p);
        vm.startPrank(owner);
        v.setMarket(address(whsk), FEE, true);
        v.registerStrategy(STRATEGY);
        v.unpause();
        usdt.mint(owner, 5_000e6);
        usdt.approve(address(v), type(uint256).max);
        v.deposit(address(usdt), 5_000e6);
        vm.stopPrank();

        pool.setSpot(baseTick + spotOffset);
        uint64 nonce;
        if (preSpend > 0) {
            uint256 preFair = _fairOutAt(baseTick, preSpend, true);
            router.setNextAmountOut(preFair);
            SentinelVault.Intent memory pre = SentinelVault.Intent({
                tokenIn: address(usdt),
                tokenOut: address(whsk),
                fee: FEE,
                amountIn: uint128(preSpend),
                minAmountOut: uint128((preFair * 9_990) / 10_000),
                declaredEdgeBps: 120,
                strategyHash: STRATEGY,
                parentId: bytes32(0),
                nonce: nonce,
                deadline: uint64(block.timestamp + 300),
                maxNotionalQuote: preSpend
            });
            v.executeIntent(pre, _sigOn(v, strategyPk, pre), _sigOn(v, riskPk, pre));
            nonce += 1;
        }

        uint256 fair = _fairOutAt(baseTick, amountIn, true);
        SentinelVault.Intent memory it = SentinelVault.Intent({
            tokenIn: address(usdt),
            tokenOut: address(whsk),
            fee: FEE,
            amountIn: uint128(amountIn),
            minAmountOut: uint128((fair * minOutBps) / 10_000),
            declaredEdgeBps: declaredEdge,
            strategyHash: STRATEGY,
            parentId: bytes32(0),
            nonce: nonce,
            deadline: uint64(block.timestamp + 300),
            maxNotionalQuote: intentCap
        });
        router.setNextAmountOut(fair);
        bytes memory s1 = _sigOn(v, strategyPk, it);
        bytes memory s2 = _sigOn(v, riskPk, it);

        if (keccak256(bytes(expect)) == keccak256("OK")) {
            v.executeIntent(it, s1, s2);
            assertEq(v.tradeCount(), preSpend > 0 ? 2 : 1, id);
        } else {
            vm.expectRevert(_selectorFor(expect));
            v.executeIntent(it, s1, s2);
        }
        pool.setSpot(baseTick);
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
        uint256 out = _exec(_sellIntent(uint128(fairBuy), minOut, 0, 2));

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
        emit SentinelVault.NoTradeDecision(STRATEGY, 30, 113, keccak256("hurdle"), 7);
        vm.prank(strategy);
        vault.recordNoTrade(STRATEGY, 30, 113, keccak256("hurdle"), 7);
    }

    /// @dev A refusal and a trade share one nonce register, so a decision cannot be logged twice or
    ///      replayed, and the same nonce cannot later be spent on a different decision.
    function test_NoTradeConsumesNonce() public {
        vm.startPrank(strategy);
        vault.recordNoTrade(STRATEGY, 30, 113, keccak256("hurdle"), 9);
        vm.expectRevert(SentinelVault.NonceUsed.selector);
        vault.recordNoTrade(STRATEGY, 30, 113, keccak256("hurdle"), 9);
        vm.stopPrank();

        // the same nonce is also gone for a trade
        uint256 fair = _fairOut(10e6, true);
        router.setNextAmountOut(fair);
        vm.warp(block.timestamp + 120);
        _expectRevert(SentinelVault.NonceUsed.selector, _intent(10e6, fair, 120, 9));
    }

    /// @dev A refusal may not claim it was judged against a softer hurdle than the vault enforces.
    function test_NoTradeRejectsHurdleBelowFloor() public {
        vm.prank(strategy);
        vm.expectRevert(SentinelVault.EdgeBelowHurdle.selector);
        vault.recordNoTrade(STRATEGY, 30, 40, keccak256("hurdle"), 11);
    }

    function test_NoTradeRequiresRegisteredStrategy() public {
        vm.prank(strategy);
        vm.expectRevert(SentinelVault.StrategyNotRegistered.selector);
        vault.recordNoTrade(keccak256("shadow"), 30, 113, keccak256("hurdle"), 12);
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
        _exec(_sellIntent(uint128(qty), (fairSell * 99) / 100, 0, 9));
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
        pool.setSpot(crashed + 200); // the pool is dislocated again, so the buy reaches the drawdown gate

        uint256 eq = vault.equity();
        assertLt(eq, (vault.highWater() * 8500) / 10_000, "drawdown beyond 15%");

        vm.warp(block.timestamp + 120);
        uint256 fairBuyCrashed = TwapLib.quoteAtTick(crashed, 10e6, address(usdt), address(whsk));
        SentinelVault.Intent memory buy = _intent(10e6, fairBuyCrashed, 100, 3);
        _expectRevert(SentinelVault.DrawdownBreach.selector, buy);

        // exits are still allowed: sell the position into the quote token
        uint256 fairSell = TwapLib.quoteAtTick(crashed, qty, address(whsk), address(usdt));
        router.setNextAmountOut(fairSell);
        SentinelVault.Intent memory sell = _sellIntent(uint128(qty), (fairSell * 99) / 100, 0, 4);
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

    /// @dev The edge hurdle is the vault's differentiator: a policy that switches it off must not exist.
    function test_Revert_SetPolicyTurnsOffEdgeHurdle() public {
        SentinelVault.Policy memory p = _policy();
        p.minEdgeBps = 0;
        vm.prank(owner);
        vm.expectRevert(SentinelVault.InvalidConfig.selector);
        vault.setPolicy(p);

        p.minEdgeBps = 5_001; // beyond the plausible-spread ceiling
        vm.prank(owner);
        vm.expectRevert(SentinelVault.InvalidConfig.selector);
        vault.setPolicy(p);

        p.minEdgeBps = 5_000; // the bound itself is legal
        vm.prank(owner);
        vault.setPolicy(p);
        (uint16 maxSlip, uint16 minEdge,,,,,,,) = vault.policy();
        assertEq(minEdge, 5_000);
        assertEq(maxSlip, 100);
    }

    /// @dev Same predicate in the constructor: a vault cannot be born with the gate switched off.
    function test_Revert_ConstructorRejectsZeroEdgeHurdle() public {
        SentinelVault.Policy memory p = _policy();
        p.minEdgeBps = 0;
        vm.expectRevert(SentinelVault.InvalidConfig.selector);
        new SentinelVault(owner, address(router), address(factory), address(usdt), strategy, risk, p);
    }

    function test_Revert_ConstructorRejectsAbsurdCooldown() public {
        SentinelVault.Policy memory p = _policy();
        p.cooldown = uint32(8 days);
        vm.expectRevert(SentinelVault.InvalidConfig.selector);
        new SentinelVault(owner, address(router), address(factory), address(usdt), strategy, risk, p);
    }

    function test_OwnerCanCloseParent() public {
        bytes32 parent = keccak256("owner-close");
        vm.prank(strategy);
        vault.openParent(parent, 100e6);
        vm.prank(owner);
        vault.closeParent(parent);
        (,,, bool open) = vault.parents(parent);
        assertFalse(open);
    }

    /// @dev Withdrawing the position asset must take its cost basis with it, or the book keeps claiming
    ///      a position the vault no longer holds and a later sale underflows the basis.
    function test_WithdrawAssetKeepsPositionBookConsistent() public {
        uint256 fair = _fairOut(100e6, true);
        router.setNextAmountOut(fair);
        _exec(_intent(100e6, fair, 100, 1));

        uint256 half = fair / 2;
        vm.prank(owner);
        vault.withdraw(address(whsk), owner, half);

        (uint256 qty, uint256 cost) = vault.positions(address(whsk));
        assertEq(qty, fair - half);
        assertApproxEqAbs(cost, 50e6, 2, "half the basis left with half the tokens");

        // the remainder is still sellable without touching the withdrawn part
        vm.warp(block.timestamp + 120);
        uint256 fairSell = _fairOut(fair - half, false);
        router.setNextAmountOut(fairSell);
        _exec(_sellIntent(uint128(fair - half), (fairSell * 99) / 100, 0, 2));
        (qty, cost) = vault.positions(address(whsk));
        assertEq(qty, 0);
        assertEq(cost, 0);
    }

    /// @dev An oracle outage must not make the vault unauditable: the trading path fails closed, the
    ///      reporting view still answers and says the mark is partly cost-basis.
    function test_StatsSurvivesOracleOutage() public {
        uint256 fair = _fairOut(100e6, true);
        router.setNextAmountOut(fair);
        _exec(_intent(100e6, fair, 120, 1));

        pool.setObserveReverts(true);

        (uint256 eq, bool fullyPriced) = vault.tryEquity();
        assertFalse(fullyPriced, "flag must say the mark is not a TWAP mark");
        assertGt(eq, 0);

        SentinelVault.Stats memory s = vault.stats();
        assertFalse(s.oracleHealthy);
        assertEq(s.tradeCount, 1);

        vm.expectRevert(SentinelVault.OracleUnavailable.selector);
        vault.equity();
    }

    function test_StrategyKeyCannotWithdraw() public {
        vm.prank(strategy);
        vm.expectRevert(SentinelVault.NotOwner.selector);
        vault.withdraw(address(usdt), strategy, 1);
    }

    /// @dev An exit must never be blocked by the claim gate: refusing to reduce risk because the expected
    ///      move is small is the same trap the per-trade cap had. It still respects the TWAP band.
    function test_ExitIsNotGatedByEdgeHurdle() public {
        uint256 fair = _fairOut(100e6, true);
        router.setNextAmountOut(fair);
        _exec(_intent(100e6, fair, 120, 1));

        vm.warp(block.timestamp + 120);
        uint256 fairSell = _fairOut(fair, false);
        router.setNextAmountOut(fairSell);
        uint256 out = _exec(_sellIntent(uint128(fair), (fairSell * 99) / 100, 0, 2));
        assertEq(out, fairSell);
        (uint256 qty,) = vault.positions(address(whsk));
        assertEq(qty, 0, "position closed with a zero-edge claim");
    }

    /// @dev A claim larger than the dislocation the venue was showing is rejected: you cannot claim a move
    ///      that was not there to capture.
    function test_Revert_WhenClaimExceedsObservedDislocation() public {
        uint256 fair = _fairOut(50e6, true);
        router.setNextAmountOut(fair);
        pool.setSpot(TICK + 100); // 100bp of dislocation
        SentinelVault.Intent memory it = _intent(50e6, fair, 120, 1); // but claiming 120bp
        _expectRevert(SentinelVault.EdgeClaimAboveDislocation.selector, it);
    }

    /// @dev An exit must not be delayed by a rate limit meant for capital deployment.
    function test_ExitIsNotBlockedByCooldown() public {
        uint256 fair = _fairOut(100e6, true);
        router.setNextAmountOut(fair);
        _exec(_intent(100e6, fair, 120, 1));

        // same block: an entry would be refused, an exit may still run
        SentinelVault.Intent memory anotherBuy = _intent(10e6, _fairOut(10e6, true), 120, 2);
        _expectRevert(SentinelVault.CooldownActive.selector, anotherBuy);

        uint256 fairSell = _fairOut(fair, false);
        router.setNextAmountOut(fairSell);
        uint256 out = _exec(_sellIntent(uint128(fair), (fairSell * 99) / 100, 0, 3));
        assertEq(out, fairSell);
        (uint256 qty,) = vault.positions(address(whsk));
        assertEq(qty, 0);
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

    /// @dev The declared-edge rule is a hard predicate: below the hurdle always reverts, and a claim can
    ///      never exceed the dislocation the pool is actually showing (200bp in this fixture).
    function testFuzz_EdgeHurdleIsMonotonic(uint16 declared) public {
        declared = uint16(bound(declared, 0, 200));
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
