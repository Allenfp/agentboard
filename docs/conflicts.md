# Conflicts: detected, never prevented

Two clones, two card files, two paths. Each claims `src/api/**`, git merges the
two files without a single complaint, and both agents believe they hold the same
directory. Git has no arbiter and neither does yamlboard — there is no server in
the loop to become one. So this is detect-and-resolve with a deterministic winner,
surfaced to a human.

## Reporting

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
and a report can never become a silent edit of somebody else's claim.

The same sweep reports three things, and only the first two are fixable:

1. **Two live claims whose globs intersect.** Winner: earliest `at`; a tie goes
   to the lexicographically smaller agent name. Deterministic on purpose — two
   clones that never talk must name the same winner.
2. **A card whose `id:` disagrees with its filename** — the ordinary result of a
   merge where you kept both copies of `cards/WEB-1.yaml` and one landed on a
   borrowed name. The filename is the address every reader uses, so the id is the
   half that is wrong.
3. **A claim whose paths match files already modified in your working tree.**
   Report only: nothing here has decided who is right about uncommitted code.

`conflicts` exits 2 when it has something to report, so a CI gate fails on a board
that is lying about itself.

## Resolving

Only `--resolve` writes, and it writes `overridden_by: ada` into the loser's
claim — which makes that claim inert, so the card is back in `ready` on the next
read:

```console
$ yamlboard conflicts --resolve --as ada
  as ada
PROD-3: grigory overridden by ada
1 claim(s) released; those cards are ready again

$ yamlboard conflicts
no conflicts - no two live claims share a path, and no ids collide.
```

For a misnamed card, `--resolve` rewrites the `id:` to match the filename; the
file itself is never moved. When two files want the same name it says so and
leaves both alone rather than picking a survivor — losing a card to make the
counts line up is not a repair.

`--resolve` demands an identity (`--as`, or `$YAMLBOARD_AGENT`): it is the one
operation that overrides another agent's claim, and the audit trail has to name
who did it. It is also asked of the *store*, not of the caller's filtered view —
gating it on what one agent can see would let it report "nothing to resolve"
beside a conflict it could still clear.

## Losing, as an agent

If `conflicts` names you as the loser, or a card you were working on comes back
`overridden_by`, stop editing those paths. The winner keeps the files; your card
is in `ready` again and you can re-take it with a narrower `--paths` that does not
collide, or leave it. Do not re-claim and hope.

`git pull --ff-only` before you claim and after you finish is what keeps this list
short. The longer your view of the board lags, the more often you will be the
loser.

## Prove it, in two clones

```bash
git clone . clone-a && git clone . clone-b
(cd clone-a && yamlboard claim WEB-1 --as ada     --paths 'src/**')
(cd clone-b && yamlboard claim WEB-2 --as grigory --paths 'src/api/**')
(cd clone-a && git add -A && git commit -qm 'ada claims')
(cd clone-b && git pull --ff-only && yamlboard conflicts)   # ada wins, earlier `at`
```

Git merges the two card files with **zero** textual conflicts. That is exactly the
problem: a clean merge is not an absence of collision, it is two claims that have
not met yet.
