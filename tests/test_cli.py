"""``yamlboard`` as an agent meets it: which exit code, which stream, which identity.

Agents branch on numbers, not prose. The contract pinned here is the one in
``--help``'s epilog — 0 ok, 2 bad argument / refused operation / invalid file /
unknown agent name, 3 *only* "another agent holds it" — plus the stream rules
that make those codes usable: ``--json`` stdout is exactly one parseable JSON
document with nothing mixed in, and the ``as <agent>`` banner lands on stderr
*before* a write, so a stale ``$YAMLBOARD_AGENT`` cannot quietly mislabel a card.

Everything runs :func:`yamlboard.cli.main` in-process against a real git repo in
a temp directory (``GIT_ENV`` from ``test_gitops`` kills ambient gpgsign,
fsmonitor and the developer's own global config), and ``serve`` really binds a
loopback socket. Only three things are stood in for, each because it cannot be
made to happen on demand: ``serve_forever``, which never returns; a
``KeyboardInterrupt`` from inside an op; and the error a closed pipe pushes back.
The :class:`Out` subclass is harness plumbing, not a stub — see its docstring
for why ``cli.Out``'s own default stream cannot be used in-process.
"""

from __future__ import annotations

import contextlib
import datetime
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

from yamlboard import api, cli, model, store

# The fixture's own git calls: a developer machine with commit.gpgSign=true, a
# global core.fsmonitor or a personal user.email must not decide which identity
# a test resolves to, in either direction.
GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(cwd: Path, *args: str) -> str:
    """Run git in ``cwd`` with the ambient config switched off; fail loudly."""
    done = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=True, env={**os.environ, **GIT_ENV})
    return done.stdout.strip()


def said(text: str) -> list[str]:
    """The non-blank lines of a stream."""
    return [line for line in text.splitlines() if line.strip()]


def dirty(cwd: Path) -> list[str]:
    """Every path git considers changed, untracked files included.

    Reads git raw rather than through :func:`git`: porcelain prefixes each line
    with a two-character status and one space, and ``git()``'s ``strip()`` eats
    that space, shifting every path a character — enough to turn
    ``.yamlboard/cards/WEB-1.yaml`` into ``yamlboard/cards/...`` and make a
    correctly quiet tree look like the wrong file was touched.
    """
    done = subprocess.run(["git", "status", "--porcelain=v1", "--untracked-files=all"],
                          cwd=str(cwd), capture_output=True, text=True, check=True,
                          env={**os.environ, **GIT_ENV})
    return sorted(line[3:] for line in done.stdout.splitlines() if line.strip())


class Out(cli.Out):
    """``cli.Out`` with the stream looked up when the run starts.

    ``cli.Out.__init__`` defaults ``stream`` to ``sys.stdout``, and a default
    argument is evaluated once — at import, long before ``redirect_stdout``
    swaps anything. Without this subclass an in-process harness writes the board
    onto the real terminal and captures nothing, which would make every stdout
    assertion below silently vacuous.
    """

    def __init__(self, *, json_mode: bool) -> None:
        super().__init__(json_mode=json_mode, stream=sys.stdout)


