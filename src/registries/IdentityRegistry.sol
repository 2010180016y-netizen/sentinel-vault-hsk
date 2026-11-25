// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ERC721} from "@openzeppelin/contracts/token/ERC721/ERC721.sol";
import {ERC721URIStorage} from "@openzeppelin/contracts/token/ERC721/extensions/ERC721URIStorage.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";

/// @title ERC-8004 Identity Registry
/// @notice A portable, censorship-resistant handle for an agent. An agent id is an ERC-721 token whose
///         `tokenURI` resolves to the agent's registration file, so every wallet and marketplace that
///         speaks ERC-721 can browse agents without new tooling.
/// @dev Implemented from `EIPS/eip-8004.md` (Identity Registry, on-chain metadata, reserved
///      `agentWallet` key). The bits we changed on purpose are marked; the rest is the spec verbatim:
///      `register`, `setAgentURI`, `getMetadata/setMetadata`, `getAgentWallet/setAgentWallet/
///      unsetAgentWallet`, `Registered`/`URIUpdated`/`MetadataSet`, and wallet clearing on transfer.
contract IdentityRegistry is ERC721URIStorage, EIP712 {
    struct MetadataEntry {
        string metadataKey;
        bytes metadataValue;
    }

    /// @dev Reserved key from the spec: it may not be written through `setMetadata` or `register`.
    string public constant AGENT_WALLET_KEY = "agentWallet";
    bytes32 public constant SET_AGENT_WALLET_TYPEHASH =
        keccak256("SetAgentWallet(uint256 agentId,address newWallet,uint256 deadline)");

    uint256 private _nextAgentId = 1;
    mapping(uint256 agentId => address wallet) private _agentWallet;
    mapping(uint256 agentId => mapping(bytes32 key => bytes value)) private _metadata;

    event Registered(uint256 indexed agentId, string agentURI, address indexed owner);
    event URIUpdated(uint256 indexed agentId, string newURI, address indexed updatedBy);
    event MetadataSet(
        uint256 indexed agentId, string indexed indexedMetadataKey, string metadataKey, bytes metadataValue
    );
    event AgentWalletSet(uint256 indexed agentId, address indexed wallet);

    error AgentNotFound(uint256 agentId);
    error ReservedKey();
    error InvalidURI();
    error WalletProofExpired();
    error WalletProofInvalid();

    constructor() ERC721("ERC-8004 Agent Identity", "AGENT8004") EIP712("ERC-8004 IdentityRegistry", "1") {}

    // ---------------------------------------------------------------------------- registration

    function register() external returns (uint256 agentId) {
        return _register(msg.sender, "", new MetadataEntry[](0));
    }

    function register(string calldata agentURI) external returns (uint256 agentId) {
        return _register(msg.sender, agentURI, new MetadataEntry[](0));
    }

    function register(string calldata agentURI, MetadataEntry[] calldata metadata)
        external
        returns (uint256 agentId)
    {
        return _register(msg.sender, agentURI, metadata);
    }

    function _register(address owner_, string memory agentURI, MetadataEntry[] memory metadata)
        internal
        returns (uint256 agentId)
    {
        agentId = _nextAgentId++;
        _mint(owner_, agentId);
        if (bytes(agentURI).length > 0) _setTokenURI(agentId, agentURI);

        // The spec: the agent wallet starts as the owner's, and that is announced like any metadata.
        _agentWallet[agentId] = owner_;
        emit MetadataSet(agentId, AGENT_WALLET_KEY, AGENT_WALLET_KEY, abi.encode(owner_));
        emit AgentWalletSet(agentId, owner_);

        for (uint256 i = 0; i < metadata.length; i++) {
            _setMetadata(agentId, metadata[i].metadataKey, metadata[i].metadataValue);
        }
        emit Registered(agentId, agentURI, owner_);
    }

    function setAgentURI(uint256 agentId, string calldata newURI) external {
        // `_update`-based ownership means the operator check is the ERC-721 one: owner or approved.
        if (!_isAuthorised(agentId, msg.sender)) revert ERC721NonexistentToken(agentId);
        _setTokenURI(agentId, newURI);
        emit URIUpdated(agentId, newURI, msg.sender);
    }

    // ---------------------------------------------------------------------------- metadata

    function getMetadata(uint256 agentId, string calldata metadataKey) external view returns (bytes memory) {
        if (_ownerOf(agentId) == address(0)) revert AgentNotFound(agentId);
        return _metadata[agentId][keccak256(bytes(metadataKey))];
    }

    function setMetadata(uint256 agentId, string calldata metadataKey, bytes calldata metadataValue) external {
        if (_ownerOf(agentId) == address(0)) revert AgentNotFound(agentId);
        if (!_isAuthorised(agentId, msg.sender)) revert ERC721NonexistentToken(agentId);
        _setMetadata(agentId, metadataKey, metadataValue);
    }

    function _setMetadata(uint256 agentId, string memory metadataKey, bytes memory metadataValue) private {
        if (keccak256(bytes(metadataKey)) == keccak256(bytes(AGENT_WALLET_KEY))) revert ReservedKey();
        _metadata[agentId][keccak256(bytes(metadataKey))] = metadataValue;
        emit MetadataSet(agentId, metadataKey, metadataKey, metadataValue);
    }

    // ---------------------------------------------------------------------------- agent wallet

    function getAgentWallet(uint256 agentId) external view returns (address) {
        if (_ownerOf(agentId) == address(0)) revert AgentNotFound(agentId);
        return _agentWallet[agentId];
    }

    /// @notice Change the paying address, proving control of the new wallet (EIP-712 for EOAs,
    ///         ERC-1271 handled by the caller off-chain in this implementation).
    function setAgentWallet(uint256 agentId, address newWallet, uint256 deadline, bytes calldata signature)
        external
    {
        if (_ownerOf(agentId) == address(0)) revert AgentNotFound(agentId);
        if (!_isAuthorised(agentId, msg.sender)) revert ERC721NonexistentToken(agentId);
        if (block.timestamp > deadline) revert WalletProofExpired();

        bytes32 digest = _hashTypedDataV4(
            keccak256(abi.encode(SET_AGENT_WALLET_TYPEHASH, agentId, newWallet, deadline))
        );
        // `tryRecover` rather than `recover`: a malformed signature is a rejection, not a panic.
        (address recovered, ECDSA.RecoverError err,) = ECDSA.tryRecover(digest, signature);
        if (err != ECDSA.RecoverError.NoError || recovered != newWallet) revert WalletProofInvalid();

        _agentWallet[agentId] = newWallet;
        emit MetadataSet(agentId, AGENT_WALLET_KEY, AGENT_WALLET_KEY, abi.encode(newWallet));
        emit AgentWalletSet(agentId, newWallet);
    }

    function unsetAgentWallet(uint256 agentId) external {
        if (_ownerOf(agentId) == address(0)) revert AgentNotFound(agentId);
        if (!_isAuthorised(agentId, msg.sender)) revert ERC721NonexistentToken(agentId);
        _agentWallet[agentId] = address(0);
        emit AgentWalletSet(agentId, address(0));
    }

    /// @dev Spec: transferring the agent clears the wallet, so the new owner must prove control again.
    function _update(address to, uint256 tokenId, address auth) internal override returns (address from) {
        from = super._update(to, tokenId, auth);
        if (from != address(0) && from != to) {
            _agentWallet[tokenId] = address(0);
            emit AgentWalletSet(tokenId, address(0));
        }
    }

    function _isAuthorised(uint256 agentId, address who) private view returns (bool) {
        address holder = _ownerOf(agentId);
        return who == holder || isApprovedForAll(holder, who) || getApproved(agentId) == who;
    }

    // ---------------------------------------------------------------------------- views for other registries

    function agentExists(uint256 agentId) external view returns (bool) {
        return _ownerOf(agentId) != address(0);
    }

    /// @notice `{namespace}:{chainId}:{identityRegistry}` as the spec names an agent registry.
    function agentRegistryId() external view returns (string memory) {
        return string.concat("eip155:", _toString(block.chainid), ":", _toString(uint256(uint160(address(this)))));
    }

    function _toString(uint256 value) private pure returns (string memory) {
        if (value == 0) return "0";
        uint256 temp = value;
        uint256 digits;
        while (temp != 0) {
            digits++;
            temp /= 10;
        }
        bytes memory buffer = new bytes(digits);
        while (value != 0) {
            digits--;
            buffer[digits] = bytes1(uint8(48 + (value % 10)));
            value /= 10;
        }
        return string(buffer);
    }
}
