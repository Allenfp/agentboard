# Spec Delta

## Purpose

Defines how a board is laid out on disk so that N writers can commit to one
repository without touching each other's bytes: a tracked directory holding one
small file per card, plus board-level configuration that changes rarely.

## ADDED Requirements

### Requirement: The board is a directory
The system SHALL store a board as a directory named `.yamlboard/` inside the
repository it coordinates, containing a board configuration file, one file per
card, and an archive directory for retired cards. The system SHALL NOT keep card
data in the board configuration file.

#### Scenario: A fresh board
- **WHEN** a person initialises a board in an empty git repository
- **THEN** the directory contains a configuration file and empty card and archive directories, and the command prints what to run next

#### Scenario: One card write touches one path
- **WHEN** an agent edits a card
- **THEN** exactly one file changes on disk, named after that card's id, and the board configuration file is unmodified

### Requirement: Card files are named by card id
Each card file SHALL be named after the card's id, so a card's identity and its
path are the same fact. Archiving a card SHALL move its file into the archive
directory, which git records as a rename.

#### Scenario: Archiving is a rename
- **WHEN** a card is archived
- **THEN** its file appears under the archive directory and its previous path is gone

#### Scenario: Two cards archived concurrently
- **WHEN** two clones archive two different cards and merge
- **THEN** git merges without a textual conflict

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
an older understood version SHALL be refused with a pointer to the migration
command.

#### Scenario: A newer file than the build
- **WHEN** a store declares a version this build does not understand
- **THEN** reading it fails with both version numbers named and nothing is written

#### Scenario: An un-migrated store
- **WHEN** a store still uses the single-file format
- **THEN** the command refuses and names the migration command rather than silently reading it

### Requirement: Card ids are minted without re-issuing
Card ids SHALL be a board key prefix, a hyphen, and a number. Each board SHALL
track the next number, and minting SHALL also consider ids that already exist so a
hand-written id is never re-issued.

#### Scenario: A hand-written high id is respected
- **WHEN** the counter says 4 and a card with id 9 exists
- **THEN** the next minted id is 10

#### Scenario: Two clones mint the same id
- **WHEN** two clones each create a card at the same number and merge, producing two files with the same name
- **THEN** the duplicate is reported as a conflict and neither card is silently renumbered or dropped

### Requirement: Transient files stay untracked
The lock file and interrupted-write temporary files the system creates inside the
board directory SHALL be ignored by git, and the ignore rules SHALL be specific
enough that the board directory itself and all card files remain tracked.

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

#### Scenario: Two clones reorder different cards
- **WHEN** two clones each move a different card in the same column and merge
- **THEN** both moves survive with no conflict to resolve

#### Scenario: Identical positions
- **WHEN** two cards in one column end up with the same position
- **THEN** their relative order is fixed and the same in every reader

#### Scenario: Ordering stays possible after many inserts
- **WHEN** a card is inserted between two neighbours with no free position left between them
- **THEN** the store renumbers the affected column and the requested order is preserved