class Board(unittest.TestCase):
    """A committed git repo holding one fresh v2 store, two people, no cards.

    Isolation is load-bearing: identity falls through to ``git config
    user.email``, so an inherited ``$YAMLBOARD_AGENT`` or the developer's own
    global config would decide who a claim is signed as. Each test gets an
    environment containing nothing but the git neutering flags, and a repo whose
    only config is what the test itself writes.
    """

    PEOPLE = ("ada", "grigory")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        # A real tree, because `--paths src/api` is only refuseable against one:
        # a wildcard-free path that is not a directory is a file, and a file is a
        # legitimate claim.
        (self.repo / "src" / "api").mkdir(parents=True)
        (self.repo / "src" / "api" / "routes.py").write_text("x\n", encoding="utf-8")
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "guide.md").write_text("x\n", encoding="utf-8")
        # What a consuming repo's ignore file has to say once the board is
        # tracked: the lock and the temps stay out, the cards do not. Without
        # this the lock file shows up as an untracked surprise in the "a claim
        # touched exactly one file" test.
        (self.repo / ".gitignore").write_text(
            ".yamlboard/.lock\n.yamlboard/.tmp-*\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "seed")

        # Agents work inside the checkout they coordinate, and both of the CLI's
        # defaults hang off the process cwd: `Store.discover` searches upwards
        # from it, and so does the `git config user.email` identity read. One
        # directory down, because `gitops.repo_root` starts at the *parent* of
        # the path it is handed and so never recognises the repo root itself.
        self.work = self.repo / "src"

        patcher = mock.patch.dict(os.environ, GIT_ENV, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

        code, out, err = self.yb("init", "--dir", str(self.repo),
                                "--title", "Website", "--key", "WEB")
        assert code == 0, out + err
        self.store = store.Store(self.repo)
        for name in self.PEOPLE:
            api.apply(self.store, {"op": "person.create",
                                   "args": {"name": name, "id": name}})

    # -- harness ---------------------------------------------------------- #

    @contextlib.contextmanager
    def inside(self, path: Path) -> Iterator[Path]:
        """Run with ``path`` as the process cwd, restored afterwards.

        Pinning the cwd is not tidiness: an earlier version of this fixture let
        ``init`` default to "here", and a failing test wrote a live
        ``.yamlboard/`` into the yamlboard checkout itself.
        """
        previous = Path.cwd()
        os.chdir(path)
        try:
            yield path
        finally:
            os.chdir(previous)

    def yb(self, *argv: str, cwd: Path | None = None) -> tuple[int, str, str]:
        """Return (exit code, stdout, stderr) for one ``main()`` call.

        Two kinds of ``SystemExit`` reach here and they are not the same code:
        argparse refuses with the integer 2, while a failed bind raises
        ``SystemExit("message")``, which the interpreter reports as 1 with the
        text on stderr. Both are mapped to what a shell would observe, so a test
        can assert on the number an agent actually branches on.
        """
        out, err = io.StringIO(), io.StringIO()
        raised: list[SystemExit] = []
        with self.inside(cwd or self.work), \
                mock.patch.object(cli, "Out", Out), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main([str(a) for a in argv])
            except SystemExit as exc:
                raised.append(exc)
        if raised:
            payload = raised[0].code
            code = payload if isinstance(payload, int) else cli.EXIT_CANNOT_LISTEN
            if not isinstance(payload, int):
                err.write(f"{payload}\n")
        return code, out.getvalue(), err.getvalue()

    def yj(self, *argv: str) -> tuple[int, dict, str]:
        """Run with ``--json``: (code, the one document on stdout, stderr)."""
        code, out, err = self.yb(*argv, "--json")
        try:
            parsed = json.loads(out)
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"stdout is not one JSON document: {exc}\n{out!r}") from exc
        return code, parsed, err

    def create(self, title: str = "Card") -> str:
        """Add a card with no identity in play and return its id."""
        code, parsed, err = self.yj("create", title)
        assert code == 0, err
        return str(parsed["id"])

    def card(self, card_id: str) -> dict:
        return self.store.read_card(card_id).doc

    def claim(self, card_id: str, agent: str, paths: str | None = None) -> int:
        code, _out, _err = self.yb("claim", card_id, "--as", agent,
                                  *(("--paths", paths) if paths else ()))
        return code

    def plant_claim(self, card_id: str, agent: str, paths: list[str],
                    *, minutes: int = 30) -> None:
        """Write a live claim straight to a card file, the way a merge would.

        Two clones each claim a card of their own and git merges both files with
        no complaint; this reproduces the *result* of that merge so
        :mod:`conflicts` is tested against a state the CLI alone cannot produce.
        """
        tx = api.Tx(self.store)
        card = tx.card(card_id)
        now = model.now()
        model.set_field(card, "card", "claim", {
            "agent": agent,
            "at": now,
            "expires": now + datetime.timedelta(minutes=minutes),
            "paths": paths,
        })
        tx.dirty(card)
        tx.flush()

    def people(self) -> list[str]:
        return [str(p.get("id")) for p in self.store.read_board().doc["people"]]


# --------------------------------------------------------------------------- #
# exit code 0 — the loop an agent actually runs
# --------------------------------------------------------------------------- #

class TestHappyPath(Board):
    def test_the_loop_runs_end_to_end_and_every_step_exits_zero(self) -> None:
        """ready → claim → renew → done, every verb exit 0.

        This is the sequence SKILL.md tells an agent to run. If any step
        misreports, the agent abandons work it was entitled to, or believes a
        write landed when nothing was written.
        """
        card_id = self.create("Ship the endpoint")
        self.assertEqual(self.yb("ready", "--as", "ada")[0], cli.EXIT_OK)

        code, out, err = self.yb("claim", card_id, "--as", "ada",
                                "--paths", "src/api/**")
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIn(f"claimed {card_id} as ada", out)
        claim = self.card(card_id)["claim"]
        self.assertEqual(claim["agent"], "ada")
        self.assertEqual(claim["paths"], ["src/api/**"])
        self.assertEqual(self.card(card_id)["assignee"], "ada",
                         "a claim that does not set the assignee leaves the "
                         "board saying nobody is doing the work")

        code, out, err = self.yb("renew", card_id, "--as", "ada")
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIn("extended to", out)

        code, out, err = self.yb("done", card_id, "--as", "ada")
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIn("done", str(self.card(card_id)["column"]))
        self.assertNotIn("claim", self.card(card_id),
                         "finishing a card must free its lease")

    def test_re_claiming_a_card_i_already_hold_writes_nothing(self) -> None:
        """A retry of a claim I already hold says so and leaves the bytes alone.

        An agent that suspects its lease timed out re-issues the claim. If that
        moved ``expires`` or rewrote the file, every retry would land in a
        human's ``git status``, and a genuinely lost lease would be
        indistinguishable from a redundant one.
        """
        card_id = self.create()
        self.assertEqual(self.claim(card_id, "ada", "src/api/**"), cli.EXIT_OK)
        before = self.store.card_path(card_id).read_bytes()
        expires = self.card(card_id)["claim"]["expires"]

        code, out, err = self.yb("claim", card_id, "--as", "ada",
                                "--paths", "src/api/**")
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIn("already yours", out)
        self.assertIn("nothing written", out)
        self.assertEqual(self.store.card_path(card_id).read_bytes(), before,
                         "a no-op claim rewrote the card file")
        self.assertEqual(self.card(card_id)["claim"]["expires"], expires,
                         "a retry silently extended a lease; that is what "
                         "`renew` is for")

    def test_coordination_traffic_never_moves_updated_but_an_edit_does(self) -> None:
        """``updated:`` means "someone changed this card's content".

        If claiming bumped it, list-view sort order and a human's ``git log``
        would churn on every lease on the board and nobody could tell an edited
        card from a merely busy one. The mirror half matters equally: an edit
        that failed to stamp it hides work that is genuinely new. The clock is
        moved forward between the two so the comparison cannot pass simply
        because both writes landed in the same second.
        """
        one = self.create("Adas card")
        two = self.create("Second card")
        stamped = self.card(one)["updated"]

        real_now = model.now
        model.now = lambda: real_now() + datetime.timedelta(hours=1)
        try:
            self.assertEqual(self.claim(one, "ada", "src/api/**"), cli.EXIT_OK)
            self.assertEqual(self.card(one)["updated"], stamped,
                             "a claim moved the card's `updated:`")
            self.assertEqual(self.yb("dep", "add", two, "--blocked-by", one)[0],
                             cli.EXIT_OK)
            self.assertGreater(self.card(two)["updated"], stamped,
                               "an hour later a real content edit still left the "
                               "card stamped as its creation time")
        finally:
            model.now = real_now

        self.assertEqual(self.claim(one, "ada", "src/api/**"), cli.EXIT_OK)
        self.assertEqual(self.card(one)["updated"], stamped,
                         "a no-op claim moved `updated:` once the clock was back")

    def test_releasing_hands_the_card_back_to_the_board(self) -> None:
        """An agent that cannot finish must free the card, or the work sits locked
        until the lease expires with nobody able to start it."""
        card_id = self.create()
        self.assertEqual(self.claim(card_id, "ada", "src/api/**"), cli.EXIT_OK)
        self.assertNotIn(card_id, self.yj("ready", "--as", "grigory")[1]["ready"])

        code, out, err = self.yb("release", card_id, "--as", "ada")
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIn(f"released {card_id}", out)
        self.assertNotIn("claim", self.card(card_id))
        self.assertNotIn("assignee", self.card(card_id))
        self.assertIn(card_id, self.yj("ready", "--as", "grigory")[1]["ready"])


# --------------------------------------------------------------------------- #
# exit code 3 — and only exit code 3 — "someone else holds it"
# --------------------------------------------------------------------------- #

class TestDenials(Board):
    def test_a_live_claim_by_another_agent_exits_3_and_names_the_holder(self) -> None:
        """Mutual exclusion: the second agent must be able to tell that it lost.

        This is the whole point of the rewrite. If both could take the same card
        they would edit the same files and one set of work would be silently
        thrown away in the merge.
        """
        card_id = self.create("Shared work")
        self.assertEqual(self.claim(card_id, "ada", "src/api/**"), cli.EXIT_OK)

        code, out, err = self.yb("claim", card_id, "--as", "grigory")
        self.assertEqual(code, cli.EXIT_DENIED)
        self.assertEqual(out, "", "a denial must not print as if it succeeded")
        self.assertIn(f"{card_id} is taken", err)
        self.assertIn("ada holds it", err)

    def test_a_denial_ends_with_an_actionable_command_naming_me(self) -> None:
        """The last line of every denial is a command the caller can run now.

        A terse refusal is what makes an agent retry the same claim in a loop or
        hunt for a ``--force`` that deliberately does not exist. The next move
        has to be in the output, carrying the caller's own identity.
        """
        card_id = self.create()
        self.assertEqual(self.claim(card_id, "ada"), cli.EXIT_OK)

        code, out, err = self.yb("claim", card_id, "--as", "grigory")
        self.assertEqual(code, cli.EXIT_DENIED)
        self.assertEqual(said(err)[-1], "next: yamlboard ready --as grigory")

    def test_overlapping_paths_on_a_different_card_are_refused_as_3(self) -> None:
        """Two cards, one set of files: glob intersection blocks the second.

        Card-level exclusion alone would not prevent collisions — the plan's
        verification step is that ``src/api/**`` and ``src/api/routes.py`` on
        *different* cards cannot both be held at once.
        """
        mine = self.create("Adas work")
        other = self.create("Grigorys work")
        self.assertEqual(self.claim(mine, "ada", "src/api/**"), cli.EXIT_OK)

        code, out, err = self.yb("claim", other, "--as", "grigory",
                                "--paths", "src/api/routes.py")
        self.assertEqual(code, cli.EXIT_DENIED)
        self.assertIn("your paths collide", err)
        self.assertIn("ada claimed src/api/**", err)
        self.assertEqual(said(err)[-1], "next: yamlboard ready --as grigory")

        # ... and work nobody has spoken for is still claimable.
        self.assertEqual(self.claim(other, "grigory", "docs/**"), cli.EXIT_OK)

    def test_show_is_a_pre_flight_that_returns_3_before_i_start_work(self) -> None:
        """``show`` answers "can I take this" with the code ``claim`` would give.

        Otherwise an agent has to attempt the claim — touching state, and risking
        a conflict — just to learn the answer.
        """
        card_id = self.create()
        self.assertEqual(self.claim(card_id, "ada"), cli.EXIT_OK)

        code, out, err = self.yb("show", card_id, "--as", "grigory")
        self.assertEqual(code, cli.EXIT_DENIED)
        self.assertIn("taken", out)
        self.assertIn("ada", out)
        self.assertEqual(said(err)[-1], "next: yamlboard ready --as grigory")
        self.assertNotIn("is taken", out, "the denial must live on one stream")

    def test_renewing_someone_elses_lease_is_denied_not_silently_allowed(self) -> None:
        """Extending a lease you do not hold would pin a card forever.

        One agent could keep another's card locked past its own expiry and the
        board would never free it, with no error to explain why.
        """
        card_id = self.create()
        self.assertEqual(self.claim(card_id, "ada", "src/api/**"), cli.EXIT_OK)

        code, out, err = self.yb("renew", card_id, "--as", "grigory")
        self.assertEqual(code, cli.EXIT_DENIED)
        self.assertIn("held by ada", err)
        self.assertEqual(said(err)[-1], "next: yamlboard ready --as grigory")


# --------------------------------------------------------------------------- #
# exit code 2 — everything a retry cannot fix
# --------------------------------------------------------------------------- #

class TestUsage(Board):
    def test_a_store_that_does_not_exist_exits_2_and_points_at_init(self) -> None:
        """A clone with no board must read as "not set up", not as an empty board.

        An agent that cannot find the store would otherwise conclude the board is
        empty and start duplicating work already running somewhere else.
        """
        elsewhere = Path(self._tmp.name) / "bare"
        elsewhere.mkdir()
        code, out, err = self.yb("check", "--dir", str(elsewhere))
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn(".yamlboard", err)
        self.assertIn("yamlboard init", err)

    def test_an_unknown_verb_exits_2_and_lists_the_real_verbs(self) -> None:
        """A hallucinated verb must be a usage error carrying the alternatives.

        An agent handed a bare traceback invents a different verb each time; one
        shown the list self-corrects in a single round trip.
        """
        code, out, err = self.yb("assign", "WEB-1")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("invalid choice", err)
        for verb in ("claim", "ready", "release"):
            self.assertIn(verb, err)

    def test_a_bare_directory_passed_to_paths_is_refused_and_names_the_fix(self) -> None:
        """``--paths src/api`` reads like one filename and means the whole tree.

        Accepted silently, one agent would block the entire source tree on a card
        about one file and the board would stall with no error anywhere to read.
        The refusal must print the pattern that actually works.
        """
        card_id = self.create()
        code, out, err = self.yb("claim", card_id, "--as", "ada",
                                "--paths", "src/api")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertNotIn("claim", self.card(card_id),
                         "a refused claim must not touch the card")
        self.assertIn("'src/api'", err)
        self.assertIn("src/api/**", err, "the refusal does not name the fix")

    def test_a_dependency_cycle_is_refused_and_names_both_ends(self) -> None:
        """A blocked_by cycle deadlocks the board: neither card is ever ready.

        ``ready`` would return nothing and every agent would sit idle while two
        cards wait on each other. Both ids must appear or nobody knows which
        edge to cut.
        """
        one = self.create("One")
        two = self.create("Two")
        self.assertEqual(self.yb("dep", "add", one, "--blocked-by", two)[0],
                         cli.EXIT_OK)

        code, out, err = self.yb("dep", "add", two, "--blocked-by", one)
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("cycle", err)
        self.assertIn(one, err)
        self.assertIn(two, err)
        self.assertNotIn("blocked_by", self.card(two),
                         "the cycle-forming edge must not be persisted")

    def test_an_unknown_blocker_is_refused_rather_than_blocking_forever(self) -> None:
        """A typo'd blocker blocks a card against a card that will never finish.

        Nothing else would notice: the card simply stops appearing in ``ready``,
        which looks exactly like ordinary contention for days.
        """
        card_id = self.create()
        code, out, err = self.yb("dep", "add", card_id, "--blocked-by", "WEB-99")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("WEB-99", err)
        self.assertNotIn("blocked_by", self.card(card_id))

    def test_a_malformed_ttl_is_refused_instead_of_silently_clamped(self) -> None:
        """A lease is a promise to other agents about how long a path is held.

        Substituting the default for a value the caller mistyped breaks that
        promise quietly; and a length outside 30s–24h is a lease that either
        outlives the command guarding it or expires before the agent notices.
        """
        card_id = self.create()
        for bad, fragment in (("5x", "90s"), ("1s", "30s"), ("99h", "24h")):
            code, out, err = self.yb("claim", card_id, "--as", "ada", "--ttl", bad)
            self.assertEqual(code, cli.EXIT_USAGE, f"--ttl {bad} was accepted")
            self.assertIn(bad, err)
            self.assertIn(fragment, err, "the refusal does not state the range")
            self.assertNotIn("claim", self.card(card_id))

    def test_a_legal_short_ttl_is_accepted(self) -> None:
        """90s is above the 30s floor: refusing it would push agents towards
        over-long leases, which is the failure that actually blocks a board."""
        card_id = self.create()
        self.assertEqual(self.claim(card_id, "ada", "src/api/**"), cli.EXIT_OK)
        code, out, err = self.yb("claim", card_id, "--as", "grigory")
        self.assertEqual(code, cli.EXIT_DENIED, "the short lease is not being held")

    def test_a_missing_card_is_reported_as_usage_not_as_a_traceback(self) -> None:
        """Agents guess at ids after a merge; ``WEB-404`` may simply not exist."""
        code, out, err = self.yb("show", "WEB-404")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("WEB-404", err)
        self.assertIn("yamlboard list", err)


# --------------------------------------------------------------------------- #
# identity — who a claim is signed as
# --------------------------------------------------------------------------- #

class TestIdentity(Board):
    def test_the_flag_beats_the_environment_beats_the_git_config(self) -> None:
        """Three sources of identity, one documented order.

        Get this wrong and work is attributed to whoever the shell happens to be
        configured for — a mistake that only surfaces in a blame weeks later, on
        a card nobody recognises. Each pair is tested with *different* names: if
        two sources agree, the test passes whether or not precedence holds.
        """
        git(self.repo, "config", "user.email", "ada@work.example")

        # --as against $YAMLBOARD_AGENT, with the env var naming the *other*
        # person: if both sources agreed the test would pass either way.
        first = self.create()
        os.environ["YAMLBOARD_AGENT"] = "grigory"
        code, out, err = self.yb("claim", first, "--as", "ada")
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertEqual(self.card(first)["assignee"], "ada",
                         "--as lost to $YAMLBOARD_AGENT")
        self.assertIn("as ada", err)

        # git config as the last resort, once the env var is gone.
        del os.environ["YAMLBOARD_AGENT"]
        second = self.create()
        code, out, err = self.yb("claim", second)
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertEqual(self.card(second)["assignee"], "ada",
                         "the local part of git user.email is not the fallback")

    def test_the_environment_variable_beats_the_git_config(self) -> None:
        """``export YAMLBOARD_AGENT`` is how the skill sets identity per shell; a
        machine-level git email must not silently override a deliberate choice."""
        card_id = self.create()
        git(self.repo, "config", "user.email", "ada@work.example")
        os.environ["YAMLBOARD_AGENT"] = "grigory"

        code, out, err = self.yb("claim", card_id)
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertEqual(self.card(card_id)["assignee"], "grigory")

    def test_a_name_the_board_has_never_heard_of_exits_2_not_3(self) -> None:
        """Mistaking your own bad identity for a lost race sends the agent shopping
        for other cards when the fix is to correct the name and rerun.

        Exit 3 means "pick different work". A name the roster has never seen
        means "fix your name", and the hint has to point at the roster or the
        agent cannot find the right one.
        """
        card_id = self.create()
        before = self.people()
        code, out, err = self.yb("claim", card_id, "--as", "ghost")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertNotEqual(code, cli.EXIT_DENIED)
        self.assertIn("'ghost'", err)
        self.assertIn("people", err)
        self.assertIn("yamlboard list", err)
        self.assertEqual(self.people(), before,
                         "an unknown agent was added to the board's people")
        self.assertNotIn("claim", self.card(card_id))

    def test_a_name_that_is_not_a_slug_is_refused_before_the_roster_looks_at_it(self) -> None:
        """``--as 'Ada L.'`` would write an assignee the file grammar and the UI
        chip cannot round-trip; the grammar is checked so the card stays
        readable by every other agent."""
        card_id = self.create()
        code, out, err = self.yb("claim", card_id, "--as", "Ada L.")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("slug", err)
        self.assertEqual(self.people(), list(self.PEOPLE))

    def test_claiming_with_no_identity_anywhere_is_refused_not_invented(self) -> None:
        """No flag, no env, no git email: the claim is refused with exit 2.

        Defaulting to a name like "agent" would put a phantom person on the board
        and make the claim unattributable, which defeats the point of recording
        the claim at all.
        """
        card_id = self.create()
        before = self.people()
        code, out, err = self.yb("claim", card_id)
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("YAMLBOARD_AGENT", err)
        self.assertIn("yamlboard list", err)
        self.assertEqual(self.people(), before)
        self.assertNotIn("claim", self.card(card_id))

    def test_filling_and_wiring_the_board_needs_no_identity_at_all(self) -> None:
        """``create`` and ``dep add`` require nobody: writing the queue is not
        claiming work.

        Demanding identity here breaks the documented bootstrap — a person
        laying out a board has not decided who will work it — and the plan's own
        verification steps run exactly these commands with no identity set.
        """
        self.assertNotIn("YAMLBOARD_AGENT", os.environ)
        code, out, err = self.yb("create", "Backlog item")
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertEqual(out.split()[1], "WEB-1")

        code, out, err = self.yb("create", "Follow-up")
        self.assertEqual(code, cli.EXIT_OK, err)
        code, out, err = self.yb("dep", "add", "WEB-2", "--blocked-by", "WEB-1")
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertEqual(self.card("WEB-2")["blocked_by"], ["WEB-1"])
        self.assertEqual(err, "", "no identity banner may print with no identity")
        self.assertEqual(self.people(), list(self.PEOPLE),
                         "filling the board must not invent a person")

    def test_the_banner_names_the_identity_before_anything_is_written(self) -> None:
        """A stale ``$YAMLBOARD_AGENT`` has to be visible while it is fixable.

        Printed afterwards, the card is already mislabelled and the name is
        already in a commit message — which is the failure mode the banner
        exists to prevent.
        """
        card_id = self.create()
        seen: dict[str, object] = {}
        real_apply = api.apply

        def spy(target, payload, **kwargs):
            seen["stderr"] = sys.stderr.getvalue()
            seen["before"] = "claim" in self.card(card_id)
            result = real_apply(target, payload, **kwargs)
            seen["after"] = "claim" in self.card(card_id)
            return result

        out, err = io.StringIO(), io.StringIO()
        with self.inside(self.work), \
                mock.patch.object(cli, "Out", Out), \
                mock.patch.object(cli.api, "apply", spy), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["claim", card_id, "--as", "ada"])

        self.assertEqual(code, cli.EXIT_OK)
        self.assertFalse(seen["before"], "the card was already claimed")
        self.assertTrue(seen["after"], "the spy never wrapped the real write")
        self.assertIn("as ada", str(seen["stderr"]),
                      "the identity banner was not on stderr before the write")

    def test_json_mode_prints_no_banner_at_all(self) -> None:
        """Under ``--json`` the document is the whole answer; identity chatter on
        either stream is noise a script has to strip."""
        card_id = self.create()
        code, out, err = self.yb("claim", card_id, "--as", "ada", "--json")
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertEqual(json.loads(out)["agent"], "ada")
        self.assertNotIn("as ada", out + err)


