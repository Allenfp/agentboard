"""What the model decides about a board: lease liveness, path claims, order, refusal.

This is the half of v2 with no file in it — "is this claim still a claim", "could
these two globs ever touch the same file", "does this dependency loop close". Those
verdicts are the whole coordination story, and they are all pure functions of one
argument plus a clock, so none of this should need a disk or a socket.

The deliberate weakness: *every* clock here is injected. :func:`model.claim_live`
and :func:`model.ttl_left` take an ``at``, and the one test that pins the default
clock patches :func:`model.now` — a suite running on a loaded machine, or in a
leap second, or in CI at 23:59, cannot change a verdict here. Nothing sleeps.
"""

from __future__ import annotations

import copy
import datetime
import fnmatch
import itertools
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yamlboard import model
from yamlboard.model import ValidationError

#: Every stamp in this file is spelled against T0 and injected, never read off the
#: wall clock, so no test's verdict depends on when the suite ran.
T0 = datetime.datetime(2026, 11, 23, 12, 0, 0)
MINUTE = datetime.timedelta(minutes=1)
HOUR = datetime.timedelta(hours=1)

DOC: dict = {
    "version": 2,
    "title": "Personal",
    "people": [
        {"id": "ada", "name": "Ada", "color": "#2f6f4f"},
        {"id": "grigory", "name": "Grigory", "color": "#d1495b"},
    ],
    "labels": [{"id": "bug", "name": "Bug", "color": "#d1495b"}],
    "boards": [
        {
            "id": "web",
            "key": "WEB",
            "title": "Website",
            "columns": [
                {"id": "backlog", "name": "Backlog"},
                {"id": "doing", "name": "In Progress", "wip_limit": 2},
                {"id": "done", "name": "Done", "is_done": True},
            ],
            "custom_fields": [
                {"id": "customer", "name": "Customer", "type": "select",
                 "options": ["Acme", "Globex"]},
                {"id": "qty", "name": "Qty", "type": "number"},
            ],
            "next_card": 3,
        },
        {
            "id": "ops",
            "key": "OPS",
            "title": "Operations",
            "columns": [{"id": "todo", "name": "To do"}],
            "lease_ttl": "4h",
            "next_card": 1,
        },
    ],
}


class Base(unittest.TestCase):
    """A pristine deep copy of the fixture per test.

    Sharing one dict would make ``validate``'s purity untestable (the previous
    version back-filled defaults on read, so the order tests ran in decided
    whether a later test saw a ``desc`` nobody wrote) and would let one test's
    mutation silently become another's premise.
    """

    def setUp(self) -> None:
        self.doc = copy.deepcopy(DOC)

    # -- helpers ---------------------------------------------------------- #

    def board(self, board_id: str = "web") -> dict:
        for board in self.doc["boards"]:
            if board["id"] == board_id:
                return board
        raise AssertionError(f"no board {board_id}")

    def card(self, card_id: str = "WEB-1", **over: object) -> dict:
        card = {
            "id": card_id,
            "title": "Fix the deploy timeout",
            "column": "backlog",
            "order": model.ORDER_GAP,
            "labels": ["bug"],
        }
        card.update(over)
        return card

    def ctx(self, *ids: str) -> dict:
        known = set(ids) or {f"WEB-{n}" for n in range(1, 10)}
        return model.card_context(self.doc, self.board(), known)

    def claim(self, *, agent: str = "ada", minutes: int = 30,
              at: datetime.datetime | None = None, **over: object) -> dict:
        taken = at or T0
        claim = {
            "agent": agent,
            "at": taken,
            "expires": taken + datetime.timedelta(minutes=minutes),
        }
        claim.update(over)
        return claim

    def errors_for(self, card: dict) -> list[str]:
        return model.validate_card(card, self.board(), self.ctx())


class TestLeaseLiveness(Base):
    """A claim is a timestamp, not a status — so liveness is computed per read."""

    def test_a_claim_past_its_expiry_is_not_a_claim(self) -> None:
        """Expiry *is* release. No daemon rewrites bytes to free a card.

        If liveness were a stored field, a board whose agents were all killed
        mid-lease would stay locked forever with nothing left to unlock it.
        """
        claimed = self.claim()
        self.assertTrue(model.claim_live(claimed, at=T0))
        self.assertTrue(model.claim_live(claimed, at=T0 + datetime.timedelta(minutes=29)))
        self.assertFalse(model.claim_live(claimed, at=T0 + datetime.timedelta(minutes=31)))

    def test_the_expiry_instant_is_already_dead(self) -> None:
        """One second of slack here is a second in which two agents edit a file.

        ``expires`` is the last instant the holder may be writing, so the boundary
        must fall on the dead side; ``>`` vs ``>=`` is the whole difference.
        """
        expires = T0 + datetime.timedelta(minutes=30)
        claimed = self.claim()
        self.assertTrue(model.claim_live(claimed, at=expires - MINUTE))
        self.assertFalse(model.claim_live(claimed, at=expires),
                         "expires inclusive lets the loser believe it still holds the card")
        self.assertEqual(model.ttl_left(claimed, at=expires), 0)

    def test_an_overridden_claim_is_inert_but_still_auditable(self) -> None:
        """The loser of a cross-machine overlap keeps its bytes and loses its force.

        Deleting it would delete the evidence the human needs to adjudicate;
        leaving it live would let two agents both believe they hold ``src/api/**``.
        """
        beaten = self.claim(agent="grigory", overridden_by="ada")
        self.assertFalse(model.claim_live(beaten, at=T0))
        self.assertEqual(beaten["agent"], "grigory")
        self.assertEqual(model.claim_of({"claim": beaten})["overridden_by"], "ada")

    def test_with_no_injected_clock_liveness_reads_model_now(self) -> None:
        """``model.now`` is the only time source, which is why expiry is testable.

        A liveness check that read ``datetime.now()`` directly could not be moved
        forward, so the expiry test would sleep 31 minutes or become a skip.
        """
        claimed = self.claim()
        with mock.patch.object(model, "now", return_value=T0):
            self.assertTrue(model.claim_live(claimed))
            self.assertEqual(model.ttl_left(claimed), 1800)
        later = T0 + datetime.timedelta(minutes=31)
        with mock.patch.object(model, "now", return_value=later):
            self.assertFalse(model.claim_live(claimed))
            self.assertEqual(model.ttl_left(claimed), 0)

    def test_ttl_left_never_counts_below_zero(self) -> None:
        """A negative countdown renders as ``-3m left`` and a UI that lies.

        ``left`` is also what the CLI prints in a denial; the clamp is why the
        message can promise a time without arithmetic at the call site.
        """
        claimed = self.claim()
        self.assertEqual(model.ttl_left(claimed, at=T0), 30 * 60)
        self.assertEqual(model.ttl_left(claimed, at=T0 + HOUR), 0)
        self.assertEqual(model.ttl_left({"agent": "ada"}, at=T0), 0)

    def test_claim_span_is_that_claims_own_ttl_not_the_boards_default(self) -> None:
        """The UI's amber threshold is 25% of the claim's own TTL.

        A 2-hour lease and a 30-second lease on the same board would otherwise
        amber at the same minute, which is either useless or a lie.
        """
        self.assertEqual(model.claim_span(self.claim(minutes=30)), 1800)
        self.assertEqual(model.claim_span(self.claim(minutes=120)), 7200)
        self.assertEqual(model.claim_span(self.claim(minutes=1)), 60)

    def test_claim_span_of_a_malformed_claim_falls_back_to_the_floor(self) -> None:
        """A hand-edited claim with no ``at`` must not divide by a missing span.

        The fallback is ``TTL_MIN``: the shortest legal lease, so a broken claim
        goes amber immediately rather than never.
        """
        self.assertEqual(model.claim_span({}), model.TTL_MIN)
        self.assertEqual(model.claim_span({"agent": "ada"}), model.TTL_MIN)
        self.assertEqual(model.claim_span({"at": "yesterday", "expires": "whenever"}),
                         model.TTL_MIN)

    def test_a_stamp_written_as_a_string_is_still_a_stamp(self) -> None:
        """A claim hand-written in an editor has to mean the same thing.

        Files are the arbiter and people edit files: a quoted ISO timestamp that
        liveness cannot read would free a card its holder is still working on.
        """
        written = {"agent": "ada", "at": "2026-11-23T12:00:00",
                   "expires": "2026-11-23T12:30:00"}
        self.assertTrue(model.claim_live(written, at=T0))
        self.assertEqual(model.ttl_left(written, at=T0), 1800)

    def test_a_claim_with_no_agent_or_no_expiry_is_no_claim(self) -> None:
        """A claim with no ``expires`` must not read as one that never ends.

        A claim that never frees the card is the worst possible failure of a
        lease: the board needs a human, and nothing has said so.
        """
        self.assertFalse(model.claim_live({"expires": T0 + HOUR}, at=T0))
        self.assertFalse(model.claim_live({"agent": "ada"}, at=T0))
        self.assertFalse(model.claim_live({"agent": "ada", "expires": "soon"}, at=T0))
        self.assertFalse(model.claim_live(None, at=T0))
        self.assertIsNone(model.claim_of({"claim": "ada"}))

    def test_humanize_never_shows_a_negative_duration(self) -> None:
        self.assertEqual(model.humanize(125), "2m")
        self.assertEqual(model.humanize(59), "59s")
        self.assertEqual(model.humanize(7300), "2h1m")
        self.assertEqual(model.humanize(-400), "0s")


