#!/usr/bin/env python3
"""JOLT — reference MCP server.

A dependency-free Python 3 MCP (Model Context Protocol) server over stdio
that helps an AI agent take part in the JOLT claim season on BNB Smart
Chain (chainId 56): read the live season state, fetch its daily CORE RUSH
board, solve the route puzzle, submit the solution for a signed claim
voucher, and build the UNSIGNED claim transaction.

Tools:
  - season_state   : live on-chain reads (opened, totalClaims,
                     totalDistributed, poolReserve, projectAccrued,
                     launched, pair, season window) + the game API state.
  - wallet_status  : claimsOf / claimsTodayOf / hasAgentId for a wallet,
                     plus its next ladder payout.
  - get_board      : the wallet's official daily board from the game API.
  - solve_board    : fetch the board and solve it locally (shortest route
                     through all 5 cores, storm cells blocked) — returns
                     the move list; does NOT submit anything.
  - submit_solution: POST a move list to the game API for replay
                     validation; on a win returns the signed voucher.
  - claim_calldata : build the UNSIGNED claim(nonce, expiry, signature)
                     transaction {to, value, data} for a voucher, with
                     live cap/gate re-checks.

This server NEVER holds keys and NEVER signs or sends transactions. It
only reads public state, solves the puzzle locally, and talks to the
public game API. The calling agent signs and submits any transaction
with its own wallet.

Token:   0x7A3cD992e6aeA95C684d71d1f33511ff1206d9E6 (JOLT)
Claim:   0x63C9d73f09bb7CCD8E363510676Bd9ecD563f8e4
Guide:   https://jolt-2fe.pages.dev/skill.md
"""

import json
import sys
import urllib.request
from collections import deque

# --------------------------------------------------------------------------
# Constants (from the deployed, source-verified JoltClaim contract)
# --------------------------------------------------------------------------

TOKEN_ADDRESS = "0x7A3cD992e6aeA95C684d71d1f33511ff1206d9E6"
CLAIM_ADDRESS = "0x63C9d73f09bb7CCD8E363510676Bd9ecD563f8e4"
CHAIN_ID = 56
CLAIM_FEE_WEI = 200_000_000_000_000  # 0.0002 BNB per claim
MAX_CLAIMS = 6
MAX_PER_DAY = 2
LADDER = [8_000, 9_000, 11_000, 13_000, 16_000, 19_000]
COMPLETION_BONUS = 24_000
AGENT_REGISTRY = "0x60B9262456dE011F7d15a54CdD8c48bAaa8B022F"
GAME_API = "https://jolt-voucher.oagenthq.workers.dev"
SITE = "https://jolt-2fe.pages.dev/"

RPC_URLS = [
    "https://bsc-dataseed.binance.org",
    "https://bsc-rpc.publicnode.com",
    "https://bsc-dataseed1.defibit.io",
]
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

SERVER_NAME = "jolt"
SERVER_VERSION = "1.0.0"
DEFAULT_PROTOCOL_VERSION = "2024-11-05"

# --------------------------------------------------------------------------
# Keccak-256 (Ethereum variant — original Keccak padding 0x01, NOT the
# NIST SHA3-256 padding 0x06 that hashlib.sha3_256 uses). Pure Python.
#
# Self-test: keccak256(b"") must equal
#   c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470
# --------------------------------------------------------------------------

_MASK64 = (1 << 64) - 1

_KECCAK_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A,
    0x8000000080008000, 0x000000000000808B, 0x0000000080000001,
    0x8000000080008081, 0x8000000000008009, 0x000000000000008A,
    0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
    0x8000000000008003, 0x8000000000008002, 0x8000000000000080,
    0x000000000000800A, 0x800000008000000A, 0x8000000080008081,
    0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]

# Rotation offsets r[x][y] for the rho step.
_KECCAK_ROT = [
    [0, 36, 3, 41, 18],
    [1, 44, 10, 45, 2],
    [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56],
    [27, 20, 39, 8, 14],
]


