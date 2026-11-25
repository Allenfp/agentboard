# Upgrades

The gap between "works, tested, documented" and "someone else's project depends
on this". Written 2026-11-25 against `0.1.0` at the rename to `agentboard`.

Each item says what is true *today*, not what a checklist wants. Where a number
is quoted it was measured, not guessed: 437 tests passing, 1 skipped (the
Node-gated client harness), 10.7k lines of `src` against 9.0k lines of tests,
one dependency (`ruamel.yaml`). The counts are post-rename and
post-`migrate`-deletion; re-measure them if this document ever disagrees with
`wc -l`.

Done so far: **1** (name), **2** (release automation), **3** (the CI gate).
Items 4–10 are open, in the order I would take them.

## Decisions already taken

- **The name is `agentboard`, everywhere**: module, CLI, `$AGENTBOARD_AGENT`,
  `.agentboard/`, the GitHub repo. No `yamlboard` alias, no compat shim, no
  migration reader — the project has no users, so there is nothing to be kind
  to. A second spelling that survives a rename is a second spelling forever.
- **No PyPI.** `uv tool install git+https://github.com/Allenfp/agentboard@vX.Y.Z`
  is the install path, pinned to a tag. This is a real choice, not a deferral:
  `yamlboard` and `agentboard` are *both* taken on the index by unrelated
  packages (`yamlboard` 1.1.0, uploaded 2026-10-21, a SQL/Streamlit reporting
  tool; `agentboard` 0.0.2, uploaded 2024-11-21, an agent-visualisation
  toolkit), and `uv` installs from git as well as it installs from the index.
  What giving up the index costs, and how to buy it back, is item 10.

---

## 1. Resolve the distribution name, then publish · done

`yamlboard` was squatted on PyPI: `pip install yamlboard` returned somebody
else's SQL reporting tool, at version `1.1.0`, which beats `0.1.0` in any
resolver. The only honest fix was a name nobody wanted, so the project is
`agentboard` and installs from git.