class TestLeaseLength(Base):
    """``--ttl`` parsing, and whose default wins when nobody says."""

    def test_the_documented_spellings_parse(self) -> None:
        for text, seconds in (("90s", 90), ("5m", 300), ("2h", 7200), ("1d", 86400)):
            self.assertEqual(model.parse_ttl(text), seconds, text)

    def test_an_out_of_range_lease_is_refused_naming_the_range(self) -> None:
        """Refused, not clamped — a clamped lease outlives its own command.

        A caller who asks for one second and silently gets thirty wrote a lease
        that stays live past the work it was guarding, and nothing told them.
        """
        for text in ("1s", "29s", "25h", "8d", "0m"):
            with self.assertRaises(ValidationError, msg=text) as caught:
                model.parse_ttl(text)
            self.assertIn("outside the range", str(caught.exception))
            self.assertIn("30s", str(caught.exception))
            self.assertIn("24h", str(caught.exception))

    def test_a_malformed_duration_names_the_shapes_it_wants(self) -> None:
        for text in ("soon", "30", "1w", "m", "5 minutes"):
            with self.assertRaises(ValidationError, msg=text) as caught:
                model.parse_ttl(text)
            self.assertIn("like 90s, 5m, 2h or 1d", str(caught.exception))

    def test_the_boards_own_default_beats_the_global_default(self) -> None:
        """``lease_ttl: 4h`` is a statement about every card on that board."""
        self.assertEqual(model.lease_seconds(None, self.board("ops")), 4 * 3600)
        self.assertEqual(model.lease_seconds("", self.board("ops")), 4 * 3600)
        self.assertEqual(model.lease_seconds(None, self.board("web")), 1800)
        self.assertEqual(model.lease_seconds(None, None), 1800)

    def test_an_explicit_ttl_beats_the_board(self) -> None:
        self.assertEqual(model.lease_seconds("45m", self.board("ops")), 2700)

    def test_a_board_with_an_unparseable_lease_ttl_is_a_read_error(self) -> None:
        """Caught on read, because ``lease_seconds`` trusts a validated file.

        A board that loaded with ``lease_ttl: whenever`` would make every claim
        on it raise at write time, blaming the person who typed ``--ttl``.
        """
        self.board("ops")["lease_ttl"] = "whenever"
        errors = model.validate(self.doc)
        self.assertEqual([e for e in errors if "lease_ttl" in e],
                         ["boards[1].lease_ttl: --ttl 'whenever': "
                          "expected a duration like 90s, 5m, 2h or 1d"])
        self.board("ops")["lease_ttl"] = "1s"
        self.assertTrue(any("lease_ttl" in e and "outside the range" in e
                            for e in model.validate(self.doc)))
        self.board("ops")["lease_ttl"] = "4h"
        self.assertEqual(model.validate(self.doc), [])

