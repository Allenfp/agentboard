# agent-cli Specification

## Purpose

The command line is what an agent actually touches. Its contract is therefore about
survivability: a denial must say what to do next, an identity must be unambiguous
before anything is written, and no verb may assume a server is running in a fresh
clone.

## Requirements

### Requirement: No verb requires a running server
Every command SHALL read and write the store directly. A clone with no server
running SHALL be able to initialise a board, list ready work, claim, renew, release,
finish and report conflicts. The dashboard remains an additional writer, not the
only one.

#### Scenario: A fresh clone coordinates with no server
- **WHEN** an agent in a clone with nothing listening runs the claim workflow
- **THEN** every step succeeds against the files alone

### Requirement: Claim verbs name an agent, board verbs do not
Commands that act *on a claim* — taking, renewing, releasing, finishing,
commenting — SHALL act as a named agent, taken from an explicit flag, else an
environment variable, else the local part of the configured git user email, and
the resolved name SHALL be echoed before anything is written. Commands that only
shape the board SHALL NOT demand one. A name supplied to either kind is used.

#### Scenario: Filling a queue you will not work in
- **WHEN** a person creates cards and wires dependencies with no identity configured
- **THEN** every one of those commands succeeds

### Requirement: A claimed identity is a known person
A name that is syntactically invalid, or absent from the board's people, SHALL be
refused, and no command SHALL create a person as a side effect of a claim.

#### Scenario: Initialising a store seeds the roster
- **WHEN** a brand-new board is initialised under a name
- **THEN** that name is written into the people list, because a board with no people can never be claimed from
- **AND** a name that is not a valid slug is not written at all

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
A refused claim SHALL report every reason it has, not only the first, and SHALL
end with a concrete command the caller can run next naming its own agent. A denial
SHALL be distinguishable from other failures by exit status alone.

#### Scenario: A denied claim is actionable
- **WHEN** a claim is refused because another agent holds the card
- **THEN** the output names the holder, the remaining time, the colliding paths and a ready-command for the caller, and the exit status is the claim-denied code

#### Scenario: Card facts read before caller facts
- **WHEN** a claim is refused for several reasons at once
- **THEN** who holds the card and any unfinished blocker come first, and the caller's own colliding paths last
- **AND** nothing counts the lines: card-level facts are the same for every caller, and the count varies with how many apply

#### Scenario: Success and refusal are separable without reading text
- **WHEN** a script runs a claim
- **THEN** it can distinguish success, a refusal because the card is taken, and an invalid argument from the exit status alone

### Requirement: Machine-readable output
Every command SHALL accept a flag producing a single JSON document on standard
output with no prose on either stream. Without that flag, human facts SHALL go to
standard output one per line and only the identity banner to standard error:
`yamlboard claim ... | tee log` should capture what happened, not the chatter
around it, and a scripted caller opts into the flag rather than learning which
lines of prose to discard.

#### Scenario: Success keeps the outcome on stdout
- **WHEN** a claim succeeds without the machine-readable flag
- **THEN** the line naming the claimed card is on standard output and standard error holds nothing but the identity banner

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

#### Scenario: The pre-flight agrees with the claim it predicts
- **WHEN** an agent asks about a card and then attempts the claim that same question described
- **THEN** both answer with the same exit status, and a card the caller already holds is reported as success with its renewal command — never as a refusal pointing at other work

#### Scenario: A refused argument is refused the same way at both doors
- **WHEN** the single-card question is given a path argument the claim would reject, such as a bare directory
- **THEN** it exits with the refused-operation status and names the fix, not with the claim-denied status and not with a traceback

### Requirement: The verb surface
The command line SHALL expose: initialising a store, serving the dashboard,
creating a card, validating and summarising the store, showing one card, listing
cards, listing ready cards, claiming, renewing, releasing, finishing, editing
dependencies, commenting on a card, reporting and resolving conflicts, migrating
an old store, and installing or locating the agent skill. Command help SHALL
document the exit codes, including the claim-denied code.

#### Scenario: Validation replaces the old inspection flags
- **WHEN** a person asks whether a store is valid
- **THEN** one command validates it, prints a summary, exits zero when valid and the invalid-file status otherwise

#### Scenario: Inspecting a store that is not there
- **WHEN** validation is run outside an initialised store
- **THEN** it exits with the invalid status and says the store does not exist, without creating one

### Requirement: Every prescribed step is a verb
Every operation the skill tells an agent to perform SHALL have a verb. An
operation that exists only in the HTTP API is invisible to an agent, which is
pointed at the command line and never at the transport.

#### Scenario: A prescribed recovery step exists as a command
- **WHEN** the skill tells an agent to leave a note for the human after losing a claim
- **THEN** a `comment` verb performs exactly that
- **AND** a prescribed step with no command is a dead end an agent can only fail at

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

#### Scenario: Merging a second file into a live board
- **WHEN** migration runs with `--force` on a board that has changed shape since — a column or field definition its own cards use
- **THEN** the cards and definitions the file brings are added, and the board keeps every column, people and field it already had
- **AND** a card id present in both is refused with nothing written