# --------------------------------------------------------------------------- #
# stream discipline
# --------------------------------------------------------------------------- #

class TestStreams(Board):
    def test_json_stdout_is_one_document_even_when_the_command_failed(self) -> None:
        """``json.loads(stdout)`` must succeed for a refusal as well as a success.

        A scripted agent reads one stream. Prose mixed into the document makes it
        unparseable; a refusal with no machine-readable form forces the agent to
        regex human prose.
        """
        code, out, err = self.yb("ready", "--as", "nobody", "--json")
        self.assertEqual(code, cli.EXIT_USAGE)
        document = json.loads(out)
        self.assertFalse(document["ok"])
        self.assertIn("people", document["error"])
        self.assertEqual(document["lines"][-1],
                         "next: yamlboard list   # see this board's people")
        self.assertEqual(out.strip().count("{"), 1, "more than one document")
        # Under --json the document is the only output: `main` deliberately skips
        # the prose echo, so nothing on stderr is ever something a parser has to
        # ignore to stay quiet.
        self.assertEqual(err, "")

    def test_a_denial_under_json_is_one_document_naming_the_holder(self) -> None:
        """Exit 3 with ``--json``: still exactly one parseable answer, with the
        reason inside it. Otherwise every script needs a prose parser to tell
        "taken" from "no such card"."""
        card_id = self.create()
        self.assertEqual(self.claim(card_id, "ada"), cli.EXIT_OK)

        code, document, err = self.yj("claim", card_id, "--as", "grigory")
        self.assertEqual(code, cli.EXIT_DENIED)
        self.assertFalse(document["ok"])
        self.assertIn("ada", document["error"])
        self.assertEqual(document["lines"][-1], "next: yamlboard ready --as grigory")
        self.assertEqual(document["holder"], "ada")
        self.assertEqual(err, "", "no prose may accompany the document")

    def test_success_prints_facts_to_stdout_and_only_the_banner_to_stderr(self) -> None:
        """Two channels, one job each: ``yamlboard claim ... | tee log`` should
        capture the outcome, not the identity chatter."""
        card_id = self.create()
        code, out, err = self.yb("claim", card_id, "--as", "ada")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn(f"claimed {card_id} as ada", out)
        self.assertEqual(err.strip(), "as ada")

    def test_an_interrupted_run_exits_130_and_leaves_the_card_unclaimed(self) -> None:
        """Ctrl-C mid-op is the one thing an operator does constantly.

        Exit 130 says "interrupted, nothing happened". Anything else and the
        caller cannot tell whether the claim landed — so the card stays locked
        until the lease expires while its owner waits.
        """
        card_id = self.create()

        def interrupt(*args, **kwargs):
            raise KeyboardInterrupt()

        out, err = io.StringIO(), io.StringIO()
        with self.inside(self.work), \
                mock.patch.object(cli, "Out", Out), \
                mock.patch.object(cli.api, "apply", interrupt), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["claim", card_id, "--as", "ada"])

        self.assertEqual(code, cli.EXIT_INTERRUPT)
        self.assertIn("interrupted", err.getvalue())
        self.assertNotIn("claim", self.card(card_id))

    def test_a_closed_pipe_exits_141_rather_than_printing_a_traceback(self) -> None:
        """``yamlboard list --json | head`` closes the reader early.

        EPIPE surfacing as a traceback reads as a yamlboard bug, so an agent
        files an issue about a command that worked. The code table promises 141
        and a quiet stderr.
        """
        class Closed(io.StringIO):
            def write(self, _text: str) -> int:
                raise BrokenPipeError(32, "Broken pipe")

        class PipedOut(cli.Out):
            def __init__(self, *, json_mode: bool) -> None:
                super().__init__(json_mode=json_mode, stream=Closed())

        self.create()
        err = io.StringIO()
        with self.inside(self.work), \
                mock.patch.object(cli, "Out", PipedOut), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            code = cli.main(["list", "--json"])
        self.assertEqual(code, cli.EXIT_PIPE)
        self.assertNotIn("Traceback", err)

    def test_a_pipe_broken_only_at_flush_does_not_hijack_the_exit_code(self) -> None:
        """The flush lives in a ``finally``, so it must not overwrite the verdict.

        If a late EPIPE replaced the code, ``yamlboard check`` on a genuinely
        invalid board could report 141 — "the reader stopped" — to a CI job that
        was about to ask why the board is broken.
        """
        class BrokenOnFlush(io.StringIO):
            def flush(self) -> None:
                raise BrokenPipeError(32, "Broken pipe")

            def write(self, text: str) -> int:
                return len(text)

        class FlakyOut(cli.Out):
            def __init__(self, *, json_mode: bool) -> None:
                super().__init__(json_mode=json_mode, stream=BrokenOnFlush())

        self.create()
        err = io.StringIO()
        with self.inside(self.work), \
                mock.patch.object(cli, "Out", FlakyOut), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            code = cli.main(["check", "--json"])
        self.assertEqual(code, cli.EXIT_OK,
                         "a broken flush changed the verdict of a valid board")
        self.assertEqual(err.getvalue(), "")