class TestPathOverlap(Base):
    """The intersection test two agents' claims must agree on."""

    def test_a_tree_glob_and_a_file_inside_it_overlap_in_both_directions(self) -> None:
        """Symmetric, both ways round.

        Testing containment in one direction only accepts ``src/api/routes.py``
        against ``ada``'s ``src/api/**``: the narrower claim looks disjoint when
        the *wide* pattern is the second argument, and two agents edit one file.
        """
        wide, narrow = "src/api/**", "src/api/routes.py"
        self.assertTrue(model.patterns_overlap(wide, narrow))
        self.assertTrue(model.patterns_overlap(narrow, wide))
        self.assertTrue(model.patterns_overlap("src/**/*.py", "src/api/routes.py"))
        self.assertTrue(model.patterns_overlap("src/api/routes.py", "src/**/*.py"))

    def test_disjoint_trees_never_overlap(self) -> None:
        self.assertFalse(model.patterns_overlap("docs/**", "src/**"))
        self.assertFalse(model.patterns_overlap("docs/a.md", "src/api/routes.py"))
        self.assertFalse(model.patterns_overlap("src/api/**", "src/apixyz/y.py"),
                         "a prefix test would claim src/apixyz because it starts with src/api")

    def test_a_bare_directory_and_a_glob_under_it_overlap(self) -> None:
        """Prefix containment, both ways: ``src`` is inside ``src/**``.

        The bare name is refused as a *claim* (see ``pattern_is_loose``) but it is
        still legal in a claim someone else wrote, and the overlap test has to
        treat it as the set of files it actually names.
        """
        self.assertTrue(model.patterns_overlap("src/**", "src"))
        self.assertTrue(model.patterns_overlap("src", "src/**"))
        self.assertTrue(model.patterns_overlap("src", "src/api/routes.py"))

    def test_repeated_slashes_match_any_depth(self) -> None:
        self.assertTrue(model.patterns_overlap("src/**/x.py", "src/x.py"))
        self.assertTrue(model.patterns_overlap("src/**/x.py", "src/a/b/c/x.py"))
        self.assertTrue(model.patterns_overlap("**/routes.py", "routes.py"))
        self.assertTrue(model.patterns_overlap("**", "anything/at/all.py"))

    def test_the_same_pattern_always_overlaps_itself(self) -> None:
        self.assertTrue(model.patterns_overlap("src/api/**", "src/api/**"))
        self.assertTrue(model.patterns_overlap("README.md", "README.md"))

    def test_a_star_segment_is_read_as_wider_than_the_path_it_faces(self) -> None:
        """Ambiguity resolves toward refusal, so ``*`` never looks narrower.

        ``src/*`` names one segment and ``src/api/routes.py`` has two, yet a real
        tree can put a directory behind that star; the pair is reported as
        overlapping. A false refusal costs the caller one extra ``ready`` call, a
        false accept costs two agents their work and a human the merge.
        """
        self.assertTrue(model.patterns_overlap("src/*", "src/api/routes.py"))
        self.assertTrue(model.patterns_overlap("docs/*", "docs/a/b.md"))
        self.assertTrue(model.patterns_overlap("src/**", "src"))

    def test_two_wildcarded_globs_that_share_a_file_are_a_collision(self) -> None:
        """Neither glob contains the other, so containment alone misses them.

        Two agents can hold ``src/**`` and ``**/routes.py`` across a clean merge —
        one file per card means git never complains — and both are about to edit
        ``src/routes.py``. A false refusal costs one extra ``ready`` call; a false
        accept costs both their work and the human the merge.
        """
        self.assertTrue(model.patterns_overlap("src/**", "**/routes.py"))
        self.assertTrue(model.patterns_overlap("**/routes.py", "src/**"))
        self.assertTrue(model.patterns_overlap("src/api/*.py", "src/*/routes.py"))
        self.assertTrue(model.patterns_overlap("src/**/routes.py", "src/api/*.py"))
        self.assertTrue(model.patterns_overlap("a*.py", "*b.py"),
                        "these meet at ab.py: `*b.py` ends with .py")
        self.assertTrue(model.patterns_overlap("*.py", "a*.py"))
        self.assertTrue(model.patterns_overlap("**/api/**", "docs/api"))

    def test_two_globs_that_cannot_name_a_common_file_stay_apart(self) -> None:
        """The intersection is real, not a blanket "both have stars, collide".

        The cheap answer to the test above is to refuse anything with a wildcard
        against anything with a wildcard, and it would pass every collision case
        here while quietly serialising the board: nobody could claim two files in
        one repo at the same time.
        """
        self.assertFalse(model.patterns_overlap("a/secret.py", "a/*.md"))
        self.assertFalse(model.patterns_overlap("*.py", "*.js"))
        self.assertFalse(model.patterns_overlap("**/*.ts", "src/**/*.py"))
        self.assertFalse(model.patterns_overlap("src/api/**", "src/apixyz/y.py"))
        self.assertFalse(model.patterns_overlap("**/api/**", "docs/apix"))
        self.assertFalse(model.patterns_overlap("src/**/x.py", "docs/**"))
        self.assertFalse(model.patterns_overlap("src/**/*.py", "src/**/*.js"))
        self.assertFalse(model.patterns_overlap("**/routes.py", "**/*.js"))
        # ... but a tree glob and a suffix glob do share src/x.ts, however unlikely
        self.assertTrue(model.patterns_overlap("**/*.ts", "src/**"))

    def test_overlap_agrees_with_a_reference_matcher_over_a_generated_tree(self) -> None:
        """The brute force this file's other cases cannot be.

        Every assertion above was written by hand, so every one of them tests a
        pair someone already thought to try. This walks a generated tree and
        checks the verdict against what a plain ``fnmatch`` matcher says, which is
        the only way to find the collision nobody imagined. Two directions:

        * any pair sharing a real path MUST collide — a miss is two agents
          editing one file;
        * any pair the matcher reports MUST share a path under the *loose* reading
          of a glob (a ``*`` may name a whole subtree, a pattern may stop at a
          directory). Beyond that, a report is a bug.

        Extras permitted between the two bounds are the deliberate loose calls,
        like ``src/*`` against ``src/api/*.py``: the star can name the directory.
        """
        tokens = ("src", "api", "docs", "routes.py", "x.py", "y.js")
        patterns = ("src", "src/**", "src/*", "src/api", "src/api/**", "src/api/*.py",
                    "src/*/routes.py", "**/routes.py", "**/*.py", "**/*.js", "*.py",
                    "*.js", "src/**/*.py", "docs/**", "**", "src/**/x.py", "routes.py",
                    "src/*/*.py")

        def matches(pattern: str, segments: list, loose: bool = False) -> bool:
            pat = pattern.split("/")

            def walk(i: int, j: int) -> bool:
                if i == len(pat):
                    # loose: the pattern stopped at a directory, so the rest of
                    # the path lives under what it named
                    return loose or j == len(segments)
                if pat[i] == "**":
                    return any(walk(i + 1, k) for k in range(j, len(segments) + 1))
                if loose and pat[i] == "*":
                    return any(walk(i + 1, k) for k in range(j + 1, len(segments) + 1))
                return (j < len(segments)
                        and fnmatch.fnmatchcase(segments[j], pat[i])
                        and walk(i + 1, j + 1))
            return walk(0, 0)

        # A file segment cannot be a parent directory: `routes.py/x.py` is not a
        # path that can exist, and generating it would invent collisions.
        tree = ["/".join(parts) for depth in range(1, 7)
                for parts in itertools.product(tokens, repeat=depth)
                if all("." not in part for part in parts[:-1])]
        strict = {pattern: frozenset(path for path in tree
                                     if matches(pattern, path.split("/")))
                  for pattern in patterns}
        lenient = {pattern: frozenset(path for path in tree
                                      if matches(pattern, path.split("/"), loose=True))
                   for pattern in patterns}
        self.assertEqual([pattern for pattern in patterns if not strict[pattern]], [],
                         "a pattern matched nothing generated: this test proves nothing")

        missed, impossible = [], []
        for left, right in itertools.combinations_with_replacement(patterns, 2):
            says = model.patterns_overlap(left, right)
            self.assertEqual(says, model.patterns_overlap(right, left),
                             f"overlap is not symmetric for {left!r} / {right!r}")
            if strict[left] & strict[right] and not says:
                missed.append((left, right))
            if says and not (lenient[left] & lenient[right]):
                impossible.append((left, right))
        self.assertEqual(missed, [], "real shared files the overlap test walked past")
        self.assertEqual(impossible, [],
                         "claimed a collision no path under any reading can match")

    def test_a_pattern_cannot_reach_outside_the_repo(self) -> None:
        """Refused, not normalised.

        ``../secrets/**`` quietly rewritten to ``secrets/**`` would be a promise
        other agents read and the author did not make.
        """
        for bad in ("../secrets/**", "src/../../etc/passwd", "/etc/passwd",
                    "C:/Windows/x", "//server/share"):
            with self.assertRaises(ValidationError, msg=bad) as caught:
                model.normalize_pattern(bad)
            message = str(caught.exception)
            self.assertTrue("relative to the repo root" in message
                            or "outside the repo" in message, message)

    def test_an_empty_pattern_is_refused_rather_than_becoming_the_whole_repo(self) -> None:
        """An empty pattern would be the widest claim on the board."""
        with self.assertRaises(ValidationError):
            model.normalize_pattern("")
        with self.assertRaises(ValidationError):
            model.normalize_pattern("   ")

    def test_cosmetic_noise_is_normalised_and_only_danger_is_refused(self) -> None:
        """Cosmetic noise is fixed silently; only dangerous shapes are refused.

        ``src\\api\\**`` typed in a Windows shell and ``src/api/**/`` mean the
        same claim, and refusing them would only teach agents to distrust the
        tool and quote their paths oddly.
        """
        self.assertEqual(model.normalize_pattern("  src/api/**  "), "src/api/**")
        self.assertEqual(model.normalize_pattern("src/api/**/"), "src/api/**")
        self.assertEqual(model.normalize_pattern("src\\api\\routes.py"), "src/api/routes.py")
        self.assertEqual(model.normalize_pattern("./README.md"), "./README.md")

    def test_an_unsafe_pattern_already_in_a_file_is_a_read_error(self) -> None:
        """The store-side teeth: validation, not just the CLI's argument check.

        ``claim.paths`` can arrive from a hand edit or a merge. If the glob check
        lived only in ``--paths`` parsing, one written ``..`` would poison every
        later overlap test.
        """
        errors = self.errors_for(self.card(
            claim=self.claim(paths=["src/api/**", "../elsewhere/**"])))
        self.assertTrue(any("claim.paths[1]" in e and "outside the repo" in e
                            for e in errors), errors)