def _rotl64(value, shift):
    if shift == 0:
        return value & _MASK64
    return ((value << shift) | (value >> (64 - shift))) & _MASK64


def _keccak_f1600(state):
    """One Keccak-f[1600] permutation. `state` is a list of 25 lanes,
    indexed x + 5*y, mutated in place."""
    for rc in _KECCAK_RC:
        # theta
        c = [state[x] ^ state[x + 5] ^ state[x + 10] ^ state[x + 15]
             ^ state[x + 20] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rotl64(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            dx = d[x]
            for y in range(5):
                state[x + 5 * y] ^= dx
        # rho + pi
        b = [0] * 25
        for x in range(5):
            for y in range(5):
                b[y + 5 * ((2 * x + 3 * y) % 5)] = _rotl64(
                    state[x + 5 * y], _KECCAK_ROT[x][y])
        # chi
        for x in range(5):
            for y in range(5):
                state[x + 5 * y] = (
                    b[x + 5 * y]
                    ^ ((~b[(x + 1) % 5 + 5 * y]) & b[(x + 2) % 5 + 5 * y])
                )
        # iota
        state[0] ^= rc


def keccak256(data: bytes) -> bytes:
    """Ethereum Keccak-256 of `data` (rate 136 bytes, capacity 512)."""
    rate = 136
    padded = bytearray(data)
    padded.append(0x01)                      # Keccak domain suffix bit
    while len(padded) % rate != 0:
        padded.append(0x00)
    padded[-1] |= 0x80                       # final pad10*1 bit
    state = [0] * 25
    for offset in range(0, len(padded), rate):
        block = padded[offset:offset + rate]
        for lane in range(rate // 8):
            state[lane] ^= int.from_bytes(
                block[lane * 8:(lane + 1) * 8], "little")
        _keccak_f1600(state)
    out = bytearray()
    for lane in range(4):                    # 4 lanes = 32 bytes
        out += state[lane].to_bytes(8, "little")
    return bytes(out[:32])


assert keccak256(b"").hex() == (
    "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
), "Keccak-256 self-test failed"


def _selector(signature: str) -> bytes:
    return keccak256(signature.encode("ascii"))[:4]


# Selector cross-checks against the deployed contract (verified live).
assert _selector("totalClaims()").hex() == "41c61383"
assert _selector("launched()").hex() == "8091f3bf"
assert _selector("claimsTodayOf(address)").hex() == "01bacc42"
assert _selector("claim(uint256,uint256,bytes)").hex() == "5eddd157"

# --------------------------------------------------------------------------
# Minimal ABI helpers
# --------------------------------------------------------------------------


def _encode_uint256(value: int) -> bytes:
    return value.to_bytes(32, "big")


def _encode_address(address: str) -> bytes:
    return bytes.fromhex(address[2:].lower()).rjust(32, b"\x00")


def _parse_address(address):
    if (not isinstance(address, str) or len(address) != 42
            or not address.startswith("0x")):
        raise ValueError("wallet must be a 0x-prefixed 20-byte address")
    try:
        return bytes.fromhex(address[2:])
    except ValueError:
        raise ValueError("wallet must be a 0x-prefixed 20-byte address")


# --------------------------------------------------------------------------
# Public BSC RPC reads (eth_call, first healthy endpoint)
# --------------------------------------------------------------------------


def _rpc(method, params):
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode("utf-8")
    last_error = None
    for url in RPC_URLS:
        try:
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json",
                         "User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            if "error" in body:
                raise RuntimeError("RPC error: %s" % body["error"])
            return body["result"]
        except Exception as exc:  # try the next endpoint
            last_error = exc
    raise RuntimeError("all BSC RPC endpoints failed: %s" % last_error)


def _eth_call(to, data: bytes) -> bytes:
    result = _rpc("eth_call", [{"to": to, "data": "0x" + data.hex()},
                               "latest"])
    return bytes.fromhex(result[2:])


def _claim_uint(signature, args=b""):
    return int.from_bytes(
        _eth_call(CLAIM_ADDRESS, _selector(signature) + args), "big")


def _claim_address(signature):
    raw = _eth_call(CLAIM_ADDRESS, _selector(signature))
    return "0x" + raw[-20:].hex()


# --------------------------------------------------------------------------
# Game API (public HTTPS)
# --------------------------------------------------------------------------


def _api(method, path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        GAME_API + path, data=data, method=method,
        headers={"Content-Type": "application/json",
                 "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


# --------------------------------------------------------------------------
# CORE RUSH solver — BFS over (x, y, collected-cores bitmask); the board
# is 8x8 with 5 cores, so the state space is tiny. Returns the shortest
# move list that steps onto every core without crossing a storm cell.
# --------------------------------------------------------------------------

_DIRS = {"U": (0, -1), "D": (0, 1), "L": (-1, 0), "R": (1, 0)}


def solve(board):
    cols = board.get("cols", 8)
    rows = board.get("rows", 8)
    start = tuple(board["start"])
    cores = [tuple(c) for c in board["cores"]]
    storms = {tuple(s) for s in board["storms"]}
    budget = board.get("budget", 48)
    core_index = {cell: i for i, cell in enumerate(cores)}
    full = (1 << len(cores)) - 1

    start_mask = 0
    if start in core_index:
        start_mask |= 1 << core_index[start]
    queue = deque()
    queue.append((start[0], start[1], start_mask))
    parent = {(start[0], start[1], start_mask): None}
    move_used = {}
    while queue:
        x, y, mask = queue.popleft()
        depth = move_used.get((x, y, mask), 0)
        if mask == full:
            moves = []
            node = (x, y, mask)
            while parent[node] is not None:
                prev, step = parent[node]
                moves.append(step)
                node = prev
            moves.reverse()
            return moves
        if depth >= budget:
            continue
        for step, (dx, dy) in _DIRS.items():
            nx, ny = x + dx, y + dy
            if not (0 <= nx < cols and 0 <= ny < rows):
                continue
            if (nx, ny) in storms:
                continue
            nmask = mask
            if (nx, ny) in core_index:
                nmask |= 1 << core_index[(nx, ny)]
            node = (nx, ny, nmask)
            if node in parent:
                continue
            parent[node] = ((x, y, mask), step)
            move_used[node] = depth + 1
            queue.append(node)
    return None


def replay(board, moves):
    """Local replay of a move list, mirroring the server's validation."""
    x, y = board["start"]
    storms = {tuple(s) for s in board["storms"]}
    cores = {tuple(c) for c in board["cores"]}
    collected = set()
    if (x, y) in cores:
        collected.add((x, y))
    for step in moves:
        dx, dy = _DIRS[step]
        x, y = x + dx, y + dy
        if not (0 <= x < board.get("cols", 8)
                and 0 <= y < board.get("rows", 8)):
            return False, "route leaves the board"
        if (x, y) in storms:
            return False, "route crosses a storm cell"
        if (x, y) in cores:
            collected.add((x, y))
    if len(collected) != len(cores):
        return False, "route misses %d core(s)" % (len(cores) - len(collected))
    if len(moves) > board.get("budget", 48):
        return False, "route exceeds the move budget"
    return True, "ok"


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


def _ladder_next(claims_done):
    """JOLT paid by the NEXT claim for a wallet with `claims_done` claims."""
    if claims_done >= MAX_CLAIMS:
        return 0
    amount = LADDER[claims_done]
    if claims_done == MAX_CLAIMS - 1:
        amount += COMPLETION_BONUS
    return amount


def tool_season_state(_args):
    opened = _claim_uint("opened()") != 0
    launched = _claim_uint("launched()") != 0
    total_claims = _claim_uint("totalClaims()")
    distributed = _claim_uint("totalDistributed()") // 10 ** 18
    pool_wei = _claim_uint("poolReserve()")
    project_wei = _claim_uint("projectAccrued()")
    start = _claim_uint("startTime()")
    ends = _claim_uint("endsAt()")
    pair = _claim_address("pair()")
    try:
        api_state = _api("GET", "/api/state")
    except Exception:
        api_state = None
    return {
        "name": "JOLT",
        "token": TOKEN_ADDRESS,
        "claim_contract": CLAIM_ADDRESS,
        "chain": "BNB Smart Chain",
        "chain_id": CHAIN_ID,
        "season_open": opened,
        "launched": launched,
        "pair": None if int(pair, 16) == 0 else pair,
        "season_window_unix": {"start": start, "ends": ends},
        "total_claims": total_claims,
        "total_distributed_jolt": distributed,
        "pool_reserve_wei": str(pool_wei),
        "pool_reserve_bnb": pool_wei / 1e18,
        "project_accrued_wei": str(project_wei),
        "project_accrued_bnb": project_wei / 1e18,
        "claim_fee_wei": str(CLAIM_FEE_WEI),
        "max_claims_per_wallet": MAX_CLAIMS,
        "max_claims_per_day": MAX_PER_DAY,
        "game_api_state": api_state,
        "guide": SITE + "skill.md",
        "note": (
            "All figures are read live from the claim contract and are "
            "the only authority. The pool reserve can leave the contract "
            "only through launch() after the season ends."
        ),
    }


def tool_wallet_status(args):
    wallet = args.get("wallet")
    _parse_address(wallet)
    arg = _encode_address(wallet)
    claims = _claim_uint("claimsOf(address)", arg)
    today = _claim_uint("claimsTodayOf(address)", arg)
    has_id = _claim_uint("hasAgentId(address)", arg) != 0
    balance = int.from_bytes(_eth_call(
        TOKEN_ADDRESS, _selector("balanceOf(address)") + arg), "big")
    return {
        "wallet": wallet,
        "has_agent_id": has_id,
        "claims_total": claims,
        "claims_today": today,
        "claims_remaining_today": max(0, MAX_PER_DAY - today),
        "claims_remaining_season": max(0, MAX_CLAIMS - claims),
        "next_claim_pays_jolt": _ladder_next(claims),
        "jolt_balance": balance // 10 ** 18,
        "note": (
            "A claim needs has_agent_id true, an unused signed voucher, "
            "and exactly 0.0002 BNB + gas in this wallet."
        ),
    }


def tool_get_board(args):
    wallet = args.get("wallet")
    _parse_address(wallet)
    board = _api("GET", "/api/board?wallet=%s" % wallet)
    return board


def tool_solve_board(args):
    wallet = args.get("wallet")
    _parse_address(wallet)
    resp = _api("GET", "/api/board?wallet=%s" % wallet)
    board = resp.get("board")
    if not board:
        return {
            "solved": False,
            "reason": resp.get("reason", "no board available"),
            "api_response": resp,
            "note": "New boards open at 00:00 UTC.",
        }
    moves = solve(board)
    if moves is None:
        return {"solved": False, "reason": "no route within budget",
                "board": board}
    ok, why = replay(board, moves)
    if not ok:
        raise RuntimeError("internal error: solution failed replay: %s" % why)
    return {
        "solved": True,
        "board_id": board.get("boardId"),
        "slot": resp.get("slot"),
        "moves": moves,
        "moves_used": len(moves),
        "budget": board.get("budget"),
        "note": (
            "Shortest route found locally and re-verified by replay. "
            "Call submit_solution with this wallet and these moves to "
            "earn the signed voucher."
        ),
    }


def tool_submit_solution(args):
    wallet = args.get("wallet")
    _parse_address(wallet)
    moves = args.get("moves")
    if (not isinstance(moves, list) or not moves
            or any(m not in _DIRS for m in moves)):
        raise ValueError(
            'moves must be a non-empty list of "U"|"D"|"L"|"R" steps')
    resp = _api("POST", "/api/submit", {"wallet": wallet, "moves": moves})
    return resp


def tool_claim_calldata(args):
    wallet = args.get("wallet")
    _parse_address(wallet)
    nonce = args.get("nonce")
    expiry = args.get("expiry")
    signature = args.get("signature")
    if isinstance(nonce, str) and nonce.isdigit():
        nonce = int(nonce)
    if not isinstance(nonce, int) or nonce < 0:
        raise ValueError("nonce must be the voucher's uint256 nonce")
    if not isinstance(expiry, int) or expiry <= 0:
        raise ValueError("expiry must be the voucher's unix expiry")
    if (not isinstance(signature, str) or not signature.startswith("0x")):
        raise ValueError("signature must be a 0x-prefixed hex string")
    try:
        sig = bytes.fromhex(signature[2:])
    except ValueError:
        raise ValueError("signature must be a 0x-prefixed hex string")
    if len(sig) != 65:
        raise ValueError("signature must be 65 bytes (r|s|v)")
    arg = _encode_address(wallet)
    claims = _claim_uint("claimsOf(address)", arg)
    today = _claim_uint("claimsTodayOf(address)", arg)
    has_id = _claim_uint("hasAgentId(address)", arg) != 0
    # usedVouchers is keyed by the voucher digest, exactly as the
    # contract computes it: keccak256(abi.encodePacked(
    #   wallet, nonce, expiry, address(this), block.chainid))
    digest = keccak256(
        _parse_address(wallet)
        + _encode_uint256(nonce)
        + _encode_uint256(expiry)
        + bytes.fromhex(CLAIM_ADDRESS[2:])
        + _encode_uint256(CHAIN_ID))
    used = _claim_uint("usedVouchers(bytes32)", digest) != 0
    data = (
        _selector("claim(uint256,uint256,bytes)")
        + _encode_uint256(nonce)
        + _encode_uint256(expiry)
        + _encode_uint256(96)          # offset of the bytes argument
        + _encode_uint256(len(sig))
        + sig + b"\x00" * ((32 - len(sig) % 32) % 32)
    )
    return {
        "unsigned_transaction": {
            "to": CLAIM_ADDRESS,
            "chain_id": CHAIN_ID,
            "value_wei": str(CLAIM_FEE_WEI),
            "value_bnb": CLAIM_FEE_WEI / 1e18,
            "data": "0x" + data.hex(),
        },
        "pays_jolt": _ladder_next(claims),
        "live_checks": {
            "has_agent_id": has_id,
            "claims_total": claims,
            "claims_today": today,
            "voucher_already_used": used,
        },
        "warning": (
            "UNSIGNED transaction parameters only. This server never "
            "holds keys and never signs: the calling agent signs with "
            "its OWN wallet and submits. The claim reverts if the "
            "voucher is expired or already used, the wallet holds no "
            "Agent ID, the daily (2) or season (6) cap is reached, or "
            "the value is not exactly 0.0002 BNB. Re-check the live "
            "checks above before submitting, and show the preview to "
            "your operator where your setup requires approval."
        ),
    }


TOOLS = [
    {
        "name": "season_state",
        "description": (
            "Live state of the JOLT claim season (BNB Smart Chain), "
            "read from the claim contract at call time: whether the "
            "season is open, total claims, JOLT distributed, the locked "
            "pool reserve, project fees accrued, the season window, "
            "whether launch() has happened and the pair address, plus "
            "the game API's voucher state."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_season_state,
    },
    {
        "name": "wallet_status",
        "description": (
            "A wallet's JOLT season standing, read live on-chain: "
            "Agent ID gate, claims used (season + today), claims "
            "remaining, the JOLT the next claim would pay, and the "
            "wallet's current JOLT balance."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "wallet": {
                    "type": "string",
                    "description": "The 0x wallet address to inspect.",
                },
            },
            "required": ["wallet"],
        },
        "handler": tool_wallet_status,
    },
    {
        "name": "get_board",
        "description": (
            "The wallet's official CORE RUSH board for today from the "
            "game API: start cell, 5 core cells, storm cells and the "
            "move budget, plus the current voucher slot. Returns "
            "board: null once today's 2 vouchers are earned."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "wallet": {
                    "type": "string",
                    "description": "The claiming wallet's 0x address.",
                },
            },
            "required": ["wallet"],
        },
        "handler": tool_get_board,
    },
    {
        "name": "solve_board",
        "description": (
            "Fetch the wallet's daily board and solve it locally: the "
            "shortest route that steps onto all 5 cores without "
            "crossing a storm cell, within the move budget. The "
            "solution is verified by a local replay before it is "
            "returned. Does NOT submit anything — call "
            "submit_solution to earn the voucher."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "wallet": {
                    "type": "string",
                    "description": "The claiming wallet's 0x address.",
                },
            },
            "required": ["wallet"],
        },
        "handler": tool_solve_board,
    },
    {
        "name": "submit_solution",
        "description": (
            "Submit a CORE RUSH move list to the game API. The server "
            "replays every step; a valid route earns a signed, "
            "single-use claim voucher bound to the wallet (or "
            "voucher: null once today's 2 vouchers are earned). "
            "Submissions are free and unlimited."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "wallet": {
                    "type": "string",
                    "description": "The claiming wallet's 0x address.",
                },
                "moves": {
                    "type": "array",
                    "items": {"type": "string",
                              "enum": ["U", "D", "L", "R"]},
                    "description": (
                        "One entry per step, from the start cell."
                    ),
                },
            },
            "required": ["wallet", "moves"],
        },
        "handler": tool_submit_solution,
    },
    {
        "name": "claim_calldata",
        "description": (
            "Build the UNSIGNED claim transaction for a voucher: "
            "{to, value (exactly 0.0002 BNB), data} for "
            "claim(nonce, expiry, signature) on the JOLT claim "
            "contract, with live re-checks (Agent ID, caps, voucher "
            "unused) and the JOLT the claim would pay. This server "
            "never holds keys and never signs or sends anything — "
            "the calling agent signs with its own wallet."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "wallet": {
                    "type": "string",
                    "description": (
                        "The wallet the voucher was issued to."
                    ),
                },
                "nonce": {
                    "type": "integer",
                    "description": "The voucher's nonce (uint256).",
                },
                "expiry": {
                    "type": "integer",
                    "description": "The voucher's unix expiry.",
                },
                "signature": {
                    "type": "string",
                    "description": (
                        "The voucher's 65-byte signature, 0x hex."
                    ),
                },
            },
            "required": ["wallet", "nonce", "expiry", "signature"],
        },
        "handler": tool_claim_calldata,
    },
]

_TOOL_MAP = {tool["name"]: tool for tool in TOOLS}


# --------------------------------------------------------------------------
# MCP / JSON-RPC plumbing (stdio, newline-delimited JSON)
# --------------------------------------------------------------------------

def _result_response(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error_response(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": code, "message": message}}


def handle_request(request):
    """Returns a response dict, or None for notifications."""
    method = request.get("method")
    req_id = request.get("id")
    params = request.get("params") or {}

    if method == "initialize":
        protocol = params.get("protocolVersion", DEFAULT_PROTOCOL_VERSION)
        return _result_response(req_id, {
            "protocolVersion": protocol,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method == "ping":
        return _result_response(req_id, {})
    if method == "tools/list":
        return _result_response(req_id, {
            "tools": [
                {key: tool[key]
                 for key in ("name", "description", "inputSchema")}
                for tool in TOOLS
            ],
        })
    if method == "tools/call":
        name = params.get("name")
        tool = _TOOL_MAP.get(name)
        if tool is None:
            return _error_response(req_id, -32602, "unknown tool: %s" % name)
        try:
            result = tool["handler"](params.get("arguments") or {})
            text = json.dumps(result, indent=2)
            return _result_response(req_id, {
                "content": [{"type": "text", "text": text}],
                "isError": False,
            })
        except Exception as exc:
            return _result_response(req_id, {
                "content": [{"type": "text",
                             "text": "%s: %s" % (type(exc).__name__, exc)}],
                "isError": True,
            })
    if req_id is None:
        return None  # notification we do not handle: ignore
    return _error_response(req_id, -32601, "method not found: %s" % method)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle_request(request)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
