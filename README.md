# fleet-ci-lab — sandbox for the fleet CD credential model

Public lab org `webcu-fleet-lab` (GitHub Free ⇒ org secrets reach only PUBLIC repos, so every lab
repo is public and holds no real code). It rehearses the part of `goetz-fleet-ci` that changes
when the stored PATs are replaced by GitHub Apps and the plugin set becomes a self-maintained
group. The Admin-API deploy job is not here (no shop); `scripts/deploy.py` is the real one.

## Pieces

| Piece | Lab | Real (WebcustomsIT) |
|---|---|---|
| Trigger App | `webcu-lab-fleet-trigger` — Actions: write, installed on **this repo only** | same, installed on goetz-fleet-ci only |
| Reader App | `webcu-lab-fleet-reader` — Contents/Actions: read + org Secrets/Variables: write, installed on **all repos** | same |
| Group secrets (org, visibility *selected*) | `FLEET_TRIGGER_APP_ID`, `FLEET_TRIGGER_APP_KEY` | same names |
| Group variables (org, *selected*) | `CD_AUTO_DEPLOY_STAGING`, `CD_AUTO_DEPLOY_PRODUCTION` | same |
| Reader key | repo secrets here: `FLEET_READER_APP_ID`, `FLEET_READER_APP_KEY` | repo secrets in goetz-fleet-ci |
| Group definition | `fleet.json` (`fleet` + `extras`, `group`) | same file |
| Group sync | `sync-fleet-access.yml` → `scripts/sync_fleet_access.py` | to be added |

Who does what: the org owner creates the two Apps, installs them, creates the 4 group
secrets/variables (initial sharing list may be empty) and the 2 reader repo secrets — once.
From then on a new plugin = its ci.yml stub + one `fleet.json` commit; the sync shares the group
with it. Rotation = regenerate one App key, update one secret. Nothing expires.

## Flow

1. Push to `plugin-a` `dev` → its stub calls `plugin-ci.yml@main` here (`secrets: inherit` passes
   the org secrets) → test → the step mints a trigger token from the App → `POST …/deploy.yml/
   dispatches` → `Deploy` runs here (plan only in the lab).
2. `fleet.json` change on `main` → `Sync fleet access` sets the sharing lists.

## Setup order (lab)

1. Apps created + transferred to the org ✅ (ids 5393967 trigger, 5393988 reader).
2. Repos `fleet-ci-lab`, `plugin-a`, `plugin-b` (this step).
3. Install: trigger → only `fleet-ci-lab`; reader → all repositories.
4. Org owner: 2 org secrets + 2 org variables, visibility *selected*, no repos yet; both variables `false`.
   Repo secrets here: `FLEET_READER_APP_ID`, `FLEET_READER_APP_KEY` (`gh secret set NAME -R webcu-fleet-lab/fleet-ci-lab < file`).
5. Run `Sync fleet access` (dry run, then real) → the 4 group items are shared with plugin-a + plugin-b.
6. Run `Deploy` by hand (staging, all-pending, dry run) → plan reads both repos via the reader App.
7. Set `CD_AUTO_DEPLOY_STAGING=true` (org variable) → push to `plugin-a` `dev` → trigger → Deploy run appears here.
8. Add `plugin-c` to `fleet.json` → sync adds it → its first `dev` push triggers too. Remove → drops out.