class TestLoosePatterns(Base):
    """Refusing ``src/api`` — a claim that silently swallows 41 files."""

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        (self.root / "src" / "api").mkdir(parents=True)
        (self.root / "src" / "api" / "routes.py").write_text("", encoding="utf-8")
        (self.root / "README.md").write_text("", encoding="utf-8")

    def test_a_directory_with_no_wildcard_is_loose(self) -> None:
        """``src/api`` reads like a filename and claims its whole subtree.

        A bare prefix counts as containing everything under it, so one careless
        claim blocks the board and the refusal says why.
        """
        self.assertTrue(model.pattern_is_loose("src/api", self.root))
        self.assertTrue(model.pattern_is_loose("src", self.root))

    def test_a_plain_file_path_is_the_most_precise_claim_there_is(self) -> None:
        self.assertFalse(model.pattern_is_loose("src/api/routes.py", self.root))
        self.assertFalse(model.pattern_is_loose("README.md", self.root))

    def test_a_file_that_does_not_exist_yet_is_not_loose(self) -> None:
        """Refusing what is absent would forbid the commonest honest claim.

        Writing a new module means naming a path no one has created: TDD, a
        generated client, a rename. ``is_dir()`` is false for it, so the claim
        stands — the trap is only the directory whose breadth is invisible.
        """
        self.assertFalse(model.pattern_is_loose("src/api/new_module.py", self.root))
        self.assertFalse(model.pattern_is_loose("does/not/exist/yet.py", self.root))

    def test_a_wildcard_pattern_is_never_loose(self) -> None:
        """A pattern with a star has already declared its breadth out loud.

        Checked before the filesystem, and deliberately so: a claim is a glob, so
        a tree with a directory literally named ``*`` (a shell accident, and
        ls-able) must not turn ``src/*`` into a bare-directory refusal. Pattern
        shape decides, not what happens to be on disk.
        """
        self.assertTrue((self.root / "src" / "api").is_dir())
        self.assertFalse(model.pattern_is_loose("src/api/**", self.root))
        self.assertFalse(model.pattern_is_loose("src/*", self.root))
        (self.root / "star" / "*").mkdir(parents=True)
        self.assertFalse(model.pattern_is_loose("star/*", self.root))
        self.assertTrue((self.root / "star" / "*").is_dir())

    def test_without_a_tree_to_look_in_nothing_is_loose(self) -> None:
        """The check is only meaningful against a repo, and says nothing without one."""
        self.assertFalse(model.pattern_is_loose("src/api", None))
        self.assertFalse(model.pattern_is_loose("src/api"))


class TestOrdering(Base):
    """Fractional positions: the reason a move rewrites one card, not a column."""

    def test_order_between_sits_strictly_between_two_neighbours(self) -> None:
        mid = model.order_between({"order": 1024.0}, {"order": 2048.0})
        self.assertTrue(1024.0 < mid < 2048.0)
        self.assertEqual(mid, 1536.0)

    def test_the_ends_of_a_column_are_the_same_call_as_the_middle(self) -> None:
        """``inf`` sentinels mean start/end, so drag-to-end is not a second path."""
        self.assertEqual(model.order_between(None, None), model.ORDER_GAP)
        self.assertGreater(model.order_between({"order": 1024.0}, None), 1024.0)
        self.assertLess(model.order_between(None, {"order": 1024.0}), 1024.0)

    def test_order_between_raises_when_two_floats_have_no_room(self) -> None:
        """Two adjacent floats have no midpoint, and a silent 0.5 would corrupt both.

        Returning either neighbour would put two cards on one position and make
        the column order depend on which clone rendered it.
        """
        floor = 1.0
        ceiling = math.nextafter(1.0, 2.0)
        self.assertTrue(floor < ceiling)
        with self.assertRaises(model.ReorderExhausted):
            model.order_between({"order": floor}, {"order": ceiling})

    def test_rebalance_repairs_a_column_that_ran_out_of_room(self) -> None:
        """Squeeze a gap to nothing, then prove one renumber makes room again.

        This is the recovery the caller takes when ``order_between`` raises, so
        the exception is survivable rather than a dead end the UI swallows.
        """
        cards = [{"id": "WEB-1", "order": 1024.0}, {"id": "WEB-2", "order": 1025.0}]
        squeezed = 0
        low, high = 1024.0, 1025.0
        with self.assertRaises(model.ReorderExhausted):
            for _ in range(400):
                low = model.order_between({"order": low}, {"order": high})
                squeezed += 1
                cards.insert(1, {"id": f"SQUEEZE-{squeezed}", "order": low})
        self.assertGreater(squeezed, 20, "the gap gave out too early to be a squeeze")

        updates = model.rebalance(cards)
        self.assertTrue(updates, "a squeezed column came back with no fixes")
        positions = {str(c["id"]): float(c["order"]) for c in cards}
        for update in updates:
            positions[update["id"]] = update["order"]
        rebuilt = sorted(positions.values())
        self.assertEqual(rebuilt, [model.ORDER_GAP * n for n in range(1, len(cards) + 1)],
                         f"renumber did not restore even spacing: {rebuilt[:6]}")
        for before, after in zip(rebuilt, rebuilt[1:]):
            mid = model.order_between({"order": before}, {"order": after})
            self.assertTrue(before < mid < after,
                            f"rebuilt gap {before}..{after} still cannot take an insert")

    def test_rebalance_is_silent_for_a_column_that_needs_nothing(self) -> None:
        """An idempotent renumber, so a retry cannot spam a whole column of diffs.

        A ``rebalance`` that always returned every card would rewrite a column on
        every drag and put forty lines in a human's git diff for one move.
        """
        cards = [{"id": f"WEB-{n}", "order": float(n * model.ORDER_GAP)}
                 for n in range(1, 5)]
        self.assertEqual(model.rebalance(cards), [])

    def test_sort_is_deterministic_on_an_order_tie(self) -> None:
        """Two clones that wrote the same position must render one column alike.

        There is no shared array left to arbitrate it, so the tie-break is the id
        and the only alternative is a board that shuffles between machines.
        """
        tied = [{"id": "WEB-10", "order": 5.0}, {"id": "WEB-2", "order": 5.0},
                {"id": "WEB-1", "order": 5.0}]
        self.assertEqual([c["id"] for c in model.sort_cards(tied)],
                         ["WEB-1", "WEB-10", "WEB-2"])
        self.assertEqual([c["id"] for c in model.sort_cards(list(reversed(tied)))],
                         ["WEB-1", "WEB-10", "WEB-2"])

    def test_a_card_without_an_order_queues_behind_the_numbered_front(self) -> None:
        """Hand-written cards have no ``order``; they queue up, never jump.

        The fallback position is ``ORDER_GAP * 1000``, past anywhere a person has
        dragged to by hand, so a card that arrived without a position lands at the
        end instead of the top — where it would read as fresh urgent work.
        """
        ordered = model.sort_cards([
            {"id": "WEB-9"},
            {"id": "WEB-1", "order": model.ORDER_GAP},
            {"id": "WEB-2", "order": 3 * model.ORDER_GAP},
        ])
        self.assertEqual([c["id"] for c in ordered], ["WEB-1", "WEB-2", "WEB-9"])

    def test_an_infinite_order_is_refused_and_cannot_hijack_the_sort(self) -> None:
        """``.inf`` is valid YAML and poisons every midpoint taken beside it.

        A midpoint against an infinite neighbour is infinite, so one ``.inf`` in a
        column makes every later insert land on the same number and the column's
        order becomes whoever rendered last. Validation refuses the value, and the
        sort degrades it to the un-numbered sentinel rather than trusting it.
        """
        errors = self.errors_for(self.card(order=float("inf")))
        self.assertTrue(any("order: expected a finite number" in e for e in errors),
                        errors)
        ordered = model.sort_cards([{"id": "WEB-1", "order": float("inf")},
                                    {"id": "WEB-2", "order": 1024.0}])
        self.assertEqual([c["id"] for c in ordered], ["WEB-2", "WEB-1"])

    def test_sort_cards_ignores_junk_rather_than_raising(self) -> None:
        self.assertEqual(model.sort_cards([None, "x", {"id": "WEB-1"}]), [{"id": "WEB-1"}])


