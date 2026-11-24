# Spec Delta

## Purpose

Path ownership is enforced by honour, so its value comes from reporting a
collision the merge already swallowed. That report is only worth having if the
reader can act on it: naming an id that appears in no file twice tells them
nothing about which file to open, and pointing them at a command that reports
success while changing nothing costs them the one thing a conflict report is for.

## MODIFIED Requirements

### Requirement: Resolution is explicit
A losing claim SHALL remain in place, continuing to block its holder, until a
resolve operation is run. Resolving SHALL mark the losing claim as overridden by
the winner, which makes it no longer a claim, returning its card to the ready pool.
Resolving SHALL also repair a naming fault, and SHALL leave for a human anything it
cannot decide rather than choosing on their behalf.

#### Scenario: Losing a conflict frees the card
- **WHEN** conflicts are resolved
- **THEN** the loser's claim is marked overridden, the card appears in the ready queue, and the winner's claim is untouched

#### Scenario: Nothing is resolved automatically
- **WHEN** an overlap exists and no resolve has been run
- **THEN** the loser still holds its claim and the conflict is still reported

#### Scenario: A choice it cannot make is left alone
- **WHEN** two files cannot both keep one name
- **THEN** resolve changes neither and names both as left for a human

### Requirement: Duplicate ids and dirty trees are reported
The system SHALL report, without repairing, two card files carrying the same id
where no rename can separate them, and a claim whose patterns match files already
modified in the working tree by other work.

A file whose declared id disagrees with its filename is not two cards, and SHALL
be reported as a naming fault that names the file rather than as a duplicate: a
duplicate names an id, and the reader needs the path to fix it.

#### Scenario: Two files, one id
- **WHEN** two card files in the store carry the same id and neither is named for it
- **THEN** the duplicate is reported and neither is silently loaded over the other

#### Scenario: A file named for a different id
- **WHEN** a card file's declared id differs from the stem of its filename
- **THEN** the report names the file, the id it declares, and the id it will take

#### Scenario: The tree already moved
- **WHEN** a live claim's patterns match files modified but uncommitted in the working tree
- **THEN** the overlap is reported and nothing is reverted

## ADDED Requirements

### Requirement: A resolve reports what it did
The summary a resolve prints SHALL describe the actions it actually took rather than
a fixed outcome, and SHALL exit non-zero while anything is left unfixed.

#### Scenario: The summary names the action
- **WHEN** a resolve releases no claim but corrects one id
- **THEN** the output says one id was corrected and does not claim a claim was released

#### Scenario: Resolving twice is inert
- **WHEN** resolve runs again after its own repair
- **THEN** nothing further changes

### Requirement: A repair never destroys a card
No repair, rename, or archive SHALL delete a card the operation did not move. Where
the tool cannot prove a file it is about to remove belongs to the card being
written, it keeps the file.

Re-pointing a dependency SHALL follow an id only when that id is left with no
owner; rewriting an edge that still resolves would move the dependency onto a
different card.

#### Scenario: Archiving past an unrelated namesake
- **WHEN** a card is archived and the archive directory already holds a different card of the same name
- **THEN** the existing archived card survives and both remain readable

#### Scenario: A rename would overwrite a card
- **WHEN** repairing a card's name would write onto a file already holding another card
- **THEN** neither file is changed and both are named in the report

#### Scenario: A dependency that still resolves is left alone
- **WHEN** a card is renamed away from an id another card still answers to
- **THEN** dependencies on that id are unchanged
