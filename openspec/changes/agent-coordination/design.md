# Design

## Context

Current state is the shipped kanban app: one `boards.yaml`, modules `model.py`,
`store.py`, `api.py`, `gitops.py`, `server.py`, `cli.py`, and a single
no-build-step `static/app.js`. See `proposal.md` for motivation and `specs/` for
requirements; this document fixes the *how* so implementation does not re-decide
anything.

Constraints that shape every decision below:

- Python 3.11+, exactly one dependency (`ruamel.yaml`), stdlib `http.server`,
  vanilla JS with no bundler. This is what makes `install.sh`, `uv tool install`
  and the round-trip diff fidelity work; abandoning it is not on the table.
- No authentication anywhere. `server.is_loopback` is the entire security story.
- `docs/CONVENTIONS.md` is binding: closed key sets enforced on read (P-surface),
  "409 is a reload", the `fcntl`-serialised single write path, the narrow six-argv
  git surface, and enum-drift tests that read `app.js` from Python.
- Multi-machine topology with `git pull`/`push` performed by whoever runs git. A
  peer's loopback server is unreachable from another clone, which rules out any
  server-mediated arbitration.

## Goals / Non-Goals

**Goals:**

- Two agents in two clones can work unrelated cards with zero coordination and
  zero textual conflicts.
- Two agents can never *unwittingly* work overlapping paths in one clone; across
  clones, overlap is detected and adjudicated rather than silently resolved.
- A denial is survivable by an agent with no human in the loop.
- The rewrite lands as one coherent history, not a compat layer.

**Non-Goals:**

- Authentication, per-agent tokens, or any claim of hostile-agent safety. Claims
  are honour-system; a rogue agent can lie about its identity and its paths.
- Multi-board-per-repository. One repo, one `.yamlboard/` directory.
- Git verbs inside yamlboard: no `pull`, `fetch`, `push`, `merge`. The existing
  commit button and the foreign-staged-file guard are unchanged.
- Any compatibility path for `version: 1` reads. `migrate` is the only bridge.
- Removing features the baseline spec carries (swimlanes, list view, densities,
  custom fields, archive). The pivot adds coordination; it does not subtract a
  kanban.

**D1b — card order becomes a per-card `order:` number.** Today card order *is*
array order in the shared file, and that array is exactly the shared byte this
rewrite exists to delete — so the ordering has to move onto the card. Cards carry
`order:` as a float, spaced 1024 apart on creation; inserting between neighbours
writes `midpoint(prev, next)` and touches one file. When two neighbours are
already adjacent (no representable midpoint, i.e. float53 exhaustion after ~50
inserts in one gap), the column is renumbered wholesale — a deliberate,
rare rebalance instead of a shared counter on every drag. Ties break by
`(order, id)` so two clones that coincidentally wrote equal positions still agree
on the result.

*Rejected:* an `order: 1,2,3` integer column renumbered on every drag — that
rewrites every sibling card on any reorder, which recreates the conflict this
whole change removes. *Rejected:* a shared `columns:` ordering list in
`board.yaml` — same problem, one file per drag.

## Decisions

### D1 — Directory store, one file per card

```
.yamlboard/
  board.yaml         # version, title, people, labels, boards (id, key, title,
                     #   columns, custom_fields, next_card, lease_ttl) — no cards, no rev
  cards/WEB-3.yaml   # one card per file, claim included
  archive/WEB-1.yaml # archived cards move here
  .lock              # fcntl lock (was .yamlboard.lock)
  .tmp-*.yaml        # interrupted writes (was .yamlboard-*.tmp)
```

