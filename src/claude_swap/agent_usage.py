"""Read-only usage queries for the locally signed-in Codex and Muse agents."""

from __future__ import annotations

import json
from pathlib import Path
from datetime import date
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
        raise ValueError("malformed usage window")
    percent = value.get("usedPercent")
    reset = value.get("resetsAt")
    if not _number(percent) or percent < 0:
        raise ValueError("malformed usedPercent")
    if reset is not None and (not _number(reset) or reset < 0):
        raise ValueError("malformed resetsAt")
    return {"usedPercent": percent, "resetsAt": reset}


def _codex_limits(result: object) -> dict:
    if not isinstance(result, dict) or not isinstance(result.get("rateLimits"), dict):
        raise ValueError("no rateLimits in response")
    limits = result["rateLimits"]
    primary = _window(limits.get("primary"))
    secondary = _window(limits.get("secondary"))
    if primary is None and secondary is None:
        raise ValueError("no usage windows")
    credits = limits.get("credits")
    if credits is None:
        credits = {}
    if not isinstance(credits, dict):
        raise ValueError("malformed credits")
    balance = credits.get("balance")
    has_credits = credits.get("hasCredits")
    plan = limits.get("planType")
    if balance is not None and not (isinstance(balance, str) or _number(balance)):
        raise ValueError("malformed credits.balance")
    if has_credits is not None and not isinstance(has_credits, bool):
        raise ValueError("malformed credits.hasCredits")
    if plan is not None and not isinstance(plan, str):
        raise ValueError("malformed planType")
    return {
        "status": "ok", "primary": primary, "secondary": secondary,
        "credits": {"balance": balance, "hasCredits": has_credits},
        "planType": plan,
    }


def read_codex_usage(*, timeout: float = QUERY_TIMEOUT_SECONDS) -> dict:
    binary = shutil.which(os.environ.get("ASWAP_CODEX_BIN", "codex"))
    if binary is None:
        return _unavailable("not installed")
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
                        return _unavailable("timed out")
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        return _unavailable("exited without a response")
                    received += len(chunk)
                    if received > MAX_RESPONSE_BYTES:
                        return _unavailable("response too large")
                    pending += chunk
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        message = json.loads(line)
                        if not isinstance(message, dict):
                            raise ValueError("malformed JSON-RPC response")
                        if "method" in message:
                            continue
                        expected_id = 2 if initialized else 1
                        if message.get("id") != expected_id:
                            continue  # app-server also emits notifications.
                        if "error" in message:
                            raise ValueError(f"JSON-RPC error (request {expected_id})")
                        if initialized:
                            return _codex_limits(message.get("result"))
                        if not isinstance(message.get("result"), dict):
                            raise ValueError("malformed initialize response")
                        initialized = True
                        send({"method": "initialized"})
                        send({"id": 2, "method": "account/rateLimits/read"})
    except (OSError, ValueError, OverflowError) as exc:
        return _unavailable(f"query failed: {type(exc).__name__}: {exc}")


def read_muse_usage(*, timeout: float = QUERY_TIMEOUT_SECONDS) -> dict | None:
    binary = shutil.which(os.environ.get("ASWAP_MUSE_BIN", "muse"))
    if binary is None:
        return None
    try:
        with _agent_process([binary, "usage", "--json"]) as process:
            output, _ = process.communicate(timeout=timeout)
            if process.returncode:
                return _unavailable(f"exit code {process.returncode}")
            if len(output) > MAX_RESPONSE_BYTES:
                return _unavailable("response too large")
            result = json.loads(output)
            if (
                not isinstance(result, dict) or result.get("schemaVersion") != 1
                or "allowance" not in result
                or (result["allowance"] is not None and not isinstance(result["allowance"], dict))
            ):
                raise ValueError("malformed Muse usage response")
            return {
                "status": "ok", "allowance": result["allowance"],
                "reason": result.get("reason"),
                "today": read_muse_local_usage(),
            }
    except subprocess.TimeoutExpired:
        return _unavailable("timed out")
    except (OSError, ValueError) as exc:
        return _unavailable(f"query failed: {type(exc).__name__}: {exc}")


def _muse_home() -> Path:
    return Path(os.environ.get("ASWAP_MUSE_HOME", Path.home() / ".local/share/muse"))


def _muse_prices(home: Path) -> dict:
    """model_id -> (input, output, cached) USD per million tokens, from Muse's local model catalog."""
    prices = {}
    for path in sorted((home / "model-catalog").glob("*.json")):
        try:
            rows = json.loads(path.read_text(encoding="utf-8")).get("rows") or []
        except (OSError, ValueError):
            continue
        for row in rows:
            cost = row.get("cost") if isinstance(row, dict) else None
            if isinstance(cost, dict) and row.get("model_id"):
                try:
                    prices[row["model_id"]] = tuple(float(cost[k]) for k in ("input", "output", "cached"))
                except (KeyError, TypeError, ValueError):
                    pass
    return prices


