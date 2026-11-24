# Conventions

Not a style guide for its own sake. Half of these rules exist because two or
three files have to agree and nothing in the code enforces that they do — the
client's shortcut list, the in-app help table and the README's shortcut line; the
exit-code table in `cli.py`, the README and `SKILL.md`; the artefact names in
`store.py` and the two lines in `.gitignore`; the enum in `model.py` and the
dropdown in `app.js`. When you change one side, this is the list of the other
sides.

A review finding that cites a rule here names the rule id (`P17`) and the
precedent it violates. A finding that invents a new rule is a design proposal,
not a defect.

## Layout

- **P1** Every module opens with a banner docstring that says *why* the module
  exists and names the one weakness it accepts on purpose — not what it
  contains. `store.py` says editing a card touches one path so git's merge is the
  concurrency control, names `next_card` as the byte that breaks on create, and
  says card files carry no comments to preserve;
  `server.py` says every poll re-parses every card and why a cache would lie;
  `identity.py` says identity is honour-system by choice. Length is whatever
  that takes (8–25 lines now); a banner that only inventories the file has told
  the reader nothing they could not grep.
- **P2** Python section banner: a 79-column `# ---- #` rule, a lowercase 1–3
  word title, one blank line. Files under ~310 lines skip it (there is nothing
  to section); `gitops.py` and `identity.py` skip it too, each being one story
  told once, and `server.py` uses in-class banners only.
- **P3** In-class banner: `    # -- reading --------- #`, 77 columns.
- **P4** JS/CSS banner: `/* ---------------- title */` + one blank line. The
  section order in `app.js` is the layering: icons → transport → lookups →
  theme → chrome → transient layers → render → board grid → card ui → drag+drop
  → list ui → toasts → modals → new card → settings → shortcuts → bootstrap.
  New code goes under its own layer, never appended to the end of the file.
- **P5** Two blank lines around top-level defs and classes.
- **P6** Import order: `__future__` → stdlib → third-party → local → constants.
  A `# noqa` always carries the reason in a comment.

## Python

- **P7** Annotate every function, both directions.
- **P8** Return types stay coarse: `-> dict:`, `-> list:`, `Any` for an opaque
  payload, `Any | None` for "maybe not there". Never `dict[str, Any]` as a
  return type — the shape is not the point. Exception: a *local* accumulator
  that the reader needs to know is a mutable bag (`out: dict[str, Any] = {}`).
  `Optional[...]` never appears; `| None` always.
- **P9** `from __future__ import annotations` is the first statement.
- **P10** A docstring is an imperative one-liner plus the WHY. Trivial
  predicates (`_text`, `is_int`, `_one_of`) have none, on purpose.
- **P11** One exception per layer, translated once, in `Handler._route`:

  | raised | status | note |
  | --- | --- | --- |
  | `api.NotFound` | 404 | path/id the client named |
  | `api.BadRequest` | 400 | bad payload shape |
  | `model.ValidationError` | 422 | the file, with its problems |
  | `api.Conflict` | 409 | `claim_denied: false`, carries the fresh state — reload |
  | `api.ClaimDenied` | 409 | `claim_denied: true`, carries `lines` + `detail`, **no state** |
  | `store.StoreError` | 500 | disk said no; detail goes to stderr, not the browser |
  | `gitops.GitError` | 400 | `git_error: true`, message is git's, one line |

  Both 409s are refusals and they are opposite advice: one means "the bytes
  moved, re-read", the other means "someone else holds it, choose other work".
  Collapsing them is how a claim denial starts reloading the board and wiping
  the modal a person was typing into. There is no shared `ApiError`; adding one
  is a design change.
- **P12** Raise messages: lowercase, no trailing period, `!r` around a bad
  value, no em dash. ` - ` introduces a parenthetical.
- **P13** `snake_case` functions, `UPPER_SNAKE` module constants, `PascalCase`
  types. `store.locked` is lowercase because it is used as a context manager
  (`with locked(path):`), which is the stdlib's own precedent (`contextlib`).
