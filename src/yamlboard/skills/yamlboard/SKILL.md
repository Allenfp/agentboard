---
name: yamlboard
description: Coordinate work in a repo that has a .yamlboard/ directory — claim a card before editing it, declare the paths you will touch, and release when done. Use when starting work in such a repo, when asked what to work on, or before touching a file another agent may hold.
metadata:
  commands: [init, serve, create, check, show, list, ready, claim, renew, release, done, dep, comment, conflicts, migrate, skill]
---

# yamlboard

Several agents work this repo at once. The board records who is doing what and which
files that covers, so two of you never edit the same path. It is files under
`.yamlboard/`, not a service: no server needs to be running for any command below, and
a fresh clone already has the whole picture.

Set your identity once per shell. Every command takes `--as <name>` instead, but a name
that is not in `.yamlboard/board.yaml`'s `people` list is refused — you cannot claim as
someone who was never added.

```bash
export YAMLBOARD_AGENT=grigory
```

## The loop

```bash
git pull --ff-only                    # claims you cannot see are claims you will lose
yamlboard ready --paths 'src/api/**'  # work nobody holds that you could actually start
yamlboard claim WEB-3 --paths 'src/api/**'
# ... edit, test ...
yamlboard renew WEB-3                 # before any step you expect to take minutes
yamlboard done WEB-3
git pull --ff-only                    # your card file and their card file are different paths
```

`yamlboard show WEB-3` answers "can I take this, and if not why" in one call. A card is
`ready`, `taken`, `blocked`, or `yours` — four words, the whole vocabulary, and `ready`
and `yours` are the only two you may start work in.

`--paths` is a promise about files, so name files (`src/api/routes.py`) or a tree
(`src/api/**`). A bare directory is refused: `src/api` silently holds every file under
it off the board, including ones you were never going to touch. Name a file that does
not exist yet when you are the one about to write it — that is allowed, and is usually
the point of the card.

## Exit codes

|code|meaning|
|---|---|
|0|ok|
|1|cannot listen|
|2|bad argument, refused operation, or invalid file|
|3|**claim denied**|
|130|interrupted|
|141|pipe closed|

**3 means someone else holds it.** Pick another card from `ready`. Never retry the same
claim — the holder's lease has to expire first, and retrying does not make that happen
sooner. There is no `--force` on `claim` because a claim you forced is a claim someone
else is still working under. With `--json` the reason is one JSON document on stdout;
without it the explanation is on stderr, and its last line is always a command you can
run next.

## Leases

A claim expires. Default 30 minutes, or the board's `lease_ttl` — read
`.yamlboard/board.yaml`. Choose deliberately: `--ttl 30s` on a card you are about to
work for an hour is a claim you do not really hold, and the moment it lapses another
agent may take the card out from under you. `renew` before anything long, not after.
`claim` and `renew` take `--ttl 90s|5m|2h|1d`. Outside 30s–24h it is refused, not
quietly clamped: a typo like `100d` should stop the command, not silently become
something you did not ask for.

When a lease lapses it is simply not a claim any more: the card returns to `ready`, and
whoever takes it records `preempted: <you>`.

## When conflicts says you lost

`yamlboard conflicts` compares claims that git merged without complaint. If yours was
overridden, your card carries `overridden_by: <winner>` and the card is back in `ready`.
The winner is the earlier `at`, not the better claim — your work is not necessarily
wrong.

Stop. Run `yamlboard show <card>`. If you already wrote code, do not delete it and do
not overwrite the winner's: leave a note on the card naming what you changed and where,
so the human can adjudicate with the whole picture. Do not edit the card's fields —
the winner's claim is theirs to release.

```bash
yamlboard comment PROD-3 "stale session cache: 4 files under src/cache/, reverting mine" --as grigory
yamlboard conflicts            # read-only
yamlboard conflicts --resolve  # writes overridden_by; a human decision, not yours
```

## Hard rules

- Never edit a file inside another agent's claimed `paths`, however obviously correct the edit is.
- Never edit a card you do not hold — `claim` first, then `done` or `dep add`.
- Never commit another agent's staged files. `yamlboard` refuses to; so must you.
- `git pull --ff-only` before you claim, and again after you finish.
- Finished: `done <card>`, which releases your claim. Blocked: `dep add <card> --blocked-by <id>`. Neither is `release` — releasing an unfinished card hands it to someone else to repeat.
- A human may be watching this board in a browser, so write card text to be read.

## AGENTS.md

`yamlboard skill install` prints the block to paste into the repo's `AGENTS.md`
and saves it to `.agents/skills/yamlboard/AGENTS-block.md`. Use that text
verbatim: it is the copy the CLI owns and the tests check. Do not word your own —
a second hand-written block drifts from the protocol within a release, and an
agent following the stale one believes it complied.