class TestDependencies(Base):
    """``blocked_by`` edges, and naming the loop instead of saying "cycle"."""

    def test_a_cycle_is_reported_as_a_path_of_ids(self) -> None:
        """On a board of forty cards, "refused: cycle" tells nobody what to fix."""
        edges = {"WEB-1": ["WEB-2"], "WEB-2": ["WEB-3"], "WEB-3": ["WEB-1"]}
        cycle = model.find_cycle(edges)
        self.assertIsNotNone(cycle)
        self.assertEqual(cycle[0], cycle[-1], "the path must close")
        self.assertEqual(set(cycle), {"WEB-1", "WEB-2", "WEB-3"})
        self.assertEqual(" -> ".join(cycle), "WEB-1 -> WEB-2 -> WEB-3 -> WEB-1")

    def test_find_cycle_from_a_start_names_the_cycle_that_card_is_in(self) -> None:
        """The edge being added is the one to report, not the first loop found.

        ``card.dep.add`` passes the card it was asked to block. Naming an
        unrelated pre-existing cycle sends the agent to untangle the wrong pair,
        and on a board where both loops exist that mistake is invisible.
        """
        edges = {"WEB-1": ["WEB-2"], "WEB-2": ["WEB-1"],
                 "OPS-1": ["OPS-2"], "OPS-2": ["OPS-1"]}
        self.assertEqual(model.find_cycle(edges, "OPS-1"), ["OPS-1", "OPS-2", "OPS-1"])
        self.assertEqual(model.find_cycle(edges, "WEB-2"), ["WEB-2", "WEB-1", "WEB-2"])

    def test_a_cycle_downstream_of_the_start_is_reported_from_the_cycle(self) -> None:
        """The start need not be in the loop — it just has to reach it.

        ``dep add WEB-1 WEB-9`` where ``WEB-9`` already loops through ``WEB-7``
        is still a refusal the caller can act on, and the named path has to be
        the loop, not the tail that leads into it.
        """
        edges = {"WEB-1": ["WEB-7"], "WEB-7": ["WEB-9"], "WEB-9": ["WEB-7"]}
        cycle = model.find_cycle(edges, "WEB-1")
        self.assertEqual(cycle, ["WEB-7", "WEB-9", "WEB-7"])

    def test_a_diamond_is_not_a_cycle(self) -> None:
        """Two paths to one card is ordinary fan-in, not a deadlock.

        Mistaking a diamond for a loop would refuse a perfectly workable
        dependency the moment two cards both waited on the same task.
        """
        edges = {"WEB-1": ["WEB-2", "WEB-3"], "WEB-2": ["WEB-4"],
                 "WEB-3": ["WEB-4"], "WEB-4": []}
        self.assertIsNone(model.find_cycle(edges))
        self.assertIsNone(model.find_cycle(edges, "WEB-1"))

    def test_a_self_loop_is_a_cycle(self) -> None:
        self.assertEqual(model.find_cycle({"WEB-1": ["WEB-1"]}, "WEB-1"),
                         ["WEB-1", "WEB-1"])

    def test_an_acyclic_graph_is_no_cycle_in_any_direction(self) -> None:
        edges = {"WEB-1": ["WEB-2", "WEB-3"], "WEB-2": ["WEB-3"], "WEB-3": []}
        self.assertIsNone(model.find_cycle(edges))
        for start in edges:
            self.assertIsNone(model.find_cycle(edges, start))

    def test_dependencies_skips_junk_it_was_never_promised(self) -> None:
        edges = model.dependencies([
            {"id": "WEB-1", "blocked_by": ["WEB-2", 7, None]},
            {"id": "WEB-2", "blocked_by": "WEB-1"},
            {"blocked_by": ["WEB-9"]},
            "junk",
        ])
        self.assertEqual(edges["WEB-1"], ["WEB-2"])
        self.assertEqual(edges["WEB-2"], [])

    def test_a_finished_blocker_stops_blocking(self) -> None:
        edges = {"WEB-1": ["WEB-2", "WEB-3"]}
        self.assertEqual(model.blocked_by_open(edges, "WEB-1", set()), ["WEB-2", "WEB-3"])
        self.assertEqual(model.blocked_by_open(edges, "WEB-1", {"WEB-2"}), ["WEB-3"])
        self.assertEqual(model.blocked_by_open(edges, "WEB-1", {"WEB-2", "WEB-3"}), [])

    def test_a_card_blocking_itself_is_refused_where_the_ids_are_known(self) -> None:
        errors = self.errors_for(self.card(blocked_by=["WEB-1"]))
        self.assertTrue(any("cannot block itself" in e for e in errors), errors)

    def test_an_unknown_blocker_is_refused_not_ignored(self) -> None:
        """A dangling id is a card nobody can ever unblock.

        With the store's id set available, ``WEB-77`` must be an error: silently
        accepted, it makes ``WEB-1`` permanently non-ready and ``ready`` has no
        way to say why.
        """
        errors = self.errors_for(self.card(blocked_by=["WEB-77"]))
        self.assertTrue(any("'WEB-77' is not a card in this store" in e
                            for e in errors), errors)
        self.assertEqual(self.errors_for(self.card(blocked_by=["WEB-2"])), [])

    def test_is_done_column_reads_the_flag_the_author_set(self) -> None:
        board = self.board()
        self.assertTrue(model.is_done_column(board, "done"))
        self.assertFalse(model.is_done_column(board, "doing"))
        self.assertFalse(model.is_done_column(board, "not-a-column"))

    def test_board_for_card_finds_the_home_by_key_alone(self) -> None:
        self.assertIs(model.board_for_card(self.doc, "OPS-4"), self.board("ops"))
        self.assertIsNone(model.board_for_card(self.doc, "NOPE-4"))
        self.assertIsNone(model.board_for_card(self.doc, "WEB-banana"))