`board.yaml` is written rarely — columns, labels, people. A card write touches
exactly one path, so git's own merge is the concurrency control for unrelated
work. Prior art converged on this independently (`dtch1997/cairn`: "one file per
issue is the whole trick"; `gastownhall/beads` reached for Dolt).

*Rejected:* keeping one file with per-card locking — the `rev` line stays a
shared byte every writer touches, which is the exact failure being removed.
*Rejected:* JSONL append log — unreadable in a PR diff, which is the product.

**`store.py` shape.** `Store(root)` replaces the file-based `State`. Card reads
are per-file: `read_card(id) -> (doc, token)`; the lock is one directory-wide
`.lock` (an `fcntl` lock is cheap and the critical sections are microseconds).
`update_card(id, mutate, *, expected_token=None)` reuses the existing
no-op short-circuit (`store.py:196-205`: serialise, byte-compare, return without
writing) so lease churn stays quiet. `board.yaml` is updated the same way with
its own token. The existing `abort()`/temp-file/`os.replace` discipline is
carried over unchanged — a corrupt card is impossible by the same argument it is
today.

**Token** is per-file, computed exactly as `State.token` is today
(`st_mtime_ns` + `st_size`). `rev` and the `rev` bookkeeping in `model.py`
(`bump_rev`, `check_rev`, the `rev` key in `TOP_KEYS`/`KEY_ORDER`) are deleted,
not deprecated.

### D2 — `.gitignore` narrows, in the same commit

`.gitignore:17-22` currently ignores `.yamlboard*`, which would ignore the entire
new tracked directory — the board would silently be untracked. It becomes:

```
.yamlboard/.lock
.yamlboard/.tmp-*.yaml
```

`docs/CONVENTIONS.md` ("Artefacts" contract) and the README "Private boards" note
that tells people to copy the line change in the same commit. A test asserts
`git check-ignore` refuses to ignore `board.yaml` and `cards/WEB-1.yaml` —
otherwise the regression is invisible until someone loses work.

### D3 — Schema: `VERSION = 2`, closed key sets, additive only

```python
CLAIM_KEYS = ("agent", "at", "expires", "paths", "note", "preempted", "overridden_by")
CARD_KEYS  = (..., "claim", "blocked_by", "released_by")   # additions only
BOARD_KEYS = (..., "lease_ttl")
```

`KEY_ORDER` gains entries so card keys keep deterministic placement (the diff
fidelity is the product). Because `_check_keys` raises `ValidationError` **on
read**, any key used anywhere in the codebase must be in these tuples or the card
file becomes permanently unreadable — this is the single highest-consequence
constraint in the rewrite and is why `TestEnumDrift` is extended to the key
tuples.

Semantics: a claim is *live* iff `expires > model.now()` and `overridden_by` is
unset. There is deliberately **no** `status` field: a second source of truth for
"who holds this" is how a board starts lying about itself.

A `version: 1` file (or a bare `boards.yaml` where a store is expected) is
refused with the existing message shape plus `run 'yamlboard migrate' or start a
new board`.

*Rejected:* dual-format reader — a silently-dead board is a worse failure than a
loud refusal, and the user has no data worth preserving.

### D4 — Identity, and `people` as the allowlist

`identity.resolve(store, as_flag) -> str`: flag → `$YAMLBOARD_AGENT` → local part
of `git config user.email` (via `gitops`, one new argv list, still no shell).
Validated against `model.SLUG_RE` **and** membership in `board.yaml` `people`.
Unknown name → `ClaimDenied`-adjacent refusal (exit 2), never auto-create. The
resolved name is printed as `as grigory` **before** the write, so a wrong
identity is visible before it lands in history.

*Rejected:* per-agent tokens. There is no auth anywhere in the project and
`is_loopback` is the whole model; a token implies a security property this design
does not have and would pretend otherwise.

### D5 — Lease arithmetic, single clock

`model.now()` is already the only time source; every liveness decision goes
through it, which is what makes expiry testable without sleeping. `--ttl` parses
`90s|5m|2h|1d`, default 30m, clamped 30s–24h, with `board.lease_ttl` overriding
the default. `renew` sets `expires = now() + ttl`.

Re-claiming your own card with the same paths is a **no-op** — exit 0, prints
`already yours`, zero bytes written. A claim write never touches `updated:`:
that field means "someone edited this card's content", and letting coordination
traffic move it would poison both `git log` and list-view sorting.

### D6 — Glob intersection

`paths_overlap(a, b)` in `model.py`, used by both CLI and server (one
definition, per the Loopback precedent in CONVENTIONS):

1. Normalise both patterns (strip `./`, reject absolute, reject `..`).
2. Either pattern literally matches a path prefix of the other.
3. Expand `fnmatch`-style segment sets: two patterns overlap if any segment
   position is compatible (equal, one is `*`, one is `**`, or one is the literal
   remainder). `src/api/**` vs `src/api/routes/**` → overlap. `src/api/**` vs
   `docs/**` → no overlap.
4. Ambiguous → refuse. The asymmetry is deliberate: a false refusal costs one
   extra `ready` call, a false accept costs two agents' work.

`ready` also uses it to pre-filter, and `--paths` is validated against the
working tree: a bare directory with no wildcard yields the teaching message
(`--paths 'src/api' covers 41 files — end a directory pattern with /** if you
mean the whole tree`) rather than a shrug.

### D7 — Cross-machine conflicts: detect, never auto-write

Detection is read-only, always. `conflicts.detect(store)` returns structured
pairs: overlapping live claims (across cards), duplicate ids (two files that
declare the same `id`), and claims matching dirty working-tree paths (report
only). Winner = earliest `at`, tie → lexicographically smaller agent. Only
`conflicts --resolve` writes: it sets `claim.overridden_by: <winner>` on the
loser, which makes the claim not-live, which returns the card to `ready`. The
UI's Resolve button calls the same Python function through `api.py`: one
implementation, two entry points.

Idempotence matters: resolving twice, or resolving in a clone that already
received the resolved file, must be a no-op.

### D8 — CLI: subcommands, and exit code 3

`cli.py` has **no subparsers today** (flat flag parser, positional file). This is
new convention. Argparse subparsers; verbs
`init serve create check show list ready claim renew release done dep conflicts migrate skill`.

```
0 ok · 1 cannot listen · 2 bad argument / refused operation / invalid file
3 claim denied · 130 interrupted · 141 pipe closed
```

3 exists so an agent script branches on "someone else has it" without parsing
prose. `ClaimDenied` (new, in `api.py` next to `BadRequest`) is the exception;
`Handler._route` maps it to **409 with `claim_denied: true`** — kept distinct
from `store.Conflict`'s 409, because "409 is a reload" is a pinned contract and
the client must not reload-and-retry a denial it caused.

Denied output goes to **stderr** as four lines (holder + time left, colliding
paths, blockers, `next: yamlboard ready --as <you>`); with `--json` a single
document with `ok: false` goes to **stdout** instead. Same exit code either way.

### D9 — The skill ships inside the package

`src/yamlboard/skills/yamlboard/SKILL.md`, with
`[tool.setuptools.package-data] yamlboard = ["static/*", "skills/*"]`.
Non-negotiable: `[tool.setuptools.packages.find] where = ["src"]` makes anything
outside `src/yamlboard/` invisible to setuptools, and a `uv tool install`
checkout may not exist at install time — a skill at repo root would be silently
unshippable.

`skill install` copies it to the current repo's `.agents/skills/yamlboard/SKILL.md`
and prints the 5-line `AGENTS.md` block; refuses to overwrite divergent bytes
without `--force`. `skill path` prints the packaged location.

This repository does **not** also commit a copy under its own `.agents/skills/` —
OpenSpec owns that directory here (it wrote six `openspec-*` skills into it at
init) and a second hand-maintained copy is precisely the drift the rest of this
design exists to prevent.

Anti-drift test, in the existing `TestEnumDrift` style: parse the packaged
`SKILL.md`, assert `metadata.commands` == the CLI verb registry exactly, assert
every exit code in its table appears in the `--help` epilog, and resolve the file
**through the installed package** (`importlib.resources`), never a repo-relative
path — that also proves `package-data` is correct.

The skill documents the CLI only. Mentioning `serve` beyond "a human may be
watching" would steer agents at a server that need not exist.

### D10 — UI: coordination as state, not as a screen

Landing stays the classic board. Anchors verified this session (`app.js` line
numbers drift; symbols do not): claim chip in `cardTile`'s `.card-foot`
(`:833-842`), whose siblings are hidden at Minimal by `styles.css:427-436` — so
the chip needs its own class `.claim`; toolbar tools row assembled at
`app.js:397`, painted by `paintViewButtons()` (`:547`); keydown table
`:1978-2015`; help rows `:1964-1969`; reload toast in `refresh()` (`:210-232`);
`chip()` (`:875`) already tolerates an unknown id and `avatar()` (`:903`)
already handles a pale colour.

- **Countdown**: server sends absolute `expires`; client computes a clock offset
  at each poll and ticks locally. One `setInterval` calling `paintLeases()`,
  which patches text nodes only — never `render()`, never `S.state` (P20 says
  `paint*` patches, and P21 says the client never decides). Amber below 25% of
  that claim's own TTL, then `expired`; clamped, never negative; paused when
  `document.visibilityState !== "visible"`.
- **Expiry toasts** deduped on `cardId + claim.at`; first load seeds the set and
  emits nothing, so opening a board never dumps a backlog (the existing 4-toast
  cap absorbs the worst case).
- **Reload toast** compares per card, not per file, and stays silent for
  claim-only changes to a card that isn't open — otherwise N agents' renewals
  toast the human every 3 s.
- **`Conflicts (N)`** in the tools row, badge child like `els.filterBadge`; dialog
  lists each pair, winner, loser, and one Resolve per pair. Zero ⇒ removed, not
  disabled.
- **Agent identity** is a labelled `<select>` of `people` in the toolbar's right
  group, persisted as `yb.agent` (P24: prefs in `localStorage`, never the file),
  default first person, `aria-label` naming the shortcut it feeds. `c` claims the
  focused card as that identity. `r` toggles the ready filter. Both go into all
  three copies in one commit: keydown handler, `openHelp` rows, README.
- New CSS token `--claim` (fifth JS-supplied custom property after `--col`,
  `--prio`, `--c`, `--who`), declared in **both** themes with the measured
  contrast ratio in the comment (P28). State is `.claimed` / `.overlaps` classes
  (P30), never `data-*`.
- Keyboard letters `r`/`c` are from the verified-free set (current bindings:
  `n`, `/`, `a`, `b`, `l`, `d`, `f`, `s`, `t`, `?`).

### D11 — Ordering, and the cutover

Implementation order that keeps the tree honest at every commit: `model` →
`store` → `api` ops → `cli` → `ui` → docs/fixtures. The UI must not start before
the CLI can produce a claim, or the UI gets built against an invented payload
shape.

History is an orphan branch (`git checkout --orphan fresh` → one commit →
`git push --force-with-lease origin fresh:main`) because the old kanban history
describes a file format that will not exist; pre-rewrite SHA
`7eece2f0f8c8177763fe3f9424196fc48f628613` is recorded for recovery.

## Risks / Trade-offs

- **Honour-system claims.** An agent can lie about `--paths` or `--as`. → The
  post-hoc `conflicts` report against the *actual* dirty tree (D7 case 3) turns a
  lie into a visible event; `released_by`/`preempted`/`overridden_by` leave an
  attributable trail.
- **`ValidationError` on read is a brick button.** One key used in code but
  missing from a key tuple makes a real user's card permanently unreadable. →
  `TestEnumDrift` extended to every key tuple; every new key lands in the same
  commit as its test.
- **Path globs are agent-asserted and coarse.** A broad `src/**` claim starves the
  board. → `ready` output names *why* a card is not ready, so starvation is
  diagnosable; the teaching error (D6) discourages accidental breadth.
- **Cross-machine overlap is only detected after a merge.** The window is real
  and cannot be closed without a shared server, which the topology forbids. →
  Deterministic winner + explicit resolve + mandatory UI rendering; `pull` before
  claim and after finish is in the skill's hard rules to shrink the window.
- **Two clones mint the same card id.** → Detected as a duplicate filename and
  reported; never silently renumbered (renumbering breaks references).
- **ruamel round-trip on many small files** is slower than one document read. →
  Per-card reads are strictly less work than today's whole-document parse; the
  state payload still ships one resolved document, assembled in one pass.
- **A large rewrite is hard to review.** → One commit is accepted here on purpose
  (the format is being replaced wholesale); `openspec/` carries the delta trail
  that makes it legible, and archive keeps it.
- **Stale `docs/*.png`.** There is no screenshot generator in the repo. →
  Regenerate from the new example board; if that proves unfaithful, delete the
  images and the README `<img>` lines rather than ship a picture of a board that
  no longer exists.

## Migration Plan

1. `yamlboard migrate <boards.yaml>`: one pass, writing `board.yaml` +
   `cards/*.yaml`, preserving order, comments where ruamel allows, ids, and
   `archived` → `archive/`. Refuses if `.yamlboard/` already exists.
2. Serve the new store, re-screenshot, regenerate `examples/`, fix
   `install.sh`'s printed hint (a directory is not one `curl`), narrow
   `.gitignore`.
3. Full suite, `uv tool install --force .`, browser-driven UI verification.
4. Orphan-branch commit, `git push --force-with-lease origin fresh:main`.
5. `openspec archive agent-coordination` last, so `openspec/specs/` ends
   reflecting the new behaviour and the change directory becomes the record.

Rollback: the pre-rewrite SHA is on the remote's reflog and in the PR body;
`git push --force-with-lease origin 7eece2f:main` restores the kanban app.

## Open Questions

- Whether `done` should also *archive* by default when the target column has
  `is_done: true`, or keep today's "offer to archive" behaviour. Either satisfies
  the specs; decide against the UI flow when the CLI verb is written.
