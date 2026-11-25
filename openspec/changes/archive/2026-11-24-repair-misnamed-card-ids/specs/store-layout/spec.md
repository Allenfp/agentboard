# Spec Delta

## Purpose

Cards are files, so a card's name and its path are meant to be one fact. This makes
that an explicit invariant rather than a convention, and states the two places
where quietly breaking it costs a card rather than merely confusing a reader.

## MODIFIED Requirements

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

## ADDED Requirements

### Requirement: One id in two files stays two files
Resolving a card by id internally SHALL NOT collapse two card files that declare
the same id into one. While the disagreement exists, every card file in the store
SHALL stay visible in listing and in the conflict report.

#### Scenario: Two files declare one id
- **WHEN** a store holds two card files declaring the same id
- **THEN** both files appear in listing and in the conflict report, not one twice
