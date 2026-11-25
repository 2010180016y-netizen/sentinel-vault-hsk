// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";

/// @title MockEIP3009 — a 6-decimal token with `transferWithAuthorization`
/// @notice HSK's USDT does not implement EIP-3009, so the x402 settlement leg cannot be exercised
///         against it. This token exists to exercise it for real on a local fork: the exact digest the
///         Python client signs (`agent/x402/eip3009.py`) is verified by the exact OpenZeppelin ECDSA
///         check a production facilitator would rely on, and `authorizationState` makes the nonce
///         single-use on-chain rather than in a dictionary.
/// @dev Test-only: `mint` is open. Never deploy this as money.
contract MockEIP3009 {
    string public name;
    string public version;
    uint8 public constant decimals = 6;
    string public symbol = "mUSDT";

    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    mapping(address => mapping(bytes32 => bool)) public authorizationState;

    bytes32 public constant DOMAIN_TYPEHASH =
        keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");
    bytes32 public constant TRANSFER_WITH_AUTHORIZATION_TYPEHASH = keccak256(
        "TransferWithAuthorization(address from,address to,uint256 value,uint256 validAfter,uint256 validBefore,bytes32 nonce)"
    );

    bytes32 public immutable domainSeparator;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);
    event AuthorizationUsed(address indexed authorizer, bytes32 indexed nonce);

    error InvalidSignature();
    error AuthorizationExpired();
    error AuthorizationNotYetValid();
    error AuthorizationAlreadyUsed();
    error InsufficientBalance();
    error ChainIdMismatch();

    constructor(string memory name_, string memory version_, uint256 chainId_) {
        name = name_;
        version = version_;
        if (chainId_ != block.chainid) revert ChainIdMismatch();
        domainSeparator = keccak256(
            abi.encode(DOMAIN_TYPEHASH, keccak256(bytes(name_)), keccak256(bytes(version_)), block.chainid, address(this))
        );
    }

    function mint(address to, uint256 amount) external {
        totalSupply += amount;
        balanceOf[to] += amount;
        emit Transfer(address(0), to, amount);
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        _move(msg.sender, to, amount);
        return true;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        uint256 allowed = allowance[from][msg.sender];
        if (allowed != type(uint256).max) allowance[from][msg.sender] = allowed - amount;
        _move(from, to, amount);
        return true;
    }

    function _move(address from, address to, uint256 amount) private {
        if (balanceOf[from] < amount) revert InsufficientBalance();
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        emit Transfer(from, to, amount);
    }

    /// @notice EIP-3009: the signature authorises a transfer, and the nonce is consumed exactly once.
    function transferWithAuthorization(
        address from,
        address to,
        uint256 value,
        uint256 validAfter,
        uint256 validBefore,
        bytes32 nonce,
        bytes calldata signature
    ) external {
        if (block.timestamp <= validAfter) revert AuthorizationNotYetValid();
        if (block.timestamp >= validBefore) revert AuthorizationExpired();
        if (authorizationState[from][nonce]) revert AuthorizationAlreadyUsed();

        bytes32 structHash = keccak256(
            abi.encode(TRANSFER_WITH_AUTHORIZATION_TYPEHASH, from, to, value, validAfter, validBefore, nonce)
        );
        bytes32 digest = keccak256(abi.encodePacked("\x19\x01", domainSeparator, structHash));
        (address recovered, ECDSA.RecoverError err,) = ECDSA.tryRecover(digest, signature);
        if (err != ECDSA.RecoverError.NoError || recovered != from) revert InvalidSignature();

        authorizationState[from][nonce] = true;
        emit AuthorizationUsed(from, nonce);
        _move(from, to, value);
    }

    /// @notice The digest a signer must sign, exposed so a client can be verified against the token
    ///         itself instead of against a second implementation of the same type string.
    function authorizationDigest(
        address from,
        address to,
        uint256 value,
        uint256 validAfter,
        uint256 validBefore,
        bytes32 nonce
    ) external view returns (bytes32) {
        bytes32 structHash = keccak256(
            abi.encode(TRANSFER_WITH_AUTHORIZATION_TYPEHASH, from, to, value, validAfter, validBefore, nonce)
        );
        return keccak256(abi.encodePacked("\x19\x01", domainSeparator, structHash));
    }
}
