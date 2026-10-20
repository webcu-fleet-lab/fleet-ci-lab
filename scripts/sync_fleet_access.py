#!/usr/bin/env python3
"""Share the group's org secrets/variables with exactly the repos in fleet.json.

  sync_fleet_access.py [--dry-run]      env SYNC_TOKEN = a token with org Secrets + Variables write

The group = fleet.json `fleet` (+ `extras`, they call plugin-ci.yml too). For every name in
`group.secrets` / `group.variables` of fleet.json the org-level sharing list is set to those
repos — nothing else about the secret is touched (value, visibility). A secret that does not
exist or is not "selected"-visibility is reported and skipped: creating it is the org owner's
one-time step, the value never passes through here.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"


def call(method: str, path: str, token: str, payload: dict | None = None):
    req = urllib.request.Request(f"{API}{path}", method=method,
                                 data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={"Authorization": f"Bearer {token}",
                                          "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28",
                                          "User-Agent": "fleet-ci-sync-access",
                                          **({"Content-Type": "application/json"} if payload is not None else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:200]
        raise RuntimeError(f"{method} {path} -> HTTP {error.code}: {detail}") from None


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    token = os.environ.get("SYNC_TOKEN", "")
    if not token:
        print("::error::SYNC_TOKEN is empty")
        return 2

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "fleet.json"), encoding="utf-8") as handle:
        manifest = json.load(handle)
    org = manifest["org"]
    group = manifest.get("group", {})
    repos = list(dict.fromkeys(manifest["fleet"] + manifest.get("extras", [])))

    ids: dict[str, int] = {}
    for repo in repos:
        try:
            ids[repo] = call("GET", f"/repos/{org}/{repo}", token)["id"]
        except RuntimeError as error:
            print(f"::warning title=repo missing::{repo}: {error}")
    wanted = set(ids.values())

    failures = 0
    for kind, names in (("secrets", group.get("secrets", [])), ("variables", group.get("variables", []))):
        for name in names:
            base = f"/orgs/{org}/actions/{kind}/{name}"
            try:
                meta = call("GET", base, token)
            except RuntimeError as error:
                print(f"::error title={kind[:-1]} missing::{name}: {error} — an org owner creates it once (visibility: selected)")
                failures += 1
                continue
            if meta.get("visibility") != "selected":
                print(f"::error title=not selected-visibility::{name} is '{meta.get('visibility')}' — the group needs 'selected'")
                failures += 1
                continue
            current = {r["id"]: r["name"] for r in call("GET", f"{base}/repositories?per_page=100", token).get("repositories", [])}
            add = sorted(name_ for id_, name_ in ids.items() if id_ not in current)
            remove = sorted(name_ for id_, name_ in current.items() if id_ not in wanted)
            if not add and not remove:
                print(f"{kind[:-1]} {name}: in sync ({len(current)} repos)")
                continue
            print(f"{kind[:-1]} {name}: +{add or '-'} -{remove or '-'}")
            if dry_run:
                continue
            call("PUT", f"{base}/repositories", token, {"selected_repository_ids": sorted(wanted)})
            print(f"::notice title=group updated::{kind[:-1]} {name} now shared with {len(wanted)} repos")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