# --------------------------------------------------------------------------- #
# dependency direction, init idempotence, inspection
# --------------------------------------------------------------------------- #

class TestDependencyDirection(Board):
    def test_the_blocker_is_a_flag_because_the_direction_cannot_be_guessed(self) -> None:
        """``dep add WEB-1 WEB-2`` does not say which card waits.

        A guessed direction silently blocks the wrong agent's work, and the
        symptom — a card that never becomes ready — is indistinguishable from
        ordinary contention until somebody's afternoon is gone.
        """
        self.create("First")
        self.create("Second")

        code, out, err = self.yb("dep", "add", "WEB-1", "--blocked-by", "WEB-2")
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertEqual(self.card("WEB-1")["blocked_by"], ["WEB-2"])
        self.assertIsNone(self.card("WEB-2").get("blocked_by"))

        code, out, err = self.yb("dep", "add", "WEB-2", "WEB-1")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("--blocked-by", err)
        self.assertIsNone(self.card("WEB-2").get("blocked_by"),
                          "the positional form must not create an edge")

    def test_removing_an_edge_frees_the_card_back_into_ready(self) -> None:
        """An edge that cannot be removed is permanent: one mis-wired dependency
        would take a card off the board for the rest of the project."""
        self.create("First")
        self.create("Second")
        self.assertEqual(self.yb("dep", "add", "WEB-2", "--blocked-by", "WEB-1")[0],
                         cli.EXIT_OK)
        self.assertNotIn("WEB-2", self.yj("ready", "--as", "ada")[1]["ready"])

        self.assertEqual(self.yb("dep", "rm", "WEB-2", "--blocked-by", "WEB-1")[0],
                         cli.EXIT_OK)
        self.assertIn("WEB-2", self.yj("ready", "--as", "ada")[1]["ready"])

    def test_a_blocked_card_says_which_card_blocks_it(self) -> None:
        """An agent that cannot see *why* a card is missing from ``ready`` will
        attempt the claim and be refused without understanding the refusal, so it
        retries instead of finishing the blocker."""
        self.create("First")
        two = self.create("Second")
        self.assertEqual(self.yb("dep", "add", two, "--blocked-by", "WEB-1")[0],
                         cli.EXIT_OK)

        code, out, err = self.yb("show", two)
        # `show` is a pre-flight: "not takeable" is exit 3, the same answer
        # `claim` would give, so an agent can branch without attempting the write.
        self.assertEqual(code, cli.EXIT_DENIED)
        self.assertIn("blocked", out)
        self.assertIn("WEB-1", out)
        self.assertIn(f"{two} also waits on WEB-1", err)

        code, out, err = self.yb("claim", two, "--as", "ada")
        self.assertEqual(code, cli.EXIT_DENIED)
        self.assertIn(f"{two} also waits on WEB-1", err)


