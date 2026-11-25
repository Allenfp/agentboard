# yamlboard

**A board in your repo that tells concurrent agents who holds what, so they stop
editing the same file.**

Several agents working one repository collide on the same files, and neither git
nor "I'll be quick" prevents it. yamlboard makes the claim a file: each card is
one YAML file that names the agent holding it, until when, and which paths the
work will touch. A claim is refused before the edit, not discovered in a merge
conflict after it.

No database, no account, no build step, and — for the parts agents use — no
server. `.yamlboard/` is committed to the repository it coordinates, so a fresh
clone already has the whole picture and `yamlboard claim` works offline.
`yamlboard serve` is a human dashboard over those same files, not the thing that
owns them.

## Install

```bash
curl -LsSf https://raw.githubusercontent.com/Allenfp/yamlboard/main/install.sh | bash
```

That installs [`uv`](https://docs.astral.sh/uv/) if you don't already have it,
then installs `yamlboard` as a tool. If you already have `uv`:

```bash
uv tool install "git+https://github.com/Allenfp/yamlboard"
```

Python 3.11+ and one dependency (`ruamel.yaml`). No Node, no bundler.

## Start a board

```bash
cd my-repo && git init -q -b main
yamlboard init --title Web --key WEB
```

```
created /home/you/my-repo/.yamlboard
  board.yaml, cards/, archive/, .gitignore

  people: allen (you; add the rest in the board's Settings)
next:
  yamlboard create "First card" --as allen
  yamlboard skill install     # teach agents in this repo the protocol
```

`init` refuses to overwrite an existing store, because an overwritten board is
unrecoverable and "already initialised" costs you one confused second.

It also lists whoever it was named as — here `allen`, from
`git config user.email`, or whoever you passed to `--as`. That is not a
convenience: a board with an empty `people` list is one nothing can ever be
claimed from, and no CLI verb adds one — letting a claim widen the list it is
checked against is exactly what the list is there to prevent.
Add teammates in the board's Settings, or by hand in `.yamlboard/board.yaml`:

```yaml
people:
  - id: allen          # slug, and the name you pass to --as
    name: Allen
    color: "#2f6f4f"
```

Then make a card and take it:

```console
$ yamlboard create "First card"
created WEB-1  First card
  next: yamlboard claim WEB-1 --paths '<glob>'

$ yamlboard claim WEB-1 --as allen --paths 'src/**'
  as allen
claimed WEB-1 as allen until 2026-11-24T01:33:59
  paths src/**
  next: renew before anything long - yamlboard renew WEB-1
```

The claim is now in `.yamlboard/cards/WEB-1.yaml`, and `assignee` moved with it.
Say you're done with `yamlboard done WEB-1`: the card goes to the board's done
column and the lease is released, because finishing without releasing is how a
board fills up with cards nobody can take.

## The agent loop

Every verb that acts on a claim takes `--as <name>`; set `$YAMLBOARD_AGENT` once
per shell and it is assumed. The name must already be in `people` — an identity
nobody recognises surfaces weeks later in a `git blame`, so no verb invents one
mid-board. `init` is the exception it has to be: a new board lists nobody, and a
board with an empty `people` list can never be claimed from, so `init` seeds the
roster with whoever it was named as and says so. Add the rest in the board's
Settings — a claim cannot widen the list it is being checked against.

The resolved identity is printed before anything is written, so a stale
`$YAMLBOARD_AGENT` in a script is caught rather than committed.

```console
$ yamlboard ready --as grigory --paths 'src/api/**'
nothing is ready.
  every unclaimed card is either blocked or touches a path someone already holds - try without --paths to see why
  next: yamlboard conflicts   # who holds what, and for how long

$ yamlboard claim PROD-11 --as grigory --paths 'src/api/cli.py'
  as grigory
your paths collide: src/api/cli.py (ada claimed src/api/** on PROD-2)
next: yamlboard ready --as grigory

$ yamlboard claim PROD-11 --as grigory --paths 'README.md'
  as grigory
claimed PROD-11 as grigory until 2026-11-24T03:13:38
  paths README.md
  next: renew before anything long - yamlboard renew PROD-11
```

A refusal lists every reason, not the first one it found, and its last line is
always a command you can run, naming you. That is the whole design: a terse
`denied` is what sends an agent back to retry the same claim in a loop. How many
lines it takes depends on how many things are wrong — a taken card whose paths
also collide is three, a blocker-only refusal is two — so nothing should count
them. The denial went to stderr; with `--json` you get one document on stdout
instead and nothing else.

`show` answers "can I take this, and if not why" in one call, and is a useful
pre-flight because it exits 3 for the same reason `claim` would:

```console
$ yamlboard show PROD-2 --as grigory
PROD-2  Split the apply endpoint out of the store
  taken  [product/In Progress]  priority urgent
  held by ada until 2026-11-25T00:40:00 (23h26m left)
  paths src/api/**, tests/test_api.py
PROD-2 is taken — ada holds it until 2026-11-25T00:40:00 (23h26m left)
your paths collide: src/api/** (grigory claimed src/api/routes.py on PROD-3)
next: yamlboard ready --as grigory
```

Four words describe a card at both ends of a lease: `ready`, `taken`, `blocked`,
`yours`. `ready` and `yours` are the only two you may start work in.

`--paths` is a promise about files, so name files (`src/api/routes.py`) or a tree
(`src/api/**`). A bare directory is refused, because it *means* the whole tree:

```console
$ yamlboard claim PROD-8 --as ras --paths 'src/api'
  as ras
error: --paths 'src/api' is a directory: it would claim every file under it. Name the files, or write src/api/**
```

### Leases

A claim expires, and an expired claim is simply not a claim: the card returns to
`ready` and whoever takes it next records `preempted: <you>`. Default 30 minutes,
or the board's `lease_ttl` (`lease_ttl: 2h` in `board.yaml`); `--ttl 90s|5m|2h|1d`
between 30s and 24h, and outside that range it is refused rather than quietly
clamped — a caller who asks for one second and gets thirty holds a lease that
outlived the command it was guarding. `renew` extends by one full TTL from now,
and re-claiming a card you already hold writes nothing at all, which is what
`renew` is for:

```console
$ yamlboard claim WEB-1 --as allen --paths 'src/**'
  as allen
WEB-1 is already yours until 2026-11-24T01:33:59 - nothing written
```

Nothing under `claim:` touches the card's `updated:`. Coordination traffic is not
content, and letting it move that stamp would make every human's `git log` and
list-view sort lie. `renew` legitimately changes exactly one line:

```
 .yamlboard/cards/PROD-3.yaml | 2 +-
 1 file changed, 1 insertion(+), 1 deletion(-)
```

### Verbs

`init` `serve` `create` `check` `show` `list` `ready` `claim` `renew` `release`
`done` `dep` `comment` `conflicts` `migrate` `skill`. Every one takes `--json`: one
document, `indent=2`, on stdout, with no prose around it. The store is found by
walking up from the current directory (`--dir` overrides), so any verb works from
anywhere inside the repository, and none of them starts or needs a server.

Dependencies are `yamlboard dep add PROD-4 --blocked-by PROD-3` (`dep rm` to
undo). A cycle is refused and the message contains the path through it, because
an unresolvable one silently empties the ready queue:

```console
$ yamlboard dep add PROD-8 --blocked-by PROD-9 --as ada
  as ada
error: blocked_by would form a cycle: PROD-8 -> PROD-9 -> PROD-8
```

### Exit codes

|code|meaning|
|---|---|
|0|ok|
|1|cannot listen|
|2|bad argument / refused op / invalid file|
|3|claim denied|
|130|interrupted|
|141|pipe closed|

**3 means someone else holds it, or the work is finished.** Pick other work;
retrying does not make the holder's lease lapse any sooner, and no claim reopens
a done card. `1` is `serve` failing to bind, where nothing you typed is wrong. An
agent that mistyped its own name gets `2`, not `3` — `3` has to mean "choose
different work", never "fix your name".

`check` and `conflicts` exit 2 when they have something to report, so CI fails on
a board that is lying about itself. `141` is `yamlboard list | head` closing the
pipe. This table, the `--help` epilog and `SKILL.md` are the same list in three
files and are checked against each other.

## The store

```
.yamlboard/
  board.yaml        people, labels, and the boards: columns, custom fields,
                    next_card, lease_ttl. No cards, no rev.
  cards/WEB-3.yaml  one card per file, id == filename stem
  archive/WEB-9.yaml retired cards (a move is a rename: one clean diff)
  .gitignore        tells git to ignore the two lines below — `init` writes it
  .lock             advisory flock, gitignored
  .tmp-a1b2c3.yaml  write scratch, gitignored
```

`version: 2`. There is no `rev:` counter and no whole-file token. Editing a card
touches exactly one path — the card's own file — which is what makes git the good
collaborator it was always supposed to be: two agents finishing two cards edit two
files and merge without anyone's help. *Creating* a card touches two, because the
board's `next_card` counter moves with it, so two clones creating from the same
commit mint the same id and collide in both files. That is why the loop pulls
before you create and not only before you claim. `yamlboard migrate OLD_FILE`
imports a v1 single-file board; there is no v1 reader and no compatibility shim — a
silently misread board is worse than a loud refusal naming `migrate`.

`board.yaml`, trimmed to one board and three columns, with the end-of-line notes
added for reading; the fixture's own file is `examples/demo/.yamlboard/board.yaml`.

```yaml
version: 2
title: Demo

# people is also the agent allowlist: --as a name that is not here is refused
people:
  - id: ada
    name: Ada Lovelace
    color: "#2f6f4f"

labels:
  - id: bug
    name: Bug
    color: "#d1495b"

boards:
  - id: product         # slug, unique in the file
    key: PROD           # 2-8 capitals; prefixes the card ids
    title: Product
    columns:
      - id: backlog
        name: Backlog
        color: "#7c8797"
      - id: doing
        name: In Progress
        color: "#b45309"
        wip_limit: 3    # the column header flags itself past this
      - id: done
        name: Done
        color: "#2f6f4f"
        is_done: true   # moving a card here releases its claim
    custom_fields:
      - id: customer
        name: Customer
        type: select    # text | select | date | number | bool
        options: [Acme, Globex]
    next_card: 13       # the id the next card gets
    lease_ttl: 2h       # this board's default lease; --ttl still wins
```

And `.yamlboard/cards/PROD-3.yaml` from that store, annotated — the real file has
no end-of-line comments, and a claim or two carries a note about its own expiry:

```yaml
id: PROD-3
title: An unknown op should name the ops it does know
desc: |
  Free text, kept byte-for-byte as a block scalar, never re-rendered.
column: review
order: 3072.0           # spaced by 1024, so an insert writes one card
labels: [bug]
priority: high
assignee: grigory       # set by claim, cleared by release
due: 2026-11-27
fields:
  risk: false
claim:
  agent: grigory
  at: 2026-11-24T00:52:00
  expires: 2026-11-25T00:52:00
  paths:
    - src/api/routes.py
  note: one file, but it sits inside ada's tree; see conflicts
archived: false
created: 2026-11-21T13:05:00
updated: 2026-11-24T00:58:00
```

There is no `status` key inside `claim:`. A second source of truth for "who holds
this" is how a board starts lying about itself: a claim is live if `expires` is in
the future and `overridden_by` is unset, and nothing else. `preempted` names an
agent whose *expired* claim this one replaced; `overridden_by` names the winner
that beat it in a live overlap.

Key sets are closed. An unrecognised key is an error **on read**, so a key the
code uses but the schema forgot would make a real person's card permanently
unreadable — the fix for a typo is in the message, and it names the allowed keys.

Play with the shipped store instead of typing the above:

```bash
cp -r examples/demo/.yamlboard ./ && yamlboard serve
```

It has two boards, fourteen live cards plus one in `archive/`, four live claims
and one reported path overlap, so `check` and `conflicts` have something to say:

```console
$ yamlboard check --dir examples/demo
/home/you/my-repo/examples/demo/.yamlboard
  2 board(s), 14 card(s), 3 people
  conflict: overlap: PROD-2 (ada) and PROD-3 (grigory) claim the same files

1 conflict(s): yamlboard conflicts --resolve frees the loser of an overlap and corrects a mismatched card id
```

Its leases are timestamps, not promises: hand-written so a fresh clone opens on
a board with something in flight, and they lapse -- which sends those cards back
to `ready`. That is the mechanism working, not a fixture gone stale. To put the
tour's traffic back on (`check` reporting that overlap again), renew them:

```bash
yamlboard renew PROD-2 --as ada   && yamlboard renew PROD-7 --as ras
yamlboard renew PROD-3 --as grigory && yamlboard renew PROD-6 --as ras
```

## Conflicts: detected, never prevented

Two clones, two card files, two paths. Each claims `src/api/**`, git merges the
two files without a single complaint, and both agents believe they hold the same
directory. Git has no arbiter and neither does yamlboard — there is no server in
the loop to become one. So this is detect-and-resolve with a deterministic winner,
surfaced to a human:

```console
$ yamlboard conflicts
overlap: PROD-2 (ada) and PROD-3 (grigory) claim the same files
  src/api/** ∩ src/api/routes.py
  winner ada on PROD-2 (claimed 2026-11-25T09:00:00)
  loser  grigory on PROD-3 (claimed 2026-11-25T09:30:00)
  earliest `at` wins; a tie goes to the alphabetically first name

next: yamlboard conflicts --resolve   # frees the loser of an overlap, and corrects a card whose id disagrees with its filename
```

Reporting writes nothing, so two clones asking independently get the same answer
and a report can never become a silent edit of somebody else's claim. Only
`--resolve` writes, and it writes `overridden_by: ada` into the loser's claim —
which makes that claim inert, so the card is back in `ready` on the next read:

```console
$ yamlboard conflicts --resolve --as ada
  as ada
PROD-3: grigory overridden by ada
1 claim(s) released; those cards are ready again

$ yamlboard conflicts
no conflicts - no two live claims share a path, and no ids collide.
```

The same sweep reports a card whose `id:` disagrees with its filename — the
ordinary result of a merge where you kept both copies of `cards/WEB-1.yaml` and
one landed on a borrowed name — and a claim whose paths match files already
modified in your working tree. `--resolve` rewrites the `id:` to match the
filename, because the filename is the address every reader uses; the file itself
is never moved. When two files want the same name it says so and leaves both
alone rather than picking a survivor. `git pull --ff-only` before you claim and
after you finish is what keeps this list short.

## The board in a browser

```bash
yamlboard serve
```

```
yamlboard 0.1.0 on http://localhost:7474
  board /home/you/my-repo/.yamlboard
  ctrl-c to stop
```

It binds `127.0.0.1:7474` and does not open a tab. Flags: `--host`, `--port`,
`--read-only`, `--no-git`, `--allow-public`, `-v`, `--json`. Binding a
non-loopback address is refused unless you pass `--read-only` or `--allow-public`,
because there is no authentication.

The landing view is the classic board — same columns, same drag and drop.
Coordination arrives as state on cards, not as a new screen:

- **A claim chip** on each held card: who, and a countdown that ticks locally
  against the absolute `expires` the server sent, so it moves every second without
  a request every second. Under a quarter of its own TTL it goes amber; at zero it
  reads `expired` and stays there.
- **Overlap is loud.** A card whose claim collides with another live claim gets a
  red left edge, and a `Conflicts (N)` control appears in the toolbar listing each
  pair, the winner and a Resolve button. At zero conflicts it is gone entirely.
- **Blocked cards** render dimmed with the ids they wait on, and a ready card gets
  an accent border, so "go get me" reads at a glance.
- **You claim as whoever the toolbar's agent select says** (`c` uses it too). It is
  a select over `people`, never free text, and it lives in `localStorage` — a pref,
  so two people can read one board as different agents.
- **Commit** stages `.yamlboard/` and nothing else, and refuses outright if your
  index already holds someone else's files — a human clicking Commit must not ship
  an agent's half-finished source edit.

`n` new card · `/` search · `Enter` open the focused card · `c` claim the focused
card as the selected agent · `r` ready filter · `a` archive view · `b`/`l` board
or list view · `d` card density · `f` filters · `s` settings · `t` theme · `?` all
shortcuts · `Esc` close. Drag a card to move it; drop order is written to the file.

There are no screenshots. The v1 board's PNGs were deleted rather than left as a
picture of something that no longer exists, and nothing in this repo generates
them, so the demo store above is the tour: `cp -r examples/demo/.yamlboard ./ &&
yamlboard serve` shows the claims, the ticking leases and the conflict control in
a browser in about ten seconds.

## Teach the agents

A coordination scheme nobody knows the vocabulary for prevents no collisions at
all, so the skill file is not documentation, it is the interface.

```bash
yamlboard skill install
```

Copies the bundled `.agents/skills/yamlboard/SKILL.md` into *this* repository and
prints the block to paste into `AGENTS.md`:

```markdown
## Coordination

This repo uses `yamlboard`. Before editing files, check `.yamlboard/`: claim a
card with `yamlboard claim <ID> --paths '<globs you will touch>'` and release it
when done. Exit code 3 means another agent holds it - take different work, never
retry. Full protocol: `.agents/skills/yamlboard/SKILL.md`.
```

`yamlboard skill path` prints where the bundled copy lives. `install` refuses to
overwrite a file whose bytes differ from the bundled one unless `--force`, so a
hand-edited skill is a decision rather than an accident. The skill documents the
CLI only — an agent should not be steered toward a server that need not be
running. Its `metadata.commands` list is pinned against the CLI's verb list, so a
new verb without a skill update fails the suite.

## Editing the files directly

Hand-edit anything; this is a file. The browser polls and repaints within a few
seconds, and its own saves keep your comments, key order and block scalars intact.

Concurrency is per file. There is no `rev` any more — a whole-store counter made
two unrelated cards look like a conflict. Each write carries the token it was
based on, an opaque `mtime_ns`-`size` stamp per file (`board`, or a card id), and
the store refuses to write a file whose bytes moved since. Two tabs racing means
the loser re-reads. Two kinds of refusal come back as `409`, and they are not the
same: **a stale token is a reload** (the client adopts the fresh document), while
**a claim denial is not** — reloading would throw away the reason the claim
failed, so it carries `claim_denied: true` and the four explanation lines instead.

Saves go to a temp file and then `os.replace`, so a corrupt file is never
partially written; a write whose bytes are unchanged writes nothing, which is why
lease traffic does not spam `git status`.

## Private boards

Keep the code public and the board somewhere else. Discovery walks up from the
current directory, so a board in its own repo behaves the same way: run the verb
from inside that directory, or name it after the verb —
`yamlboard ready --dir ~/private-boards`.

Two things to get right:

- **Do not** commit real data to a fork of this repo. Public code plus a private
  data repo makes a leak impossible by construction; a private fork of the code
  makes every `git remote add public …` a mistake waiting to happen.
- You do not have to add anything to your `.gitignore`. `init` writes
  `.yamlboard/.gitignore`, which ignores the lock and the write scratch and
  nothing else — the board itself must stay tracked, or a clone arrives unable
  to tell who holds what. The rule lives inside the directory so a board dropped
  into a repo that has never heard of yamlboard keeps itself clean; the one job
  left for you is not to widen it to `.yamlboard*`, which untracks the board
  silently. `git check-ignore -v .yamlboard/board.yaml` printing nothing is the
  proof a pattern is right.

## How it works

```
agent      ── yamlboard claim WEB-3 --paths 'src/api/**' ──▶  validate, refuse or write one card file
human      ── GET  /api/state ─────────────────────────────▶  board + cards + ready + agents + conflicts
         ── POST /api/apply {op, args, tokens} ───────────▶  apply, save, return the whole state
         ── POST /api/commit {message} ───────────────────▶  git add .yamlboard/ && git commit
```

| module | job |
| --- | --- |
| `model.py` | schema, validation, leases, path-pattern overlap, cycle detection |
| `store.py` | the directory: round-trip YAML, per-file tokens, atomic replace, `fcntl` lock |
| `api.py` | the 33 mutating ops (`claim.create`, `card.move`, …) and their rules |
| `identity.py` | `--as` → `$YAMLBOARD_AGENT` → git `user.email`, checked against `people` |
| `gitops.py` | `status` / `add .yamlboard/` / `commit -m`, nothing else |
| `server.py` | static files + JSON on stdlib `http.server` |
| `static/` | the whole client, no framework, no build |

Both doors call the same 33 ops in `api.py`, which is the only thing that makes
"the files are the arbiter" true rather than a slogan: the CLI is not a thinner
version of the API, it is the same code with a different transport. The browser
never decides where a card lands or what an id is; it posts intent and renders
what comes back. Concurrent writes are serialised by an `fcntl` lock around
read-modify-write, and per-file tokens catch anything that slipped through —
including a hand edit or a `git pull`.

`gitops.py` stays deliberately narrow: one `_run` helper, a fixed set of argv
lists, no shell, and writes scoped to `.yamlboard/`. It is a convention, not a
sandbox — a repo you did not write can still steer git — so `core.fsmonitor` is
switched off on every call (plain `git status` executes it) while your own hooks
still run on Commit. Pointing yamlboard at a directory you do not control is not
supported. See [docs/CONVENTIONS.md](docs/CONVENTIONS.md) P35.

## Limits, on purpose

- **Identity is honour-system.** `--as ada` is a claim about who you are, checked
  only against `people`. There is no auth, no tokens, no signing anywhere in this
  project, and loopback is the entire security story for `serve`. A deliberate
  liar can take anything; what this stops is accidents, which is where the
  collisions actually come from.
- **Cross-machine overlap can be detected, never prevented.** Two clones can each
  claim `src/api/**` and merge cleanly. `conflicts` will name both of you and pick
  the earlier claim; nothing can stop the second claim from being written. Run
  `git pull --ff-only` before you claim — the longer your view of the board lags,
  the more often you will be the loser.
- **The board is only as honest as the agents that declare their paths.** A claim
  with no `--paths` holds a card but blocks nothing; a claim listing `src/**`
  blocks the board. The pattern is a promise, and the only enforcement is that
  everyone else read it.
- **Not GitHub Issues.** If you want issues to live in GitHub's tracker, use
  GitHub's tracker. This is for people whose board should be a file in the repo.

MIT — see [LICENSE](LICENSE).

## Development

```bash
git clone https://github.com/Allenfp/yamlboard && cd yamlboard
uv run pytest -q
uv run python -m yamlboard.cli conflicts --dir examples/demo   # --dir follows the verb
```

[docs/CONVENTIONS.md](docs/CONVENTIONS.md) is the house style plus the list of
things that live in two or three files and have to agree — read it before
touching the client, the file format, or the CLI surface.
