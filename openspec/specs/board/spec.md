# board

## Purpose

The behaviour yamlboard ships today: a kanban board that is one YAML file in a git
repository. The file is the backend and the source of truth; the browser is a view
on top of it whose saves preserve comments, key order and block scalars. This spec
records the baseline the agent-coordination rewrite deviates from.

## Requirements

### Requirement: The file is the board
The system SHALL keep a board in a tracked directory of small files rather than one
document: board configuration in one file, each card in a file of its own. Reads and
writes SHALL preserve comments, key order and block scalars in every file they
touch. There SHALL be no database, server-side state, account, or build step.

#### Scenario: A hand edit survives an app write
- **WHEN** a person adds a YAML comment and edits a card by hand, then edits another card in the browser
- **THEN** the comment and the hand edit are still present, and no unrelated file was rewritten

#### Scenario: Nothing is written when nothing changed
- **WHEN** an operation results in byte-identical serialised output
- **THEN** the system performs no write and the file's modification time is unchanged

### Requirement: Validation before write
Every write SHALL be validated in full before it reaches disk, and an invalid
document SHALL never be written. Writes SHALL go to a temp file then
`os.replace`. Unknown keys SHALL be validation errors, so the accepted key set is
closed: a key the build does not know makes the file unreadable.

#### Scenario: A corrupt file is never produced
- **WHEN** an op would leave a card referring to a deleted label
- **THEN** the request is refused with the offending path and the file on disk is byte-identical to before

### Requirement: Columns are defined by the file
Each board SHALL define its own columns with `id`, `name`, `color`, an optional
`wip_limit`, and an optional `is_done` flag. A column's position is its index in
the `columns` array, so reordering columns rewrites the board file. A column
header SHALL flag itself when its card count exceeds `wip_limit`. Dropping a card
into an `is_done` column SHALL offer to archive it.

#### Scenario: A column carries no order key
- **WHEN** a column is written with an `order:` key
- **THEN** it is refused as an unknown key, because the array index is the order and two sources of it would disagree

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

### Requirement: Card order is a number on the card
A card's position within its column SHALL be a float `order` field on the card
itself. There is no shared `cards` array. A drop SHALL place the card where it was
aimed, and a move that runs out of room between two neighbours SHALL renumber the
column rather than fail. Moving a card to a different board SHALL re-key it to
that board's `key` prefix.

#### Scenario: Why not a shared array
- **WHEN** two people reorder the same stretch of a column in two clones
- **THEN** each writes only the card it moved, and git merges the two without a conflict

#### Scenario: A reorder touches one file
- **WHEN** a card is dragged to the top of its column
- **THEN** only that card's file changes, and it carries an `order:` below every other card in the column

### Requirement: Card identifiers
Card ids SHALL be `KEY-NN`, where `KEY` is the board's 2–8 capital-letter prefix.
Each board SHALL keep a `next_card` counter, and id allocation SHALL rescan
existing ids so that the minted id is `max(counter, highest existing + 1)` and a
hand-written id is never re-issued.

#### Scenario: A hand-written high id is respected
- **WHEN** `next_card` says 4 but `PROD-9` exists in the file
- **THEN** the next card created is `PROD-10`

### Requirement: Optimistic concurrency with per-file tokens
Every write SHALL carry a token, an opaque stamp of the exact bytes of the file it was based on. A mismatch SHALL be refused with the current content of what it conflicts with, and the client SHALL adopt it rather than retry. Concurrent writes to the same file SHALL be serialised by a lock. There SHALL be no board-wide revision counter: writes to different cards are independent, and a hand edit or pull to one card does not invalidate work on another.

#### Scenario: Two tabs race
- **WHEN** two writers post changes to the same card based on the same bytes
- **THEN** one wins and the other is refused with the current content and refreshes

#### Scenario: Unrelated cards do not collide
- **WHEN** two clones write different cards and merge
- **THEN** both writes survive with no conflict to resolve

#### Scenario: A hand edit is protected
- **WHEN** a card file is edited outside the app and a writer then posts a change based on the pre-edit bytes
- **THEN** that write is refused and the hand edit survives

#### Scenario: A deletion is protected
- **WHEN** a card file is removed outside the app and a writer then posts a change based on the bytes it read
- **THEN** that write is refused and the card stays deleted, rather than being rewritten from the stale snapshot

### Requirement: The browser never decides
Every interaction SHALL be an op posted to `/api/apply {op,args,tokens}`, where
`tokens` names each file the client read (`"board"` plus every card id); the
33 registered ops (`card.create`, `card.move`, `column.delete`, …) SHALL be the
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
`b`/`l` board/list, `d` density, `f` filters, `s` settings, `t` theme, `r` the
ready filter, `c` claim the focused card as the browser's chosen agent, `?` for the
full list. The key handler, the in-app shortcut list and the README's shortcut table
SHALL always agree.

#### Scenario: The documented list is the real list
- **WHEN** a shortcut is added or removed
- **THEN** the README table and the in-app list change in the same commit

#### Scenario: Take it from the keyboard
- **WHEN** a ready card is focused and `c` is pressed
- **THEN** the claim is written server-side and the card shows the claim without a reload

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
The CLI SHALL exit `0` on success, `1` when it cannot listen, `2` for a bad file, a
bad flag, or a refused operation, `3` when a claim is denied because another agent
holds the work, `130` when interrupted, and `141` when the reader of a pipe goes
away.

#### Scenario: A denied claim is distinguishable
- **WHEN** a claim is refused because another agent holds the card
- **THEN** the exit status is the claim-denied code and not the general failure code

#### Scenario: A pipe closes early
- **WHEN** machine-readable output is piped to a reader that exits early
- **THEN** the process exits with the broken-pipe status and prints no traceback
