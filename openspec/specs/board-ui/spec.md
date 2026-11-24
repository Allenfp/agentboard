# board-ui Specification

## Purpose

What the browser must show now that other writers exist, and what it must still
refuse to do. The board stays the classic board: coordination arrives as state on
cards, not as a new screen.

## Requirements

### Requirement: A live claim is visible on the card
A card held by a live claim SHALL show the holder and the time remaining, in a form
that survives the most compact card density. Hovering or focusing the indicator
SHALL spell out holder, expiry, and the claimed paths. A holder absent from the
board's people list SHALL render as unknown rather than fail.

#### Scenario: Who has this
- **WHEN** an agent holds a card
- **THEN** the tile shows that agent and a countdown, at every density, and the detail names holder, expiry and paths

#### Scenario: An unfamiliar holder
- **WHEN** a claim names an agent absent from the people list
- **THEN** the indicator renders an unknown marker and the rest of the board is unaffected

### Requirement: The countdown ticks without lying
Remaining time SHALL count down smoothly between refreshes, derived from the
absolute expiry sent by the server and a locally computed clock offset. Updating it
SHALL NOT re-render the board, refetch state, or change any card's reported state.
It SHALL take a warning treatment below a quarter of that lease's own duration, then
read as expired, and SHALL never count into negative time. A hidden tab SHALL NOT
advance it.

#### Scenario: Steady ticking
- **WHEN** a held card stays on screen for several seconds with no refresh
- **THEN** the displayed time decreases each second and no request is made

#### Scenario: Returning to a backgrounded tab
- **WHEN** a tab was hidden while a lease ran down and is shown again
- **THEN** the displayed time matches the true remaining time rather than the value frozen at hide time

#### Scenario: A reduced-motion preference
- **WHEN** the person has asked for reduced motion
- **THEN** the countdown still updates and nothing animates

### Requirement: Expiry is announced once
The dashboard SHALL announce, no more than once per card per claim, the moment it
observes a lease transition into expired while the tab is open. Opening or reloading
a board SHALL produce no backlog of such announcements.

#### Scenario: One announcement per expiry
- **WHEN** a lease expires with the tab open and several refreshes follow
- **THEN** exactly one announcement appears

#### Scenario: Reopening is quiet
- **WHEN** a board already containing expired leases is opened
- **THEN** no expiry announcement is made

### Requirement: Routine coordination does not interrupt
The "changed on disk, reloaded" notice SHALL NOT be raised for a change that only
moved claims, unless the affected card is open in front of the person.

#### Scenario: A busy board stays quiet
- **WHEN** other agents take and renew leases while a person watches an unrelated part of the board
- **THEN** no reload notice is raised

### Requirement: Overlap is unmistakable
A card whose claim collides with another unexpired claim SHALL be visually marked,
and the marking SHALL name both agents and both patterns. Such cards SHALL be
enumerated by one toolbar control carrying the count, opening a view that lists each
pair with its winner and loser and an action that resolves it. With no conflicts the
control SHALL be absent.

#### Scenario: The conflict control appears and disappears
- **WHEN** one overlapping pair exists and is then resolved
- **THEN** the control shows a count of one, and afterwards it is gone rather than disabled

#### Scenario: Both sides are named
- **WHEN** the conflict view is open
- **THEN** each pair names both agents, both patterns, and which one won

### Requirement: Blocked work reads as blocked
A card with unfinished blockers SHALL be visibly dimmed and name its blockers, and a
ready card SHALL read as takeable by comparison.

#### Scenario: Blocked at a glance
- **WHEN** a card is blocked by two unfinished cards
- **THEN** the tile is dimmed and names both blockers

### Requirement: A human can perform the same coordination
The dashboard SHALL let a person take, release and resolve work, choosing the agent
from the board's people list rather than typing a name. Such actions SHALL use the
same server-side write path as every other change and adopt the returned state
rather than a locally guessed one. The identity in use SHALL persist per browser and
SHALL be visible where it can be seen before an action depends on it.

#### Scenario: No invented identities
- **WHEN** a person takes a card from the dashboard
- **THEN** the agent comes from the people list and a name absent from it cannot be submitted

#### Scenario: The identity in use is visible
- **WHEN** the dashboard is open
- **THEN** the identity that keyboard claiming will use is displayed and labelled as such

### Requirement: New visitors learn both jobs
A board with no cards SHALL state, briefly, how to create a card and how to let
agents coordinate against the repository. The footer SHALL keep naming the files
being watched and report how many agents hold work and how many conflicts are open.

#### Scenario: An empty board is not a dead end
- **WHEN** a person opens a freshly initialised board
- **THEN** the empty state names both creating a card and installing the agent skill
