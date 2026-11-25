// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IIdentityRegistry} from "./ReputationRegistry.sol";

/// @title ERC-8004 Validation Registry
/// @notice Generic hooks for independent validators: an agent owner requests a validation with a
///         commitment (`requestHash`) to the evidence, and the designated validator answers 0-100.
/// @dev The response is deliberately not a boolean: `tag` lets a validator post "soft" then "hard"
///         finality, and an intermediate score is allowed for graded verification.
contract ValidationRegistry {
    struct Validation {
        address validatorAddress;
        uint256 agentId;
        uint8 response;
        bytes32 responseHash;
        uint64 lastUpdate;
        string tag;
        bool responded;
    }

    IIdentityRegistry public identityRegistry;
    address public deployer;
    bool private _initialised;

    mapping(bytes32 requestHash => Validation) private _validations;
    mapping(uint256 agentId => bytes32[] hashes) private _agentRequests;
    mapping(address validator => bytes32[] hashes) private _validatorRequests;

    event ValidationRequest(
        address indexed validatorAddress, uint256 indexed agentId, string requestURI, bytes32 indexed requestHash
    );
    event ValidationResponse(
        address indexed validatorAddress,
        uint256 indexed agentId,
        bytes32 indexed requestHash,
        uint8 response,
        string responseURI,
        bytes32 responseHash,
        string tag
    );

    error AlreadyInitialised();
    error NotInitialised();
    error UnknownAgent(uint256 agentId);
    error NotAgentOperator();
    error RequestExists(bytes32 requestHash);
    error UnknownRequest(bytes32 requestHash);
    error NotDesignatedValidator(address caller);
    error ResponseOutOfRange(uint8 response);

    modifier initialised() {
        if (address(identityRegistry) == address(0)) revert NotInitialised();
        _;
    }

    function initialize(address identityRegistry_) external {
        if (_initialised) revert AlreadyInitialised();
        _initialised = true;
        deployer = msg.sender;
        identityRegistry = IIdentityRegistry(identityRegistry_);
    }

    function getIdentityRegistry() external view returns (address) {
        return address(identityRegistry);
    }

    // ---------------------------------------------------------------------------- write

    function validationRequest(
        address validatorAddress,
        uint256 agentId,
        string calldata requestURI,
        bytes32 requestHash
    ) external initialised {
        if (!identityRegistry.agentExists(agentId)) revert UnknownAgent(agentId);
        address agentOwner = identityRegistry.ownerOf(agentId);
        if (
            msg.sender != agentOwner && !identityRegistry.isApprovedForAll(agentOwner, msg.sender)
                && identityRegistry.getApproved(agentId) != msg.sender
        ) revert NotAgentOperator();
        if (_validations[requestHash].validatorAddress != address(0)) revert RequestExists(requestHash);

        _validations[requestHash] = Validation({
            validatorAddress: validatorAddress,
            agentId: agentId,
            response: 0,
            responseHash: bytes32(0),
            lastUpdate: 0,
            tag: "",
            responded: false
        });
        _agentRequests[agentId].push(requestHash);
        _validatorRequests[validatorAddress].push(requestHash);

        emit ValidationRequest(validatorAddress, agentId, requestURI, requestHash);
    }

    function validationResponse(
        bytes32 requestHash,
        uint8 response,
        string calldata responseURI,
        bytes32 responseHash,
        string calldata tag
    ) external {
        Validation storage v = _validations[requestHash];
        if (v.validatorAddress == address(0)) revert UnknownRequest(requestHash);
        if (msg.sender != v.validatorAddress) revert NotDesignatedValidator(msg.sender);
        if (response > 100) revert ResponseOutOfRange(response);

        v.response = response;
        v.responseHash = responseHash;
        v.lastUpdate = uint64(block.timestamp);
        v.tag = tag;
        v.responded = true;

        emit ValidationResponse(msg.sender, v.agentId, requestHash, response, responseURI, responseHash, tag);
    }

    // ---------------------------------------------------------------------------- read

    function getValidationStatus(bytes32 requestHash)
        external
        view
        returns (
            address validatorAddress,
            uint256 agentId,
            uint8 response,
            bytes32 responseHash,
            string memory tag,
            uint64 lastUpdate
        )
    {
        Validation storage v = _validations[requestHash];
        if (v.validatorAddress == address(0)) revert UnknownRequest(requestHash);
        return (v.validatorAddress, v.agentId, v.response, v.responseHash, v.tag, v.lastUpdate);
    }

    function getAgentValidations(uint256 agentId) external view returns (bytes32[] memory) {
        return _agentRequests[agentId];
    }

    function getValidatorRequests(address validatorAddress) external view returns (bytes32[] memory) {
        return _validatorRequests[validatorAddress];
    }

    /// @notice Average response over a caller-chosen validator set, the same Sybil reasoning as the
    ///         reputation summary: the set is the caller's, never "everyone".
    function getSummary(uint256 agentId, bytes32[] calldata requestHashes)
        external
        view
        returns (uint64 count, uint8 averageResponse)
    {
        uint256 total = 0;
        for (uint256 i = 0; i < requestHashes.length; i++) {
            Validation storage v = _validations[requestHashes[i]];
            if (v.agentId != agentId || !v.responded) continue;
            total += v.response;
            count++;
        }
        averageResponse = count == 0 ? 0 : uint8(total / count);
    }
}
