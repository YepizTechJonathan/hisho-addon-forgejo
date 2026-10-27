"""Forgejo add-on tests. Run with Hisho's venv at the repository's top:
~/code/hisho/.venv/bin/python -m pytest -q"""
import json
import os
import subprocess
import time
import urllib.error
from io import BytesIO
from pathlib import Path

import pytest

from hisho import addon_store, addons

HERE = Path(__file__).resolve().parents[1]
URL = "https://git.example.test"
HEALTHY = {"status": "pass", "checks": {"cache:ping": [{"status": "pass"}], "database:ping": [{"status": "pass"}]}}
LOGIN = "<script>window.config = {assetVersionEncoded: encodeURIComponent('15.0.9~gitea-1.22.0'),</script>"
RELEASES = [{"tag_name": "v17.0.0", "published_at": "2026-10-15T18:00:00+02:00", "body": "See notes"},
            {"tag_name": "v16.0.6", "published_at": "2026-10-12T15:00:00+02:00", "body": "See notes"},
            {"tag_name": "v15.0.10", "published_at": "2026-10-12T15:00:00+02:00", "body": "See notes"},
            {"tag_name": "v15.0.9", "published_at": "2026-09-17T17:00:00+02:00", "body": "See notes"},
            {"tag_name": "v15.1.0-rc0", "published_at": "2026-10-01T00:00:00+02:00", "prerelease": True}]
NOTES = {"15.0.10": "## Release notes\n- Security bug fixes\n  - fix: something\n- Bug fixes\n",
         "15.0.9": "## Release notes\n- Bug fixes\n  - Update module x [SECURITY]\n"}


