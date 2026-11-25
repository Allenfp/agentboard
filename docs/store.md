# The file format

`.yamlboard/` is committed to the repository it coordinates. That one decision
buys everything else: a fresh clone arrives already knowing who holds what, a card
write is one path, and git becomes the concurrency control rather than the thing
that has to be worked around.

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

## `board.yaml`

Trimmed to one board and three columns, with the end-of-line notes added for
reading; the fixture's own file is `examples/demo/.yamlboard/board.yaml`. A
document only has to carry `version`, `title` and `boards`, and a board only
`id`, `key`, `title` and `columns` — everything else has a default (the minimal
shape is pinned by `test_a_minimal_document_validates`).

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

## A card

`examples/demo/.yamlboard/cards/PROD-3.yaml`, byte for byte — a card is allowed
to carry comments, and this one's explain their own lease:

```yaml
id: PROD-3
title: An unknown op should name the ops it does know
desc: |
  An unknown op came back as a bare 400 with no hint, so a caller that cannot see what the server accepts has to guess, and a guess costs a round trip every time.
column: review
order: 3072.0
labels:
  - bug
priority: high
assignee: grigory
due: 2026-11-27
estimate: 2
comments:
  - at: 2026-11-22T11:45:00
    by: ras
    text: This one overlaps PROD-2 - conflicts names both holders.
fields:
  risk: false
claim:
  # A hand-written lease so a fresh clone shows what a live claim looks like.
  # It expires, the card goes back to `ready`, and that is the feature working.
  agent: grigory
  at: 2026-11-25T09:30:00
  expires: 2026-11-26T09:30:00
  paths:
    - src/api/routes.py
  note: one file, but it sits inside ada's tree; see conflicts
archived: false
created: 2026-11-21T13:05:00
updated: 2026-11-24T00:58:00
```

`order` is spaced by 1024 so inserting a card rewrites one file, not a column.
`assignee` is set by `claim` and cleared by `release`. The free text in `desc`
stays a block scalar, kept byte-for-byte and never re-rendered.

There is no `status` key inside `claim:`. A second source of truth for "who holds
this" is how a board starts lying about itself: a claim is live if `expires` is in
the future and `overridden_by` is unset, and nothing else. `preempted` names an
agent whose *expired* claim this one replaced; `overridden_by` names the winner
that beat it in a live overlap.

Key sets are closed. An unrecognised key is an error **on read**, so a key the
code uses but the schema forgot would make a real person's card permanently
unreadable — the fix for a typo is in the message, and it names the allowed keys.
See CONVENTIONS P39 for the tuples that have to move together.

## Play with the shipped store instead of typing any of this

```bash
cp -r examples/demo/.yamlboard ./ && yamlboard serve
```

Two boards, fourteen live cards plus one in `archive/`, four live claims and one
reported path overlap, so `check` and `conflicts` have something to say:

```console
$ yamlboard check --dir examples/demo
/home/you/my-repo/examples/demo/.yamlboard
  2 board(s), 14 card(s), 3 people
  conflict: overlap: PROD-2 (ada) and PROD-3 (grigory) claim the same files

1 conflict(s): yamlboard conflicts --resolve frees the loser of an overlap and corrects a mismatched card id
```

Its leases are timestamps, not promises: hand-written so a fresh clone opens on a
board with something in flight, and they lapse — which sends those cards back to
`ready`. That is the mechanism working, not a fixture gone stale. To put the
tour's traffic back on (`check` reporting that overlap again), renew them:

```bash
yamlboard renew PROD-2 --as ada   && yamlboard renew PROD-7 --as ras
yamlboard renew PROD-3 --as grigory && yamlboard renew PROD-6 --as ras
```

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
failed, so it carries `claim_denied: true` and the explanation lines instead.

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
