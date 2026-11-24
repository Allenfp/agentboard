# Tasks

Order is load-bearing: `model` → `store` → `api` → `cli` → `skill` → `ui` →
docs/fixtures. The UI must not start before the CLI can produce a claim, or it
gets built against an invented payload shape. Run the suite after every group.

## 1. model: schema and rules

- [x] 1.1 `VERSION = 2`; delete `rev` entirely (`bump_rev`, `check_rev`, `rev` from `TOP_KEYS` and `KEY_ORDER`); a `version: 1` document is refused with the existing message shape plus the `run 'yamlboard migrate'` line
- [x] 1.2 Add `CLAIM_KEYS = ("agent", "at", "expires", "paths", "note", "preempted", "overridden_by")`; add `claim`, `blocked_by`, `released_by` to `CARD_KEYS`; add `lease_ttl` to `BOARD_KEYS`; extend `KEY_ORDER` so every new key has a deterministic position
- [x] 1.3 Claim liveness helpers on `model`: `claim_live(claim, now)`, `ttl` parser (`90s|5m|2h|1d`, default 30m, clamped 30s–24h), board `lease_ttl` override
- [x] 1.4 `blocked_by` validation: ids must exist; DFS cycle check whose message names the full cycle path in order
- [x] 1.5 `paths_overlap(a, b)`: normalise (reject absolute/`..`), prefix containment both ways, `fnmatch`-segment compatibility (`*`, `**`, literal remainder), ambiguous ⇒ overlap; plus a helper that expands a pattern against the working tree for the teaching error
- [x] 1.6 `TTL floors` are enforced at the CLI edge, not silently clamped — an out-of-range `--ttl` is refused stating the range
- [x] 1.7 Tests: version refusal, every key tuple round-trips, liveness across `model.now()`, cycle naming for 2-card and 3-card cycles, overlap truth table (`src/api/**` vs `src/api/routes/**` yes, vs `docs/**` no, `src/api` bare vs `src/api/routes.py` yes), ttl parsing and refusal bounds

## 2. store: directory layout

- [x] 2.1 `Store(root)`: discover `.yamlboard/` by walking up; `board.yaml`, `cards/<ID>.yaml`, `archive/<ID>.yaml`; `init` writes a valid starter store and prints the next two commands
- [x] 2.2 Per-file token (`st_mtime_ns` + `st_size`); `read_card`/`read_board`; per-file CAS through `Tx.tokens` reusing the existing byte-compare no-op short-circuit; keep temp-file + `os.replace` + `fcntl` discipline, lock at `.yamlboard/.lock`, temps at `.yamlboard/.tmp-*.yaml`
- [x] 2.3 `next_card` minting keeps `max(counter, highest+1)`; duplicate id across `cards/` and `archive/` is reported, never silently resolved
- [x] 2.4 Resolved-document assembly for the API payload (one pass, cards in file order) and `check` support: validate the whole store, report per-file problems
- [x] 2.5 `.gitignore`: replace `.yamlboard*` with `.yamlboard/.lock` + `.yamlboard/.tmp-*.yaml`; test that `git check-ignore` does NOT ignore `board.yaml` or `cards/WEB-1.yaml` and DOES ignore the lock and a temp
- [x] 2.6 Tests: unrelated card writes touch one path each; byte-identical write performs no filesystem write; stale token on one card does not refuse a write to another; `init` output; duplicate-id detection

## 3. api: operations

- [x] 3.1 `ClaimDenied` exception next to `BadRequest`; identity resolution (flag → `$YAMLBOARD_AGENT` → `git config user.email` local part), validated against `SLUG_RE` and membership in `people`, never auto-created
- [x] 3.2 Ops: `claim.create`, `claim.renew`, `claim.release`, `card.done`, `card.dep.add`, `card.dep.remove`, `conflicts.resolve` — claim writes never touch `updated:`; re-claim with same paths is a zero-byte no-op; `done` into an `is_done` column releases the claim; claim sets `assignee`, release clears it
- [x] 3.3 Denial payloads carry holder, `expires`, minutes left, colliding patterns with their holder, unfinished blockers, and a `next` command naming the caller
- [x] 3.4 Ready queue: unclaimed-or-expired ∧ no unfinished blocker ∧ paths free; ordered priority then created; `--paths` pre-filter
- [x] 3.5 `conflicts(tx, dirty)` read-only: overlapping live claims with deterministic winner (earliest `at`, tie → lexicographically smaller agent), duplicate ids, claims matching dirty tracked files; `conflicts.resolve` writes `overridden_by` and is idempotent
- [x] 3.6 Migrate: read a `version: 1` single-file board, write the directory store preserving order, ids, custom fields, `archived` → `archive/`; refuse when `.yamlboard/` already exists
- [x] 3.7 `server.Handler._route`: map `ClaimDenied` → 409 with `claim_denied: true` and **no document**, kept separate from `api.Conflict`'s 409 ("409 is a reload" stays pinned); `/api/state` carries `expires`, per-card conflict flags, and agent counts
- [x] 3.8 Tests: denial field completeness, ready-queue exclusion cases, winner determinism regardless of query order, resolve idempotence, migrate fidelity against the example board (`TestMigrateFidelity`), and the pinned rule that a claim write leaves `updated:` untouched

