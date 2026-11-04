"""API-key 칸이 「mcpOAuth 만 든 OAuth 키체인 항목」에 덮이는 결함 재현.

macOS 에서 API 키 계정이 켜져 있으면 키는 Keychain "Claude Code" 에 있고,
"Claude Code-credentials" 에는 Claude Code 가 쓰는 MCP 로그인(mcpOAuth)만 남는다.
그 상태에서 다른 계정으로 넘어가면 cswap 이 mcpOAuth 묶음을 「지금 자격증명」으로 읽어
API 키 칸에 백업한다 — 키가 사라지고 칸이 `no credentials` 가 된다(2026-11-04 실측 두 번).
"""
from __future__ import annotations

import json

from claude_swap import macos_keychain
from claude_swap.credentials import (
    CLAUDE_CODE_KEYCHAIN_SERVICE,
    CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE,
)
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher

PERSONAL_KEY = "sk-ant-usr-" + "u1v2w3x4y5" * 9
MCP_ONLY = json.dumps({"mcpOAuth": {"server|abc": {"accessToken": "m", "refreshToken": "mr"}}})


def _macos_switcher() -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s.platform = Platform.MACOS
    s._setup_directories()
    s._init_sequence_file()
    return s


def test_active_read_prefers_managed_key_over_mcp_only_blob(temp_home, block_real_keychain):
    store = block_real_keychain
    s = _macos_switcher()
    acct = macos_keychain.keychain_account_name()
    store.set_password(CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE, acct, PERSONAL_KEY)
    store.set_password(CLAUDE_CODE_KEYCHAIN_SERVICE, acct, MCP_ONLY)
    assert s._read_credentials() == PERSONAL_KEY


SETUP_TOKEN = "sk-ant-oat01-" + "o9p8q7r6s5" * 4


def test_switching_away_from_api_key_keeps_the_key(temp_home, block_real_keychain):
    store = block_real_keychain
    s = _macos_switcher()
    s.add_account_from_token(SETUP_TOKEN, slot=1)
    s.add_account_from_token(PERSONAL_KEY, slot=4)
    s.switch_to("4", json_output=True)
    acct = macos_keychain.keychain_account_name()
    assert store.get_password(CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE, acct) == PERSONAL_KEY
    # Claude Code 가 API 키 모드에서도 MCP 로그인을 OAuth 항목에 남긴다.
    store.set_password(CLAUDE_CODE_KEYCHAIN_SERVICE, acct, MCP_ONLY)
    s.switch_to("1", json_output=True)
    assert s._read_account_credentials("4", "api-key-4@token.local") == PERSONAL_KEY
