#!/usr/bin/env python3
"""Deploy WebcuGoetz fleet plugins to a SW6 shop over the Admin API.

Driven by .github/workflows/deploy.yml; not meant to be run by hand (it needs
the environment's secrets). Design and the verified core facts behind every
step: README "deploy.yml — fleet deploy (staging + production)".

Shape: one run = one target, two modes (INPUT_MODE):
  plan    GitHub data only, no shop access — the preview the production
          reviewer reads BEFORE approving; sets the job output `pending`.
  deploy  plugins resolved from fleet.json in its fixed `order`, gated one by
          one against the shop (CI green at the branch head, plus the target's
          version/sha rule), then deployed SEQUENTIALLY — refresh/update mutate
          shared shop state. The batch stops at the first failure.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

GITHUB_API = "https://api.github.com"

# One logic for both targets; they differ only in data: the branch here, the
# plugin set in fleet.json `targets`, the reviewer in the GitHub Environment.
TARGETS = {
    "staging": {"branch": "dev"},
    "production": {"branch": None},  # each repo's default branch (main/master)
}

# "skipped"/"neutral" are tolerated: plugin-ci.yml skips commercial-dependent
# tests on the OSS shop (README "Skipped tests").
OK_CONCLUSIONS = {"success", "skipped", "neutral"}

# urllib sends no Accept header and Shopware then answers 415.
SHOP_ACCEPT = {"Accept": "application/json"}

UPLOAD_TIMEOUT = 300
# An update compiles the theme for storefront plugins (PluginLifecycleSubscriber).
UPDATE_TIMEOUT = 900

# Only a CI run already IN PROGRESS is waited for, briefly (its status lags the end
# of the job that triggered this deploy). A QUEUED run is skipped at once: on the
# single runner it cannot progress while this deploy holds the runner, and its own
# push trigger starts a later deploy anyway.
CI_WAIT_SECONDS = 90
CI_POLL_SECONDS = 15
QUEUED_STATES = {"queued", "waiting", "requested", "pending"}


class Fail(Exception):
    """Stops the whole run."""


class HttpError(Exception):
    def __init__(self, method: str, url: str, code: int, detail: str) -> None:
        super().__init__(f"{method} {url} -> HTTP {code}: {detail}")
        self.code = code


def env(name: str, *, required: bool = True) -> str:
    value = os.environ.get(name, "")
    if required and not value:
        raise Fail(f"missing required environment variable {name}")
    return value


def http(url: str, *, headers: dict, data: bytes | None = None,
         method: str = "GET", timeout: int = 60):
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:600]
        raise HttpError(method, url, error.code, detail) from error
    return json.loads(body) if body else {}


# ------------------------------------------------------------------ pure logic


def version_tuple(raw: str) -> tuple:
    """Numeric compare (`0.10.0 > 0.9.8`); a stray suffix never crashes a decision."""
    parts = []
    for chunk in str(raw or "0").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts)


def resolve_plugins(manifest: dict, requested: str) -> list[str]:
    """fleet.json `order` first (dependency order), then the rest of the fleet;
    extras only when named."""
    fleet, extras = manifest["fleet"], manifest.get("extras", [])
    ordered = [p for p in manifest.get("order", []) if p in fleet]
    ordered += [p for p in fleet if p not in ordered]

    requested = requested.strip()
    if requested == "all-pending":
        return ordered
    names = [p.strip() for p in requested.split(",") if p.strip()]
    unknown = [p for p in names if p not in fleet and p not in extras]
    if unknown:
        raise Fail(f"not in fleet.json: {', '.join(unknown)}")
    return [p for p in ordered if p in names] + [p for p in extras if p in names]


def allowed_on(manifest: dict, target: str, plugin: str) -> bool:
    """fleet.json `targets`: "all" or an explicit list. Production has a list, so
    one approval can never put an unchecked plugin onto the live shop."""
    allowed = manifest.get("targets", {}).get(target, [])
    return allowed == "all" or plugin in allowed


def bump_warning(version: str, deployed_version: str | None, sha: str, last_sha: str | None) -> list[str]:
    """Same version, different commit = a release without a bump. Informational:
    the version on main is the real one, so the promotion should have bumped."""
    if last_sha and last_sha != sha and deployed_version and \
            version_tuple(version) == version_tuple(deployed_version):
        return [f"same version {version} as deployed, different commit — bump missing?"]
    return []


def decide(current: dict | None, new_version: str, sha: str, last_sha: str | None,
           green: bool, ci_note: str) -> tuple[str, str, list[str]]:
    """-> (INSTALL | UPDATE | SKIP, note, warnings); `current` = the shop's extension row.
    The commit decides (versions on `dev` are not reliable); never older than deployed."""
    if not green:
        return "SKIP", ci_note, []
    if current is None or not current.get("installedAt"):
        return "INSTALL", ci_note, []
    old = current.get("version") or "0"
    if version_tuple(new_version) < version_tuple(old):
        return "SKIP", f"{new_version} is older than deployed {old} — Shopware never downgrades", []
    if last_sha == sha:
        return "SKIP", f"already deployed at {sha[:8]}", []
    if last_sha is None:
        # Unknown deployed commit: redeploy once (a same-version update re-runs
        # migrations/assets) so the shop provably equals the branch afterwards.
        return "UPDATE", f"{ci_note} · no deploy record yet", []
    return "UPDATE", ci_note, bump_warning(new_version, old, sha, last_sha)


def plan_decide(target: str, record: dict | None, new_version: str, sha: str,
                green: bool, ci_note: str) -> tuple[str, str, list[str]]:
    """-> (PENDING | SKIP, note, warnings) from GitHub data only; the deploy job
    re-decides against the shop."""
    if not green:
        return "SKIP", ci_note, []
    if record is None:
        return "PENDING", ci_note, [f"first CD deploy to {target} — a FIRST INSTALL + activate "
                                    f"if the plugin is not on the shop yet"]
    if record.get("sha") == sha:
        return "SKIP", f"already deployed at {sha[:8]}", []
    if version_tuple(new_version) < version_tuple(record.get("version")):
        return "SKIP", f"{new_version} is older than last deployed {record.get('version')}", []
    return "PENDING", ci_note, bump_warning(new_version, record.get("version"), sha, record.get("sha"))


def auto_deploy_on(target: str) -> bool:
    """The org switch for push-triggered deploys (only changes the wording here)."""
    return os.environ.get(f"AUTO_DEPLOY_{target.upper()}", "").lower() == "true"


def follow_up(auto: bool) -> str:
    return ("its own push trigger follows" if auto
            else "auto-deploy is off — start a deploy again once it is green")


def ci_verdict(runs: list[dict], sha: str, auto: bool = True) -> tuple[str, str]:
    """-> (green | red | queued | running | none, note) from the newest PUSH run per
    workflow at this commit on this branch (pull_request/dispatch runs of the same
    workflow must not mask it — the caller filters them out in the API query)."""
    newest: dict = {}
    for row in runs:
        key = row.get("workflow_id")
        if key not in newest or row.get("run_number", 0) > newest[key].get("run_number", 0):
            newest[key] = row
    if not newest:
        return "none", f"no push CI run at {sha[:8]} on this branch"
    bad = [f"{r.get('name')}={r.get('conclusion')}" for r in newest.values()
           if r.get("status") == "completed" and r.get("conclusion") not in OK_CONCLUSIONS]
    if bad:
        return "red", f"CI not green ({', '.join(bad[:3])})"
    queued = [r.get("name", "?") for r in newest.values() if r.get("status") in QUEUED_STATES]
    if queued:
        return "queued", f"CI queued ({', '.join(queued[:3])}) — {follow_up(auto)}"
    running = [r.get("name", "?") for r in newest.values() if r.get("status") != "completed"]
    if running:
        return "running", f"CI still running ({', '.join(running[:3])})"
    return "green", f"CI green ({len(newest)} workflow(s))"


def wait_for_ci(gh, repo: str, sha: str, branch: str, *, auto: bool = True, timeout: int = CI_WAIT_SECONDS,
                poll: int = CI_POLL_SECONDS, sleep=time.sleep) -> tuple[bool, str]:
    waited = 0
    while True:
        state, note = ci_verdict(gh.ci_runs(repo, sha, branch), sha, auto)
        if state != "running" or waited >= timeout:
            return state == "green", note
        sleep(poll)
        waited += poll


def pick_commit(planned: dict | None, repo: str, head: str, auto: bool = True) -> tuple[str | None, str]:
    """The deploy job deploys exactly the commits the plan showed — the production
    reviewer approved that preview, and the head may have moved since."""
    if planned is None:
        return head, ""
    if repo not in planned:
        return None, "not in the approved plan"
    sha = planned[repo]
    if sha != head:
        return sha, (f" · head moved to {head[:8]} after the plan — deploying the planned commit; "
                     f"for the newer one {follow_up(auto)}")
    return sha, ""


_CONSTRUCTOR = re.compile(r"^([-+].*|@@.*@@.*)function __construct\b", re.M)
_REMOVED_SERVICE = re.compile(r"^-.*<service\s+id=", re.M)
_UPDATE_HOOK = re.compile(r"^([-+].*|@@.*@@.*)function (post)?[Uu]pdate\b", re.M)


def preflight(plugin: str, files: list[dict]) -> tuple[list[str], list[str]]:
    """GitHub compare `files` -> (trap reasons, dump reasons).

    Trap = the upload request will likely 500 on its stale container (recovered
    by the refresh step). Dump = the release changes data on update, so a fresh
    DB dump before a prod deploy is recommended (optional step).
    """
    traps, dumps = [], []
    base_class = f"src/{plugin}.php"
    for row in files:
        name, status, patch = row.get("filename", ""), row.get("status", ""), row.get("patch")
        is_php = name.endswith(".php") and name.startswith("src/")
        if is_php and status in ("removed", "renamed"):
            traps.append(f"class file {status}: {row.get('previous_filename') or name}")
        if name.endswith("services.xml") and patch and _REMOVED_SERVICE.search(patch):
            traps.append(f"service id removed in {name}")
        if is_php and patch and _CONSTRUCTOR.search(patch):
            traps.append(f"constructor changed: {name}")
        if is_php and status == "added" and name.startswith("src/Migration/"):
            dumps.append(f"new migration {name}")
        if name == base_class and patch and _UPDATE_HOOK.search(patch):
            dumps.append(f"update()/postUpdate() changed in {name}")
        if is_php and status == "modified" and patch is None:
            traps.append(f"no diff from GitHub for {name} (too large) — check by hand")
    return traps, dumps


def landed(ext: dict | None, new_version: str) -> bool:
    """After upload + refresh: is the new code the one Shopware will update to?
    Same-version redeploy: nothing to upgrade, update re-runs migrations/assets."""
    if ext is None:
        return False
    if ext.get("latestVersion") == new_version:
        return True
    return not ext.get("latestVersion") and ext.get("version") == new_version


# ---------------------------------------------------------------------- GitHub


class GitHub:
    def __init__(self, token: str, org: str) -> None:
        self.token, self.org = token, org

    def _headers(self, token: str | None = None) -> dict:
        return {
            "Authorization": f"Bearer {token or self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "goetz-fleet-ci-deploy",
        }

    def get(self, path: str, token: str | None = None):
        return http(f"{GITHUB_API}{path}", headers=self._headers(token))

    def post(self, path: str, payload: dict, token: str | None = None):
        return http(f"{GITHUB_API}{path}", headers=self._headers(token),
                    data=json.dumps(payload).encode(), method="POST")

    def head(self, repo: str, branch: str | None) -> tuple[str, str]:
        # GitHub answers 404 for a repo the token cannot see, too — say both.
        try:
            if branch is None:
                branch = self.get(f"/repos/{self.org}/{repo}").get("default_branch") or "main"
            commit = self.get(f"/repos/{self.org}/{repo}/commits/{branch}")
        except HttpError as error:
            if error.code in (404, 422):
                raise Fail(f"{repo}: branch {branch or 'default'} not found, or the token has no access "
                           f"to the repo") from error
            raise
        return commit["sha"], branch

    def composer_version(self, repo: str, sha: str) -> str:
        blob = self.get(f"/repos/{self.org}/{repo}/contents/composer.json?ref={sha}")
        version = json.loads(base64.b64decode(blob["content"]).decode()).get("version")
        if not version:
            raise Fail(f"{repo}: composer.json at {sha[:8]} has no version field")
        return version

    def ci_runs(self, repo: str, sha: str, branch: str) -> list[dict]:
        """Actions API, not check-runs: the fleet-read PAT has no `Checks: read`."""
        return self.get(f"/repos/{self.org}/{repo}/actions/runs?head_sha={sha}&event=push"
                        f"&branch={branch}&per_page=100").get("workflow_runs", [])

    def compare_files(self, repo: str, base: str, head: str) -> list[dict]:
        return self.get(f"/repos/{self.org}/{repo}/compare/{base}...{head}").get("files", [])


class DeployRecord:
    """Last deployed sha per plugin = GitHub Deployments in THIS repo
    (environment `fleet-<target>`, task `deploy:<Plugin>`), written with the
    workflow's own GITHUB_TOKEN. No state in the shop, no extra token."""

    def __init__(self, gh: GitHub, repo: str, token: str, ref: str, target: str) -> None:
        self.gh, self.repo, self.token, self.ref = gh, repo, token, ref
        self.environment = f"fleet-{target}"

    def last(self, plugin: str) -> dict | None:
        """Payload `{plugin, sha, version}` of the newest SUCCESSFUL deploy."""
        rows = self.gh.get(f"/repos/{self.repo}/deployments?environment={self.environment}"
                           f"&task=deploy:{plugin}&per_page=20", token=self.token)
        for row in rows:
            statuses = self.gh.get(f"/repos/{self.repo}/deployments/{row['id']}/statuses?per_page=1",
                                   token=self.token)
            if statuses and statuses[0].get("state") == "success":
                return row.get("payload") or None
        return None

    def write(self, plugin: str, sha: str, version: str, ok: bool) -> None:
        """Never fails the deploy: a missing record only costs a redeploy next run."""
        try:
            self._write(plugin, sha, version, ok)
        except HttpError as error:
            print(f"::warning title=deploy record::{plugin}: {error}")

    def _write(self, plugin: str, sha: str, version: str, ok: bool) -> None:
        row = self.gh.post(f"/repos/{self.repo}/deployments", {
            "ref": self.ref,
            "task": f"deploy:{plugin}",
            "environment": self.environment,
            "auto_merge": False,
            "required_contexts": [],
            "payload": {"plugin": plugin, "sha": sha, "version": version},
            "description": f"{plugin} {version} @ {sha[:8]}",
        }, token=self.token)
        self.gh.post(f"/repos/{self.repo}/deployments/{row['id']}/statuses",
                     {"state": "success" if ok else "failure"}, token=self.token)


