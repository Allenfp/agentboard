# Proposal

## Why

A card file's name and the `id:` it declares are the same fact, and a merge can
break that: two clones mint `WEB-1`, git conflicts on `cards/WEB-1.yaml`, and the
human keeps both copies — the second lands on a borrowed name still declaring
`id: WEB-1`. Every reader then disagrees. `locate()` and `card_ids()` answer by
filename, `find_card()` by content, so the next `claim WEB-1` writes
`WEB-1.yaml` and leaves the borrowed file behind as a stale twin that keeps
appearing in `list` under an id nothing can load. `conflicts` reported it as a
"duplicate id" and pointed at `conflicts --resolve`, which released zero claims
and exited 0 — advice that was both useless and reported as a success.

Two latent bugs made the state worse than visible. `store._ordered` keyed card
files by id, so the pair collapsed into one handle: one card vanished from
`list`, `ready` and `conflicts`, its twin rendered twice. And `write_card`
unlinked the same-named file in the other directory unconditionally — the move it
means when a card is archived, and a silent delete when the sibling is an
unrelated card that merely shares the name.

## What Changes

- `conflicts` reports a new `misnamed` kind naming the **file**, not just the id,
  so the reader has something to open.
- `conflicts --resolve` repairs a misnamed card by rewriting `id:` to match its
  filename, and re-points `blocked_by` edges only when the old id is left with no
  owner. It refuses when the rename would land on another card's file, naming
  both, and exits `2` while anything is left for a human.
- **BREAKING** (payload shape) `conflicts.resolve`'s `resolved[]` items carry a
  `kind`, and the result gains `needs_human[]`.
- `store._ordered` returns every file it is given instead of one per id, so both
  cards in a duplicate pair stay visible.
- `write_card` proves a same-named sibling declares the id it is about to delete
  before unlinking it.
- `check` and the conflict view state what `--resolve` actually did, replacing the
  unconditional "claim(s) released" line.
- The browser's conflict dialog gets a repair button for a misnamed card.

## Capabilities

**New Capabilities**: none.

**Modified Capabilities**:

- `path-ownership` — the "reported without silently repairing" requirement splits:
  duplicate ids stay report-only, a filename/id disagreement becomes repairable.
- `store-layout` — makes the filename the address a stated invariant, with the
  two read/write consequences that keep it true.
- `board-ui` — a misnamed card in the conflict view carries an action, not just
  advice.

## Impact

`src/yamlboard/api.py` (`_misnamed`, `_repair_misnames`, `conflicts`,
`conflicts_resolve`), `src/yamlboard/store.py` (`_ordered`, `_file_id`,
`write_card`), `src/yamlboard/cli.py` (`verb_check`, `verb_conflicts`, conflict
rendering), `src/yamlboard/static/app.js` (`openConflicts`), README,
`docs/CONVENTIONS.md` (P43, new P43b), `.github/workflows/ci.yml`. No new
dependencies, no format change, no migration: the store already intended
filename == id.
