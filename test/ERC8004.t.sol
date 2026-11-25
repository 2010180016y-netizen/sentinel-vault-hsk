// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";

import {IdentityRegistry} from "../src/registries/IdentityRegistry.sol";
import {ReputationRegistry} from "../src/registries/ReputationRegistry.sol";
import {ValidationRegistry} from "../src/registries/ValidationRegistry.sol";
import {ISentinelStats, Sentinel8004Adapter} from "../src/Sentinel8004Adapter.sol";

/// @dev A vault-shaped contract with settable numbers, so the adapter's reading is testable without
///      pushing trades through the policy kernel (that path is covered by SentinelVault.t.sol).
contract MockStatsVault is ISentinelStats {
    Stats internal s;

    function set(uint256 trades, int256 declared, int256 realised, bool healthy) external {
        s.tradeCount = trades;
        s.declaredEdgeSumBps = declared;
        s.realizedEdgeSumBps = realised;
        s.oracleHealthy = healthy;
    }

    function stats() external view returns (Stats memory) {
        return s;
    }

    function edgeCredibilityBps() external view returns (int256) {
        if (s.declaredEdgeSumBps == 0) return 0;
        return (s.realizedEdgeSumBps * 10_000) / s.declaredEdgeSumBps;
    }

    function avgExecutionEdgeBps() external view returns (int256) {
        if (s.tradeCount == 0) return 0;
        return s.executionEdgeSumBps / int256(s.tradeCount);
    }
}