# ------------------------------------------------------------------------- shop


class Shop:
    """Admin API with the narrow deploy integration: `system.plugin_maintain`
    + `system.plugin_upload` + `system:clear:cache` (core route ACLs)."""

    def __init__(self, url: str, client_id: str, secret: str) -> None:
        self.url, self.client_id, self.secret = url.rstrip("/"), client_id, secret
        self._token: str | None = None

    def _auth(self) -> str:
        if self._token is None:
            result = http(f"{self.url}/api/oauth/token",
                          headers={**SHOP_ACCEPT, "Content-Type": "application/json"},
                          data=json.dumps({"grant_type": "client_credentials",
                                           "client_id": self.client_id,
                                           "client_secret": self.secret}).encode(),
                          method="POST")
            self._token = result.get("access_token")
            if not self._token:
                raise Fail("Admin API returned no access_token")
        return self._token

    def call(self, method: str, path: str, *, data: bytes | None = None,
             content_type: str = "application/json", timeout: int = 120):
        headers = {**SHOP_ACCEPT, "Authorization": f"Bearer {self._auth()}"}
        if data is not None or method in ("POST", "PUT"):
            headers["Content-Type"] = content_type
        return http(f"{self.url}{path}", headers=headers, data=data, method=method, timeout=timeout)

    def extensions(self) -> dict[str, dict]:
        rows = self.call("GET", "/api/_action/extension/installed")
        return {row["name"]: row for row in rows if row.get("type", "plugin") == "plugin"}

    def upload(self, name: str, payload: bytes) -> str | None:
        """Returns a warning instead of raising on 5xx: the removed-class /
        constructor trap 500s AFTER the files are extracted (the same request
        refreshes on its stale container). The refresh step recovers it."""
        boundary = uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                f"filename=\"{name}.zip\"\r\nContent-Type: application/zip\r\n\r\n").encode() \
            + payload + f"\r\n--{boundary}--\r\n".encode()
        try:
            self.call("POST", "/api/_action/extension/upload", data=body,
                      content_type=f"multipart/form-data; boundary={boundary}",
                      timeout=UPLOAD_TIMEOUT)
        except HttpError as error:
            if error.code >= 500:
                return str(error)
            raise
        return None

    def refresh(self) -> None:
        self.call("POST", "/api/_action/extension/refresh")

    def install(self, name: str) -> None:
        self.call("POST", f"/api/_action/extension/install/plugin/{name}", timeout=UPDATE_TIMEOUT)

    def activate(self, name: str) -> None:
        self.call("PUT", f"/api/_action/extension/activate/plugin/{name}", timeout=UPDATE_TIMEOUT)

    def update(self, name: str) -> None:
        self.call("POST", f"/api/_action/extension/update/plugin/{name}", timeout=UPDATE_TIMEOUT)

    def clear_cache(self) -> None:
        self.call("DELETE", "/api/_action/cache", timeout=UPDATE_TIMEOUT)


