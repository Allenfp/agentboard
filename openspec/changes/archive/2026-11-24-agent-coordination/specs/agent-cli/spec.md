# Spec Delta

## Purpose

The command line is what an agent actually touches. Its contract is therefore about
survivability: a denial must say what to do next, an identity must be unambiguous
before anything is written, and no verb may assume a server is running in a fresh
clone.

## ADDED Requirements

### Requirement: No verb requires a running server
Every command SHALL read and write the store directly. A clone with no server
running SHALL be able to initialise a board, list ready work, claim, renew, release,
finish and report conflicts. The dashboard remains an additional writer, not the
only one.

#### Scenario: A fresh clone coordinates with no server
- **WHEN** an agent in a clone with nothing listening runs the claim workflow
- **THEN** every step succeeds against the files alone

### Requirement: Identity resolution
Mutating commands SHALL act as a named agent, taken from an explicit flag, else an
environment variable, else the local part of the configured git user email. The
resolved name SHALL be echoed before anything is written. A name that is
syntactically invalid, or that is not listed among the board's people, SHALL be
refused; the system SHALL NOT create people as a side effect of a claim.

#### Scenario: Environment names the agent
- **WHEN** the environment variable is set and no flag is given
- **THEN** the command announces that identity and acts as it

#### Scenario: Git config as fallback
- **WHEN** neither the flag nor the variable is set and git records an author email
- **THEN** the local part of that address is used

#### Scenario: An unknown agent is refused
- **WHEN** a claim is attempted as a name that is not among the board's people
- **THEN** the command is refused with exit status for a refused operation and no file is written, and the people list is not extended

### Requirement: A denial names the next move
A refused claim SHALL report, in order: who holds the card and for how long, which
of the caller's paths collide and with whose pattern, any unfinished blocker, and
finally a concrete command the caller can run next naming its own agent. A denial
SHALL be distinguishable from other failures by exit status alone.

#### Scenario: A denied claim is actionable
- **WHEN** a claim is refused because another agent holds the card
- **THEN** the output names the holder, the remaining time, the colliding paths and a ready-command for the caller, and the exit status is the claim-denied code

#### Scenario: Success and refusal are separable without reading text
- **WHEN** a script runs a claim
- **THEN** it can distinguish success, a refusal because the card is taken, and an invalid argument from the exit status alone

### Requirement: Machine-readable output
Every command SHALL accept a flag producing a single JSON document on standard
output with no surrounding prose. Human output SHALL go to standard error, so a
scripted caller never has to separate prose from data within one stream.

#### Scenario: Denied claim as JSON
- **WHEN** a refused claim is requested in machine-readable form
- **THEN** one JSON document with a false success field and the same facts as the human output appears on standard output, nothing else does, and the exit status is still the claim-denied code

#### Scenario: A clean list
- **WHEN** the ready queue is requested in machine-readable form
- **THEN** standard output parses as one JSON document

### Requirement: Card state vocabulary
The system SHALL describe a card to an agent with one of four states: ready, taken,
blocked, or yours. Asking about a single card SHALL answer whether the caller may
take it and, if not, why, in one call.

#### Scenario: One question, one call
- **WHEN** an agent asks about a specific card naming itself
- **THEN** the reply states its state and, if it cannot be taken, the reason

### Requirement: The verb surface
The command line SHALL expose: initialising a store, serving the dashboard,
creating a card, validating and summarising the store, showing one card, listing
cards, listing ready cards, claiming, renewing, releasing, finishing, editing
dependencies, reporting and resolving conflicts, migrating an old store, and
installing or locating the agent skill. Command help SHALL document the exit codes,
including the claim-denied code.

#### Scenario: Validation replaces the old inspection flags
- **WHEN** a person asks whether a store is valid
- **THEN** one command validates it, prints a summary, exits zero when valid and the invalid-file status otherwise

#### Scenario: Inspecting a store that is not there
- **WHEN** validation is run outside an initialised store
- **THEN** it exits with the invalid status and says the store does not exist, without creating one

### Requirement: Migration is one-shot
The system SHALL convert a single-file board into the directory store, preserving
titles, columns, people, labels, custom field definitions, card content and card
order, and SHALL NOT read the old format anywhere else. Migrating an already-migrated
store SHALL be refused rather than repeated.

#### Scenario: An old board converts
- **WHEN** a single-file board is migrated
- **THEN** every card exists as its own file with its original id and fields, the board configuration carries the columns, people, labels and field definitions, and the validator accepts the result

#### Scenario: Migrating twice
- **WHEN** migration is run on a store already in the new format
- **THEN** it is refused and nothing is overwritten