def read_muse_local_usage(day: date | None = None) -> dict | None:
    """Sum today's Muse request usage from local session logs (no network).

    Each model response is one ``payload.event`` carrying ``usage`` and ``model``
    in ``sessions/YYYY/MM/DD/<id>/session.jsonl``. Cost is an estimate:
    tokens x the prices in Muse's own model catalog; cached reads are
    billed at the cached rate and the rest of the input at the input rate.
    """
    home = _muse_home()
    day = day or date.today()
    folder = home / "sessions" / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
    if not folder.is_dir():
        return None
    prices = _muse_prices(home)
    total = {"requests": 0, "input": 0, "cached": 0, "output": 0, "cost": 0.0, "priced": True}
    for log in folder.glob("*/session.jsonl"):
        try:
            handle = log.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            last_model = None  # some usage events omit the model; they belong to the session's model
            for line in handle:
                if '"usage"' not in line:
                    continue
                try:
                    event = json.loads(line).get("payload", {}).get("event", {})
                except ValueError:
                    continue
                usage = event.get("usage") if isinstance(event, dict) else None
                if not isinstance(usage, dict):
                    continue
                model = event.get("model")
                if isinstance(model, dict):
                    model = model.get("model_id")
                if isinstance(model, str):
                    last_model = model
                else:
                    model = last_model
                inp = int(usage.get("input_tokens") or 0)
                cached = int(usage.get("cache_read_tokens") or usage.get("cached_tokens") or 0)
                out = int(usage.get("output_tokens") or 0)
                total["requests"] += 1
                total["input"] += inp
                total["cached"] += cached
                total["output"] += out
                price = prices.get(model) if isinstance(model, str) else None
                if price is None:
                    total["priced"] = False
                    continue
                total["cost"] += (max(inp - cached, 0) * price[0] + cached * price[2] + out * price[1]) / 1e6
    return total if total["requests"] else None


def read_other_agents() -> dict:
    def safely_read(reader):
        try:
            return reader()
        except Exception as exc:
            # Optional usage must never prevent listing the Claude accounts.
            return _unavailable(f"query failed: {type(exc).__name__}")

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


def _tokens(n: int) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1_000_000 else f"{n / 1e3:.0f}K" if n >= 1_000 else str(n)


def _credits_text(balance: object) -> str:
    """Round a credit balance for display (``60217.6045`` -> ``60,218``); keep odd values as-is."""
    try:
        return f"{round(float(balance)):,}"
    except (TypeError, ValueError):
        return _single_line(balance)


AGENT_LABELS = {"codex": "Codex", "muse": "Muse"}


def agent_menu_rows(name: str, reading: dict) -> tuple[str, list[str]]:
    """Short headline for one agent plus detail lines (menu bar submenu / CLI)."""
    if reading["status"] != "ok":
        return f"unavailable ({_single_line(reading['reason'])})", []
    if name == "codex":
        def percent(key: str) -> str:
            window = reading[key]
            return f"{window['usedPercent']:g}%" if window is not None else "n/a"

        credits = reading["credits"]
        balance = credits["balance"]
        if balance is None:
            balance = "none" if credits["hasCredits"] is False else "n/a"
        details = [f"Credits {_credits_text(balance)}"]
        if reading.get("planType"):
            details.append(f"Plan {_single_line(reading['planType'])}")
        return f"5h {percent('primary')} · weekly {percent('secondary')}", details
    if reading["allowance"] is None:
        allowance = f"no plan allowance ({_single_line(reading.get('reason') or 'unknown')})"
        today = reading.get("today")
        if not today:
            return allowance, []
        cost = f"~${today['cost']:.2f}" + ("" if today["priced"] else "+")
        return f"today {today['requests']} req · {cost}", [
            f"Input {_tokens(today['input'])} (cached {_tokens(today['cached'])}) · output {_tokens(today['output'])}",
            "Cost is estimated from local session logs and catalog prices",
            allowance[0].upper() + allowance[1:],
        ]
    summary = " · ".join(
        f"{_single_line(key)}:{_single_line(json.dumps(value, ensure_ascii=False))}"
        for key, value in reading["allowance"].items()
    ) or "allowance details empty"
    return summary, []


def format_other_agents(agents: dict) -> list[str]:
    """One line per agent: ``Label · headline · details``."""
    lines = []
    for name, reading in agents.items():
        headline, details = agent_menu_rows(name, reading)
        if name == "codex":
            details = [d for d in details if d.startswith("Credits")]
        elif name == "muse":
            details = [d for d in details if not d.startswith("Cost is")]
        lines.append(" · ".join([AGENT_LABELS.get(name, name), headline, *details]))
    return lines