# ---------------------------------------------------------------------- deploy


def run(argv: list[str]) -> None:
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    if result.returncode != 0:
        print(result.stdout.decode("utf-8", "replace").rstrip())
        shown = " ".join(a if "x-access-token" not in a else "<remote>" for a in argv[:6])
        raise Fail(f"command failed (exit {result.returncode}): {shown}")


def build_zip(token: str, org: str, repo: str, sha: str) -> bytes:
    """git archive at the gated sha — same semantics as ~/bin/git_archive
    (`--prefix=<Name>/`, .gitattributes export-ignore honoured)."""
    workdir = f"/tmp/fleet-deploy/{repo}"
    subprocess.run(["rm", "-rf", workdir], check=False)
    os.makedirs(workdir)
    git = ["git", "-C", workdir]
    run(git + ["init", "-q"])
    run(git + ["remote", "add", "origin", f"https://x-access-token:{token}@github.com/{org}/{repo}.git"])
    run(git + ["fetch", "-q", "--depth", "1", "origin", sha])
    path = os.path.join(workdir, f"{repo}.zip")
    run(git + ["archive", "--format=zip", "-9", f"--prefix={repo}/", "-o", path, sha])
    with open(path, "rb") as handle:
        return handle.read()


def deploy_one(shop: Shop, repo: str, action: str, payload: bytes,
               new_version: str, was_active: bool) -> list[str]:
    """Upload → refresh → (install + activate | update) → verify. Returns notes."""
    notes = []
    warning = shop.upload(repo, payload)
    if warning:
        notes.append("upload answered 5xx (stale-container trap) — recovered via refresh")
        print(f"::warning title=upload 5xx::{repo}: {warning}")
    shop.refresh()

    ext = shop.extensions().get(repo)
    if action == "INSTALL":
        if ext is None or ext.get("version") != new_version:
            raise Fail(f"{repo}: not listed at {new_version} after upload + refresh")
        shop.install(repo)
        shop.activate(repo)
    else:
        if not landed(ext, new_version):
            raise Fail(f"{repo}: upload did not land — shop shows version "
                       f"{(ext or {}).get('version')} / upgrade {(ext or {}).get('latestVersion')}")
        try:
            shop.update(repo)
        except HttpError as error:
            # Verify below still runs: a failed update() makes Shopware
            # DEACTIVATE the plugin, and that must be reported as such.
            notes.append(f"update failed: {error}")

    ext = shop.extensions().get(repo) or {}
    expect_active = True if action == "INSTALL" else was_active
    if expect_active and not ext.get("active"):
        raise Fail(f"{repo}: plugin is INACTIVE after the deploy — Shopware deactivates a plugin "
                   f"whose update fails; the feature is OFF on the shop. {'; '.join(notes)}")
    if ext.get("version") != new_version or ext.get("latestVersion"):
        raise Fail(f"{repo}: shop reports {ext.get('version')} (upgrade pending "
                   f"{ext.get('latestVersion')}), expected {new_version}. {'; '.join(notes)}")
    return notes


