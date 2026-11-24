"""The coordination invariants, exercised through ``api.apply`` on a real store.

Everything here runs against ``.yamlboard/`` files inside a real ``git init``'d
temporary repository, because the guarantees that matter are file guarantees:
a refused op leaves every byte where it was, a lease renewal moves one line, and
a card write touches one path so an unrelated card cannot conflict in git. The
clock is :func:`yamlboard.model.now` patched forward — nothing sleeps, and the
30-second lease floor is proven by moving time, not waiting for it.

What this file deliberately does *not* test: the HTTP mapping (``test_server``),
exit codes and argv (``test_cli``), glob/lease/cycle arithmetic on its own
(``test_model``), and round-trip YAML fidelity (``test_store``).
"""

from __future__ import annotations

import datetime
import importlib.resources as resources
import os
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable

from test_gitops import GIT_ENV, git

from yamlboard import api, cli, model, store
from yamlboard.model import ValidationError

T0 = datetime.datetime(2026, 5, 4, 12, 0, 0)
PEOPLE = ("ada", "grigory", "zoe")


class Clock:
    """Move ``model.now`` and hand it back, whatever the test does to it.

    Lease liveness is read through that one function, so time travel is the only
    way to test expiry without a sleeping suite.
    """

    def __init__(self, start: datetime.datetime = T0):
        self.now = start
        self._real = model.now

    def __enter__(self) -> "Clock":
        model.now = lambda: self.now
        return self

    def __exit__(self, *exc: object) -> None:
        model.now = self._real

    def travel(self, **delta: int) -> datetime.datetime:
        self.now = self.now + datetime.timedelta(**delta)
        return self.now


