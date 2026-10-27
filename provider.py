"""Forgejo integration for Hisho (see addon.yaml). Runs inside Core. Read only: every call is a GET
(or ``docker inspect`` / a file's size and age); nothing here changes Forgejo.

Upgrades are Hisho's own (``hisho.forgejo_upgrade``, Hisho 2026-10-27 and later): for the Forgejo that runs on
Core's machine (an entry with ``container``) this add-on only reports what that flow is doing."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

#: Forgejo's releases (every line), and one release's notes (they say when it fixes security bugs)
RELEASES = "https://codeberg.org/api/v1/repos/forgejo/forgejo/releases?limit=50&draft=false&pre-release=false"
NOTES = "https://codeberg.org/forgejo/forgejo/raw/branch/forgejo/release-notes-published/{version}.md"
RELEASES_EVERY = 6 * 3600          # ask Codeberg at most this often
BACKUP_MAX_AGE = 26 * 3600         # a nightly backup older than this was missed
BACKUP_MIN_BYTES = 10 * 1024       # smaller than this is an empty or broken backup
MIRROR_MIN_STALE = 6 * 3600        # a pull mirror not synced for 3 intervals (and at least this) is stuck
VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
#: the sign-in page carries the version (no token needed while sign-in is required)
PAGE_VERSION_RE = re.compile(r"assetVersionEncoded:\s*encodeURIComponent\('v?(\d+\.\d+\.\d+)")
SECURITY_RE = re.compile(r"^\s*-\s*Security\b|security (fix|release|bug)", re.I | re.M)

_releases: dict[str, Any] = {}     # what Codeberg said last (also kept in the state file)


# ── reading ──────────────────────────────────────────────────────────────

def base_url(value: str) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        raise ValueError("no address: give Forgejo's web address")
    return text if "://" in text else f"https://{text}"


def fetch(url: str, token: str | None = None, timeout: int = 15) -> bytes:
    headers = {"Accept": "application/json", "User-Agent": "hisho-addon-forgejo"}
    if token:
        headers["Authorization"] = f"token {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def api(entry: dict[str, Any], ctx: Any, path: str) -> Any:
    """GET one API path with the entry's read-only token."""
    try:
        raw = fetch(base_url(entry["url"]) + "/api/v1" + path, ctx.secret(entry["token"]))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise PermissionError(f"the token can't read {path.split('?')[0]} ({exc.code})")
        raise RuntimeError(f"Forgejo answered {exc.code} on {path.split('?')[0]}")
    return json.loads(raw) if raw else None


def health(entry: dict[str, Any]) -> dict[str, Any]:
    """/api/healthz: needs no token. up = it answered and every check passed."""
    url = base_url(entry["url"]) + "/api/healthz"
    try:
        data = json.loads(fetch(url, timeout=10))
    except urllib.error.HTTPError as exc:   # 503 when a check fails, with the same body
        try:
            data = json.loads(exc.read())
        except (ValueError, OSError):
            return {"up": False, "error": f"Forgejo answered {exc.code}"}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return {"up": False, "error": f"can't reach {base_url(entry['url'])} ({getattr(exc, 'reason', exc)})"}
    failed = [name.split(":")[0] for name, runs in (data.get("checks") or {}).items()
              if any((r or {}).get("status") != "pass" for r in runs or [])]
    up = data.get("status") == "pass" and not failed
    return {"up": up, "failed": failed, "error": None if up else f"health check failed: {', '.join(failed) or data.get('status')}"}


def running_version(entry: dict[str, Any], ctx: Any) -> str | None:
    """The version Forgejo runs: from the API with a token, else from its sign-in page."""
    if entry.get("token"):
        try:
            v = (api(entry, ctx, "/version") or {}).get("version")
            if v:
                return VERSION_RE.search(str(v)).group(0) if VERSION_RE.search(str(v)) else str(v)
        except Exception:  # noqa: BLE001 -- fall back to the page
            pass
    try:
        page = fetch(base_url(entry["url"]) + "/user/login", timeout=10).decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    m = PAGE_VERSION_RE.search(page)
    return m.group(1) if m else None


def _vt(text: str) -> tuple[int, int, int] | None:
    m = VERSION_RE.search(str(text or ""))
    return tuple(int(x) for x in m.groups()) if m else None  # type: ignore[return-value]


def state_path() -> Path:
    return Path(os.environ.get("HISHO_STATE_DIR") or "~/.local/state/hisho").expanduser() / "addons" / "forgejo" / "releases.json"


