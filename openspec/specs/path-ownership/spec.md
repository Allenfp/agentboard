# path-ownership Specification

## Purpose

The coordination actually prevents collisions only if it covers files, not just
card ids. This capability defines what a claim says about the working tree, what
the tool can promise locally, and how it must behave when two machines each made a
clean commit that claims the same files — a case version control cannot arbitrate.

## Requirements

### Requirement: A claim declares the paths it will touch
A claim SHALL carry a list of path patterns describing the files the work will
modify. A claim with no declared paths SHALL cover only the card and SHALL be
recorded as covering nothing in the working tree.

#### Scenario: Claiming a module
- **WHEN** an agent claims a card declaring a pattern covering a directory tree
- **THEN** the claim records that pattern and other agents' readiness checks account for it

#### Scenario: Claiming without paths
- **WHEN** an agent claims a card declaring no paths
- **THEN** the claim blocks the card but does not block any file pattern

### Requirement: Intersecting patterns are refused on claim
Claiming SHALL be refused when any declared pattern intersects a pattern held by an
unexpired claim on a different card. The refusal SHALL name the colliding pattern
and its holder. Intersections SHALL be evaluated in both directions and
conservatively: an ambiguous comparison SHALL be treated as a collision.

#### Scenario: A narrower pattern inside a held tree
- **WHEN** one agent holds a pattern covering a whole source tree and another claims a card declaring a pattern for a subdirectory of it
- **THEN** the second claim is refused and names the first agent and the covering pattern

#### Scenario: Disjoint patterns
- **WHEN** a claim declares a pattern that shares no file with any unexpired claim
- **THEN** the claim succeeds

### Requirement: A directory named as a pattern is refused
A bare directory path with no wildcard SHALL be refused, naming the tree form to
use instead — it reads like the name of one file and claims an entire subtree.

#### Scenario: A directory named as a pattern
- **WHEN** an agent declares a bare directory path with no wildcard
- **THEN** the refusal says it would claim every file beneath it and shows the tree form to use instead

#### Scenario: The refusal gives no file count
- **WHEN** a directory is refused
- **THEN** the message states no number of files it would cover, because nothing in the store walks the tree and the count would differ in a shallow clone, a partial checkout, or a tree mid-`git clean`

### Requirement: A pattern that matches nothing is valid
Matching nothing SHALL NOT be an error.

#### Scenario: A file that does not exist yet
- **WHEN** an agent claims a card with a path for a file it is about to write
- **THEN** the claim is accepted
- **AND** refusing it would make new files unclaimable and push agents to claim a whole directory instead, the exact trap the rule above exists to close

### Requirement: Cross-machine overlap is detected, not prevented
Two clones SHALL be able to take claims whose patterns intersect without either
being refused, because neither can see the other. After they are merged, the system
SHALL detect the overlap and report it. Detection SHALL NOT modify anything.

#### Scenario: Two machines claim the same tree
- **WHEN** two clones each claim overlapping patterns on different cards, commit, and merge
- **THEN** the merge completes with no textual conflict and the overlap is reported as a conflict naming both agents and both patterns

#### Scenario: Detection does not write
- **WHEN** overlapping claims are reported
- **THEN** no file has changed and version control still reports a clean tree

### Requirement: The winner is deterministic
Among overlapping unexpired claims the winner SHALL be the one taken earliest, ties
broken by the lexicographically smaller agent name. The outcome SHALL not depend on
which clone asks, on file order, or on merge direction.

#### Scenario: Both clones agree
- **WHEN** both clones report the same overlap
- **THEN** both name the same winner

#### Scenario: Identical timestamps
- **WHEN** two overlapping claims were taken at the same instant
- **THEN** the winner is the alphabetically first agent name

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

#### Scenario: The work is the claimant's own
- **WHEN** the report is requested naming the agent who holds that claim
- **THEN** that claim is not reported — a modified file cannot say who modified it, and edits inside a path you were authorised to take are the work the claim was taken for
- **AND** the same claim is still reported to any other asker, including a gate running with no identity at all
- **AND** the human dashboard keeps showing it to everyone, because adjudicating it is a person's job, not the claimant's

### Requirement: Conflicts reach the human
Every reported conflict SHALL be visible in the browser as well as in command
output, and resolving from the browser SHALL produce the same recorded outcome as
resolving from the command line.

#### Scenario: A human adjudicates
- **WHEN** a human opens the conflict view and resolves a pair
- **THEN** the same override the command line would write is written, and the conflict indicator disappears

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
