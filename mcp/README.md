# JOLT — MCP server

A dependency-free Python 3 [MCP](https://modelcontextprotocol.io) server
(stdio transport) for the JOLT claim season on BNB Smart Chain. It reads
live season state, checks a wallet's standing, fetches and solves the
daily CORE RUSH route puzzle, submits solutions to the game API for
signed claim vouchers, and builds the unsigned claim transaction.

It **never holds keys and never signs or sends transactions** — the
calling agent signs with its own wallet. See
[`../skill.md`](../skill.md) for the full claim procedure and rules.

## Run

Requires Python 3.8+ and nothing else (standard library only; it ships
its own pure-Python Keccak-256 — Ethereum Keccak, not NIST SHA3).

```bash
python3 mcp/jolt_mcp.py
```

## Tools

| Tool | What it does |
| --- | --- |
| `season_state` | Live on-chain season state: open/launched, total claims, JOLT distributed, locked pool reserve, season window, pair. |
| `wallet_status` | A wallet's Agent ID gate, claims used (season + today), next claim payout, JOLT balance. |
| `get_board` | The wallet's official daily CORE RUSH board from the game API. |
| `solve_board` | Fetch the board and solve it locally (shortest route, replay-verified). Does not submit. |
| `submit_solution` | Submit a move list; a valid route earns the signed claim voucher. |
| `claim_calldata` | Build the unsigned `claim(nonce, expiry, signature)` transaction (exactly 0.0002 BNB) with live cap/gate re-checks. |

Contracts (BNB Smart Chain, chainId 56), both source-verified:

- JOLT token: `0x7A3cD992e6aeA95C684d71d1f33511ff1206d9E6`
- Claim: `0x63C9d73f09bb7CCD8E363510676Bd9ecD563f8e4`
