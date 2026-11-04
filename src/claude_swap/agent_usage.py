"""Read-only usage queries for the locally signed-in Codex and Muse agents."""

from __future__ import annotations

import json
import math
import os
import selectors
import shutil
import signal
import subprocess
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from typing import Iterator

QUERY_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 1024 * 1024


@contextmanager
def _agent_process(command: list[str]) -> Iterator[subprocess.Popen]:
    # A separate session lets us reap helpers too, including after a successful
    # response from a long-lived app-server. No login or account switching runs.
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        yield process
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            # macOS may report EPERM for an exited, unreaped group leader.
            # Never hide a refusal to terminate a process that is still live.
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                raise exc
        process.wait()
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()


def _unavailable(reason: str) -> dict:
    return {"status": "unavailable", "reason": reason}


def _number(value: object) -> bool:
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _window(value: object) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("사용량 구간 형식 오류")
    percent = value.get("usedPercent")
    reset = value.get("resetsAt")
    if not _number(percent) or percent < 0:
        raise ValueError("usedPercent 형식 오류")
    if reset is not None and (not _number(reset) or reset < 0):
        raise ValueError("resetsAt 형식 오류")
    return {"usedPercent": percent, "resetsAt": reset}


def _codex_limits(result: object) -> dict:
    if not isinstance(result, dict) or not isinstance(result.get("rateLimits"), dict):
        raise ValueError("rateLimits 응답 없음")
    limits = result["rateLimits"]
    primary = _window(limits.get("primary"))
    secondary = _window(limits.get("secondary"))
    if primary is None and secondary is None:
        raise ValueError("사용량 구간 없음")
    credits = limits.get("credits")
    if credits is None:
        credits = {}
    if not isinstance(credits, dict):
        raise ValueError("credits 형식 오류")
    balance = credits.get("balance")
    has_credits = credits.get("hasCredits")
    plan = limits.get("planType")
    if balance is not None and not (isinstance(balance, str) or _number(balance)):
        raise ValueError("credits.balance 형식 오류")
    if has_credits is not None and not isinstance(has_credits, bool):
        raise ValueError("credits.hasCredits 형식 오류")
    if plan is not None and not isinstance(plan, str):
        raise ValueError("planType 형식 오류")
    return {
        "status": "ok", "primary": primary, "secondary": secondary,
        "credits": {"balance": balance, "hasCredits": has_credits},
        "planType": plan,
    }


def read_codex_usage(*, timeout: float = QUERY_TIMEOUT_SECONDS) -> dict:
    binary = shutil.which(os.environ.get("ASWAP_CODEX_BIN", "codex"))
    if binary is None:
        return _unavailable("실행 파일 없음")
    deadline = time.monotonic() + timeout
    try:
        with _agent_process([binary, "app-server"]) as process:
            def send(message: dict) -> None:
                process.stdin.write((json.dumps({"jsonrpc": "2.0", **message}) + "\n").encode())
                process.stdin.flush()

            send({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "agent-swap", "version": "0.1.0"},
            }})
            initialized = False
            pending = b""
            received = 0
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        return _unavailable("시간 초과")
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        return _unavailable("응답 없이 프로세스 종료")
                    received += len(chunk)
                    if received > MAX_RESPONSE_BYTES:
                        return _unavailable("응답 크기 초과")
                    pending += chunk
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        message = json.loads(line)
                        if not isinstance(message, dict):
                            raise ValueError("JSON-RPC 응답 형식 오류")
                        if "method" in message:
                            continue
                        expected_id = 2 if initialized else 1
                        if message.get("id") != expected_id:
                            continue  # app-server also emits notifications.
                        if "error" in message:
                            raise ValueError(f"JSON-RPC 오류 (요청 {expected_id})")
                        if initialized:
                            return _codex_limits(message.get("result"))
                        if not isinstance(message.get("result"), dict):
                            raise ValueError("initialize 응답 형식 오류")
                        initialized = True
                        send({"method": "initialized"})
                        send({"id": 2, "method": "account/rateLimits/read"})
    except (OSError, ValueError, OverflowError) as exc:
        return _unavailable(f"응답/실행 오류: {type(exc).__name__}: {exc}")


def read_muse_usage(*, timeout: float = QUERY_TIMEOUT_SECONDS) -> dict | None:
    binary = shutil.which(os.environ.get("ASWAP_MUSE_BIN", "muse"))
    if binary is None:
        return None
    try:
        with _agent_process([binary, "usage", "--json"]) as process:
            output, _ = process.communicate(timeout=timeout)
            if process.returncode:
                return _unavailable(f"종료 코드 {process.returncode}")
            if len(output) > MAX_RESPONSE_BYTES:
                return _unavailable("응답 크기 초과")
            result = json.loads(output)
            if (
                not isinstance(result, dict) or result.get("schemaVersion") != 1
                or "allowance" not in result
                or (result["allowance"] is not None and not isinstance(result["allowance"], dict))
            ):
                raise ValueError("Muse 사용량 응답 형식 오류")
            return {
                "status": "ok", "allowance": result["allowance"],
                "reason": result.get("reason"),
            }
    except subprocess.TimeoutExpired:
        return _unavailable("시간 초과")
    except (OSError, ValueError) as exc:
        return _unavailable(f"응답/실행 오류: {type(exc).__name__}: {exc}")


def read_other_agents() -> dict:
    def safely_read(reader):
        try:
            return reader()
        except Exception as exc:
            # Optional usage must never prevent listing the Claude accounts.
            return _unavailable(f"응답/실행 오류: {type(exc).__name__}")

    # Both deadlines start together, keeping list's added wait near 10s.
    with ThreadPoolExecutor(max_workers=2) as executor:
        codex_query = executor.submit(safely_read, read_codex_usage)
        muse_query = executor.submit(safely_read, read_muse_usage)
        agents = {"codex": codex_query.result()}
        muse = muse_query.result()
    if muse is not None:
        agents["muse"] = muse
    return agents


def _single_line(value: object) -> str:
    # Avoid control characters from external programs affecting terminal output.
    return "".join(c if c.isprintable() else " " for c in str(value))


def format_other_agents(agents: dict) -> list[str]:
    lines = []
    for name, reading in agents.items():
        label = "ChatGPT Codex" if name == "codex" else "Muse"
        if reading["status"] != "ok":
            summary = f"못 읽음({_single_line(reading['reason'])})"
        elif name == "codex":
            def percent(key: str) -> str:
                window = reading[key]
                return f"{window['usedPercent']:g}%" if window is not None else "미제공"

            credits = reading["credits"]
            balance = credits["balance"]
            if balance is None:
                balance = "없음" if credits["hasCredits"] is False else "미제공"
            summary = f"5h {percent('primary')} · 주간 {percent('secondary')} · 크레딧 {_single_line(balance)}"
        elif reading["allowance"] is None:
            summary = f"구독 한도 없음 ({_single_line(reading.get('reason') or 'unknown')})"
        else:
            summary = " · ".join(
                f"{_single_line(key)}:{_single_line(json.dumps(value, ensure_ascii=False))}"
                for key, value in reading["allowance"].items()
            ) or "구독 한도 정보 없음"
        lines.append(f"{label} · {summary}")
    return lines
