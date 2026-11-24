# Spec Delta

## Purpose

The board capability as shipped: a kanban board that is one YAML document in a git
repository. These requirements change because N concurrent writers cannot share one
document or one write counter.

## MODIFIED Requirements

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

### Requirement: Optimistic concurrency with rev and token
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

## REMOVED Requirements

### Requirement: File resolution and creation

**Reason**: the board is no longer a single file chosen by argument, environment variable, or `./boards.yaml`; it is a `.yamlboard/` directory inside the repository being coordinated, discovered by walking up from the working directory. The read-only inspection flags `--check` and `--print` are replaced by the `check` verb.

**Migration**: run `yamlboard migrate <old-file>` once to produce the directory store, then point `yamlboard` at the repository instead of a file. `--check` becomes `yamlboard check`, and `--print` becomes `yamlboard check --json`.
