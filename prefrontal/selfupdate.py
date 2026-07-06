"""Remote update + restart of the running service.

Lets the operator pull the latest code and restart Prefrontal without SSHing to
the host — over HTTP (``POST /admin/update`` / ``/admin/restart``, operator-only
and gated by :attr:`Settings.self_update_enabled`) or the CLI (``prefrontal
update`` / ``restart``, which run on the host and are always allowed).

The design keeps two concerns separate and both overridable by env:

* **update** — a full deploy (``git pull`` → install deps → apply the idempotent
  schema), by default the ``deploy/update.sh`` script in the repo. Runs
  synchronously so its output can be reported.
* **restart** — bounce the service process. By default ``launchctl kickstart -k``
  the launchd job on the Mac mini. This *kills the current process*, so when the
  caller is the service itself (the HTTP endpoint) the restart is spawned
  **detached** with a short delay, letting the HTTP response flush first. The CLI
  is a separate process, so a detached restart is equally safe there.

Everything shells out through injectable callables so the logic is unit-testable
without touching git or launchd.
"""
from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

#: launchd job label for the reference Mac-mini deployment (see docs/deployment.md).
DEFAULT_LAUNCHD_LABEL = "com.prefrontal"

#: Seconds the synchronous update step may run before we give up (git + pip).
UPDATE_TIMEOUT_SECONDS = 600.0

#: Seconds to wait before the detached restart fires, so an HTTP response flushes.
RESTART_DELAY_SECONDS = 1

#: Runner signature: (argv, cwd, timeout) -> (returncode, combined_output).
Runner = Callable[[list[str], str, float], "tuple[int, str]"]


def repo_dir(settings: Any) -> str:
    """Directory to run the update in — the configured dir, or the repo root.

    Defaults to the parent of the ``prefrontal`` package (its checkout), which is
    where ``git pull`` and ``deploy/update.sh`` belong.
    """
    configured = (getattr(settings, "self_update_repo_dir", "") or "").strip()
    return configured or str(Path(__file__).resolve().parent.parent)


def update_command(settings: Any) -> list[str]:
    """The command that pulls + installs + migrates. Override via ``update_cmd``."""
    override = (getattr(settings, "update_cmd", "") or "").strip()
    if override:
        return shlex.split(override)
    return ["bash", str(Path(repo_dir(settings)) / "deploy" / "update.sh")]


def restart_command(settings: Any) -> list[str]:
    """The command that bounces the service. Override via ``restart_cmd``.

    Default targets the launchd job in the caller's GUI domain, matching the
    reference deployment: ``launchctl kickstart -k gui/<uid>/<label>``.
    """
    override = (getattr(settings, "restart_cmd", "") or "").strip()
    if override:
        return shlex.split(override)
    target = f"gui/{os.getuid()}/{DEFAULT_LAUNCHD_LABEL}"
    return ["launchctl", "kickstart", "-k", target]


def _run(argv: list[str], cwd: str, timeout: float) -> tuple[int, str]:
    """Run ``argv`` in ``cwd``, returning ``(returncode, stdout+stderr)``."""
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as exc:
        return 127, f"{argv[0]}: {exc}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:g}s"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _spawn_detached(restart_argv: list[str], *, delay: int = RESTART_DELAY_SECONDS) -> None:
    """Fire the restart in a new session after ``delay`` so the caller can reply first.

    ``start_new_session`` puts the helper in its own process group, so a restart
    that kills the service's group (``kickstart -k``) doesn't take the helper down
    before it issues the command.
    """
    quoted = " ".join(shlex.quote(a) for a in restart_argv)
    subprocess.Popen(  # noqa: S602 — argv is operator-configured, not user input
        ["bash", "-c", f"sleep {int(delay)}; {quoted}"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _clip(text: str, limit: int = 4000) -> str:
    """Keep the tail of noisy command output so reports stay bounded."""
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def run_update(
    settings: Any,
    *,
    restart: bool = True,
    runner: Runner = _run,
    spawn_restart: Callable[[list[str]], None] = _spawn_detached,
) -> dict[str, Any]:
    """Run the update, then (on success) restart. Returns a JSON-able report.

    The update runs synchronously so its output is captured. If it fails, the
    restart is **skipped** — a broken pull must not bounce a working service. On
    success (and when ``restart`` is set) the restart is spawned detached and the
    report returns immediately.

    Args:
        settings: App settings (reads ``update_cmd`` / ``restart_cmd`` / repo dir).
        restart: Whether to restart after a successful update.
        runner: Injectable command runner (for tests).
        spawn_restart: Injectable detached-restart launcher (for tests).
    """
    ucmd = update_command(settings)
    code, out = runner(ucmd, repo_dir(settings), UPDATE_TIMEOUT_SECONDS)
    ok = code == 0
    report: dict[str, Any] = {
        "update": {"cmd": ucmd, "ok": ok, "code": code, "output": _clip(out)},
        "restarted": False,
    }
    if not ok or not restart:
        return report
    rcmd = restart_command(settings)
    spawn_restart(rcmd)
    report["restart"] = {"cmd": rcmd}
    report["restarted"] = True
    return report


def run_restart(
    settings: Any,
    *,
    spawn_restart: Callable[[list[str]], None] = _spawn_detached,
) -> dict[str, Any]:
    """Restart the service without updating. Returns a JSON-able report."""
    rcmd = restart_command(settings)
    spawn_restart(rcmd)
    return {"restart": {"cmd": rcmd}, "restarted": True}
