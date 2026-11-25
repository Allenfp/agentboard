# The command line

Every verb, what it promises, and the numbers it answers with. The transport is
irrelevant to the rules: `serve` and the CLI call the same 33 ops in `api.py`,
and no verb needs a server running.

## Identity

Every verb that acts on a claim takes `--as <name>`; set `$AGENTBOARD_AGENT` once
per shell and it is assumed. Resolution is `--as` → `$AGENTBOARD_AGENT` → the
local part of `git config user.email`, and the resolved name is printed before
anything is written, so a stale `$AGENTBOARD_AGENT` in a script is caught rather
than committed.

The name must already be in `people` — an identity nobody recognises surfaces
weeks later in a `git blame`, so no verb invents one mid-board. `init` is the
exception it has to be: a new board lists nobody, and a board with an empty
`people` list can never be claimed from, so `init` seeds the roster with whoever
it was named as and says so. Add the rest in the board's Settings, or by hand in
`.agentboard/board.yaml`:

```yaml
people:
  - id: allen          # slug, and the name you pass to --as
    name: Allen
    color: "#2f6f4f"
```

A claim cannot widen the list it is being checked against — that is exactly what
the list is there to prevent.

## The loop

`ready` → `claim` → work → `renew` before anything long → `done`.

```console
$ agentboard ready --as grigory --paths 'src/api/**'
nothing is ready.
  every unclaimed card is either blocked or touches a path someone already holds - try without --paths to see why
  next: agentboard conflicts   # who holds what, and for how long

$ agentboard claim PROD-11 --as grigory --paths 'src/api/cli.py'
  as grigory
your paths collide: src/api/cli.py (ada claimed src/api/** on PROD-2)
next: agentboard ready --as grigory

$ agentboard claim PROD-11 --as grigory --paths 'README.md'
  as grigory
claimed PROD-11 as grigory until 2026-11-25T07:57:12
  paths README.md
  next: renew before anything long - agentboard renew PROD-11
```

A refusal lists every reason, not the first one it found, and its last line is
always a command you can run, naming you. That is the whole design: a terse
`denied` is what sends an agent back to retry the same claim in a loop. How many
lines it takes depends on how many things are wrong — a taken card whose paths
also collide is three, a blocker-only refusal is two — so nothing should count
them. The denial went to stderr; with `--json` you get one document on stdout
instead and nothing else.

`show` answers "can I take this, and if not why" in one call, and is a useful
pre-flight because it exits 3 for exactly the reasons `claim` would:

```console
$ agentboard show PROD-2 --as grigory
PROD-2  Split the apply endpoint out of the store
  taken  [product/In Progress]  priority urgent
  held by ada until 2026-11-26T09:00:00 (27h2m left)
  paths src/api/**, tests/test_api.py
PROD-2 is taken — ada holds it until 2026-11-26T09:00:00 (27h2m left)
your paths collide: src/api/** (grigory claimed src/api/routes.py on PROD-3)
next: agentboard ready --as grigory
```

Four words describe a card at both ends of a lease: `ready`, `taken`, `blocked`,
`yours`. `ready` and `yours` are the only two you may start work in.

## `--paths`

A promise about files, so name files (`src/api/routes.py`) or a tree
(`src/api/**`). A bare directory is refused, because it *means* the whole tree:

```console
$ agentboard claim PROD-8 --as ras --paths 'src/api'
  as ras
error: --paths 'src/api' is a directory: it would claim every file under it. Name the files, or write src/api/**
```

Nothing counts the files for you: a pattern naming a file that does not exist yet
has to stay claimable, because an agent writing that file *is* the reason for the
claim. A claim with no `--paths` holds a card but blocks nothing; a claim listing
`src/**` blocks the board.

## Leases

A claim expires, and an expired claim is simply not a claim: the card returns to
`ready` and whoever takes it next records `preempted: <you>`. Default 30 minutes,
or the board's `lease_ttl` (`lease_ttl: 2h` in `board.yaml`); `--ttl 90s|5m|2h|1d`
between 30s and 24h, and outside that range it is refused rather than quietly
clamped — a caller who asks for one second and gets thirty holds a lease that
outlived the command it was guarding. `renew` extends by one full TTL from now,
and re-claiming a card you already hold writes nothing at all, which is what
`renew` is for:

```console
$ agentboard claim WEB-1 --as allen --paths 'src/**'
  as allen
WEB-1 is already yours until 2026-11-25T06:26:38 - nothing written
```

Nothing under `claim:` touches the card's `updated:`. Coordination traffic is not
content, and letting it move that stamp would make every human's `git log` and
list-view sort lie. `renew` legitimately changes exactly one line:

```
 .agentboard/cards/PROD-3.yaml | 2 +-
 1 file changed, 1 insertion(+), 1 deletion(-)
```

## Dependencies

`agentboard dep add PROD-8 --blocked-by PROD-9` (`dep rm` to undo). A cycle is
refused and the message contains the path through it, because an unresolvable one
silently empties the ready queue:

```console
$ agentboard dep add PROD-9 --blocked-by PROD-8 --as ada
  as ada
error: blocked_by would form a cycle: PROD-9 -> PROD-8 -> PROD-9
```

## Verbs

`init` `serve` `create` `check` `show` `list` `ready` `claim` `renew` `release`
`done` `dep` `comment` `conflicts` `skill`. Every one takes `--json`: one
document, `indent=2`, on stdout, with no prose around it. The store is found by
walking up from the current directory (`--dir` overrides), so any verb works from
anywhere inside the repository, and none of them starts or needs a server.

## Exit codes

|code|meaning|
|---|---|
|0|ok|
|1|cannot listen|
|2|bad argument, refused operation, or invalid file|
|3|**claim denied**|
|130|interrupted|
|141|pipe closed|

**3 means someone else holds it, or the work is finished.** Pick other work;
retrying does not make the holder's lease lapse any sooner, and no claim reopens
a done card. `1` is `serve` failing to bind, where nothing you typed is wrong. An
agent that mistyped its own name gets `2`, not `3` — `3` has to mean "choose
different work", never "fix your name".

`check` and `conflicts` exit 2 when they have something to report, so CI fails on
a board that is lying about itself. `141` is `agentboard list | head` closing the
pipe. This table and `SKILL.md`'s are the same list word for word, and
`TestAntiDrift::test_the_exit_table_is_the_same_list_in_all_four_places` compares
them against the constants and the `--help` epilog (CONVENTIONS P40).
