"""The store is the arbiter: layout, per-file tokens, quiet writes, the lock.

Version 2 deleted ``read``/``update``/``write`` and the one global ``rev`` they
shared. What replaced them is a directory — ``board.yaml`` for the rare board
edit, ``cards/<ID>.yaml`` for one card per file, ``archive/`` for retired ones —
plus a per-file conflict token and an advisory flock on ``.agentboard/.lock``.
Every guarantee here is one an agent or a human loses loudly if it breaks:

* a card write must touch **one path**, or two clones collide in git again;
* a token must move exactly when the bytes move, and *not* when they do not,
  because it is the only thing that catches a hand edit or a ``git pull``;
* a redundant write must write **no bytes**, or N agents' lease churn fills a
  person's ``git status`` and they stop trusting the board;
* the scratch artefacts must be exactly the two names ``.gitignore`` narrows to,
  or the board — which *is* the coordination state — stops being tracked.

Nothing here sleeps for a lease: ``store`` owns no clock, and the claim in the
fixture expires in 2099, so no verdict depends on when the suite ran. Git is
real (``test_gitops.GIT_ENV``, so an ambient ``commit.gpgsign`` cannot steer a
result), the temp dirs are real, and roots are compared after ``.resolve()``
because macOS names the temp dir through a symlink.
"""

from __future__ import annotations

import datetime
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from io import StringIO
from pathlib import Path

from ruamel.yaml import YAML
from test_gitops import GIT_ENV, git

from agentboard import api, model, store
from agentboard.model import ValidationError

try:  # POSIX only; store degrades to a no-op lock without it
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

REPO = Path(__file__).resolve().parent.parent
needs_flock = unittest.skipUnless(store.fcntl is not None and fcntl is not None,
                                  "no flock on this platform")

BOARD = """\
# Personal boards -- comments are mine
version: 2
title: Personal
people:
  - id: ada
    name: Ada Lovelace
    color: "#2f6f4f"
  - id: grigory
    name: Grigory Perelman
    color: "#7c3aed"
labels:
  - id: bug
    name: Bug
    color: "#d1495b"
boards:
  - id: product
    key: PROD
    title: Product
    columns:
      - id: backlog
        name: Backlog
        color: "#7c8797"
      - id: doing
        name: In Progress
        wip_limit: 3
      - id: done
        name: Done
        is_done: true
    custom_fields:
      - id: customer
        name: Customer
        type: select
        options:
          - Acme
          - Globex
    next_card: 3
    lease_ttl: 2h
"""

# -> cards/PROD-1.yaml. A live claim, so "who holds what" reads straight off the
# fixture; 2099 so the suite never ages into a lie.
CLAIMED = """\
id: PROD-1
title: Fix the 'deploy' timeout   # trailing note
desc: |
  line one
  line two
column: doing
order: 1024.0
labels:
  - bug
priority: high
due: 2026-12-01
claim:
  agent: ada
  at: 2026-11-23T10:00:00
  expires: 2099-01-01T00:00:00
  paths:
    - src/api/**
archived: false
created: 2026-11-14T10:00:00
updated: 2026-11-21T14:03:00
"""

# -> archive/PROD-2.yaml
RETIRED = """\
id: PROD-2
title: The whole-file document reader
desc: |
  Superseded by the per-card store.
column: done
order: 2048.0
labels:
  - bug
archived: true
created: 2026-11-19T15:40:00
updated: 2026-11-22T16:00:00
"""