class TestInitIsSafe(Board):
    def test_init_never_overwrites_or_duplicates_the_board_it_found(self) -> None:
        """An over-written board is unrecoverable; "already initialised" costs one
        confused second. The ``--force`` help says it is accepted and ignored.

        ``--dir`` is the point of this test rather than decoration: ``init``
        without it means "here", so in a repo whose store sits higher up it
        would silently create a *second* empty board, and every later verb would
        act on whichever one ``discover`` reached first.
        """
        card_id = self.create("Work nobody has committed yet")
        code, out, err = self.yb("init", "--dir", str(self.repo), "--force")
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIn("already holds a board", out)
        self.assertIn("left alone", out)
        self.assertFalse((self.work / ".yamlboard").exists(),
                         "a second store was created inside the tree")
        self.assertEqual(self.card(card_id)["title"], "Work nobody has committed yet")
        self.assertEqual(self.people(), list(self.PEOPLE))

    def test_init_tells_the_reader_the_two_commands_that_make_it_useful(self) -> None:
        """A fresh store is the moment someone wants the follow-ups; ``--json``
        must stay clean of them or a script has to strip prose."""
        fresh = Path(self._tmp.name) / "fresh"
        fresh.mkdir()
        code, out, err = self.yb("init", "--dir", str(fresh), "--key", "OPS")
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIn("yamlboard create", out)
        self.assertIn("skill install", out)

        other = Path(self._tmp.name) / "jsonstore"
        other.mkdir()
        code, document, err = self.yj("init", "--dir", str(other))
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertTrue(document["ok"])
        self.assertFalse(document["existed"])

    def test_the_store_lands_where_the_contract_says_it_lives(self) -> None:
        """One file per card is the whole design; a store written to the wrong path
        or shape makes every other verb search the wrong directory."""
        board_file = self.repo / ".yamlboard" / "board.yaml"
        self.assertTrue(board_file.is_file())
        self.assertTrue((self.repo / ".yamlboard" / "cards").is_dir())
        self.assertTrue((self.repo / ".yamlboard" / "archive").is_dir())
        self.assertEqual(self.store.read_board().doc["version"], model.VERSION)
        self.assertNotIn("rev:", board_file.read_text(encoding="utf-8"),
                         "a global rev counter is back; it caused every conflict")


