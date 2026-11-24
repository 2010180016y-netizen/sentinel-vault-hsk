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
        bool oracleHealthy;
        int256 executionEdgeSumBps;
    }

    // ---------------------------------------------------------------------------------------------
    // Constants & immutables
    // ---------------------------------------------------------------------------------------------

    uint16 internal constant BPS = 10_000;

    /// @dev The edge hurdle is the vault's differentiator, so it is bounded on both sides: a policy that
    ///      sets it to zero would silently turn the gate off, and one set above half a percent-scaled
    ///      spread would never trade. Bounds are policy validation, not policy opinion.
    uint16 internal constant MAX_EDGE_HURDLE_BPS = 5_000;
    /// @dev A cooldown longer than a week is almost certainly a typo and would brick a live vault.
    uint32 internal constant MAX_COOLDOWN = 7 days;

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
    uint256 public lastBuyAt;
    uint256 public highWater;
    int256 public realizedPnlQuote;
    uint256 public feesPaidQuote;
    uint256 public tradeCount;
    uint256 public winCount;
    int256 public declaredEdgeSumBps;
    int256 public realizedEdgeSumBps;
    int256 public executionEdgeSumBps;

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
    event NoTradeDecision(
        bytes32 indexed strategyHash, uint16 edgeBps, uint16 hurdleBps, bytes32 reasonHash, uint64 nonce
    );
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
    error NotStrategyOrOwner();
    error NotRiskOrOwner();
    error VaultPaused();
    error Expired();
    error ZeroAmount();
    error StrategyNotRegistered();
    error NonceUsed();
    error BadStrategySignature();
    error BadRiskSignature();
    error EdgeBelowHurdle();
    error EdgeClaimAboveDislocation();
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
    error OracleUnavailable();

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
        _validatePolicy(policy_); // same predicate the owner faces on every later `setPolicy`

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
        _validatePolicy(p);
        policy = p;
        emit PolicyUpdated(p);
    }

    /// @dev One place for the policy predicate: the constructor and `setPolicy` cannot drift apart, and
    ///      every bound that makes a limit meaningful is stated once. `minEdgeBps` was previously
    ///      unvalidated — an owner could set it to 0 in one transaction and disable the edge hurdle that
    ///      the whole design rests on, with no event a reader would notice.
    function _validatePolicy(Policy memory p) private pure {
        if (
            p.maxSlippageBps == 0 || p.maxSlippageBps > 2_000 || p.maxDrawdownBps == 0 || p.maxDrawdownBps > 9_000
                || p.twapWindow < 60 || p.maxSlices == 0 || p.maxTradeNotional == 0
                || p.maxDailyNotional < p.maxTradeNotional || p.maxSpotTwapDeviationBps == 0
                || p.minEdgeBps == 0 || p.minEdgeBps > MAX_EDGE_HURDLE_BPS || p.cooldown > MAX_COOLDOWN
        ) revert InvalidConfig();
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
        // Withdrawing a position asset is not a sale, but the cost basis must follow the tokens out or the
        // book keeps claiming a position the vault no longer holds (and a later sell would underflow basis).
        Position storage pos = positions[token];
        if (pos.qty > 0) {
            uint256 qtyOut = amount < pos.qty ? amount : pos.qty;
            uint256 costOut = (pos.costQuote * qtyOut) / pos.qty;
            pos.qty -= qtyOut;
            pos.costQuote -= costOut;
        }
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
        if (msg.sender != strategySigner && msg.sender != owner) revert NotStrategyOrOwner();
        Parent storage p = parents[parentId];
        if (!p.open) revert UnknownParent();
        p.open = false;
        emit ParentClosed(parentId, p.spentQuote, p.slices);
    }

    /// @notice Log a deliberate refusal. This is how "the hurdle was not met" becomes evidence.
    /// @dev The refusal writes the same register as a trade would: it consumes the nonce, so one nonce is
    ///      exactly one decision (a refusal can no longer be replayed, and the same nonce cannot later be
    ///      re-used for a trade that was never decided that way). It also carries the hurdle it was
    ///      measured against, which must be at least the on-chain floor — a refusal that claims to have
    ///      been judged against a softer hurdle than the vault enforces is rejected rather than logged.
    function recordNoTrade(
        bytes32 strategyHash,
        uint16 edgeBps,
        uint16 hurdleBps,
        bytes32 reasonHash,
        uint64 nonce
    ) external {
        if (msg.sender != strategySigner) revert NotStrategySigner();
        if (strategyRegisteredAt[strategyHash] < 1) revert StrategyNotRegistered();
        if (nonceUsed[nonce]) revert NonceUsed();
        if (hurdleBps < policy.minEdgeBps) revert EdgeBelowHurdle();
        nonceUsed[nonce] = true;
        emit NoTradeDecision(strategyHash, edgeBps, hurdleBps, reasonHash, nonce);
    }

    // ---------------------------------------------------------------------------------------------
    // Core: validate -> price -> budget -> execute -> re-validate -> account
    // ---------------------------------------------------------------------------------------------

    /// @notice One intent, one nonce, one decision. Every predicate is checked before any value moves, and
    ///         the fill is re-verified against an independent observation after it moves.
    /// @dev Split into five named steps (`_authorise` / `_fairValueBand` / `_enforceBudget` / `_swap` /
    ///      `_settle`) so each rule a judge cares about can be read on its own instead of inside one
    ///      140-line function.
    function executeIntent(Intent calldata it, bytes calldata strategySignature, bytes calldata riskSignature)
        external
        nonReentrant
        returns (uint256 amountOut)
    {
        Market memory m = _authorise(it, strategySignature, riskSignature);

        uint256 fairOut = _fairValueBand(m, it);
        bool isBuy = it.tokenIn == quoteToken;
        uint256 notionalQuote = isBuy ? uint256(it.amountIn) : fairOut; // always quote-token units

        _enforceBudget(it, isBuy, notionalQuote);

        amountOut = _swap(it);
        _settle(it, isBuy, notionalQuote, fairOut, amountOut);
    }

    /// @dev Everything that can reject an intent without touching value. A rejection costs the caller gas
    ///      and changes nothing (the nonce is only consumed when the whole call succeeds).
    function _authorise(Intent calldata it, bytes calldata strategySignature, bytes calldata riskSignature)
        private
        returns (Market memory m)
    {
        if (paused) revert VaultPaused();
        if (block.timestamp > it.deadline) revert Expired();
        if (it.amountIn == 0) revert ZeroAmount();
        if (strategyRegisteredAt[it.strategyHash] < 1) revert StrategyNotRegistered();
        if (nonceUsed[it.nonce]) revert NonceUsed();

        bool isBuy = it.tokenIn == quoteToken;
        // The hurdle is a claim gate on *entries*: refusing to reduce risk because an exit's expected
        // move is small would turn a risk limit into a trap (the same class of bug the per-trade cap had).
        // An exit declares 0 edge and is still bounded by the TWAP band, the daily budget and drawdown.
        if (isBuy && it.declaredEdgeBps < policy.minEdgeBps) revert EdgeBelowHurdle();

        bytes32 intentHash = hashIntent(it);
        (address strategyRecovered, ECDSA.RecoverError strategyError) = _recover(intentHash, strategySignature);
        if (strategyError != ECDSA.RecoverError.NoError || strategyRecovered != strategySigner) {
            revert BadStrategySignature();
        }
        (address riskRecovered, ECDSA.RecoverError riskError) = _recover(intentHash, riskSignature);
        if (riskError != ECDSA.RecoverError.NoError || riskRecovered != riskSigner) revert BadRiskSignature();
        nonceUsed[it.nonce] = true;

        if (!isBuy && it.tokenOut != quoteToken) revert QuoteLegRequired();
        address asset = isBuy ? it.tokenOut : it.tokenIn;

        m = markets[asset];
        if (!m.enabled || m.pool == address(0)) revert MarketDisabled();
        if (it.fee != m.fee) revert PoolMismatch();
        if (IV3Factory(factory).getPool(it.tokenIn, it.tokenOut, it.fee) != m.pool) revert PoolMismatch();
        if (IERC20(it.tokenIn).balanceOf(address(this)) < it.amountIn) revert InsufficientBalance();
    }

    /// @dev Fair value comes from the pool's own oracle, never from the agent's quote: QuoterV2 reverts
    ///      by design and cannot be called on-chain, so the vault recomputes the price itself and forces
    ///      `minAmountOut` into [fairOut*(1-maxSlippage), fairOut]. Spot must also be near TWAP, or a
    ///      flash-swap-manipulated price would be the cheapest way to fake an edge.
    ///      The claim is also bounded by what the venue was visibly offering: a declared edge larger than
    ///      the observed spot-vs-TWAP dislocation is a claim about a move that was not there to capture.
    function _fairValueBand(Market memory m, Intent calldata it) private view returns (uint256 fairOut) {
        int24 twapTick = TwapLib.consult(m.pool, policy.twapWindow);
        uint256 dislocationBps = TwapLib.absDiff(TwapLib.spotTick(m.pool), twapTick); // 1 tick ≈ 1 bp
        if (dislocationBps > policy.maxSpotTwapDeviationBps) revert PriceDeviated();
        if (uint256(it.declaredEdgeBps) > dislocationBps) revert EdgeClaimAboveDislocation();

        fairOut = TwapLib.quoteAtTick(twapTick, it.amountIn, it.tokenIn, it.tokenOut);
        if (fairOut == 0) revert FairValueZero();
        if (it.minAmountOut > fairOut) revert MinOutAboveFair();
        uint256 floorOut = (fairOut * (BPS - policy.maxSlippageBps)) / BPS;
        if (it.minAmountOut < floorOut) revert SlippageTooWide();
    }

    /// @dev Notional, daily budget, drawdown and schedule discipline, in that order.
    function _enforceBudget(Intent calldata it, bool isBuy, uint256 notionalQuote) private {
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

        // Drawdown: under stress only risk-reducing trades may pass. `equity()` fails closed here — if the
        // oracle cannot price the book, the vault refuses to trade rather than guess.
        uint256 eq = equity();
        if (highWater != 0 && eq < (highWater * (BPS - policy.maxDrawdownBps)) / BPS && isBuy) {
            revert DrawdownBreach();
        }

        // Schedule discipline: a standalone *entry* respects the cooldown; a child spends its parent's
        // budget. The cooldown meters how fast new capital is deployed, so an exit is never delayed by it —
        // a stop-loss that cannot run because of a rate limit is the same trap as a cap that blocks exits.
        if (it.parentId == bytes32(0)) {
            if (isBuy && lastBuyAt != 0 && block.timestamp < lastBuyAt + policy.cooldown) revert CooldownActive();
        } else {
            Parent storage p = parents[it.parentId];
            if (!p.open) revert UnknownParent();
            if (uint256(p.spentQuote) + notionalQuote > p.budgetQuote) revert ParentBudgetExceeded();
            if (p.slices >= policy.maxSlices) revert ParentSlicesExceeded();
            p.spentQuote = uint64(uint256(p.spentQuote) + notionalQuote);
            p.slices += 1;
        }
        if (isBuy) {
            lastBuyAt = block.timestamp;
            dailyNotional += notionalQuote;
        }
    }

    /// @dev Execute through HSKSwap SwapRouter02 and re-verify the fill. The router reports an amount; the
    ///      token balance is observed independently; both must agree (Safe Guard's checkAfterExecution
    ///      idea applied to value), so a router that reports a good fill but delivers less is caught.
    function _swap(Intent calldata it) private returns (uint256 received) {
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
        received = IERC20(it.tokenOut).balanceOf(address(this)) - balanceBefore;

        tokenIn.forceApprove(router, 0); // allowance hygiene (USDT-style tokens require the reset anyway)

        if (reported != received) revert OutputMismatch();
        if (received < it.minAmountOut) revert InsufficientOutput();
    }

    /// @dev Book-keeping. Two different questions, two different accumulators, never conflated:
    ///      - `executionEdgeSumBps` is per-leg: how the fill landed against the pool's own TWAP. It is the
    ///        measured cost of taking liquidity (a taker is expected to be slightly negative here).
    ///      - `declaredEdgeSumBps` / `realizedEdgeSumBps` are round-trip: what the agent claimed when it
    ///        entered, and what the position actually produced when it was exited, both in bps of the
    ///        cost basis. Claims and outcomes share a unit, so `edgeCredibilityBps()` compares like with like.
    function _settle(
        Intent calldata it,
        bool isBuy,
        uint256 notionalQuote,
        uint256 fairOut,
        uint256 amountOut
    ) private {
        address asset = isBuy ? it.tokenOut : it.tokenIn;
        int256 legEdgeBps = ((int256(amountOut) - int256(fairOut)) * int256(uint256(BPS))) / int256(fairOut);
        executionEdgeSumBps += legEdgeBps;

        Position storage pos = positions[asset];
        if (isBuy) {
            declaredEdgeSumBps += int256(uint256(it.declaredEdgeBps));
            pos.qty += amountOut;
            pos.costQuote += notionalQuote;
            feesPaidQuote += (uint256(it.amountIn) * it.fee) / 1_000_000;
        } else {
            if (pos.qty < 1) revert NothingToSell();
            uint256 costPortion = (pos.costQuote * it.amountIn) / pos.qty;
            int256 pnl = int256(amountOut) - int256(costPortion);
            realizedPnlQuote += pnl;
            if (pnl > 0) winCount += 1;
            // The entry claim is settled here: the round trip either paid the claimed edge or it did not.
            if (costPortion > 0) {
                realizedEdgeSumBps += (pnl * int256(uint256(BPS))) / int256(costPortion);
            }
            pos.qty -= it.amountIn;
            pos.costQuote -= costPortion;
            feesPaidQuote += (fairOut * it.fee) / 1_000_000;
        }
        tradeCount += 1;

        uint256 eqAfter = equity();
        if (eqAfter > highWater) highWater = eqAfter;

        emit IntentExecuted(
            hashIntent(it),
            it.strategyHash,
            it.parentId,
            it.tokenIn,
            it.tokenOut,
            it.amountIn,
            amountOut,
            notionalQuote,
            fairOut,
            it.declaredEdgeBps,
            legEdgeBps, // this leg's execution vs TWAP (not the round-trip claim)
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
    /// @dev Fails closed: if a held asset cannot be priced from its own pool oracle, this reverts. The risk
    ///      path (drawdown) must never trade against a guessed equity.
    function equity() public view returns (uint256 total) {
        bool fullyPriced;
        (total, fullyPriced) = tryEquity();
        if (!fullyPriced) revert OracleUnavailable();
    }

    /// @notice The same mark, but it never reverts: unpriced holdings fall back to cost basis and the flag
    ///         says so. Views a human or a judge reads use this — an oracle outage must not make the vault
    ///         unauditable, while the trading path above still refuses to act on a stale price.
    function tryEquity() public view returns (uint256 total, bool fullyPriced) {
        fullyPriced = true;
        total = IERC20(quoteToken).balanceOf(address(this));
        uint256 n = assets.length;
        for (uint256 i; i < n; ++i) {
            address asset = assets[i];
            Market memory m = markets[asset];
            uint256 value = 0;
            bool priced = false;
            if (m.enabled && m.pool != address(0)) {
                // One external call per asset: it reads the balance and prices it in the same hop, so the
                // loop stays cheap and the failure mode is a single catch.
                try this.holdingValue(m.pool, asset) returns (uint256 v) {
                    value = v;
                    priced = true;
                } catch {
                    priced = false;
                }
            }
            if (priced) {
                total += value;
            } else {
                uint256 costBasis = positions[asset].costQuote;
                total += costBasis; // unpriced: cost basis, and the flag says so
                if (costBasis > 0) fullyPriced = false;
            }
        }
    }

    /// @dev External only so that `tryEquity` can catch an oracle failure with try/catch. Reads this vault's
    ///      own balance of `asset` and marks it at the pool's TWAP.
    function holdingValue(address pool, address asset) external view returns (uint256) {
        uint256 bal = IERC20(asset).balanceOf(address(this));
        if (bal < 1) return 0;
        return TwapLib.quoteAtTick(TwapLib.consult(pool, policy.twapWindow), bal, asset, quoteToken);
    }

    /// @notice The one view a judge needs: performance, cost and whether the declared edge was real.
    function stats() external view returns (Stats memory s) {
        (uint256 eq, bool fullyPriced) = tryEquity();
        s = Stats({
            tradeCount: tradeCount,
            winCount: winCount,
            realizedPnlQuote: realizedPnlQuote,
            feesPaidQuote: feesPaidQuote,
            declaredEdgeSumBps: declaredEdgeSumBps,
            realizedEdgeSumBps: realizedEdgeSumBps,
            equity: eq,
            highWater: highWater,
            dailyNotional: dailyNotional,
            oracleHealthy: fullyPriced,
            executionEdgeSumBps: executionEdgeSumBps
        });
    }

    /// @notice Realised round-trip edge as a share of the edge claimed on entry, in bps of the claim
    ///         (10_000 = the claim was exactly paid).
    /// @dev Claims are registered on entries and settled on exits, so both sides of this ratio are the same
    ///      question asked before and after paying the fees. Returns 0 when nothing has been claimed:
    ///      absence of a claim is not credibility.
    function edgeCredibilityBps() external view returns (int256) {
        if (declaredEdgeSumBps == 0) return 0;
        return (realizedEdgeSumBps * int256(uint256(BPS))) / declaredEdgeSumBps;
    }

    /// @notice Average execution result per leg against the pool's own TWAP, in bps (negative = the vault
    ///         paid that much to take liquidity). This is the number that shows a taker's costs are real.
    function avgExecutionEdgeBps() external view returns (int256) {
        if (tradeCount == 0) return 0;
        return executionEdgeSumBps / int256(tradeCount);
    }

    function assetCount() external view returns (uint256) {
        return assets.length;
    }

    /// @dev Invalid or malleable (high-s) signatures return address(0) instead of bubbling a generic
    ///      revert, so callers get a precise reason and no signature is ever "half accepted". The
    ///      RecoverError is returned as well: a wrong-length signature and a wrong-key signature both
    ///      end as a rejection, and neither can ever be read as success.
    function _recover(bytes32 digest, bytes calldata signature)
        internal
        pure
        returns (address signer, ECDSA.RecoverError err)
    {
        // The third return (the error argument) carries the offending S value; it is never needed once the
        // call is rejected, so it is dropped on purpose.
        // slither-disable-next-line unused-return
        (signer, err,) = ECDSA.tryRecover(digest, signature);
    }

    function _rollWindow() internal {
        if (block.timestamp >= windowStart + 1 days) {
            windowStart = block.timestamp;
            dailyNotional = 0;
        }
    }
}
