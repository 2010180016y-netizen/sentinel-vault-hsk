// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

import {ReputationRegistry} from "./registries/ReputationRegistry.sol";
import {ValidationRegistry} from "./registries/ValidationRegistry.sol";

interface ISentinelStats {
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

    function stats() external view returns (Stats memory);
    function edgeCredibilityBps() external view returns (int256);
    function avgExecutionEdgeBps() external view returns (int256);
}

/// @title Sentinel8004Adapter — ERC-8004 reputation and validation driven by the vault's own numbers
/// @notice The interesting claim an agent makes is "my calls are worth their cost". ERC-8004 lets an
///         independent party post that claim as reputation and validate it. This adapter is that
///         party: it reads `SentinelVault.stats()` — an immutable, on-chain record the agent cannot
///         edit — and posts
///
///           * reputation: `edgeCredibilityBps` (realised edge / declared edge, 10,000 = claims held),
///             tagged `edgeCredibility`, and `avgExecutionEdgeBps` tagged `executionCost`;
///           * validation: 0-100 for a requested `requestHash`, where 100 requires enough trades,
///             a healthy oracle and credibility above the threshold the requester chose.
///
/// @dev The adapter never holds the agent id and never trades. It is the client in ERC-8004 terms,
///      which is exactly why it is allowed to give feedback: the spec forbids an agent's owner from
///      rating its own agent.
contract Sentinel8004Adapter is Ownable {
    ReputationRegistry public immutable reputation;
    ValidationRegistry public immutable validation;

    uint256 public agentId;
    string public constant TAG_CREDIBILITY = "edgeCredibility";
    string public constant TAG_EXECUTION = "executionCost";

    event AgentSet(uint256 indexed agentId);
    event Attested(uint256 indexed agentId, address indexed vault, int128 credibilityBps, uint256 trades);
    event Validated(bytes32 indexed requestHash, address indexed vault, uint8 response, int256 credibilityBps);

    error NoAgent();
    error NoClaimsYet(int256 declaredEdgeSumBps);
    error VaultUnreadable(address vault);

    constructor(address reputation_, address validation_, address owner_) Ownable(owner_) {
        reputation = ReputationRegistry(reputation_);
        validation = ValidationRegistry(validation_);
    }

    /// @notice Point the adapter at the agent id whose work it will attest. The id must exist in the
    ///         identity registry that backs both reputation and validation.
    function setAgent(uint256 agentId_) external onlyOwner {
        agentId = agentId_;
        emit AgentSet(agentId_);
    }

    /// @notice Post reputation for `vault`'s record. Reverts when the agent has never declared an edge:
    ///         a 0/0 credibility ratio is "no evidence", not "perfect".
    function attest(address vault) external returns (int128 credibilityBps) {
        if (agentId == 0) revert NoAgent();
        ISentinelStats.Stats memory s = _stats(vault);
        if (s.declaredEdgeSumBps <= 0) revert NoClaimsYet(s.declaredEdgeSumBps);

        credibilityBps = int128(ISentinelStats(vault).edgeCredibilityBps());
        int128 executionBps = int128(ISentinelStats(vault).avgExecutionEdgeBps());

        reputation.giveFeedback(
            agentId,
            credibilityBps,
            0,
            TAG_CREDIBILITY,
            "bps",
            "",
            "",
            bytes32(0)
        );
        reputation.giveFeedback(agentId, executionBps, 0, TAG_EXECUTION, "bps", "", "", bytes32(0));
        emit Attested(agentId, vault, credibilityBps, s.tradeCount);
    }

    /// @notice Answer a validation request with a verdict derived from the vault, never from a caller's
    ///         opinion. `minTrades` keeps a two-trade lucky streak from reading as a validated system.
    function validate(bytes32 requestHash, address vault, uint16 minCredibilityBps, uint256 minTrades)
        external
        returns (uint8 response)
    {
        ISentinelStats.Stats memory s = _stats(vault);
        int256 credibility = ISentinelStats(vault).edgeCredibilityBps();
        bool passed = s.tradeCount >= minTrades && s.oracleHealthy && credibility >= int256(uint256(minCredibilityBps));
        response = passed ? 100 : 0;
        validation.validationResponse(requestHash, response, "", bytes32(0), passed ? "hard" : "soft");
        emit Validated(requestHash, vault, response, credibility);
    }

    function _stats(address vault) private view returns (ISentinelStats.Stats memory s) {
        if (vault.code.length == 0) revert VaultUnreadable(vault);
        try ISentinelStats(vault).stats() returns (ISentinelStats.Stats memory read) {
            s = read;
        } catch {
            revert VaultUnreadable(vault);
        }
    }
}
