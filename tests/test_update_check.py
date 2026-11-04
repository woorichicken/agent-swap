"""Tests for update_check module."""

from __future__ import annotations

import json
import subprocess
import time
from unittest.mock import MagicMock, patch

import pytest

from claude_swap.update_check import (
    _detect_install_method,
    _installed_version_on_disk,
    check_for_update,
    held_back_release_notice,
    run_self_upgrade,
)


def _uv_pip_list(rows: list[dict]) -> MagicMock:
    """A completed `uv pip list --outdated --format json` run."""
    return MagicMock(returncode=0, stdout=json.dumps(rows))


_OUTDATED = [{"name": "claude-swap", "version": "0.27.2", "latest_version": "0.28.0"}]


def _write_cache(path, version, timestamp=None):
    """Write a cache file in the shared cache format."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "timestamp": timestamp if timestamp is not None else time.time(),
        "data": version,
    }))


class TestCheckForUpdate:
    @pytest.mark.parametrize("method", ["uv", "pipx", None])
    @pytest.mark.parametrize("version", ["0.1.0", "0.27.0b1", "main"])
    def test_fork_never_looks_up_upstream(self, method, version, tmp_path, monkeypatch):
        cache_path = tmp_path / "cache.json"
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", cache_path)
        with patch("claude_swap.update_check._detect_install_method", return_value=method), \
             patch("urllib.request.urlopen") as network, \
             patch("claude_swap.update_check.subprocess.run") as run:
            assert check_for_update(version) is None
        network.assert_not_called()
        run.assert_not_called()
        assert not cache_path.exists()

    def test_legacy_pypi_lookup_is_also_disabled(self):
        from claude_swap.update_check import _pypi_latest_version

        with patch("urllib.request.urlopen") as network:
            assert _pypi_latest_version() is None
        network.assert_not_called()

    @pytest.mark.parametrize("cached", ["99.0.0", None, {"latest": "99.0.0"}])
    def test_upstream_cache_cannot_advertise_an_update(self, cached, tmp_path, monkeypatch):
        cache_path = tmp_path / "cache.json"
        _write_cache(cache_path, cached)
        before = cache_path.read_bytes()
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", cache_path)
        with patch("claude_swap.update_check._latest_version") as lookup:
            assert check_for_update("0.1.0") is None
        lookup.assert_not_called()
        assert cache_path.read_bytes() == before


class TestDetectInstallMethod:
    def _set_prefix(self, monkeypatch, prefix: str) -> None:
        monkeypatch.setattr("claude_swap.update_check.sys.prefix", prefix)
        # Clear env vars by default so path-based detection runs in isolation.
        monkeypatch.delenv("UV_TOOL_DIR", raising=False)
        monkeypatch.delenv("PIPX_HOME", raising=False)

    def test_uv_tool_default_path(self, monkeypatch):
        self._set_prefix(monkeypatch, "/home/me/.local/share/uv/tools/claude-swap")
        assert _detect_install_method() == "uv"

    def test_pipx_default_path(self, monkeypatch):
        self._set_prefix(monkeypatch, "/home/me/.local/pipx/venvs/claude-swap")
        assert _detect_install_method() == "pipx"

    def test_non_adjacent_uv_tools_does_not_match(self, monkeypatch):
        # Both segments present but not adjacent — must not false-positive.
        self._set_prefix(monkeypatch, "/home/me/projects/uv/some-tools/.venv")
        assert _detect_install_method() is None

    def test_non_adjacent_pipx_venvs_does_not_match(self, monkeypatch):
        self._set_prefix(monkeypatch, "/home/me/repos/pipx-clone/venvs-of-mine/.venv")
        assert _detect_install_method() is None

    def test_source_checkout_returns_none(self, monkeypatch):
        self._set_prefix(monkeypatch, "/home/me/code/claude-swap/.venv")
        assert _detect_install_method() is None

    def test_mixed_case_path_detected(self, monkeypatch):
        # Lowercasing should make matching case-insensitive (e.g. Windows).
        self._set_prefix(monkeypatch, "/Home/Me/.local/share/UV/Tools/claude-swap")
        assert _detect_install_method() == "uv"

    def test_uv_tool_dir_env_with_prefix_under_it(self, monkeypatch, tmp_path):
        custom_root = tmp_path / "uv-tools"
        prefix = custom_root / "claude-swap"
        monkeypatch.setattr("claude_swap.update_check.sys.prefix", str(prefix))
        monkeypatch.setenv("UV_TOOL_DIR", str(custom_root))
        monkeypatch.delenv("PIPX_HOME", raising=False)
        assert _detect_install_method() == "uv"

    def test_uv_tool_dir_env_set_but_prefix_elsewhere(self, monkeypatch, tmp_path):
        custom_root = tmp_path / "uv-tools"
        # Prefix lives somewhere else entirely — env var alone must not trigger.
        monkeypatch.setattr(
            "claude_swap.update_check.sys.prefix", str(tmp_path / "some-project" / ".venv")
        )
        monkeypatch.setenv("UV_TOOL_DIR", str(custom_root))
        monkeypatch.delenv("PIPX_HOME", raising=False)
        assert _detect_install_method() is None

    def test_pipx_home_env_with_prefix_under_it(self, monkeypatch, tmp_path):
        custom_root = tmp_path / "pipx-home"
        prefix = custom_root / "venvs" / "claude-swap"
        monkeypatch.setattr("claude_swap.update_check.sys.prefix", str(prefix))
        monkeypatch.setenv("PIPX_HOME", str(custom_root))
        monkeypatch.delenv("UV_TOOL_DIR", raising=False)
        assert _detect_install_method() == "pipx"


class _UvInstall:
    """A uv tool install under tmp_path, with the receipt written per test."""

    @pytest.fixture(autouse=True)
    def _uv_install(self, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        monkeypatch.setattr("claude_swap.update_check.sys.prefix", str(tmp_path))
        monkeypatch.setattr("claude_swap.update_check.sys.platform", "linux")
        monkeypatch.setattr("claude_swap.update_check._detect_install_method", lambda: "uv")
        monkeypatch.delenv("UV_EXCLUDE_NEWER", raising=False)
        # PyPI is unreachable unless a test says otherwise, so a uv failure
        # that falls back to it cannot leave the sandbox.
        monkeypatch.setattr(
            "urllib.request.urlopen",
            MagicMock(side_effect=OSError("network error")),
        )

    @staticmethod
    def _write_receipt(
        prefix, options: str = "", requirements: str = '{ name = "claude-swap" }'
    ) -> None:
        (prefix / "uv-receipt.toml").write_text(
            f"[tool]\nrequirements = [{requirements}]\n\n[tool.options]\n{options}\n"
        )

    @staticmethod
    def _uv_command(mock_run) -> list[str]:
        mock_run.assert_called_once()
        return mock_run.call_args.args[0]


class TestHeldBackReleaseNotice(_UvInstall):
    """`uv tool upgrade` prints "Nothing to upgrade" both when the install is
    current and when the newest release is younger than the cooldown, so
    `cswap upgrade` asks uv what it would install without the cooldown and
    explains the second case itself."""

    @patch("claude_swap.update_check.subprocess.run")
    def test_held_back_release_is_explained(self, mock_run, tmp_path):
        self._write_receipt(tmp_path, 'exclude-newer-span = "P14D"')
        mock_run.return_value = _uv_pip_list(_OUTDATED)

        notice = held_back_release_notice("0.27.2")

        assert notice is not None
        assert "claude-swap 0.28.0 is on PyPI" in notice
        assert 'uv-receipt.toml: exclude-newer-span = "P14D"' in notice
        assert 'To take it now:          uv tool upgrade claude-swap --exclude-newer "0 days"' in notice
        assert "To restore the cooldown: uv tool upgrade claude-swap --exclude-newer P14D" in notice
        assert "uv tool install" not in notice
        assert "--exclude-newer" not in self._uv_command(mock_run)

    @patch("claude_swap.update_check.subprocess.run")
    def test_absolute_cutoff_is_restored_verbatim(self, mock_run, tmp_path):
        self._write_receipt(tmp_path, 'exclude-newer = "2026-01-01T00:00:00Z"')
        mock_run.return_value = _uv_pip_list(_OUTDATED)

        notice = held_back_release_notice("0.27.2")

        assert notice is not None
        assert "--exclude-newer 2026-01-01T00:00:00Z" in notice

    @patch("claude_swap.update_check.subprocess.run")
    def test_receipt_value_with_shell_characters_gets_no_restore_line(self, mock_run, tmp_path):
        # uv validates what it writes, but the value is pasted into a shell
        # unquoted, so anything outside a duration or timestamp stays out.
        self._write_receipt(tmp_path, 'exclude-newer-span = "P14D; echo $HOME"')
        mock_run.return_value = _uv_pip_list(_OUTDATED)

        notice = held_back_release_notice("0.27.2")

        assert notice is not None
        assert "To take it now:" in notice
        assert "To restore the cooldown" not in notice
        assert "echo" not in notice.split("To take it now")[1]

    @patch("claude_swap.update_check.subprocess.run")
    def test_env_var_source_is_overridden_by_the_flag(self, mock_run, tmp_path, monkeypatch):
        # The command line flag wins over UV_EXCLUDE_NEWER for that one run,
        # and the variable keeps applying to later upgrades, so there is
        # nothing to unset and nothing to restore.
        monkeypatch.setenv("UV_EXCLUDE_NEWER", "1 week")
        mock_run.return_value = _uv_pip_list(_OUTDATED)

        notice = held_back_release_notice("0.27.2")

        assert notice is not None
        assert "UV_EXCLUDE_NEWER=1 week" in notice
        assert 'uv tool upgrade claude-swap --exclude-newer "0 days"' in notice
        assert "overrides UV_EXCLUDE_NEWER" in notice
        assert "unset" not in notice
        assert "To restore the cooldown" not in notice

    @patch("claude_swap.update_check.subprocess.run")
    def test_no_cooldown_means_no_notice_and_no_uv_call(self, mock_run):
        assert held_back_release_notice("0.27.2") is None
        mock_run.assert_not_called()

    @patch("claude_swap.update_check.subprocess.run")
    def test_nothing_newer_is_quiet(self, mock_run, tmp_path):
        self._write_receipt(tmp_path, 'exclude-newer-span = "P14D"')
        mock_run.return_value = _uv_pip_list([])

        assert held_back_release_notice("0.27.2") is None

    @patch("claude_swap.update_check.subprocess.run")
    def test_final_release_is_not_told_about_a_prerelease(self, mock_run, tmp_path):
        self._write_receipt(tmp_path, 'exclude-newer-span = "P14D"')
        mock_run.return_value = _uv_pip_list(
            [{"name": "claude-swap", "latest_version": "0.28.0b1"}]
        )

        assert held_back_release_notice("0.27.2") is None

    @patch("claude_swap.update_check.subprocess.run", side_effect=FileNotFoundError)
    def test_uv_failure_is_quiet(self, mock_run, tmp_path):
        self._write_receipt(tmp_path, 'exclude-newer-span = "P14D"')

        assert held_back_release_notice("0.27.2") is None


@patch("claude_swap.update_check.sys.platform", "linux")
class TestRunSelfUpgrade:
    @pytest.mark.parametrize("method", ["uv", "pipx", None])
    def test_reinstalls_fork_and_shows_command_before_execution(self, method, capsys):
        command = [
            "uv", "tool", "install", "--force",
            "git+https://github.com/woorichicken/agent-swap",
        ]

        def invoke(cmd, **kwargs):
            assert " ".join(command) in capsys.readouterr().out
            assert cmd == command
            assert kwargs == {"check": False}
            return MagicMock(returncode=0)

        with patch("claude_swap.update_check._detect_install_method", return_value=method), \
             patch("claude_swap.update_check._installed_version_on_disk", return_value=None), \
             patch("claude_swap.update_check.subprocess.run", side_effect=invoke) as run, \
             patch("claude_swap.update_check.held_back_release_notice") as notice:
            assert run_self_upgrade() == 0
        run.assert_called_once()
        notice.assert_not_called()

    def test_propagates_failure_without_verifying_version(self):
        with patch("claude_swap.update_check.subprocess.run", return_value=MagicMock(returncode=2)), \
             patch("claude_swap.update_check._installed_version_on_disk") as version:
            assert run_self_upgrade("0.1.0") == 2
        version.assert_not_called()

    @pytest.mark.parametrize("failure", [FileNotFoundError("uv"), PermissionError("uv")])
    def test_unavailable_uv_reports_actionable_error(self, failure, capsys):
        with patch("claude_swap.update_check.subprocess.run", side_effect=failure):
            assert run_self_upgrade("0.1.0") == 1
        captured = capsys.readouterr()
        assert "PATH" in captured.err
        assert "git+https://github.com/woorichicken/agent-swap" in captured.out
        assert "uv tool upgrade claude-swap" not in captured.out + captured.err

    @pytest.mark.parametrize("after", ["0.2.0", "0.1.0", None])
    def test_upgrade_report(self, after, capsys):
        with patch("claude_swap.update_check.subprocess.run", return_value=MagicMock(returncode=0)), \
             patch("claude_swap.update_check._installed_version_on_disk", return_value=after):
            assert run_self_upgrade("0.1.0") == 0
        out = capsys.readouterr().out
        if after == "0.2.0":
            assert "agent-swap upgraded: 0.1.0 -> 0.2.0" in out
        elif after == "0.1.0":
            assert "agent-swap reinstalled (version 0.1.0; Git revision may have changed)" in out
        else:
            assert "upgraded:" not in out
            assert "is still at" not in out

    def test_version_verification_timeout_does_not_mask_upgrade(self):
        with patch("claude_swap.update_check.subprocess.run", side_effect=[
            MagicMock(returncode=0), subprocess.TimeoutExpired("python", 10),
        ]):
            assert run_self_upgrade("0.1.0") == 0


class TestInstalledVersion:
    def test_reads_fork_distribution_metadata(self):
        with patch("claude_swap.update_check.subprocess.run", return_value=MagicMock(stdout="0.1.0\n")) as run:
            assert _installed_version_on_disk() == "0.1.0"
        assert "m.version('agent-swap')" in run.call_args.args[0][2]


@patch("claude_swap.update_check.sys.platform", "win32")
class TestRunSelfUpgradeWindows:
    def test_prints_fork_command_without_replacing_locked_launcher(self, capsys):
        with patch("claude_swap.update_check.subprocess.run") as run:
            assert run_self_upgrade("0.1.0") == 1
        run.assert_not_called()
        assert "uv tool install --force git+https://github.com/woorichicken/agent-swap" in capsys.readouterr().out
