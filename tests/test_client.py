"""The browser's coordination rules, run against the shipped ``app.js``.

Everything here is a decision the client makes *between* polls, which is exactly
the part no HTTP test can see: whether a lease moving looks like a human edit,
whether a countdown reads the server's clock or the laptop's, whether a claim
whose holder vanished renders or throws. The server-side half of each of these
is pinned in ``test_api.py``; this file is the half that only exists in JS.

The harness (``client_harness.mjs``) evaluates the real ``app.js`` in a stub DOM
and prints what the real functions returned. It is not a re-typed copy of the
logic: change ``app.js`` and these answers change with it. A few assertions are
source-level instead, where the property is "this text exists in two files",
which no amount of execution can establish.

The deliberate weakness: without Node there is nothing to run, and the whole
class skips. That is honest rather than fatal — the project ships no JS
toolchain, so the browser guarantees are proven by a live run in the absence of
Node and pinned by this file in its presence. ``test_the_harness_actually_runs_app_js``
is what stops the skip from hiding a harness that silently stopped loading the
file.
"""

from __future__ import annotations

import json
import re
import tempfile
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP_JS = REPO / "src" / "yamlboard" / "static" / "app.js"
STYLES = REPO / "src" / "yamlboard" / "static" / "styles.css"
README = REPO / "README.md"
SKILL = REPO / "src" / "yamlboard" / "skills" / "yamlboard" / "SKILL.md"
HARNESS = Path(__file__).resolve().parent / "client_harness.mjs"


NODE = shutil.which("node")
needs_node = unittest.skipUnless(NODE, "no node on PATH: app.js cannot be executed")