- **P14** Double quotes; f-strings for interpolation.
- **P15** Flags are keyword-only: `Tx(store, snapshot=None, *, tokens=)`,
  `api.apply(store, payload, *, tx=)`, `summary(tx, *, dirty=[])`,
  `Application(..., *, read_only=, allow_git=)`,
  `model.claim_live(claim, at=None)` — `at=` exists so lease behaviour is
  testable without sleeping.
- **P16** 100 columns in Python, no tabs, no trailing whitespace. JS and CSS
  have no width rule — a CSS one-liner per selector is the house style, so a
  long CSS line is never a finding.

## JavaScript (`app.js` is one file by decision — no bundler, no split)

- **P17** Every element comes from `$(tag, attrs, ...kids)` (`app.js:10`).
  `document.createElement*` is legal only inside `$` itself and inside `icon()`
  (it needs `createElementNS`). Handlers ride in attrs (`onclick: fn`); raw
  `addEventListener` is for targets that were never constructed here —
  `document`'s keydown, the delegated body drop/click.
  Note the helper's shape: a spread in the `...kids` position makes N children
  of *one* node, never N siblings.
- **P18** One state object, `const S`, with a trailing `//` note on any field
  whose meaning is not in its name. Module singletons are `let` with a one-line
  `/** */` when the variable encodes a rule (see `textFlushInFlight`).
  Read-throughs return `[]` or `null`, never `undefined`.
- **P19** `const`, `let`, never `var`.
- **P20** camelCase in JS; wire keys stay snake_case and are read literally
  (`card.wip_limit`, `S.state.read_only`, `git.is_dirty`) because they are the
  YAML keys. Verbs: `render*` rebuilds a subtree, `paint*` patches existing
  nodes, `open*`/`close*` a modal or popover, `*Pop` builds popover content,
  `build*` the one-time shell.
- **P21** The only mutation path is `await send("card.update", args, "Label")`.
  `send` adopts the returned state. Never mutate `S.state.doc` client-side,
  never order or allocate ids in the client — the server owns both. Read-only
  servers are guarded by `readOnly()`, and `send` returns `null` on one.
- **P22** `/** */` above a non-obvious function, `//` inside a body, WHY only,
  continuation aligned under the first word.
- **P23** Double quotes (single quotes only inside ICONS SVG data), semicolons,
  2-space indent, one blank line between functions.
- **P24** Prefs live in `localStorage` under `yb.*` (`yb.theme`, `yb.view`,
  `yb.density`, `yb.agent`). Never in the board file: prefs are per-browser, the
  file is shared and committed. `yb.agent` is the identity the toolbar's select
  holds and `c` claims as — two people reading one board must be able to be
  different agents without either editing the file.
- **P25** UI strings are sentence-case with a period (`"This server is
  read-only."`). That is the opposite of P12 on purpose: one is a log line, the
  other is prose a person reads.

## CSS

- **P26** Token-first: colour, radius, shadow, gap and stacking rank are
  `--x` in `:root`, consumed via `var()`. A literal in a rule is a bug unless
  it is inside the token block.
- **P27** `--kebab-case`, with numeric scale suffixes (`--surface-2`,
  `--radius-sm`, `--prio-urgent`).
- **P28** `[data-theme="dark"]` redeclares every *colour* token; root-only
  declarations are geometry. Both blocks end with `color-scheme`. A new colour
  token needs both values and a comment giving the contrast it was chosen for.
- **P29** JS-supplied properties are `--col`, `--prio`, `--c`, `--who`,
  `--claim`. CSS reads them with a fallback (`var(--col, var(--muted))`) *only*
  where the property legitimately appears outside the element that sets it — a
  list row has no `--prio` ancestor, a card tile always does, and a fallback on
  the tile would silently turn a stripe-less card into a striped one on hover.
  `--lane` and `--prio-*` are read from JS (`gridTemplateColumns`,
  `priorityColor`), so "unused in CSS" does not mean unused. A sixth property
  has to be added to this list in the same commit that sets it, or the next
  person deletes it as dead code and the claim colour goes with it.