class Forgejo:
    """A stand-in for Forgejo and Codeberg: answers fetch(url) by path."""

    def __init__(self, now):
        self.now, self.health, self.login, self.urls = now, HEALTHY, LOGIN, []
        self.repos = [{"full_name": "hisho/hisho", "mirror": False},
                      {"full_name": "hisho/hisho-addon-a", "mirror": True, "mirror_interval": "1h0m0s",
                       "mirror_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 1800))},
                      {"full_name": "hisho/hisho-addon-b", "mirror": True, "mirror_interval": "1h0m0s",
                       "mirror_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3 * 86400))}]

    def __call__(self, url, token=None, timeout=15):
        self.urls.append((url, token))
        path = url.split("?")[0]
        if path.endswith("/api/healthz"):
            if self.health is None:
                raise urllib.error.URLError("connection refused")
            if self.health["status"] != "pass":
                raise urllib.error.HTTPError(url, 503, "down", {}, BytesIO(json.dumps(self.health).encode()))
            return json.dumps(self.health).encode()
        if path.endswith("/user/login"):
            return self.login.encode()
        if "codeberg.org/api" in url:
            return json.dumps(RELEASES).encode()
        if "release-notes-published" in url:
            return NOTES[path.rsplit("/", 1)[1][:-3]].encode()
        assert token == "value-of-FORGEJO_TEST_READONLY", "API calls carry the read-only token"
        if path.endswith("/api/v1/version"):
            return b'{"version": "15.0.9+gitea-1.22.0"}'
        if path.endswith("/orgs/hisho/repos"):
            return json.dumps(self.repos).encode()
        if path.endswith("/orgs/hisho/actions/runners"):
            return json.dumps([{"name": "builder-1", "status": "offline", "labels": [{"name": "ren"}], "version": "v13.2.0"}]).encode()
        if path.endswith("/repos/hisho/hisho/actions/runs"):
            return json.dumps({"workflow_runs": [{"status": "failure", "title": "tests", "index_in_repo": 7,
                                                  "stopped": "2026-10-21T10:00:00Z"}]}).encode()
        raise urllib.error.HTTPError(url, 404, "not found", {}, BytesIO(b""))


@pytest.fixture
def mod(addon, tmp_path, monkeypatch):
    m = addons.provider(addon)
    monkeypatch.setenv("HISHO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(m, "_releases", {})
    return m


@pytest.fixture
def forgejo(mod, monkeypatch):
    fake = Forgejo(time.time())
    monkeypatch.setattr(mod, "fetch", fake)
    return fake


def backups(tmp_path, age=3600, dump=500_000, db=400_000):
    d = tmp_path / "backups"
    d.mkdir(exist_ok=True)
    for name, n in (("forgejo-dump-20261021T032000Z.tar.gz", dump), ("forgejo-db-20261021T032000Z.dump", db)):
        (d / name).write_bytes(b"x" * n)
        os.utime(d / name, (time.time() - age, time.time() - age))
    (d / "forgejo-dump-20261022T032000Z.tar.gz.part").write_bytes(b"")   # a run in progress is ignored
    (d / ".last-copy").touch()
    os.utime(d / ".last-copy", (time.time() - 5 * 86400, time.time() - 5 * 86400))
    return d


def test_manifest_is_valid_and_readme_matches(addon):
    assert addons.validate_manifest(addon.manifest) == []
    assert addon_store.readme_problems(HERE) == []


def test_no_entries_is_unknown_not_an_alert(addon, ctx_for):
    assert addons.summarize(addon, ctx_for(addon))["state"] in ("unknown", "ok")
    assert addons.provider(addon).summary({}, ctx_for(addon))["state"] == "unknown"


def test_without_a_token_it_shows_health_version_and_backups_and_says_an_update_is_out(mod, forgejo, tmp_path):
    entry = {"url": URL, "backups": str(backups(tmp_path))}
    s = mod.summary({"main": entry}, None)
    assert s["headline"] == "up · 15.0.9 · security update 15.0.10 available · backup 1 h ago"
    # a newer patch with security fixes is a warn (Needs you), and only the API never saw a token
    assert [(p["severity"], p["key"], p["title"]) for p in s["problems"]] == \
        [("warn", "update", "Forgejo security update 15.0.10 is out")]
    assert all(token is None for _, token in forgejo.urls)
    items = {i["id"]: i for i in mod.details({"main": entry}, None)[0]["items"]}
    assert items["update"]["facts"][0] == "15.0.10 (2026-10-12) fixes security bugs"
    assert "newest overall: 17.0.0" in items["update"]["facts"][-1]
    assert items["token"]["state"] == "off"
    # a stale off-machine copy is shown, never alerted (the copy job has its own 2-day alert)
    assert items["backup"]["state"] == "up" and items["backup"]["facts"][-1] == "copied to another machine 5 days ago"


def test_a_plain_bug_fix_release_is_a_quiet_info_item_and_codeberg_is_asked_rarely(mod, forgejo, monkeypatch):
    monkeypatch.setitem(NOTES, "15.0.10", "## Release notes\n- Bug fixes\n")
    s = mod.summary({"main": {"url": URL}}, None)
    assert [(p["severity"], p["title"]) for p in s["problems"]] == [("info", "Forgejo 15.0.10 is out")]
    assert s["state"] == "ok"
    asked = sum("codeberg.org/api" in u for u, _ in forgejo.urls)
    mod.summary({"main": {"url": URL}}, None)
    assert sum("codeberg.org/api" in u for u, _ in forgejo.urls) == asked == 1
    # kept across a Core restart
    monkeypatch.setattr(mod, "_releases", {})
    mod.summary({"main": {"url": URL}}, None)
    assert sum("codeberg.org/api" in u for u, _ in forgejo.urls) == 1


def test_on_the_newest_of_its_line_there_is_no_update_item(mod, forgejo):
    forgejo.login = LOGIN.replace("15.0.9", "15.0.10")
    s = mod.summary({"main": {"url": URL}}, None)
    assert s["problems"] == [] and s["headline"] == "up · 15.0.10"


def test_missing_old_or_empty_backups_raise_one_plain_alert(mod, forgejo, tmp_path):
    forgejo.login = LOGIN.replace("15.0.9", "15.0.10")
    d = backups(tmp_path, age=30 * 3600)
    p = mod.summary({"main": {"url": URL, "backups": str(d)}}, None)["problems"]
    assert [x["key"] for x in p] == ["backup"]
    assert "the newest Forgejo dump is 30 h old" in p[0]["detail"]
    backups(tmp_path, db=200)
    p = mod.summary({"main": {"url": URL, "backups": str(d)}}, None)["problems"]
    assert "the newest database dump is only 200 B" in p[0]["detail"]
    for f in d.glob("forgejo-db-*"):
        f.unlink()
    p = mod.summary({"main": {"url": URL, "backups": str(d)}}, None)["problems"]
    assert "no database dump at all" in p[0]["detail"]
    p = mod.summary({"main": {"url": URL, "backups": str(tmp_path / "nope")}}, None)["problems"]
    assert "isn't there" in p[0]["detail"]


def test_down_or_failing_health_is_down(mod, forgejo):
    forgejo.health = None
    s = mod.summary({"main": {"url": URL}}, None)
    assert s["state"] == "down" and s["headline"] == "down"
    assert s["problems"][0]["key"] == "down" and "can't reach" in s["problems"][0]["detail"]
    forgejo.health = {"status": "fail", "checks": {"database:ping": [{"status": "fail"}]}}
    s = mod.summary({"main": {"url": URL}}, None)
    assert s["state"] == "down" and "health check failed: database" in s["problems"][0]["detail"]
    assert not mod.test({"url": URL}, None)["ok"]


def test_with_a_token_it_shows_mirrors_runners_and_ci_and_flags_stuck_mirrors_only(mod, forgejo, ctx_for, addon):
    entry = {"url": URL, "token": "FORGEJO_TEST_READONLY", "org": "hisho", "watch": "hisho/hisho"}
    ctx = ctx_for(addon)
    s = mod.summary({"main": entry}, ctx)
    assert s["headline"].endswith("1 of 2 mirrors stuck")
    stuck = [p for p in s["problems"] if p["key"] == "mirrors"]
    assert stuck[0]["title"] == "1 Forgejo mirror stopped syncing" and "hisho/hisho-addon-b" in stuck[0]["detail"]
    # an offline runner (Ren parked) and a red CI run are shown, not alerted
    assert {p["key"] for p in s["problems"]} == {"mirrors", "update"}
    items = {i["id"]: i for i in mod.details({"main": entry}, ctx)[0]["items"]}
    assert items["mirrors"]["name"] == "3 repositories, 2 pull mirrors"
    assert items["mirrors"]["facts"] == ["hisho/hisho-addon-b: last synced 3 days ago"]
    assert items["runner-builder-1"]["state"] == "off" and "parked" in items["runner-builder-1"]["facts"][0]
    assert items["ci-hisho-hisho"]["state"] == "down" and items["ci-hisho-hisho"]["facts"][0] == "latest run #7: failure"
    assert mod.test(entry, ctx)["ok"]


def test_a_token_that_cant_read_a_part_says_so_without_hiding_the_rest(mod, forgejo, ctx_for, addon):
    entry = {"url": URL, "token": "FORGEJO_TEST_READONLY", "org": "other"}
    items = {i["id"]: i for i in mod.details({"main": entry}, ctx_for(addon))[0]["items"]}
    assert "error-repos" in items and "error-runners" in items and items["server"]["state"] == "up"


def test_containers_come_from_docker_inspect_with_their_compose_project(mod, monkeypatch):
    calls = []
    forgejo = {"Id": "aaa111", "Name": "/forgejo", "Config": {"Image": "forgejo:15", "Labels": {"com.docker.compose.project": "fj"}},
               "State": {"Status": "running", "StartedAt": "2026-10-21T07:08:00Z"}, "RestartCount": 0}
    db = {"Id": "bbb222", "Name": "/forgejo-db", "Config": {"Image": "postgres:17", "Labels": {}},
          "State": {"Status": "running", "Health": {"Status": "unhealthy"}}, "RestartCount": 3}

    def run(ctx, cmd):
        calls.append(cmd)
        if cmd[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(cmd, 0, "aaa111\nbbb222\n", "")
        return subprocess.CompletedProcess(cmd, 0, json.dumps([forgejo] if cmd[2] == "forgejo" else [db]), "")
    monkeypatch.setattr(mod, "_run", run)
    found = mod.containers({"container": "forgejo"}, None)
    assert [(c["name"], c["state"], c["health"]) for c in found] == [("forgejo", "running", ""), ("forgejo-db", "running", "unhealthy")]
    assert all(c[:2] in (["docker", "inspect"], ["docker", "ps"]) for c in calls), "only reads"
    monkeypatch.setattr(mod, "_run", lambda ctx, cmd: subprocess.CompletedProcess(cmd, 1, "", "No such object: forgejo"))
    assert mod.containers({"container": "forgejo"}, None)[0]["state"] == "missing"


def test_it_has_no_buttons(mod):
    assert not mod.act({"main": {"url": URL}}, None, "main", "server", "restart")["ok"]


def test_on_cores_machine_hisho_upgrades_it_and_the_item_says_how_it_goes(mod, forgejo, monkeypatch):
    from hisho import forgejo_upgrade
    monkeypatch.setattr(mod, "containers", lambda entry, ctx: [])
    monkeypatch.setattr(forgejo_upgrade, "status", lambda: {"managed": True, "forgejo": "15.0.10",
                                                            "status": "waiting", "reason": "CI on abc1234"})
    entry = {"url": URL, "container": "forgejo"}
    s = mod.summary({"main": entry}, None)
    [p] = s["problems"]
    assert (p["severity"], p["title"]) == ("warn", "Forgejo 15.0.10 is out: Hisho hasn't upgraded yet")  # out > a day
    assert "upgrades same-line releases by itself" in p["detail"] and "last run: waiting: CI on abc1234" in p["detail"]
    assert "doesn't upgrade" not in p["detail"]
    items = {i["id"]: i for i in mod.details({"main": entry}, None)[0]["items"]}
    assert items["update"]["facts"][-2].startswith("Hisho upgrades it by itself: pinned 15.0.10")
    assert "Hisho asks on Approvals" in items["update"]["facts"][-1]
    # any other Forgejo (not on Core's machine): Hisho only tells
    s = mod.summary({"main": {"url": URL}}, None)
    assert "Hisho doesn't upgrade it" in s["problems"][0]["detail"]