class Base(unittest.TestCase):
    """A resolved temp dir holding one real ``.agentboard/`` store.

    The store is written from text, never from ``Store.create``, so the fixtures
    show the layout a human actually types (comments, trailing notes, quotes)
    and a regression in ``create`` cannot hide behind a fixture built with it.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = None
        self.install(Path(self._tmp.name).resolve())

    # -- fixtures --------------------------------------------------------- #

    def install(self, into: Path) -> None:
        """Point ``self.store`` at a fresh store written inside ``into``."""
        self.dir = into
        self.store = store.Store(into)
        self.root = self.store.root
        self.store.cards_dir.mkdir(parents=True)
        self.store.archive_dir.mkdir(parents=True)
        self.board_path = self.store.board_path
        self.board_path.write_text(BOARD, encoding="utf-8")
        self.write_file("PROD-1", CLAIMED)
        self.write_file("PROD-2", RETIRED, archived=True)

    def write_file(self, card_id: str, text: str, *, archived: bool = False) -> Path:
        path = self.store.card_path(card_id, archived=archived)
        path.write_text(text, encoding="utf-8")
        return path

    def scratch_repo(self) -> Path:
        """Rebuild the store inside a real committed git repo and bind to it.

        The ``.gitignore`` comes from the checkout rather than being written
        here: the claim under test is that *the file we ship* narrows to the two
        scratch artefacts, so widening it back to ``.agentboard*`` must fail.
        """
        repo = self.dir / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "t@example.com")
        git(repo, "config", "user.name", "Test")
        git(repo, "config", "commit.gpgsign", "false")
        try:
            ignore = (REPO / ".gitignore").read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - an installed wheel ships no .gitignore
            ignore = "**/.agentboard/.lock\n**/.agentboard/.tmp-*.yaml\n"
        (repo / ".gitignore").write_text(ignore, encoding="utf-8")
        self.install(repo)
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "seed")
        self.repo = repo
        return repo

    def elsewhere(self) -> Path:
        """A directory with no store in it or above it, for negative lookups."""
        spare = tempfile.TemporaryDirectory()
        self.addCleanup(spare.cleanup)
        return Path(spare.name).resolve()

    # -- helpers ---------------------------------------------------------- #

    def porcelain(self, *args: str) -> list[str]:
        assert self.repo is not None, "call scratch_repo() first"
        return git(self.repo, "status", "--porcelain", *args).splitlines()

    def card(self, card_id: str) -> dict:
        return self.store.read_card(card_id).doc

    def save(self, doc: dict) -> store.File:
        """Write a card the way the ops layer does: document in, one file out."""
        return self.store.write_card(doc, board=self.store.read_board().doc)

    def bump(self, card_id: str, **claim: object) -> store.File:
        """Rewrite one claim field of one card, as ``renew`` would."""
        doc = self.card(card_id)
        doc["claim"].update(claim)
        return self.save(doc)

    def temps(self, of: store.Store | None = None) -> list[str]:
        """Scratch left anywhere under a store root, at any depth."""
        return sorted(p.name for p in (of or self.store).root
                      .rglob(f"{store.TEMP_PREFIX}*"))

    def held(self) -> bool:
        """True when a fresh descriptor cannot get the lock — i.e. it is held."""
        path = self.root / store.LOCK_NAME
        if not path.is_file():
            return False                      # nobody has touched the lock yet
        fd = os.open(str(path), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except OSError:
            return True
        finally:
            os.close(fd)


class TestLayout(Base):
    """One card is one path — the split that let the global ``rev`` go."""

    def test_discover_walks_up_from_a_subdirectory(self) -> None:
        """Agents run *inside* the repo they coordinate, often in ``src/``.

        A ``discover`` that only checked the cwd would tell a working agent to
        run ``agentboard init``, and a second board in one repo is worse than
        none: two agents then claim against different files and both believe
        they hold ``src/api/**``.
        """
        deep = self.store.cards_dir / "sub" / "deeper"
        deep.mkdir(parents=True)
        self.assertEqual(store.Store.discover(deep).root, self.root)
        self.assertEqual(store.Store.discover(self.dir).root, self.root)

    def test_discover_names_the_directory_it_searched_and_the_way_out(self) -> None:
        """A clone with no board is a real state, and the message is the fix.

        "no store" alone sends an agent off to read the source; the searched
        path plus ``agentboard init`` is the difference between one confused
        second and a human being paged. Discovery must also not *make* a store —
        an empty board in a clone looks like a board somebody emptied.
        """
        bare = self.elsewhere() / "fresh-clone"
        (bare / "src").mkdir(parents=True)
        with self.assertRaises(store.StoreError) as caught:
            store.Store.discover(bare)
        message = str(caught.exception)
        self.assertIn(str(bare), message)
        self.assertIn("agentboard init", message)
        self.assertFalse((bare / store.Store.DIRNAME).exists())

    def test_a_root_may_be_named_with_or_without_the_directory(self) -> None:
        """``init`` passes the repo, ``discover`` hands back ``.agentboard`` itself.

        If the two spellings disagreed, one of them would write a nested
        ``.agentboard/.agentboard`` and the board would silently split in two —
        each half convinced the other's claims do not exist.
        """
        self.assertEqual(store.Store(self.dir).root, self.root)
        self.assertEqual(store.Store(self.root).root, self.root)
        self.assertEqual(store.Store(str(self.dir)).root, self.root)

    def test_exists_is_the_board_file_alone(self) -> None:
        """``init`` refuses to overwrite, so ``exists`` decides whether it may write.

        ``cards/`` can outlive ``board.yaml`` after an interrupted init or a
        partial checkout. Treating that as "already a board" leaves a repo nobody
        can initialise; treating it as "no board" and then writing over a real
        one is unrecoverable — hence the check is the one file carrying all the
        board-level state.
        """
        self.assertTrue(self.store.exists())
        half = store.Store(self.dir / "halfway")
        half.cards_dir.mkdir(parents=True)
        (half.cards_dir / "PROD-1.yaml").write_text(CLAIMED, encoding="utf-8")
        self.assertFalse(half.exists())
        with self.assertRaises(store.StoreError) as caught:
            half.read_board()
        self.assertIn("agentboard init", str(caught.exception))

    def test_locate_follows_a_card_between_the_directories(self) -> None:
        """Everyone asks "where is PROD-2" before opening it.

        A stale answer writes a second copy of one card, and two files with one
        id is the one state where ``ready`` and ``conflicts`` disagree about who
        holds what — permanently, until someone notices by hand.
        """
        self.assertEqual(self.store.locate("PROD-1"),
                         self.root / store.CARDS_DIR / "PROD-1.yaml")
        self.assertEqual(self.store.locate("PROD-2"),
                         self.root / store.ARCHIVE_DIR / "PROD-2.yaml")
        self.assertIsNone(self.store.locate("PROD-3"))
        with self.assertRaises(ValidationError):
            self.store.locate("nope")

    def test_an_id_from_argv_is_never_path_joined_unchecked(self) -> None:
        """The id arrives from ``argv`` and from the browser.

        Joining it straight to a directory is how ``../../etc/passwd`` becomes a
        write target, and ``WEB-1/../../x`` is a perfectly legal filename, so
        the grammar has to be enforced *before* the join. It is the same
        ``CARD_RE`` the validator uses, so nothing a real card could be named is
        ever refused.
        """
        for bad in ("../etc/passwd", "WEB-1/../../x", "prod-1", "WEB", "",
                    None, 7, "WEB-1.yaml", "WEB-1/", "PROD-1/../PROD-2"):
            with self.assertRaises(ValidationError, msg=f"accepted {bad!r}"):
                self.store.card_path(bad)
        self.assertEqual(self.store.card_path("PROD-12").name, "PROD-12.yaml")
        self.assertEqual(self.store.card_path("PROD-12", archived=True).parent,
                         self.store.archive_dir)

    def test_card_ids_lists_live_cards_before_archived_ones(self) -> None:
        """The listing is what ``check`` prints and what a duplicate is caught in.

        Live before archived — a card present in both is the merge accident
        ``conflicts`` has to name, and the order is what tells a reader which
        copy ``locate`` resolved to — and identical across runs, or an agent
        diffing two ``check --json`` outputs cannot tell churn from a change.
        """
        self.write_file("PROD-10", "id: PROD-10\ntitle: Ten\ncolumn: backlog\n")
        self.write_file("PROD-3", "id: PROD-3\ntitle: Three\ncolumn: backlog\n",
                        archived=True)
        found = self.store.card_ids()
        self.assertEqual(found, ["PROD-1", "PROD-10", "PROD-2", "PROD-3"], found)
        self.assertEqual(self.store.card_ids(), found, "unstable across runs")
        self.assertEqual(self.store.locate("PROD-3").parent, self.store.archive_dir)

    def test_a_duplicate_id_from_a_merge_shows_up_twice(self) -> None:
        """Two clones can mint the same id; the merge leaves two files.

        ``conflicts`` is meant to catch that, and it can only do so if the
        listing reports both copies instead of collapsing them into one.
        """
        shutil.copy(self.store.card_path("PROD-1"),
                    self.store.card_path("PROD-1", archived=True))
        self.assertEqual(self.store.card_ids(), ["PROD-1", "PROD-1", "PROD-2"])
        self.assertEqual(self.store.locate("PROD-1"), self.store.card_path("PROD-1"),
                         "locate must prefer the live copy")

    def test_a_stray_yaml_file_is_named_not_dropped(self) -> None:
        """``cards/notes.yaml`` is somebody's card that will never be read.

        Skipping it silently loses work. It is reported by ``strays()`` rather
        than smuggled into ``card_ids()`` with a marker: that list is fed to
        ``model.card_context`` as a set of real ids, and a phantom ``?!notes.yaml``
        there is a fake id in the validator's view of the board. A non-YAML
        neighbour (a README left in the directory) stays invisible, because that
        is not a card by any reading and reporting it would train people to
        ignore the warning.
        """
        (self.store.cards_dir / "notes.yaml").write_text("hello: world\n",
                                                         encoding="utf-8")
        (self.store.cards_dir / "README.md").write_text("not a card\n", encoding="utf-8")
        self.assertEqual(self.store.card_ids(), ["PROD-1", "PROD-2"],
                         "a stray is not an id")
        self.assertEqual(self.store.strays(), ["notes.yaml"])
        self.assertEqual([str(f.doc.get("id")) for f in self.store.read_all().all_cards()],
                         ["PROD-1", "PROD-2"], "a stray file must not load as a card")

    def test_a_stray_file_is_not_a_validation_problem(self) -> None:
        """A stray is reported but must not gate every write.

        Folding ``strays()`` into ``Tx.problems`` would refuse an unrelated
        claim because of a file sitting in the same directory: ``flush`` won't
        write while ``problems`` is non-empty. So the store separates "this
        card is corrupt" (a problem, blocks writes, exit 2) from "there is a
        file here that is not a card" (a stray, visible, non-blocking). The
        exit code ``check`` gives it is CliTestsV2's to pin.
        """
        (self.store.cards_dir / "notes.yaml").write_text("hello: world\n",
                                                         encoding="utf-8")
        self.assertEqual(self.store.strays(), ["notes.yaml"])
        self.assertEqual(api.Tx(self.store).problems(), [],
                         "a stray must not gate every write")

    def test_a_malformed_card_is_kept_with_its_problem_parked_on_it(self) -> None:
        """One hand-edited card must not brick the board.

        Dropping it is worse than showing it: ``ready`` and ``conflicts`` go
        quiet about the file, so a second agent takes work somebody is holding.
        The errors ride on ``File.result`` so ``check`` can name the file, and
        the readable cards must still load around it.

        A file whose root parses but is not a mapping is the nastiest variant:
        ``id: [oops`` fails to parse and already took the StoreError path, while
        ``- 1`` / ``just a string`` parsed cleanly and reached ``_ordered`` as a
        list or str, raising ``AttributeError`` — so ``check``, ``serve`` and
        every write died with a traceback. Each shape must land on ``result``.
        """
        self.write_file("PROD-4", "id: PROD-4\ntitle: Broken\ncolumn: nowhere\n")
        self.write_file("PROD-5", "id: [oops\n  :::\n")
        self.write_file("PROD-6", "- 1\n- 2\n")
        self.write_file("PROD-7", "just a string\n")
        snapshot = self.store.read_all()
        problems = {f.path.name: f.result for f in snapshot.all_cards() if f.result}
        self.assertEqual(sorted(problems),
                         ["PROD-4.yaml", "PROD-5.yaml", "PROD-6.yaml", "PROD-7.yaml"])
        self.assertIn("column", " ".join(problems["PROD-4.yaml"]))
        self.assertIn("not valid YAML", " ".join(problems["PROD-5.yaml"]))
        for card in ("PROD-6", "PROD-7"):
            joined = " ".join(problems[f"{card}.yaml"])
            self.assertIn("expected a card mapping", joined)
            self.assertIn(card, " ".join(str(f.doc.get("id"))
                                         for f in snapshot.all_cards()),
                          f"{card} vanished from the listing entirely")
        # every doc the store hands out is a mapping, which is what _ordered,
        # summary and conflicts all index without re-checking
        for handle in snapshot.all_cards():
            self.assertIsInstance(handle.doc, dict)
        self.assertEqual(sorted(str(f.doc.get("id")) for f in snapshot.all_cards()
                               if not f.result), ["PROD-1", "PROD-2"])
        # and each problem reaches the surface `check` prints, exactly once —
        # the file verdict is not per board, so two boards must not double it
        problems_out = api.Tx(self.store).problems()
        self.assertTrue(any("PROD-4" in e for e in problems_out),
                        "an invalid card never reached check")
        self.assertEqual(sum("PROD-6" in e for e in problems_out), 1,
                         "a file-level problem was counted once per board")

    def test_the_scratch_of_an_interrupted_save_is_never_a_card(self) -> None:
        """A crashed save leaves ``.tmp-abc.yaml`` behind.

        If a scan loaded it as a card, the board gains a phantom whose id is a
        random suffix — and its filename no longer matches its ``id``, so the
        next real save of that id cannot find the file to replace. It is not a
        stray either: that advice is "rename it to WEB-9.yaml", and following it
        on agentboard's own git-ignored debris would corrupt the store.
        """
        stray = f"{store.TEMP_PREFIX}crash{store.TEMP_SUFFIX}"
        (self.store.cards_dir / stray).write_text(
            "id: PROD-77\ntitle: phantom\ncolumn: backlog\n", encoding="utf-8")
        self.assertEqual([str(f.doc.get("id")) for f in self.store.read_all().all_cards()],
                         ["PROD-1", "PROD-2"])
        self.assertEqual(self.store.card_ids(), ["PROD-1", "PROD-2"])
        self.assertEqual(self.store.strays(), [],
                         "temp scratch is debris, not a misfiled card")


class TestTokens(Base):
    """Per-file compare-and-set replaces the deleted global ``rev``."""

    def test_a_token_tracks_those_exact_bytes(self) -> None:
        """The token is the entire conflict story, so it must be byte-honest.

        A token that missed an edit lets a write land on top of a hand edit and
        the person loses work with a green checkmark; a token that moved without
        the bytes changing would make the browser reload mid-typing, forever.
        """
        handle = self.store.read_card("PROD-1")
        self.assertEqual(handle.token(), f"{handle.mtime:x}-{handle.size:x}")
        self.assertEqual(self.store.read_card("PROD-1").token(), handle.token())
        doc = handle.doc
        doc["title"] = "Fix the deploy timeout, again"
        written = self.save(doc)
        self.assertNotEqual(written.token(), handle.token())
        self.assertEqual(self.store.read_card("PROD-1").token(), written.token())

    def test_a_same_size_hand_edit_still_moves_the_token(self) -> None:
        """``2026-12-01`` -> ``2026-12-02`` keeps the byte count identical.

        A size-only token would call the later write safe and overwrite it — and
        that is exactly what ``rsync --size-only`` and some backup tools
        produce. The nanosecond half of the pair is the only reason it is caught.
        """
        handle = self.store.read_card("PROD-1")
        path = handle.path
        text = path.read_text(encoding="utf-8")
        edited = text.replace("due: 2026-12-01", "due: 2026-12-02")
        self.assertEqual(len(edited), len(text))
        path.write_text(edited, encoding="utf-8")
        again = self.store.read_card("PROD-1")
        self.assertEqual(again.size, handle.size)
        self.assertNotEqual(again.token(), handle.token())

    def test_a_missing_file_has_an_empty_token(self) -> None:
        """So "nothing there yet" and "I cannot stat it" stay distinguishable.

        A create carries no token for the card it is minting, and raising here
        would turn a first claim into a crash instead of a write.
        """
        self.assertIsNone(self.store.locate("PROD-9"))
        self.assertEqual(store.File(self.store.card_path("PROD-9"), {},
                                    None, None).token(), "")
        self.assertEqual(store.File(Path("nowhere"), {}, None, None).stamp(),
                         (None, None))

    def test_a_stale_token_refuses_the_write_and_names_the_file(self) -> None:
        """Two agents, two clones, one card file — the case ``rev`` got wrong.

        The browser posts back the tokens it rendered from. If the store did not
        re-stat inside the lock, a click would silently undo whatever landed in
        between and the UI would show a claim that is not on disk — which is how
        two agents end up editing one file believing each owns it.
        """
        mine = store.Store(self.dir)
        tokens = {"PROD-1": mine.read_card("PROD-1").token()}
        self.bump("PROD-1", agent="grigory")          # the write lands in between
        tx = api.Tx(mine, tokens=tokens)
        card = tx.card("PROD-1")
        card["title"] = "written from a stale view"
        tx.dirty(card)
        with self.assertRaises(api.Conflict) as caught:
            tx.flush()
        self.assertEqual(caught.exception.paths, ["PROD-1"])
        self.assertNotIn("written from a stale view",
                         self.store.card_path("PROD-1").read_text(encoding="utf-8"))
        self.assertEqual(self.card("PROD-1")["claim"]["agent"], "grigory")

    def test_a_neighbour_write_leaves_the_other_tokens_alone(self) -> None:
        """This is the whole rewrite: ``PROD-1`` changing must not touch ``PROD-2``.

        One global counter made every write conflict with every other write, so
        two clones editing *unrelated* cards got 409s in the app and a one-line
        merge conflict on line 1 — the failure this rewrite exists to kill.
        """
        before = {"board": self.store.read_board().token(),
                  "PROD-1": self.store.read_card("PROD-1").token(),
                  "PROD-2": self.store.read_card("PROD-2").token()}
        doc = self.card("PROD-1")
        doc["title"] = "Renamed while the other agent worked"
        written = self.save(doc)
        self.assertNotEqual(written.token(), before["PROD-1"],
                            "the edit did not even move its own file")
        self.assertEqual({"board": self.store.read_board().token(),
                          "PROD-2": self.store.read_card("PROD-2").token()},
                         {key: before[key] for key in ("board", "PROD-2")},
                         "a card write touched more than its own file")

    def test_a_transaction_with_no_tokens_is_not_refused(self) -> None:
        """An agent editing files directly has no token to offer.

        Requiring one would make the CLI useless — and the honest fallback is
        "last writer wins on the file it just read", not "nobody may write".
        """
        tx = api.Tx(self.store)
        card = tx.card("PROD-1")
        card["title"] = "written without a token"
        tx.dirty(card)
        tx.flush()
        self.assertIn("written without a token",
                      self.store.card_path("PROD-1").read_text(encoding="utf-8"))


class TestQuietWrites(Base):
    """Lease traffic must be invisible when it changes nothing."""

    def test_writing_identical_bytes_touches_nothing(self) -> None:
        """A retried claim or a redundant save must not move an mtime.

        Without the byte compare, every agent's no-op ``renew`` rewrites a card,
        dirties the working tree and puts a phantom one-line change in a human's
        ``git status`` — with N agents that is the noise that makes people stop
        reading the board at all. Nanoseconds, because a same-second rewrite of
        the same size would look unchanged to a coarser clock.
        """
        path = self.store.card_path("PROD-1")
        before_bytes = path.read_bytes()
        stat = path.stat()
        written = self.save(self.card("PROD-1"))
        self.assertEqual(path.read_bytes(), before_bytes)
        self.assertEqual((written.mtime, written.size),
                         (stat.st_mtime_ns, stat.st_size))
        self.assertEqual(written.token(), f"{stat.st_mtime_ns:x}-{stat.st_size:x}")

    def test_a_renewal_changes_exactly_one_line_and_nothing_else(self) -> None:
        """``claim.expires`` is the only byte a renewal may rewrite.

        A save that reflowed the card — re-quoted dates, re-indented lists —
        turns a lease bump into a 40-line diff a reviewer has to read, and hides
        which field actually moved.
        """
        repo = self.scratch_repo()
        self.bump("PROD-1", expires=datetime.datetime(2099, 6, 1, 12, 0, 0))
        numstat = git(repo, "diff", "--numstat")
        self.assertEqual(numstat, "1\t1\t.agentboard/cards/PROD-1.yaml",
                         f"a renewal rewrote more than one line:\n{numstat}")
        changed = [line for line in git(repo, "diff", "-U0").splitlines()
                   if line[:1] in "+-" and not line.startswith(("+++", "---"))]
        self.assertEqual(len(changed), 2, changed)
        self.assertTrue(all("expires" in line for line in changed), changed)

    def test_a_no_op_save_leaves_a_real_repo_clean(self) -> None:
        """The end-to-end version of the byte compare, in real git.

        This is the guarantee the plan checks by hand ("claim twice, ``git
        status`` stays clean"). If it regresses, every agent's retry becomes a
        commit and a human cannot tell a real change from churn.
        """
        self.scratch_repo()
        self.assertEqual(self.porcelain(), [])
        with store.locked(self.root):
            pass                          # even a read-mostly request takes the lock
        self.save(self.card("PROD-1"))
        self.save(self.card("PROD-2"))
        self.assertEqual(self.porcelain(), [],
                         "a write that changed nothing dirtied the working tree")

    def test_the_lock_and_the_scratch_are_the_two_ignored_names(self) -> None:
        """``.gitignore`` must narrow to exactly ``.lock`` and ``.tmp-*.yaml``.

        Widen it back to ``.agentboard*`` and the board stops being tracked: the
        repo looks clean while the coordination state lives on one laptop, and
        every clone that arrives believes every card is free. Narrow it too far
        and the lock becomes an untracked change an agent sweeps into a commit
        of somebody else's work.
        """
        repo = self.scratch_repo()
        with store.locked(repo / store.Store.DIRNAME):
            pass
        scratch = repo / store.Store.DIRNAME / f"{store.TEMP_PREFIX}x{store.TEMP_SUFFIX}"
        scratch.write_text("scratch\n", encoding="utf-8")
        self.addCleanup(scratch.unlink, True)
        self.assertEqual(self.porcelain(), [], "tracked board state or scratch showed up")
        self.assertEqual(set(self.porcelain("--ignored=matching")),
                         {f"!! .agentboard/{store.LOCK_NAME}",
                          f"!! .agentboard/{store.TEMP_PREFIX}x{store.TEMP_SUFFIX}"})
        self.assertEqual(sorted(p.name for p in (repo / store.Store.DIRNAME).iterdir()
                                if "lock" in p.name.lower()),
                         [store.LOCK_NAME], f"expected {store.LOCK_NAME!r} only")
        for path, ignore in ((f".agentboard/{store.LOCK_NAME}", True),
                             (f".agentboard/{store.TEMP_PREFIX}x{store.TEMP_SUFFIX}", True),
                             (f".agentboard/{store.BOARD_FILE}", False),
                             (".agentboard/cards/PROD-1.yaml", False)):
            done = subprocess.run(["git", "check-ignore", "-q", path], cwd=str(repo),
                                  capture_output=True, text=True,
                                  env={**os.environ, **GIT_ENV})
            self.assertEqual(done.returncode == 0, ignore, f"wrong rule for {path}")

    def test_a_real_edit_shows_as_one_modified_card(self) -> None:
        """The counterpart to the quiet-write tests: a real change must be visible.

        Deduplication that swallowed a genuine edit is worse than noise — the
        human commits nothing believing the board travelled.
        """
        self.scratch_repo()
        self.bump("PROD-1", expires=datetime.datetime(2099, 6, 1, 12, 0, 0))
        # ``git()`` strips each line, so compare paths, not the XY columns
        changed = [line.split()[-1] for line in self.porcelain()]
        self.assertEqual(changed, [".agentboard/cards/PROD-1.yaml"],
                         f"a real edit did not show as exactly one dirty card: {changed}")

class TestLocking(Base):
    """``.agentboard/.lock`` serialises the writes tokens cannot order."""

    @needs_flock
    def test_the_lock_is_reentrant_on_the_one_thread(self) -> None:
        """The server reads a snapshot and writes inside it; helpers nest.

        ``flock`` belongs to the open file description, so a second ``open`` of
        the same file in the same process is refused by the lock the first one
        holds. Without re-entrancy every HTTP write deadlocks against itself and
        ``serve`` stops answering at the first claim; releasing early is the
        mirror-image bug, where the outer plan loses its protection.
        """
        self.assertFalse(self.held())
        with store.locked(self.root):
            self.assertTrue(self.held())
            with store.locked(self.root):                # would hang if not reentrant
                doc = self.store.read_card("PROD-1").doc
                doc["title"] = "written under a nested lock"
                self.store.write_card(doc, board=self.store.read_board().doc)
            self.assertTrue(self.held(),
                            "the inner scope released a lock it did not take")
        self.assertFalse(self.held())
        self.assertIn("nested lock",
                      self.store.card_path("PROD-1").read_text(encoding="utf-8"))

    @needs_flock
    def test_another_thread_cannot_take_a_held_lock(self) -> None:
        """Two writer threads must take turns, not interleave one plan.

        Tokens catch a lost update after the fact; they cannot stop two
        transactions minting the same card id, because both read the same
        highest id. Without this, two agents get ``PROD-7`` and the duplicate
        only surfaces as a merge accident.
        """
        outcome: list[str] = []

        def contender() -> None:
            try:
                with store.locked(self.root, timeout=0.2):
                    outcome.append("acquired")
            except store.StoreError:
                outcome.append("refused")

        with store.locked(self.root):
            thread = threading.Thread(target=contender)
            thread.start()
            thread.join(10)
        self.assertEqual(outcome, ["refused"],
                         "a second thread wrote while the store was locked")

    @needs_flock
    def test_a_held_lock_times_out_rather_than_hanging(self) -> None:
        """A wedged process must cost a command, not a terminal.

        Without a deadline ``agentboard claim`` inside an agent's shell blocks
        until a human kills it, and the agent cannot tell it is not merely
        waiting on the board. The refusal has to name the store, since one repo
        can hold more than one, and it has to be ``StoreError`` so the CLI maps
        it to a usage failure rather than exit 3 ("someone else holds this"),
        which would tell the agent to go pick different work instead.
        """
        child = subprocess.Popen(
            [sys.executable, "-c",
             "import fcntl, os, sys, time\n"
             "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)\n"
             "fcntl.flock(fd, fcntl.LOCK_EX)\n"
             "print('HELD', flush=True)\n"
             "time.sleep(60)\n",
             str(self.root / store.LOCK_NAME)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(child.kill)
        self.assertEqual(child.stdout.readline().strip(), "HELD")
        started = time.monotonic()
        with self.assertRaises(store.StoreError) as caught:
            with store.locked(self.root, timeout=0.25):
                self.fail("took a lock another process holds")
        elapsed = time.monotonic() - started
        self.assertIn(str(self.root), str(caught.exception))
        self.assertGreaterEqual(elapsed, 0.2, "gave up before the caller's timeout")
        self.assertLess(elapsed, 5.0, f"ignored the timeout (waited {elapsed:.1f}s)")
        child.kill()
        child.wait()
        with store.locked(self.root, timeout=1.0):
            pass                                   # and it is reclaimable after

    @needs_flock
    def test_the_lock_is_released_when_the_body_raises(self) -> None:
        """Every write path can throw mid-flight — a denied claim, a bad payload.

        A leaked lock holds the store until the process dies, so the next
        agent's ``claim`` times out on a board nobody is using, which looks
        exactly like a contention bug in the board. The retry is asked of a
        *second thread* because ``locked`` is re-entrant per thread: the same
        thread would take its own leftover for free and report no problem.
        """
        for boom in (ValidationError(["refused"]), KeyError("gone")):
            with self.assertRaises(type(boom)):
                with store.locked(self.root, timeout=1.0):
                    raise boom

        acquired: list[bool] = []

        def retry() -> None:
            try:
                with store.locked(self.root, timeout=0.2):
                    acquired.append(True)
            except store.StoreError:
                acquired.append(False)

        thread = threading.Thread(target=retry)
        thread.start()
        thread.join(10)
        self.assertEqual(acquired, [True],
                         "a raised body left the store locked")
        with store.locked(self.root, timeout=1.0) as handle:
            self.assertEqual(handle.path, self.root / store.LOCK_NAME)

    @needs_flock
    def test_a_long_run_of_locks_leaks_no_descriptors(self) -> None:
        """``serve`` takes this lock on every request, forever.

        An unclosed descriptor grows until the process cannot open a card file,
        and the failure lands hours later, far from anything that explains it.
        """
        listing = Path("/dev/fd")
        if not listing.is_dir():
            self.skipTest("no descriptor listing on this platform")
        base = len(os.listdir(str(listing)))
        for _ in range(200):
            with store.locked(self.root):
                pass
        self.assertEqual(len(os.listdir(str(listing))), base)


class TestAtomicReplace(Base):
    """A crash must not leave a truncated card, and a refusal must not leave dirt."""

    def test_a_successful_write_leaves_no_scratch(self) -> None:
        """Temp-then-replace is the anti-truncation guarantee, minus the litter.

        Surviving temp files get read as cards by anything that scans the
        directory, and a board full of ``.tmp-`` debris makes a person distrust
        every claim chip on screen.
        """
        doc = self.card("PROD-1")
        doc["title"] = "Rewritten cleanly"
        self.save(doc)
        self.assertEqual(self.temps(), [])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         [store.LOCK_NAME, store.ARCHIVE_DIR, store.BOARD_FILE,
                          store.CARDS_DIR])

    def test_a_failed_write_leaves_the_previous_bytes_and_no_scratch(self) -> None:
        """A full disk or a read-only directory is normal on someone's laptop.

        If a failed save truncated the card, the board loses an agent's claim
        and every path it was holding — the exact state that puts two agents in
        one file. ``os.replace`` is atomic, so the old card must still be there,
        byte for byte.
        """
        path = self.store.card_path("PROD-1")
        original = path.read_bytes()
        os.chmod(self.store.cards_dir, 0o500)          # readable, not writable
        self.addCleanup(os.chmod, self.store.cards_dir, 0o700)
        doc = self.card("PROD-1")
        doc["title"] = "never landed"
        with self.assertRaises(store.StoreError):
            self.save(doc)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.temps(), [])

    def test_a_rejected_document_never_reaches_the_file(self) -> None:
        """``write_card`` validates before it serialises.

        Half-writing a payload the validator disagrees with leaves a card that
        fails on every later read — including ``check`` — so the board stops
        opening at all and the only fix is hand-editing YAML under pressure.
        """
        path = self.store.card_path("PROD-1")
        original = path.read_bytes()
        doc = self.card("PROD-1")
        doc["column"] = "nowhere"
        with self.assertRaises(ValidationError) as caught:
            self.save(doc)
        self.assertTrue(any("column" in e for e in caught.exception.errors),
                        caught.exception.errors)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.temps(), [])
        self.assertEqual(self.store.read_card("PROD-1").result, None)

    def test_an_unserialisable_document_leaves_the_board_file_alone(self) -> None:
        """The guard beneath ``write_card``, for documents that skip validation.

        ``write_board`` deliberately trusts its caller, so a value ruamel cannot
        represent must fail *before* anything is replaced: otherwise a truncated
        ``board.yaml`` survives and every column definition is gone.
        """
        original = self.board_path.read_bytes()
        with self.assertRaises(store.StoreError):
            store._write_file(self.board_path, {"columns": [object()]})
        self.assertEqual(self.board_path.read_bytes(), original)
        self.assertEqual(self.temps(), [])
        self.assertEqual(self.store.read_board().doc["title"], "Personal")

    def test_a_card_for_an_unknown_board_key_is_refused(self) -> None:
        """``OPS-1`` has no directory on a PROD-only board that makes sense.

        Writing it anyway parks a card no ``ready`` queue will ever serve, and
        it resurfaces as a "no board has the key" read error nobody can trace
        back to the typo that made it.
        """
        doc = self.card("PROD-1")
        doc["id"] = "OPS-1"
        with self.assertRaises(ValidationError) as caught:
            self.save(doc)
        self.assertIn("'OPS'", " ".join(caught.exception.errors))
        self.assertEqual(list(self.store.cards_dir.glob("OPS-*")), [])
        self.assertEqual(list(self.store.archive_dir.glob("OPS-*")), [])
        self.assertEqual(self.temps(), [])

    def test_many_threads_write_their_own_card(self) -> None:
        """The per-file split under load: 12 writers, 12 files, nothing lost.

        A lost write here means an agent's claim vanishes and a second agent
        starts editing files somebody is already in.
        """
        def worker(index: int) -> None:
            handle = store.Store(self.dir)             # each thread is a process
            handle.write_card(
                {"id": f"PROD-{100 + index}", "title": f"card {index}",
                 "column": "backlog", "order": float(1024 * (index + 1)),
                 "archived": False},
                board=handle.read_board().doc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual([t.name for t in threads if t.is_alive()], [])
        written = [f for f in self.store.read_all().cards
                   if int(str(f.doc.get("id")).split("-")[1]) >= 100]
        self.assertEqual(len(written), 12)
        self.assertEqual([f.result for f in written if f.result], [])
        self.assertEqual({f.doc["title"] for f in written},
                         {f"card {i}" for i in range(12)},
                         "one writer's card overwrote another's")
        self.assertEqual(self.temps(), [])


class TestArchiveAndRemove(Base):
    """Retiring a card is a rename, so git sees a move instead of a rewrite."""

    def test_flipping_archived_moves_it_without_a_second_call(self) -> None:
        """The directory is part of the document's meaning, not a side effect.

        Two code paths for "archive" is how a card ends up in ``archive/``
        saying ``archived: false`` inside it — a card the browser renders as
        live from a directory nothing else reads.
        """
        doc = self.card("PROD-1")
        doc["archived"] = True
        written = self.save(doc)
        self.assertEqual(written.path, self.store.card_path("PROD-1", archived=True))
        self.assertFalse(self.store.card_path("PROD-1").exists())
        doc["archived"] = False
        back = self.save(doc)
        self.assertEqual(back.path, self.store.card_path("PROD-1"))
        self.assertEqual(sorted(p.name for p in self.store.archive_dir.iterdir()),
                         ["PROD-2.yaml"])

    def test_a_move_that_would_clobber_is_refused_and_changes_nothing(self) -> None:
        """Restoring a retired card must not overwrite a live card of the same name.

        The mirror of the archive direction pinned in ``tests/test_api.py``; same
        ``write_card`` guard. Two clones that each renamed a different card to one
        basename merge into two files with one id, and restoring either is a
        plain write from here. Overwriting destroys one card outright; refusing
        keeps both so a human can see which survived.
        """
        retired = self.card("PROD-2")                     # read from archive/
        retired["archived"] = False                       # ... and restored
        squatter = self.store.card_path("PROD-2")
        squatter.write_text(
            "id: PROD-9\ntitle: A live card that happens to share the name\n"
            "column: doing\npriority: low\narchived: false\n", encoding="utf-8")
        held = squatter.read_bytes()

        with self.assertRaises(ValidationError) as caught:
            self.save(retired)                            # restores into cards/
        self.assertIn("already holds", " ".join(caught.exception.errors))
        self.assertEqual(squatter.read_bytes(), held,
                         "restoring one card deleted another")
        self.assertTrue(self.store.card_path("PROD-2", archived=True).is_file(),
                        "the refused restore deleted the archived card")
        self.assertEqual(self.temps(), [])

    def test_removing_a_card_clears_whichever_copy_exists(self) -> None:
        """A delete that missed the archived copy leaves a card ``ready`` will
        offer again, still carrying a claim from an agent who finished it.
        """
        self.store.remove_card("PROD-2")
        self.assertIsNone(self.store.locate("PROD-2"))
        self.assertEqual(self.store.card_ids(), ["PROD-1"])
        for call in (lambda: self.store.remove_card("PROD-2"),
                     lambda: self.store.read_card("PROD-2")):
            with self.assertRaises(store.NotFound):
                call()

    def test_the_archive_directory_appears_on_demand(self) -> None:
        """A store may have no archived cards — and so no ``archive/`` at all.

        Requiring the directory makes a fresh clone's first archive die on a
        ``FileNotFoundError`` instead of doing the one thing that was asked.
        """
        shutil.rmtree(self.store.archive_dir)
        self.assertEqual(self.store.read_all().archived, [])
        self.assertEqual(self.store.card_ids(), ["PROD-1"])
        doc = self.card("PROD-1")
        doc["archived"] = True
        written = self.save(doc)
        self.assertTrue(written.path.is_file())
        self.assertEqual(self.store.locate("PROD-1"), written.path)


class TestCreation(Base):
    """``init`` is the first thing an agent runs; what it writes must load."""

    def test_new_board_validates_at_the_current_version(self) -> None:
        """A skeleton the validator rejects makes ``init`` exit 2 on an empty repo.

        The worst possible failure: nothing on disk to inspect, and the message
        blames a schema problem in a file the tool just wrote itself.
        """
        doc = store.new_board("Website")
        self.assertEqual(model.validate(doc), [])
        self.assertEqual(doc["version"], store.model.VERSION)
        self.assertEqual(store.model.VERSION, 2)
        self.assertEqual([c["id"] for c in model.columns(doc["boards"][0])],
                         ["backlog", "ready", "doing", "done"])
        self.assertTrue(model.is_done_column(doc["boards"][0], "done"))
        self.assertNotIn("rev", doc, "the deleted global counter must not come back")
        self.assertNotIn("cards", doc["boards"][0], "cards are files, not a section")

    def test_the_key_argument_becomes_the_card_id_prefix(self) -> None:
        """``init --key fin`` is a promise about every id the board will mint.

        If the stored key and the derived prefix disagreed, each card would land
        in a file whose prefix no board owns, and every one of them would fail
        on read with "no board has the key".
        """
        doc = store.new_board("Payments API", key="fin")
        board = doc["boards"][0]
        self.assertEqual(board["key"], "FIN")
        self.assertTrue(model.KEY_RE.match(board["key"]))
        self.assertEqual(model.validate(doc), [])
        self.assertEqual(model.next_card_id(board, []), "FIN-1")
        self.assertIs(model.board_for_card(doc, "FIN-7"), board)
        self.assertIsNone(model.board_for_card(doc, "WEB-1"))

    def test_a_difficult_title_still_gets_a_legal_key_and_slug(self) -> None:
        """``init "Q3 — Goals"`` has no obvious slug, and ``###`` has none at all.

        A key that fails ``KEY_RE`` or a board id that fails ``SLUG_RE`` makes
        the board unusable the instant it is written, so every fallback has to
        land inside the same grammar the validator enforces.
        """
        for title in ("Q3 — Goals", "###", "—", "2 fast", "Ünïcode Board"):
            doc = store.new_board(title)
            self.assertEqual(model.validate(doc), [], title)
            self.assertTrue(model.KEY_RE.match(doc["boards"][0]["key"]),
                            f"{title!r} -> {doc['boards'][0]['key']!r}")
            self.assertTrue(model.SLUG_RE.match(doc["boards"][0]["id"]), title)

    def test_create_writes_a_store_that_reads_back(self) -> None:
        """``init`` then ``check`` on an empty repo has to pass.

        ``cards/`` and ``archive/`` existing-but-empty is the difference between
        "board with no cards" and a store whose ``ready`` query crashes on a
        missing directory.
        """
        fresh = store.Store(self.dir / "greenfield")
        self.assertFalse(fresh.exists())
        snapshot = fresh.create("Payments API", key="fin")
        self.assertTrue(fresh.exists())
        self.assertTrue(fresh.cards_dir.is_dir() and fresh.archive_dir.is_dir())
        self.assertEqual(list(fresh.cards_dir.iterdir()), [])
        self.assertEqual(model.validate(snapshot.doc), [])
        self.assertEqual(fresh.read_board().result, None)
        self.assertEqual(fresh.card_ids(), [])
        self.assertEqual(fresh.read_all().cards, [])
        self.assertEqual(snapshot.doc["boards"][0]["key"], "FIN")
        self.assertEqual(self.temps(fresh), [])

    def test_create_ignores_its_own_scratch_inside_the_store(self) -> None:
        """A board lands in somebody else's repo, so it has to keep itself clean.

        ``.lock`` and ``.tmp-*.yaml`` are written beside ``board.yaml`` and are
        not board state. A repo that never heard of agentboard has no rule for
        them, so ``git add .`` sweeps a 0-byte lock into the commit, and the
        board's own Commit button then refuses: its ``git add`` stages the lock,
        and the foreign-file guard correctly sees a file it did not put there.
        The store ships the rule inside ``.agentboard/`` rather than editing the
        host repo's root ``.gitignore`` unasked, so the fix travels with the
        board and cannot be lost by a clone.
        """
        fresh = store.Store(self.dir / "tenant")
        fresh.create("Imported", key="IMP")
        repo = self.dir / "tenant-repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "t@example.com")
        git(repo, "config", "commit.gpgsign", "false")
        shutil.copytree(fresh.root, repo / store.Store.DIRNAME)

        ignore = repo / store.Store.DIRNAME / store.IGNORE_NAME
        self.assertTrue(ignore.is_file(), "init wrote no ignore rule of its own")
        with store.locked(repo / store.Store.DIRNAME):
            pass
        scratch = repo / store.Store.DIRNAME / f"{store.TEMP_PREFIX}z{store.TEMP_SUFFIX}"
        scratch.write_text("scratch\n", encoding="utf-8")
        self.addCleanup(scratch.unlink, True)

        staged = git(repo, "add", "-A", "--", store.Store.DIRNAME)
        listed = git(repo, "diff", "--cached", "--name-only").split()
        self.assertIn(f"{store.Store.DIRNAME}/{store.BOARD_FILE}", listed)
        self.assertNotIn(f"{store.Store.DIRNAME}/{store.LOCK_NAME}", listed,
                         f"`git add` staged the lock; a commit records it forever: {staged}")
        self.assertNotIn(f"{store.Store.DIRNAME}/{store.TEMP_PREFIX}z{store.TEMP_SUFFIX}", listed)
        for path, ignored in ((f"{store.Store.DIRNAME}/{store.LOCK_NAME}", True),
                              (f"{store.Store.DIRNAME}/{store.BOARD_FILE}", False),
                              (f"{store.Store.DIRNAME}/cards/x.yaml", False)):
            done = subprocess.run(["git", "check-ignore", "-q", path], cwd=str(repo),
                                  capture_output=True, text=True,
                                  env={**os.environ, **GIT_ENV})
            self.assertEqual(done.returncode == 0, ignored, f"wrong rule for {path}")

    def test_an_existing_ignore_rule_of_ones_own_is_left_alone(self) -> None:
        """A narrowed rule the owner wrote is theirs to keep.

        Rewriting it on every init would silently undo a deliberate choice —
        un-ignoring the scratch to debug a killed save, say — and it would put a
        diff in a working tree that changed nothing else.
        """
        fresh = store.Store(self.dir / "owner")
        fresh.root.mkdir(parents=True)
        (fresh.root / store.IGNORE_NAME).write_text("# keep the lock visible\n",
                                                    encoding="utf-8")
        before = (fresh.root / store.IGNORE_NAME).read_bytes()
        fresh.create("Owned", key="OWN")
        self.assertEqual((fresh.root / store.IGNORE_NAME).read_bytes(), before)
        self.assertFalse(fresh.write_ignore(), "a second init claimed to write again")
        self.assertEqual((fresh.root / store.IGNORE_NAME).read_bytes(), before)

    def test_create_refuses_to_overwrite_an_existing_board(self) -> None:
        """An overwritten board is unrecoverable; "already initialised" is not.

        Without this, a stale script or an agent that lost track of its working
        directory replaces a team's board with four empty columns and a green
        exit code.
        """
        original = self.board_path.read_bytes()
        with self.assertRaises(store.StoreError) as caught:
            self.store.create("Personal", key="PER")
        self.assertIn("not overwriting", str(caught.exception))
        self.assertEqual(self.board_path.read_bytes(), original)
        self.assertEqual(self.card("PROD-1")["claim"]["agent"], "ada")

    def test_the_shipped_example_round_trips_byte_for_byte(self) -> None:
        """The fixture the README tells people to ``cp -r`` must already be valid.

        If it drifts from the schema, every demo, every walkthrough and the
        first board a new user ever sees fails ``check`` — and the first save
        would rewrite all fifteen files, which is the diff that tells people the
        tool does not respect their files.
        """
        demo = REPO / "examples" / "demo" / store.Store.DIRNAME
        if not (demo / store.BOARD_FILE).is_file():
            self.skipTest("example store not installed")
        scratch = self.dir / "demo"
        shutil.copytree(demo, scratch / store.Store.DIRNAME)
        sample = store.Store(scratch)
        snapshot = sample.read_all()
        self.assertEqual([f.result for f in snapshot.all_cards() if f.result], [])
        self.assertEqual(api.Tx(sample).problems(), [])
        before = {p: p.read_bytes() for p in sorted(sample.root.rglob("*.yaml"))}
        sample.write_board(snapshot.doc)
        for handle in snapshot.all_cards():
            sample.write_card(handle.doc, board=snapshot.doc)
        after = {p: p.read_bytes() for p in sorted(sample.root.rglob("*.yaml"))}
        self.assertEqual(before, after, "saving the example store reflowed its files")
        self.assertEqual(self.temps(sample), [])


class TestYamlFidelity(Base):
    """ruamel is the only dependency, so a save must be a no-op to the eye."""

    def test_comments_quotes_and_block_scalars_survive_a_save(self) -> None:
        """These files are human artefacts reviewed in a pull request.

        A save that dropped the comment above a column or re-quoted a colour
        turns a one-field edit into a diff nobody will approve — and deletes the
        notes explaining why the board is shaped the way it is.
        """
        self.save(self.card("PROD-1"))
        text = self.board_path.read_text(encoding="utf-8")
        self.assertIn("# Personal boards -- comments are mine", text)
        self.assertIn('color: "#2f6f4f"', text)
        card = self.store.card_path("PROD-1").read_text(encoding="utf-8")
        self.assertIn("# trailing note", card)
        self.assertIn("  line one\n  line two", card)
        self.assertIn("due: 2026-12-01", card)
        self.assertNotIn("due: '2026-12-01'", card)
        self.assertEqual(self.card("PROD-1")["desc"], "line one\nline two\n")
        self.assertEqual(self.card("PROD-1")["due"], datetime.date(2026, 12, 1))

    def test_a_reshuffled_card_is_saved_back_in_the_authors_order(self) -> None:
        """Field order a person chose is kept; only new keys get placed.

        Re-sorting a card on save makes every diff a whole-file rewrite, so the
        one-line lease bump a reviewer needs becomes forty lines and the real
        change is the one they skim past.
        """
        path = self.write_file("PROD-3",
                              "title: Out of order\n"
                              "id: PROD-3\n"
                              "column: backlog\n"
                              "archived: false\n")
        doc = self.card("PROD-3")
        model.set_field(doc, "card", "priority", "urgent")
        self.save(doc)
        self.assertEqual(path.read_text(encoding="utf-8"),
                         "title: Out of order\n"
                         "id: PROD-3\n"
                         "column: backlog\n"
                         "priority: urgent\n"
                         "archived: false\n")

    def test_a_hand_written_fraction_survives_an_unrelated_edit(self) -> None:
        """``created: ...T10:00:00.123456`` has to come back unchanged.

        Reformatting one stamp rewrites a line the edit never touched, so the
        diff claims a change to a card's history that nobody made.
        """
        path = self.write_file("PROD-4",
                              "id: PROD-4\ntitle: Fractional\n"
                              "created: 2026-11-14T10:00:00.123456\n"
                              "column: backlog\n")
        doc = self.card("PROD-4")
        doc["title"] = "Fractional, edited"
        self.save(doc)
        self.assertIn("created: 2026-11-14T10:00:00.123456",
                      path.read_text(encoding="utf-8"))

    def test_a_file_asks_for_its_own_indent(self) -> None:
        r"""Two spellings are in the wild: ``people:\n- id:`` and the indented one.

        ruamel's default pushes every dash in by two columns, so the first
        assignee edit reflows the whole file — which is how a one-line change
        becomes a merge conflict in a board two people were editing.
        """
        self.assertEqual(store._indent_for(self.board_path), (4, 2))
        compact = ("# compact on purpose\n"
                   "version: 2\n"
                   "title: Flat\n"
                   "people:\n"
                   "- id: ada\n"
                   "  name: Ada\n"
                   "labels: []\n"
                   "boards:\n"
                   "- id: web\n"
                   "  key: WEB\n"
                   "  title: Website\n"
                   "  columns:\n"
                   "  - id: todo\n"
                   "    name: To do\n")
        flat = store.Store(self.dir / "flat")
        flat.cards_dir.mkdir(parents=True)
        flat.archive_dir.mkdir(parents=True)
        flat.board_path.write_text(compact, encoding="utf-8")
        self.assertEqual(store._indent_for(flat.board_path), (2, 0))
        handle = flat.read_board()
        handle.doc["title"] = "Flat, renamed"
        flat.write_board(handle.doc)
        after = flat.board_path.read_text(encoding="utf-8")
        changed = [(a, b) for a, b in zip(compact.splitlines(), after.splitlines())
                   if a != b]
        self.assertEqual(changed, [("title: Flat", "title: Flat, renamed")],
                         f"a save reflowed the file: {changed}")
        self.assertIn("\n- id: ada\n", after)
        self.assertIn("# compact on purpose", after)

    def test_the_indent_probe_survives_an_unreadable_file(self) -> None:
        """A brand-new card has no file to learn the layout from.

        If reading it raised, the first write of that card — the claim that just
        succeeded — would fail with an ``OSError`` about a file that is allowed
        to be missing.
        """
        self.assertEqual(store._indent_for(self.root / "nope.yaml"), (4, 2))
        hidden = self.dir / "hidden"
        hidden.mkdir()
        os.chmod(hidden, 0o000)
        self.addCleanup(os.chmod, hidden, 0o700)
        self.assertEqual(store._indent_for(hidden / "board.yaml"), (4, 2))

    def test_timestamps_are_written_in_iso_by_both_spellings(self) -> None:
        """A space where the ``T`` belongs is unparsable by ``Date.parse``.

        The browser would render every countdown ``Invalid Date``, so a live
        lease looks expired and an agent takes work somebody is holding. Values
        read back from a file arrive as ``TimeStamp`` — a ``datetime`` subclass —
        so it needs its own registration or those keep the old spelling.
        """
        stamp = datetime.datetime(2026, 11, 22, 19, 58, 3)
        doc = {"created": stamp, "due": datetime.date(2026, 12, 1),
               "people": [{"id": "ada", "name": "Ada"}]}
        self.assertEqual(store._dumps(doc, 4, 2),
                         b"created: 2026-11-22T19:58:03\n"
                         b"due: 2026-12-01\n"
                         b"people:\n  - id: ada\n    name: Ada\n")
        self.assertEqual(store._dumps(doc, 2, 0),
                         b"created: 2026-11-22T19:58:03\n"
                         b"due: 2026-12-01\n"
                         b"people:\n- id: ada\n  name: Ada\n")
        reloaded = store._yaml().load("t: 2026-11-22T19:58:03\n")["t"]
        buffer = StringIO()
        store._yaml().dump({"t": reloaded}, buffer)
        self.assertEqual(buffer.getvalue(), "t: 2026-11-22T19:58:03\n")

    def test_the_iso_representer_does_not_leak_into_another_yaml_instance(self) -> None:
        """``add_representer`` writes through to a class dict every YAML shares.

        Registering that way would restyle every other ruamel user in the
        process — including a library the board is embedded in — and the bug
        would surface in somebody else's output entirely.
        """
        store._yaml()                                   # the patcher
        stranger = YAML()
        buffer = StringIO()
        stranger.dump({"t": datetime.datetime(2026, 11, 22, 19, 58, 3)}, buffer)
        self.assertIn("2026-11-22 19:58:03", buffer.getvalue())

    def test_a_quoted_value_keeps_its_quotes(self) -> None:
        """``preserve_quotes`` is why a colour and a quoted title do not drift.

        Un-quoting ``color: "#2f6f4f"`` is a byte change on every line of the
        file: CSS accepts both spellings, so the save looks free while it
        dirties every file it touches.
        """
        self.save(self.card("PROD-1"))
        self.assertIn('color: "#2f6f4f"', self.board_path.read_text(encoding="utf-8"))
        self.assertIn("title: Fix the 'deploy' timeout",
                      self.store.card_path("PROD-1").read_text(encoding="utf-8"))

    def test_an_older_version_is_refused_and_names_its_way_out(self) -> None:
        """There is deliberately no reader for a version this build did not write.

        A silently-misread board is worse than a loud refusal: an older file
        keeps ``rev:`` and the cards inside the board, so a permissive reader
        would show an empty board and let an agent take work another agent
        already claimed.
        """
        self.board_path.write_text("version: 1\ntitle: Old\nrev: 4\nboards: []\n",
                                   encoding="utf-8")
        with self.assertRaises(ValidationError) as caught:
            self.store.read_board()
        message = " ".join(caught.exception.errors)
        self.assertIn("file says 1", message)
        self.assertIn("nothing was read", message)

    def test_a_broken_board_is_a_store_error_not_a_traceback(self) -> None:
        """The server polls the store on a timer.

        A raw ruamel exception escapes the request handler and the poll starts
        failing with a 500, so the board blanks instead of telling the human
        that their last edit in vim was malformed.
        """
        self.board_path.write_text("boards: [oops\n  :::\n", encoding="utf-8")
        with self.assertRaises(store.StoreError) as caught:
            self.store.read_board()
        self.assertIn("not valid YAML", str(caught.exception))
        with self.assertRaises(store.StoreError):
            self.store.read_all()

    def test_a_board_with_a_cards_key_is_told_where_cards_live_now(self) -> None:
        """``cards:`` is what a v1 file, or a hand edit, reaches for.

        The closed key set already refuses it, but without the pointer a person
        deletes the section to make the error go away — and that deletes their
        cards. The message has to name the new home instead.
        """
        self.board_path.write_text(
            BOARD.replace("    next_card: 3",
                          "    cards:\n"
                          "      - id: PROD-1\n"
                          "        title: In the wrong place\n"
                          "        column: backlog\n"
                          "    next_card: 3"),
            encoding="utf-8")
        with self.assertRaises(ValidationError) as caught:
            self.store.read_board()
        self.assertTrue(any("cards/<ID>.yaml" in e for e in caught.exception.errors),
                        caught.exception.errors)

    def test_a_dangling_symlink_is_not_silently_an_empty_board(self) -> None:
        """A backup tool that moved the target leaves a broken link behind.

        ``is_file`` is false for a dangling link, so reading it as "no board
        yet" lets the next write drop an empty skeleton where a real board was —
        and ``init`` would then report that it had created one.
        """
        real = self.dir / "real-board.yaml"
        real.write_text(BOARD, encoding="utf-8")
        elsewhere = self.dir / "linked"
        (elsewhere / store.Store.DIRNAME).mkdir(parents=True)
        link = elsewhere / store.Store.DIRNAME / store.BOARD_FILE
        link.symlink_to(real)
        handle = store.Store(elsewhere)
        self.assertTrue(handle.exists())
        self.assertEqual(handle.read_board().doc["title"], "Personal")
        real.unlink()
        self.assertFalse(handle.exists())
        with self.assertRaises(store.StoreError):
            handle.read_board()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