- **P30** kebab-case class names; state is an extra class (`.on`, `.over`,
  `.dragging`, `.archived`, `.claimed`, `.overlaps`); density and theme arrive
  as `[data-*]`. Coordination state is a class, not a `data-*` attribute, so a
  card's four states style the same way its older states do.
- **P31** One line for a single-purpose selector, a 2-space multi-line block
  from about four declarations up. Section order mirrors `app.js`.
- **P32** `/* */` only — CSS has no line comments — and the same WHY voice, with
  the continuation aligned to the text column.

## Cross-file contracts

These break silently, which is the whole reason they are written down.

- **Artefacts.** The board is tracked now: `.yamlboard/` *is* the coordination
  data, and a clone that arrived without it cannot tell who holds what. Two
  per-machine artefacts sit inside it — `.lock` and `.tmp-*.yaml` — and both are
  ignored from a `.gitignore` **the store owns**: `init` seeds one inside
  `.yamlboard/` (`store.IGNORE_BODY`), and this repo additionally carries
  `**/.yamlboard/.lock` and `**/.yamlboard/.tmp-*.yaml` at its root so a store
  created before that rule, and `examples/demo/`, stay honest without re-running
  `init`. The nested file is what makes the board safe to drop into a repo that
  has never heard of yamlboard: editing a host repo's root `.gitignore` unasked
  is not ours to do, and without the rule `git add .` commits a 0-byte lock and
  the Commit button then refuses on its own foreign-file guard. A pattern like
  `.yamlboard*` untracks the board and the demo silently, and `git check-ignore
  -v .yamlboard/board.yaml` returning a match is the symptom. Rename an artefact
  in `store.py` (`LOCK_NAME`, `TEMP_PREFIX`, `TEMP_SUFFIX`) and you edit
  `IGNORE_BODY`, this repo's root `.gitignore`, this bullet, and the README's
  "Private boards" note that tells people which lines to copy, in the same
  commit — `TestQuietWrites::test_the_lock_and_the_scratch_are_the_two_ignored_names`
  pins the root file and
  `TestCreation::test_create_ignores_its_own_scratch_inside_the_store` the nested one.
- **Tokens.** `rev` is gone. The client sends `tokens` — a per-file opaque
  `mtime_ns-size` stamp from `api.tokens_of`, keyed `"board"` or by card id —
  and `Tx.flush` refuses to write any file whose bytes moved since. A whole-store
  counter made two unrelated cards look like a conflict, which is the bug the
  rewrite exists to fix; a new write path that does not carry tokens for the
  files it touches is a data-loss bug. The CLI may pass none (an agent editing
  files directly has no token to offer), the browser must not.
- **"A stale token is a reload; a claim denial is not."** A stale write comes
  back `claim_denied: false` carrying the whole fresh state including `git`, and
  the client adopts it and repaints — "the board changed on disk" is the intended
  UX, not an error to retry around. A claim denial comes back
  `claim_denied: true` with `lines` and `detail` and deliberately **no state**:
  nothing moved under the client, so reloading would throw away the modal it was
  typing a note into. Do not add client-side retry to either, do not drop `git`
  from the reload payload or the commit button reads "git off" until the next
  poll, and never unify the two branches because they both say 409.
- **Enum drift.** `model.PRIORITIES` and `model.FIELD_TYPES` are the single
  source; `app.js` keeps its own copy of both (it cannot import Python) for the
  dropdowns, and `styles.css` carries a `--prio-*` token per priority.
  `TestEnumDrift` in `test_api.py` reads `app.js` and fails the suite if a value
  is added on one side only.
- **Loopback.** `server.is_loopback` is the only definition of "only this
  machine can reach it" — the bind check, the banner warning, the printed URL
  and, through `server.local_hosts`, the Host/Origin allowlist all derive from
  it. It lives in `server.py` because `cli.py` imports `server.py`. Adding a
  second copy is how `127.0.0.2` ended up refused as writable while a
  `--allow-public` alias bind served with no rebinding guard at all.
- **Shortcuts.** The key handler in `app.js`, the `openHelp` rows in the same
  file, and the README's keyboard line are one list in three formats. Editing
  one without the other two is a finding; `Esc` (close) and `Done` (close and
  flush) are a pair — a new dialog needs both.
