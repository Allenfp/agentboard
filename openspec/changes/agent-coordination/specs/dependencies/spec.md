# Spec Delta

## Purpose

Some work cannot start until other work finishes. This capability defines the
dependency edges a card may declare, the refusal to accept a cycle, and the ready
queue that is the only list agents are told to pick from.

## ADDED Requirements

### Requirement: Cards declare blockers
A card SHALL be able to list other cards it is blocked by. Every listed id SHALL
exist in the store. A card whose blockers are all finished SHALL count as
unblocked.

#### Scenario: Blocking a known card
- **WHEN** a card declares a blocker that exists
- **THEN** the edge is recorded and the card stops appearing as available

#### Scenario: A dangling reference
- **WHEN** a card declares a blocker whose id is not in the store
- **THEN** the change is refused and the unknown id is named

### Requirement: Cycles are refused
Adding a dependency that would create a cycle SHALL be refused, and the refusal
SHALL name every card in the cycle. A store already containing a cycle SHALL be
reported invalid rather than partially evaluated.

#### Scenario: A two-card cycle
- **WHEN** A is blocked by B and B is then declared blocked by A
- **THEN** the second change is refused with exit status for a refused operation and the message names both A and B

#### Scenario: A longer cycle is named in order
- **WHEN** a chain A → B → C is closed by declaring C blocked by A
- **THEN** the refusal names the full cycle path, not only the two cards just touched

### Requirement: The ready queue is the only thing agents are offered
A card SHALL be ready when it is unclaimed or its claim has expired, it has no
unfinished blocker, and none of its declared paths collide with an unexpired claim.
Ready cards SHALL be ordered by priority, then by creation time.

#### Scenario: Blocked cards never appear
- **WHEN** the ready queue is requested while some cards are blocked
- **THEN** no blocked card is listed

#### Scenario: An expired lease unblocks work
- **WHEN** the only thing blocking a card is a lease that has expired
- **THEN** the card is listed as ready

#### Scenario: Paths already spoken for
- **WHEN** a card's declared paths collide with another agent's unexpired claim
- **THEN** the card is not listed as ready

#### Scenario: Order is deterministic
- **WHEN** two cards are ready with the same priority
- **THEN** the older card is listed first

### Requirement: The ready filter is available to both audiences
The command line SHALL expose the ready queue, optionally narrowed to paths the
caller intends to touch. The browser SHALL expose the same filter over the same
cards.

#### Scenario: Narrowing by intent
- **WHEN** an agent asks for ready work limited to a pattern it plans to edit
- **THEN** only cards ready for those paths are returned

#### Scenario: The human sees the same set
- **WHEN** the board's ready filter is engaged
- **THEN** exactly the cards the command line reports as ready remain visible
