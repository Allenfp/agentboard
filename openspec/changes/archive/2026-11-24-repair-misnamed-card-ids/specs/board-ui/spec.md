# Spec Delta

## Purpose

The board is where a human lands after the CLI tells them something is wrong. A
report they can read but not act on sends them back to a terminal to type the
command the button would have run.

## MODIFIED Requirements

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

## ADDED Requirements

### Requirement: A fixable report carries its own action
A conflict report the tool can settle by itself — a card whose id disagrees with its
filename — SHALL carry that action in the conflict view rather than advice alone. The
control's label SHALL name the action the tool actually performs, so it never promises
a file move that does not happen.

#### Scenario: A naming fault is fixable from the view
- **WHEN** the conflict view lists a card whose id disagrees with its filename
- **THEN** the row names the file and the id it takes, and its control applies the repair without moving the file

#### Scenario: An unfixable report offers nothing
- **WHEN** the conflict view lists two files that cannot both keep one name
- **THEN** the row names both files and has no action that would discard either
