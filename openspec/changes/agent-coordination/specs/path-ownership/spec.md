# Spec Delta

## Purpose

The coordination actually prevents collisions only if it covers files, not just
card ids. This capability defines what a claim says about the working tree, what
the tool can promise locally, and how it must behave when two machines each made a
clean commit that claims the same files — a case version control cannot arbitrate.

## ADDED Requirements

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

### Requirement: A pattern that matches nothing is a teaching error
A declared pattern that matches no file in the working tree SHALL be refused with a
message stating what was matched and how to express the intent, rather than a bare
invalid-value error.

#### Scenario: A directory named as a pattern
- **WHEN** an agent declares a bare directory path with no wildcard
- **THEN** the refusal states how many files it would cover and shows the tree form to use instead

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

#### Scenario: Losing a conflict frees the card
- **WHEN** conflicts are resolved
- **THEN** the loser's claim is marked overridden, the card appears in the ready queue, and the winner's claim is untouched

#### Scenario: Nothing is resolved automatically
- **WHEN** an overlap exists and no resolve has been run
- **THEN** the loser still holds its claim and the conflict is still reported

### Requirement: Duplicate ids and dirty trees are reported
The system SHALL also report, without silently repairing, two cards claiming the
same id from two merges, and a claim whose patterns match files already modified in
the working tree by other work.

#### Scenario: Two files, one id
- **WHEN** two card files in the store carry the same id
- **THEN** the duplicate is reported and neither is silently loaded over the other

#### Scenario: The tree already moved
- **WHEN** a live claim's patterns match files modified but uncommitted in the working tree
- **THEN** the overlap is reported and nothing is reverted

### Requirement: Conflicts reach the human
Every reported conflict SHALL be visible in the browser as well as in command
output, and resolving from the browser SHALL produce the same recorded outcome as
resolving from the command line.

#### Scenario: A human adjudicates
- **WHEN** a human opens the conflict view and resolves a pair
- **THEN** the same override the command line would write is written, and the conflict indicator disappears
