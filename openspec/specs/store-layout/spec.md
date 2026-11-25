# store-layout Specification

## Purpose

Defines how a board is laid out on disk so that N writers can commit to one
repository without touching each other's bytes: a tracked directory holding one
small file per card, plus board-level configuration that changes rarely.

## Requirements

### Requirement: The board is a directory
The system SHALL store a board as a directory named `.agentboard/` inside the
repository it coordinates, containing a board configuration file, one file per
card, and an archive directory for retired cards. The system SHALL NOT keep card
data in the board configuration file.

#### Scenario: A fresh board
- **WHEN** a person initialises a board in an empty git repository
- **THEN** the directory contains a configuration file and empty card and archive directories, and the command prints what to run next

#### Scenario: One card edit touches one path
- **WHEN** an agent edits an existing card
- **THEN** exactly one file changes on disk, named after that card's id, and the board configuration file is unmodified

#### Scenario: Creating a card touches two
- **WHEN** an agent creates a card
- **THEN** the card's file appears and the board configuration file's `next_card` advances in the same operation
- **AND** two clones creating from the same commit SHALL be detected as a duplicate id by `conflicts`, not silently merged

### Requirement: Card files are named by card id
Each card file SHALL be named after the card's id, so a card's identity and its
path are the same fact. Where the two disagree, the filename is authoritative and
the declared id SHALL be corrected to match it, rather than the file renamed to
match the id. Archiving a card SHALL move its file into the archive directory,
which git records as a rename.

#### Scenario: Archiving is a rename
- **WHEN** a card is archived
- **THEN** its file appears under the archive directory and its previous path is gone

#### Scenario: Two cards archived concurrently
- **WHEN** two clones archive two different cards and merge
- **THEN** git merges without a textual conflict

#### Scenario: Repair chooses the filename over the id
- **WHEN** `cards/WEB-9.yaml` declares `id: WEB-1`
- **THEN** the repair rewrites the id to `WEB-9` and the file keeps its path

### Requirement: No board-wide revision counter
The system SHALL NOT maintain a board-wide write counter. Two writes to different
cards SHALL be independent, and neither SHALL invalidate the other. Concurrency
control SHALL be per file.

#### Scenario: Unrelated concurrent writes both succeed
- **WHEN** two clones each write a different card based on the same commit and both commit and merge
- **THEN** both writes survive and git reports no conflict

### Requirement: Schema version is enforced on read
Every file in the store SHALL declare the schema version it was written by. A file
whose version exceeds what the build understands SHALL be refused with a message
naming the version found, the versions understood, and the way forward. A file at
a lower version SHALL be refused the same way: there is no migration command to
point at, so the message names an action instead.

#### Scenario: A newer file than the build
- **WHEN** a store declares a version this build does not understand
- **THEN** reading it fails with both version numbers named and nothing is written

#### Scenario: An older store than the build
- **WHEN** a store declares a version this build did not write
- **THEN** the command refuses and names an action (start a new board, or restore a backup) rather than silently reading it

### Requirement: Card ids are minted without re-issuing
Card ids SHALL be a board key prefix, a hyphen, and a number. Each board SHALL
track the next number, and minting SHALL also consider ids that already exist so a
hand-written id is never re-issued.

#### Scenario: A hand-written high id is respected
- **WHEN** the counter says 4 and a card with id 9 exists
- **THEN** the next minted id is 10

#### Scenario: Two clones mint the same id
- **WHEN** two clones each create a card at the same number and merge
- **THEN** the duplicate is reported as a conflict and nothing changes until a human resolves it

#### Scenario: Merging two copies of one filename
- **WHEN** the conflict on a card file is settled by keeping both copies, so one sits on a borrowed name
- **THEN** the naming fault is reported, and any renumbering happens only through the explicit resolve that names the file and the id it takes

### Requirement: Transient files stay untracked
The lock file and interrupted-write temporary files the system creates inside the
board directory SHALL be ignored by git, and the ignore rules SHALL be specific
enough that the board directory itself and all card files remain tracked.

#### Scenario: The rules travel with the board
- **WHEN** a board is initialised inside a repository whose root ignore file says nothing about this tool
- **THEN** the board directory contains its own ignore file covering the lock and the temporary files
- **AND** the repository's root ignore file is not modified

#### Scenario: The owner's own ignore file wins
- **WHEN** the board directory already contains an ignore file
- **THEN** initialisation leaves its bytes exactly as they were

#### Scenario: The board is tracked but its lock is not
- **WHEN** a write happens and `git status` is consulted
- **THEN** the changed card files are listed and neither the lock nor any temporary file appears

#### Scenario: An initialised repository tracks the board
- **WHEN** a board is initialised in a fresh repository and `git check-ignore` is consulted for the configuration file
- **THEN** the configuration file is not ignored

### Requirement: Card order lives on the card
Card order within a column SHALL be carried by the card itself as a position
value, so that ordering needs no shared list and reordering writes only the card
that moved. Equal positions SHALL break deterministically, and reading the same
store twice SHALL produce the same order.

#### Scenario: Reorder writes one file
- **WHEN** a card is dragged to a new position within its column
- **THEN** only that card's file changes

#### Scenario: A move names the card it lands in front of
- **WHEN** a move requests position `before` some card B
- **THEN** the moved card reads immediately ahead of B, not behind it
- **AND** a move with no target appends to the end of the column
- **AND** a target that no longer exists in the column appends, rather than being placed at a position inferred from the absent card
- **AND** a card moved in front of itself keeps the position it had

#### Scenario: The gap is computed against the column the move produces
- **WHEN** the moved card's own position would otherwise be a neighbour
- **THEN** it is excluded, so the new position sits strictly between the two cards that will remain adjacent

#### Scenario: Two clones reorder different cards
- **WHEN** two clones each move a different card in the same column and merge
- **THEN** both moves survive with no conflict to resolve

#### Scenario: Identical positions
- **WHEN** two cards in one column end up with the same position
- **THEN** their relative order is fixed and the same in every reader

#### Scenario: Ordering stays possible after many inserts
- **WHEN** a card is inserted between two neighbours with no free position left between them
- **THEN** the store renumbers the affected column and the requested order is preserved

### Requirement: One id in two files stays two files
Resolving a card by id internally SHALL NOT collapse two card files that declare
the same id into one. While the disagreement exists, every card file in the store
SHALL stay visible in listing and in the conflict report.

#### Scenario: Two files declare one id
- **WHEN** a store holds two card files declaring the same id
- **THEN** both files appear in listing and in the conflict report, not one twice