`install.sh` is gone rather than rewritten. A `curl | bash` that installs a
package manager as a side effect, from a mutable `main`, is the shape
[OpenSSF's developer guide](https://best.openssf.org/Concise-Guide-for-Developing-More-Secure-Software.html)
tells people not to trust, and it existed only because the index was closed.
With a git tag there is one command, it is pinned, and it reads
`uv tool install git+…@v0.1.0`.

## 2. Tag a release, and automate the tagging · done

There were no tags at all: `git tag` was empty, so `uv tool install
git+https://…` could only ever mean "whatever `main` is today", which is not a
version, and `install.sh` was honest about it and useless because of it.

Now: the version lives in exactly one place (`__init__.py`), `pyproject.toml`
reads it as a dynamic attribute, and a `v*` tag builds a wheel and an sdist,
attests build provenance, and publishes a GitHub Release with the artifacts
attached. `release.yml` does not publish to PyPI, per the decision above; that
step is a one-line addition if item 10 changes the answer.

## 3. Make CI a gate instead of a poll · done

The old `ci.yml` was one job, `ubuntu-latest`, one interpreter, `ruff check` and
`pytest`. Things it could not catch: a broken install (nothing built the
package), a Windows regression (`store.py`'s `fcntl` fallback is a silent no-op,
see item 4), a type error in code that is annotated end to end, and a dependency
that stops resolving.

Now, in `ci.yml`:

- **`static`** — `ruff check`, `pyright`, `pip-audit`. The lint rule set grew
  `E5`, `W291/2/3`, `I001/I002`, `UP037`: those are P16 and P6 written down, and
  the codebase was already clean under them apart from seven long lines and
  seven autofixable imports, so the gate ships at zero findings. `ruff format`
  was *not* adopted — it rewrote 1351 lines and flattened the aligned
  continuations P22 asks for. Codifying the two rules the house style actually
  states is the smaller, truer change.
- **`test`** — `ubuntu-latest` and `macos-latest` × Python 3.11/3.12/3.13, with
  the interpreter version asserted per cell so a 3.12-only stdlib call cannot
  hide behind whatever the runner had. Coverage runs on one cell against the
  `fail_under` floor in `pyproject.toml`, set to 80 because branch coverage
  measures 81 today (`api.py` 70%, `server.py` 95%); the floor is a drop-alarm,
  not an aspiration, and the number moves up only.
- **`package`** — builds the wheel, installs it with `uv tool install`, and runs
  the README's transcript against the installed executable, asserting the
  exit-3-on-collision contract and that `skill install` lands a real
  `SKILL.md`. This is the job that catches a `[tool.setuptools.package-data]`
  glob that stopped matching `skills/*/*`, which no in-repo test can see.
- **`dependencies`** — PR-only dependency review.
- Every action is pinned to a peeled commit SHA with its release in a comment;
  dependabot bumps both the pins and `uv.lock`.

## 4. Decide Windows explicitly · open

`store.py` guards the lock with `try: import fcntl / except ImportError:
fcntl = None`, and `locked()` returns a no-op when `fcntl is None` — marked
`# pragma: no cover - Windows`. So on Windows every atomic-write guarantee in
the module banner is switched off **in silence**, and CI has never run there.
An agent on a Windows box believes it holds a card while another agent writes
the same file.

Two honest options, both smaller than the bug:

- **Declare POSIX-only.** Refuse to start on `os.name == "nt"` with a message
  that says why, add the right classifiers, and say so in the README. Costs
  nothing; the failure moves from silent-wrong to loud-refused.
- **Support it.** `msvcrt.locking` for the advisory lock, a `windows-latest` CI
  lane (now cheap — item 3 built the matrix), and a test that two processes in
  one repo really do serialise.

Pick one. `pragma: no cover` on the platform boundary is the one comment that
should not be permanent.

## 5. Write the trust documents · open

`SECURITY.md`, `CONTRIBUTING.md`, a code of conduct, issue/PR templates, branch
protection with required checks. These are checkboxes on the
[OpenSSF Baseline](https://baseline.openssf.org/) and the
[best-practices badge](https://github.com/ossf/best-practices-badge), and the
first place a reviewer looks before trusting a repo with a token on it.

`SECURITY.md` is nearly already written — it is transcribed from comments in
this codebase:

- Identity is honour-system. `--as ada` is checked only against `people`; there
  is no authentication anywhere.
- `serve` binds loopback and that *is* the security boundary
  (`is_loopback` is the single definition of it, deliberately).
  Non-loopback needs `--read-only` or `--allow-public`, and neither adds auth.
- `gitops.py` is "a convention, not a sandbox". Every git call passes
  `-c core.fsmonitor=false` because `git status` executes it on every poll, and
  ambient `GIT_DIR`/`GIT_INDEX_FILE` are dropped — but repo hooks still run on
  Commit, on purpose. **Pointing yamlboard at a directory you did not write is
  unsupported, full stop**, and that sentence is the security policy.
- The install path clones and executes code from a git tag.

A threat model that says "this stops accidents, not attackers, and here is the
line" is worth more than a form letter.

## 6. Harden `serve` beyond one tab · open

`ThreadingHTTPServer` is unbounded and every poll re-parses every card. The
banner says the cost out loud — 200 cards × every 3s × per open tab — and
defends the *cache refusal*, which is right: a lease expires on the clock, not
on a write, so a cached document keeps calling a free card "taken". But the
refusal does not have to mean no caching at all.

- Fingerprint the store (directory mtime + the card files' inode/mtime set) and
  answer `304 Not Modified`; keep correctness by folding wall-clock lease
  expiry into the fingerprint, so a lease crossing `expires` invalidates without
  any write. That preserves the no-lying property and deletes most of the cost.
- `ETag` + `Cache-Control` on the static assets (`app.js` is 2.9k lines and is
  currently re-fetched; the CSP is only `frame-ancestors 'none'`).
- `/healthz`, an `--access-log` that names the op and its milliseconds, a
  request timeout (`alarm()` — a stalled body holds a thread forever today),
  and graceful `SIGTERM` so a systemd/`launchd` stop does not land mid-`replace`.

## 7. Prove durability and scale instead of asserting them · open

The banner claims "a crash cannot truncate a card", and `os.replace` gets you
most of the way. It does not get you across a power loss: the new file's data
has to be `fsync`ed *and* the containing directory `fsync`ed, or the rename can
survive with a zero-length body on ext4. Either do that on the write path or
downgrade the sentence to what is actually guaranteed (a crash cannot truncate
a card *while the OS is running*).

Then: a benchmark job at 1k cards / 200 live claims with thresholds on
`list --json`, `conflicts`, and poll cost, so "the poll is expensive" is a
number and a regression, not a paragraph. A `reclaim` verb for `.tmp-*` left by
a killed save. Explicit clock-skew behaviour: a lease is an absolute timestamp
in a *shared, committed* file, so a laptop 20 minutes behind takes work nobody
thinks is free — worth one test and one doc line. Path-traversal tests for
every path derived from a card id or a filename, since `id:` and the filename
are both attacker-visible in a repo you might clone.

## 8. Run the thing in its own repo · open

This repo had no `.agentboard/` of its own — the coordination tool was not
coordinating the project that builds it, and `~/Repos/agentboard-data` (a
separate private board repo) was the only real board in existence, with one
card in it. `init` here, `skill install` here, and make CI fail if the repo's
own `check` is red: dogfooding is the only test that exercises a real merge
conflict between two humans, which is the failure mode the whole design is
built around.

The demo fixture has the same problem in miniature: its claims are hand-stamped
at commit time and the longest TTL is 24h, so the README's "See it working"
increases the chance of showing an *idle* board with no conflict control — the
one screen that sells it. A `--demo` flag on `init` that stamps fresh claims at
run time fixes that permanently, deletes the comment explaining why stale
timestamps are acceptable, and keeps the deliberate no-screenshots rule intact.
If a picture is ever wanted, generate an asciinema cast from `examples/demo` in
CI rather than committing a PNG that rots.

## 9. Close the gap the competitors leave open · open

The strongest argument for this project is external:
[Claude Code's own worktree docs](https://code.claude.com/docs/en/worktrees)
isolate each agent's files, and
[gitta's counter-argument](https://gitta.io/guides/worktrees-and-agent-conflicts)
is that a worktree decides nothing about *who owns a path* — two worktrees both
happily edit `api.py` and the collision arrives at merge.
[NP-Bench](https://arxiv.org/html/2610.07261) exists because nobody verifies
coordination between parallel coding agents. Put that comparison in the README:
the pitch is not "a kanban in YAML", it is "the ownership decision worktrees
skip".

Then make cross-machine detection *actionable*, because detection nobody runs
prevents nothing — `conflicts` already exits 2 specifically so CI hears about
it:

- A composite GitHub Action (`.github/actions/agentboard-check`) that fails a PR
  when two merged claims overlap, so the collision shows up on the PR instead of
  in someone's merge.
- `agentboard hook install` writing a `pre-push` hook that refuses to push a
  claim colliding with a claim already on the remote.
- Record `branch` (and worktree) on the claim, so two clones of one agent name
  are distinguishable instead of reading as one holder with amnesia.

## 10. Publish the contract, then launch · open

Agents are the primary user, so the JSON is API surface and it is currently
undocumented: `reason: widens` and `reason: overridden` are load-bearing
vocabulary that an agent branches on, and nothing states that they are stable.
Ship a versioned JSON Schema for `--json` output, and state the 0.x→1.0
compatibility policy for the board file format and `model.VERSION` — the refusal
logic (a lower version is refused with a pointer, never silently upgraded) is
already the right behaviour and just needs to be a published rule.

Build `docs/` into a site (mkdocs, same Markdown, no rewrite). Then launch where
the audience is: Show HN, `r/ClaudeAI`, `r/cursor`, the Claude/Cursor skill
listings, `awesome-ai-agents` — with the ten-second
`cp -r examples/demo/.agentboard ./ && agentboard serve` demo as the entry
point, and issue templates shaped as integration requests (Claude Code hooks,
Codex, Cursor rules).

If the launch works, buy back the index name: `uv publish` needs an account and
a name, and a [PEP 541](https://peps.python.org/pep-0541) request against the
idle `agentboard` (last upload 2024-11-21, owner's repo last pushed 2024-12-25)
meets the abandonment bar. Ship `agentboard-py` in the meantime if you want
`pip install` to work at all for people who have no `uv`.
