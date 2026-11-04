"""Exercise actual subprocesses and CLI output without touching real accounts."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import textwrap
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import agent_usage
from claude_swap.agent_usage import read_other_agents
from claude_swap.cli import main
from claude_swap.switcher import ClaudeAccountSwitcher


LIMITS = {
    "primary": {"usedPercent": 0, "resetsAt": 1793770000},
    "secondary": {"usedPercent": 67.5, "resetsAt": 1794000000},
    "credits": {"balance": "12.50", "hasCredits": True},
    "planType": "plus",
}


@pytest.fixture(autouse=True)
def missing_agents(monkeypatch, tmp_path):
    for name in ("CODEX", "MUSE"):
        monkeypatch.setenv(f"ASWAP_{name}_BIN", str(tmp_path / f"missing-{name}"))


@pytest.fixture
def executable(tmp_path, monkeypatch):
    def create(agent: str, script: str) -> Path:
        binary = tmp_path / f"fake {agent}"
        binary.write_text(f"#!{sys.executable}\n" + textwrap.dedent(script))
        binary.chmod(0o755)
        monkeypatch.setenv(f"ASWAP_{agent.upper()}_BIN", str(binary))
        return binary
    return create


def codex_script(limits=LIMITS, *, before="", after="") -> str:
    # Assert the real wire order and IDs, rather than mocking subprocess I/O.
    return f"""