contract ERC8004Test is Test {
    IdentityRegistry internal identity;
    ReputationRegistry internal reputation;
    ValidationRegistry internal validation;
    Sentinel8004Adapter internal adapter;

    address internal operator = address(0xA11CE);
    address internal client = address(0xB0B);
    address internal validator = address(0xCAFE);
    address internal stranger = address(0xDEAD);

    uint256 internal agentId;

    function setUp() public {
        identity = new IdentityRegistry();
        reputation = new ReputationRegistry();
        reputation.initialize(address(identity));
        validation = new ValidationRegistry();
        validation.initialize(address(identity));

        vm.prank(operator);
        agentId = identity.register("ipfs://registration.json");
        adapter = new Sentinel8004Adapter(address(reputation), address(validation), operator);
    }

    // ------------------------------------------------------------------ identity

    function test_IdentityIsAnERC721WithAResolvableURI() public view {
        assertEq(identity.ownerOf(agentId), operator);
        assertEq(identity.tokenURI(agentId), "ipfs://registration.json");
        assertEq(identity.getAgentWallet(agentId), operator, "wallet starts as the owner");
        assertTrue(identity.agentExists(agentId));
    }

    function test_ReservedAgentWalletKeyCannotBeSmuggledThroughMetadata() public {
        vm.prank(operator);
        vm.expectRevert(IdentityRegistry.ReservedKey.selector);
        identity.setMetadata(agentId, "agentWallet", abi.encode(stranger));
    }

    function test_MetadataRoundTrip() public {
        vm.prank(operator);
        identity.setMetadata(agentId, "track", bytes("AI Agent Trading"));
        assertEq(identity.getMetadata(agentId, "track"), bytes("AI Agent Trading"));
    }

    function test_OnlyOwnerOrOperatorMayUpdateTheURI() public {
        vm.prank(stranger);
        vm.expectRevert(abi.encodeWithSelector(0x7e273289, agentId)); // ERC721NonexistentToken
        identity.setAgentURI(agentId, "ipfs://other.json");
    }

    function test_AgentWalletNeedsAProofOfControl() public {
        uint256 newPk = 0xBEEF;
        address newWallet = vm.addr(newPk);
        uint256 deadline = block.timestamp + 1 hours;

        bytes32 digest = keccak256(
            abi.encodePacked(
                "\x19\x01",
                keccak256(
                    abi.encode(
                        keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
                        keccak256("ERC-8004 IdentityRegistry"),
                        keccak256("1"),
                        block.chainid,
                        address(identity)
                    )
                ),
                keccak256(
                    abi.encode(
                        keccak256("SetAgentWallet(uint256 agentId,address newWallet,uint256 deadline)"),
                        agentId,
                        newWallet,
                        deadline
                    )
                )
            )
        );
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(newPk, digest);
        bytes memory sig = abi.encodePacked(r, s, v);

        // Someone else's signature is not a proof.
        vm.prank(operator);
        vm.expectRevert(IdentityRegistry.WalletProofInvalid.selector);
        identity.setAgentWallet(agentId, newWallet, deadline, abi.encodePacked(bytes32(0), bytes32(0), uint8(27)));

        vm.prank(operator);
        identity.setAgentWallet(agentId, newWallet, deadline, sig);
        assertEq(identity.getAgentWallet(agentId), newWallet);
    }

    function test_TransferClearsTheAgentWallet() public {
        address newOwner = address(0xF00D);
        vm.prank(operator);
        identity.transferFrom(operator, newOwner, agentId);
        assertEq(identity.getAgentWallet(agentId), address(0), "new owner must re-prove wallet control");
    }

    // ------------------------------------------------------------------ reputation

    function test_FeedbackIsRecordedAndSummarised() public {
        vm.startPrank(client);
        reputation.giveFeedback(agentId, 87, 0, "starred", "", "", "", bytes32(0));
        reputation.giveFeedback(agentId, 9977, 2, "uptime", "", "", "", bytes32(0));
        vm.stopPrank();

        address[] memory clients = new address[](1);
        clients[0] = client;

        (uint64 count, int128 total, uint8 decimals) = reputation.getSummary(agentId, clients, "starred", "");
        assertEq(count, 1);
        assertEq(total, 87);
        assertEq(decimals, 0);

        string[] memory tags = new string[](0);
        tags;
        (count, total, decimals) = reputation.getSummary(agentId, clients, "", "");
        assertEq(count, 2);
        assertEq(decimals, 2, "summary is scaled to the widest decimal in the set");
        assertEq(total, 87_00 + 9977, "87/100 becomes 8700/10000 when another value uses 2 decimals");
    }

    function test_TheOwnerCannotRateItsOwnAgent() public {
        vm.prank(operator);
        vm.expectRevert(ReputationRegistry.SelfFeedback.selector);
        reputation.giveFeedback(agentId, 100, 0, "starred", "", "", "", bytes32(0));
    }

    function test_RevokedFeedbackIsOmittedFromTheSummary() public {
        vm.startPrank(client);
        reputation.giveFeedback(agentId, 100, 0, "starred", "", "", "", bytes32(0));
        reputation.giveFeedback(agentId, 0, 0, "starred", "", "", "", bytes32(0));
        reputation.revokeFeedback(agentId, 2);
        vm.stopPrank();

        address[] memory clients = new address[](1);
        clients[0] = client;
        (uint64 count, int128 total,) = reputation.getSummary(agentId, clients, "", "");
        assertEq(count, 1);
        assertEq(total, 100);
        assertEq(reputation.getLastIndex(agentId, client), 2);
    }

    function test_FeedbackIndexingIsPerClient() public {
        vm.prank(client);
        reputation.giveFeedback(agentId, 1, 0, "a", "", "", "", bytes32(0));
        vm.prank(stranger);
        reputation.giveFeedback(agentId, 2, 0, "a", "", "", "", bytes32(0));
        assertEq(reputation.getLastIndex(agentId, client), 1);
        assertEq(reputation.getLastIndex(agentId, stranger), 1);
        assertEq(reputation.getClients(agentId).length, 2);
    }

    // ------------------------------------------------------------------ validation

    function test_ValidationRequestAndResponse() public {
        bytes32 requestHash = keccak256("trade-batch-1");
        vm.prank(operator);
        validation.validationRequest(validator, agentId, "ipfs://evidence.json", requestHash);

        // Only the designated validator may answer.
        vm.prank(stranger);
        vm.expectRevert(abi.encodeWithSelector(ValidationRegistry.NotDesignatedValidator.selector, stranger));
        validation.validationResponse(requestHash, 100, "", bytes32(0), "hard");

        vm.prank(validator);
        validation.validationResponse(requestHash, 100, "ipfs://audit.json", keccak256("audit"), "hard");

        (address who, uint256 id, uint8 response, bytes32 hash, string memory tag, uint64 updated) =
            validation.getValidationStatus(requestHash);
        assertEq(who, validator);
        assertEq(id, agentId);
        assertEq(response, 100);
        assertEq(hash, keccak256("audit"));
        assertEq(tag, "hard");
        assertGt(updated, 0);

        bytes32[] memory hashes = new bytes32[](1);
        hashes[0] = requestHash;
        (uint64 count, uint8 average) = validation.getSummary(agentId, hashes);
        assertEq(count, 1);
        assertEq(average, 100);
    }

    function test_OnlyTheAgentOperatorMayRequestValidation() public {
        bytes32 requestHash = keccak256("trade-batch-2");
        vm.prank(stranger);
        vm.expectRevert(ValidationRegistry.NotAgentOperator.selector);
        validation.validationRequest(validator, agentId, "ipfs://", requestHash);
    }

    // ------------------------------------------------------------------ adapter

    function test_AdapterAttestsFromVaultNumbers() public {
        MockStatsVault vault = new MockStatsVault();
        vault.set(12, 1_000, 850, true);

        vm.prank(operator);
        adapter.setAgent(agentId);

        int128 credibility = adapter.attest(address(vault));
        assertEq(credibility, 8_500);

        address[] memory clients = new address[](1);
        clients[0] = address(adapter);
        (uint64 count, int128 total,) = reputation.getSummary(agentId, clients, "edgeCredibility", "");
        assertEq(count, 1);
        assertEq(total, 8_500);
    }

    function test_AdapterRefusesToAttestAnAgentWithNoClaims() public {
        MockStatsVault vault = new MockStatsVault();
        vault.set(0, 0, 0, true);
        vm.prank(operator);
        adapter.setAgent(agentId);
        vm.expectRevert(abi.encodeWithSelector(Sentinel8004Adapter.NoClaimsYet.selector, int256(0)));
        adapter.attest(address(vault));
    }

    function test_AdapterValidationIsAPredicateOnTheVault() public {
        MockStatsVault vault = new MockStatsVault();
        vault.set(12, 1_000, 850, true);
        bytes32 requestHash = keccak256("req");
        vm.prank(operator);
        validation.validationRequest(address(adapter), agentId, "ipfs://req.json", requestHash);
        vm.prank(operator);
        adapter.setAgent(agentId);

        // Threshold above the record: fails, and says so with a soft tag.
        uint8 response = adapter.validate(requestHash, address(vault), 9_000, 10);
        (, , uint8 stored, , string memory tag,) = validation.getValidationStatus(requestHash);
        assertEq(response, 0);
        assertEq(stored, 0);
        assertEq(tag, "soft");

        // Threshold below the record: passes.
        response = adapter.validate(requestHash, address(vault), 8_000, 10);
        (, , stored, , tag,) = validation.getValidationStatus(requestHash);
        assertEq(response, 100);
        assertEq(stored, 100);
        assertEq(tag, "hard");
    }

    function test_AdapterValidationRequiresEnoughTrades() public {
        MockStatsVault vault = new MockStatsVault();
        vault.set(3, 1_000, 990, true); // perfect credibility on three trades is not a record
        bytes32 requestHash = keccak256("req-short");
        vm.prank(operator);
        validation.validationRequest(address(adapter), agentId, "ipfs://req.json", requestHash);
        vm.prank(operator);
        adapter.setAgent(agentId);
        assertEq(adapter.validate(requestHash, address(vault), 5_000, 10), 0);
    }

    function test_AdapterRejectsAContractThatIsNotAVault() public {
        vm.prank(operator);
        adapter.setAgent(agentId);
        vm.expectRevert(abi.encodeWithSelector(Sentinel8004Adapter.VaultUnreadable.selector, address(stranger)));
        adapter.attest(stranger);
    }
}