class TestValidateBoard(Base):
    """``validate`` is the read gate. Everything it misses becomes a crash later."""

    def test_the_fixture_is_valid(self) -> None:
        self.assertEqual(model.validate(self.doc), [])

    def test_a_version_one_file_is_refused_and_names_the_way_out(self) -> None:
        """A v1 file still on disk must not load as if it were v2.

        Its cards are inside ``boards[].cards``; loading it would show an empty
        board and the first save would delete them. The message has to name
        ``yamlboard migrate`` or the reader has no idea the bridge exists.
        """
        self.doc["version"] = 1
        errors = model.validate(self.doc)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("file says 1", errors[0])
        self.assertIn("yamlboard migrate", errors[0])

    def test_a_newer_version_is_refused_before_it_stamps_the_file_old(self) -> None:
        """Refusing upward is what stops this build downgrading someone's board."""
        self.doc["version"] = model.VERSION + 1
        errors = model.validate(self.doc)
        self.assertTrue(any("understands up to" in e for e in errors), errors)

    def test_a_version_below_one_is_a_value_error_not_a_migration(self) -> None:
        self.doc["version"] = 0
        self.assertTrue(any(e.startswith("version:") for e in model.validate(self.doc)))

    def test_a_board_with_a_cards_key_says_where_cards_live_now(self) -> None:
        """``cards:`` is the v1 shape, so it gets more than "unknown key".

        A person who just upgraded and sees ``unknown key 'cards' (allowed: ...)``
        has no way to infer that the whole storage model moved.
        """
        self.board()["cards"] = []
        errors = model.validate(self.doc)
        self.assertTrue(any("unknown key 'cards'" in e for e in errors), errors)
        self.assertTrue(any("cards live in cards/<ID>.yaml" in e and
                            "yamlboard migrate" in e for e in errors), errors)

    def test_the_deleted_global_counter_is_an_unknown_key(self) -> None:
        """``rev:`` must not survive as a tolerated key.

        Reviving it would silently reintroduce the v1 failure this rewrite exists
        to fix: two clones touching two unrelated cards conflicting on line one.
        """
        self.doc["rev"] = 7
        errors = model.validate(self.doc)
        self.assertTrue(any("unknown key 'rev'" in e for e in errors), errors)

    def test_an_unknown_key_anywhere_is_reported_with_its_path(self) -> None:
        self.doc["peopel"] = self.doc.pop("people")
        self.board()["columns"][1]["wip_limitx"] = 3
        errors = model.validate(self.doc)
        self.assertTrue(any("document: unknown key 'peopel'" in e for e in errors), errors)
        self.assertTrue(any("boards[0].columns[1]: unknown key 'wip_limitx'" in e
                            for e in errors), errors)

    def test_validate_never_mutates_the_document_it_checked(self) -> None:
        """Pure, because a read must not be a write.

        The first version back-filled defaults here, so opening a board in the
        browser and saving one card stamped ``desc: ''`` onto every card nobody
        touched — a 40-line diff for one drag, and a merge conflict for everyone
        whose hand-edited file the app had quietly reformatted.
        """
        self.board()["columns"][1]["name"] = ""      # deliberately invalid too
        before = copy.deepcopy(self.doc)
        errors = model.validate(self.doc)
        self.assertTrue(errors)
        self.assertEqual(list(self.doc), list(before))
        self.assertEqual(model.jsonable(self.doc), model.jsonable(before))

    def test_validate_does_not_back_fill_the_keys_the_file_omitted(self) -> None:
        """Stripped of every optional key, the file must come back that way.

        A ``setdefault`` in ``validate`` is invisible on a fully-populated
        fixture and fatal on a lean one: the next save writes ``people: []``,
        ``labels: []`` and ``custom_fields: []`` into a board whose author
        deliberately left them out, and every clone of it conflicts on line one.
        """
        lean = {
            "version": 2,
            "title": "Lean",
            "boards": [{"id": "web", "key": "WEB", "title": "Website",
                        "columns": [{"id": "todo", "name": "To do"}]}],
        }
        keys = [list(lean), list(lean["boards"][0]), list(lean["boards"][0]["columns"][0])]
        self.assertEqual(model.validate(lean), [])
        self.assertEqual([list(lean), list(lean["boards"][0]),
                          list(lean["boards"][0]["columns"][0])], keys)

        card = {"id": "WEB-1", "title": "x", "column": "todo"}
        card_keys = list(card)
        board = lean["boards"][0]
        self.assertEqual(model.validate_card(card, board,
                                             model.card_context(lean, board, {"WEB-1"})), [])
        self.assertEqual(list(card), card_keys)

    def test_duplicate_ids_are_reported_at_the_index_that_collides(self) -> None:
        self.doc["people"].append({"id": "ada", "name": "Clone"})
        self.board()["columns"].append({"id": "doing", "name": "Also doing"})
        self.doc["boards"].append({
            "id": "web2", "key": "WEB", "title": "Clone",
            "columns": [{"id": "todo", "name": "To do"}],
        })
        errors = model.validate(self.doc)
        self.assertTrue(any("duplicate person id 'ada'" in e for e in errors), errors)
        self.assertTrue(any("duplicate column id 'doing'" in e for e in errors), errors)
        self.assertIn("boards[2].key: duplicate board key 'WEB'", errors)

    def test_the_card_counter_is_bounded_by_the_id_width(self) -> None:
        """999999999 is the widest ``CARD_RE`` allows; past it, id minting lies."""
        self.board()["next_card"] = 10 ** 9
        self.assertTrue(any("next_card" in e for e in model.validate(self.doc)))
        self.board()["next_card"] = 0
        self.assertTrue(any("next_card" in e for e in model.validate(self.doc)))
        self.board()["next_card"] = 5
        self.assertEqual(model.validate(self.doc), [])

    def test_a_select_field_needs_options_or_it_cannot_be_answered(self) -> None:
        self.board()["custom_fields"].append({"id": "env", "name": "Env",
                                              "type": "select", "options": []})
        self.assertTrue(any("a select field needs at least one option" in e
                            for e in model.validate(self.doc)))

    def test_a_wip_limit_of_zero_or_less_is_refused(self) -> None:
        """A ``wip_limit: 0`` would flag a column as over capacity while empty.

        The UI paints the header from this number; zero is not "unlimited", it is
        "already one too many", and a board that means unlimited leaves the key
        out. Overstepping itself stays a UI concern — validate only checks the
        limit is a whole number a header can be compared against.
        """
        self.board()["columns"][1]["wip_limit"] = 0
        self.assertTrue(any("wip_limit: expected a whole number of 1 or more" in e
                            for e in model.validate(self.doc)))
        self.board()["columns"][1]["wip_limit"] = "3"
        self.assertTrue(any("wip_limit" in e for e in model.validate(self.doc)))
        self.board()["columns"][1]["wip_limit"] = 1
        self.assertEqual(model.validate(self.doc), [])

    def test_a_minimal_document_validates(self) -> None:
        """README's promise: only version, boards, and a board's own id/keys."""
        minimal = {
            "version": 2,
            "title": "Minimal",
            "boards": [{"id": "web", "key": "WEB", "title": "Website",
                        "columns": [{"id": "todo", "name": "To do"}]}],
        }
        self.assertEqual(model.validate(minimal), [])

    def test_a_non_list_section_is_reported_not_laundered(self) -> None:
        self.doc["people"] = ""
        self.doc["boards"] = {}
        errors = model.validate(self.doc)
        self.assertIn("people: expected a list", errors)
        self.assertIn("boards: expected a list", errors)

    def test_a_junk_column_is_an_error_not_a_typeerror(self) -> None:
        """An unhashable value used to raise out of ``validate`` and 500 the board."""
        self.board()["columns"].append(["todo", "To do"])
        errors = model.validate(self.doc)
        self.assertTrue(any("columns[3]: expected a mapping" in e for e in errors), errors)


