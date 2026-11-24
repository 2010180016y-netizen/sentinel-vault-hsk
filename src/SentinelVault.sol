// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {MessageHashUtils} from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";

import {IV3Factory, IV3SwapRouter02} from "./interfaces/IHskSwap.sol";
import {TwapLib} from "./libraries/TwapLib.sol";

/// @title SentinelVault — a policy kernel that an autonomous agent cannot talk its way out of
/// @author Built for HSK Chain Horizon Hackathon · Seoul (Track 1: AI Agent Trading)
///
/// @notice What makes this different from a "guarded vault" (docs/00_FACTS.md §8, docs/20_EXPERT_PANEL.md §1):
///
///   1. STRATEGY PRE-REGISTRATION. The agent may only trade a `strategyHash` that a human registered
///      beforehand. A strategy cannot be silently swapped after the fact to make a losing run look good.
///   2. DECLARED EDGE, RECONCILED. Every trade carries the edge (in bps) the agent claims it has, and the
///      vault refuses any trade claiming less than `minEdgeBps` (the round-trip cost hurdle). The *realised*
///      edge is computed on-chain against the pool's own TWAP and accumulated, so `edgeCredibility()`
///      answers one question anyone can check: did the agent's claims survive contact with the market?
///   3. NO-TRADE IS A DECISION. `recordNoTrade` writes the refusals to the same immutable log, so
///      "did nothing because the hurdle was not met" is visible instead of invisible.
///   4. IMPACT-BUDGETED SLICES. Large orders are parent/child: a parent declares a budget, children spend
///      against it with a hard slice count. A thin DEX never receives one oversized swap.
///   5. SELF-DERIVED FAIR VALUE. QuoterV2 cannot be called on-chain (it reverts by design), so the vault
///      does not trust the agent's quote at all: it reads the pool TWAP, computes `fairOut` itself, and
///      requires `minAmountOut` to sit inside [fairOut*(1-maxSlippage), fairOut].
///
/// @dev Immutable by design: no proxy, no upgrade path, no owner override of a signed intent. The owner
///      (the human) can always deposit/withdraw and rotate keys; the agent (strategySigner) can only
///      propose. The riskSigner can co-sign trades and stop the vault but can never move funds.
contract SentinelVault is ReentrancyGuard {
    using SafeERC20 for IERC20;

    // ---------------------------------------------------------------------------------------------
    // Types
    // ---------------------------------------------------------------------------------------------

    struct Policy {
        uint16 maxSlippageBps; // vs TWAP fair value, e.g. 100 = 1%
        uint16 minEdgeBps; // declared-edge hurdle, must exceed round-trip cost
        uint16 maxSpotTwapDeviationBps; // 1 tick ≈ 1 bp; also the manipulation circuit breaker
        uint16 maxDrawdownBps; // measured on TWAP equity vs high-water mark
        uint32 twapWindow; // seconds, >= 300 recommended
        uint32 cooldown; // seconds between *new* parent orders
        uint8 maxSlices; // children per parent
        uint64 maxTradeNotional; // quote-token raw units per intent
        uint64 maxDailyNotional; // rolling 24h quote-token raw units
    }

    struct Market {
        address pool;
        uint24 fee;
        uint8 decimals;
        bool enabled;
    }

    struct Position {
        uint256 qty; // asset raw units
        uint256 costQuote; // quote raw units spent to acquire the current qty
    }

    struct Parent {
        uint64 budgetQuote;
        uint64 spentQuote;
        uint8 slices;
        bool open;
    }

    /// @notice The agent's proposal. Signed off-chain by BOTH strategySigner and riskSigner (EIP-712).
    struct Intent {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        uint128 amountIn;
        uint128 minAmountOut;
        uint16 declaredEdgeBps;
        bytes32 strategyHash;
        bytes32 parentId;
        uint64 nonce;
        uint64 deadline;
        uint64 maxNotionalQuote;
    }

    struct Stats {
        uint256 tradeCount;
        uint256 winCount;
        int256 realizedPnlQuote;
        uint256 feesPaidQuote;
        int256 declaredEdgeSumBps;
        int256 realizedEdgeSumBps;
        uint256 equity;
        uint256 highWater;
        uint256 dailyNotional;
    }

    // ---------------------------------------------------------------------------------------------
    // Constants & immutables
    // ---------------------------------------------------------------------------------------------

    uint16 internal constant BPS = 10_000;

    bytes32 public constant DOMAIN_TYPEHASH =
        keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");
    bytes32 public constant INTENT_TYPEHASH = keccak256(
        "Intent(address tokenIn,address tokenOut,uint24 fee,uint128 amountIn,uint128 minAmountOut,uint16 declaredEdgeBps,bytes32 strategyHash,bytes32 parentId,uint64 nonce,uint64 deadline,uint64 maxNotionalQuote)"
    );

    address public immutable owner;
    address public immutable router;
    address public immutable factory;
    address public immutable quoteToken;
    bytes32 public immutable domainSeparator;

    // ---------------------------------------------------------------------------------------------
    // Mutable state
    // ---------------------------------------------------------------------------------------------

    address public strategySigner;
    address public riskSigner;
    bool public paused;

    Policy public policy;

    mapping(address asset => Market) public markets;
    address[] public assets;

    mapping(bytes32 strategyHash => uint64 registeredAt) public strategyRegisteredAt;

    mapping(uint64 nonce => bool) public nonceUsed;
    mapping(bytes32 parentId => Parent) public parents;

    mapping(address asset => Position) public positions;

    uint256 public dailyNotional;
    uint256 public windowStart;
    uint256 public lastTradeAt;
    uint256 public highWater;
    int256 public realizedPnlQuote;
    uint256 public feesPaidQuote;
    uint256 public tradeCount;
    uint256 public winCount;
    int256 public declaredEdgeSumBps;
    int256 public realizedEdgeSumBps;

    // ---------------------------------------------------------------------------------------------
    // Events & errors
    // ---------------------------------------------------------------------------------------------

    event IntentExecuted(
        bytes32 indexed intentHash,
        bytes32 indexed strategyHash,
        bytes32 indexed parentId,
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint256 amountOut,
        uint256 notionalQuote,
        uint256 fairOut,
        uint16 declaredEdgeBps,
        int256 realizedEdgeBps,
        uint64 nonce
    );
    event NoTradeDecision(bytes32 indexed strategyHash, uint16 edgeBps, bytes32 reasonHash, uint64 nonce);
    event StrategyRegistered(bytes32 indexed strategyHash, uint64 registeredAt);
    event PolicyUpdated(Policy policy);
    event SignersUpdated(address indexed strategySigner, address indexed riskSigner);
    event MarketSet(address indexed asset, address pool, uint24 fee, bool enabled);
    event ParentOpened(bytes32 indexed parentId, uint64 budgetQuote);
    event ParentClosed(bytes32 indexed parentId, uint64 spentQuote, uint8 slices);
    event Paused(address indexed by);
    event Unpaused(address indexed by);
    event Deposited(address indexed token, address indexed from, uint256 amount);
    event Withdrawn(address indexed token, address indexed to, uint256 amount);

    error NotOwner();
    error NotStrategySigner();
    error NotRiskOrOwner();
    error VaultPaused();
    error Expired();
    error ZeroAmount();
    error StrategyNotRegistered();
    error NonceUsed();
    error BadStrategySignature();
    error BadRiskSignature();
    error EdgeBelowHurdle();
    error QuoteLegRequired();
    error MarketDisabled();
    error PoolMismatch();
    error InsufficientBalance();
    error PriceDeviated();
    error FairValueZero();
    error MinOutAboveFair();
    error SlippageTooWide();
    error TradeTooLarge();
    error ExceedsIntentCap();
    error DailyLimitExceeded();
    error DrawdownBreach();
    error CooldownActive();
    error UnknownParent();
    error ParentBudgetExceeded();
    error ParentSlicesExceeded();
    error InsufficientOutput();
    error OutputMismatch();
    error NothingToSell();
    error InvalidConfig();

    // ---------------------------------------------------------------------------------------------
    // Construction
    // ---------------------------------------------------------------------------------------------

    constructor(
        address owner_,
        address router_,
        address factory_,
        address quoteToken_,
        address strategySigner_,
        address riskSigner_,
        Policy memory policy_
    ) {
        if (
            owner_ == address(0) || router_ == address(0) || factory_ == address(0) || quoteToken_ == address(0)
                || strategySigner_ == address(0) || riskSigner_ == address(0)
        ) revert InvalidConfig();
        if (router_.code.length == 0 || factory_.code.length == 0 || quoteToken_.code.length == 0) {
            revert InvalidConfig();
        }
        if (strategySigner_ == riskSigner_) revert InvalidConfig(); // dual control must be two keys
        if (
            policy_.maxSlippageBps == 0 || policy_.maxSlippageBps > 2_000 || policy_.maxDrawdownBps == 0
                || policy_.maxDrawdownBps > 9_000 || policy_.twapWindow < 60 || policy_.maxSlices == 0
                || policy_.maxTradeNotional == 0 || policy_.maxDailyNotional < policy_.maxTradeNotional
                || policy_.maxSpotTwapDeviationBps == 0
        ) revert InvalidConfig();

        owner = owner_;
        router = router_;
        factory = factory_;
        quoteToken = quoteToken_;
        strategySigner = strategySigner_;
        riskSigner = riskSigner_;
        policy = policy_;
        paused = true; // fail closed until the human arms it

        domainSeparator = keccak256(
            abi.encode(
                DOMAIN_TYPEHASH, keccak256("SentinelVault"), keccak256("1"), block.chainid, address(this)
            )
        );
    }

    // ---------------------------------------------------------------------------------------------
    // Human controls
    // ---------------------------------------------------------------------------------------------

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    /// @notice Replace the whole risk policy. Emits the full struct so every change is auditable.
    function setPolicy(Policy calldata p) external onlyOwner {
        if (
            p.maxSlippageBps == 0 || p.maxSlippageBps > 2_000 || p.maxDrawdownBps == 0 || p.maxDrawdownBps > 9_000
                || p.twapWindow < 60 || p.maxSlices == 0 || p.maxTradeNotional == 0
                || p.maxDailyNotional < p.maxTradeNotional || p.maxSpotTwapDeviationBps == 0
        ) revert InvalidConfig();
        policy = p;
        emit PolicyUpdated(p);
    }

    function setSigners(address newStrategy, address newRisk) external onlyOwner {
        if (newStrategy == address(0) || newRisk == address(0) || newStrategy == newRisk) revert InvalidConfig();
        strategySigner = newStrategy;
        riskSigner = newRisk;
        emit SignersUpdated(newStrategy, newRisk);
    }

    /// @notice Pre-commit a strategy. Only the human can do this: the agent cannot certify itself.
    function registerStrategy(bytes32 strategyHash) external onlyOwner {
        if (strategyHash == bytes32(0)) revert InvalidConfig();
        if (strategyRegisteredAt[strategyHash] == 0) {
            strategyRegisteredAt[strategyHash] = uint64(block.timestamp);
        }
        emit StrategyRegistered(strategyHash, strategyRegisteredAt[strategyHash]);
    }

    /// @notice Register the only pool the vault will ever trade a given asset through.
    function setMarket(address asset, uint24 fee, bool enabled) external onlyOwner {
        if (asset == address(0) || asset == quoteToken) revert InvalidConfig();
        address pool = IV3Factory(factory).getPool(asset, quoteToken, fee);
        if (pool == address(0)) revert PoolMismatch(); // the factory itself must know this pool

        if (markets[asset].pool == address(0)) {
            assets.push(asset);
            markets[asset] = Market({
                pool: pool,
                fee: fee,
                decimals: IERC20Metadata(asset).decimals(),
                enabled: enabled
            });
        } else {
            markets[asset].pool = pool;
            markets[asset].fee = fee;
            markets[asset].enabled = enabled;
        }
        emit MarketSet(asset, pool, fee, enabled);
    }

    function arm() external onlyOwner {
        paused = !paused;
        if (paused) emit Paused(msg.sender);
        else emit Unpaused(msg.sender);
    }

    /// @notice Either the human or the risk key can stop the vault; only the human can restart it.
    function pause() external {
        if (msg.sender != owner && msg.sender != riskSigner) revert NotRiskOrOwner();
        paused = true;
        emit Paused(msg.sender);
    }

    function unpause() external onlyOwner {
        paused = false;
        emit Unpaused(msg.sender);
    }

    function deposit(address token, uint256 amount) external onlyOwner nonReentrant {
        if (amount == 0) revert ZeroAmount();
        IERC20(token).safeTransferFrom(msg.sender, address(this), amount);
        emit Deposited(token, msg.sender, amount);
    }

    function withdraw(address token, address to, uint256 amount) external onlyOwner nonReentrant {
        if (to == address(0)) revert InvalidConfig();
        IERC20(token).safeTransfer(to, amount);
        emit Withdrawn(token, to, amount);
    }

    // ---------------------------------------------------------------------------------------------
    // Execution schedule (agent-side, no funds authority)
    // ---------------------------------------------------------------------------------------------

    /// @notice Declare a budget for a sliced order. The agent cannot exceed it nor the slice count.
    function openParent(bytes32 parentId, uint64 budgetQuote) external {
        if (msg.sender != strategySigner) revert NotStrategySigner();
        if (parentId == bytes32(0) || budgetQuote == 0) revert InvalidConfig();
        if (parents[parentId].open) revert InvalidConfig();
        if (uint256(budgetQuote) > uint256(policy.maxTradeNotional) * policy.maxSlices) revert TradeTooLarge();
        parents[parentId] = Parent({budgetQuote: budgetQuote, spentQuote: 0, slices: 0, open: true});
        emit ParentOpened(parentId, budgetQuote);
    }

    function closeParent(bytes32 parentId) external {
        if (msg.sender != strategySigner && msg.sender != owner) revert NotStrategySigner();
        Parent storage p = parents[parentId];
        if (!p.open) revert UnknownParent();
        p.open = false;
        emit ParentClosed(parentId, p.spentQuote, p.slices);
    }

    /// @notice Log a deliberate refusal. This is how "the hurdle was not met" becomes evidence.
    function recordNoTrade(bytes32 strategyHash, uint16 edgeBps, bytes32 reasonHash, uint64 nonce) external {
        if (msg.sender != strategySigner) revert NotStrategySigner();
        if (strategyRegisteredAt[strategyHash] == 0) revert StrategyNotRegistered();
        emit NoTradeDecision(strategyHash, edgeBps, reasonHash, nonce);
    }

    // ---------------------------------------------------------------------------------------------
    // Core: validate -> execute -> re-validate -> account
    // ---------------------------------------------------------------------------------------------

    function executeIntent(Intent calldata it, bytes calldata strategySignature, bytes calldata riskSignature)
        external
        nonReentrant
        returns (uint256 amountOut)
    {
        if (paused) revert VaultPaused();
        if (block.timestamp > it.deadline) revert Expired();
        if (it.amountIn == 0) revert ZeroAmount();
        if (strategyRegisteredAt[it.strategyHash] == 0) revert StrategyNotRegistered();
        if (nonceUsed[it.nonce]) revert NonceUsed();
        if (it.declaredEdgeBps < policy.minEdgeBps) revert EdgeBelowHurdle();

        bytes32 intentHash = hashIntent(it);
        if (_recover(intentHash, strategySignature) != strategySigner) revert BadStrategySignature();
        if (_recover(intentHash, riskSignature) != riskSigner) revert BadRiskSignature();
        nonceUsed[it.nonce] = true;

        bool isBuy = it.tokenIn == quoteToken;
        if (!isBuy && it.tokenOut != quoteToken) revert QuoteLegRequired();
        address asset = isBuy ? it.tokenOut : it.tokenIn;

        Market memory m = markets[asset];
        if (!m.enabled || m.pool == address(0)) revert MarketDisabled();
        if (it.fee != m.fee) revert PoolMismatch();
        if (IV3Factory(factory).getPool(it.tokenIn, it.tokenOut, it.fee) != m.pool) revert PoolMismatch();
        if (IERC20(it.tokenIn).balanceOf(address(this)) < it.amountIn) revert InsufficientBalance();

        // ---- fair value from the pool's own oracle (never from the agent's quote) ----
        int24 twapTick = TwapLib.consult(m.pool, policy.twapWindow);
        if (TwapLib.absDiff(TwapLib.spotTick(m.pool), twapTick) > policy.maxSpotTwapDeviationBps) {
            revert PriceDeviated();
        }

        uint256 fairOut = TwapLib.quoteAtTick(twapTick, it.amountIn, it.tokenIn, it.tokenOut);
        if (fairOut == 0) revert FairValueZero();
        if (it.minAmountOut > fairOut) revert MinOutAboveFair();
        uint256 floorOut = (fairOut * (BPS - policy.maxSlippageBps)) / BPS;
        if (it.minAmountOut < floorOut) revert SlippageTooWide();

        // ---- notional & budget ----
        uint256 notionalQuote = isBuy ? uint256(it.amountIn) : fairOut; // always quote-token units
        // Sizing cap. Exits get a wider ceiling on purpose: found while testing, a per-trade cap applied
        // to sells could trap a position that had grown past the cap, turning a risk limit into a risk.
        // Risk-reducing trades still respect the TWAP slippage band and the rolling daily cap.
        uint256 tradeCap = isBuy ? policy.maxTradeNotional : uint256(policy.maxTradeNotional) * policy.maxSlices;
        if (notionalQuote > tradeCap) revert TradeTooLarge();
        if (notionalQuote > it.maxNotionalQuote) revert ExceedsIntentCap();

        // The rolling daily budget meters *capital deployed*, i.e. buys. Metering exits as well would let a
        // risk limit block the very trade that removes risk (found by test, kept as an explicit decision).
        _rollWindow();
        if (isBuy && dailyNotional + notionalQuote > policy.maxDailyNotional) revert DailyLimitExceeded();

        // ---- drawdown: under stress only risk-reducing trades may pass ----
        uint256 eq = equity();
        if (highWater != 0 && eq < (highWater * (BPS - policy.maxDrawdownBps)) / BPS && isBuy) {
            revert DrawdownBreach();
        }

        // ---- schedule discipline ----
        if (it.parentId == bytes32(0)) {
            if (lastTradeAt != 0 && block.timestamp < lastTradeAt + policy.cooldown) revert CooldownActive();
        } else {
            Parent storage p = parents[it.parentId];
            if (!p.open) revert UnknownParent();
            if (uint256(p.spentQuote) + notionalQuote > p.budgetQuote) revert ParentBudgetExceeded();
            if (p.slices >= policy.maxSlices) revert ParentSlicesExceeded();
            p.spentQuote = uint64(uint256(p.spentQuote) + notionalQuote);
            p.slices += 1;
        }
        lastTradeAt = block.timestamp;
        if (isBuy) dailyNotional += notionalQuote;

        // ---- execute through HSKSwap SwapRouter02 (deadline enforced by multicall) ----
        IERC20 tokenIn = IERC20(it.tokenIn);
        tokenIn.forceApprove(router, it.amountIn);

        uint256 balanceBefore = IERC20(it.tokenOut).balanceOf(address(this));
        bytes[] memory calls = new bytes[](1);
        calls[0] = abi.encodeCall(
            IV3SwapRouter02.exactInputSingle,
            IV3SwapRouter02.ExactInputSingleParams({
                tokenIn: it.tokenIn,
                tokenOut: it.tokenOut,
                fee: it.fee,
                recipient: address(this),
                amountIn: it.amountIn,
                amountOutMinimum: it.minAmountOut,
                sqrtPriceLimitX96: 0
            })
        );
        bytes[] memory results = IV3SwapRouter02(router).multicall(it.deadline, calls);
        uint256 reported = abi.decode(results[0], (uint256));
        uint256 received = IERC20(it.tokenOut).balanceOf(address(this)) - balanceBefore;

        tokenIn.forceApprove(router, 0); // allowance hygiene (USDT-style tokens require the reset anyway)

        // ---- post-condition (Safe Guard's checkAfterExecution idea, applied to value) ----
        // Two independent observations must agree: what the router says it paid, and what actually
        // arrived. A router that reports a good fill but delivers less is caught here, not trusted.
        if (reported != received) revert OutputMismatch();
        if (received < it.minAmountOut) revert InsufficientOutput();
        amountOut = received;

        // ---- accounting ----
        int256 realizedEdgeBps = ((int256(amountOut) - int256(fairOut)) * int256(uint256(BPS))) / int256(fairOut);
        declaredEdgeSumBps += int256(uint256(it.declaredEdgeBps));
        realizedEdgeSumBps += realizedEdgeBps;

        if (isBuy) {
            Position storage pos = positions[asset];
            pos.qty += amountOut;
            pos.costQuote += notionalQuote;
            feesPaidQuote += (uint256(it.amountIn) * it.fee) / 1_000_000;
        } else {
            Position storage pos = positions[asset];
            if (pos.qty == 0) revert NothingToSell();
            uint256 costPortion = (pos.costQuote * it.amountIn) / pos.qty;
            int256 pnl = int256(amountOut) - int256(costPortion);
            realizedPnlQuote += pnl;
            if (pnl > 0) winCount += 1;
            pos.qty -= it.amountIn;
            pos.costQuote -= costPortion;
            feesPaidQuote += (fairOut * it.fee) / 1_000_000;
        }
        tradeCount += 1;

        uint256 eqAfter = equity();
        if (eqAfter > highWater) highWater = eqAfter;

        emit IntentExecuted(
            intentHash,
            it.strategyHash,
            it.parentId,
            it.tokenIn,
            it.tokenOut,
            it.amountIn,
            amountOut,
            notionalQuote,
            fairOut,
            it.declaredEdgeBps,
            realizedEdgeBps,
            it.nonce
        );
    }

    // ---------------------------------------------------------------------------------------------
    // Views
    // ---------------------------------------------------------------------------------------------

    function hashIntent(Intent calldata it) public view returns (bytes32) {
        bytes32 structHash = keccak256(
            abi.encode(
                INTENT_TYPEHASH,
                it.tokenIn,
                it.tokenOut,
                it.fee,
                it.amountIn,
                it.minAmountOut,
                it.declaredEdgeBps,
                it.strategyHash,
                it.parentId,
                it.nonce,
                it.deadline,
                it.maxNotionalQuote
            )
        );
        return MessageHashUtils.toTypedDataHash(domainSeparator, structHash);
    }

    /// @notice TWAP-marked equity in quote-token raw units.
    function equity() public view returns (uint256 total) {
        total = IERC20(quoteToken).balanceOf(address(this));
        uint32 window = policy.twapWindow;
        uint256 n = assets.length;
        for (uint256 i; i < n; ++i) {
            address asset = assets[i];
            Market memory m = markets[asset];
            uint256 bal = IERC20(asset).balanceOf(address(this));
            if (bal == 0) continue;
            if (!m.enabled || m.pool == address(0)) {
                total += positions[asset].costQuote; // no price available: fall back to cost basis
                continue;
            }
            int24 tick = TwapLib.consult(m.pool, window);
            total += TwapLib.quoteAtTick(tick, bal, asset, quoteToken);
        }
    }

    /// @notice The one view a judge needs: performance, cost and whether the declared edge was real.
    function stats() external view returns (Stats memory s) {
        s = Stats({
            tradeCount: tradeCount,
            winCount: winCount,
            realizedPnlQuote: realizedPnlQuote,
            feesPaidQuote: feesPaidQuote,
            declaredEdgeSumBps: declaredEdgeSumBps,
            realizedEdgeSumBps: realizedEdgeSumBps,
            equity: equity(),
            highWater: highWater,
            dailyNotional: dailyNotional
        });
    }

    /// @notice Realised edge as a share of declared edge, in bps of the claim (10_000 = claims fully held).
    /// @dev Returns 0 when nothing has been declared: absence of a claim is not credibility.
    function edgeCredibilityBps() external view returns (int256) {
        if (declaredEdgeSumBps == 0) return 0;
        return (realizedEdgeSumBps * int256(uint256(BPS))) / declaredEdgeSumBps;
    }

    function assetCount() external view returns (uint256) {
        return assets.length;
    }

    /// @dev Invalid or malleable (high-s) signatures return address(0) instead of bubbling a generic
    ///      revert, so callers get a precise reason and no signature is ever "half accepted".
    function _recover(bytes32 digest, bytes calldata signature) internal pure returns (address signer) {
        (address recovered,,) = ECDSA.tryRecover(digest, signature);
        signer = recovered;
    }

    function _rollWindow() internal {
        if (block.timestamp >= windowStart + 1 days) {
            windowStart = block.timestamp;
            dailyNotional = 0;
        }
    }
}