def releases(now: float | None = None, get: Any = None) -> dict[str, Any]:
    """Forgejo's published releases from Codeberg, asked at most every RELEASES_EVERY seconds
    (kept in the state file across Core restarts). A failed ask keeps what was known."""
    now = time.time() if now is None else now
    get = get or fetch
    global _releases
    if not _releases:
        try:
            _releases = json.loads(state_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _releases = {}
    if now - float(_releases.get("checked") or 0) < RELEASES_EVERY:
        return _releases
    try:
        found = [{"version": str(r.get("tag_name") or "").lstrip("v"), "published": str(r.get("published_at") or "")[:10],
                  "body": str(r.get("body") or "")[:2000]}
                 for r in json.loads(get(RELEASES, timeout=20))
                 if not r.get("draft") and not r.get("prerelease") and _vt(r.get("tag_name"))]
        _releases = {**_releases, "checked": now, "releases": found, "error": None}
    except Exception as exc:  # noqa: BLE001
        _releases = {**_releases, "checked": now - RELEASES_EVERY + 1800, "error": f"can't ask Codeberg ({str(exc)[:120]})"}
    try:
        state_path().parent.mkdir(parents=True, exist_ok=True)
        state_path().write_text(json.dumps(_releases, indent=1), encoding="utf-8")
    except OSError:
        pass
    return _releases


def is_security(release: dict[str, Any], get: Any = None) -> bool:
    """Whether a release fixes security bugs: its notes have a "Security bug fixes" part (asked once, kept)."""
    known = _releases.setdefault("security", {})
    v = release["version"]
    if v not in known:
        text = release.get("body") or ""
        try:
            text += (get or fetch)(NOTES.format(version=v), timeout=20).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 -- unknown: ask again next time
            return bool(SECURITY_RE.search(text))
        known[v] = bool(SECURITY_RE.search(text))
        try:
            state_path().write_text(json.dumps(_releases, indent=1), encoding="utf-8")
        except OSError:
            pass
    return bool(known[v])


def update(running: str | None, known: dict[str, Any], get: Any = None) -> dict[str, Any]:
    """Newer releases on the running version's line (same major: Forgejo's LTS lines are majors)."""
    cur = _vt(running or "")
    rels = known.get("releases") or []
    out: dict[str, Any] = {"running": running, "newer": [], "security": False, "latest": None,
                           "newest_major": None, "error": known.get("error")}
    if cur is None or not rels:
        return out
    line = sorted((r for r in rels if _vt(r["version"])[0] == cur[0]), key=lambda r: _vt(r["version"]))
    newer = [r for r in line if _vt(r["version"]) > cur]
    out["latest"] = line[-1]["version"] if line else None
    out["newer"] = [{"version": r["version"], "published": r["published"], "security": is_security(r, get)} for r in newer]
    out["security"] = any(r["security"] for r in out["newer"])
    top = max(rels, key=lambda r: _vt(r["version"]))
    if _vt(top["version"])[0] > cur[0]:
        out["newest_major"] = top["version"]
    return out


def _docker_env() -> dict[str, str]:
    """Rootless Docker's socket when Core (a system unit) has no DOCKER_HOST of its own."""
    if os.environ.get("DOCKER_HOST"):
        return {}
    sock = Path(f"/run/user/{os.getuid()}/docker.sock")
    return {"DOCKER_HOST": f"unix://{sock}"} if sock.exists() else {}


def _run(ctx: Any, cmd: list[str]) -> "subprocess.CompletedProcess[str]":
    if ctx is not None and hasattr(ctx, "run"):
        return ctx.run(cmd, timeout=20, env=_docker_env())
    return subprocess.run(cmd, capture_output=True, text=True, timeout=20, env={**os.environ, **_docker_env()})


def containers(entry: dict[str, Any], ctx: Any) -> list[dict[str, Any]] | None:
    """Forgejo's container and the others of its compose project (its database): state, health."""
    name = str(entry.get("container") or "").strip()
    if not name:
        return None
    res = _run(ctx, ["docker", "inspect", name])
    if res.returncode:
        return [{"name": name, "state": "missing", "health": "", "image": "",
                 "error": (res.stderr or "docker inspect failed").strip()[-160:]}]
    first = json.loads(res.stdout)[0]
    project = ((first.get("Config") or {}).get("Labels") or {}).get("com.docker.compose.project")
    found = [first]
    if project:
        ids = _run(ctx, ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"])
        others = [i for i in ids.stdout.split() if not first.get("Id", "").startswith(i)] if not ids.returncode else []
        if others:
            more = _run(ctx, ["docker", "inspect", *others])
            if not more.returncode:
                found += json.loads(more.stdout)
    out = []
    for c in found:
        st = c.get("State") or {}
        out.append({"name": str(c.get("Name") or "").lstrip("/"), "state": st.get("Status") or "unknown",
                    "health": (st.get("Health") or {}).get("Status") or "", "image": (c.get("Config") or {}).get("Image") or "",
                    "since": str(st.get("StartedAt") or "")[:16].replace("T", " "), "restarts": c.get("RestartCount") or 0})
    return out


def _newest(folder: Path, pattern: str) -> dict[str, Any] | None:
    files = [p for p in folder.glob(pattern) if p.is_file() and not p.name.endswith(".part")]
    if not files:
        return None
    p = max(files, key=lambda f: f.stat().st_mtime)
    st = p.stat()
    return {"file": p.name, "bytes": st.st_size, "mtime": st.st_mtime}


def backups(entry: dict[str, Any], now: float | None = None) -> dict[str, Any] | None:
    """The newest Forgejo dump and database dump in the backups folder, and when the folder was last
    copied to another machine (``.last-copy``, touched by the copy job). ``problems`` say what is wrong."""
    folder = str(entry.get("backups") or "").strip()
    if not folder:
        return None
    now = time.time() if now is None else now
    d = Path(folder).expanduser()
    out: dict[str, Any] = {"folder": folder, "problems": []}
    if not d.is_dir():
        out["problems"].append(f"the backups folder {folder} isn't there")
        return out
    for key, pattern, label in (("dump", "forgejo-dump-*", "Forgejo dump"), ("db", "forgejo-db-*", "database dump")):
        f = out[key] = _newest(d, pattern)
        if f is None:
            out["problems"].append(f"no {label} at all")
        elif now - f["mtime"] > BACKUP_MAX_AGE:
            out["problems"].append(f"the newest {label} is {ago(now - f['mtime'])} old")
        elif f["bytes"] < BACKUP_MIN_BYTES:
            out["problems"].append(f"the newest {label} is only {size(f['bytes'])}")
    copy = d / ".last-copy"
    out["copied"] = copy.stat().st_mtime if copy.exists() else None
    return out


def _paged(entry: dict[str, Any], ctx: Any, path: str, key: str | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in range(1, 21):
        sep = "&" if "?" in path else "?"
        got = api(entry, ctx, f"{path}{sep}limit=50&page={page}")
        rows = (got or {}).get(key) if key else got
        out += rows or []
        if len(rows or []) < 50:
            break
    return out


def interval(text: str) -> float:
    """Forgejo's mirror interval ("8h0m0s") in seconds."""
    total = 0.0
    for n, unit in re.findall(r"([\d.]+)([hms])", str(text or "")):
        total += float(n) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total or 8 * 3600


def stamp(text: str) -> float | None:
    try:
        t = datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    return t if t > 0 else None   # "0001-01-01..." = never


def forge(entry: dict[str, Any], ctx: Any, now: float | None = None) -> dict[str, Any] | None:
    """With a token: repositories, pull mirrors that stopped syncing, runners, watched CI runs.
    Each part that can't be read says why instead of failing the rest."""
    if not entry.get("token"):
        return None
    now = time.time() if now is None else now
    org = str(entry.get("org") or "").strip()
    out: dict[str, Any] = {"errors": {}}
    try:
        repos = _paged(entry, ctx, f"/orgs/{org}/repos") if org else _paged(entry, ctx, "/repos/search", "data")
        mirrors = [r for r in repos if r.get("mirror")]
        stale = []
        for r in mirrors:
            last = stamp(r.get("mirror_updated"))
            limit = max(3 * interval(r.get("mirror_interval")), MIRROR_MIN_STALE)
            if last is None or now - last > limit:
                stale.append({"name": r.get("full_name") or r.get("name"), "last": last})
        newest = max((stamp(r.get("mirror_updated")) or 0 for r in mirrors), default=0)
        out.update(repos=len(repos), mirrors=len(mirrors), stale=stale, synced=newest or None)
    except Exception as exc:  # noqa: BLE001
        out["errors"]["repos"] = str(exc)[:160]
    if org:
        try:
            got = api(entry, ctx, f"/orgs/{org}/actions/runners")
            rows = got.get("runners") if isinstance(got, dict) else got   # a list in Forgejo 15
            out["runners"] = [{"name": r.get("name"), "status": r.get("status") or "unknown",
                               "labels": [str(lbl.get("name") if isinstance(lbl, dict) else lbl) for lbl in r.get("labels") or []],
                               "version": r.get("version") or ""} for r in rows or []]
        except Exception as exc:  # noqa: BLE001
            out["errors"]["runners"] = str(exc)[:160]
    out["ci"] = []
    for repo in [w.strip() for w in str(entry.get("watch") or "").split(",") if w.strip()]:
        try:
            runs = (api(entry, ctx, f"/repos/{repo}/actions/runs?limit=1") or {}).get("workflow_runs") or []
            r = runs[0] if runs else {}
            out["ci"].append({"repo": repo, "status": r.get("status") or "no runs yet", "title": r.get("title") or "",
                              "when": stamp(r.get("stopped") or r.get("started") or r.get("created") or ""),
                              "number": r.get("index_in_repo")})
        except Exception as exc:  # noqa: BLE001
            out["ci"].append({"repo": repo, "status": "unknown", "error": str(exc)[:160]})
    return out


# ── putting it together ──────────────────────────────────────────────────

def ago(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 3600:
        return f"{max(1, s // 60)} min"
    if s < 2 * 86400:
        return f"{s // 3600} h"
    return f"{s // 86400} days"


def size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def managed(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Hisho's own upgrade flow, when it looks after this Forgejo (it runs on Core's machine): pinned version and
    the last run. None for any other Forgejo, or a Hisho without that flow."""
    if not entry.get("container"):
        return None
    try:
        from hisho import forgejo_upgrade
        st = forgejo_upgrade.status()
    except Exception:  # noqa: BLE001 -- an older Hisho: no upgrade flow
        return None
    return st if st.get("managed") else None


UPGRADE_LATE = 24 * 3600   # a same-line release out this long and still not running: say so


def check(name: str, entry: dict[str, Any], ctx: Any, now: float | None = None) -> dict[str, Any]:
    """Everything about one Forgejo server, and its problems for Needs you."""
    now = time.time() if now is None else now
    h = health(entry)
    version = running_version(entry, ctx) if h["up"] or not h.get("error", "").startswith("can't reach") else None
    out: dict[str, Any] = {"name": name, "health": h, "version": version, "problems": []}
    out["update"] = update(version, releases(now))
    out["hisho"] = managed(entry)
    try:
        out["containers"] = containers(entry, ctx)
    except Exception as exc:  # noqa: BLE001
        out["containers"] = [{"name": entry.get("container"), "state": "unknown", "health": "", "image": "", "error": str(exc)[:160]}]
    out["backups"] = backups(entry, now)
    out["forge"] = forge(entry, ctx, now) if h["up"] else None
    add = out["problems"].append
    if not h["up"]:
        add({"entry": name, "severity": "warn", "key": "down", "title": f"Forgejo {name} is down",
             "detail": f"{h['error']}. Git pushes and pulls to it fail until it's back."})
    for c in out["containers"] or []:
        if c["state"] != "running" or c["health"] == "unhealthy":
            add({"entry": name, "severity": "warn", "key": f"container-{c['name']}",
                 "title": f"Forgejo's container {c['name']} is {c['health'] if c['state'] == 'running' else c['state']}",
                 "detail": c.get("error") or f"docker on Core's machine says {c['state']}{' / ' + c['health'] if c['health'] else ''}."})
    b = out["backups"]
    if b and b["problems"]:
        add({"entry": name, "severity": "warn", "key": "backup", "title": f"Forgejo {name}'s backup needs a look",
             "detail": "Last night's backup isn't right: " + "; ".join(b["problems"]) + f" (in {b['folder']}). "
                       "If Forgejo's disk failed now, its repositories could not be restored from it."})
    u, hm = out["update"], out["hisho"]
    if u["newer"] and hm:
        newest = u["newer"][-1]
        try:
            age = now - datetime.fromisoformat(newest["published"]).timestamp()
        except ValueError:
            age = 0
        late = age > UPGRADE_LATE
        add({"entry": name, "severity": "warn" if late else "info", "key": "update",
             "title": f"Forgejo {newest['version']} is out" + (": Hisho hasn't upgraded yet" if late else ""),
             "detail": f"{name} runs {version}; Hisho upgrades same-line releases by itself (pin commit, CI, then "
                       f"Kihon's self-update with a backup and rollback). Pinned now: {hm.get('forgejo')}; "
                       f"last run: {hm.get('status') or 'not yet'}{': ' + hm['reason'] if hm.get('reason') else ''}."})
    elif u["newer"]:
        newest = u["newer"][-1]["version"]
        if u["security"]:
            add({"entry": name, "severity": "warn", "key": "update", "title": f"Forgejo security update {newest} is out",
                 "detail": f"{name} runs {version}; {newest} fixes security bugs. Upgrade when you can (this Forgejo isn't on Core's machine, so Hisho doesn't upgrade it)."})
        else:
            add({"entry": name, "severity": "info", "key": "update", "title": f"Forgejo {newest} is out",
                 "detail": f"{name} runs {version}. A bug-fix release on the same line; no hurry."})
    f = out["forge"]
    if f and f.get("stale"):
        names = ", ".join(s["name"] for s in f["stale"][:5]) + ("..." if len(f["stale"]) > 5 else "")
        add({"entry": name, "severity": "warn", "key": "mirrors",
             "title": f"{len(f['stale'])} Forgejo mirror{'s' if len(f['stale']) != 1 else ''} stopped syncing",
             "detail": f"Not updated from their source for hours: {names}. Their copies on {name} fall behind "
                       "(an expired or revoked mirror token does this)."})
    return out


def _headline(c: dict[str, Any], now: float) -> str:
    if not c["health"]["up"]:
        return "down"
    parts = [f"up · {c['version'] or 'version ?'}"]
    u = c["update"]
    if u["newer"]:
        parts.append(f"{'security ' if u['security'] else ''}update {u['newer'][-1]['version']} available")
    b = c["backups"]
    if b:
        ages = [now - b[k]["mtime"] for k in ("dump", "db") if b.get(k)]
        parts.append("backup " + ("needs a look" if b["problems"] else f"{ago(max(ages))} ago"))
    f = c["forge"]
    if f and "mirrors" in f:
        parts.append(f"{len(f['stale'])} of {f['mirrors']} mirrors stuck" if f["stale"] else f"{f['mirrors']} mirrors ok")
    return " · ".join(parts)


def summary(entries: dict[str, dict[str, Any]], ctx: Any) -> dict[str, Any]:
    if not entries:
        return {"state": "unknown", "headline": "No Forgejo server yet", "problems": []}
    now = time.time()
    checks = [check(n, e, ctx, now) for n, e in entries.items()]
    problems = [p for c in checks for p in c["problems"]]
    state = "down" if any(not c["health"]["up"] for c in checks) else \
        "warn" if any(p["severity"] != "info" for p in problems) else "ok"
    heads = [_headline(c, now) for c in checks]
    headline = heads[0] if len(checks) == 1 else " | ".join(f"{c['name']}: {h}" for c, h in zip(checks, heads))
    return {"state": state, "headline": headline, "problems": problems}


def _item(id_: str, name: str, state: str, facts: list[str]) -> dict[str, Any]:
    return {"id": id_, "name": name, "state": state, "facts": [f for f in facts if f], "actions": []}


def details(entries: dict[str, dict[str, Any]], ctx: Any) -> list[dict[str, Any]]:
    now = time.time()
    out = []
    for name, entry in entries.items():
        c = check(name, entry, ctx, now)
        h, u, items = c["health"], c["update"], []
        items.append(_item("server", f"Forgejo {c['version'] or ''}".strip(), "up" if h["up"] else "down",
                           [base_url(entry["url"]), "health checks pass (database, cache)" if h["up"] else h["error"]]))
        if u["newer"]:
            items.append(_item("update", f"Update available: {u['newer'][-1]['version']}", "warn" if u["security"] else "off",
                               [f"{r['version']} ({r['published']}){' fixes security bugs' if r['security'] else ''}" for r in u["newer"]]
                               + ([f"Hisho upgrades it by itself: pinned {c['hisho'].get('forgejo')}, last run "
                                   f"{c['hisho'].get('status') or 'not yet'}"] if c.get("hisho") else
                                  ["Not on Core's machine: Hisho only tells you; upgrading is up to you"])))
        else:
            items.append(_item("update", "Version: newest of its line" if u["latest"] else "Version: not checked yet",
                               "up" if u["latest"] else "off",
                               [f"{c['version']} is the newest {c['version'].split('.')[0]}.x release" if u["latest"] and c["version"] else
                                u.get("error") or "Codeberg's release list isn't in yet"]))
        if u.get("newest_major"):
            items[-1]["facts"].append(f"newest overall: {u['newest_major']} (a new major; "
                                      + ("Hisho asks on Approvals before moving)" if c.get("hisho") else
                                         "read its notes before moving)"))
        if u.get("error") and u["latest"]:
            items[-1]["facts"].append(u["error"])
        for k in c["containers"] or []:
            items.append(_item(f"container-{k['name']}", f"Container {k['name']}",
                               "up" if k["state"] == "running" and k["health"] != "unhealthy" else "down",
                               [k["state"] + (f", {k['health']}" if k["health"] else ""), k["image"],
                                f"up since {k['since']} UTC" if k.get("since") and k["state"] == "running" else "",
                                f"restarted {k['restarts']} times" if k.get("restarts") else "", k.get("error") or ""]))
        b = c["backups"]
        if b:
            facts = [f"{label}: {b[key]['file']}, {size(b[key]['bytes'])}, {ago(now - b[key]['mtime'])} ago"
                     for key, label in (("dump", "Forgejo dump"), ("db", "database dump")) if b.get(key)]
            facts += b["problems"]
            facts.append(f"copied to another machine {ago(now - b['copied'])} ago" if b.get("copied")
                         else "no copy to another machine seen yet")
            items.append(_item("backup", "Backups", "warn" if b["problems"] else "up", facts))
        f = c["forge"]
        if f is None:
            items.append(_item("token", "Repositories, mirrors, runners, CI", "off",
                               ["need a read-only token (read:repository, read:organization)" if h["up"] else "Forgejo is down"]))
        else:
            if "repos" in f:
                stale = [f"{s['name']}: last synced {ago(now - s['last']) + ' ago' if s['last'] else 'never'}" for s in f["stale"]]
                items.append(_item("mirrors", f"{f['repos']} repositories, {f['mirrors']} pull mirrors",
                                   "warn" if f["stale"] else "up",
                                   stale or ([f"all synced; newest sync {ago(now - f['synced'])} ago"] if f.get("synced") else [])))
            for r in f.get("runners") or []:
                items.append(_item(f"runner-{r['name']}", f"Runner {r['name']}",
                                   "up" if r["status"] in ("idle", "active") else "off",
                                   [r["status"] + (" (its machine may be parked; it's woken for jobs)" if r["status"] == "offline" else ""),
                                    "labels: " + ", ".join(r["labels"]) if r["labels"] else "", r["version"]]))
            for ci in f.get("ci") or []:
                st = ci["status"]
                items.append(_item("ci-" + re.sub(r"[^A-Za-z0-9._-]", "-", ci["repo"]), f"CI {ci['repo']}",
                                   "up" if st == "success" else "down" if st in ("failure", "cancelled") else
                                   "warn" if st in ("running", "waiting", "blocked") else "off",
                                   [f"latest run #{ci.get('number')}: {st}" if ci.get("number") else st,
                                    ci.get("title") or "", f"{ago(now - ci['when'])} ago" if ci.get("when") else "",
                                    ci.get("error") or ""]))
            for part, why in f["errors"].items():
                items.append(_item(f"error-{part}", f"Can't read {part}", "warn", [why]))
        if entry.get("note"):
            items[0]["facts"].append(str(entry["note"]))
        out.append({"title": name, "entry": name,
                    "state": "down" if not h["up"] else "warn" if any(p["severity"] != "info" for p in c["problems"]) else "ok",
                    "facts": [_headline(c, now)], "items": items})
    return out


def test(entry: dict[str, Any], ctx: Any) -> dict[str, Any]:
    h = health(entry)
    if not h["up"]:
        return {"ok": False, "error": h["error"]}
    msg = f"Connected: Forgejo {running_version(entry, ctx) or '(version unknown)'}, healthy"
    if entry.get("token"):
        try:
            api(entry, ctx, "/version")
            if entry.get("org"):
                api(entry, ctx, f"/orgs/{entry['org']}/repos?limit=1")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"Forgejo is up, but {exc}"}
        msg += "; the token reads" + (f" {entry['org']}'s repositories" if entry.get("org") else "")
    return {"ok": True, "message": msg}


def act(entries: dict[str, dict[str, Any]], ctx: Any, entry: str, item: str, action: str) -> dict[str, Any]:
    return {"ok": False, "error": "Forgejo's add-on only reads; it has no buttons"}