class TestInspection(Board):
    def test_check_exits_zero_on_a_valid_store_and_reports_the_counts(self) -> None:
        """CI gates on this exit code. A false 2 makes every push look broken; a
        false 0 waves through a board nothing else can write to."""
        self.create("One")
        self.create("Two")
        code, out, err = self.yb("check")
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIn("ok", out)
        self.assertIn("2 card(s)", out)
        code, document, err = self.yj("check")
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertTrue(document["ok"])
        self.assertEqual(document["cards"], 2)

    def test_check_exits_2_when_a_card_no_longer_validates(self) -> None:
        """One hand-edited card with an unknown key must fail the gate.

        The closed key set is enforced on read, so that card refuses every write
        until it is fixed; a ``check`` that passed here would send an agent off to
        fight an unexplained refusal instead of fixing the file.
        """
        card_id = self.create()
        path = self.store.card_path(card_id)
        doc = store._read_file(path, None).doc
        doc["bogus_key"] = "hand-edited"
        store._write_file(path, doc)

        code, out, err = self.yb("check")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("bogus_key", out)
        _, document, _ = self.yj("check")
        self.assertFalse(document["ok"])
        self.assertTrue(document["problems"])
        self.assertEqual(self.yb("list")[0], cli.EXIT_OK,
                         "the board must still be readable while one card is broken")

    def test_list_ready_and_show_survive_a_board_with_nothing_on_it(self) -> None:
        """A fresh clone is empty. A crash there reads as a broken install, and
        the first thing a new agent does is give up on the tool."""
        self.assertEqual(self.yb("list")[0], cli.EXIT_OK)
        code, out, err = self.yb("ready")
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIn("nothing is ready", out)
        _, document, _ = self.yj("ready")
        self.assertEqual(document["ready"], [])
        self.assertEqual(self.yb("show", "WEB-1")[0], cli.EXIT_USAGE)

    def test_ready_offers_only_work_that_is_there_to_take(self) -> None:
        """``ready`` is the only list agents are told to pick from, so it holds
        exactly the takeable cards: nothing another agent holds, nothing blocked,
        and nothing I already hold — a card I hold is ``yours``, continued with
        ``show``/``renew``, not re-picked. Offer someone else's card here and two
        agents start the same work within one poll interval.
        """
        one = self.create("API work")
        two = self.create("Docs work")
        self.assertEqual(self.claim(one, "ada", "src/api/**"), cli.EXIT_OK)

        _, offered, _ = self.yj("ready", "--as", "grigory")
        self.assertEqual(offered["ready"], [two])

        _, filtered, _ = self.yj("ready", "--as", "grigory", "--paths", "src/api/**")
        self.assertEqual(filtered["ready"], [],
                         "--paths was ignored: a card whose holder already covers "
                         "my pattern is work I cannot start")

        _, mine, _ = self.yj("ready", "--as", "ada")
        self.assertEqual(mine["ready"], [two],
                         "the card I hold must drop out of my own `ready`; the "
                         "unclaimed one stays, because it is genuinely free")
        self.assertNotIn(one, mine["ready"],
                         "re-offering a card I hold invites a duplicate claim "
                         "instead of a renew")
        # The card is mine, and `show` says so in the one wording that matters:
        # "already yours", not "taken by ada" — an agent that reads the latter
        # walks away from work it is entitled to continue.
        code, out, err = self.yb("show", one, "--as", "ada")
        self.assertIn(f"{one} is already yours", err)
        self.assertNotIn("is taken", err)

    def test_list_reports_a_card_i_hold_as_yours_not_taken(self) -> None:
        """``list`` and ``show`` agree on the four-word vocabulary or an agent
        that scanned the list first walks past work it already holds.

        ``summary`` computes ``state_of`` once, with the caller's identity; if
        ``list`` dropped that argument the row would read ``taken`` and the flag
        would change from a tick to a dot — a card I hold rendered as work for
        someone else.
        """
        mine = self.create("Mine")
        theirs = self.create("Theirs")
        self.claim(mine, "ada", "src/api/**")
        self.claim(theirs, "grigory", "docs/**")

        _, document, _ = self.yj("list", "--as", "ada")
        states = {c["id"]: c["state"] for c in document["cards"]}
        self.assertEqual(states[mine], "yours")
        self.assertEqual(states[theirs], "taken")

        # and the human-readable row marks it, not the dot a stranger's card gets
        _, listing, _ = self.yb("list", "--as", "ada")
        mine_row = next(line for line in listing.splitlines() if mine in line)
        theirs_row = next(line for line in listing.splitlines() if theirs in line)
        self.assertIn("✓", mine_row)
        self.assertNotIn("✓", theirs_row)


# --------------------------------------------------------------------------- #
# conflicts — the state git cannot arbitrate
# --------------------------------------------------------------------------- #

class TestConflicts(Board):
    def setUp(self) -> None:
        super().setUp()
        self.create("Adas card")
        self.create("Grigorys card")
        self.assertEqual(self.claim("WEB-1", "ada", "src/api/**"), cli.EXIT_OK)
        self.plant_claim("WEB-2", "grigory", ["src/api/routes.py"])

    def test_reporting_a_conflict_writes_nothing(self) -> None:
        """Detection runs from ``check``, which runs in CI, which runs on someone
        else's clone. If reading the board edited the loser, two clones would
        disagree about who holds what — and an agent would keep working under a
        claim that had been deleted underneath it."""
        before = {path.name: path.read_bytes()
                  for path in sorted(self.store.cards_dir.glob("*.yaml"))}

        code, out, err = self.yb("conflicts")
        self.assertEqual(code, cli.EXIT_USAGE, "an unresolved overlap must fail a gate")
        self.assertIn("WEB-1", out)
        self.assertIn("WEB-2", out)
        self.assertIn("ada", out)
        self.assertIn("grigory", out)
        # The two globs, whole, on one line. `patterns` entries are strings, and
        # a join over one turns `src/api/**` into `s/r/c///a/p/i///*/*` — the
        # README quotes this transcript, so a mangled render makes the docs wrong
        # as well as the terminal unreadable.
        self.assertIn("src/api/** ∩ src/api/routes.py", out)
        self.assertEqual({path.name: path.read_bytes()
                          for path in sorted(self.store.cards_dir.glob("*.yaml"))},
                         before, "reporting a conflict rewrote a card")

    def test_resolve_marks_the_loser_and_puts_their_card_back_in_ready(self) -> None:
        """An unresolved overlap means two agents keep editing the same files. The
        loser must end up free, and told who beat them, or the collision silently
        continues after a human has said it is settled."""
        code, out, err = self.yb("conflicts", "--resolve", "--as", "ada")
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIn("grigory overridden by ada", out)
        self.assertEqual(self.card("WEB-2")["claim"]["overridden_by"], "ada")
        self.assertIn("WEB-2", self.yj("ready", "--as", "grigory")[1]["ready"])
        self.assertEqual(self.yb("conflicts")[0], cli.EXIT_OK,
                         "the overlap is still reported after being resolved")

    def test_resolving_with_nothing_to_resolve_is_a_refusal_not_a_silence(self) -> None:
        """``--resolve`` is the one command that rewrites another agent's claim.
        Reporting success when there was nothing to settle teaches callers to
        distrust it, and an agent would conclude a collision had been
        adjudicated when no collision existed.

        A second, quiet store rather than this fixture's: ``setUp`` above
        already planted an overlap, so this class can only ever report one.
        """
        quiet = Path(self._tmp.name) / "quiet"
        quiet.mkdir()
        self.assertEqual(self.yb("init", "--dir", str(quiet), "--key", "OPS")[0],
                         cli.EXIT_OK)
        self.assertEqual(self.yb("conflicts", "--dir", str(quiet))[0], cli.EXIT_OK)

        code, out, err = self.yb("conflicts", "--dir", str(quiet), "--resolve",
                                "--as", "ada")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("no conflicts", err)

    def test_check_fails_the_gate_on_a_conflict_git_never_saw(self) -> None:
        """Two clean merges, one collision: only the board knows. A gate that
        passed here would let both agents push edits to the same files."""
        code, out, err = self.yb("check")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("conflict", out)
        _, document, _ = self.yj("check")
        self.assertEqual(document["conflicts"][0]["winner"]["agent"], "ada")