class TestValidateCard(Base):
    """One card file, checked against the board that owns its prefix."""

    def test_a_well_formed_card_passes(self) -> None:
        self.assertEqual(self.errors_for(self.card()), [])

    def test_an_unknown_claim_key_is_refused(self) -> None:
        """``CLAIM_KEYS`` is the complete vocabulary, enforced on read.

        A key code writes but the tuple does not list makes the card file
        permanently unreadable — and an unreadable card is a card whose claim
        nobody can see, release, or expire.
        """
        errors = self.errors_for(self.card(claim=self.claim(status="active")))
        self.assertTrue(any("claim: unknown key 'status'" in e for e in errors), errors)

    def test_a_claim_with_no_expiry_is_refused(self) -> None:
        """A card that never frees needs a human, quietly."""
        claim = self.claim()
        del claim["expires"]
        errors = self.errors_for(self.card(claim=claim))
        self.assertTrue(any("a claim without an expiry never frees the card" in e
                            for e in errors), errors)

    def test_an_agent_outside_the_allowlist_is_refused(self) -> None:
        """``people`` is the agent allowlist; an unknown name is never auto-created."""
        errors = self.errors_for(self.card(claim=self.claim(agent="mallory")))
        self.assertTrue(any("claim.agent" in e and "mallory" in e for e in errors), errors)
        for key in ("preempted", "overridden_by"):
            errors = self.errors_for(self.card(claim=self.claim(**{key: "mallory"})))
            self.assertTrue(any(f"claim.{key}" in e and "mallory" in e
                                for e in errors), (key, errors))

    def test_a_live_claim_shape_survives_validation_whatever_its_age(self) -> None:
        """An expired claim stays valid, because an expired claim still has bytes.

        Refusing it on read would brick the file the audit trail lives in, and
        expiry is supposed to be free, not a cleanup someone owes.
        """
        stale = self.claim(overridden_by="grigory", preempted="ada",
                           note="half-done, back tomorrow",
                           paths=["src/api/**", "docs/release.md"])
        self.assertEqual(self.errors_for(self.card(claim=stale)), [])

    def test_only_null_and_empty_string_clear_an_assignee(self) -> None:
        """Only null and "" mean nobody: ``none`` is a legal person id."""
        self.doc["people"].append({"id": "none", "name": "Nobody"})
        self.assertEqual(self.errors_for(self.card(assignee="none")), [])
        errors = self.errors_for(self.card(assignee="stranger"))
        self.assertTrue(any("assignee 'stranger' is not a person" in e
                            for e in errors), errors)

    def test_a_card_id_from_another_board_is_refused(self) -> None:
        errors = model.validate_card(self.card("OPS-4"), self.board(), self.ctx("OPS-4"))
        self.assertTrue(any("does not start with this board's key" in e
                            for e in errors), errors)

    def test_an_unhashable_column_is_an_error_not_a_crash(self) -> None:
        """A list in ``column`` used to raise TypeError straight out of validate.

        One unquoted YAML mistake would 500 the whole board instead of naming the
        card and the field that need fixing.
        """
        errors = self.errors_for(self.card(column=["doing"]))
        self.assertEqual([e for e in errors if "column" in e],
                         ["card WEB-1.column: ['doing'] is not a column of this board"])

    def test_nan_is_refused_in_a_number_field_but_degrades_on_the_wire(self) -> None:
        """NaN is a YAML float and invalid JSON: it blanks the board for everyone."""
        errors = self.errors_for(self.card(fields={"qty": float("nan")}))
        self.assertTrue(any("fields.qty: expected a number" in e for e in errors), errors)
        self.assertEqual(json.dumps(model.jsonable({"qty": float("nan")})),
                         '{"qty": null}')

    def test_a_select_value_outside_its_options_is_refused(self) -> None:
        errors = self.errors_for(self.card(fields={"customer": "Initech"}))
        self.assertTrue(any("fields.customer: expected one of Acme, Globex" in e
                            for e in errors), errors)

    def test_a_date_the_server_cannot_parse_is_refused_not_truncated(self) -> None:
        """``2026-12-01 (EOD)`` used to parse as a real date.

        A typo silently became a deadline nobody set; refusing is the only
        answer that tells the author their keystrokes were not understood.
        """
        errors = self.errors_for(self.card(due="2026-12-01 (EOD)"))
        self.assertTrue(any(".due: expected a date" in e for e in errors), errors)
        self.assertEqual(self.errors_for(self.card(due=datetime.date(2026, 12, 1))), [])

    def test_a_free_text_timestamp_is_named_with_its_path(self) -> None:
        errors = self.errors_for(self.card(
            comments=[{"at": "yesterday", "by": "ada", "text": "still happening"}]))
        self.assertTrue(any("comments[0].at: expected a timestamp" in e
                            for e in errors), errors)

    def test_a_naive_or_aware_stamp_both_load(self) -> None:
        """The app writes naive local; a hand-edited UTC offset must survive."""
        aware = self.card(created=datetime.datetime(2026, 11, 23, 12, 0,
                                                    tzinfo=datetime.timezone.utc))
        self.assertEqual(self.errors_for(aware), [])

    def test_a_custom_field_nobody_declared_is_refused(self) -> None:
        errors = self.errors_for(self.card(fields={"mystery": "x"}))
        self.assertTrue(any("unknown custom field 'mystery'" in e for e in errors), errors)

    def test_a_card_with_no_id_is_reported_as_an_orphan_not_a_crash(self) -> None:
        """The id is the only link between a card and its filename.

        A file whose body lost ``id:`` has no home, so every message about it has
        to be addressable — ``card ?.id:`` names the problem instead of raising
        KeyError out of the read path and blanking the board.
        """
        errors = model.validate_card({"title": "orphan"}, self.board(),
                                     model.card_context(self.doc, self.board(), set()))
        self.assertTrue(all(e.startswith("card ?.") for e in errors), errors)
        self.assertTrue(any("?.id: expected an id like 'WEB-1'" in e for e in errors),
                        errors)


