# Forgejo (Hisho add-on)

## What it does

Watches your Forgejo git server, read only (it never changes Forgejo itself):

- **Up or down** and the **version** it runs, on the overview card in one line, e.g.
  `up · 15.0.10 · backup 12 h ago · 16 mirrors ok`. The rest is on its page (**Add-ons > Forgejo**).
- **New releases.** Every few hours it asks Codeberg for Forgejo's releases and compares the newest one
  on the same line (major version: 15.x stays on 15.x) with what runs.
  - **The Forgejo on Core's machine** (an entry with `container`) is upgraded by Hisho itself
    (Hisho 2026-10-27 and later): a same-line release becomes a commit to Hisho's version list
    (`deploy/forgejo/versions.env`), Hisho's CI tests it, and Kihon's hourly self-update upgrades
    Forgejo with a backup first, health checks and an automatic rollback. Here a new release is a
    quiet item that says what that flow is doing; it turns into an alert only if the release has
    been out for a day and still isn't running. A new major (say 17.0.0) waits for your OK on
    **Approvals**.
  - **Any other Forgejo**: a bug-fix release is a quiet, low-priority item in Needs you; one whose
    notes list security fixes is a normal alert and says so; a newer major is only mentioned on the page.
- **Containers**, when Forgejo runs in Docker on Core's machine: Forgejo's container and the rest of
  its compose project (its database), with their health.
- **Backups**, when its nightly backups land on Core's machine: the newest Forgejo dump
  (`forgejo-dump-*`) and database dump (`forgejo-db-*`) must be under 26 hours old and not empty, or
  Needs you says so in plain words. When a copy job touches `.last-copy` in that folder, the page
  shows how long ago it was copied to another machine; that is never an alert (a machine that is
  often off would make it noisy; the copy job has its own).
- **With a read-only token**: how many repositories and pull mirrors there are, any mirror that has
  not synced for three of its intervals (at least 6 hours; an expired mirror token does this), the
  organization's Actions runners (offline is normal when their machine is parked) and the latest CI
  run of the repositories you list. Runners and CI are shown, not alerted.

## What it needs

- You need:
  - A Forgejo git server (Hisho can run one on the Core machine)
  - Optional: a read-only Forgejo token, to see runners and private mirrors
- Runs on: the Core machine, inside Hisho Core
- One entry per Forgejo server, each with:
  - `url` (url, required)
  - `token` (secret name, optional)
  - `org` (str, optional)
  - `watch` (str, optional)
  - `container` (str, optional)
  - `backups` (str, optional)
  - `note` (str, optional)

## How to install

On the dashboard: **Add-ons > Store > Forgejo > Install**. Or from a terminal:

```bash
hisho addon install forgejo
```

Hisho takes the newest version from this repository, copies it to the machine it runs on and turns
it on. Updates show as **Update available** on its Store card; **Roll back** returns to the version
before.

## How to use

Its card says up or down, the version, the last backup and the mirrors. Its page (**Add-ons > Forgejo**) shows new releases, backups, mirrors, runners and CI. It never changes Forgejo; a Forgejo on the Core machine is upgraded by Hisho's own guarded update.

No Forgejo yet? Its page has **Install in Hisho**: Hisho runs Forgejo with PostgreSQL in Docker on the Core machine (Kihon, or Souko when Souko is the only machine), on your tailnet at port 3443 (git over SSH at 2222), fills in this entry, and keeps it upgraded. The button only shows when no Forgejo is running there.

## Settings

One entry per Forgejo server, on its page (**Add-ons > Forgejo**) or in
`~/Hisho/System/Config/Addons/forgejo.yaml`:

```yaml
entries:
  main:
    url: https://git.example.ts.net      # its web address
    token: FORGEJO_READONLY_TOKEN        # optional: a secret's NAME, never its value
    org: myorg                           # optional: whose runners and repositories to show
    watch: myorg/myrepo                  # optional: latest CI run of these (comma-separated)
    container: forgejo                   # optional: its Docker container on Core's machine
    backups: ~/backups/forgejo           # optional: its nightly backups on Core's machine
```

Without a token you still get health (`/api/healthz`), the version (from the sign-in page),
containers, backups and release news. For the rest make a token with only **read:repository** and
**read:organization** (Forgejo > Settings > Applications, or on the server
`forgejo admin user generate-access-token --scopes read:repository,read:organization`), store it
as a secret (`hisho secret put NAME`) and give its name. Runners need the token's user to own the
organization. Nothing it does needs a write scope.

## How to remove

**Add-ons > Store > Forgejo > Remove**, or `hisho addon remove forgejo`. Your settings file stays,
so installing it again picks up where you left off.

## License

Source-available under the [PolyForm Shield License 1.0.0](LICENSE.md): free to use, change and share,
for any purpose except offering a product that competes with Hisho (reselling it, for example).
