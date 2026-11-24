# board

## Purpose

The behaviour yamlboard ships today: a kanban board that is one YAML file in a git
repository. The file is the backend and the source of truth; the browser is a view
on top of it whose saves preserve comments, key order and block scalars. This spec
records the baseline the agent-coordination rewrite deviates from.

## Requirements

### Requirement: The file is the board
The system SHALL keep the entire board — schema version, title, revision counter,
people, labels, board definitions and cards — in one YAML file, and SHALL have no
database, server-side state, account, or build step. Reads SHALL load it with
ruamel round-trip mode and writes SHALL serialise through the same mode so
comments, key order and block scalars survive byte-for-byte.

#### Scenario: A hand edit survives an app write
- **WHEN** a person adds a YAML comment and edits a card in Vim, then edits another card in the browser
- **THEN** the comment and the hand edit are still present, and no unrelated key was reordered or reflowed

#### Scenario: Nothing is written when nothing changed
- **WHEN** an op is applied that results in byte-identical serialised output
- **THEN** the store performs no write and the file's mtime and size are unchanged

### Requirement: File resolution and creation
`yamlboard` SHALL resolve the file from its positional argument, then
`$YAMLBOARD_FILE`, then `./boards.yaml`. Serving a missing file SHALL be legal and
the file SHALL be created on the first save. `--check` and `--print` are read-only
inspections, so a missing file SHALL be an error with exit code 2 rather than an
empty board.

#### Scenario: First run in an empty directory
- **WHEN** a person runs `yamlboard boards.yaml` where no such file exists and then creates a card
- **THEN** the file is created and contains the card

#### Scenario: Checking a file that is not there
- **WHEN** `yamlboard --check missing.yaml` is run
- **THEN** the exit code is 2 and no file is created

### Requirement: Validation before write
Every write SHALL be validated in full before it reaches disk, and an invalid
document SHALL never be written. Writes SHALL go to a temp file then
`os.replace`. Unknown keys SHALL be validation errors, so the accepted key set is
closed: a key the build does not know makes the file unreadable.

#### Scenario: A corrupt file is never produced
- **WHEN** an op would leave a card referring to a deleted label
- **THEN** the request is refused with the offending path and the file on disk is byte-identical to before

### Requirement: Columns are defined by the file
Each board SHALL define its own columns with id, name, colour, order, an optional
per-column `wip_limit`, and an optional `is_done` flag. Column order in the file
SHALL be column order on screen. A column header SHALL flag itself when its card
count exceeds `wip_limit`. Dropping a card into an `is_done` column SHALL offer to
archive it.

#### Scenario: Over the WIP limit
- **WHEN** a fourth card is dropped into a column with `wip_limit: 3`
- **THEN** the drop is permitted and the header marks itself over limit

### Requirement: Cards and their fields
A card SHALL carry id, title, free-text notes, assignee, labels, priority
(`none|low|med|high|urgent`), due date, estimate, links, subtasks, comments,
custom field values, an `archived` flag, and `created`/`updated` timestamps.
Custom fields SHALL be declared per board with types
`text|select|date|number|bool`. Due dates SHALL render red when overdue. Notes
SHALL be stored exactly as typed, with no renderer.

#### Scenario: A required custom field gates new cards only
- **WHEN** a board marks a custom field `required: true` while existing cards lack it
- **THEN** new cards must carry a value and existing cards keep theirs without being rewritten

### Requirement: Card order is array order
A card's position within its column SHALL be its index in the board's `cards`
array, with no separate position field. A drop SHALL preserve the target position.
Moving a card to a different board SHALL re-key it to that board's `key` prefix.

#### Scenario: A reorder moves the block
- **WHEN** a card is dragged to the top of its column
- **THEN** its whole YAML block moves in the file and no `order:` line appears

### Requirement: Card identifiers
Card ids SHALL be `KEY-NN`, where `KEY` is the board's 2–8 capital-letter prefix.
Each board SHALL keep a `next_card` counter, and id allocation SHALL rescan
existing ids so that the minted id is `max(counter, highest existing + 1)` and a
hand-written id is never re-issued.

#### Scenario: A hand-written high id is respected
- **WHEN** `next_card` says 4 but `PROD-9` exists in the file
- **THEN** the next card created is `PROD-10`