import json, sys
assert sys.argv[1:] == ['app-server']
{before}
request = json.loads(sys.stdin.readline())
assert request == {{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {{'clientInfo': {{'name': 'agent-swap', 'version': '0.1.0'}}}}}}
print(json.dumps({{'method': 'notification', 'params': {{}}}}), flush=True)
print(json.dumps({{'id': 1, 'result': {{'userAgent': 'fake'}}}}), flush=True)
assert json.loads(sys.stdin.readline()) == {{'jsonrpc': '2.0', 'method': 'initialized'}}
assert json.loads(sys.stdin.readline()) == {{'jsonrpc': '2.0', 'id': 2, 'method': 'account/rateLimits/read'}}
print(json.dumps({{'id': 2, 'result': {{'rateLimits': {limits!r}}}}}), flush=True)
{after}
"""


def test_codex_handshake_and_complete_reading(executable):
    executable("codex", codex_script(after="import time; time.sleep(60)"))
    result = agent_usage.read_codex_usage()
    assert result == {"status": "ok", **LIMITS}
    assert agent_usage.format_other_agents({"codex": result}) == [
        "ChatGPT Codex · 5h 0% · 주간 67.5% · 크레딧 12.50",
    ]


def test_missing_codex_is_unavailable_and_missing_muse_is_omitted():
    result = read_other_agents()
    assert result == {"codex": {"status": "unavailable", "reason": "실행 파일 없음"}}
    assert "못 읽음(실행 파일 없음)" in agent_usage.format_other_agents(result)[0]
    assert "0%" not in agent_usage.format_other_agents(result)[0]


def test_readers_run_concurrently_and_isolate_unexpected_errors(monkeypatch):
    barrier = threading.Barrier(2)

    def codex():
        barrier.wait(timeout=2)
        raise RecursionError("nested response")

    def muse():
        barrier.wait(timeout=2)
        return {"status": "ok", "allowance": None, "reason": "no_allowance"}

    monkeypatch.setattr(agent_usage, "read_codex_usage", codex)
    monkeypatch.setattr(agent_usage, "read_muse_usage", muse)
    result = read_other_agents()
    assert result["codex"]["status"] == "unavailable"
    assert "RecursionError" in result["codex"]["reason"]
    assert result["muse"]["status"] == "ok"


def test_codex_over_limit_percentage_is_still_readable(executable):
    executable("codex", codex_script({**LIMITS, "primary": {"usedPercent": 101, "resetsAt": None}}))
    result = agent_usage.read_codex_usage()
    assert result["status"] == "ok"
    assert "5h 101%" in agent_usage.format_other_agents({"codex": result})[0]


def test_muse_terminal_control_characters_are_removed():
    lines = agent_usage.format_other_agents({"muse": {
        "status": "ok", "allowance": {"quota": "\u009b31m\u202etext"},
    }})
    assert all(character.isprintable() for character in lines[0])


@pytest.mark.parametrize("script,reason", [
    ("print('not json', flush=True)", "JSONDecodeError"),
    ("print('[]', flush=True)", "응답 형식 오류"),
    ("print('{\"id\":1,\"error\":{\"code\":-1}}', flush=True)", "JSON-RPC 오류"),
    ("print('{\"id\":1,\"result\":null}', flush=True)", "initialize 응답"),
    ("pass", "응답 없이"),
    ("import sys; sys.stdout.buffer.write(b'\\xff\\n')", "UnicodeDecodeError"),
])
def test_codex_malformed_or_failed_response(executable, script, reason):
    executable("codex", script)
    result = agent_usage.read_codex_usage()
    assert result["status"] == "unavailable"
    assert reason in result["reason"]


@pytest.mark.parametrize("limits", [
    None, {}, {"primary": []}, {"primary": {"usedPercent": True}},
    {"primary": {"usedPercent": "0"}},
    {"primary": {"usedPercent": float("nan")}},
    {"primary": {"usedPercent": 20, "resetsAt": "tomorrow"}},
    {**LIMITS, "credits": []}, {**LIMITS, "planType": []},
])
def test_codex_rejects_invalid_limits(executable, limits):
    # repr(nan) is not Python source, so use JSON to round-trip these fixtures.
    script = codex_script().replace(repr(LIMITS), f"json.loads({json.dumps(limits)!r})")
    executable("codex", script)
    assert agent_usage.read_codex_usage()["status"] == "unavailable"


def test_codex_partial_limit_and_unknown_credits(executable):
    executable("codex", codex_script({"secondary": LIMITS["secondary"]}))
    result = agent_usage.read_codex_usage()
    assert result["primary"] is None
    assert result["credits"] == {"balance": None, "hasCredits": None}
    line = agent_usage.format_other_agents({"codex": result})[0]
    assert "5h 미제공" in line
    assert "크레딧 미제공" in line


def test_codex_stderr_cannot_block_protocol(executable):
    executable("codex", codex_script(before="sys.stderr.write('x' * 200000)"))
    assert agent_usage.read_codex_usage()["status"] == "ok"


def test_codex_partial_line_times_out(executable):
    executable("codex", "import sys, time; sys.stdout.write('{'); sys.stdout.flush(); time.sleep(60)")
    started = time.monotonic()
    assert agent_usage.read_codex_usage(timeout=0.2) == {
        "status": "unavailable", "reason": "시간 초과",
    }
    assert time.monotonic() - started < 3


def test_codex_oversized_response(executable, monkeypatch):
    monkeypatch.setattr(agent_usage, "MAX_RESPONSE_BYTES", 100)
    executable("codex", "print('x' * 101, flush=True)")
    assert agent_usage.read_codex_usage()["reason"] == "응답 크기 초과"


@pytest.mark.parametrize("allowance,reason,expected", [
    (None, "no_allowance", "Muse · 구독 한도 없음 (no_allowance)"),
    ({"used": 12, "limit": 100}, None, "Muse · used:12 · limit:100"),
    ({"nested": {"remaining": 0}, "name": "구독"}, None,
     'Muse · nested:{"remaining": 0} · name:"구독"'),
    ({}, None, "Muse · 구독 한도 정보 없음"),
])
def test_muse_allowance(executable, allowance, reason, expected):
    response = {"schemaVersion": 1, "allowance": allowance, "reason": reason}
    executable("muse", f"""
        import sys
        assert sys.argv[1:] == ['usage', '--json']
        print({json.dumps(response)!r})
    """)
    result = agent_usage.read_muse_usage()
    assert result == {"status": "ok", "allowance": allowance, "reason": reason}
    assert agent_usage.format_other_agents({"muse": result}) == [expected]


@pytest.mark.parametrize("response", ["bad", "[]", "{}", '{"schemaVersion":1,"allowance":7}'])
def test_muse_invalid_json_or_shape(executable, response):
    executable("muse", f"print({response!r})")
    assert agent_usage.read_muse_usage()["status"] == "unavailable"


def test_muse_failed_command(executable):
    executable("muse", "raise SystemExit(7)")
    assert agent_usage.read_muse_usage() == {"status": "unavailable", "reason": "종료 코드 7"}


@pytest.mark.parametrize("agent", ["codex", "muse"])
@pytest.mark.parametrize("succeeds", [False, True])
def test_process_group_cleanup(executable, tmp_path, agent, succeeds):
    pid_file = tmp_path / "descendant.pid"
    spawn = (
        "import subprocess, pathlib, time\n"
        f"child = subprocess.Popen([{sys.executable!r}, '-c', 'import time; time.sleep(60)'], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\n"
    )
    if succeeds and agent == "codex":
        script = codex_script(before=spawn, after="time.sleep(60)")
    elif succeeds:
        script = spawn + "print('{\"schemaVersion\":1,\"allowance\":null}', flush=True)"
    else:
        script = spawn + "time.sleep(60)"
    executable(agent, script)
    reader = getattr(agent_usage, f"read_{agent}_usage")
    result = reader(timeout=5)
    assert result["status"] == ("ok" if succeeds else "unavailable"), result
    if not succeeds:
        assert result["reason"] == "시간 초과"
    child_pid = int(pid_file.read_text())
    # Some CI init processes briefly retain killed children as zombies.
    deadline = time.monotonic() + 2
    while True:
        probe = subprocess.run(["ps", "-p", str(child_pid), "-o", "stat="], capture_output=True, text=True)
        if probe.returncode != 0 or "Z" in probe.stdout:
            break
        assert time.monotonic() < deadline, f"agent helper {child_pid} survived cleanup"
        time.sleep(0.02)


@pytest.mark.parametrize("agent", ["codex", "muse"])
def test_unlaunchable_executable_is_unavailable(executable, agent):
    binary = executable(agent, "pass")
    binary.write_text("not an executable format")
    result = getattr(agent_usage, f"read_{agent}_usage")()
    assert result["status"] == "unavailable"
    assert "OSError" in result["reason"]


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("has_accounts", [False, True])
def test_list_appends_other_agents(executable, monkeypatch, temp_home, capsys, json_output, has_accounts):
    executable("codex", codex_script())
    executable("muse", "print('{\"schemaVersion\":1,\"allowance\":null,\"reason\":\"no_allowance\"}')")
    monkeypatch.setattr(agent_usage, "read_other_agents", read_other_agents)
    switcher = ClaudeAccountSwitcher()
    if has_accounts:
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, {"accounts": {}, "sequence": []})
    with patch.object(switcher, "_first_run_setup"), \
         patch("claude_swap.switcher.get_running_instances", return_value=([], [])), \
         patch.object(switcher, "_build_accounts_info", return_value=[]), \
         patch.object(switcher, "_collect_usage_entries", return_value={}), \
         patch.object(switcher, "_plans_for_pass", return_value={}):
        result = switcher.list_accounts(json_output=json_output)
    output = capsys.readouterr().out
    if json_output:
        assert output == ""
        assert result["otherAgents"]["codex"]["primary"] == LIMITS["primary"]
        assert result["otherAgents"]["muse"]["allowance"] is None
    else:
        assert result is None
        assert output.endswith(
            "Other agents:\n"
            "  ChatGPT Codex · 5h 0% · 주간 67.5% · 크레딧 12.50\n"
            "  Muse · 구독 한도 없음 (no_allowance)\n"
        )


@pytest.mark.parametrize("command", ["aswap", "cswap", "agent-swap", "claude-swap"])
def test_cli_json_has_other_agents(executable, monkeypatch, temp_home, capsys, command):
    executable("codex", codex_script())
    monkeypatch.setattr(agent_usage, "read_other_agents", read_other_agents)
    monkeypatch.setattr(sys, "argv", [command, "list", "--json"])
    main()
    result = json.loads(capsys.readouterr().out)
    assert result["accounts"] == []
    assert result["otherAgents"] == {"codex": {"status": "ok", **LIMITS}}
