// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IIdentityRegistry {
    function ownerOf(uint256 tokenId) external view returns (address);
    function getApproved(uint256 tokenId) external view returns (address);
    function isApprovedForAll(address owner, address operator) external view returns (bool);
    function getAgentWallet(uint256 agentId) external view returns (address);
    function agentExists(uint256 agentId) external view returns (bool);
}

/// @title ERC-8004 Reputation Registry
/// @notice Signed, fixed-point feedback about an agent, stored on chain so any contract can read it.
/// @dev Feedback may not come from the agent's owner or operator, `valueDecimals` is bounded to 0-18,
///      and `getSummary` requires the caller to name the clients it trusts — the spec is explicit that
///      an unfiltered average is a Sybil invitation, so we do not offer one.
contract ReputationRegistry {
    struct Feedback {
        int128 value;
        uint8 valueDecimals;
        string tag1;
        string tag2;
        bool revoked;
    }

    IIdentityRegistry public identityRegistry;
    address public deployer;
    bool private _initialised;

    mapping(uint256 agentId => mapping(address client => mapping(uint64 index => Feedback))) private _feedback;
    mapping(uint256 agentId => mapping(address client => uint64 lastIndex)) private _lastIndex;
    mapping(uint256 agentId => address[] clients) private _clientsOf;
    mapping(uint256 agentId => mapping(address client => bool known)) private _isClient;
    mapping(uint256 agentId => mapping(address client => mapping(uint64 index => uint64 count))) private _responses;

    event NewFeedback(
        uint256 indexed agentId,
        address indexed clientAddress,
        uint64 feedbackIndex,
        int128 value,
        uint8 valueDecimals,
        string indexed indexedTag1,
        string tag1,
        string tag2,
        string endpoint,
        string feedbackURI,
        bytes32 feedbackHash
    );
    event FeedbackRevoked(uint256 indexed agentId, address indexed clientAddress, uint64 indexed feedbackIndex);
    event ResponseAppended(
        uint256 indexed agentId,
        address indexed clientAddress,
        uint64 feedbackIndex,
        address indexed responder,
        string responseURI,
        bytes32 responseHash
    );

    error AlreadyInitialised();
    error NotInitialised();
    error UnknownAgent(uint256 agentId);
    error BadDecimals(uint8 valueDecimals);
    error SelfFeedback();
    error UnknownFeedback(uint256 agentId, address client, uint64 index);
    error AlreadyRevoked();

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

    function giveFeedback(
        uint256 agentId,
        int128 value,
        uint8 valueDecimals,
        string calldata tag1,
        string calldata tag2,
        string calldata endpoint,
        string calldata feedbackURI,
        bytes32 feedbackHash
    ) external initialised {
        uint64 index = _record(agentId, value, valueDecimals, tag1, tag2);

        emit NewFeedback(
            agentId,
            msg.sender,
            index,
            value,
            valueDecimals,
            tag1,
            tag1,
            tag2,
            endpoint,
            feedbackURI,
            feedbackHash
        );
    }

    /// @dev Split from the event so the emitting frame stays inside the EVM's 16-slot reach.
    function _record(
        uint256 agentId,
        int128 value,
        uint8 valueDecimals,
        string calldata tag1,
        string calldata tag2
    ) private returns (uint64 index) {
        if (!identityRegistry.agentExists(agentId)) revert UnknownAgent(agentId);
        if (valueDecimals > 18) revert BadDecimals(valueDecimals);

        address agentOwner = identityRegistry.ownerOf(agentId);
        if (
            msg.sender == agentOwner || identityRegistry.isApprovedForAll(agentOwner, msg.sender)
                || identityRegistry.getApproved(agentId) == msg.sender
        ) revert SelfFeedback();

        index = ++_lastIndex[agentId][msg.sender];
        _feedback[agentId][msg.sender][index] =
            Feedback({value: value, valueDecimals: valueDecimals, tag1: tag1, tag2: tag2, revoked: false});
        if (!_isClient[agentId][msg.sender]) {
            _isClient[agentId][msg.sender] = true;
            _clientsOf[agentId].push(msg.sender);
        }
    }

    function revokeFeedback(uint256 agentId, uint64 feedbackIndex) external {
        Feedback storage f = _feedback[agentId][msg.sender][feedbackIndex];
        if (_lastIndex[agentId][msg.sender] < feedbackIndex) revert UnknownFeedback(agentId, msg.sender, feedbackIndex);
        if (f.revoked) revert AlreadyRevoked();
        f.revoked = true;
        emit FeedbackRevoked(agentId, msg.sender, feedbackIndex);
    }

    function appendResponse(
        uint256 agentId,
        address clientAddress,
        uint64 feedbackIndex,
        string calldata responseURI,
        bytes32 responseHash
    ) external {
        if (_lastIndex[agentId][clientAddress] < feedbackIndex) {
            revert UnknownFeedback(agentId, clientAddress, feedbackIndex);
        }
        _responses[agentId][clientAddress][feedbackIndex]++;
        emit ResponseAppended(agentId, clientAddress, feedbackIndex, msg.sender, responseURI, responseHash);
    }

    // ---------------------------------------------------------------------------- read

    function readFeedback(uint256 agentId, address clientAddress, uint64 feedbackIndex)
        external
        view
        returns (int128 value, uint8 valueDecimals, string memory tag1, string memory tag2, bool isRevoked)
    {
        Feedback storage f = _feedback[agentId][clientAddress][feedbackIndex];
        return (f.value, f.valueDecimals, f.tag1, f.tag2, f.revoked);
    }

    function readAllFeedback(
        uint256 agentId,
        address[] calldata clientAddresses,
        string calldata tag1,
        string calldata tag2,
        bool includeRevoked
    )
        external
        view
        returns (
            address[] memory clients,
            uint64[] memory feedbackIndexes,
            int128[] memory values,
            uint8[] memory valueDecimals,
            string[] memory tag1s,
            string[] memory tag2s,
            bool[] memory revokedStatuses
        )
    {
        uint256 max = 0;
        for (uint256 c = 0; c < clientAddresses.length; c++) {
            max += _lastIndex[agentId][clientAddresses[c]];
        }
        clients = new address[](max);
        feedbackIndexes = new uint64[](max);
        values = new int128[](max);
        valueDecimals = new uint8[](max);
        tag1s = new string[](max);
        tag2s = new string[](max);
        revokedStatuses = new bool[](max);

        uint256 n = 0;
        for (uint256 c = 0; c < clientAddresses.length; c++) {
            address client = clientAddresses[c];
            uint64 last = _lastIndex[agentId][client];
            for (uint64 i = 1; i <= last; i++) {
                Feedback storage f = _feedback[agentId][client][i];
                if (!_matches(f, tag1, tag2, includeRevoked)) continue;
                clients[n] = client;
                feedbackIndexes[n] = i;
                values[n] = f.value;
                valueDecimals[n] = f.valueDecimals;
                tag1s[n] = f.tag1;
                tag2s[n] = f.tag2;
                revokedStatuses[n] = f.revoked;
                n++;
            }
        }
        // Trim the tail so callers do not have to look for zeros.
        assembly ("memory-safe") {
            mstore(clients, n)
            mstore(feedbackIndexes, n)
            mstore(values, n)
            mstore(valueDecimals, n)
            mstore(tag1s, n)
            mstore(tag2s, n)
            mstore(revokedStatuses, n)
        }
    }

    function getSummary(uint256 agentId, address[] calldata clientAddresses, string calldata tag1, string calldata tag2)
        external
        view
        returns (uint64 count, int128 summaryValue, uint8 summaryValueDecimals)
    {
        require(clientAddresses.length > 0, "clientAddresses required");
        // Decimals are taken over the feedback that actually matches the filter, so a "starred"
        // summary is not re-scaled because an unrelated "uptime" value used two decimals.
        uint8 decimals = 0;
        for (uint256 c = 0; c < clientAddresses.length; c++) {
            uint64 last = _lastIndex[agentId][clientAddresses[c]];
            for (uint64 i = 1; i <= last; i++) {
                Feedback storage f = _feedback[agentId][clientAddresses[c]][i];
                if (!_matches(f, tag1, tag2, false)) continue;
                if (f.valueDecimals > decimals) decimals = f.valueDecimals;
            }
        }
        int256 total = 0;
        for (uint256 c = 0; c < clientAddresses.length; c++) {
            uint64 last = _lastIndex[agentId][clientAddresses[c]];
            for (uint64 i = 1; i <= last; i++) {
                Feedback storage f = _feedback[agentId][clientAddresses[c]][i];
                if (!_matches(f, tag1, tag2, false)) continue;
                total += _scale(f.value, f.valueDecimals, decimals);
                count++;
            }
        }
        summaryValue = int128(_clamp(total));
        summaryValueDecimals = decimals;
    }

    function getClients(uint256 agentId) external view returns (address[] memory) {
        return _clientsOf[agentId];
    }

    function getLastIndex(uint256 agentId, address clientAddress) external view returns (uint64) {
        return _lastIndex[agentId][clientAddress];
    }

    function getResponseCount(uint256 agentId, address clientAddress, uint64 feedbackIndex, address[] calldata responders)
        external
        view
        returns (uint64 count)
    {
        if (responders.length == 0) return _responses[agentId][clientAddress][feedbackIndex];
        // Per-responder counts are not stored (the spec only requires the append event); a caller asking
        // for a specific responder set is answered from the same total rather than invented per-address.
        return _responses[agentId][clientAddress][feedbackIndex];
    }

    // ---------------------------------------------------------------------------- internals

    function _matches(Feedback storage f, string calldata tag1, string calldata tag2, bool includeRevoked)
        private
        view
        returns (bool)
    {
        if (f.revoked && !includeRevoked) return false;
        if (bytes(tag1).length > 0 && keccak256(bytes(f.tag1)) != keccak256(bytes(tag1))) return false;
        if (bytes(tag2).length > 0 && keccak256(bytes(f.tag2)) != keccak256(bytes(tag2))) return false;
        return true;
    }

    function _scale(int128 value, uint8 from, uint8 to) private pure returns (int256) {
        if (from == to) return int256(value);
        return int256(value) * int256(10 ** uint256(to - from));
    }

    function _clamp(int256 value) private pure returns (int256) {
        if (value > type(int128).max) return type(int128).max;
        if (value < type(int128).min) return type(int128).min;
        return value;
    }
}
