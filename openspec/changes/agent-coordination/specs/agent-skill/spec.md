# Spec Delta

## Purpose

Agents do not read manuals. This capability defines the file an agent actually
loads — its trigger description, its content, how it reaches a consumer repository,
and the guarantee that it cannot quietly fall out of step with the tool it
describes.

## ADDED Requirements

### Requirement: A skill file ships with the tool
The distribution SHALL contain the coordination skill as a file that is installed
with the package, so a machine that installed the tool without a source checkout can
still produce the file. Locating it SHALL be a command.

#### Scenario: Installed without a checkout
- **WHEN** the tool is installed as a tool from a package index or version control and the skill is located
- **THEN** a readable skill file is found inside the installed package

### Requirement: Installing the skill into a repository
The system SHALL install the skill into the current repository at the conventional
agent-skills location for the tool's own skills directory, creating directories as
needed, and SHALL print a short block suitable for the repository's agent
instructions file.

#### Scenario: A downstream repository learns the protocol
- **WHEN** a person runs the install command in a repository that has a board
- **THEN** the skill file exists at the conventional path, the printed block points at it, and the exit status is success

#### Scenario: A locally edited skill is not clobbered
- **WHEN** the install command runs and the target file exists with different contents, without the force flag
- **THEN** the install is refused, the existing bytes are untouched, and the force flag is named

### Requirement: The tool's own skills directory is not duplicated
This repository SHALL NOT keep a hand-maintained second copy of the coordination
skill alongside the one that ships with the package. Downstream repositories obtain
it only through installation.

#### Scenario: One source of truth
- **WHEN** the repository is inspected for copies of the coordination skill
- **THEN** only the copy inside the package is present

### Requirement: The trigger description tells an agent when to use it
The skill's description SHALL be written as when-to-use-me conditions — starting
work in a repository that has a board, being asked what to work on, touching a file
another agent may hold — rather than as a description of what the tool is.

#### Scenario: A reader can self-select
- **WHEN** an agent reads only the skill's name and description
- **THEN** it can decide that the current task requires this skill without reading the body

### Requirement: The body is the loop and the rules
The skill body SHALL be short and procedural, teaching the working loop with exact
commands rather than describing features.

#### Scenario: The loop is executable as written
- **WHEN** an agent follows the body from the top
- **THEN** it finds ready work, claims it with declared paths, works, renews before a long step, and finishes

#### Scenario: The denial rule is explicit
- **WHEN** an agent that has read only the skill receives the claim-denied status
- **THEN** the skill tells it to choose other work and explicitly rules out retrying and forcing

#### Scenario: Losing is covered
- **WHEN** a conflict is resolved against an agent
- **THEN** the skill states what that agent does next

#### Scenario: Lease choice is covered
- **WHEN** an agent decides how long to hold a card
- **THEN** the skill gives a default and states the duty to renew

#### Scenario: The prohibitions are stated
- **WHEN** an agent reads the rules
- **THEN** they forbid editing inside another agent's claimed paths, committing another agent's staged files, and claiming without synchronising first

### Requirement: The skill documents the command line only
The skill SHALL NOT direct an agent to the HTTP interface or require a running
server; any mention of the dashboard SHALL be limited to noting a human may be
watching.

#### Scenario: No server assumption
- **WHEN** an agent follows every instruction in the skill in a clone with no server
- **THEN** no step fails for want of a server

### Requirement: The skill cannot drift silently
The set of commands named in the skill SHALL be checked against the real command
registry, the exit codes it quotes against the help text, and the packaged file's
resolvability through the installed package. Adding a command without updating the
skill SHALL fail the test suite.

#### Scenario: A new command without a skill update
- **WHEN** a command is added to the registry and the skill is not updated
- **THEN** the suite fails naming the mismatch