class Ops(unittest.TestCase):
    """A committed repository holding a v2 store, with a deterministic clock."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "commit.gpgsign", "false")

        self._clock_entered = Clock().__enter__()
        self.addCleanup(Clock.__exit__, self._clock_entered, None, None, None)
        self.clock = self._clock_entered

        self.store = store.Store(self.repo)
        self.store.create("Web", key="WEB")
        doc = self.store.read_board().doc
        doc["people"] = [{"id": p, "name": p.title()} for p in PEOPLE]
        doc["labels"] = [{"id": "bug", "name": "Bug"}, {"id": "feat", "name": "Feature"}]
        doc["boards"][0]["custom_fields"] = [
            {"id": "customer", "name": "Customer", "type": "select",
             "options": ["Acme", "Globex"]},
        ]
        self.store.write_board(doc)
        self.commit("seed the board")

    # -- driving the ops layer --------------------------------------------- #

    def op(self, op: str, *, tokens: dict[str, str] | None = None, **args) -> dict:
        """One payload, one ``Tx``, one write — exactly what ``serve`` does."""
        tx = api.Tx(self.store) if tokens is None else api.Tx(self.store, tokens=tokens)
        return api.apply(self.store, {"op": op, "args": args}, tx=tx)["result"]

    def expect(self, error: type[BaseException], op: str, **args):
        with self.assertRaises(error) as caught:
            self.op(op, **args)
        return caught.exception

    def new_card(self, title: str, **args) -> str:
        return self.op("card.create", board="web", title=title, **args)["id"]

    def claim(self, card_id: str, agent: str, paths: Any = None, **args) -> dict:
        return self.op("claim.create", id=card_id, agent=agent, paths=paths, **args)

    # -- reading the files back --------------------------------------------- #

    def doc(self) -> dict:
        return model.jsonable(self.store.read_board().doc)

    def board(self, board_id: str = "web") -> dict:
        return model.jsonable(model.find_board(self.store.read_board().doc, board_id))

    def card(self, card_id: str) -> dict:
        return model.jsonable(self.store.read_card(card_id).doc)

    def tx(self) -> api.Tx:
        return api.Tx(self.store)

    def ready(self, agent: str | None = None, paths: list[str] | None = None) -> list[str]:
        return [str(c.get("id")) for c in api.ready(self.tx(), agent, paths)]

    def state(self, card_id: str, agent: str | None = None) -> str:
        return api.state_of(self.tx(), self.tx().card(card_id), agent)

    # -- bytes on disk ------------------------------------------------------- #

    def files(self) -> dict[str, tuple[bytes, int]]:
        """Every YAML file as (bytes, mtime_ns): the only proof of "nothing moved"."""
        return {
            str(p.relative_to(self.store.root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted(self.store.root.rglob("*.yaml"))
        }

    def card_text(self, card_id: str) -> str:
        path = self.store.locate(card_id)
        self.assertIsNotNone(path, f"{card_id} has no file")
        return path.read_text(encoding="utf-8")  # type: ignore[union-attr]

    def no_temp_files(self) -> None:
        left = [p.name for p in self.store.root.glob(f"{store.TEMP_PREFIX}*")]
        self.assertEqual(left, [], "a refused write left a temp file behind")

    # -- git ---------------------------------------------------------------- #

    def commit(self, message: str) -> None:
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", message)

    def status(self) -> str:
        return git(self.repo, "status", "--porcelain")

    def numstat(self) -> list[tuple[int, int, str]]:
        """``git diff --numstat`` as (insertions, deletions, path) triples."""
        rows = []
        for line in git(self.repo, "diff", "--numstat").splitlines():
            added, deleted, path = line.split("\t", 2)
            rows.append((int(added), int(deleted), path))
        return rows

    # -- a cross-machine claim, written straight into the file --------------- #

    def plant_claim(self, card_id: str, agent: str, at: datetime.datetime,
                    paths: list[str], ttl: int = 1800) -> None:
        """A claim as it arrives from another clone's merge.

        ``claim.create`` refuses to overlap, which is correct locally and useless
        here: the whole point of ``conflicts`` is two claims that were each legal
        when written and only met after a pull. So this writes the file the way
        the other agent's file would read post-merge.
        """
        board = self.store.read_board().doc
        card = self.store.read_card(card_id).doc
        card["claim"] = {
            "agent": agent, "at": at,
            "expires": at + datetime.timedelta(seconds=ttl),
            "paths": paths,
        }
        card["assignee"] = agent
        self.store.write_card(card, board=board)


# --------------------------------------------------------------------------- #
# the transaction: nothing half-applies
# --------------------------------------------------------------------------- #

class TestFlushIsAtomic(Ops):
    def test_a_refused_op_leaves_every_byte_where_it_was(self) -> None:
        """A payload the store rejects must not half-apply.

        ``flush`` validates the whole store before its first byte moves. Without
        that, an op that touched two cards and then failed would leave one card
        rewritten: the agent believes it holds the work, the board says it does
        not, and no later command rewrites the orphan back.
        """
        first, other = self.new_card("One"), self.new_card("Two")
        broken = self.store.card_path(other)
        broken.write_text(self.card_text(other) + "colour: red\n", encoding="utf-8")
        self.assertTrue(self.store.read_all().card(other).result,
                        "the fixture must be a store the reader calls invalid")

        before = self.files()
        with self.assertRaises(ValidationError) as caught:
            self.claim(first, "ada", "src/api/**")
        self.assertIn(other, " ".join(caught.exception.errors))
        self.assertEqual(self.files(), before)
        self.assertNotIn("claim", self.card(first))
        self.no_temp_files()

    def test_a_refused_create_burns_no_id_and_lands_no_file(self) -> None:
        """A create that dies in validation must not eat the number it minted.

        ``next_card`` is the only record of which ids exist. Burning one hides a
        gap no tool explains, and a half-written file with a valid name is worse:
        it reads as a card nobody created.
        """
        self.op("field.create", board="web", name="Ticket", type="text", required=True)
        before = self.files()
        counter = self.board()["next_card"]

        self.expect(api.BadRequest, "card.create", board="web", title="no ticket")
        self.assertEqual(self.files(), before)
        self.assertEqual(self.board()["next_card"], counter)
        # the number is still there for the next legitimate card
        self.assertEqual(self.new_card("with ticket", fields={"ticket": "T-1"})[-1],
                         str(counter))

    def test_a_malformed_payload_is_refused_before_the_store_is_openeD(self) -> None:
        before = self.files()
        for payload in ({}, {"op": 42}, {"op": "card.frobnicate"},
                        {"op": "card.create", "args": "nope"}):
            with self.assertRaises(api.BadRequest):
                api.apply(self.store, payload)
        self.assertEqual(self.files(), before)

    def test_apply_reports_the_op_it_ran(self) -> None:
        """One payload, one op — the reply has to name it.

        ``apply`` refuses to batch on purpose; the echoed name is what lets the
        browser match a response to the drag that caused it instead of guessing
        from a result dict that says only ``{"id": ...}``.
        """
        self.assertEqual(api.apply(self.store,
                                   {"op": "document.update", "args": {"title": "Renamed"}}),
                         {"op": "document.update", "result": {"title": "Renamed"}})
        self.assertEqual(self.doc()["title"], "Renamed")


# --------------------------------------------------------------------------- #
# per-file tokens: what a stale write costs
# --------------------------------------------------------------------------- #

class TestPerFileTokens(Ops):
    def test_a_stale_token_names_the_card_that_moved(self) -> None:
        """The browser's only defence against a hand edit or a pull mid-click.

        Answer "changed since you read them: WEB-1" and it reloads; answer
        nothing and two people's edits overwrite each other silently.
        """
        card_id = self.new_card("One")
        self.commit("card")
        self.card_text(card_id)                       # exists
        (self.store.card_path(card_id)).write_text(
            self.card_text(card_id).replace("title: One", "title: hand edited"),
            encoding="utf-8")

        caught = self.expect(api.Conflict, "claim.create", id=card_id, agent="ada",
                             tokens={"WEB-1": "dead-beef"}) if False else None
        del caught
        with self.assertRaises(api.Conflict) as caught:
            api.apply(self.store, {"op": "claim.create",
                                   "args": {"id": card_id, "agent": "ada"}},
                      tx=api.Tx(self.store, tokens={card_id: "dead-beef"}))
        self.assertEqual(caught.exception.paths, [card_id])
        self.assertIn(card_id, str(caught.exception))
        self.assertNotIn("claim", self.card(card_id), "a refused write still wrote")

    def test_every_stale_key_is_reported_at_once(self) -> None:
        """One reload, not a game of 409 whack-a-mole across three cards.

        ``column.delete`` rewrites the cards in the column plus ``board.yaml``.
        Reporting one stale key at a time would make the human click retry once
        per file, each failure looking like a new bug.
        """
        for title in "ABC":
            self.new_card(title)
        self.commit("seed cards")
        for title in "DE":
            card_id = self.new_card(title, column="doing")
            self.op("card.update", id=card_id, patch={"estimate": 1})
        self.commit("more cards")

        for path in self.store.root.rglob("*.yaml"):   # everything moved under us
            path.touch()
        tokens = {key: "stale" for key in api.tokens_of(self.tx())}
        # sampled *after* the touch: the touch itself moves every mtime, and a
        # refused write is only proven harmless against the tree it started from
        before = self.files()

        with self.assertRaises(api.Conflict) as caught:
            api.apply(self.store,
                      {"op": "column.delete", "args": {"board": "web", "id": "doing"}},
                      tx=api.Tx(self.store, tokens=tokens))
        # exactly the files the op writes — the two cards in ``doing`` plus the
        # board that loses the column. Every other token is stale too (the loop
        # touched the whole tree) and must be *ignored*: see
        # :meth:`test_a_token_for_a_file_you_never_touch_is_not_checked`, which
        # pins that a touched-but-unwritten card is not this write's business.
        self.assertEqual(caught.exception.paths, ["WEB-4", "WEB-5", "board"])
        self.assertIn("board", caught.exception.paths)
        self.assertEqual(self.files(), before)

    def test_a_token_for_a_file_you_never_touch_is_not_checked(self) -> None:
        """Tokens are a claim about what you read, not a lock on the store.

        Refusing an unrelated stale token would stop an agent from claiming a
        free card because a *different* card changed — which is exactly the
        global-``rev`` behaviour the split into files removed.
        """
        mine, theirs = self.new_card("Mine"), self.new_card("Theirs")
        self.assertEqual(self.claim(mine, "ada", "src/a.py",
                                    tokens={theirs: "bogus"})["agent"], "ada")
        self.assertEqual(self.card(theirs).get("claim"), None)

    def test_a_caller_with_no_tokens_is_never_refused(self) -> None:
        """An agent working straight from the files has no token to offer.

        Requiring one would make the CLI useless: the whole promise is that
        ``yamlboard claim`` works in a fresh clone with no server and no prior
        request. The bytes it can read *are* its view of the store.
        """
        card_id = self.new_card("One")
        self.commit("card")
        (self.store.card_path(card_id)).write_text(
            self.card_text(card_id).replace("title: One", "title: pulled from origin"),
            encoding="utf-8")
        result = self.claim(card_id, "grigory", "docs/**")
        self.assertEqual(result["agent"], "grigory")
        self.assertEqual(self.card(card_id)["title"], "pulled from origin")
        self.assertEqual(self.card(card_id)["claim"]["agent"], "grigory")

    def test_a_token_read_from_the_file_writes_cleanly(self) -> None:
        """The token grammar on the wire is the one ``flush`` compares against.

        A mismatch here (hex vs decimal, mtime vs inode) makes every browser
        write a 409 while the CLI works, which reads as a broken UI, not a
        broken token.
        """
        card_id = self.new_card("One")
        tokens = api.tokens_of(self.tx())
        self.assertEqual(tokens["board"], self.store.read_board().token())
        self.assertEqual(tokens[card_id], self.store.read_card(card_id).token())
        path = self.store.card_path(card_id)
        stamp = store._stat(path)
        self.assertEqual(tokens[card_id], f"{stamp[0]:x}-{stamp[1]:x}")
        self.assertTrue(self.claim(card_id, "ada", "src/a.py",
                                   tokens={card_id: tokens[card_id]})["expires"])


# --------------------------------------------------------------------------- #
# claims
# --------------------------------------------------------------------------- #

class TestClaims(Ops):
    def test_claiming_records_the_holder_and_becomes_the_assignee(self) -> None:
        """An assignee the board does not show is a card nobody can find.

        The claim is what other agents read; the assignee is what the human and
        the list view render. They have to agree, or a card is simultaneously
        taken (to agents) and free (to a person).
        """
        card_id = self.new_card("One")
        result = self.claim(card_id, "ada", "src/api/**,tests/test_api.py")
        card = self.card(card_id)
        self.assertEqual(result["paths"], ["src/api/**", "tests/test_api.py"])
        self.assertEqual(result["expires"], model.iso(T0 + datetime.timedelta(minutes=30)))
        self.assertEqual(card["claim"]["agent"], "ada")
        self.assertEqual(card["claim"]["paths"], ["src/api/**", "tests/test_api.py"])
        self.assertEqual(card["assignee"], "ada")
        self.assertEqual(self.state(card_id, "ada"), "yours")
        self.assertEqual(self.state(card_id, "grigory"), "taken")
        self.assertEqual(self.ready("grigory"), [])

    def test_a_second_agent_is_denied_and_the_denial_names_the_holder(self) -> None:
        """Exit 3's whole job: say who has it, and what to do instead.

        A denial without the holder and a next command is what makes an agent
        retry the same claim in a loop, or reach for a ``--force`` that must not
        exist.
        """
        card_id = self.new_card("One")
        self.claim(card_id, "ada", "src/api/**")
        before = self.files()
        caught = self.expect(api.ClaimDenied, "claim.create", id=card_id, agent="grigory")
        self.assertIn("ada", caught.lines[0])
        self.assertIn("holds it", caught.lines[0])
        self.assertEqual(caught.detail["holder"], "ada")
        self.assertEqual(caught.detail["reason"], "taken")
        self.assertEqual(caught.lines[-1], f"next: yamlboard ready --as grigory")
        self.assertEqual(self.files(), before, "a denial must not touch the store")

    def test_a_denial_lists_every_reason_at_once(self) -> None:
        """One corrected command beats three retries.

        Held *and* blocked *and* colliding: reporting only the first reason sends
        the agent back with a fixed path that still hits the block, and it reads
        the repeated failure as a bug in the board.
        """
        held, blocker, other = self.new_card("Held"), self.new_card("Blocker"), self.new_card("Other")
        self.claim(held, "ada", "src/api/**")
        self.claim(other, "zoe", "docs/**")
        self.op("card.dep.add", id=held, blocked_by=blocker)

        caught = self.expect(api.ClaimDenied, "claim.create", id=held, agent="grigory",
                             paths="docs/x.py")
        body = "\n".join(caught.lines)
        self.assertIn("ada holds it", body)
        self.assertIn(f"waits on {blocker}", body)
        self.assertIn("zoe claimed docs/**", body)
        self.assertEqual(caught.lines[-1], f"next: yamlboard ready --as grigory")

    def test_re_claiming_your_own_card_with_the_same_paths_writes_nothing(self) -> None:
        """A retry after a lost response must not churn the file.

        The agent cannot tell "timed out" from "landed". If the retry moved
        ``expires`` it would also move the card's diff, so ``git status`` shows a
        change nobody made and every downstream lease clock silently reset.
        """
        card_id = self.new_card("One")
        self.claim(card_id, "ada", "src/api/**")
        self.commit("claim")
        before = self.files()
        stamp = self.store.card_path(card_id).stat().st_mtime_ns

        result = self.claim(card_id, "ada", "src/api/**")
        self.assertTrue(result["unchanged"])
        self.assertEqual(self.files(), before)
        self.assertEqual(self.store.card_path(card_id).stat().st_mtime_ns, stamp)
        self.assertEqual(self.status(), "", "a no-op claim dirtied the working tree")
        # an empty re-ask is covered by whatever is already held
        self.assertTrue(self.claim(card_id, "ada", None)["unchanged"])
        self.assertEqual(self.files(), before)

    def test_widening_your_own_claim_is_a_denial_not_a_quiet_edit(self) -> None:
        """Paths are a promise other agents rely on; you cannot grow one alone.

        Silent widening means grigory edits ``docs/`` because ada's claim said
        ``src/**`` and then quietly became ``**`` — a collision nobody refused.
        """
        card_id = self.new_card("One")
        self.claim(card_id, "ada", "src/**")
        before = self.files()
        caught = self.expect(api.ClaimDenied, "claim.create", id=card_id, agent="ada",
                             paths="docs/x.py")
        self.assertIn("already yours", caught.lines[0])
        self.assertEqual(self.card(card_id)["claim"]["paths"], ["src/**"])
        self.assertEqual(self.files(), before)

    def test_renew_moves_the_expiry_line_and_nothing_else(self) -> None:
        """A renewal has to be one line in ``git diff`` or it is spam.

        Renewals happen constantly. If one rewrote the card, every human's
        ``git log`` would be lease traffic and the real edit would be lost in it.
        """
        card_id = self.new_card("One")
        self.claim(card_id, "ada", "src/api/**")
        self.commit("claim")
        self.clock.travel(minutes=10)

        result = self.op("claim.renew", id=card_id, agent="ada")
        self.assertEqual(self.numstat(),
                         [(1, 1, ".yamlboard/cards/WEB-1.yaml")],
                         self.card_text(card_id))
        diff = git(self.repo, "diff", "--unified=0")
        changed = [l for l in diff.splitlines()
                   if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
        self.assertEqual(len(changed), 2, diff)
        self.assertTrue(all("expires" in l for l in changed), changed)
        self.assertEqual(result["expires"],
                         model.iso(T0 + datetime.timedelta(minutes=40)))
        self.assertFalse(result["retaken"])
        self.assertEqual(self.card(card_id)["claim"]["paths"], ["src/api/**"])

    def test_renewing_someone_elses_claim_or_an_unclaimed_card_is_denied(self) -> None:
        """Extending another agent's lease from their back is a claim by proxy."""
        card_id = self.new_card("One")
        self.claim(card_id, "ada")
        caught = self.expect(api.ClaimDenied, "claim.renew", id=card_id, agent="grigory")
        self.assertEqual(caught.detail["reason"], "not_yours")
        self.assertEqual(caught.lines[-1], "next: yamlboard ready --as grigory")

        other = self.new_card("Two")
        caught = self.expect(api.ClaimDenied, "claim.renew", id=other, agent="grigory")
        self.assertEqual(caught.detail["reason"], "unclaimed")
        self.assertNotIn("claim", self.card(other))

    def test_release_clears_the_claim_and_the_assignee(self) -> None:
        """A released card that still shows an assignee is a card nobody will pick up.

        The list view and the ready queue disagree, and the next agent assumes
        someone else has it — which is how work dies after a half-finished run.
        """
        card_id = self.new_card("One")
        self.claim(card_id, "ada", "src/api/**")
        self.assertEqual(self.op("claim.release", id=card_id, agent="ada")["was"], "ada")
        card = self.card(card_id)
        self.assertNotIn("claim", card)
        self.assertNotIn("assignee", card)
        self.assertEqual(self.ready("grigory"), [card_id])
        # releasing twice is not an error: an agent retrying a release must not
        # see a failure it cannot explain.
        self.assertEqual(self.op("claim.release", id=card_id, agent="ada")["released"], False)

    def test_releasing_another_agents_live_claim_needs_force_and_says_who(self) -> None:
        """``released_by`` is the audit trail for a lease someone ended early.

        Without it, an agent that frees a colleague's card to take the work
        leaves no trace, and weeks later nobody can say who moved what.
        """
        card_id = self.new_card("One")
        self.claim(card_id, "ada", "src/api/**")
        before = self.files()
        caught = self.expect(api.ClaimDenied, "claim.release", id=card_id, agent="grigory")
        self.assertIn("--force", caught.lines[0])
        self.assertEqual(self.files(), before)

        result = self.op("claim.release", id=card_id, agent="grigory", force=True)
        self.assertEqual(result["was"], "ada")
        self.assertEqual(self.card(card_id)["released_by"], "grigory")
        self.assertNotIn("claim", self.card(card_id))

    def test_finishing_a_card_releases_its_lease(self) -> None:
        """Done is the commonest way a lease ends, and it must free the paths.

        If ``done`` left the claim in place, the card's files stay blocked after
        the work is merged and the board slowly fills with ghosts.
        """
        done_id, moved = self.new_card("Done"), self.new_card("Moved")
        self.claim(done_id, "ada", "src/api/**")
        self.claim(moved, "grigory", "docs/**")
        self.assertEqual(self.op("card.done", id=done_id)["column"], "done")
        self.assertNotIn("claim", self.card(done_id))
        self.assertNotIn("assignee", self.card(done_id))

        # and a drag into a done column is the same contract, whichever op made it
        self.op("card.update", id=moved, patch={"column": "done"})
        self.assertNotIn("claim", self.card(moved))
        self.assertEqual(self.card(moved)["column"], "done")
        self.assertNotIn(done_id, [c["id"] for c in api.ready(self.tx())])

    def test_an_unknown_agent_is_refused_and_lists_the_board_people(self) -> None:
        """A stale ``$YAMLBOARD_AGENT`` must read as "fix your name", not "go away".

        Auto-creating the identity would be worse: an assignee nobody added
        surfaces weeks later in a git blame, and the allowlist stops meaning
        anything.
        """
        card_id = self.new_card("One")
        caught = self.expect(api.ClaimDenied, "claim.create", id=card_id, agent="ghost")
        self.assertEqual(caught.detail["reason"], "unknown_agent")
        self.assertEqual(sorted(caught.detail["known"]), sorted(PEOPLE))
        self.assertNotIn("claim", self.card(card_id))

        self.assertEqual(self.expect(api.ClaimDenied, "claim.create", id=card_id,
                                     agent="Ada!").detail["reason"], "bad_agent")
        self.assertEqual(self.expect(api.ClaimDenied, "claim.create", id=card_id,
                                     agent="  ").detail["reason"], "no_agent")

    def test_the_lease_length_comes_from_the_board_unless_you_overrule_it(self) -> None:
        """A board of long refactors says ``lease_ttl: 4h`` and means every card.

        Falling back to the 30-minute constant would free a card mid-task, and a
        silently clamped ``--ttl`` hands back a lease shorter than the one the
        caller asked to work under.
        """
        board = self.store.read_board().doc
        board["boards"][0]["lease_ttl"] = "2h"
        self.store.write_board(board)
        slow = self.new_card("Long refactor")
        self.assertEqual(model.iso(self.card(slow)["claim"]["expires"]) if False else
                         self.claim(slow, "ada")["expires"],
                         model.iso(T0 + datetime.timedelta(hours=2)))

        quick = self.new_card("Quick")
        self.assertEqual(self.claim(quick, "ada", ttl="90s")["expires"],
                         model.iso(T0 + datetime.timedelta(seconds=90)))
        for bad in ("5s", "2d", "soon", ""):
            card_id = self.new_card(f"bad {bad}")
            error = self.expect(ValidationError, "claim.create", id=card_id,
                                agent="ada", **({} if bad == "" else {"ttl": bad})) \
                if bad != "" else None
            if error is not None:
                self.assertTrue(any("--ttl" in e for e in error.errors), error.errors)
            else:   # an empty --ttl is the board's default, not a parse failure
                self.assertEqual(self.claim(card_id, "ada")["expires"],
                                 model.iso(T0 + datetime.timedelta(hours=2)))

    def test_a_bare_directory_or_absolute_path_is_refused_with_the_fix(self) -> None:
        """``--paths src/api`` reads like one file and claims forty.

        Refusing it with ``/**`` in the message turns a board-blocking mistake
        into one retyped argument; ``../`` and absolute paths are a promise that
        reaches outside the repository.
        """
        (self.repo / "src" / "api").mkdir(parents=True)
        card_id = self.new_card("One")
        caught = self.expect(api.BadRequest, "claim.create", id=card_id, agent="ada",
                             paths="src/api")
        self.assertIn("src/api/**", str(caught))
        self.assertNotIn("claim", self.card(card_id))

        for junk in ("/etc/passwd", "../secrets/**", "C:/Windows/**"):
            self.expect(ValidationError, "claim.create", id=card_id, agent="ada",
                        paths=junk)
        # a real file path is the most precise claim there is
        self.assertEqual(self.claim(card_id, "ada", "src/api/routes.py")["paths"],
                         ["src/api/routes.py"])


# --------------------------------------------------------------------------- #
# expiry is release
# --------------------------------------------------------------------------- #

class TestExpiry(Ops):
    def test_an_expired_claim_is_simply_not_a_claim(self) -> None:
        """No daemon frees a card, so liveness is decided at read time.

        If expiry needed a writer, a board whose agents all died would stay
        blocked forever — every card held by a process that no longer exists.
        """
        card_id = self.new_card("One")
        self.claim(card_id, "ada", "src/api/**")
        self.assertEqual(self.state(card_id, "grigory"), "taken")

        self.clock.travel(minutes=31)
        claim = model.claim_of(self.store.read_card(card_id).doc)
        self.assertFalse(model.claim_live(claim), "the reader still thinks it is held")
        self.assertEqual(self.state(card_id, "grigory"), "ready")
        self.assertEqual(self.ready("grigory"), [card_id])
        summary = api.summary(self.tx())
        entry = next(c for b in summary["doc"]["boards"] for c in b["cards"]
                     if c["id"] == card_id)
        self.assertFalse(entry["live"])
        self.assertEqual(summary["agents"], {})
        self.assertEqual(api.conflicts(self.tx()), [])

    def test_a_second_agent_takes_an_expired_card_and_records_the_preemption(self) -> None:
        """``preempted`` is the difference between a steal and a documented handover.

        When ada's run dies and grigory finishes the card, the file has to say
        whose abandoned lease that was, or two agents' work looks like one's.
        """
        card_id = self.new_card("One")
        self.claim(card_id, "ada", "src/api/**")
        self.clock.travel(minutes=31)
        result = self.claim(card_id, "grigory", "src/api/**")
        self.assertEqual(result["preempted"], "ada")
        claim = self.card(card_id)["claim"]
        self.assertEqual(claim["agent"], "grigory")
        self.assertEqual(claim["preempted"], "ada")
        self.assertEqual(model.iso(claim["at"]), model.iso(self.clock.now))
        self.assertEqual(self.card(card_id)["assignee"], "grigory")

    def test_the_thirty_second_floor_is_legal_and_expires_without_sleeping(self) -> None:
        """The floor exists for short jobs; an agent must be able to use it.

        Proved by moving the clock, never by ``time.sleep``: a sleeping test is
        the one a CI run deletes, and then nothing stops the floor drifting.
        """
        card_id = self.new_card("One")
        result = self.claim(card_id, "ada", "src/api/**", ttl="30s")
        self.assertEqual(result["expires"], model.iso(T0 + datetime.timedelta(seconds=30)))
        self.assertEqual(self.state(card_id, "grigory"), "taken")

        self.clock.travel(seconds=31)
        claim = model.claim_of(self.store.read_card(card_id).doc)
        self.assertEqual(model.ttl_left(claim, self.clock.now), 0)
        self.assertEqual(self.state(card_id, "grigory"), "ready")
        self.assertEqual(self.ready("grigory"), [card_id])
        self.assertEqual(self.claim(card_id, "grigory", "src/api/**")["preempted"], "ada")

    def test_renewing_your_own_expired_claim_retakes_it_against_yourself(self) -> None:
        """An agent that lost its lease mid-task must not silently carry on.

        ``renew`` re-takes the card and records ``preempted`` against the same
        name, so if someone else grabbed it while the lease was open, the audit
        trail shows the gap instead of an unbroken lease that never existed.
        """
        card_id = self.new_card("One")
        self.claim(card_id, "ada", ttl="30s")
        self.clock.travel(seconds=31)
        result = self.op("claim.renew", id=card_id, agent="ada")
        self.assertTrue(result["retaken"])
        claim = self.card(card_id)["claim"]
        self.assertEqual(claim["preempted"], "ada")
        self.assertEqual(model.iso(claim["at"]), model.iso(self.clock.now))

    def test_an_expired_claim_does_not_block_your_paths(self) -> None:
        """The point of expiry: a dead agent's glob must not fence off the tree.

        Otherwise one crashed run holds ``src/**`` until someone notices and
        hand-edits YAML, which is the failure the whole lease design exists to
        avoid.
        """
        held, other = self.new_card("Held"), self.new_card("Other")
        self.claim(held, "ada", "src/**")
        self.assertEqual(self.expect(api.ClaimDenied, "claim.create", id=other,
                                     agent="grigory", paths="src/api/routes.py").detail
                         ["reason"], "ready_check")
        self.clock.travel(minutes=31)
        # both are ready once the lease dies; with no priority difference and one
        # frozen clock the queue falls back to id, so ``held`` (WEB-1) is first
        self.assertEqual(self.ready("grigory", ["src/api/routes.py"]), [held, other])
        # ``other`` was never claimed, so taking it records no preemption...
        self.assertEqual(self.claim(other, "grigory", "src/api/routes.py")["agent"],
                         "grigory")
        self.assertNotIn("preempted", self.card(other).get("claim") or {})
        # ...but taking the card whose dead lease just lapsed does: that is the
        # audit trail showing the takeover was a takeover, not a fresh grant
        self.assertEqual(self.claim(held, "zoe", "docs/**")["preempted"], "ada")


# --------------------------------------------------------------------------- #
# path ownership
# --------------------------------------------------------------------------- #

class TestPathOwnership(Ops):
    def test_a_glob_that_intersects_a_held_glob_is_refused(self) -> None:
        """Two agents in one file is the failure this product exists to prevent.

        ``src/api/**`` and ``src/api/routes/**`` are different strings that match
        the same files; a string comparison waves them through and both agents
        edit ``src/api/routes.py`` believing they hold it.
        """
        mine, theirs = self.new_card("Mine"), self.new_card("Theirs")
        self.claim(mine, "ada", "src/api/**")
        caught = self.expect(api.ClaimDenied, "claim.create", id=theirs,
                             agent="grigory", paths="src/api/routes/**")
        line = caught.lines[0]
        self.assertIn("ada claimed src/api/**", line)
        self.assertIn("src/api/routes/**", line)
        # the holder is ada; naming the caller here would send grigory to ask
        # himself for the file back
        self.assertNotIn("grigory", line)

        # a disjoint pattern is fine, and the ready queue says so without asking
        self.assertEqual(self.claim(theirs, "grigory", "docs/**")["agent"], "grigory")
        self.assertEqual(self.ready("zoe"), [])

    def test_ready_filters_to_work_you_can_actually_start(self) -> None:
        """``ready --paths`` is the one call that saves an agent a denial.

        Without the filter an agent walks the queue claiming until one sticks,
        and every failed attempt is a write to another card's file.
        """
        blocked_free = self.new_card("Free")
        held = self.new_card("Held")
        self.claim(held, "ada", "src/api/**")
        self.assertEqual(self.ready("grigory", ["src/api/routes.py"]), [])
        self.assertEqual(self.ready("grigory", ["docs/a.md"]), [blocked_free])
        self.assertEqual(self.ready("grigory"), [blocked_free])
        # your own held paths do not fence you out of your own card
        self.assertEqual(self.state(held, "ada"), "yours")

    def test_why_not_ready_reads_as_the_refusal_the_cli_prints(self) -> None:
        """``show`` and ``claim`` must not disagree about the same card.

        If ``show`` says ready and ``claim`` says denied, the agent trusts the
        first one and repeats a command that cannot work. The three reasons an
        agent acts on — someone else holds it, it is blocked, my paths collide
        — must all appear here and in that order, which is why the collision is
        built on a *rival* card: ``_holders`` excludes the card being asked
        about, so a claim already on that card is reported as ``taken``, never
        double-counted as a collision.
        """
        held, blocker, rival = (self.new_card("Held"), self.new_card("Blocker"),
                               self.new_card("Rival"))
        self.claim(held, "ada", "src/api/**")          # someone else holds it
        self.op("card.dep.add", id=held, blocked_by=blocker)   # it is blocked
        self.claim(rival, "grigory", "src/frontend/**")        # collides with mine
        lines = api.why_not_ready(self.tx(), self.tx().card(held), "grigory",
                                 ["src/api/x.py", "src/frontend/app.js"])
        joined = "\n".join(lines)
        self.assertIn("ada holds it", joined)
        self.assertIn(f"waits on {blocker}", joined)
        self.assertIn("grigory claimed src/frontend/**", joined)
        self.assertEqual(lines, self.expect(api.ClaimDenied, "claim.create", id=held,
                                            agent="grigory",
                                            paths=["src/api/x.py",
                                                  "src/frontend/app.js"]).lines[:-1])


# --------------------------------------------------------------------------- #
# dependencies
# --------------------------------------------------------------------------- #

class TestDependencies(Ops):
    def test_a_cycle_is_refused_and_names_the_whole_path(self) -> None:
        """A cycle makes the ready queue empty and the board looks finished.

        Nothing else reports it: every card waits on another, nothing is ready,
        and the message has to name both ends or nobody finds the edge to cut.
        """
        one, two = self.new_card("One"), self.new_card("Two")
        self.op("card.dep.add", id=two, blocked_by=one)
        caught = self.expect(api.BadRequest, "card.dep.add", id=one, blocked_by=two)
        self.assertIn(one, str(caught))
        self.assertIn(two, str(caught))
        self.assertIn(f"{one} -> {two} -> {one}", str(caught))
        # and nothing was written: WEB-1 must not now wait on WEB-2
        self.assertNotIn("blocked_by", self.card(one))

    def test_a_card_waits_for_its_blocker_and_only_a_finished_one(self) -> None:
        """``ready`` is the only list agents are told to pick from.

        A blocked card in it is an agent starting work that cannot land, then
        sitting on a claim while it waits.
        """
        blocker, dependent = self.new_card("Blocker"), self.new_card("Dependent")
        self.op("card.dep.add", id=dependent, blocked_by=blocker)
        self.assertEqual(self.ready("grigory"), [blocker])
        self.assertEqual(self.state(dependent, "grigory"), "blocked")
        self.assertEqual(
            api.summary(self.tx())["doc"]["boards"][0]["cards"]
            and next(c["open_blockers"] for b in api.summary(self.tx())["doc"]["boards"]
                     for c in b["cards"] if c["id"] == dependent),
            [blocker])

        self.op("card.done", id=blocker)
        self.assertEqual(self.ready("grigory"), [dependent])
        self.assertEqual(self.state(dependent, "grigory"), "ready")

    def test_self_block_and_unknown_blocker_are_refused_before_the_write(self) -> None:
        """An edge to a card that does not exist makes every read fail.

        ``validate`` rejects an unknown ``blocked_by``, so writing one would turn
        one bad argument into a store nobody can open, including ``check``.
        """
        card_id = self.new_card("One")
        before = self.files()
        self.assertIn("itself", str(self.expect(api.BadRequest, "card.dep.add",
                                                id=card_id, blocked_by=card_id)))
        self.assertIn("not a card", str(self.expect(api.BadRequest, "card.dep.add",
                                                    id=card_id, blocked_by="WEB-9")))
        self.assertIn("card id", str(self.expect(api.BadRequest, "card.dep.add",
                                                 id=card_id, blocked_by="everything")))
        self.assertEqual(self.files(), before)

    def test_deleting_a_blocker_frees_every_card_that_waited_on_it(self) -> None:
        """Delete the blocker, or the dependents dangle on nothing.

        The edge survives as an id with no file, which fails validation on the
        *dependent*'s file — so deleting one card makes another unreadable.
        """
        blocker, dependent = self.new_card("Blocker"), self.new_card("Dependent")
        self.op("card.dep.add", id=dependent, blocked_by=blocker)
        self.op("card.delete", id=blocker)
        self.assertIsNone(self.store.locate(blocker))
        self.assertNotIn("blocked_by", self.card(dependent))
        self.assertEqual(self.ready("grigory"), [dependent])
        self.assertIsNone(self.store.read_all().card(dependent).result)


# --------------------------------------------------------------------------- #
# cross-machine conflicts, and who wins
# --------------------------------------------------------------------------- #

class TestCrossMachineConflicts(Ops):
    def two_overlapping_claims(self) -> tuple[str, str, datetime.datetime, datetime.datetime]:
        """Two claims each legal where they were written, meeting after a pull."""
        early, late = self.new_card("Early"), self.new_card("Late")
        first = T0
        second = T0 + datetime.timedelta(minutes=5)
        self.plant_claim(early, "grigory", second, ["src/api/**"])
        self.plant_claim(late, "ada", first, ["src/**"])
        return early, late, first, second

    def test_the_overlap_is_reported_with_both_agents_and_both_patterns(self) -> None:
        """Git merged these files without a murmur; only the board can see it.

        Two clones, two card files, zero textual conflict — and both agents
        believe they own ``src/api``. Silence here is two people's work thrown
        away, so the report names both globs, not just the fact of a clash.
        """
        early, late, first, second = self.two_overlapping_claims()
        reports = api.conflicts(self.tx())
        self.assertEqual([r["kind"] for r in reports], ["overlap"])
        report = reports[0]
        self.assertEqual({p["a"] for p in report["patterns"]}, {"src/api/**"})
        self.assertEqual({p["b"] for p in report["patterns"]}, {"src/**"})
        self.assertEqual({report["winner"]["agent"], report["loser"]["agent"]},
                         {"ada", "grigory"})

    def test_the_earliest_claim_wins_and_resolve_frees_the_loser(self) -> None:
        """Every clone must pick the same winner without talking to each other.

        Earliest ``at`` is the only fact both sides already have. A resolve that
        picked the other way would leave two clones each insisting a different
        agent holds ``src/**``, with no way to notice.
        """
        early, late, first, second = self.two_overlapping_claims()
        report = api.conflicts(self.tx())[0]
        self.assertEqual(report["winner"]["agent"], "ada")
        self.assertEqual(report["winner"]["card"], late)
        self.assertEqual(model.iso(report["winner"]["at"]), model.iso(first))
        self.assertEqual(report["loser"]["agent"], "grigory")
        self.assertEqual(self.ready("zoe"), [], "both cards are held; nothing is ready")

        result = self.op("conflicts.resolve")
        self.assertEqual(result["resolved"],
                         [{"card": early, "agent": "grigory", "overridden_by": "ada"}])
        card = self.card(early)
        self.assertEqual(card["claim"]["overridden_by"], "ada")
        self.assertNotIn("assignee", card)
        self.assertEqual(self.ready("zoe"), [early],
                         "the loser's card must be takeable again")
        self.assertEqual(self.state(early, "zoe"), "ready")
        # the winner keeps holding theirs, and resolving twice changes nothing
        self.assertEqual(self.state(late, "ada"), "yours")
        self.assertEqual(self.op("conflicts.resolve")["count"], 0)
        self.assertEqual(api.conflicts(self.tx()), [])

    def test_a_tie_is_broken_by_the_alphabetically_smaller_agent(self) -> None:
        """Two agents can claim in the same second; the tie still needs one answer.

        Without a deterministic rule, clone A and clone B each name a different
        winner and neither backs down — the exact deadlock arbitration exists for.
        """
        mine, theirs = self.new_card("Mine"), self.new_card("Theirs")
        self.plant_claim(mine, "grigory", T0, ["src/**"])
        self.plant_claim(theirs, "ada", T0, ["src/**"])
        report = api.conflicts(self.tx())[0]
        self.assertEqual(report["winner"]["agent"], "ada")
        self.assertEqual(report["loser"]["agent"], "grigory")
        self.assertEqual(self.op("conflicts.resolve")["resolved"][0]["card"], mine)

    def test_detection_never_writes(self) -> None:
        """Reporting a conflict must not edit somebody else's claim.

        Two clones poll this on every refresh; if the read resolved, the first
        one to ask would decide the outcome and the second would see a board it
        never wrote.
        """
        self.two_overlapping_claims()
        self.commit("merged from a peer")   # the plant is the post-merge state;
                                           # "nothing dirty" is measured from here
        before = self.files()
        api.conflicts(self.tx(), ["src/api/routes.py"])
        api.summary(self.tx(), dirty=["src/api/routes.py"])
        self.assertEqual(self.files(), before)
        self.assertEqual(self.status(), "", "a read dirtied the working tree")

    def test_resolve_only_touches_the_card_you_named(self) -> None:
        """A human adjudicating one pair must not silently concede the others.

        ``--resolve`` is the one operation in this product that overrides
        another agent's claim; scoping it by card is what makes it safe to run
        on a board with several live overlaps.
        """
        first_a, first_b = self.new_card("A1"), self.new_card("B1")
        second_a, second_b = self.new_card("A2"), self.new_card("B2")
        self.plant_claim(first_a, "grigory", T0 + datetime.timedelta(minutes=5),
                         ["src/api/**"])
        self.plant_claim(first_b, "ada", T0, ["src/**"])
        self.plant_claim(second_b, "zoe", T0 + datetime.timedelta(minutes=7),
                         ["docs/**"])
        self.plant_claim(second_a, "grigory", T0 + datetime.timedelta(minutes=3),
                         ["docs/guide.md"])

        self.assertEqual(len(api.conflicts(self.tx())), 2)
        result = self.op("conflicts.resolve", card=first_a)
        self.assertEqual([r["card"] for r in result["resolved"]], [first_a])
        self.assertIn("overridden_by", self.card(first_a)["claim"])
        self.assertNotIn("overridden_by", self.card(second_b)["claim"])
        self.assertIn("no unresolved conflict",
                      str(self.expect(api.BadRequest, "conflicts.resolve",
                                      card=first_b)))

    def test_duplicate_ids_from_two_clones_are_reported_not_silently_dropped(self) -> None:
        """Two clones can mint ``WEB-7``; whichever file loses the merge must be seen.

        Renumbering behind someone's back breaks every ``blocked_by`` edge that
        pointed at it, so the honest answer is: report it, change nothing.
        """
        card_id = self.new_card("Clash")
        shutil_copy = self.store.card_path(card_id).read_bytes()
        (self.store.archive_dir / f"{card_id}.yaml").write_bytes(shutil_copy)

        reports = api.conflicts(self.tx())
        duplicates = [r for r in reports if r["kind"] == "duplicate"]
        self.assertEqual(len(duplicates), 1, reports)
        self.assertEqual(duplicates[0]["card"], card_id)
        self.assertEqual(duplicates[0]["times"], 2)
        self.assertIn("two clones", duplicates[0]["detail"])
        # the card is still there for the human to fix, not hidden
        self.assertEqual(self.card(card_id)["id"], card_id)

    def test_a_claimed_path_already_dirty_in_the_worktree_is_reported(self) -> None:
        """Someone edited inside a claimed path; the board says so out loud.

        This is the one overlap honour-system claims cannot prevent — the edit
        is in the working tree, not the files — so the only defence is naming it.
        """
        card_id = self.new_card("Held")
        self.claim(card_id, "ada", "src/api/**")
        reports = api.conflicts(self.tx(), ["src/api/routes.py", "README.md"])
        dirty = [r for r in reports if r["kind"] == "dirty"]
        self.assertEqual(len(dirty), 1, reports)
        self.assertEqual(dirty[0]["card"], card_id)
        self.assertEqual(dirty[0]["agent"], "ada")
        self.assertEqual(dirty[0]["patterns"], ["src/api/**"])
        self.assertEqual(dirty[0]["files"], ["src/api/routes.py"],
                         "the report names the claim but not the file to go look at")
        self.assertIn("working tree", dirty[0]["detail"])
        self.assertEqual(api.conflicts(self.tx(), ["docs/a.md", "README.md"]), [])


# --------------------------------------------------------------------------- #
# the document the UI and the CLI both read
# --------------------------------------------------------------------------- #

class TestSummary(Ops):
    def test_there_is_no_global_revision_left_anywhere(self) -> None:
        """One counter for the store is what made two writers collide on line 1.

        A stray ``rev`` back — in the payload or the document — and the browser
        starts refusing unrelated writes again, which is the bug this rewrite was
        for.
        """
        card_id = self.new_card("One")
        summary = api.summary(self.tx())
        self.assertEqual(summary["schema_version"], model.VERSION == 2 and 2 or 0)
        self.assertNotIn("rev", summary)
        self.assertNotIn("rev", summary["doc"])
        self.assertEqual(sorted(summary["tokens"]), sorted(["board", card_id]))
        self.assertEqual(summary["tokens"]["board"], self.store.read_board().token())
        self.assertEqual(summary["ready"], [card_id])
        self.assertEqual(summary["conflicts"], [])
        self.assertEqual(summary["archived"], [])

    def test_lease_clocks_are_computed_once_for_both_ends(self) -> None:
        """The countdown the browser ticks and the CLI prints come from here.

        Two implementations of "how long is left" disagree by a rounding rule and
        the UI says 2m while the terminal says expired, right when an agent is
        deciding whether to renew.
        """
        card_id = self.new_card("One")
        self.claim(card_id, "ada", "src/api/**", ttl="2h")
        self.clock.travel(seconds=4500)          # 75m of a 120m lease: 45m left
        entry = next(c for b in api.summary(self.tx())["doc"]["boards"]
                     for c in b["cards"] if c["id"] == card_id)
        self.assertTrue(entry["live"])
        self.assertEqual(entry["claim"]["left"], 2700)
        self.assertEqual(entry["claim"]["span"], 7200)
        self.assertEqual(model.humanize(entry["claim"]["left"]), "45m")
        self.assertEqual(list(api.summary(self.tx())["agents"]), ["ada"])
        self.assertEqual(api.summary(self.tx())["agents"]["ada"]["cards"], [card_id])

    def test_the_ready_queue_is_deterministic_across_two_reads(self) -> None:
        """Same files, same order — even between two clones on two machines.

        Agents pick the first card in ``ready``. A queue that reordered between
        two reads of the same bytes sends two agents at the same card.
        """
        for priority in ("none", "low", "high"):
            self.new_card(f"p {priority}", priority=priority)
        first, second = self.ready("grigory"), self.ready("grigory")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 3)
        # priority first, high before none; equal priority (the two "med") falls
        # back to created, then id — never dict order. Under the frozen clock the
        # stamps are identical, so id is what separates the pair, and it is the
        # tie a second clone must resolve the same way.
        same = [self.new_card("same", priority="med"), self.new_card("same", priority="med")]
        self.assertEqual(self.ready("grigory"),
                         ["WEB-3", same[0], same[1], "WEB-2", "WEB-1"])


# --------------------------------------------------------------------------- #
# one card per file: the git guarantee the split exists for
# --------------------------------------------------------------------------- #

class TestOneCardPerFile(Ops):
    def test_editing_a_card_rewrites_only_that_card(self) -> None:
        """The whole reason for ``cards/<ID>.yaml``.

        Two agents editing two cards must produce two paths, or git conflicts on
        the shared file every time and the board becomes the thing people work
        around.
        """
        mine, theirs = self.new_card("Mine"), self.new_card("Theirs")
        self.commit("two cards")
        before = self.files()
        theirs_bytes = self.card_text(theirs)

        self.op("card.update", id=mine, patch={"priority": "urgent"})
        self.assertEqual(self.card(mine)["priority"], "urgent")
        self.assertEqual(self.card(theirs), before and self.card(theirs))
        self.assertEqual(self.card_text(theirs), theirs_bytes)
        self.assertEqual([p for _, _, p in self.numstat()],
                         [".yamlboard/cards/WEB-1.yaml"])

    def test_a_claim_is_coordination_and_never_moves_updated(self) -> None:
        """``updated:`` means "someone edited this card", and a lease is not that.

        If claim traffic moved it, ``git log`` and the list view's sort would say
        a card was edited every time somebody's clock ticked, and the real last
        edit would be unfindable.
        """
        card_id = self.new_card("One")
        self.commit("card")
        created = self.card(card_id)["updated"]
        diff = git(self.repo, "diff")
        self.assertEqual(diff, "", "committing after the claim hid the diff")

        self.claim(card_id, "ada", "src/api/**")
        self.clock.travel(minutes=10)
        self.op("claim.renew", id=card_id, agent="ada")
        self.op("claim.release", id=card_id, agent="ada")
        self.assertEqual(self.card(card_id)["updated"], created)

        self.op("card.update", id=card_id, patch={"desc": "a real edit"})
        self.assertNotEqual(self.card(card_id)["updated"], created)
        diff = git(self.repo, "diff", "--unified=0")
        self.assertIn("updated", diff)

    def test_a_card_cannot_be_renamed_while_it_holds_cards(self) -> None:
        """In v2 the id *is* the filename, so a board key rename renames files.

        Half-renaming would leave cards named ``WEB-*`` on a board whose key is
        ``WWW``, and ``validate`` rejects a card whose prefix no board claims —
        one rename would make every card on the board unreadable.
        """
        self.new_card("One")
        self.commit("card")
        caught = self.expect(api.BadRequest, "board.update", board="web", key="WWW")
        self.assertIn("filename", str(caught))
        self.assertEqual(self.board()["key"], "WEB")
        self.assertEqual(self.status(), "")

        empty = self.op("board.create", title="Ads", key="ADS")["id"]
        self.assertEqual(self.op("board.update", board=empty, key="AD")["key"], "AD")

    def test_archiving_moves_the_file_and_takes_the_card_out_of_circulation(self) -> None:
        """An archived card still holding ``ready`` status invites duplicate work.

        The move is a rename, so git sees one clean operation instead of a
        delete plus an add that a merge can recombine into two copies.
        """
        card_id = self.new_card("Old")
        self.commit("card")
        result = self.op("card.archive", id=card_id, archived=True)
        self.assertEqual(result["moved"], f"{card_id}.yaml")
        self.assertFalse((self.store.cards_dir / f"{card_id}.yaml").exists())
        self.assertTrue((self.store.archive_dir / f"{card_id}.yaml").is_file())
        self.assertEqual(self.ready("grigory"), [])
        summary = api.summary(self.tx())
        self.assertEqual(summary["archived"], [card_id])
        self.assertEqual([c["id"] for b in summary["doc"]["boards"] for c in b["cards"]],
                         [])

        self.op("card.unarchive", id=card_id)
        self.assertTrue((self.store.cards_dir / f"{card_id}.yaml").is_file())
        self.assertEqual(self.ready("grigory"), [card_id])

    def test_dropping_a_person_clears_claims_before_the_board_forgets_them(self) -> None:
        """Cards are written first, ``board.yaml`` last, and the order is the safety.

        Remove the person first and a crash between the two leaves a card whose
        assignee the file does not define — which fails validation on read and
        bricks the board, including ``check``, which is how you would fix it.
        """
        card_id = self.new_card("One")
        self.claim(card_id, "ada", "src/api/**")
        self.op("card.comment", id=card_id, text="noted", by="ada")
        self.commit("ada's work")

        self.assertEqual(self.op("person.delete", id="ada")["unassigned"], 1)
        card = self.card(card_id)
        self.assertNotIn("claim", card)
        self.assertNotIn("assignee", card)
        self.assertNotIn("by", card["comments"][0])
        self.assertNotIn("ada", [p["id"] for p in self.doc()["people"]])
        # and the board is still readable, which is the whole point of the order
        self.assertIsNone(self.store.read_all().card(card_id).result)
        self.assertEqual(self.expect(api.ClaimDenied, "claim.create", id=card_id,
                                     agent="ada").detail["reason"], "unknown_agent")

    def test_deleting_a_column_rewrites_the_card_files_that_were_in_it(self) -> None:
        """A card in a column that no longer exists is an invalid file.

        The move has to land in the same operation as the column's removal, or
        every later read of that card reports a column id the board never had.
        """
        card_id = self.new_card("One", column="doing")
        self.commit("card in doing")
        result = self.op("column.delete", board="web", id="doing", to="ready")
        self.assertEqual(result["moved"], 1)
        self.assertEqual(self.card(card_id)["column"], "ready")
        self.assertNotIn("doing", [c["id"] for c in self.board()["columns"]])
        self.assertIsNone(self.store.read_all().card(card_id).result)
        self.assertNotIn("column: doing", self.card_text(card_id))
        self.assertIn(".yamlboard/cards/WEB-1.yaml", [p for _, _, p in self.numstat()])

    def test_a_create_lands_one_new_file_and_advances_the_boards_counter(self) -> None:
        """``next_card`` lives on the board now; the rescan keeps hand-written ids safe.

        A counter that only increments can re-issue an id someone typed into a
        file by hand, and two cards with one id silently loses one in a merge.
        """
        self.new_card("First")
        # a human types a card file straight into the directory: no op ran, so
        # nothing raised the counter on the board
        (self.store.cards_dir / "WEB-2.yaml").write_text(
            "id: WEB-2\ntitle: Hand written\ncolumn: backlog\norder: 2048\n"
            "labels: []\npriority: none\n", encoding="utf-8")
        board = self.store.read_board().doc
        board["boards"][0]["next_card"] = 2       # a hand edit below the real high
        self.store.write_board(board)
        minted = self.new_card("Minted")
        self.assertEqual(minted, "WEB-3",
                         "the rescan did not climb past the hand-written id")
        self.assertEqual(self.board()["next_card"], 4)
        self.assertEqual(sorted(p.name for p in self.store.cards_dir.glob("*.yaml")),
                         ["WEB-1.yaml", "WEB-2.yaml", "WEB-3.yaml"])

    def test_a_custom_field_value_moves_with_the_card_and_dies_with_the_field(self) -> None:
        """Values are validated against the board that declares them.

        Leave an undeclared key behind and the card file stops validating, which
        takes the whole store's ``check`` with it.
        """
        card_id = self.new_card("One")
        self.op("card.update", id=card_id, patch={"fields": {"customer": "Acme"}})
        self.assertEqual(self.card(card_id)["fields"], {"customer": "Acme"})
        self.op("field.update", board="web", id="customer", options=["Globex"])
        self.assertEqual(self.card(card_id)["fields"], {"customer": ""})
        self.op("field.delete", board="web", id="customer")
        self.assertNotIn("customer", self.card(card_id).get("fields", {}))
        self.assertIsNone(self.store.read_all().card(card_id).result)


# --------------------------------------------------------------------------- #
# anti-drift pins: registry, verbs, skill, exit codes
# --------------------------------------------------------------------------- #

#: Every mutating op the agent-facing protocol depends on: ``cli.MUTATING``
#: promises a verb exists for it, and the skill teaches that verb.
COORDINATION_OPS = frozenset({
    "card.create", "card.done", "card.dep.add", "card.dep.remove",
    "claim.create", "claim.renew", "claim.release", "conflicts.resolve",
})

#: The slice of that protocol a person can run from the dashboard. Narrower on
#: purpose: board-ui *displays* blockers, it does not draw the graph, so the dep
#: edges belong to a verb and not to app.js.
BROWSER_CLAIM_OPS = COORDINATION_OPS - {"card.dep.add", "card.dep.remove"}


def ops_in_code(function: Callable[..., Any]) -> set[str]:
    """Dotted op names the function mentions, read from its code objects.

    Bytecode constants, not source text: this survives reformatting and moving
    the string into a dict, and it is how the test finds the op names ``verb_dep``
    builds rather than a hand-copied list that drifts with the CLI. Three-segment
    names (``card.dep.add``, ``card.comment.delete``) must survive too; matching
    on a single dot silently drops them and the dep verbs read as unreachable.
    """
    found: set[str] = set()
    sections = {"card", "claim", "conflicts", "column", "board", "person",
                "label", "field", "document"}
    stack = [function.__code__]
    seen: set[int] = set()
    while stack:
        code = stack.pop()
        if id(code) in seen:
            continue
        seen.add(id(code))
        for const in code.co_consts:
            if callable(getattr(const, "co_consts", None)):
                stack.append(const)
            elif isinstance(const, str) and "." in const:
                head, _, tail = const.partition(".")
                if head in sections and re.fullmatch(r"[a-z][a-z.]*", tail or ""):
                    found.add(const)
    return found


class TestAntiDrift(unittest.TestCase):
    """The three copies of the vocabulary — registry, CLI, skill — are one list.

    ``app.js`` cannot import Python and ``SKILL.md`` is prose, so each keeps its
    own copy of what exists. A verb added in one place and not another is an
    agent typing a command that does not exist, or an op the browser posts that
    the server answers ``unknown op`` — and nothing else in the suite notices.
    """

    def bundled_skill(self) -> str:
        """``SKILL.md`` as the installed package ships it, never a repo path.

        After ``uv tool install`` there is no checkout: a cwd-relative read would
        pass here and fail on the machine that matters, which is the bug
        ``skill install`` exists not to have.
        """
        path = Path(str(resources.files("yamlboard").joinpath(
            "skills", "yamlboard", "SKILL.md")))
        package = Path(api.__file__).resolve().parent
        self.assertTrue(path.is_file(), f"the wheel is missing {path}")
        self.assertEqual(path.resolve().parent, (package / "skills" / "yamlboard").resolve())
        return path.read_text(encoding="utf-8")

    def test_the_op_registry_is_exactly_the_documented_set(self) -> None:
        expected = {
            "card.create", "card.update", "card.delete", "card.move", "card.archive",
            "card.unarchive", "card.done", "card.comment", "card.comment.delete",
            "card.subtask.toggle", "card.dep.add", "card.dep.remove",
            "claim.create", "claim.renew", "claim.release", "conflicts.resolve",
            "column.create", "column.update", "column.reorder", "column.delete",
            "board.create", "board.update", "board.delete",
            "person.create", "person.update", "person.delete",
            "label.create", "label.update", "label.delete",
            "field.create", "field.update", "field.delete",
            "document.update",
        }
        self.assertTrue(expected, "the pin would be vacuous against an empty set")
        self.assertTrue(set(api.OPS) and all(callable(h) for h in api.OPS.values()))
        self.assertEqual(set(api.OPS), expected)
        # not vacuous: dropping one op, or inventing one, has to fail here
        self.assertNotEqual(set(api.OPS) - {"claim.create"}, expected)
        self.assertNotEqual(set(api.OPS) | {"claim.grant"}, expected)

    def test_every_op_the_client_posts_still_exists(self) -> None:
        """``app.js`` posts op names it cannot check; this is its type system.

        An op renamed on the server only shows up as a card that silently refuses
        to move, in whichever drag nobody tested.
        """
        source = (Path(api.__file__).resolve().parent / "static" / "app.js")
        posted = set(re.findall(
            r'"((?:card|claim|conflicts|column|board|person|label|field|document)\.[a-z.]+)"',
            source.read_text(encoding="utf-8")))
        self.assertGreater(len(posted), 10, "app.js no longer posts ops by name")
        self.assertEqual(posted - set(api.OPS), set(),
                         "the browser posts ops the registry does not have")
        # Only the claim protocol has to be reachable from the board: a person
        # takes, renews, releases and resolves there. Dependency edges are the
        # CLI's to edit — board-ui's contract is to *show* a blocked card and
        # name its blockers, not to redraw the graph — so those two ops are
        # pinned to a verb by the test below, not to the browser here.
        self.assertTrue(BROWSER_CLAIM_OPS <= posted,
                        "the claim protocol vanished from the browser")

    def test_every_mutating_op_the_agents_need_is_reachable_from_a_verb(self) -> None:
        """An op with no verb is a rule nobody can follow.

        The skill tells an agent to ``claim``, ``renew``, ``done``, ``dep``. If
        the claim layer grows an op and no verb reaches it, the protocol it
        describes is one the CLI cannot actually perform — and the agent works
        uncoordinated rather than reporting the gap.
        """
        posted: dict[str, set[str]] = {
            verb: ops_in_code(handler) for verb, handler in cli.VERB_FUNCTIONS.items()}
        reachable = set().union(*posted.values())
        self.assertTrue(reachable, "the CLI posts no ops at all")
        self.assertEqual(reachable - set(api.OPS), set(),
                         "the CLI posts an op the registry does not have")

        from_registry = {name for name in api.OPS
                         if name.startswith(("claim.", "card.dep."))
                         or name in ("card.create", "card.done", "conflicts.resolve")}
        self.assertEqual(from_registry, COORDINATION_OPS)
        self.assertTrue(COORDINATION_OPS <= reachable,
                        f"ops with no CLI verb: {sorted(COORDINATION_OPS - reachable)}")
        for op in sorted(COORDINATION_OPS):
            verbs = [verb for verb, ops in posted.items() if op in ops]
            self.assertEqual(len(verbs), 1, f"{op} is posted by {verbs}")
            self.assertIn(verbs[0], cli.MUTATING,
                          f"{op} writes files but {verbs[0]} is not in cli.MUTATING")

    def test_the_verb_registry_is_the_parser_and_the_skill_in_one_order(self) -> None:
        """``VERBS`` is the order ``--help`` lists and the skill declares.

        A verb in ``VERBS`` with no function is a ``--help`` line that dies at
        "unknown verb"; the order matters because the skill's command list is
        compared element-wise, so a reworded help page cannot drift from it.
        """
        self.assertEqual(len(cli.VERBS), len(set(cli.VERBS)), "duplicate verb")
        self.assertTrue(len(cli.VERBS) >= 15, cli.VERBS)
        self.assertEqual(tuple(cli.VERB_FUNCTIONS), cli.VERBS)
        subparsers = next(a for a in cli.build_parser()._actions
                          if isinstance(a, cli.argparse._SubParsersAction))
        self.assertEqual(tuple(subparsers.choices), cli.VERBS)
        self.assertNotEqual(tuple(reversed(cli.VERBS)), cli.VERBS,
                            "the comparison this pin relies on is a palindrome")
        self.assertTrue(set(cli.MUTATING) <= set(cli.VERBS))
        self.assertNotIn("serve", cli.MUTATING,
                         "agents are pointed at files, never at a server")
        self.assertIn("claim", cli.MUTATING)

    def test_the_skill_declares_exactly_the_verbs_that_exist(self) -> None:
        """``metadata.commands`` is how an agent picks this skill and what it trusts.

        A stale list is worse than none: an agent that reads ``yamlboard
        take WEB-3`` will run it, get exit 2, and conclude the board is broken
        rather than the documentation.
        """
        front = self.bundled_skill().split("---")[1]
        declared = re.search(r"^\s*commands:\s*\[([^\]]*)\]", front, re.MULTILINE)
        self.assertIsNotNone(declared, "SKILL.md has no metadata.commands list")
        commands = tuple(s.strip() for s in declared.group(1).split(","))  # type: ignore[union-attr]
        self.assertEqual(commands, cli.VERBS)
        # not vacuous: a reordered or edited list must fail the same assertion
        self.assertNotEqual(tuple(reversed(commands)), cli.VERBS)
        self.assertNotEqual(commands + ("take",), cli.VERBS)
        self.assertNotEqual(tuple(sorted(commands)), cli.VERBS)
        name = re.search(r"^name:\s*(\S+)", front, re.MULTILINE)
        self.assertEqual(name and name.group(1), "yamlboard")

    def test_the_skills_exit_table_matches_the_clis_own_promises(self) -> None:
        """Exit 3 is the one value an agent branches on without reading prose.

        If the skill's table and ``--help`` disagree on which code means "someone
        else holds it", a script retries a claim it should abandon — the failure
        the separate code exists to prevent.
        """
        constants = {
            "EXIT_OK": 0, "EXIT_CANNOT_LISTEN": 1, "EXIT_USAGE": 2,
            "EXIT_DENIED": 3, "EXIT_INTERRUPT": 130, "EXIT_PIPE": 141,
        }
        for name, value in constants.items():
            self.assertEqual(getattr(cli, name), value, name)

        epilog = {int(m.group(1)): m.group(2).strip()
                  for m in re.finditer(r"^ {2}(\d+)\s+(.+)$", cli.EPILOG, re.MULTILINE)}
        skill = {int(m.group(1)): m.group(2).strip()
                 for m in re.finditer(r"^\|(\d+)\|(.+)\|$", self.bundled_skill(),
                                      re.MULTILINE)}
        self.assertEqual(set(epilog), {0, 1, 2, 3, 130, 141}, epilog)
        self.assertEqual(set(skill), set(epilog),
                         "the skill's exit table and --help disagree")
        self.assertEqual(set(skill), set(constants.values()))

        def plain(text: str) -> str:
            return re.sub(r"[*_`]", "", text).lower()

        self.assertIn("claim denied", plain(skill[3]))
        self.assertIn("claim denied", plain(epilog[3]))
        self.assertIn("listen", plain(skill[1]))
        self.assertIn("listen", plain(epilog[1]))
        # the two halves of 2 that an agent acts on must be named in both
        for phrase in ("bad argument", "refused operation"):
            self.assertIn(phrase, plain(skill[2]))
            self.assertIn(phrase, plain(epilog[2]))

    def test_skill_install_ships_the_bytes_the_package_advertises(self) -> None:
        """``skill install`` copies the bundled file; a repo-relative copy drifts.

        A downstream repo that installs from the wheel holds no checkout, so if
        the resolved path ever moves outside the package the command installs
        nothing, or the wrong thing, while the tests still pass on a dev box.

        Dev checkouts resolve the package *through* ``src/``, so comparing the
        path against a cwd-built string proves nothing — the two strings are
        equal whether or not cwd had anything to do with it. The honest test is
        to run the lookup from a directory that holds no such file and show the
        answer does not move.
        """
        resolved = cli.skill_path()
        self.assertIsNotNone(resolved)
        bundled = Path(str(resources.files("yamlboard").joinpath(
            "skills", "yamlboard", "SKILL.md")))
        self.assertEqual(resolved.resolve(), bundled.resolve())
        self.assertTrue(bundled.read_bytes())
        elsewhere = Path(tempfile.mkdtemp()).resolve()
        self.assertFalse((elsewhere / "src").exists())
        saved = Path.cwd()
        try:
            os.chdir(elsewhere)
            moved = cli.skill_path()
        finally:
            os.chdir(saved)
        self.assertIsNotNone(moved)
        self.assertEqual(moved.resolve(), bundled.resolve(),
                         "the skill resolved from the working directory, not "
                         "the package")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
