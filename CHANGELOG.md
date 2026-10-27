# Changelog

## 0.2.2

- Install in Hisho: with no Forgejo yet, its page installs one on the Core machine (Hisho's own install: Docker, PostgreSQL, tailnet port 3443) and fills in the entry; never over an existing Forgejo.

## 0.2.1

- README says what you need (addon.yaml you_need) and how to use it; the README check requires both.

## 0.2.0

- The Forgejo on Core's machine is upgraded by Hisho itself now (`hisho.forgejo_upgrade`: pin commit,
  CI, guarded upgrade with backup and rollback). Its update item says what that flow is doing, is
  quiet until a release has been out a day without running, and a new major points to Approvals.
  Other Forgejo servers: unchanged ("Hisho doesn't upgrade it" now says why).

## 0.1.0

- First version: health and version, new-release heads-up from Codeberg (same line; security releases
  flagged), containers and backups on Core's machine, and with a read-only token repositories, stuck
  pull mirrors, runners and watched CI runs. Read only; no buttons.