### Requirement: Optimistic concurrency with rev and token
Every write SHALL carry the `rev` it was based on (a counter every app write
bumps) and a `token`, an opaque stamp of the exact bytes the client was looking
at. A mismatch SHALL return HTTP 409 carrying the complete fresh state including
git status; the client SHALL adopt it and repaint rather than retry. A concurrent
read-modify-write SHALL be serialised by an `fcntl` lock. A hand edit or `git pull`
does not bump `rev` but does move `token`, so a stale write still bounces.

#### Scenario: Two tabs race
- **WHEN** two tabs post writes based on the same `rev`
- **THEN** one wins and the other receives 409 with the current document and reloads

#### Scenario: A hand edit is protected
- **WHEN** a file is edited outside the app and a tab then posts a write based on the pre-edit bytes
- **THEN** the write is refused with 409 and the hand edit survives

### Requirement: The browser never decides
Every interaction SHALL be an op posted to `/api/apply {op,args,rev,token}`; the
25 registered ops (`card.create`, `card.move`, `column.delete`, …) SHALL be the
only mutations, and the returned document SHALL replace the client's copy.
Ordering, id allocation and validation SHALL live in exactly one place, server-side.

#### Scenario: No client-side ordering
- **WHEN** a card is dropped anywhere on the board
- **THEN** the client sends intent and adopts the column and index the server returned

### Requirement: Views, filters and densities
The UI SHALL provide the board grid, a list view of the same board, and an archive
view; search over id, title, description, subtasks, comments, labels and custom
field values; swimlanes grouped by assignee, label or priority, where dropping a
card into a lane sets that lane's field; light and dark themes; and three card
densities (Minimal: title, labels, who; Compact: adds points and due date; Full:
adds the start of the notes).

#### Scenario: Two people read one board differently
- **WHEN** one browser is set to Minimal and another to Full
- **THEN** both show the same document and the file is unchanged by either preference

### Requirement: Preferences stay out of the file
Theme, view and density SHALL be per-browser preferences stored in `localStorage`
under `yb.*` and SHALL never be written to the board file.

#### Scenario: Changing the theme writes nothing
- **WHEN** a person presses `t`
- **THEN** `git status` stays clean

### Requirement: Keyboard shortcuts
The UI SHALL be operable from the keyboard: `n` new card, `/` search, `a` archive,
`b`/`l` board/list, `d` density, `f` filters, `s` settings, `t` theme, `?` for the
full list. The key handler and the README's shortcut table SHALL always agree.

#### Scenario: The documented list is the real list
- **WHEN** a shortcut is added or removed
- **THEN** the README table changes in the same commit

### Requirement: Commit button
The UI SHALL offer a commit that stages only the board file and commits it with the
person's message, so board history is git history. The git surface SHALL be
exactly `status`, `ls-files`, `add`, `diff --cached`, `commit` and `rev-parse`,
with no shell, and writes SHALL be scoped to that one file. Files staged by anyone
else SHALL make the commit refuse rather than swallow them.

#### Scenario: A foreign staged file is refused
- **WHEN** unrelated files are already staged when Commit is pressed
- **THEN** the commit is refused and those files remain staged and untouched

### Requirement: Loopback-only by default
The server SHALL bind `127.0.0.1:7474` by default and SHALL refuse a non-loopback
bind unless `--read-only` or `--allow-public` is passed, because there is no
authentication. Host and Origin checks SHALL derive from the single
`server.is_loopback` definition.

#### Scenario: A public bind needs an explicit opt-in
- **WHEN** `--host 0.0.0.0` is passed with neither `--read-only` nor `--allow-public`
- **THEN** startup is refused with exit code 2 and nothing is served

#### Scenario: Read-only is honest end to end
- **WHEN** the server runs with `--read-only`
- **THEN** every POST is refused, the client disables write controls, and a caller's success check cannot report a save that never happened

### Requirement: Exit codes
The CLI SHALL exit `0` on success, `1` when it cannot listen, `2` for a bad file,
a bad flag or a refused setting, `130` when interrupted, and `141` when the reader
of a pipe goes away.

#### Scenario: A pipe closes early
- **WHEN** `yamlboard --print boards.yaml | head -1` is run on a large file
- **THEN** the process exits 141 without a Python traceback
