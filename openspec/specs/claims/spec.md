# claims Specification

## Purpose

Card leases: how an agent takes a card for a bounded time, how it keeps it, how it
gives it back, and what happens when it stops answering. A claim is a promise to
other agents, so its denial and its expiry have to be as well-defined as its
taking.

## Requirements

### Requirement: A card carries at most one live claim
A card SHALL record zero or one claims. A claim SHALL name the agent holding it,
the moment it was taken, the moment it expires, the path patterns it covers, and an
optional note. Only the keys the current schema declares are accepted; an
unrecognised key in a claim makes the card invalid.

#### Scenario: Taking a free card
- **WHEN** an agent claims an unclaimed card
- **THEN** the card records that agent, the time taken, the expiry, and the declared paths, and the exit status is success

#### Scenario: An unknown key inside a claim
- **WHEN** a card file contains a claim key the build does not declare
- **THEN** the card is reported invalid rather than silently accepted

### Requirement: A live claim blocks another agent
Claiming SHALL be refused when another agent holds an unexpired claim on the card.
The refusal SHALL name the holder, the expiry time, and the time remaining.

#### Scenario: Second agent is refused
- **WHEN** a second agent tries to claim a card claimed by another agent that has not expired
- **THEN** the attempt is refused with the holder's name and the remaining time

#### Scenario: The holder re-claims its own card
- **WHEN** the holder claims a card it already holds, with the same or no paths
- **THEN** the result is success, nothing is written, and the output says the card is already theirs

### Requirement: Finished and retired work is not offered
A card in a column marked done, or retired to the archive, SHALL NOT be claimable
by anyone. The refusal SHALL say the work is finished or retired, and SHALL name
the action that genuinely reopens it — moving the card out of the done column, or
unarchiving it in the card panel — rather than only sending the caller elsewhere.

#### Scenario: A finished card is refused
- **WHEN** an agent tries to claim a card sitting in a done column
- **THEN** the attempt is refused with the claim-denied status, the column is named, and no lease is written

#### Scenario: A retired card is refused
- **WHEN** an agent tries to claim a card held in the archive
- **THEN** the attempt is refused, and the card stays in the archive

#### Scenario: The refusal does not pretend the card is someone else's
- **WHEN** either refusal is reported
- **THEN** it names no holder, and points at the command or panel that reopens the work rather than at a re-claim

### Requirement: Expiry is release
A claim whose expiry is in the past SHALL not be a claim. No background process
SHALL be required to free it. A card with an expired claim SHALL appear in the
ready queue, and a subsequent claim by another agent SHALL succeed and record the
name of the agent whose expired claim it replaced.

#### Scenario: A silent agent stops blocking the board
- **WHEN** a claim's expiry passes and a second agent claims the card
- **THEN** the second claim succeeds and the card records that it preempted the first agent

#### Scenario: Liveness is decided at read time
- **WHEN** a board is read after several leases lapsed with no process running
- **THEN** no bytes have changed and the lapsed claims are absent from the ready set's blockers

### Requirement: Lease length is bounded and configurable
A claim SHALL be taken with a lease duration expressible in seconds, minutes, hours
or days, defaulting to thirty minutes, and refused outside a floor of thirty seconds
and a ceiling of twenty-four hours. A board SHALL be able to declare its own default
duration. Renewing SHALL extend expiry by one full duration from now.

#### Scenario: A tiny lease is refused
- **WHEN** a duration below the floor is requested
- **THEN** the request is refused and the permitted range is stated

#### Scenario: A board with long-running work
- **WHEN** a board declares a default duration of four hours
- **THEN** claims on that board expire four hours after they are taken, absent an explicit duration

### Requirement: Renewing is the holder's to do
Renewing SHALL extend only the caller's own claim. Renewing a claim held by another
agent SHALL be refused. Renewing one's own expired claim SHALL re-take the card and
record that it preempted itself.

#### Scenario: A holder extends its lease before a long step
- **WHEN** the holder renews an unexpired claim
- **THEN** expiry moves out by one full duration and the holder is unchanged

#### Scenario: A stranger tries to extend
- **WHEN** an agent renews a claim that is not its own and is unexpired
- **THEN** the renewal is refused

### Requirement: Releasing
Releasing SHALL remove the claim. Releasing another agent's unexpired claim SHALL
require an explicit force flag and SHALL record who released it. Moving a card into
a column marked done SHALL release its claim automatically.

#### Scenario: Done releases
- **WHEN** a card is moved into a column marked as done
- **THEN** its claim is gone without a separate release call

#### Scenario: Taking a card off a live agent
- **WHEN** a release targets another agent's unexpired claim without the force flag
- **THEN** the release is refused, and with the flag it succeeds and names who forced it

### Requirement: Claim traffic is not content
Taking, renewing and releasing a claim SHALL NOT change the card's "updated"
timestamp, and a claim operation that changes nothing SHALL write no bytes.

#### Scenario: A redundant claim writes nothing
- **WHEN** the same agent claims the same card with the same paths twice
- **THEN** the file is byte-identical afterwards and version control reports no change

#### Scenario: A renewal is a one-line diff
- **WHEN** a holder renews its claim
- **THEN** the recorded change to the file is exactly the expiry value

### Requirement: Claiming sets the assignee
Taking a claim SHALL set the card's assignee to the claiming agent; releasing SHALL
clear it.

#### Scenario: Assignment follows the lease
- **WHEN** ada claims then releases a card
- **THEN** the assignee reads ada while held and is empty afterwards
