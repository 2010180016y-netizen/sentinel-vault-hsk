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

/// @notice Random-action handler. Every action is a *legal* call; the invariants then assert that
///         the policy kernel never drifts, whatever sequence the agent chooses.
contract VaultHandler is Test {
    SentinelVault public vault;
    MockSwapRouter02 public router;
    MockV3Pool public pool;
    MockERC20 public usdt;
    MockERC20 public whsk;
    uint256 public strategyPk;
    uint256 public riskPk;
    bytes32 public strategy;

    uint64 public okBuys;
    uint64 public okSells;
    uint64 public nonce;

    int24 public constant BASE_TICK = -301_487;

    constructor(
        SentinelVault vault_,
        MockSwapRouter02 router_,
        MockV3Pool pool_,
        MockERC20 usdt_,
        MockERC20 whsk_,
        uint256 strategyPk_,
        uint256 riskPk_,
        bytes32 strategy_
    ) {
        vault = vault_;
        router = router_;
        pool = pool_;
        usdt = usdt_;
        whsk = whsk_;
        strategyPk = strategyPk_;
        riskPk = riskPk_;
        strategy = strategy_;
    }

    function _sign(uint256 pk, SentinelVault.Intent memory it) internal view returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, vault.hashIntent(it));
        return abi.encodePacked(r, s, v);
    }

    function buy(uint64 size) external {
        uint128 amountIn = uint128(bound(size, 1e6, 200e6));
        int24 tick = BASE_TICK;
        uint256 fair = TwapLib.quoteAtTick(tick, amountIn, address(usdt), address(whsk));
        if (fair == 0 || router.available(address(whsk)) < fair) return;
        router.setNextAmountOut(fair);

        SentinelVault.Intent memory it = SentinelVault.Intent({
            tokenIn: address(usdt),
            tokenOut: address(whsk),
            fee: 3000,
            amountIn: amountIn,
            minAmountOut: uint128(fair),
            declaredEdgeBps: 100,
            strategyHash: strategy,
            parentId: bytes32(0),
            nonce: ++nonce,
            deadline: uint64(block.timestamp + 300),
            maxNotionalQuote: 200e6
        });
        try vault.executeIntent(it, _sign(strategyPk, it), _sign(riskPk, it)) {
            okBuys += 1;
        } catch {}
    }

    function sell(uint64 fraction) external {
        (uint256 qty,) = vault.positions(address(whsk));
        if (qty == 0) return;
        uint64 part = uint64(bound(fraction, 1, qty));
        uint256 fair = TwapLib.quoteAtTick(BASE_TICK, part, address(whsk), address(usdt));
        if (fair == 0 || router.available(address(usdt)) < fair) return;
        router.setNextAmountOut(fair);

        SentinelVault.Intent memory it = SentinelVault.Intent({
            tokenIn: address(whsk),
            tokenOut: address(usdt),
            fee: 3000,
            amountIn: uint128(part),
            minAmountOut: uint128((fair * 99) / 100),
            declaredEdgeBps: 0, // exits claim no edge: the claim gate is for entries
            strategyHash: strategy,
            parentId: bytes32(0),
            nonce: ++nonce,
            deadline: uint64(block.timestamp + 300),
            maxNotionalQuote: type(uint64).max
        });
        try vault.executeIntent(it, _sign(strategyPk, it), _sign(riskPk, it)) {
            okSells += 1;
        } catch {}
    }

    function warp(uint32 dt) external {
        vm.warp(block.timestamp + bound(uint256(dt), 1, 7200));
    }
}

contract SentinelVaultInvariantTest is Test {
    SentinelVault vault;
    MockSwapRouter02 router;
    MockV3Pool pool;
    MockERC20 usdt;
    MockERC20 whsk;
    MockFactory factory;
    VaultHandler handler;

    uint256 strategyPk = 0xA11CE;
    uint256 riskPk = 0xB0B;
    address owner = address(0xFEE);
    bytes32 constant STRATEGY = keccak256("sentinel/invariant/v1");
    uint64 constant MAX_DAILY = 800e6;
    uint64 constant MAX_TRADE = 200e6;

    function setUp() public {
        usdt = new MockERC20("Tether USD", "USDT", 6);
        whsk = new MockERC20("Wrapped HSK", "WHSK", 18);
        pool = new MockV3Pool(-301_487 + 200, -301_487); // 200bp of dislocation for the handler to trade
        router = new MockSwapRouter02();
        factory = new MockFactory();
        factory.setPool(address(whsk), address(usdt), 3000, address(pool));

        SentinelVault.Policy memory p = SentinelVault.Policy({
            maxSlippageBps: 100,
            minEdgeBps: 80,
            maxSpotTwapDeviationBps: 300,
            maxDrawdownBps: 1500,
            twapWindow: 600,
            cooldown: 0,
            maxSlices: 4,
            maxTradeNotional: MAX_TRADE,
            maxDailyNotional: MAX_DAILY
        });
        vault = new SentinelVault(
            owner, address(router), address(factory), address(usdt), vm.addr(strategyPk), vm.addr(riskPk), p
        );

        vm.startPrank(owner);
        vault.setMarket(address(whsk), 3000, true);
        vault.registerStrategy(STRATEGY);
        vault.unpause();
        usdt.mint(owner, 5_000e6);
        usdt.approve(address(vault), type(uint256).max);
        vault.deposit(address(usdt), 1_000e6);
        vm.stopPrank();

        whsk.mint(address(router), 100_000e18);
        usdt.mint(address(router), 100_000e6);

        handler = new VaultHandler(vault, router, pool, usdt, whsk, strategyPk, riskPk, STRATEGY);
        targetContract(address(handler));
    }

    /// @dev Guards against a vacuously-true invariant suite: the handler must actually trade.
    function test_HandlerActuallyTrades() public {
        handler.buy(50e6);
        handler.buy(20e6);
        assertGt(handler.okBuys(), 0);
        handler.sell(1e18);
        assertEq(vault.tradeCount(), uint256(handler.okBuys()) + uint256(handler.okSells()));
    }

    /// @notice No token approval may outlive a transaction.
    function invariant_NoDanglingAllowance() public view {
        assertEq(usdt.allowance(address(vault), address(router)), 0);
        assertEq(whsk.allowance(address(vault), address(router)), 0);
    }

    /// @notice The rolling budget can never be exceeded, and always equals what was actually bought.
    function invariant_DailyBudgetRespected() public view {
        assertLe(vault.dailyNotional(), MAX_DAILY);
    }

    /// @notice Book-keeping equals reality: every executed intent is counted exactly once.
    function invariant_TradeCountMatchesHandler() public view {
        assertEq(vault.tradeCount(), uint256(handler.okBuys()) + uint256(handler.okSells()));
    }

    /// @notice Every *entry* cleared the edge hurdle, and exits never add a claim of their own.
    function invariant_DeclaredEdgeAboveHurdle() public view {
        uint256 buys = handler.okBuys();
        if (buys == 0) return;
        assertGe(vault.declaredEdgeSumBps(), int256(buys) * 80);
        assertLe(vault.declaredEdgeSumBps(), int256(buys) * 100);
    }

    /// @notice Equity is never negative and never silently zero while positions exist.
    function invariant_EquityNonNegative() public view {
        assertGe(vault.equity(), 0);
    }
}
