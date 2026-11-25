"""HTTP surface, path confinement, read-only mode and the git commit button.

The store here is real: a git repo in a temp directory holding a real
``.yamlboard/`` (``board.yaml`` plus ``cards/<ID>.yaml``), written through the
same ``api`` the CLI uses and committed with the same git the operator runs.
Nothing is mocked except the tests whose whole subject is a failure mode (a
vanished file, a leaked traceback).

Two contracts dominate this file, and both are about what the browser is told:

* **A stale write reloads; a denied claim does not.** 409 carries two opposite
  meanings and the client can only tell them apart from the body, so the
  difference is asserted here rather than trusted.
* **The payload is per file.** ``tokens`` maps ``"board"`` and every card id to
  that one file's identity. One global stamp would make every lease renewal in
  the repo look like a change to the board a human is editing.
"""

from __future__ import annotations

import contextlib
import datetime
import errno
import http.client
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from test_gitops import GIT_ENV
from yamlboard import __version__, api, gitops, model, store
from yamlboard.server import (INTERNAL_ERROR, Application, is_loopback, local_hosts,
                             make_server, resolve_static)

#: The two per-machine artefacts a real repo ignores (README, "Private boards").
#: Without them the Commit button stages the advisory flock, and every other
#: clone conflicts with a file that means nothing.
GITIGNORE = "**/.yamlboard/.lock\n**/.yamlboard/.tmp-*.yaml\n"


class Stderr:
    """A stand-in ``sys.stderr`` that keeps everything written to it.

    The server logs from its own handler thread, so a ``redirect_stderr`` block
    would be over before the line lands; this one is installed for the whole
    test and restored by cleanup.
    """

    def __init__(self) -> None:
        self.buffer = io.StringIO()

    def write(self, text: str) -> int:
        return self.buffer.write(text)

    def flush(self) -> None:
        pass

    def text(self) -> str:
        return self.buffer.getvalue()


def capture_stderr(test: unittest.TestCase) -> Stderr:
    sink = Stderr()
    test.addCleanup(setattr, sys, "stderr", sys.stderr)
    sys.stderr = sink
    return sink


def git(cwd: Path, *args: str) -> str:
    """git with the ambient config switched off; fail loudly."""
    done = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=True, env={**os.environ, **GIT_ENV})
    return done.stdout.strip()


def make_repo(where: Path) -> store.Store:
    """A committed repo holding a real ``.yamlboard/`` with one card, ``WEB-1``.

    The commit matters: the tests below assert commit counts and "nothing to
    commit", which only mean something against a tree that started clean.
    """
    repo = where / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / ".gitignore").write_text(GITIGNORE, encoding="utf-8")

    # ``Store`` wants the repo root (or the .yamlboard directory) - never a
    # card file: there is no single data file to hand it any more.
    board = store.Store(repo)
    board.create("Test", key="WEB")
    board_id = str(board.read_board().doc["boards"][0]["id"])
    for ident, name in (("ada", "Ada"), ("grigory", "Grigory")):
        api.apply(board, {"op": "person.create", "args": {"id": ident, "name": name}})
    api.apply(board, {"op": "card.create",
                      "args": {"board": board_id, "title": "Card 1"}})
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed")
    return board


def serve(app: Application, test: unittest.TestCase):
    """A live server on an ephemeral loopback port, shut down with the test."""
    server = make_server(app, "127.0.0.1", 0)
    test.addCleanup(server.server_close)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    test.addCleanup(server.shutdown)
    return server, server.server_address[1]