# --------------------------------------------------------------------------- #
# serve: only that a busy port and a typo are different codes
# --------------------------------------------------------------------------- #

class TestServeExitCodes(Board):
    """The two ways ``serve`` can fail have to be distinguishable by code.

    The server is real: :func:`server.make_server` binds a real loopback socket,
    because a bind can be made to fail on command by holding the port. Only
    ``serve_forever`` is stood in for — it never returns, and that is the whole
    point of the command.
    """

    def stub_loop(self) -> None:
        """Stop ``serve`` from blocking forever, leaving the bind real."""
        patcher = mock.patch.object(cli.server.Server, "serve_forever",
                                   lambda self: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def free_port(self) -> int:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        return port

    def test_a_port_that_will_not_open_is_not_reported_as_a_bad_argument(self) -> None:
        """Nothing the caller typed is wrong — the machine will not hand over the
        socket. An agent handed exit 2 here re-checks its arguments forever
        instead of picking another port, and a script that reads 2 as "the board
        is broken" would stop on an otherwise healthy laptop.
        """
        taken = socket.socket()
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        self.addCleanup(taken.close)

        self.stub_loop()
        code, out, err = self.yb("serve", "--host", "127.0.0.1",
                                "--port", str(taken.getsockname()[1]))
        self.assertEqual(code, cli.EXIT_CANNOT_LISTEN)
        self.assertIn("cannot listen", err)
        self.assertNotIn("usage", err.lower())

    def test_a_typo_in_the_port_is_a_usage_error(self) -> None:
        """The other half of the same distinction: this one *is* the caller's
        mistake, so it must not arrive as a listen failure."""
        code, out, err = self.yb("serve", "--port", "seventy")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("--port", err)

    def test_a_board_that_is_not_there_is_refused_before_the_port_opens(self) -> None:
        """A dashboard over a board that does not exist would open, look empty,
        and invite a human to type cards that go nowhere.

        The port is held on purpose, and the code is the proof of ordering: if
        ``serve`` bound before resolving the store, this would come back as
        "cannot listen" (1). Coming back as 2 with an ``init`` hint means the
        board was checked first, which is the order that saves a human from
        staring at a working server over nothing.
        """
        self.stub_loop()
        taken = socket.socket()
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        self.addCleanup(taken.close)

        elsewhere = Path(self._tmp.name) / "boardless"
        elsewhere.mkdir()
        code, out, err = self.yb("serve", "--dir", str(elsewhere),
                                "--port", str(taken.getsockname()[1]))
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("yamlboard init", err)
        self.assertNotIn("cannot listen", err)

    def test_a_served_board_announces_the_file_it_is_writing(self) -> None:
        """The board is the arbiter, so the human has to know which checkout the
        browser is looking at — two clones on one laptop is the normal case here."""
        self.stub_loop()
        code, out, err = self.yb("serve", "--port", str(self.free_port()))
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIn(str(self.store.root), out)


# --------------------------------------------------------------------------- #
# the skill, which is the actual product surface
# --------------------------------------------------------------------------- #

class TestSkillInstall(Board):
    """``skill install`` is how a downstream repo learns the protocol at all.

    An agent that does not know the vocabulary collides anyway, so the install
    path is not documentation plumbing — it is the coordination. The byte-exact
    and refusal rules below are what keep the installed copy honest about which
    build wrote it.
    """

    def dest(self, root: Path) -> Path:
        return root / ".agents" / "skills" / "yamlboard" / "SKILL.md"

    def test_install_writes_the_bundled_bytes_and_shows_the_agents_block(self) -> None:
        """A repo that ran ``skill install`` must hold exactly the shipped file.

        Any drift — a truncated copy, a stale wheel — means agents in that repo
        follow a protocol the installed CLI no longer implements, which is worse
        than no skill because it looks authoritative.
        """
        bundled = cli.skill_path()
        self.assertIsNotNone(bundled, "this install ships no SKILL.md at all")

        code, out, err = self.yb("skill", "install", "--dir", str(self.repo))
        self.assertEqual(code, cli.EXIT_OK, err)
        written = self.dest(self.repo)
        self.assertTrue(written.is_file())
        self.assertEqual(written.read_bytes(), bundled.read_bytes())
        # The AGENTS.md block: runtimes that read that file and never look in
        # .agents/skills/ still have to find the protocol.
        self.assertIn("paste this into AGENTS.md", out)
        self.assertIn("yamlboard claim", out)

    def test_an_edited_copy_survives_a_plain_reinstall(self) -> None:
        """A local edit to the skill is someone's deliberate decision.

        Silently overwriting it would delete a team's customised rules on the
        next install and leave the change unrecoverable — an install is not
        something anyone expects to be destructive.
        """
        self.assertEqual(self.yb("skill", "install", "--dir", str(self.repo))[0],
                         cli.EXIT_OK)
        target = self.dest(self.repo)
        edited = target.read_bytes() + b"\n# local rule: renew every 10m\n"
        target.write_bytes(edited)

        code, out, err = self.yb("skill", "install", "--dir", str(self.repo))
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("--force", err, "the refusal does not name the way out")
        self.assertEqual(target.read_bytes(), edited,
                         "a refused install overwrote a local edit")

        code, out, err = self.yb("skill", "install", "--dir", str(self.repo),
                                "--force")
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertEqual(target.read_bytes(), cli.skill_path().read_bytes(),
                         "--force did not restore the bundled copy")

    def test_reinstalling_an_unchanged_copy_is_a_quiet_success(self) -> None:
        """Idempotence: CI may run this on every clone. A second install that
        rewrote bytes would dirty the tree and fail the very gate that ran it."""
        self.assertEqual(self.yb("skill", "install", "--dir", str(self.repo))[0],
                         cli.EXIT_OK)
        before = dirty(self.repo)
        code, out, err = self.yb("skill", "install", "--dir", str(self.repo))
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIn("already current", out)
        self.assertEqual(dirty(self.repo), before,
                         "a no-op install changed the working tree")

    def test_path_names_a_real_file_inside_the_installed_package(self) -> None:
        """``skill path`` is how a human verifies which copy a repo will get.

        It resolves through the package, never the working directory: after
        ``uv tool install`` there is no checkout, and a cwd-relative answer
        would point at whatever repo the caller happened to stand in.
        """
        elsewhere = Path(self._tmp.name) / "elsewhere"
        elsewhere.mkdir()
        code, out, err = self.yb("skill", "path", cwd=elsewhere)
        self.assertEqual(code, cli.EXIT_OK, err)
        reported = Path(out.strip())
        self.assertTrue(reported.is_file())
        package = Path(cli.__file__).resolve().parent
        self.assertEqual(reported.resolve().parent,
                         (package / "skills" / "yamlboard").resolve())


# --------------------------------------------------------------------------- #
# the shipped entry point, as a real process
# --------------------------------------------------------------------------- #

class TestMigrateFidelity(Board):
    """``migrate`` is the only bridge from v1, and there is no reader behind it.

    A silent loss here is unrecoverable in the worst way: the v1 file still looks
    fine on disk, so nobody notices the card that did not arrive until someone
    looks for it months later. So every field the old shape carried is asserted on
    the way out, and the source file is asserted untouched.
    """

    V1 = """\
version: 1
title: Product
people:
  - id: ada
    name: Ada
    color: "#7c8797"
  - id: grigory
    name: Grigory
labels:
  - id: bug
    name: Bug
boards:
  - id: product
    key: PROD
    title: Product
    next_card: 7
    columns:
      - id: backlog
        name: Backlog
      - id: doing
        name: Doing
        wip_limit: 2
      - id: done
        name: Done
        is_done: true
    custom_fields:
      - id: customer
        name: Customer
        type: select
        options: [Acme, Globex]
    cards:
      - id: PROD-1
        title: Ship the API
        desc: |
          line one
          line two
        column: doing
        labels: [bug]
        priority: urgent
        assignee: ada
        due: 2026-12-01
        estimate: 3
        fields:
          customer: Acme
      - id: PROD-6
        title: Already shipped
        column: done
        archived: true
"""

    def setUp(self) -> None:
        super().setUp()
        self.source = Path(self._tmp.name) / "old.yaml"
        self.source.write_text(self.V1, encoding="utf-8")
        self.original = self.source.read_bytes()
        self.target = Path(self._tmp.name) / "fresh"
        self.target.mkdir()

    def test_every_field_survives_and_the_source_is_left_alone(self) -> None:
        code, out, err = self.yb("migrate", str(self.source), "--dir", str(self.target))
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIn("2 card(s)", out)
        self.assertEqual(self.source.read_bytes(), self.original,
                         "migrate rewrote the file it was only supposed to read")

        arrived = store.Store(self.target)
        self.assertEqual(model.validate(arrived.read_board().doc), [])
        board = arrived.read_board().doc["boards"][0]
        self.assertEqual(board["key"], "PROD")
        # the counter must clear the highest migrated id, or the next create
        # re-issues PROD-6 and a merge loses one of them
        self.assertGreaterEqual(board["next_card"], 7)
        self.assertEqual([c["id"] for c in model.columns(board)],
                         ["backlog", "doing", "done"])
        self.assertTrue(model.is_done_column(board, "done"))
        self.assertEqual(model.find_column(board, "doing")["wip_limit"], 2)
        self.assertEqual([f["id"] for f in board["custom_fields"]], ["customer"])
        self.assertEqual([p["id"] for p in arrived.read_board().doc["people"]],
                         ["ada", "grigory"])
        self.assertEqual([lbl["id"] for lbl in arrived.read_board().doc["labels"]],
                         ["bug"])

        one = arrived.read_card("PROD-1").doc
        self.assertEqual(one["title"], "Ship the API")
        self.assertEqual(one["column"], "doing")
        self.assertEqual(one["priority"], "urgent")
        self.assertEqual(one["assignee"], "ada")
        self.assertEqual([str(x) for x in one["labels"]], ["bug"])
        self.assertEqual(one["estimate"], 3)
        self.assertEqual(one["due"], datetime.date(2026, 12, 1))
        self.assertEqual(one["desc"], "line one\nline two\n")
        self.assertEqual(dict(one["fields"]), {"customer": "Acme"})

        # an archived card lives under archive/: the directory is part of the
        # meaning, and a card in cards/ saying archived: true renders as live
        self.assertTrue(arrived.card_path("PROD-6", archived=True).is_file())
        self.assertFalse(arrived.card_path("PROD-6").exists())
        self.assertTrue(arrived.read_card("PROD-6").doc["archived"])

    def test_a_second_board_in_the_one_file_is_refused_not_silently_dropped(self) -> None:
        """Two boards cannot share one store without renaming every card id.

        Importing the first and ignoring the second would report success while
        quietly discarding a whole board's cards.
        """
        self.source.write_text(
            self.source.read_text(encoding="utf-8")
            + "  - id: personal\n    key: PER\n    title: Personal\n"
              "    columns:\n      - id: todo\n        name: To do\n    cards: []\n",
            encoding="utf-8")
        code, _, err = self.yb("migrate", str(self.source), "--dir", str(self.target))
        self.assertEqual(code, cli.EXIT_USAGE, err)
        self.assertIn("2 boards", err)
        self.assertEqual(list(self.target.iterdir()), [], "it wrote a partial store")

    def test_migrating_into_a_live_store_needs_force(self) -> None:
        """An overwrite is unrecoverable; "already initialised" is not."""
        code, _, err = self.yb("migrate", str(self.source), "--dir", str(self.repo))
        self.assertEqual(code, cli.EXIT_USAGE, err)
        self.assertEqual(self.store.card_ids(), [], "the refused migrate wrote cards")


class TestRealProcess(Board):
    """Only what cannot be true in-process: a real exit status from
    ``python -m yamlboard.cli``, and git's view of the files afterwards.

    ``main()``'s return value is not the same contract as a process exit status:
    the module-level ``sys.exit`` and argparse's own refusals both sit between
    them, so the codes agents branch on are verified end to end here.
    """

    def setUp(self) -> None:
        super().setUp()
        self.env = {**os.environ, **GIT_ENV}
        self.env.pop("YAMLBOARD_AGENT", None)

    def proc(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "yamlboard.cli", *argv],
                              cwd=str(self.repo), capture_output=True,
                              text=True, env=self.env, timeout=120)

    def test_a_real_process_gives_the_documented_code_for_each_outcome(self) -> None:
        """Agents branch on the process status, not on a Python return value.

        If ``__main__`` ever stopped propagating a code — an exception escaping
        the handler, or ``main``'s value never reaching ``sys.exit`` — every
        scripted branch on 3 would silently stop working, and two agents would
        collide again with nothing reporting it.
        """
        self.assertEqual(self.proc("create", "A card").returncode, cli.EXIT_OK)
        self.assertEqual(self.proc("claim", "WEB-1", "--as", "ada",
                                   "--paths", "src/api/**").returncode, cli.EXIT_OK)
        self.assertEqual(self.proc("claim", "WEB-1", "--as", "grigory").returncode,
                         cli.EXIT_DENIED)
        self.assertEqual(self.proc("claim", "WEB-1", "--as", "ghost").returncode,
                         cli.EXIT_USAGE)
        self.assertEqual(self.proc("nope").returncode, cli.EXIT_USAGE)
        denied = self.proc("claim", "WEB-1", "--as", "grigory", "--json")
        self.assertEqual(denied.returncode, cli.EXIT_DENIED)
        self.assertEqual(json.loads(denied.stdout)["holder"], "ada")

    def test_lease_churn_leaves_the_working_tree_clean_of_extra_files(self) -> None:
        """A claim touches exactly one card file, and a no-op claims nothing.

        The rewrite exists so an unrelated card is an unrelated *path* in git. If
        a claim rewrote ``board.yaml``, or a retry rewrote the card, every agent
        would touch line 1 of the same file and merge conflicts would be back —
        the exact thing this project was rewritten to remove.
        """
        self.assertEqual(self.proc("create", "A card").returncode, cli.EXIT_OK)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "board")

        self.assertEqual(self.proc("claim", "WEB-1", "--as", "ada",
                                   "--paths", "src/api/**").returncode, cli.EXIT_OK)
        self.assertEqual(dirty(self.repo), [".yamlboard/cards/WEB-1.yaml"],
                         "a claim must touch one card file and nothing else")

        self.assertEqual(self.proc("claim", "WEB-1", "--as", "ada",
                                   "--paths", "src/api/**").returncode, cli.EXIT_OK)
        self.assertEqual(dirty(self.repo), [".yamlboard/cards/WEB-1.yaml"],
                         "re-claiming the same card changed bytes again")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
