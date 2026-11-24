# Tasks

Order is load-bearing: `store` → `api` → `cli` → `ui` → docs. The read path must
stop collapsing two files into one before the repair is written, or the repair
operates on a store that cannot see the card it is fixing. Run the suite after
every group.

## 1. store: stop losing a file

- [x] 1.1 `_ordered` returns every file it is given instead of one handle per id, so two files declaring one id both stay visible in `list`, `ready` and `conflicts`
- [x] 1.2 `_file_id(path)`: the `id:` a card file declares, `None` on any read failure — unreadable means unprovable
- [x] 1.3 `write_card` unlinks a same-named sibling in the other directory only after proving it declares the id being written; an unprovable sibling stays
- [x] 1.4 Tests: archive past an unrelated namesake keeps both cards; the duplicate pair renders as two cards, not one twice

## 2. api: name the file, repair the id

- [x] 2.1 `_misnamed(tx)` finds a card whose declared `id` disagrees with its filename; reported as `misnamed`, naming the file, never as a bare duplicate
- [x] 2.2 `_misname_block(cards, handle, stem)`: another file that makes the repair unsafe, so a rename that would land on someone else's card is refused rather than executed
- [x] 2.3 `_misname_detail(record)` states what is wrong and what happens next, per file; a blocked pair prints once per file, each line naming both paths
- [x] 2.4 `_repair_misnames(tx, wanted)` rewrites `id:` to match the filename, re-points `blocked_by` only when the old id is left with no owner, and returns `(repaired, needs_human)`
- [x] 2.5 `conflicts()` carries `fixable` and `detail` so the browser renders a button only where `--resolve` would act
- [x] 2.6 `conflicts_resolve()` returns `resolved[]` items tagged with `kind` plus `needs_human[]`; a targeted `card` with nothing to act on is a `BadRequest`
- [x] 2.7 Tests: misnamed-only repair, duplicate-with-canonical, blocked pair leaves both alone, dependency untouched when its id still resolves, resolve twice is inert

## 3. cli: report what was done

- [x] 3.1 `verb_conflicts` prints `bad name:` per file and exits 2 while anything is reported; `--resolve` prints one line per action and a `left for you:` line per refusal
- [x] 3.2 The resolve summary is built from what happened, not a fixed "claim(s) released" string, and exits non-zero while anything is left unfixed
- [x] 3.3 `verb_check` surfaces the naming fault among its conflicts, with the same exit code
- [x] 3.4 The `next:` hint describes correcting an id, not renaming a file — the repair never moves a file

## 4. ui: a button where the tool can act

- [x] 4.1 `openConflicts` renders a misnamed row as `<file> claims to be <declared id>`, titled by what the file declares so it is not circular against its own filename
- [x] 4.2 A `fixable` row gets a "Correct the id" button calling `conflicts.resolve`; a blocked row names both files and offers nothing clickable
- [x] 4.3 The dialog refreshes after a repair and closes when the last conflict clears; the toolbar control keeps counting what remains

## 5. docs and gates

- [x] 5.1 README: the `conflicts` and `check` samples match real output, and the prose says `--resolve` rewrites the `id:` and never moves a file
- [x] 5.2 `docs/CONVENTIONS.md`: P43 extended, and the rule that a repair proves ownership before deleting anything
- [x] 5.3 `src/yamlboard/skills/yamlboard/SKILL.md`: what a `bad name:` report means for an agent — report it with the path, do not resolve it yourself
- [x] 5.4 `.github/workflows/ci.yml`: `ruff check` plus `pytest`, and the example board's `list` as a smoke test

## 6. verify

- [x] 6.1 Full suite green, ruff clean, `openspec validate repair-misnamed-card-ids --strict` exits 0
- [x] 6.2 Browser check: the row renders, "Correct the id" repairs the file on disk, the toast matches, and a surviving overlap keeps the control counting
- [x] 6.3 Two-clone dogfood: two clones mint the same id blind, git raises add/add, a human keeps both, `conflicts` names the borrowed file, `--resolve` corrects the id then releases the overlap loser, both clones converge to exit 0