class Http(unittest.TestCase):
    """A live server on an ephemeral port, against a fresh temp git repo per test.

    Isolation is load-bearing: these tests stage, commit and hand-edit files,
    and a shared repo would make ``rev-list --count`` and the "nothing to
    commit" branch depend on test order. ``.resolve()`` because macOS names the
    temp dir through a ``/var`` -> ``/private/var`` symlink that ``gitops``
    resolves and a test comparing roots would otherwise see both spellings.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name).resolve()
        self.store = make_repo(self.dir)
        self.repo = self.store.root.parent
        self.card = self.store.card_path("WEB-1")
        self.board_id = str(self.store.read_board().doc["boards"][0]["id"])

        self.app = Application(self.store.root)
        self.server, self.port = serve(self.app, self)

    # -- helpers ---------------------------------------------------------- #

    def get(self, path: str):
        """Return (status, body) without raising on 4xx, like post() does."""
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
                body = r.read()
                kind = r.headers.get("Content-Type", "")
                return r.status, (json.loads(body) if "json" in kind else body)
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw)
            except json.JSONDecodeError:
                return error.code, raw

    def post(self, path: str, payload: dict):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def state(self) -> dict:
        return self.get("/api/state")[1]

    def tokens(self) -> dict:
        """The per-file identities a well-behaved client would be holding."""
        return self.state()["tokens"]

    def apply(self, op: str, *, tokens: dict | None = None, **args):
        """Post one op with the tokens for the document we just read."""
        return self.post("/api/apply", {"op": op, "args": args,
                                        "tokens": self.tokens() if tokens is None else tokens})

    def cards(self) -> list[dict]:
        return self.state()["doc"]["boards"][0]["cards"]

    def commit_count(self) -> int:
        return int(git(self.repo, "rev-list", "--count", "HEAD"))

    def head_files(self) -> list[str]:
        return sorted(git(self.repo, "show", "--name-only", "--format=", "HEAD").splitlines())

    def porcelain(self) -> str:
        return git(self.repo, "status", "--porcelain=v2", "--untracked-files=all")

    def write_card(self, card: Path, old: str, new: str) -> None:
        """The hand edit in Vim: same file, different bytes, no ``rev`` to bump."""
        card.write_text(card.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


class TestBasics(Http):
    def test_index_and_assets_served(self) -> None:
        """A missing asset is a board that renders unstyled, silently.

        ``package-data`` ships ``static/*``; an empty file still answers 200, so
        size is the only thing that says the real file is in the wheel.
        """
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"<title>yamlboard", body.lower())
        for asset in ("/app.js", "/styles.css"):
            status, body = self.get(asset)
            self.assertEqual(status, 200, asset)
            self.assertGreater(len(body), 1000, asset)

    def test_state_is_the_document_plus_per_file_tokens(self) -> None:
        """The shape the browser paints from, key for key.

        Each key is a promise the UI relies on: ``tokens`` keyed by file (so a
        card write must not move ``board``), ``ready``/``agents``/``conflicts``
        derived server-side so the CLI and the tab cannot disagree about who
        holds what, and no ``rev`` anywhere - one global counter is exactly what
        made two unrelated cards collide, in the app and again in git on line 1.
        """
        payload = self.state()
        self.assertEqual(
            sorted(payload),
            ["agents", "archived", "conflicts", "doc", "file", "git", "read_only",
             "ready", "schema_version", "tokens"])
        self.assertEqual(payload["schema_version"], model.VERSION)
        self.assertEqual(payload["doc"]["boards"][0]["id"], self.board_id)
        self.assertEqual([c["id"] for c in payload["doc"]["boards"][0]["cards"]], ["WEB-1"])
        self.assertEqual(sorted(payload["tokens"]), ["WEB-1", "board"])
        self.assertEqual(payload["ready"], ["WEB-1"])
        self.assertEqual(payload["agents"], {})
        self.assertEqual(payload["conflicts"], [])
        self.assertEqual(payload["archived"], [])
        self.assertEqual(payload["file"], str(self.store.root))
        self.assertFalse(payload["read_only"])
        self.assertTrue(payload["git"]["is_repo"])
        # real git agrees the tree is clean; whether the server *says* so is the
        # subject of TestGitStatus, not of this shape test
        self.assertEqual(self.porcelain(), "")
        self.assertNotIn("rev", payload["doc"])
        self.assertNotIn("rev", self.store.board_path.read_text(encoding="utf-8"))

    def test_health_names_the_build_and_the_schema(self) -> None:
        """A monitoring script asks the port which build is up.

        Before ``version`` the only answer was the ``Server:`` header, which the
        same file deliberately strips of its version - so the two together left
        no way to check that a deploy is the build you shipped.
        """
        status, payload = self.get("/api/health")
        self.assertEqual((status, payload["ok"]), (200, True))
        # a script asking "which build is up" must not have to parse a header
        self.assertEqual(payload["version"], __version__)
        self.assertEqual(payload["schema_version"], model.VERSION)

    def test_unknown_routes_are_404_json(self) -> None:
        """A typo in a URL is a 404 with JSON, not HTML and not a stale 200.

        app.js branches on ``data.ok``; an HTML body makes it report "unreadable
        response" and the human never learns the endpoint simply is not there.
        ``/api/reload`` in particular was deleted, so nothing may answer it.
        """
        self.assertEqual(self.get("/api/nope")[0], 404)
        self.assertEqual(self.post("/api/nope", {})[0], 404)
        # /api/reload was dead code: the client polls and 409-reloads instead
        self.assertEqual(self.post("/api/reload", {})[0], 404)

    def test_bad_json_body_is_400(self) -> None:
        """A malformed body is refused before anything further is parsed.

        Half-read JSON reaching an op would either 500 - a fake server bug - or,
        if the parser were lenient, apply an op nobody sent.
        """
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/apply", data=b"{not json",
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 400)

    def test_a_version_1_store_is_refused_and_names_migrate(self) -> None:
        """Pointing the server at an un-migrated board must not serve a board.

        There is no v1 reader, on purpose: reading the old file would show zero
        cards and zero leases, and an agent that trusts an empty ``ready`` list
        edits files another agent is holding. The refusal has to name the fix.
        """
        self.store.board_path.write_text(
            "version: 1\ntitle: Old\nrev: 7\npeople: []\nlabels: []\nboards: []\n",
            encoding="utf-8")
        status, body = self.get("/api/state")
        self.assertEqual(status, 422)
        self.assertFalse(body["ok"])
        self.assertTrue(any("migrate" in e for e in body["errors"]), body["errors"])
        self.assertTrue(any("rev" in e for e in body["errors"]), body["errors"])


class TestWrites(Http):
    def test_apply_writes_one_file_and_returns_the_document(self) -> None:
        """One card, one path: the whole reason the store was split.

        A create that rewrote every card file would put two agents' unrelated
        work on the same git lines - the collision the rewrite exists to remove.
        """
        before = self.tokens()
        status, out = self.apply("card.create", board=self.board_id, title="Via HTTP")
        self.assertEqual(status, 200)
        self.assertEqual(out["result"]["id"], "WEB-2")
        self.assertEqual([c["id"] for c in out["state"]["doc"]["boards"][0]["cards"]],
                         ["WEB-1", "WEB-2"])
        self.assertTrue(self.store.card_path("WEB-2").is_file())
        self.assertIn("Via HTTP", self.store.card_path("WEB-2").read_text(encoding="utf-8"))
        # WEB-1 was never touched, so its token still identifies those bytes
        self.assertEqual(out["state"]["tokens"]["WEB-1"], before["WEB-1"])
        # and the server's own next read agrees without a restart
        self.assertEqual([c["id"] for c in self.cards()], ["WEB-1", "WEB-2"])

    def test_a_claim_moves_exactly_one_file_and_no_updated_stamp(self) -> None:
        """Lease traffic must be quiet, or the board lies in two directions.

        If a claim moved every card's token, the reload toast would fire for
        cards nobody opened every time any agent renewed; if it moved the card's
        ``updated:``, a human's ``git log`` and the list-view sort would claim
        somebody edited a card that nobody edited.
        """
        self.apply("card.create", board=self.board_id, title="Second")
        before = self.tokens()
        updated_before = {c["id"]: c.get("updated") for c in self.cards()}

        status, out = self.apply("claim.create", agent="ada", paths=["src/api/**"], id="WEB-1")
        self.assertEqual(status, 200, out)
        after = out["state"]["tokens"]
        self.assertEqual([key for key in after if after[key] != before.get(key)], ["WEB-1"])
        self.assertEqual(after["board"], before["board"])
        self.assertEqual(after["WEB-2"], before["WEB-2"])
        self.assertEqual({c["id"]: c.get("updated") for c in self.cards()}, updated_before)

        painted = {c["id"]: c for c in out["state"]["doc"]["boards"][0]["cards"]}
        self.assertEqual(painted["WEB-1"]["state"], "taken")
        self.assertTrue(painted["WEB-1"]["live"])
        self.assertEqual(painted["WEB-2"]["state"], "ready")
        self.assertEqual(out["state"]["ready"], ["WEB-2"])
        self.assertEqual(out["state"]["agents"]["ada"]["cards"], ["WEB-1"])

    def test_a_write_based_on_stale_bytes_is_409_with_the_fresh_document(self) -> None:
        """The headline promise: your hand edit is not overwritten.

        The browser is looking at a render up to three seconds old. A write
        carrying a token that no longer matches must come back 409 with
        ``conflict: true, claim_denied: false`` and the fresh document, or the
        tab keeps painting a card that no longer exists and its next click
        clobbers the hand edit that beat it.

        The reply is built by :meth:`Application.payload`, reached through
        ``self.app``: an ``AttributeError`` raised inside this ``except`` would
        escape ``_route`` unseen by any other handler and close the connection
        with no response at all -- a stale write silently vanishing, which is
        the exact bug the per-file token was invented to catch.
        """
        seen = self.tokens()
        self.write_card(self.card, "title: Card 1", "title: My hand edit")

        try:
            status, out = self.apply("card.update", tokens=seen,
                                    id="WEB-1", patch={"desc": "from a stale tab"})
        except (urllib.error.URLError, ConnectionError) as exc:
            self.fail(f"the server hung up instead of answering with the reload: {exc!r}")

        self.assertEqual(status, 409, out)
        self.assertTrue(out["conflict"])
        self.assertFalse(out["claim_denied"])
        self.assertEqual(out["paths"], ["WEB-1"])
        self.assertEqual([c["title"] for c in out["state"]["doc"]["boards"][0]["cards"]],
                         ["My hand edit"])
        self.assertIsNotNone(out["state"]["git"], "a missing git paints 'git off'")

    def test_a_stale_write_never_lands_on_disk_whatever_it_answers(self) -> None:
        """The data half of the promise, which must hold even if the reply is broken.

        A token the server cannot match has to mean "write nothing". Whatever the
        transport does with the answer (see the test above), the hand edit in the
        file survives and the tab is unstuck by its next read - if the write had
        landed, no amount of reloading would bring the human's edit back.
        """
        seen = self.tokens()
        self.write_card(self.card, "title: Card 1", "title: My hand edit")
        with contextlib.suppress(urllib.error.URLError, ConnectionError):
            # the reply is broken (defect above); the question here is only
            # whether the stale write reached the file
            self.apply("card.update", tokens=seen, id="WEB-1",
                       patch={"desc": "stale tab"})

        text = self.card.read_text(encoding="utf-8")
        self.assertIn("title: My hand edit", text)
        self.assertNotIn("stale tab", text)
        # the next read hands out tokens that do work, so the client is unstuck
        fresh = self.tokens()
        self.assertNotEqual(fresh["WEB-1"], seen["WEB-1"])
        self.assertEqual(self.apply("card.update", tokens=fresh, id="WEB-1",
                                   patch={"desc": "found it"})[0], 200)
        self.assertEqual(self.cards()[0]["desc"], "found it")

    def test_a_stale_board_token_reports_the_board_not_the_card(self) -> None:
        """Which file moved is the difference between a retry and a hunt.

        ``paths`` names the stale files - ``["board"]`` here, after a hand edit
        to ``board.yaml`` - so the CLI can say "the board changed" and the UI
        knows a card-scoped retry would fail again. The fresh document rides
        along on the same reply as
        :meth:`test_a_write_based_on_stale_bytes_is_409_with_the_fresh_document`.
        """
        seen = self.tokens()
        self.write_card(self.store.board_path, "title: Test", "title: Renamed in vim")
        try:
            status, out = self.apply("card.create", tokens=seen,
                                     board=self.board_id, title="stale tab")
        except (urllib.error.URLError, ConnectionError) as exc:
            self.fail(f"no reload document was sent: {exc!r}")
        self.assertEqual(status, 409)
        self.assertEqual(out["paths"], ["board"])
        self.assertEqual(out["state"]["doc"]["title"], "Renamed in vim")
        self.assertNotIn("stale tab", json.dumps(out["state"]["doc"]))

    def test_a_refused_claim_carries_no_document(self) -> None:
        """409 has two meanings, and this one must NOT be a reload.

        Nothing moved under the caller - somebody else holds the card. Sending a
        document would make app.js adopt it and wipe the note the human is
        typing into the modal, which is the bug the module banner calls out. The
        answer is ``claim_denied: true`` plus the lines an agent acts on.
        """
        self.assertEqual(self.apply("claim.create", agent="ada",
                                   paths=["src/api/**"], id="WEB-1")[0], 200)
        status, out = self.apply("claim.create", agent="grigory", id="WEB-1")
        self.assertEqual(status, 409)
        self.assertTrue(out["conflict"])
        self.assertTrue(out["claim_denied"])
        self.assertNotIn("state", out, "a document here wipes the open modal")
        self.assertNotIn("paths", out)
        self.assertIn("ada", out["error"], out)
        self.assertEqual(out["detail"]["holder"], "ada")
        self.assertTrue(out["lines"][-1].startswith("next: "), out["lines"])
        # and the denial cost the winner nothing: ada still holds it
        self.assertEqual(self.state()["agents"]["ada"]["cards"], ["WEB-1"])

    def test_overlapping_paths_on_a_different_card_are_denied_too(self) -> None:
        """Mutual exclusion is about paths, not about one card's row.

        Two agents who each grab a different card and both declare the API layer
        are the exact collision the board exists to prevent, and the containment
        direction (``src/api/**`` vs ``src/api/routes.py``) is the one a
        symmetric-only glob test misses.
        """
        self.apply("card.create", board=self.board_id, title="Second")
        self.assertEqual(self.apply("claim.create", agent="ada",
                                   paths=["src/api/**"], id="WEB-1")[0], 200)
        status, out = self.apply("claim.create", agent="grigory", id="WEB-2",
                                paths=["src/api/routes.py"])
        self.assertEqual(status, 409)
        self.assertTrue(out["claim_denied"])
        self.assertIn("src/api/routes.py", " ".join(out["lines"]), out["lines"])
        self.assertIn("ada", " ".join(out["lines"]), out["lines"])
        self.assertNotIn("state", out)
        # work that does not overlap is still there for the loser
        status, out = self.apply("claim.create", agent="grigory", id="WEB-2",
                                paths=["docs/**"])
        self.assertEqual(status, 200, out)
        self.assertEqual(out["result"]["agent"], "grigory")

    def test_an_expired_claim_is_free_and_records_preempted(self) -> None:
        """Expiry is the clock, not a daemon - and the takeover names the victim.

        Nothing runs to release a lease, so a card whose ``expires`` has passed
        must simply stop being claimed on the next read. If it stayed "taken"
        until something wrote it, an agent that died mid-task would lock its
        card for the rest of the session.
        """
        self.assertEqual(self.apply("claim.create", agent="ada", id="WEB-1")[0], 200)
        real_now = model.now
        model.now = lambda: real_now() + datetime.timedelta(minutes=31)
        try:
            payload = self.state()
            painted = {c["id"]: c for c in payload["doc"]["boards"][0]["cards"]}
            self.assertEqual(painted["WEB-1"]["state"], "ready")
            self.assertFalse(painted["WEB-1"]["live"])
            self.assertEqual(payload["ready"], ["WEB-1"])
            self.assertEqual(payload["agents"], {})

            status, out = self.apply("claim.create", agent="grigory", id="WEB-1")
            self.assertEqual(status, 200, out)
            self.assertEqual(out["result"]["preempted"], "ada")
        finally:
            model.now = real_now
        self.assertIn("preempted: ada", self.card.read_text(encoding="utf-8"))

    def test_a_change_that_would_make_the_board_invalid_is_422_and_writes_nothing(self) -> None:
        """Half a write is worse than a refused one.

        Validation runs over the whole store before the first byte moves, so a
        payload that would leave any card unreadable cannot leave a file on disk
        that every later read - including ``yamlboard check`` - then fails on.
        """
        self.apply("card.update", id="WEB-1", patch={"title": "Real"})
        text = self.card.read_text(encoding="utf-8")
        porcelain = self.porcelain()

        status, out = self.apply("card.update", id="WEB-1", patch={"title": "   "})
        self.assertEqual(status, 422)
        self.assertTrue(out["errors"])
        self.assertTrue(any("title" in e for e in out["errors"]), out["errors"])
        self.assertEqual(self.card.read_text(encoding="utf-8"), text)
        self.assertEqual(self.cards()[0]["title"], "Real")
        self.assertEqual(self.porcelain(), porcelain, "the refused op changed the tree")

    def test_an_unreasonable_request_is_400(self) -> None:
        """Every way a body can be malformed, and none of them a 500 or a 409.

        A caller that gets 409 for a malformed body reloads and retries forever;
        one that gets 500 files a server bug instead of fixing its own typo.
        """
        good = {"op": "card.create", "args": {"board": self.board_id, "title": "x"}}
        cases = [
            ({"op": "card.create", "args": good["args"]}, "tokens are required"),
            ({**good, "tokens": "abc"}, "tokens must be the mapping"),
            ({**good, "tokens": {"board": 17}}, "must be a string"),
            ({**good, "op": "nope.nope", "tokens": self.tokens()}, "unknown op"),
            ({**good, "args": "nope", "tokens": self.tokens()}, "args must be a mapping"),
            ({"op": "card.create", "args": {"title": "x"},
              "tokens": self.tokens()}, "board is required"),
        ]
        for body, needle in cases:
            status, out = self.post("/api/apply", body)
            self.assertEqual(status, 400, (body, out))
            self.assertIn(needle, out["error"], (body, out))
        self.assertEqual(self.porcelain(), "", "a refused request must not write")

    def test_unknown_ids_are_404(self) -> None:
        """A missing id is 404 so a script can tell "gone" from "nonsense" (400)."""
        self.assertEqual(self.apply("card.update", id="WEB-999", patch={})[0], 404)
        self.assertEqual(self.apply("card.move", id="WEB-1", column="nope")[0], 404)
        self.assertEqual(self.apply("card.create", board="nope", title="x")[0], 404)

    def test_an_external_edit_is_picked_up_without_a_restart(self) -> None:
        """Every poll re-reads the store, because a cache would call a free card taken.

        A lease expires on the clock, not on a write, so nothing dirties the
        file at the moment the claim stops meaning anything - only a fresh read
        can say so. Both edits here are the interesting kinds: one changes the
        file's length, one does not, which is why ``mtime`` and not ``size`` is
        the invalidation trigger.
        """
        self.write_card(self.card, "title: Card 1", "title: Edited in vim")
        first = self.state()
        self.assertEqual(first["doc"]["boards"][0]["cards"][0]["title"], "Edited in vim")
        before = first["tokens"]["WEB-1"]

        self.write_card(self.card, "Edited in vim", "Edited-in-vi")
        second = self.state()
        self.assertEqual(second["doc"]["boards"][0]["cards"][0]["title"], "Edited-in-vi")
        self.assertNotEqual(second["tokens"]["WEB-1"], before)


class TestReadOnlyMode(unittest.TestCase):
    """``--read-only`` is the shape a shared or public board takes, so it holds.

    The refusal has to be a 400 with a reason, not a crash and not a silent
    success: a toolbar that believes it saved a card nobody has is worse than
    one that admits it cannot write.
    """

    def boot(self, **kwargs):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        board = make_repo(tmp)
        app = Application(board.root, **kwargs)
        server, port = serve(app, self)
        return board, server, port

    def request(self, port: int, path: str, payload: dict):
        http_request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(http_request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_writes_refused_reads_fine(self) -> None:
        """``--read-only`` must not be able to write, and must still serve.

        This is the flag that makes a board safe to expose on a shared machine.
        Hiding the button is not enough: a stale tab, or anything else that can
        POST, would edit the file the operator said was frozen. And if reads
        broke too, the board would be unusable instead of read-only.
        """
        board, _server, port = self.boot(read_only=True)
        text = board.card_path("WEB-1").read_text(encoding="utf-8")

        status, out = self.request(port, "/api/apply",
                                  {"op": "card.create", "args": {"board": "test",
                                                                "title": "nope"},
                                   "tokens": {}})
        self.assertEqual(status, 400)
        self.assertIn("read-only", out["error"])

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=5) as ok:
            payload = json.loads(ok.read())
        self.assertEqual(ok.status, 200)
        self.assertTrue(payload["read_only"])
        self.assertEqual([c["id"] for c in payload["doc"]["boards"][0]["cards"]], ["WEB-1"])
        self.assertEqual(board.card_path("WEB-1").read_text(encoding="utf-8"), text)
        self.assertEqual(git(board.root.parent, "status", "--porcelain"), "")

    def test_commit_refused_before_git_is_even_consulted(self) -> None:
        """A read-only server must not be able to move the git index either."""
        board, _server, port = self.boot(read_only=True)
        status, out = self.request(port, "/api/commit", {"message": "should not happen"})
        self.assertEqual(status, 400)
        self.assertIn("read-only", out["error"])
        self.assertEqual(git(board.root.parent, "rev-list", "--count", "HEAD"), "1")

    def test_commit_on_a_no_git_server_is_400(self) -> None:
        """``--no-git`` hides the button; the endpoint still refuses politely.

        The URL outlives the flag (bookmarks, scripts). Without the guard a
        missing repository surfaces as a 500 quoting git, which reads as a
        broken install rather than "this server does not do commits".
        """
        _board, _server, port = self.boot(allow_git=False)
        status, out = self.request(port, "/api/commit", {"message": "should not happen"})
        self.assertEqual(status, 400)
        self.assertIn("disabled", out["error"])


class TestPublicBindGuard(Http):
    def test_writable_public_bind_refused_until_asked(self) -> None:
        """There is no auth, so a LAN bind is a decision, never a default.

        Serving a writable board on every interface lets any machine on the
        network rewrite the card files and drive the Commit button.
        """
        with self.assertRaises(RuntimeError):
            make_server(self.app, "0.0.0.0", 0)
        # read-only public serving is the sanctioned way, as is --allow-public
        ro = Application(self.store.root, read_only=True)
        server = make_server(ro, "0.0.0.0", 0)
        server.server_close()
        server = make_server(self.app, "0.0.0.0", 0, allow_public=True)
        server.server_close()


class TestLoopbackDefinition(unittest.TestCase):
    """One definition of "only this machine" - the guard cannot be skipped.

    The bind check, the banner and the Host/Origin allowlist used to be two
    different rules: an alias like ``127.0.0.2`` was loopback to the CLI and
    public to the server, which meant a writable bind was refused outright and a
    public one ran with no DNS-rebinding protection at all.
    """

    def test_every_loopback_spelling_is_loopback(self) -> None:
        """The banner, the bind check and the Host allowlist must agree.

        They were two rules once: an alias like ``127.0.0.2`` was loopback to the
        CLI and public to the server, so a writable alias bind was refused
        outright. Any host one of them calls local has to be local to all three,
        or it runs without the other's CSRF protection.
        """
        for host in ("127.0.0.1", "127.0.0.2", "127.5.5.5", "localhost", "::1"):
            self.assertTrue(is_loopback(host), host)

    def test_public_hosts_are_not_loopback(self) -> None:
        """The other half of that agreement: what is not loopback stays public.

        A host wrongly called loopback gets the writable-bind check waived and
        no Host guard - a LAN-reachable board with no auth, serving writes.
        """
        for host in ("0.0.0.0", "::", "*", "", "192.168.1.5", "evil.example"):
            self.assertFalse(is_loopback(host), host)

    def test_alias_bind_keeps_its_own_host_allowed(self) -> None:
        """Your own tab must not be refused by the guard meant to protect it.

        The allowlist used to be three literals; binding an alias matched none of
        them, so the request fell through as if the user had asked for a public
        port - which is to say, with no guard at all.
        """
        allowed = local_hosts("127.0.0.2")
        self.assertIsNotNone(allowed)
        self.assertIn("127.0.0.2", allowed)
        self.assertIn("localhost", allowed)

    def test_wildcard_bind_gets_no_host_guard(self) -> None:
        """A wildcard bind answers everywhere, so guarding by name is wrong.

        Returning a set here would reject the very hostnames ``--allow-public``
        exists to admit; ``None`` is the honest answer, and the loopback check is
        what made the user ask for the exposure in the first place.
        """
        self.assertIsNone(local_hosts("0.0.0.0"))
        self.assertIsNone(local_hosts("::"))

    def test_writable_alias_bind_is_not_refused_as_public(self) -> None:
        """The CLI must reach the socket layer, not die at the bind decision.

        Dying here printed a speech about auth for a command that was perfectly
        safe, and reported exit 2 for what is really the machine having no such
        alias - a problem with the host, not with the arguments.
        """
        with tempfile.TemporaryDirectory() as raw:
            board = make_repo(Path(raw))
            app = Application(board.root)
            # the CLI used to die here; it must now reach the socket layer
            try:
                server = make_server(app, "127.0.0.2", 0)
            except OSError:               # pragma: no cover - CI without aliases
                self.skipTest("127.0.0.2 is not bindable on this host")
            self.addCleanup(server.server_close)
            self.assertIn("127.0.0.2", server.RequestHandlerClass.allowed_hosts)


class TestForeignRequests(Http):
    """The loopback bind is the only auth, so the Host/Origin of a request matters.

    A page on evil.example that resolves the victim's hostname to 127.0.0.1
    (DNS rebinding), or any page blind-POSTing text/plain to this port, has to
    be refused - otherwise it can rewrite the card files and drive the Commit
    button with no preflight and no readable response.
    """

    def raw(self, method: str, path: str, headers: dict, body: bytes = b""):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        payload = response.read()
        try:
            return response.status, json.loads(payload)
        except json.JSONDecodeError:      # pragma: no cover - body is always JSON
            return response.status, payload

    def test_rebinding_host_is_refused(self) -> None:
        """A page that resolves its own hostname to 127.0.0.1 gets nothing.

        There is no auth here - loopback is the whole story - so a DNS-rebinding
        page would otherwise read the board and rewrite every card file with no
        preflight and no cross-origin check to stop it.
        """
        status, _ = self.raw("GET", "/api/state", {"Host": "evil.example.com"})
        self.assertEqual(status, 403)
        status, _ = self.raw("POST", "/api/apply",
                            {"Host": "evil.example.com",
                             "Content-Type": "application/json"},
                            json.dumps({"op": "document.update", "tokens": self.tokens(),
                                        "args": {"title": "pwned"}}).encode())
        self.assertEqual(status, 403)
        self.assertEqual(self.store.read_board().doc["title"], "Test")   # nothing written

    def test_cross_origin_post_is_refused_even_with_a_correct_host(self) -> None:
        """The commit button is reachable by any page the victim visits.

        A blind ``text/plain`` POST carries no CORS preflight and a browser
        allows it with the right ``Host:``; only the Origin check stands between
        some other site and a commit of the victim's board under their identity.
        """
        self.apply("card.create", board=self.board_id, title="uncommitted work")
        status, _ = self.raw("POST", "/api/commit",
                            {"Host": f"127.0.0.1:{self.port}",
                             "Origin": "https://evil.example",
                             "Content-Type": "text/plain"},
                            b'{"message": "pwned via CSRF"}')
        self.assertEqual(status, 403)
        self.assertEqual(self.commit_count(), 1, "the attacker must not get a commit")
        self.assertEqual(git(self.repo, "log", "-1", "--format=%s"), "seed")
        self.assertIn("WEB-2", self.porcelain(), "the card is still uncommitted")

    def test_ordinary_and_headless_requests_still_work(self) -> None:
        """The guard that blocks the attack must not block the user.

        Curl and scripts send ``Host: 127.0.0.1:port`` and no Origin; the app's
        own fetch sends a same-origin Origin. Refusing either is a board nobody
        can save.
        """
        self.assertEqual(self.raw("GET", "/api/health",
                                  {"Host": f"localhost:{self.port}"})[0], 200)
        self.assertEqual(self.raw("GET", "/api/health", {"Host": "127.0.0.1"})[0], 200)
        status, out = self.raw("POST", "/api/apply",
                              {"Host": f"127.0.0.1:{self.port}",
                               "Origin": f"http://127.0.0.1:{self.port}",
                               "Content-Type": "application/json"},
                              json.dumps({"op": "document.update", "tokens": self.tokens(),
                                          "args": {"title": "Renamed"}}).encode())
        self.assertEqual((status, out["result"]), (200, {"title": "Renamed"}))
        self.assertEqual(self.store.read_board().doc["title"], "Renamed")


class TestPathConfinement(unittest.TestCase):
    def test_traversal_and_outside_paths_refused(self) -> None:
        """The static handler must not turn a URL into an arbitrary file read.

        The server binds loopback, and any page the developer visits can reach
        it - so a readable ``~/.ssh/id_rsa`` is a full-disk oracle on the machine
        that runs the board.
        """
        root = Path(__file__).resolve().parents[1] / "src" / "yamlboard" / "static"
        self.assertTrue(root.is_dir(), "static dir must exist for this suite to mean anything")
        self.assertIsNotNone(resolve_static("/"))
        self.assertIsNotNone(resolve_static("styles.css"))
        for attack in ("/../etc/passwd", "/..%2f..%2fetc/passwd", "//etc/passwd",
                      "/../../README.md", "/" + "\x00index.html"):
            self.assertIsNone(resolve_static(attack), attack)

    def test_symlink_escape_refused(self) -> None:
        """``resolve()`` closes traversal, not symlink attacks.

        A link dropped inside the static directory (by a build step, or a
        package-data glob) would otherwise hand out whatever it points at
        outside it.
        """
        import yamlboard.server as server_module
        # the link lives in a temp static dir, never the shipped package: a crash
        # between symlink and unlink would leave it in src/yamlboard/static/,
        # where package-data globs it into the wheel
        sandbox = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, sandbox, True)
        (sandbox / "index.html").write_text("hi", encoding="utf-8")
        self.addCleanup(setattr, server_module, "STATIC_DIR", server_module.STATIC_DIR)
        server_module.STATIC_DIR = sandbox
        secret = Path(tempfile.mkstemp(suffix=".txt")[1])
        secret.write_text("secret", encoding="utf-8")
        try:
            (sandbox / "zz-test-link.txt").symlink_to(secret)
            self.assertIsNone(resolve_static("/zz-test-link.txt"))
            self.assertIsNotNone(resolve_static("/index.html"))  # sandbox is root now
        finally:
            secret.unlink(missing_ok=True)


class TestGitButton(Http):
    """The only write path that leaves the store, and so the only one that can
    commit somebody else's work."""

    def test_commit_stages_only_the_board(self) -> None:
        """The button ships board files and nothing else.

        Staging the whole tree would ship an agent's half-finished source with a
        human's board edit, and the author of the commit would be whoever
        clicked last.
        """
        self.apply("card.create", board=self.board_id, title="Committed card")
        (self.repo / "unrelated.txt").write_text("do not commit me", encoding="utf-8")
        status, out = self.post("/api/commit", {"message": "yamlboard: add a card"})
        self.assertEqual(status, 200, out)
        self.assertTrue(out["committed"], out)
        self.assertTrue(out["sha"])
        files = self.head_files()
        self.assertIn(".yamlboard/cards/WEB-2.yaml", files)
        self.assertEqual([p for p in files if not p.startswith(".yamlboard/")], [], files)
        again = self.post("/api/commit", {"message": "the same card twice"})
        self.assertEqual((again[0], again[1]["reason"]), (200, "nothing to commit"), again)
        self.assertIn("unrelated.txt", self.porcelain())

    def test_the_flock_and_the_write_scratch_never_get_committed(self) -> None:
        """A ``.lock`` in a commit is a merge conflict in every other clone.

        ``git add`` is deliberately not ``--force``: it honours ``.gitignore``,
        which names the two per-machine artefacts rather than the directory -
        the reason the ignore file could stop naming them wholesale.
        """
        self.apply("card.create", board=self.board_id, title="Written under the lock")
        self.assertTrue((self.store.root / ".lock").is_file(), "no lock, no test")
        (self.store.root / ".tmp-cafebabe.yaml").write_text("interrupted save\n",
                                                            encoding="utf-8")
        status, out = self.post("/api/commit", {"message": "yamlboard: cards only"})
        self.assertEqual(status, 200, out)
        self.assertTrue(out["committed"])
        self.assertEqual([p for p in self.head_files()
                          if "/.lock" in p or "/.tmp-" in p], [], self.head_files())
        # gitignored, not deleted and not staged: still sitting there, untracked
        self.assertIn("! .yamlboard/.tmp-cafebabe.yaml",
                      git(self.repo, "status", "--porcelain=v2", "--ignored=matching", "-uall"))
        self.assertEqual(git(self.repo, "diff", "--cached", "--name-only"), "")

    def test_a_foreign_staged_file_refuses_the_commit(self) -> None:
        """An agent mid-``git add`` must not have its work shipped by the button.

        The refusal names the file, commits nothing, and leaves the index
        alone - unstaging it would destroy the state that proves whose it was.
        """
        self.apply("card.create", board=self.board_id, title="Mine")
        (self.repo / "theirs.py").write_text("half-finished work\n", encoding="utf-8")
        git(self.repo, "add", "theirs.py")

        status, out = self.post("/api/commit", {"message": "mine only"})
        self.assertEqual(status, 400)
        self.assertTrue(out["git_error"])
        self.assertIn("theirs.py", out["error"])
        self.assertEqual(self.commit_count(), 1, "no commit happened")
        self.assertIn("theirs.py",
                      git(self.repo, "diff", "--cached", "--name-only").split())
        self.assertIn("WEB-2", self.porcelain(), "the card is still pending")

    def test_nothing_to_commit_is_not_an_error(self) -> None:
        """A second click is not a fault, or the toolbar cries wolf.

        This is also the answer the loser of a concurrent commit gets, so it has
        to be a 200 with a reason rather than a 400 quoting git.
        """
        self.apply("card.create", board=self.board_id, title="Worth committing")
        first = self.post("/api/commit", {"message": "first"})
        self.assertTrue(first[1]["committed"], first)
        self.assertEqual(self.commit_count(), 2)
        status, out = self.post("/api/commit", {"message": "again"})
        self.assertEqual(status, 200)
        self.assertFalse(out["committed"])
        self.assertEqual(out["reason"], "nothing to commit")
        self.assertEqual(self.commit_count(), 2)

    def test_two_people_committing_at_once_do_not_quarrel(self) -> None:
        """Commit is serialized, so the loser is told "nothing to commit".

        Unsynchronized, concurrent POSTs fought over git's index.lock and the
        losers came back 400 quoting raw git stderr.
        """
        self.apply("card.create", board=self.board_id, title="To commit")
        results: list = []

        def commit() -> None:
            results.append(self.post("/api/commit", {"message": "yamlboard: concurrent"}))

        threads = [threading.Thread(target=commit) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(len(results), 4, results)
        self.assertEqual([status for status, _ in results], [200] * 4, results)
        self.assertEqual(sum(1 for _, out in results if out["committed"]), 1, results)
        self.assertEqual(self.commit_count(), 2)

    def test_an_empty_message_gets_the_tools_own_prefix(self) -> None:
        """``yamlboard: <board>``, not ``boards(boards.yaml): ...``.

        The board is a directory now and one commit names many files; a subject
        naming one of them describes a fifth of the change, and the old prefix
        is a string people grep history for and no longer find.
        """
        self.apply("card.create", board=self.board_id, title="Needs a message")
        status, out = self.post("/api/commit", {})
        self.assertEqual(status, 200, out)
        self.assertTrue(out["committed"])
        subject = git(self.repo, "log", "-1", "--format=%s")
        self.assertTrue(subject.startswith("yamlboard: "), subject)
        self.assertIn("Test", subject)
        self.assertFalse(subject.startswith("boards("), subject)


class TestGitStatus(Http):
    def test_dirty_and_branch_reported(self) -> None:
        """Per-card files, so ``changed`` names the card that moved.

        The toolbar's label and ``conflicts``' dirty-tree check both read this
        list; one entry naming the whole directory would make every card look
        dirty and every claim look like a collision.
        """
        self.apply("card.create", board=self.board_id, title="Uncommitted")
        payload = self.state()["git"]
        self.assertTrue(payload["is_dirty"])
        self.assertEqual(payload["branch"], "main")
        self.assertIsNone(payload["error"])
        self.assertEqual(
            sorted(p for p in payload["changed"] if p.endswith(".yaml")),
            [".yamlboard/board.yaml", ".yamlboard/cards/WEB-2.yaml"])

    def test_a_committed_board_stops_reporting_itself_dirty(self) -> None:
        """The Commit button must go quiet once the board is committed.

        An ignored porcelain record names a path, and only one path means
        anything: the board directory itself, which is what git reports when the
        whole ``.yamlboard/`` was never committed and a ``.gitignore`` hides it.
        The two per-machine artefacts ignored on purpose -- the ``.lock`` every
        read leaves, an interrupted save's ``.tmp-*.yaml`` -- are not the board's
        work, and counting them kept the toolbar saying "uncommitted changes"
        forever on a tree with nothing to save.
        """
        self.apply("card.create", board=self.board_id, title="Committed now")
        committed = self.post("/api/commit", {"message": "yamlboard: committed"})
        self.assertTrue(committed[1]["committed"], committed)
        self.assertEqual(self.porcelain(), "", "git itself calls the tree clean")
        self.assertTrue((self.store.root / ".lock").is_file(), "the artefact under test")

        git_status = self.state()["git"]
        self.assertFalse(git_status["is_dirty"], git_status)
        self.assertEqual(git_status["changed"], [], git_status)

    def test_a_claim_on_a_path_somebody_already_edited_is_reported(self) -> None:
        """The collision git cannot see: two agents, two different files.

        ``src/api/**`` is claimed while ``src/api/routes.py`` sits modified and
        uncommitted. The merge stays clean, so only the report stops the second
        agent overwriting the first -- which is why the spec says it SHALL be
        reported. The dirty list is therefore repo-wide (``gitops.changed_paths``)
        rather than the board directory's path-scoped status, which could name
        nothing outside ``.yamlboard/`` and so made the check unreachable.

        The board's own Commit count stays board-scoped: it has to match what
        ``/api/commit`` will stage, so the two lists are deliberately different.
        """
        (self.repo / "src/api").mkdir(parents=True)
        (self.repo / "src/api/routes.py").write_text("A = 1\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "code")

        self.apply("claim.create", agent="ada", paths=["src/api/**"], id="WEB-1")
        (self.repo / "src/api/routes.py").write_text("A = 2\n", encoding="utf-8")

        # the payload is the promise a human and an agent both read
        conflicts = self.state()["conflicts"]
        self.assertEqual([c["kind"] for c in conflicts], ["dirty"], conflicts)
        self.assertIn("src/api/**", conflicts[0]["patterns"], conflicts[0])
        self.assertIn("src/api/routes.py", conflicts[0]["files"], conflicts[0])
        # reporting only: nothing was reverted, nothing extra was written
        self.assertIn("WEB-1", self.state()["tokens"])
        # and a clean source tree claims nothing
        git(self.repo, "checkout", "--", "src/api/routes.py")
        self.assertEqual([c["kind"] for c in self.state()["conflicts"]], [])

    def test_no_git_mode_hides_status(self) -> None:
        """``--no-git`` is a promise about the index, not just the button.

        A ``git: null`` payload is how the UI learns to hide Commit and stop
        polling; ``dirty_paths() == []`` is the same promise on the conflict
        side, or a claim gets blamed for a working tree this server was told to
        keep out of.
        """
        app = Application(self.store.root, allow_git=False)
        self.assertIsNone(app.payload()["git"])
        self.assertEqual(app.dirty_paths(), [])

    def test_a_store_outside_any_repository_is_reported_not_fatal(self) -> None:
        """A scratch board with no ``.git`` still serves, and says git is off.

        Commit has to answer 400 with git's own reason: a 500 reads as a broken
        install, and the operator goes looking for the wrong problem.
        """
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        board = store.Store(tmp)
        board.create("Loose", key="LS")
        app = Application(board.root)
        payload = app.payload()
        self.assertFalse(payload["git"]["is_repo"])
        self.assertIsNone(payload["git"]["error"])
        with self.assertRaises(gitops.GitError) as caught:
            app.commit({"message": "nowhere to commit"})
        self.assertIn("repository", str(caught.exception))


class TestRequestFraming(Http):
    """Requests that are framed wrongly, or stop halfway.

    ``http.server`` leaves framing to the application, so an answer that leaves
    bytes unread is a second request: the leftover is handed to the parser as
    the next request line. Every one of these has to end the connection.
    """

    def open(self) -> socket.socket:
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(sock.close)
        return sock

    def send_raw(self, raw: bytes) -> bytes:
        """Write a hand-written request and read until the server hangs up."""
        sock = self.open()
        sock.sendall(raw)
        chunks = []
        while True:
            part = sock.recv(4096)
            if not part:
                return b"".join(chunks)
            chunks.append(part)
            self.assertLess(len(chunks), 64, "the server never stopped answering")

    def post_with(self, headers: dict, body: bytes = b"") -> bytes:
        lines = ["POST /api/apply HTTP/1.1", f"Host: 127.0.0.1:{self.port}",
                 "Content-Type: application/json", *[f"{k}: {v}" for k, v in headers.items()]]
        return self.send_raw(("\r\n".join(lines) + "\r\n\r\n").encode() + body)

    def test_a_non_numeric_content_length_is_400_not_a_500(self) -> None:
        """An integer-parsing exception is a 500 the client cannot act on.

        Worse, ``isdigit`` alone accepts non-ASCII digits that ``int`` then
        refuses, so the guard has to be ASCII-explicit or the 500 comes back.
        """
        data = self.post_with({"Content-Length": "abc"})
        self.assertIn(b" 400 ", data.split(b"\r\n", 1)[0], data)
        self.assertIn(b"Content-Length", data)
        # the value is echoed back quoted, and nothing about the interpreter is
        self.assertIn(b"'abc'", data)
        self.assertNotIn(b"ValueError", data)

    def test_a_negative_content_length_is_not_read_as_no_body(self) -> None:
        """``-5`` read as "no body" produced the wrong diagnosis.

        The client was told "tokens are required; reload the page" for a
        framing bug, and would reload forever.
        """
        # "-5" used to mean "no body" and come back as the misleading
        # "tokens are required; reload the page"
        data = self.post_with({"Content-Length": "-5"})
        self.assertIn(b" 400 ", data.split(b"\r\n", 1)[0], data)
        self.assertNotIn(b"tokens are required", data)

    def test_an_oversized_body_is_refused_without_reading_it(self) -> None:
        """Answering without reading the body lets the client smuggle a request.

        The unread bytes are handed to the parser as the next request on a
        keep-alive connection, so a refused 4 MB body could be followed by a 200
        for something the guard never saw.
        """
        # The declared body is never written, so if the server answered and kept
        # the connection open these bytes would be parsed as a fresh request.
        smuggled = b"GET /api/state HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
        data = self.post_with({"Content-Length": str(4 * 1024 * 1024 + 1)}, smuggled)
        self.assertIn(b" 400 ", data.split(b"\r\n", 1)[0], data)
        self.assertEqual(data.count(b"HTTP/1.1 200"), 0, data)

    def test_chunked_transfer_encoding_is_refused_not_misframed(self) -> None:
        """There is no chunk decoder, so the terminator must never be parsed.

        Accepting the header and then meeting ``0`` produced ``400 Bad request
        syntax ('0')`` on the *following* request, blaming the next caller for
        this one's framing.
        """
        # no chunk decoder here: the terminator surfaced as
        # ``400 Bad request syntax ('0')`` on the connection after this one
        chunked = b"3\r\n{}\r\n0\r\n\r\n"
        data = self.post_with({"Content-Length": "0",
                              "Transfer-Encoding": "chunked"}, chunked)
        self.assertIn(b" 400 ", data.split(b"\r\n", 1)[0], data)
        self.assertNotIn(b"Bad request syntax", data)

    def test_a_declared_body_that_never_arrives_closes_the_connection(self) -> None:
        """A half-written request must not hold a thread or print a traceback.

        The idle timeout fires inside ``http.server``'s own readline; if it
        surfaces as a 500 the operator sees a server error for a client that
        simply stopped typing.
        """
        err = capture_stderr(self)
        # 0.2 s stands in for the handler's 15 s idle timeout: same code path,
        # a fifth of a second of test time.
        self.server.RequestHandlerClass = type(
            "ImpatientHandler", (self.server.RequestHandlerClass,), {"timeout": 0.2})
        sock = self.open()
        sock.sendall(f"POST /api/apply HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                     "Content-Type: application/json\r\nContent-Length: 100000\r\n\r\n".encode())
        data = b""
        while True:
            part = sock.recv(4096)
            if not part:
                break               # the server hung up, which is the point
            data += part
        self.assertIn(b" 408 ", data.split(b"\r\n", 1)[0], data)
        self.assertNotIn(b" 500 ", data, data)
        self.assertNotIn("Traceback", err.text(), err.text())


class TestUnroutableRequests(Http):
    """Verbs and headers that used to reach the stdlib's own HTML answers."""

    def raw(self, method: str, path: str):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request(method, path)
        response = conn.getresponse()
        payload = response.read()
        try:
            return response.status, json.loads(payload), response.headers
        except json.JSONDecodeError:
            return response.status, payload, response.headers

    def test_delete_on_an_api_path_is_json_405_with_allow(self) -> None:
        """405 as JSON, with the verbs that do work.

        The stdlib's answer here is ``501`` and ``text/html``, which app.js
        reports as "unreadable response" - a wrong verb looked like a broken
        server rather than a wrong verb.
        """
        status, body, headers = self.raw("DELETE", "/api/apply")
        self.assertEqual(status, 405)
        self.assertFalse(body["ok"])
        self.assertEqual(headers["Allow"], "GET, HEAD, POST")
        self.assertIn("json", headers["Content-Type"])

    def test_write_verbs_on_a_static_path_are_not_501_html(self) -> None:
        """Same on static paths, where the stdlib is even more wrong.

        ``BaseHTTPRequestHandler`` has no ``do_PUT``, so an accidental PUT at
        ``/styles.css`` got the HTML 501 page and a 200-shaped parse failure.
        """
        for method in ("PUT", "DELETE", "PATCH"):
            status, body, headers = self.raw(method, "/styles.css")
            self.assertEqual(status, 405, method)
            self.assertIn("json", headers["Content-Type"], method)
            self.assertEqual(headers["Allow"], "GET, HEAD", method)
            self.assertIsInstance(body, dict, method)      # parsed JSON, not the 501 page
            self.assertFalse(body["ok"], method)

    def test_a_verb_with_no_handler_is_still_json(self) -> None:
        """Every verb the server can receive answers as JSON.

        PUT, DELETE, PATCH, OPTIONS and TRACE all route through ``_route``:
        left to the stdlib they answer 501 with a text/html body, and a proxy
        check or CORS preflight that gets HTML makes app.js report
        "unreadable response" to the one client that asked politely.
        """
        for method in ("OPTIONS", "TRACE"):
            status, body, headers = self.raw(method, "/api/apply")
            self.assertEqual(status, 405, method)
            self.assertIn("json", headers["Content-Type"], method)
            self.assertIsInstance(body, dict, method)
            self.assertEqual(headers["Allow"], "GET, HEAD, POST", method)

    def test_head_answers_with_headers_and_no_body(self) -> None:
        """A monitoring script HEADs the board; a body would corrupt keep-alive."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("HEAD", "/api/state")
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), b"")
        self.assertGreater(int(response.headers["Content-Length"]), 100)

    def test_the_server_header_advertises_nothing_about_the_runtime(self) -> None:
        """``Server: yamlboard`` alone; the stdlib appends the Python version.

        That is a free runtime inventory for anyone probing the port, and it is
        the cheapest thing to stop leaking.
        """
        status, _, headers = self.raw("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Server"], "yamlboard")
        self.assertNotIn("Python/", headers["Server"])


class TestInternalErrors(Http):
    """What a bug looks like to the browser, and what it looks like here."""

    def test_an_unexpected_failure_leaks_neither_field_nor_path(self) -> None:
        """A bug must not read the board to the browser.

        The body of a 500 gets echoed into devtools, screenshots and bug
        reports; field names, card text and absolute paths belong on the
        operator's terminal, which is what the message directs you to.
        """
        err = capture_stderr(self)
        with mock.patch.object(self.app, "payload", side_effect=KeyError("people")):
            status, body = self.get("/api/state")
        self.assertEqual(status, 500)
        self.assertEqual(body, {"ok": False, "error": INTERNAL_ERROR})
        self.assertNotIn("people", json.dumps(body))
        # ...and the diagnosis is on the terminal, which is where the message
        # tells you to look
        self.assertIn("KeyError", err.text())
        self.assertIn("Traceback", err.text())

    def test_a_broken_board_file_is_a_quiet_500_not_a_yaml_dump(self) -> None:
        """The operator's file, not the browser's problem.

        A half-written or non-YAML ``board.yaml`` names absolute paths and
        parser internals that belong on the terminal the board was launched
        from; the browser gets the same opaque line as any other 500.
        """
        err = capture_stderr(self)
        self.store.board_path.write_text("version: 2\n\ttabs: everywhere\n", encoding="utf-8")
        status, body = self.get("/api/state")
        self.assertEqual(status, 500)
        self.assertEqual(body["error"], INTERNAL_ERROR)
        self.assertNotIn(str(self.store.root), json.dumps(body))
        self.assertIn("board.yaml", err.text(), err.text())

    def test_a_static_file_that_vanished_is_404_not_500(self) -> None:
        """A file that vanished between resolve and read is a 404.

        A deploy that replaces ``static/`` while a tab is open would otherwise
        log a server error for every asset and make the board look broken.
        """
        gone = OSError(errno.ENOENT, "no such file")
        with mock.patch.object(Path, "read_bytes", side_effect=gone):
            status, body = self.get("/styles.css")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not found")

    def test_a_static_file_that_cannot_be_read_is_a_quiet_500(self) -> None:
        """Anything that is not "it is not there" stays opaque.

        A permission error names the path and the filesystem; the browser gets
        the generic line and the terminal gets the one that identifies it.
        """
        err = capture_stderr(self)
        denied = OSError(errno.EACCES, "permission denied")
        with mock.patch.object(Path, "read_bytes", side_effect=denied):
            status, body = self.get("/styles.css")
        self.assertEqual(status, 500)
        self.assertEqual(body["error"], INTERNAL_ERROR)
        self.assertNotIn("styles.css", json.dumps(body))
        self.assertIn("styles.css", err.text())

    def test_a_client_that_hung_up_prints_no_traceback(self) -> None:
        """Abandoned connections must not fill the terminal.

        A browser closing a poll early is normal traffic; if each one prints a
        traceback the operator stops reading the log, and the real bug in there
        with them.
        """
        err = capture_stderr(self)

        def swallow(exc: Exception) -> None:
            # ``handle_error`` reads the exception from ``sys.exc_info()`` the
            # way the stdlib's request thread does, so it must be in flight.
            try:
                raise exc
            except Exception:
                self.server.handle_error(mock.MagicMock(), ("127.0.0.1", 43210))

        swallow(ConnectionResetError("client vanished"))
        swallow(BrokenPipeError("client vanished"))
        self.assertNotIn("Traceback", err.text(), err.text())
        # anything else still prints, or real bugs go quiet
        swallow(ValueError("a real bug"))
        self.assertIn("Traceback", err.text())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
