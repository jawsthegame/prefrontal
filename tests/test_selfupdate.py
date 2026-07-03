"""Tests for remote update/restart — command resolution, the runner, endpoints.

Everything shells out through injectable callables, so these never touch git or
launchd: command resolution is pure, and ``run_update`` / the HTTP routes take a
stub runner (or harmless ``true`` commands).
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from prefrontal import selfupdate
from prefrontal.config import Settings
from prefrontal.memory.db import init_db
from prefrontal.memory.store import MemoryStore, provision_user
from prefrontal.webhooks.app import create_app

# --- command resolution ------------------------------------------------------


def test_restart_command_default_targets_launchd():
    cmd = selfupdate.restart_command(Settings())
    assert cmd[:3] == ["launchctl", "kickstart", "-k"]
    assert cmd[3] == f"gui/{os.getuid()}/{selfupdate.DEFAULT_LAUNCHD_LABEL}"


def test_restart_command_env_override_is_shlex_split():
    cmd = selfupdate.restart_command(Settings(restart_cmd="sudo systemctl restart prefrontal"))
    assert cmd == ["sudo", "systemctl", "restart", "prefrontal"]


def test_update_command_default_is_the_deploy_script():
    cmd = selfupdate.update_command(Settings(self_update_repo_dir="/srv/pf"))
    assert cmd == ["bash", "/srv/pf/deploy/update.sh"]


def test_update_command_override():
    assert selfupdate.update_command(Settings(update_cmd="make deploy")) == ["make", "deploy"]


def test_repo_dir_defaults_to_package_root():
    # The package lives in <root>/prefrontal, so repo_dir is that parent.
    root = selfupdate.repo_dir(Settings())
    assert os.path.isdir(os.path.join(root, "prefrontal"))


# --- run_update / run_restart ------------------------------------------------


def test_run_update_success_triggers_restart():
    restarted: list[list[str]] = []
    report = selfupdate.run_update(
        Settings(update_cmd="true", restart_cmd="true"),
        runner=lambda argv, cwd, timeout: (0, "pulled\ninstalled\n"),
        spawn_restart=restarted.append,
    )
    assert report["update"]["ok"] is True
    assert report["restarted"] is True
    assert report["restart"]["cmd"] == ["true"]
    assert restarted == [["true"]]  # restart was fired exactly once


def test_run_update_failure_skips_restart():
    restarted: list[list[str]] = []
    report = selfupdate.run_update(
        Settings(update_cmd="false"),
        runner=lambda argv, cwd, timeout: (1, "merge conflict"),
        spawn_restart=restarted.append,
    )
    assert report["update"]["ok"] is False
    assert report["update"]["code"] == 1
    assert report["restarted"] is False
    assert "restart" not in report
    assert restarted == []  # a broken pull must not bounce a working service


def test_run_update_no_restart_flag():
    restarted: list[list[str]] = []
    report = selfupdate.run_update(
        Settings(),
        restart=False,
        runner=lambda argv, cwd, timeout: (0, "ok"),
        spawn_restart=restarted.append,
    )
    assert report["restarted"] is False and restarted == []


def test_run_restart_fires_restart():
    restarted: list[list[str]] = []
    report = selfupdate.run_restart(Settings(restart_cmd="true"), spawn_restart=restarted.append)
    assert report["restarted"] is True and restarted == [["true"]]


# --- endpoints ---------------------------------------------------------------


@pytest.fixture()
def store():
    conn = init_db(":memory:")
    s = MemoryStore(conn)
    provision_user(s, "op", token="op-tok", is_operator=True)
    provision_user(s, "lee", token="lee-tok")  # non-operator
    try:
        yield s
    finally:
        conn.close()


def _client(store, settings):
    app = create_app(store=store, settings=settings)
    return TestClient(app)


def _h(tok):
    return {"X-Prefrontal-Token": tok}


def test_update_endpoint_requires_operator(store):
    with _client(store, Settings(self_update_enabled=True)) as c:
        assert c.post("/admin/update", headers=_h("lee-tok")).status_code == 403


def test_update_endpoint_disabled_by_default(store):
    # Operator, but the feature is off → 403 (it runs code; must be opt-in).
    with _client(store, Settings()) as c:
        r = c.post("/admin/update", headers=_h("op-tok"))
        assert r.status_code == 403
        assert "PREFRONTAL_SELF_UPDATE" in r.json()["detail"]


def test_update_endpoint_runs_when_enabled(store):
    # Harmless real commands (`true`) so the route exercises end-to-end without
    # touching git/launchd; the detached restart is `sleep 1; true`.
    settings = Settings(self_update_enabled=True, update_cmd="true", restart_cmd="true")
    with _client(store, settings) as c:
        r = c.post("/admin/update", headers=_h("op-tok"))
    assert r.status_code == 200
    body = r.json()
    assert body["update"]["ok"] is True
    assert body["restarted"] is True


def test_restart_endpoint_operator_and_gate(store):
    with _client(store, Settings(self_update_enabled=True, restart_cmd="true")) as c:
        assert c.post("/admin/restart", headers=_h("lee-tok")).status_code == 403
        assert c.post("/admin/restart", headers=_h("op-tok")).status_code == 200