## 4. cli: verbs

- [x] 4.1 Replace the flat flag parser with subparsers. Verbs: `init serve create check show list ready claim renew release done dep conflicts migrate skill`. Delete `--check`, `--print`, and the positional file argument
- [x] 4.2 Exit codes `0|1|2|3|130|141` with 3 = claim denied only (an unknown agent name is 2); document them in the `--help` epilog; wire `141` for broken pipes
- [x] 4.3 Every verb takes `--json`: one JSON document on **stdout**; human output on stderr; `--json` denial has `ok: false` and the same fields, same exit code
- [x] 4.4 Denied-claim stderr: holder + time left, colliding paths, blockers, then `next: yamlboard ready --as <caller>`
- [x] 4.5 Echo `as <agent>` before any mutating write; bad `--paths` produces the teaching message naming the file count and the `/**` form
- [x] 4.6 `show <id>` answers ready / taken / blocked / yours with the reason; `skill install` / `skill path`

## 5. skill

- [x] 5.1 `src/yamlboard/skills/yamlboard/SKILL.md`: frontmatter `name`, trigger-shaped `description`, `metadata.commands` listing all 15 verbs; body ≈80 lines — the loop with exact commands, exit-code table stating "3 means someone else holds it: take other work, never retry, never `--force`", losing a conflict, lease choice and duty to renew, hard rules (never edit inside another agent's claimed paths, never commit another agent's staged files, pull before claiming and after finishing). CLI only; no HTTP API, `serve` mentioned only as "a human may be watching"
- [x] 5.2 `pyproject.toml`: `package-data = ["static/*", "skills/*/*"]`; verified present in a built wheel (`uv build --wheel` → `yamlboard/skills/yamlboard/SKILL.md`)
- [x] 5.3 `skill install` → `.agents/skills/yamlboard/SKILL.md` in the current repo, creating directories, refusing on divergent bytes without `--force`, printing the `AGENTS.md` block; `skill path` resolves through the installed package
- [x] 5.4 Do **not** add a copy under this repo's `.agents/skills/` (OpenSpec owns it)
- [x] 5.5 Anti-drift test: packaged `metadata.commands` == `VERBS`; every exit code in the skill's table appears in the `--help` epilog; the file resolves through the installed package, never a repo-relative read
- [x] 5.6 Test the install round-trip: creates byte-identical file, second run on an edited copy exits 2 leaving bytes untouched

## 6. ui

