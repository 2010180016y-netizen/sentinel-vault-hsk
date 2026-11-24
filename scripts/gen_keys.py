"""Generate the three keys the vault needs — owner/deployer, strategy signer, risk signer.

The vault rejects a deployment unless the strategy and risk signers differ
(`SentinelVault` constructor: `strategySigner_ == riskSigner_ -> InvalidConfig`), and the three roles
should never share a key either. This writes them to a single local file that is git-ignored by
pattern (`.env*`), sets its mode to 600, and prints only the public addresses.

    python scripts/gen_keys.py                 # writes .env.wallets.local if it does not exist
    python scripts/gen_keys.py --force         # rotate: overwrite an existing file

Nothing is sent to the network. Funds stay where they are until HSK is sent to the address printed.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys

from eth_account import Account

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(HERE, ".env.wallets.local")

HEADER = """\
# Sentinel HSK — LOCAL ONLY. Never commit, never paste into a chat or an issue.
# Three roles, three keys: the vault refuses to deploy if strategy == risk.
# The owner key is also the deployer: it pays the one-time gas and is the only key that can
# arm, re-policy or unpause the vault. Keep it offline when the demo is not running.
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="overwrite an existing wallet file (rotates keys)")
    a = ap.parse_args()

    if os.path.exists(TARGET) and not a.force:
        print(f"{TARGET} already exists — refusing to overwrite (use --force to rotate).")
        return 2

    roles = {
        "SENTINEL_OWNER": Account.create(),      # owner + deployer (address reused below)
        "SENTINEL_STRATEGY": Account.create(),   # strategy signature half of the EIP-712 pair
        "SENTINEL_RISK": Account.create(),       # risk signature half of the EIP-712 pair
    }
    owner = roles["SENTINEL_OWNER"]

    lines = [HEADER]
    lines.append("HSK_RPC=https://mainnet.hsk.xyz")
    lines.append("SENTINEL_LIVE=0")
    lines.append("")
    lines.append(f"SENTINEL_OWNER={owner.address}")
    lines.append(f"DEPLOYER_KEY={owner.key.hex()}")
    lines.append("")
    for prefix, acct in (("SENTINEL_STRATEGY", roles["SENTINEL_STRATEGY"]), ("SENTINEL_RISK", roles["SENTINEL_RISK"])):
        lines.append(f"{prefix}_ADDR={acct.address}")
        lines.append(f"{prefix}_KEY={acct.key.hex()}")
    lines.append("")
    lines.append("# filled in after deployment")
    lines.append("SENTINEL_VAULT=")

    fd = os.open(TARGET, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(TARGET, 0o600)

    addrs = [acct.address for acct in roles.values()]
    assert len(set(addrs)) == 3, "generated addresses collided"
    print(f"wrote {TARGET} (mode 600, git-ignored)")
    for prefix, acct in roles.items():
        print(f"{prefix:18s} {acct.address}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
