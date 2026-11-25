# agentboard

**A board in your repo that tells concurrent agents who holds what, so they stop
editing the same file.**

Several agents working one repository collide on the same files, and neither git
nor "I'll be quick" prevents it. agentboard makes the claim a file: each card is
one YAML file that names the agent holding it, until when, and which paths the
work will touch. A claim is refused before the edit, not discovered in a merge
conflict after it.

No database, no account, no build step, and — for the parts agents use — no
server. `.agentboard/` is committed to the repository it coordinates, so a fresh
clone already has the whole picture and `agentboard claim` works offline.
`agentboard serve` is a human dashboard over those same files, not the thing that
owns them.

## Install

```bash
uv tool install "git+https://github.com/Allenfp/agentboard@v0.1.0"
```

[`uv`](https://docs.astral.sh/uv/) installs it as a tool and puts `agentboard` on
your PATH; `pipx install` does the same job. Pin the tag — that is the release you
tested, and `main` is whatever someone pushed today. Upgrading is the same command
with a newer tag.

There is no `curl | bash` installer on purpose: it would have to install `uv` for
you, from a mutable branch, to save you one command. Python 3.11+ and one
dependency (`ruamel.yaml`). No Node, no bundler, and not on PyPI — that name
belongs to somebody else's unrelated tool, and `uv` installs from git just as
well.

## Six commands

```console
$ cd my-repo && git init -q -b main
$ agentboard init --title Web --key WEB
created /home/you/my-repo/.agentboard
  board.yaml, cards/, archive/, .gitignore

  people: allen (you; add the rest in the board's Settings)
next:
  agentboard create "First card" --as allen
  agentboard skill install     # teach agents in this repo the protocol

$ agentboard create "First card"
created WEB-1  First card
  next: agentboard claim WEB-1 --paths '<glob>'

$ agentboard claim WEB-1 --as allen --paths 'src/**'
  as allen
claimed WEB-1 as allen until 2026-11-25T06:26:38
  paths src/**
  next: renew before anything long - agentboard renew WEB-1
```

Take someone else's work and you get exit 3, every reason at once, and a command
to run instead:

```console
$ agentboard claim PROD-11 --as grigory --paths 'src/api/cli.py'
  as grigory
your paths collide: src/api/cli.py (ada claimed src/api/** on PROD-2)
next: agentboard ready --as grigory
```

`done` moves a card to the done column and releases the lease, because finishing
without releasing is how a board fills up with cards nobody can take. Every verb
takes `--json`, works from anywhere inside the repo, and needs no server.

`init` refuses to overwrite an existing store — an overwritten board is
unrecoverable, and "already initialised" costs one confused second. It also lists
whoever it named itself as: a board with an empty `people` list is one nothing can
ever be claimed from.

## Teach the agents

A coordination scheme nobody knows the vocabulary for prevents no collisions at
all, so the skill file is not documentation, it is the interface.

```bash
agentboard skill install
```

Copies the bundled `SKILL.md` into `.agents/skills/agentboard/` in *this* repo and
writes the short `## Coordination` block for `AGENTS.md`, for runtimes that read
that file and not the skill directory. It documents the CLI only — an agent should
not be steered toward a server that need not be running.

## See it working

```bash
cp -r examples/demo/.agentboard ./ && agentboard serve
```

Two boards, fourteen live cards, four live claims and one path overlap, so the
claim chips tick, the `Conflicts` control appears, and `check` has something to
say. There are no screenshots — nothing in this repo generates them, and a picture
of a board that no longer exists is worse than none.

## Where the rest lives

| | |
| --- | --- |
| [docs/cli.md](docs/cli.md) | every verb, identity, leases, `--paths`, dependencies, exit codes |
| [docs/store.md](docs/store.md) | the `.agentboard/` layout, `board.yaml`, a real card, hand-editing, private boards |
| [docs/conflicts.md](docs/conflicts.md) | why overlap can't be prevented, who wins, how to resolve, the two-clone proof |
| [docs/browser.md](docs/browser.md) | `serve`, flags, what coordination looks like, keyboard |
| [docs/CONVENTIONS.md](docs/CONVENTIONS.md) | house style, and the list of things that live in two or three files and must agree |
| [`.agents/skills/agentboard/SKILL.md`](src/agentboard/skills/agentboard/SKILL.md) | what an agent reads |

## The short version of the rules

- **A card is `ready`, `taken`, `blocked`, or `yours`.** Only the first and last
  are safe to start work in.
- **`--paths` is a promise.** Name files or a tree (`src/api/**`). The board is
  only as honest as the agents declaring them.
- **A lease lapses; that is the feature.** An expired claim is simply not a claim.
- **Cross-machine overlap is detected, never prevented.** Two clones can each
  claim `src/api/**` and merge cleanly. `conflicts` names both and picks the
  earlier claim — so `git pull --ff-only` before you claim.
- **Identity is honour-system.** `--as ada` is checked only against `people`.
  There is no auth anywhere, and loopback is the whole security story for `serve`.
  This stops accidents, which is where collisions actually come from.

Not GitHub Issues. If you want issues in GitHub's tracker, use GitHub's tracker;
this is for people whose board should be a file in the repo.

## Development

```bash
git clone https://github.com/Allenfp/agentboard && cd agentboard
uv run pytest -q
uv run python -m agentboard.cli conflicts --dir examples/demo   # --dir follows the verb
```

MIT — see [LICENSE](LICENSE).