- [x] 6.1 Claim chip `.claim` in `cardTile`'s `.card-foot`, surviving `data-density="minimal"`; `avatar(agent)` + countdown; `title` = holder · expiry · paths; unknown id falls back to the existing `chip(null)` marker
- [x] 6.2 `paintLeases()`: one `setInterval`, patches text nodes only, never `render()`, never mutates `S.state`; clock offset from server `expires`; amber below 25% of that claim's own TTL, then `expired`; clamped at expired; paused when `document.visibilityState !== "visible"`
- [x] 6.3 Expiry toast per transition into expired, deduped on `cardId + claim.at`; first load seeds the set and emits nothing
- [x] 6.4 Reload toast compares per card and stays silent for claim-only changes unless that card is open (fix: `assignee` moves caused by `claim`/`release` are coordination, not content - see `assigneeChange()`)
- [x] 6.5 `.overlaps` treatment: left edge + chip in the error colour, `title` naming both agents and patterns; `Conflicts (N)` control in the tools row (badge child like `els.filterBadge`), dialog listing each pair with winner/loser and one Resolve per pair, control removed entirely at zero
- [x] 6.6 Blocked cards dimmed with blocker ids in the chip; ready cards get the accent border
- [x] 6.7 Agent `<select>` from `people` in the toolbar's right group, persisted as `yb.agent`, default first person, `aria-label` naming the shortcut it feeds; claim/release/resolve go through `send()` and adopt the returned state
- [x] 6.8 Shortcuts `r` (ready filter) and `c` (claim focused card as the selected agent) in all three copies in the same commit: keydown handler, `openHelp` rows, README
- [x] 6.9 CSS token `--claim` declared in **both** themes with measured contrast ratios in the comment; `.claimed`/`.overlaps` as state classes; add `--claim` to the sanctioned JS-supplied list; focus trap/`aria-modal`/`#root.inert` come free by routing every new control through `openModal`
- [x] 6.10 Empty state names both jobs (create a card; `yamlboard skill install`); footer shows file path, agent count, conflict count
- [x] 6.11 Enum-drift extension covers the new enums (verbs, exit codes, the skill's `metadata.commands`) - `TestAntiDrift`
- [ ] 6.12 A DOM-level test that a claim-only refresh does not raise the reload toast. **No automated runner exists** for `app.js` (the project ships Python plus a real browser, no JS test harness, by design), so this behaviour is currently proven only by the live browser run in 8.10, not pinned by a test. A `movedFiles` unit test would need `app.js` loadable in a JS runtime.

## 7. docs, fixtures, packaging

- [x] 7.1 Rewrite `README.md`: tagline and `pyproject` description/keywords drop `kanban`, add `agent-coordination`; the "single user, single file" limit is gone; new quick start (`init` → `skill install` → `claim`); exit codes incl. 3; keyboard list synced with the two JS copies; the `AGENTS.md` block; the file-layout section replaces "The file"
- [x] 7.2 Rewrite `docs/CONVENTIONS.md`: module banners for the new layout, the per-file token contract replacing `rev`+`token`, "409 is a reload" plus the new `claim_denied` carve-out, the Artefacts contract naming `.yamlboard/.lock` and the narrowed ignore lines, the enum-drift rule extended to verbs and the skill, `--claim` in the custom-property list, and P35's argv-count claim updated for the one new git call
- [x] 7.3 `examples/boards.example.yaml` → `examples/demo/.yamlboard/` with board config, cards including a claimed one, a blocked pair, and an overlapping-claim pair for the conflict UI
- [x] 7.4 `install.sh`: fix the printed example hint (a directory is not one `curl`) to `yamlboard init` or a shallow clone + `cp -r examples/demo/.yamlboard ./`; keep the `RAW_BASE` derivation and the `https://github.com` guard verbatim
- [x] 7.5 Screenshots of the kanban board were stale and had no generator: the images and the README `<img>` lines are **deleted**, not faked. UI proof lives as browser-observed results in 8.10.
- [x] 7.6 Grep sweep: no `kanban` in README/pyproject, no `.yamlboard*` in `.gitignore`, no absolute local paths or usernames in tracked files

## 8. Verification

- [x] 8.1 `openspec validate agent-coordination --strict` → exit 0
- [x] 8.2 Fresh store: `init` creates `board.yaml` + `cards/`; `check` exits 0; `check` outside a store exits 2 saying it does not exist
- [x] 8.3 Mutual exclusion: claim `src/api/**` as ada → 0; same card as grigory → 3 naming holder; `src/api/routes/**` on another card → 3; `docs/**` → 0
- [x] 8.4 Expiry via patched `model.now()` (no sleeps in CI): patched forward 31 min, `ready` lists the card, second agent's claim records `preempted: ada`; the 30s floor proven the same way (claim minted under a skewed clock so it is already lapsed at read)
- [x] 8.5 Cross-machine in real git (two clones off a bare origin, overlapping claims commit and merge with **zero** textual conflicts, then `conflicts` names both and picks the earlier `at`; `--resolve` marks `overridden_by`, the card returns to `ready`, and an unrelated `docs/**` claim is untouched)
- [x] 8.6 Quiet diffs: second identical claim leaves `git status` clean; a renew is exactly `1 insertion(+), 1 deletion(-)`; no claim write touches `updated:`
- [x] 8.7 Cycle refusal exit 2 naming both ids
- [x] 8.8 Suite green including extended `TestEnumDrift`, verb-registry pin, and the `SKILL.md` anti-drift test; `uv tool install --force .` then `yamlboard --version`
- [x] 8.9 Skill install round-trip in a scratch repo (byte-identical, `AGENTS.md` block, exit 2 on edited copy with bytes untouched)
- [x] 8.10 Browser-driven UI proof (WebKit, live server on a scratch board): `c` POSTed `claim.create` as the toolbar agent and the chip appeared; the countdown went `12s → 11s → 10s → 9s` over 3.0 s with exactly one `/api/` poll in the window and the chip's text node never rebuilt; `r` took the view from 9 cards to 5; a `--ttl 30s` lease toasted **once** across the ~4 polls that followed its expiry and **zero** times on reload after it was already dead; `Conflicts (2)` opened the dialog naming both agents and both globs, and Resolve removed the control from the toolbar entirely; the chip was visible at `data-density="minimal"`; the overlap colour measured 6.86:1 (dark) / 6.47:1 (light); focus cycled inside the dialog for 6 Tabs and `Esc` returned it to the opener.
- [x] 8.11 Screenshots captured from the live board (both themes, list view, conflict dialog) during 8.10; deliberately **not** committed to `docs/` - see 7.5, the same reason the kanban PNGs were deleted: nothing regenerates them, so a committed image rots.
- [x] 8.12 Orphan branch: `git checkout --orphan fresh && git add -A && git commit`; `git log --oneline | wc -l` == 1 and `git status` clean before `git push --force-with-lease origin fresh:main`. The pre-force-push SHA is `7eece2f0f8c8177763fe3f9424196fc48f628613`.
