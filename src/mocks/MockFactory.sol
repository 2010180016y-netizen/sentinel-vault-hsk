// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IV3Factory} from "../interfaces/IHskSwap.sol";

contract MockFactory is IV3Factory {
    mapping(bytes32 => address) public poolOf;

    function _key(address a, address b, uint24 fee) internal pure returns (bytes32) {
        (address lo, address hi) = a < b ? (a, b) : (b, a);
        return keccak256(abi.encode(lo, hi, fee));
    }

    function setPool(address a, address b, uint24 fee, address pool) external {
        poolOf[_key(a, b, fee)] = pool;
    }

    function getPool(address a, address b, uint24 fee) external view returns (address) {
        return poolOf[_key(a, b, fee)];
    }
}
