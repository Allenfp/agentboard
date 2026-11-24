# Proposal

## Why

yamlboard today is a single-user board: one YAML file, one `rev:` counter, one
human. It is being repurposed as the coordination layer that lets several
autonomous LLM agents work the **same repository at the same time** without
editing each other's files. Two properties of the current design are load-bearing
for one person and fatal for N agents:

1. **One global `rev:` integer** bumped by every write, plus a file-wide `token`
   (`st_mtime_ns` + `st_size`). Two clones touching two unrelated cards still
   conflict — in the app (409) and in git, because both rewrite line 1. A single
   file cannot carry N writers.
2. **A closed key set enforced on read.** `model.TOP_KEYS` / `CARD_KEYS` make any
   unknown key a `ValidationError` that `store.read` raises, so a hand-written
   `leases:` section bricks every read, including `--check`. The schema changes,
   so `version` changes.

Both have the same fix, which prior art converged on independently
(`dtch1997/cairn`: "one file per issue is the whole trick"): **one file per card,
no global counter.** An unrelated card write is then an unrelated *path*, so git
merges it cleanly, and the card file itself carries its claim.

Path overlap across machines **cannot be prevented** — two clones each write their
own card file, merge cleanly, and both believe they hold `src/api/**`. Git offers
no arbiter. It becomes detect-and-resolve with a deterministic winner surfaced to
the human. Claims stay honour-system: there is no authentication anywhere in this
project (`is_loopback` is the whole security story) and a self-issued claim is
consistent with that.

A coordination scheme nobody knows the vocabulary for prevents no collisions, so
the **agent-facing skill is the product surface**, not documentation.

## What Changes

- **BREAKING** The board is a **directory** `.yamlboard/` inside the tracked repo
  (`board.yaml` + `cards/<ID>.yaml` + `archive/`), not one `boards.yaml`.
  **BREAKING** `rev` is removed entirely; concurrency is per-card and per-file.
- **BREAKING** `version: 2`. A `version: 1` file is refused with a pointer to
  `yamlboard migrate`. No compatibility reader, no aliases — `migrate` is the only
  bridge and it is one-shot.
- **BREAKING** The CLI becomes subcommands. `yamlboard <file>` and the
  `--check`/`--print` flags are gone: verbs are `init`, `serve`, `create`,
  `check`, `show`, `list`, `ready`, `claim`, `renew`, `release`, `done`, `dep`,
  `conflicts`, `migrate`, `skill`.
- **New** Leases: a card carries `claim:` (agent, `at`, `expires`, `paths`, note).
  A claim covers the card **and the path globs it will touch**; no second agent can
  take a claim whose globs intersect. Leases expire and expiry **is** release.
- **New** `ready` queue: `blocked_by` edges, cycle refusal, and "paths free".
- **New** Cross-machine conflict detection with a deterministic winner (earliest
  `at`) and explicit human/agent `--resolve`.
- **New** Honour-system identity: `--as <slug>` → `$YAMLBOARD_AGENT` → git
  `user.email` local part, validated against `people`, which is the allowlist.
- **New** Exit code **3 = claim denied**, distinct from 2, so an agent can branch
  on "someone else has it" without parsing prose. A denial prints the holder, the
  colliding paths, the blockers, and the next command to run.
- **New** Bundled skill at `src/yamlboard/skills/yamlboard/SKILL.md`, installed
  into a consumer repo by `yamlboard skill install`, pinned to the verb registry
  by an anti-drift test.
- The browser becomes a **live dashboard**: claim chip with a ticking countdown,
  expiry toasts, dimmed blocked cards, and a `Conflicts (N)` control a human
  resolves. It stays the classic board — no new spatial metaphor — and gains no
  `pull`/`push`.
- The CLI no longer requires a running server. **The files are the arbiter**;
  `serve` is no longer the only writer.

## Capabilities

### New Capabilities
- `store-layout`: the `.yamlboard/` directory tree, one file per card, id minting
  without a global counter.
- `claims`: card leases — take, renew, release, auto-expiry, preemption, and what
  a denial looks like.
- `path-ownership`: declaring globs with a claim, the local intersection rule, and
  detection plus resolution of cross-machine overlaps.
- `dependencies`: `blocked_by` edges, cycle rejection, and the `ready` queue.
- `agent-cli`: the verb surface, identity resolution, exit codes, `--json`, and the
  guarantee that no verb needs a server.
- `agent-skill`: the committed `SKILL.md` an agent reads, how it is distributed,
  and the drift guarantee that keeps it true.
- `board-ui`: what the browser shows about coordination, and what it refuses to do.

### Modified Capabilities
- `board`: the file *is* now a directory, the global `rev` counter is gone,
  `--check`/`--print` become the `check` verb, and the "single user, single file"
  limit is removed. Its loopback, validation-before-write, keyboard and
  commit-button requirements are unchanged.

## Impact

- **Code:** `model.py` (schema, `CLAIM_KEYS`, cycle check, glob intersection,
  `VERSION = 2`), `store.py` (directory store, per-card CAS, lock naming),
  `api.py` (claim ops), `server.py` (`ClaimDenied` → 409 `claim_denied: true`,
  kept separate from the "409 is a reload" contract), `cli.py` (subparsers — no
  subcommand exists today), `static/app.js` + `styles.css` + `index.html`.
- **Removed:** flat-flag parser, whole-file document handling, `rev` bookkeeping,
  the "single user, single file" limit in the README. No shims.
- **Cross-file contracts (CONVENTIONS.md):** `.gitignore` currently has
  `.yamlboard*`, which would ignore the entire new tracked directory — it must
  narrow to `.yamlboard/.lock` and `.yamlboard/.tmp-*` in the same commit as the
  store change, along with the "Private boards" README note. The keyboard list in
  three places, the enum-drift test (now covering verbs and the skill), and the
  exit-code doc set all change.
- **Packaging:** `[tool.setuptools.package-data]` gains `skills/*` or
  `skill install` has nothing to copy after `uv tool install`. `install.sh`'s
  printed example-fetch hint becomes invalid for a directory.
- **Tests:** `TestEnumDrift` extended; real-git cross-machine tests in the existing
  style (`gitops` is never monkeypatched); fixtures move to
  `examples/demo/.yamlboard/`.
- **Delivery:** orphan branch, `git push --force-with-lease origin fresh:main`.
  Pre-rewrite SHA `7eece2f0f8c8177763fe3f9424196fc48f628613` recorded for recovery.
- **Unchanged:** project name `yamlboard`, the `yamlboard` script entry,
  `install.sh`'s uv bootstrap, the single `ruamel.yaml` dependency,
  `requires-python >= 3.11`, loopback-only security, no build step.