def run_harness(app: Path = APP_JS) -> dict:
    """Evaluate ``app`` in the harness and return its observed answers."""
    proc = subprocess.run([NODE, str(HARNESS), str(app)],
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(
            f"the harness failed to run {app.name} (exit {proc.returncode}):\n{proc.stderr[-2000:]}")
    assert proc.stdout.strip(), "the harness printed nothing"
    return json.loads(proc.stdout)


@needs_node
class TestClientBehaviour(unittest.TestCase):
    """What the shipped client actually returns for a shaped payload."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.obs = run_harness()

    # -- the classifier that decides "changed on disk" ----------------------- #

    def test_a_lease_moving_is_not_reported_as_a_human_edit(self) -> None:
        """Claim sets ``assignee`` and release clears it, so a lease wears a
        content field's clothes. Counted verbatim, N agents' renewals put
        "changed on disk" on the human's screen every poll.
        """
        self.assertEqual(self.obs["claim_sets_assignee"], "lease")
        self.assertEqual(self.obs["release_clears_assignee"], "lease")
        self.assertEqual(self.obs["unchanged_assignee"], "same")

    def test_a_name_that_matches_nobodys_lease_is_still_an_edit(self) -> None:
        """The classifier must not become a blanket silence: assignee grigory
        while ada holds the card is a person typing, and that toast is the only
        thing that tells the reader their card changed under them.
        """
        self.assertEqual(self.obs["handedit_under_a_lease"], "edit")

    def test_a_claim_only_poll_reports_no_content(self) -> None:
        claim = self.obs["claim_only"]
        self.assertEqual(claim["claims"], ["WEB-1"])
        self.assertEqual(claim["content"], [])

    def test_a_poll_carrying_a_lease_and_an_edit_reports_the_edit(self) -> None:
        """The coarser answer wins. A card both claimed and hand-edited between
        two polls has genuinely changed on disk; letting the lease branch
        swallow it shows the reader somebody else's edit with no explanation of
        where it came from.
        """
        edit = self.obs["human_edit"]
        self.assertEqual(edit["content"], ["WEB-1"],
                         "a hand edit sharing a poll with a claim was swallowed")

    def test_a_new_card_is_reported_as_added_not_content(self) -> None:
        self.assertEqual(self.obs["card_appeared"], ["WEB-2"])

    # -- the countdown ------------------------------------------------------- #

    def test_the_countdown_uses_the_servers_clock_not_this_laptops(self) -> None:
        """`claim.left` is measured on the machine that owns the lease, so the
        local tick is pinned to it. Without the offset a laptop four minutes
        fast renders "26m left" for a 30m lease, and the human frees work an
        agent still believes it holds.
        """
        self.assertTrue(self.obs["skew_zeroed_to_server_frame"])

    def test_a_board_with_no_live_claim_keeps_its_last_offset(self) -> None:
        """Zeroing with nothing to measure against would make a board where
        every agent just finished tick a stale countdown further wrong rather
        than leave nothing on screen.
        """
        self.assertTrue(self.obs["skew_kept_without_a_claim"])

    def test_remaining_time_is_clamped_at_zero_and_spelled_like_the_cli(self) -> None:
        self.assertEqual(self.obs["human_left"],
                         ["0s", "0s", "59s", "1m", "2m", "1h", "1h1m", "2h2m"])

    def test_the_writer_stamps_microseconds_the_parser_must_tolerate(self) -> None:
        """ruamel emits microseconds; a silently-NaN parse renders every
        countdown as expired, which is worse than an ugly one.
        """
        self.assertTrue(self.obs["stamps"]["micros"])
        self.assertTrue(self.obs["stamps"]["naive"])
        self.assertTrue(self.obs["stamps"]["space"])
        self.assertIsNone(self.obs["stamps"]["junk"])

    def test_amber_arrives_on_the_first_frame_not_one_tick_later(self) -> None:
        """A payload that lands with the lease already under a quarter of its
        own TTL must not render healthy and change colour while the reader is
        watching it. The threshold is the claim's own span, so a 1h lease and a
        5m lease on the same board warn at different absolute times.
        """
        self.assertIn("low", self.obs["nearly"]["classes"])
        self.assertNotIn("low", self.obs["healthy"]["classes"])

    # -- the claim chip ------------------------------------------------------ #

    def test_a_holder_the_board_never_heard_of_renders_rather_than_throws(self) -> None:
        chip = self.obs["ghost"]
        self.assertIsNotNone(chip)
        self.assertIn("clashing", chip["classes"])
        self.assertIn("nobody", chip["title"])

    def test_an_unknown_holder_falls_back_to_the_neutral_colour(self) -> None:
        """A person on the board colours their chip; an unknown id must not
        inherit the previous card's colour or paint nothing at all.
        """
        self.assertEqual(self.obs["nearly"]["claim"], "#2f6f4f")
        self.assertNotEqual(self.obs["ghost"]["claim"], "#2f6f4f")

    def test_a_card_with_no_claim_builds_no_chip(self) -> None:
        self.assertTrue(self.obs["no_claim_agent"])

    def test_expired_and_overridden_read_as_different_news(self) -> None:
        """Both are dead leases, but one is free work and one is a ruling the
        human has to notice; a single "expired" label would erase the second.
        """
        self.assertIn("overridden", self.obs["overridden"]["text"])
        self.assertIn("expired", self.obs["expired"]["text"])

    def test_a_collision_names_both_agents_and_both_patterns(self) -> None:
        line = self.obs["clash_both"]
        for token in ("ada", "grigory", "src/api/**", "src/api/routes.py"):
            self.assertIn(token, line)

    def test_an_uninvolved_card_is_quiet_about_someone_elses_collision(self) -> None:
        self.assertEqual(self.obs["clash_uninvolved"], 0)

    # -- expiry announcements ------------------------------------------------ #

    def test_opening_a_board_dumps_no_backlog_of_expiries(self) -> None:
        self.assertEqual(self.obs["backlog_on_first_load"], 0)

    def test_one_expiry_toasts_once_and_then_stays_quiet(self) -> None:
        self.assertEqual(self.obs["first_expiry_toasts"], 1)
        self.assertEqual(self.obs["repeat_same_expiry"], 1, "the same expiry toasted twice")
        self.assertEqual(self.obs["stays_quiet_after_one_death"], 1)

    def test_a_claim_taken_again_after_its_predecessor_died_is_news_again(self) -> None:
        """Dedupe is per claim, not per card: a retaken card carries a new `at`,
        so its own eventual expiry has to be announced like any other.
        """
        self.assertEqual(self.obs["after_retake_new_at"], 2)

    # -- the identity picker ------------------------------------------------- #

    def test_the_toolbar_picker_offers_people_and_nothing_else(self) -> None:
        """No free text: an identity the file has never heard of is a hard
        refusal downstream, so a control that could type one is a control whose
        only output is an error.
        """
        pick = self.obs["select"]
        self.assertTrue(pick["isSelect"])
        self.assertIn("c", pick["aria"])
        self.assertEqual([name for _, name in pick["options"]], ["Ada", "Grigory"])

    def test_the_chosen_identity_is_persisted(self) -> None:
        """Two people reading one board must be able to be different agents
        without either editing a committed file.
        """
        self.assertEqual(self.obs["select"]["stored"], "ada")

    def test_the_modals_picker_actually_changes_who_claims(self) -> None:
        """The one place a person chooses who to act as inside a card. If its
        value goes nowhere, every claim from the modal is made as whoever the
        toolbar happens to say.
        """
        modal = self.obs["modal_free_text"]
        self.assertTrue(modal["handled"], "the modal's select has no handler at all")
        self.assertEqual(modal["afterPick"], "grigory")
        self.assertEqual(modal["stored"], "grigory", "the pick was not persisted")

    def test_switching_identity_rebuilds_the_moves_offered(self) -> None:
        """Renew has to disappear when you stop wearing the holder's name, or
        the card offers an action the server will refuse.
        """
        both = self.obs["modal_renew_follows_identity"]
        self.assertIn("Renew", both["asHolder"])
        self.assertNotIn("Renew", both["asStranger"])

    def test_switching_identity_does_not_discard_paths_being_typed(self) -> None:
        """The paths box is built next to the picker; rebuilding the row around
        it would silently throw away a glob typed a second earlier.
        """
        modal = self.obs["modal_free_text"]
        self.assertTrue(modal["pathsKept"], "the paths input was replaced on repaint")
        self.assertEqual(modal["pathsValue"], "src/api/**")


@needs_node
class TestTheHarnessItself(unittest.TestCase):
    """The harness must be able to fail, or every test above is decoration."""

    def test_the_harness_actually_runs_app_js(self) -> None:
        """Mutate one line of the real file and the answers must move. Without
        this, a harness that stopped loading app.js — wrong path, stub that
        no-ops — would report a plausible constant forever and every test in
        this file would still be green.
        """
        original = APP_JS.read_text(encoding="utf-8")
        self.assertIn('return agrees(before) && agrees(now) ? "lease" : "edit";', original,
                      "movedFiles' classifier changed shape; update this mutation")
        broken = original.replace(
            'return agrees(before) && agrees(now) ? "lease" : "edit";',
            'return "lease";')
        self.assertNotEqual(broken, original)
        scratch = Path(self.enterContext(tempfile.TemporaryDirectory()))
        mutant = scratch / "app.js"
        mutant.write_text(broken, encoding="utf-8")
        observed = run_harness(mutant)
        real = run_harness()
        self.assertEqual(real["handedit_under_a_lease"], "edit")
        self.assertEqual(observed["handedit_under_a_lease"], "lease",
                         "the mutant should misread a hand edit as a lease")
        self.assertNotEqual(observed, real, "app.js has no effect on the harness output")

    def test_the_harness_loads_the_shipped_file_not_a_fork(self) -> None:
        """The harness takes app.js as an argument; a default pointing elsewhere
        would test a copy that rots.
        """
        source = HARNESS.read_text(encoding="utf-8")
        self.assertIn("process.argv[2]", source)
        self.assertIn('console.error("usage:', source)


class TestClientContracts(unittest.TestCase):
    """Text that has to exist in two files at once — no execution can prove it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.js = APP_JS.read_text(encoding="utf-8")
        cls.css = STYLES.read_text(encoding="utf-8")
        cls.readme = README.read_text(encoding="utf-8")
        cls.skill = SKILL.read_text(encoding="utf-8")

    def test_every_shortcut_exists_in_the_handler_the_help_panel_and_the_readme(self) -> None:
        """One list in three formats, edited by hand. A key in the handler and
        not in the panel is invisible; in the README and not the handler is a
        line that teaches a lie.
        """
        handler = set(re.findall(r'key === "([a-z/?])"', self.js))

        panel_block = self.js[self.js.index("function openHelp()"):]
        panel_block = panel_block[:panel_block.index("\n}") + 2]
        # read the key cell only: "click a pill" and "Enter on a card" are prose,
        # and mining their letters would let this test pass on a panel missing `a`.
        panel = set()
        for cell in re.findall(r'\["([^"]*)",\s*"', panel_block):
            for token in cell.split(" / "):
                token = token.strip().lower()
                if len(token) == 1:
                    panel.add(token)

        readme_line = self.readme[self.readme.index("`n` new card"):]
        readme_line = readme_line[:readme_line.index("shortcuts · `Esc`")]
        readme = set(re.findall(r"`([a-z/?])`", readme_line))
        for key in ("n", "/", "a", "b", "l", "d", "f", "s", "t", "?", "r", "c"):
            self.assertIn(key, handler, f"{key} is documented but not handled")
            self.assertIn(key, panel, f"{key} is handled but not in the help panel")
            self.assertIn(key, readme, f"{key} is handled but missing from the README")

    def test_the_coordination_state_is_a_class_and_not_a_data_attribute(self) -> None:
        for name in ("claimed", "overlaps", "blocked", "ready-look"):
            self.assertIn(f'"{name}"', self.js, f".{name} is no longer set from JS")
            self.assertIn(f".{name}", self.css, f".{name} is set but never styled")

    def test_the_claim_token_is_declared_in_both_themes_with_measured_contrast(self) -> None:
        """A colour that only exists in one theme is white-on-white in the
        other, and the ratio in the comment is the only record of what it was
        chosen against.
        """
        self.assertIn("--claim:", self.css)
        dark = self.css[self.css.index('[data-theme="dark"]")'):] if False else \
            self.css[self.css.index('[data-theme="dark"]'):]
        self.assertIn("--claim:", dark, "--claim is missing from the dark theme")
        light = self.css[:self.css.index('[data-theme="dark"]')]
        for block, label in ((light, "light"), (dark[:dark.index("}")] + "}", "dark")):
            around = block[max(0, block.index("--claim:") - 400):block.index("--claim:")]
            self.assertRegex(around, r"\d\.\d+:\s*1",
                             f"--claim in the {label} theme has no measured ratio in its comment")

    def test_js_only_sets_the_sanctioned_custom_properties(self) -> None:
        """P29: a property set from JS but not named in the sanctioned comment
        gets deleted as dead code, taking the claim colour with it.
        """
        set_from_js = set(re.findall(r'setProperty\(\s*["\'](--[\w-]+)', self.js))
        set_from_js |= set(re.findall(r'"(--[\w-]+)":', self.js))
        sanctioned = {"--col", "--prio", "--c", "--who", "--claim"}
        self.assertEqual(set_from_js - sanctioned, set(),
                         "app.js sets a custom property P29 does not sanction")
        self.assertIn("--claim", self.js[self.js.index("sanctioned set"):
                                        self.js.index("sanctioned set") + 400],
                      "the sanctioned-set comment no longer names --claim")

    def test_the_claim_chip_survives_the_compact_density(self) -> None:
        """The point of its own class: .prio/.due/.est/.meta vanish at minimal,
        and a chip built as one of those would disappear exactly when space —
        and knowing who holds a card — matters most.
        """
        hidden = re.search(r'\[data-density="minimal"\][^{]*\{[^}]*display:\s*none',
                           self.css).group()
        for name in ("prio", "due", "est", "meta"):
            self.assertIn(name, hidden)
        self.assertNotIn(".claim", hidden, "the claim chip is hidden at minimal density")

    def test_the_skill_does_not_promise_a_file_count_the_tool_never_counts(self) -> None:
        """A stated count would be a promise the CLI cannot keep: the refusal
        says "every file under it" because nothing in the store enumerates the
        tree, and a pattern naming a file that does not exist yet has to stay
        claimable — an agent writing the file *is* the reason for the claim.
        """
        self.assertNotIn("41 files", self.skill)
        refusal = (REPO / "src" / "yamlboard" / "api.py").read_text(encoding="utf-8")
        self.assertIn("it would claim every file", refusal)
        self.assertIn("A bare directory is refused", self.skill)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
