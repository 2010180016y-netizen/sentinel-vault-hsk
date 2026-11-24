// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test, console} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {SentinelVault} from "../src/SentinelVault.sol";
import {TwapLib} from "../src/libraries/TwapLib.sol";

/// @notice Integration tests against the **real** HSK Chain mainnet (chainId 177): real HSKSwap
///         router, real WHSK/USDT pool, real TWAP oracle. Run with:
///           forge test --match-contract SentinelVaultForkTest --fork-url https://mainnet.hsk.xyz
/// @dev No state is written to mainnet: forge forges a fork in memory.
contract SentinelVaultForkTest is Test {
    address constant USDT = 0xF1B50eD67A9e2CC94Ad3c477779E2d4cBfFf9029;
    address constant WHSK = 0xB210D2120d57b758EE163cFfb43e73728c471Cf1;
    address constant ROUTER = 0x2c16f75b95Cf1390c328aB70e2CEE7f4b80bD8F3;
    address constant FACTORY = 0x972cA9D1662F5e029cD18327D29026532E84c742;
    address constant POOL = 0x4ff515761864e7838B0AdB8b5b2B8B7A068905dC; // WHSK/USDT 0.3%
    uint24 constant FEE = 3000;

    SentinelVault vault;
    uint256 strategyPk = 0xA11CE;
    uint256 riskPk = 0xB0B;
    address owner = address(0xFEE);

    uint32 twapWindow;

    function setUp() public {
        vm.createSelectFork(vm.envOr("HSK_RPC", string("https://mainnet.hsk.xyz")));
        assertEq(block.chainid, 177, "must run against HSK Chain mainnet");

        // The oracle's usable window is bounded by observationCardinality * block time. Probe it
        // instead of assuming: a window the pool cannot serve must fail closed, not silently widen.
        twapWindow = _probeWindow();

        SentinelVault.Policy memory p = SentinelVault.Policy({
            maxSlippageBps: 150, // 1.5% vs TWAP, includes the 0.3% pool fee
            minEdgeBps: 80,
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
        vault.registerStrategy(keccak256("sentinel/momentum-twap-reversion/v1"));
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
        (bool ok, bytes memory data) =
            POOL.staticcall(abi.encodeWithSignature("observe(uint32[])", sa));
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
            strategyHash: keccak256("sentinel/momentum-twap-reversion/v1"),
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

    /// @notice The whole point, proved on the live chain: price comes from the pool's TWAP, the
    ///         swap goes through the real HSKSwap router, and the vault's own accounting matches.
    function test_Fork_LiveTwapAndRealSwap() public {
        (int24 twapTick, int24 spotTick) = _twapAndSpot();
        console.log("twapWindow(s)", twapWindow);
        console.log("spotTick", int256(spotTick));
        console.log("twapTick", int256(twapTick));

        uint256 fair = TwapLib.quoteAtTick(twapTick, 100e6, USDT, WHSK); // 100 USDT -> WHSK
        console.log("fairOut (WHSK raw) for 100 USDT", fair);
        assertGt(fair, 0, "oracle must produce a fair value");

        uint256 minOut = (fair * 9_850) / 10_000; // 1.5% below TWAP fair value
        SentinelVault.Intent memory it = _intent(100e6, minOut, 120, 1);

        uint256 out = vault.executeIntent(it, _sig(strategyPk, it), _sig(riskPk, it));
        console.log("amountOut (WHSK raw)", out);

        assertGe(out, minOut, "router must respect our floor");
        assertLe(out, fair + (fair / 100), "a swap cannot beat TWAP by more than 1%");

        (uint256 qty, uint256 cost) = vault.positions(WHSK);
        assertEq(qty, out);
        assertEq(cost, 100e6);
        assertEq(IERC20(USDT).allowance(address(vault), ROUTER), 0, "allowance reset after trade");

        SentinelVault.Stats memory s = vault.stats();
        console.log("equity (USDT raw)", s.equity);
        console.log("feesPaid (USDT raw)", s.feesPaidQuote);
        console.log("realizedEdgeSumBps", s.realizedEdgeSumBps);
        assertApproxEqAbs(s.equity, 1_000e6, 2e6, "mark-to-market of the vault");
    }

    /// @notice A floor further than `maxSlippageBps` below TWAP must be refused on the live pool too.
    function test_Fork_RevertsWhenFloorTooWide() public {
        (int24 twapTick,) = _twapAndSpot();
        uint256 fair = TwapLib.quoteAtTick(twapTick, 100e6, USDT, WHSK);
        SentinelVault.Intent memory it = _intent(100e6, (fair * 90) / 100, 120, 2);
        bytes memory s1 = _sig(strategyPk, it);
        bytes memory s2 = _sig(riskPk, it);
        vm.expectRevert(SentinelVault.SlippageTooWide.selector);
        vault.executeIntent(it, s1, s2);
    }

    /// @notice The unregistered-strategy rule also holds against the real contracts.
    function test_Fork_RevertsUnregisteredStrategy() public {
        SentinelVault.Intent memory it = _intent(10e6, 1, 120, 3);
        it.strategyHash = keccak256("shadow-strategy");
        bytes memory s1 = _sig(strategyPk, it);
        bytes memory s2 = _sig(riskPk, it);
        vm.expectRevert(SentinelVault.StrategyNotRegistered.selector);
        vault.executeIntent(it, s1, s2);
    }

    function _twapAndSpot() internal view returns (int24 twapTick, int24 spotTick) {
        int56[] memory tc;
        (bool ok, bytes memory data) = POOL.staticcall(
            abi.encodeWithSignature("observe(uint32[])", _windowArray())
        );
        require(ok, "observe failed");
        (tc,) = abi.decode(data, (int56[], uint160[]));
        twapTick = int24((tc[1] - tc[0]) / int56(uint56(twapWindow)));

        (bool ok2, bytes memory d2) =
            POOL.staticcall(abi.encodeWithSignature("slot0()"));
        require(ok2, "slot0 failed");
        (, spotTick,,,,,) = abi.decode(d2, (uint160, int24, uint16, uint16, uint16, uint8, bool));
    }

    function _windowArray() internal view returns (uint32[] memory sa) {
        sa = new uint32[](2);
        sa[0] = twapWindow;
        sa[1] = 0;
    }
}