- **Read-only.** `--read-only` must be honest end to end: the server refuses
  every POST, `payload()["read_only"]` tells the client, `send()` returns `null`
  so a caller's `if (ok)` cannot lie about a save that never happened, and the
  write controls are disabled.
- **`docs:` and the README.** When the code changes what the README claims
  (a flag, an exit code, a key, what the commit button stages), the README
  changes in the same commit, or in its own `docs:` commit immediately after.
- **The demo fixture.** `examples/demo/.yamlboard` *is* the README's quick
  start, so a broken fixture breaks the first thing every reader copies. Read it
  through `store.Store` and `api.summary` rather than pasting invented output,
  and re-run the byte-stability check before committing a schema change: read
  the store, re-write every file with `store._write_file`, and the bytes must not
  move — a fixture that a first `serve` reflows turns a reader's `git status`
  dirty before they have done anything. Its hand-written leases are timestamps
  and will expire; that is expected and is *why* `conflicts` has something to
  report out of the box. There are **no screenshots** in this repo: the v1 PNGs
  went with the orphan rewrite and there is no generator. The rule is what
  happens if anyone adds some — a screenshot is a checked-in artefact with
  nothing keeping it honest, so either regenerate all of them from the demo
  store in the same commit that changes the UI, or delete them and every README
  line that points at them. A picture of a board that no longer exists is worse
  than no picture.

## Coordination

The rules that keep several agents honest about one repository. They are numbered
like the rest, because each of them has a second and third copy elsewhere.

- **P39** Key sets are closed and enforced **on read** (`_check_keys`):
  `TOP_KEYS`, `BOARD_KEYS`, `CARD_KEYS`, `CLAIM_KEYS`, `COLUMN_KEYS`,
  `CUSTOM_KEYS`. A key the code writes but the tuple omits makes a real
  person's card permanently unreadable, including by `check`. `CLAIM_KEYS` is
  the complete claim vocabulary and there is no `status` key in it: a second
  source of truth for "who holds this" is how a board starts lying about itself.
  A claim is live iff `expires` is in the future and `overridden_by` is unset —
  that is the whole rule, and nothing may cache a derived copy of it. Add the
  key, the `KEY_ORDER` entry that decides its diff placement, and its test in
  one commit.
- **P40** The exit-code *set* is `{0,1,2,3,130,141}` and each code means the same
  thing in all four places it appears: the `EXIT_*` constants in `cli.py`, the
  `EPILOG` table, the README's table, and `SKILL.md`'s table. The two tables are
  the contract's terse wording verbatim; the epilog may add a sentence per code,
  because it is the one a person reads when a command just failed on them.
  `3` means *someone else holds it* and nothing else — a mistyped or unlisted
  agent name is `2`, because the fix is to re-run the same command with a better
  name, not to choose other work. `1` is only ever `serve` failing to bind.
  `check` and `conflicts` exit `2` when they have something to report, so CI can
  fail on a board that is lying about itself.
- **P41** The verb list is one list in four formats: `cli.VERBS`, the order
  `--help` prints, the mutating half of `api.OPS`, and `SKILL.md`'s
  `metadata.commands`. `cli.VERBS` is checked against `api.OPS` (every mutating
  op has a verb, and `conflicts.resolve` counts as one) and against the skill's
  frontmatter; a verb added without a skill update fails the suite. That test is
  the product: an agent following a stale skill collides anyway, which is worse
  than an agent with no skill, because it believes it complied.
- **P42** Coordination traffic is not content. Nothing under `claim:` may touch a
  card's `updated:`, and `store._write_file`'s byte compare is not an
  optimisation — a redundant `claim` or `renew` must write zero bytes, or lease
  churn moves `updated:` and every human's `git log` and list-view sort starts
  lying. A `renew` legitimately changes one line; a claim that rewrites the card
  is a bug.
- **P43** Conflict detection is read-only; only `conflicts.resolve` writes, and
  only `overridden_by`. Detection derives the winner from bytes already on disk
  (earliest `at`, tie to the alphabetically smaller agent), so two clones asking
  independently get the same answer without talking. A read path that repairs a
  conflict is how a report becomes a silent edit of somebody else's claim.