def storefront_probe(url: str) -> str:
    """IPv4 forced: the maintenance whitelist covers the runner's IPv4
    (193.203.3.178), not its IPv6 prefix — v6 gets a 503 artefact."""
    return subprocess.run(
        ["curl", "-4", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "30", url],
        stdout=subprocess.PIPE, check=False,
    ).stdout.decode().strip()


def summary(lines: list[str]) -> None:
    text = "\n".join(lines) + "\n"
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)
    print(text)


def preflight_warnings(gh: GitHub, repo: str, base: str | None, sha: str) -> tuple[str, list[str]]:
    """-> (note, warnings) vs. the last deployed commit of this target."""
    if not base:
        return " · pre-flight skipped (no deploy record yet)", []
    traps, dumps = preflight(repo, gh.compare_files(repo, base, sha))
    warnings = [f"trap expected: {t}" for t in traps[:4]]
    warnings += [f"fresh DB dump recommended: {d}" for d in dumps[:4]]
    return "", warnings


def set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def publish(target: str, mode: str, rows: list[dict], extra: dict | None = None) -> None:
    """Every warning as a run annotation + deploy-report.json (uploaded as an
    artifact): job summaries are not readable over the REST API, these are."""
    for row in rows:
        for warning in row["warnings"]:
            print(f"::warning title={row['plugin']}::{warning}")
    report = {"target": target, "mode": mode, "plugins": rows, **(extra or {})}
    with open(os.environ.get("REPORT_PATH", "deploy-report.json"), "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)


def table(rows: list[dict], columns: list[tuple[str, str]]) -> list[str]:
    lines = ["| " + " | ".join(title for title, _ in columns) + " | Note |",
             "|" + "---|" * (len(columns) + 1)]
    for row in rows:
        parts = ([row["note"].strip(" ·")] if row["note"] else []) + [f"⚠ {w}" for w in row["warnings"]]
        note = " · ".join(parts)
        if row.get("sha"):
            note += f" ({row['branch']} @ {row['sha'][:8]})"
        cells = [str(row.get(key) or "-") for _, key in columns]
        cells[columns.index(("Action", "action"))] = f"**{row['action']}**"
        lines.append("| " + " | ".join(cells) + f" | {note} |")
    return lines


NOT_LISTED = "not on this target's list in fleet.json `targets`"


def row(plugin: str, action: str, note: str, **fields) -> dict:
    return {"plugin": plugin, "action": action, "note": note, "warnings": [], **fields}


NAMED_RUN_WARNING = ("a named (not all-pending) run replaces any waiting push-triggered deploy of this target — "
                     "what that run would have deployed waits for the next push or an all-pending run")


def plan_only(gh: GitHub, record: DeployRecord, manifest: dict, target: str, plugins: list[str],
              named_live_run: bool = False) -> int:
    rules = TARGETS[target]
    auto = auto_deploy_on(target)
    rows = []
    for repo in plugins:
        if not allowed_on(manifest, target, repo):
            rows.append(row(repo, "SKIP", NOT_LISTED))
            continue
        try:
            sha, branch = gh.head(repo, rules["branch"])
        except Fail as error:
            rows.append(row(repo, "SKIP", str(error)))
            continue
        new_version = gh.composer_version(repo, sha)
        green, ci_note = wait_for_ci(gh, repo, sha, branch, auto=auto)
        last = record.last(repo)
        action, note, warnings = plan_decide(target, last, new_version, sha, green, ci_note)
        if action == "PENDING":
            extra_note, extra = preflight_warnings(gh, repo, (last or {}).get("sha"), sha)
            note, warnings = note + extra_note, warnings + extra
        entry = row(repo, action, note, sha=sha, branch=branch, version=new_version,
                    deployed=(last or {}).get("version"))
        entry["warnings"] = warnings
        rows.append(entry)

    pending = any(r["action"] == "PENDING" for r in rows)
    lines = [f"## Fleet deploy plan → {target}", "",
             "GitHub data only — the deploy job re-checks every row against the shop.", ""]
    lines += table(rows, [("Plugin", "plugin"), ("last deployed", "deployed"),
                          ("dev" if rules["branch"] else "default branch", "version"), ("Action", "action")])
    if named_live_run:
        print(f"::warning title=named run::{NAMED_RUN_WARNING}")
        lines += ["", f"⚠ {NAMED_RUN_WARNING}"]
    if pending:
        lines += ["", "Pending — the deploy job follows"
                  + (" after the reviewer's approval." if target == "production" else ".")]
    else:
        lines += ["", "Nothing pending — no deploy job."]
    summary(lines)
    publish(target, "plan", rows, {"pending": pending})
    set_output("pending", "true" if pending else "false")
    set_output("shas", json.dumps({r["plugin"]: r["sha"] for r in rows if r["action"] == "PENDING"}))
    return 0


def main() -> int:
    target = env("INPUT_TARGET")
    if target not in TARGETS:
        raise Fail(f"unknown target {target!r}")
    rules = TARGETS[target]
    mode = env("INPUT_MODE", required=False) or "deploy"
    if mode not in ("plan", "deploy"):
        raise Fail(f"unknown mode {mode!r}")
    dry_run = env("INPUT_DRY_RUN", required=False).lower() != "false"

    manifest_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fleet.json")
    with open(manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    plugins = resolve_plugins(manifest, env("INPUT_PLUGINS"))

    fleet_token = env("FLEET_READ_TOKEN")
    gh = GitHub(fleet_token, manifest["org"])
    record = DeployRecord(gh, env("GITHUB_REPOSITORY"), env("GITHUB_TOKEN"),
                          env("GITHUB_SHA"), target)
    if mode == "plan":
        named_live = env("INPUT_PLUGINS").strip() != "all-pending" and not dry_run
        return plan_only(gh, record, manifest, target, plugins, named_live)

    raw_plan = env("PLANNED_SHAS", required=False)
    if not raw_plan and os.environ.get("GITHUB_ACTIONS") == "true":
        # fail closed: without the plan's list the job would deploy heads nobody approved
        raise Fail("PLANNED_SHAS is missing — the deploy job only deploys the commits of its plan job")
    planned = json.loads(raw_plan) if raw_plan else None
    auto = auto_deploy_on(target)
    shop = Shop(env("SHOP_URL"), env("SHOP_API_CLIENT_ID"), env("SHOP_API_CLIENT_SECRET"))
    installed = shop.extensions()

    rows = []
    for repo in plugins:
        current = installed.get(repo)
        deployed = (current or {}).get("version")
        if not allowed_on(manifest, target, repo):
            rows.append(row(repo, "SKIP", NOT_LISTED, deployed=deployed))
            continue
        try:
            head, branch = gh.head(repo, rules["branch"])
        except Fail as error:
            rows.append(row(repo, "SKIP", str(error), deployed=deployed))
            continue
        sha, pick_note = pick_commit(planned, repo, head, auto)
        if sha is None:
            rows.append(row(repo, "SKIP", pick_note, deployed=deployed))
            continue
        new_version = gh.composer_version(repo, sha)
        green, ci_note = wait_for_ci(gh, repo, sha, branch, auto=auto)
        ci_note += pick_note
        last_sha = (record.last(repo) or {}).get("sha")
        action, note, warnings = decide(current, new_version, sha, last_sha, green, ci_note)
        if action != "SKIP":
            extra_note, extra = preflight_warnings(gh, repo, last_sha, sha)
            note, warnings = note + extra_note, warnings + extra
            if action == "INSTALL":
                warnings.insert(0, f"FIRST INSTALL + activate on {target}")
        entry = row(repo, action, note, sha=sha, branch=branch, version=new_version, deployed=deployed,
                    was_active=bool((current or {}).get("active")))
        entry["warnings"] = warnings
        rows.append(entry)

    columns = [("Plugin", "plugin"), (target, "deployed"),
               ("dev" if rules["branch"] else "default branch", "version"), ("Action", "action")]
    lines = [f"## Fleet deploy → {target} ({'DRY RUN — nothing changed' if dry_run else 'LIVE'})", ""]
    lines += table(rows, columns)
    plan = [r for r in rows if r["action"] != "SKIP"]

    if dry_run or not plan:
        lines += ["", "Nothing to deploy." if not plan else "Dry run — re-dispatch with dry_run=false."]
        summary(lines)
        publish(target, "dry-run" if dry_run else "deploy", rows)
        return 0

    failed = False
    for entry in plan:
        repo, sha, new_version = entry["plugin"], entry["sha"], entry["version"]
        print(f"::group::{entry['action']} {repo} {entry['deployed'] or '-'} -> {new_version}")
        try:
            notes = deploy_one(shop, repo, entry["action"],
                               build_zip(fleet_token, manifest["org"], repo, sha),
                               new_version, entry["was_active"])
            entry["result"], entry["result_notes"] = "deployed", notes
            record.write(repo, sha, new_version, ok=True)
        except (Fail, HttpError) as error:
            failed = True
            entry["result"], entry["result_notes"] = "FAILED", [str(error)[:300]]
            print(f"::error title=deploy failed::{repo}: {error}")
            record.write(repo, sha, new_version, ok=False)
        print("::endgroup::")
        if failed:
            break

    done = [e for e in plan if e.get("result")]
    not_started = [e["plugin"] for e in plan if not e.get("result")]
    if any(e["result"] == "deployed" for e in done):
        shop.clear_cache()

    lines += ["", "### Result", "", "| Plugin | From | To | State | Notes |", "|---|---|---|---|---|"]
    lines += [f"| {e['plugin']} | {e['deployed'] or '-'} | {e['version']} | "
              f"{'✅ deployed' if e['result'] == 'deployed' else '❌ FAILED'} | {'; '.join(e['result_notes'])} |"
              for e in done]
    if not_started:
        lines += ["", f"Not started (batch stopped at the first failure): {', '.join(not_started)}"]

    probes = {}
    for url in [u.strip() for u in env("STOREFRONT_PROBE_URLS", required=False).split(",") if u.strip()]:
        code = storefront_probe(url)
        probes[url] = code
        lines.append(f"\nStorefront probe (IPv4) {url}: HTTP **{code}**")
        if code != "200":
            failed = True
            print(f"::error title=storefront::{url} expected 200, got {code}")

    summary(lines)
    publish(target, "deploy", rows, {"not_started": not_started, "probes": probes, "failed": failed})
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (Fail, HttpError) as error:
        print(f"::error title=deploy aborted::{error}")
        sys.exit(1)