class TestWriteHelpers(Base):
    """Where a new key lands in the file: the difference between a diff and a reflow."""

    def test_a_new_key_lands_in_the_declared_order_not_at_the_bottom(self) -> None:
        """A plain ``item[k] = v`` appends, parking ``assignee`` under ``updated``.

        Every save that appends grows the bottom of the card into a junk drawer,
        and two clones appending different keys conflict on the same line.
        """
        card = {"id": "WEB-1", "title": "x", "archived": False,
                "created": T0, "updated": T0}
        model.set_field(card, "card", "assignee", "ada")
        self.assertEqual(list(card),
                         ["id", "title", "assignee", "archived", "created", "updated"])

    def test_setting_an_existing_key_does_not_move_it(self) -> None:
        card = {"id": "WEB-1", "priority": "low", "title": "x"}
        model.set_field(card, "card", "priority", "urgent")
        self.assertEqual(list(card), ["id", "priority", "title"])
        self.assertEqual(card["priority"], "urgent")

    def test_the_claim_keys_have_a_declared_order_too(self) -> None:
        """A claim written in insertion order re-reads as a rewritten block.

        ``preempted`` and ``overridden_by`` are added long after ``agent``/``at``/
        ``expires``, so without a declared order they land under ``updated`` and a
        lease renewal diffs like a rewrite of the whole mapping.
        """
        claim: dict = {"agent": "ada", "at": T0, "expires": T0 + HOUR}
        model.set_field(claim, "claim", "preempted", "grigory")
        model.set_field(claim, "claim", "paths", ["src/**"])
        rank = {key: index for index, key in enumerate(model.CLAIM_KEYS)}
        self.assertEqual(sorted(claim, key=rank.get), list(claim),
                         f"claim keys out of declared order: {list(claim)}")
        self.assertEqual(list(claim), ["agent", "at", "expires", "paths", "preempted"])

    def test_set_list_swaps_items_rather_than_the_list(self) -> None:
        """Replacing the list throws away the node, flow style and its comment."""
        card = {"labels": ["bug"]}
        model.set_list(card, "card", "labels", ["bug", "docs"])
        self.assertEqual(card["labels"], ["bug", "docs"])

    def test_next_card_id_outranks_every_id_it_was_shown(self) -> None:
        """A hand-written id must never be handed out again.

        Two clones can still mint the same id — that is a duplicate filename for
        ``conflicts`` — but one clone re-issuing an id already on disk would make
        two cards fight over one path, which is not a merge, it is a loss.
        """
        board = copy.deepcopy(self.board())
        issued = model.next_card_id(board, ["WEB-9", "OPS-40", "WEB-2"])
        self.assertEqual(issued, "WEB-10")
        self.assertEqual(board["next_card"], 11)
        self.assertEqual(model.next_card_id(board), "WEB-11")

    def test_a_junk_counter_still_mints_the_next_free_id(self) -> None:
        board = copy.deepcopy(self.board())
        board["next_card"] = "banana"
        self.assertEqual(model.next_card_id(board, ["WEB-3"]), "WEB-4")

    def test_an_exhausted_sequence_is_refused_rather_than_truncated(self) -> None:
        """The id grammar is nine digits wide; past it, minting must stop.

        A hand-written card at the ceiling leaves the counter nothing to give.
        Re-issuing ``WEB-999999999`` would point two cards at one path, so the
        refusal has to come from ``next_card_id`` itself — ``validate`` passes,
        because the counter is in range and it is the minted id that is not.
        """
        board = copy.deepcopy(self.board())
        board["next_card"] = 2
        with self.assertRaises(ValidationError) as caught:
            model.next_card_id(board, ["WEB-999999999"])
        self.assertIn("sequence exhausted", str(caught.exception))

    def test_a_board_at_the_ceiling_still_validates(self) -> None:
        """999999999 is legal to hold, and only illegal to hand out."""
        board = copy.deepcopy(self.board())
        board["next_card"] = 10 ** 9 - 1
        self.assertEqual(model.validate({**self.doc, "boards": [board]}), [])

    def test_slugify(self) -> None:
        self.assertEqual(model.slugify("In Progress"), "in-progress")
        self.assertEqual(model.slugify("  !!! "), "x")
        self.assertEqual(model.slugify("2 fast"), "x-2-fast")
        self.assertTrue(model.SLUG_RE.match(model.slugify("Québec/On-line")))
        self.assertTrue(model.SLUG_RE.match(model.slugify("!!!", "board")))

    def test_a_punctuation_fallback_cannot_leak_through_as_a_slug(self) -> None:
        """``SLUG_RE`` wants an alphanumeric first character, and every caller
        assumes that.

        A fallback of ``"-"`` used to survive sanitising and come back as ``"-"``:
        an id that validate then refuses, so the column or board the caller just
        made could never be written.
        """
        for fallback in ("-", "_", "--", "__", "", "!!!"):
            produced = model.slugify("!!!", fallback)
            self.assertTrue(model.SLUG_RE.match(produced),
                            f"fallback {fallback!r} produced {produced!r}")

    def test_board_key_derives_and_dedupes(self) -> None:
        self.assertEqual(model.board_key("Website"), "WEB")
        self.assertEqual(model.board_key("Website", {"WEB"}), "WE2")
        self.assertTrue(model.KEY_RE.match(model.board_key("")))

    def test_unique_id(self) -> None:
        self.assertEqual(model.unique_id("bug", set()), "bug")
        self.assertEqual(model.unique_id("bug", {"bug", "bug-2"}), "bug-3")
        with self.assertRaises(ValidationError):
            model.unique_id("Bug!", set())

    def test_iso_never_puts_a_space_where_the_t_belongs(self) -> None:
        """``str(datetime)`` writes ``2026-11-22 19:50:48``; ``Date.parse`` rejects it."""
        stamp = datetime.datetime(2026, 11, 22, 19, 50, 48)
        self.assertEqual(model.iso(stamp), "2026-11-22T19:50:48")
        self.assertEqual(model.iso(datetime.date(2026, 12, 1)), "2026-12-01")
        self.assertEqual(model.iso("as written"), "as written")
        self.assertEqual(model.jsonable({"due": datetime.date(2026, 12, 1)}),
                         {"due": "2026-12-01"})

    def test_jsonable_detaches_the_live_document(self) -> None:
        """A payload on its way to one client must not be edited by the next op."""
        card = self.card()
        wire = model.jsonable({"cards": [card]})
        card["title"] = "edited by the next request"
        self.assertEqual(wire["cards"][0]["title"], "Fix the deploy timeout")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