- **P44** Identity resolves in exactly one place, `identity.resolve`: `--as`,
  then `$YAMLBOARD_AGENT`, then the local part of `git config user.email`, and
  it must already be in `people`. Nothing creates a person anywhere in the
  codebase. The resolved name is echoed before a mutating write, and never in
  `--json`.

## Git

- **P33** Subject: lowercase imperative clause, ≤~72 columns, no period. A
  scope prefix only when the change is genuinely narrow (`docs:`, `dark theme:`).
- **P34** Body: `-` bullets, what *and* why, naming the mechanism. An incidental
  regression gets a `Fixes:` bullet.
- **P35** `gitops.py` is a convention, not a sandbox. Eight fixed argv lists, no
  shell, writes scoped to the `.yamlboard/` directory — but a repo you did not
  write can still steer git, so: `core.fsmonitor=false` on every call (plain
  `git status` executes it), `GIT_OPTIONAL_LOCKS=0` for read-only calls,
  `GIT_CONFIG_NOSYSTEM=1`, and ambient
  `GIT_DIR`/`GIT_WORK_TREE`/`GIT_INDEX_FILE` cleared from the environment.
  Seven of the eight go through `_run` and therefore `_env()`. The eighth is
  `config_value`, which reads the *user's own* global config to answer "whose
  email is this" for the identity fallback, and `_env` points
  `GIT_CONFIG_GLOBAL` at `/dev/null` — precisely to make a hostile repository
  inert — so it cannot use it. That is safe because the value is data, never a
  command: no fsmonitor, no hook, no index, and it is `--get` on one key with no
  pathspec, so there is nothing in the working tree for it to act on. The
  `_DROP_ENV` path overrides are still stripped, so a repository cannot redirect
  the read at another repository's config. Anything new that wants the user's
  config belongs in `config_value`, never in a looser `_env`.
  Repo hooks (`.git/hooks`, `core.hooksPath`) still run on Commit, on purpose: a
  hook you installed is yours, and `--no-verify` would be a surprise. Pointing
  yamlboard at a directory you do not control is unsupported. A hung git is
  stopped with SIGTERM and only then SIGKILL, so a killed commit does not leave
  `.git/index.lock` behind; the one git error we recognise by text is exactly
  that lock, reworded as "another git command is running". Commit stages
  `.yamlboard/` with a plain `add` (never `--force`, which would commit `.lock`
  and mid-write scratch), and refuses outright if the index already holds a
  file outside that directory — an agent staging source while a human clicks
  Commit is the quarrel this refuses.

## Tests

- **P36** `unittest` (run by pytest), no fixtures, no `subTest`, `self.assert*`
  only. Each module's docstring names the slice it covers; the file ends with
  `if __name__ == "__main__":  # pragma: no cover` + `unittest.main()`.
- **P37** A fixture base class plus `Test<Area>` subclasses; the base's docstring
  says what the isolation buys. Fixture data is an inline YAML literal, not a
  file in the repo.
- **P38** Test names are behaviour sentences
  (`test_a_stale_token_is_refused_and_answers_with_the_fresh_board`), and a
  broken fixture raises `AssertionError("no card X")` rather than failing with an
  `AttributeError` three frames away. `TemporaryDirectory()` + `addCleanup`; git
  tests set `GIT_CONFIG_GLOBAL=/dev/null` and friends so your `~/.gitconfig`
  cannot change the result.
- **P45** Never monkeypatch `gitops`. Cross-machine behaviour is tested with real
  `git init` in temp dirs — two clones, a push, a pull, then `conflicts` on the
  merge that git reported as clean — because the claim under test *is* about git's
  merge, and a stub asserts the stub. Assert with `commit_count()` and paths, not
  by scraping `git log` text.
- **P46** Lease behaviour never sleeps. Every decision about time reads
  `model.now()`, the single clock, so expiry tests move the clock forward and
  assert a claim lapsed without the suite waiting 30 seconds. `model.now()` is
  the seam; a test that patches `datetime` elsewhere is testing its own mock.
