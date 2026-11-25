"""Git plumbing behind the toolbar: what `status` reports and what `commit` may touch.

Covers the read path (an ignored board, the upstream-less `ahead`, an ambient
`GIT_DIR`, a repo that executes `core.fsmonitor`, a crashed git's index.lock) and
the write path (only the board file, never someone else's staged work, never a
message that is empty or absurd).  Nothing here talks HTTP; `tests/test_server.py`
owns the button, this owns the git.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yamlboard import gitops

STARTER = """\
version: 1
title: Test
rev: 0
people: []
labels: []
boards:
  - id: web
    key: WEB
    title: Website
    columns:
      - {id: backlog, name: Backlog}
    next_card: 1
    cards: []
"""

# The fixture's own git calls: a developer machine with commit.gpgSign=true or a
# global core.fsmonitor must not change what a test observes, in either direction.
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


class GitRepo(unittest.TestCase):
    """A throwaway committed repository per test, plus a board file inside it.

    Isolation is load-bearing: these tests stage, commit and leave lock files
    behind, and a shared repo would make the counts (``rev-list --count``) and
    the "nothing to commit" branches depend on test order.  ``.git`` is written
    by the test itself, so nothing on the developer's machine -- not
    ``~/.gitconfig``, not an ``include`` in it -- can steer a result.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # resolve(): gitops resolves the board path, and macOS names the temp dir
        # through a symlink (/var -> /private/var) that a root comparison sees.
        self.dir = Path(self._tmp.name).resolve()
        self.repo = self.dir / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "commit.gpgsign", "false")
        self.file = self.repo / "boards.yaml"
        self.write_board(STARTER)
        git(self.repo, "add", "boards.yaml")
        git(self.repo, "commit", "-qm", "seed")

    # -- helpers ---------------------------------------------------------- #

    def write_board(self, text: str) -> None:
        self.file.write_text(text, encoding="utf-8")

    def commit_count(self) -> int:
        return int(git(self.repo, "rev-list", "--count", "HEAD"))

    def head_files(self) -> list:
        return git(self.repo, "show", "--name-only", "--format=", "HEAD").splitlines()

    def outside_repo(self, name: str = "elsewhere") -> Path:
        """A second repo in the same temp dir, so a root can be *wrong* by test."""
        other = self.dir / name
        other.mkdir()
        git(other, "init", "-q", "-b", "other-branch")
        # Identity, as in setUp: callers commit into this repo, and git without
        # a configured identity works on macOS (it guesses from hostname) but
        # exits 128 on the CI runner -- a decoy that cannot be committed to is
        # not a decoy.
        git(other, "config", "user.email", "t@example.com")
        git(other, "config", "user.name", "Test")
        git(other, "config", "commit.gpgsign", "false")
        return other


class TestIgnoredBoard(GitRepo):
    def test_a_gitignored_board_with_edits_is_still_dirty(self) -> None:
        """Without ``--ignored=matching`` the board is invisible, not clean.

        A board the user gitignored (a private file in a public repo) produced no
        porcelain record at all, so the toolbar said "Committed" forever while
        every edit sat unsaved.
        """
        (self.repo / ".gitignore").write_text("boards.yaml\n", encoding="utf-8")
        git(self.repo, "add", ".gitignore")
        git(self.repo, "commit", "-qm", "ignore the board")
        self.write_board(STARTER + "# an edit nobody can see in git status\n")

        status = gitops.status(self.file)

        self.assertTrue(status.is_repo)
        self.assertTrue(status.is_dirty, status.as_dict())
        self.assertEqual(status.changed, ["boards.yaml"])
        self.assertIsNone(status.error)

    def test_a_committed_board_with_no_edits_is_clean(self) -> None:
        """The control: dirty has to mean dirty, or the flag is just noisy."""
        status = gitops.status(self.file)

        self.assertTrue(status.is_repo)
        self.assertFalse(status.is_dirty, status.as_dict())
        self.assertEqual(status.changed, [])


class TestUpstream(GitRepo):
    def test_a_branch_with_no_upstream_has_no_ahead_count(self) -> None:
        """``ahead`` is None, not 0: "no upstream" and "nothing to push" differ."""
        status = gitops.status(self.file)

        self.assertTrue(status.is_repo)
        self.assertEqual(status.branch, "main")
        self.assertIsNone(status.ahead, status.as_dict())

    def test_commits_not_yet_pushed_are_counted_once_there_is_an_upstream(self) -> None:
        """The control for the test above, so None cannot mean "never computed"."""
        remote = self.dir / "origin.git"
        git(remote.parent, "init", "-q", "--bare", remote.name)
        git(self.repo, "remote", "add", "origin", str(remote))
        git(self.repo, "push", "-q", "-u", "origin", "main")
        self.assertEqual(gitops.status(self.file).ahead, 0)
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "ahead of origin")

        status = gitops.status(self.file)

        self.assertEqual(status.ahead, 1, status.as_dict())
        self.assertFalse(status.is_dirty, status.as_dict())


class TestCommitScoping(GitRepo):
    def test_a_foreign_staged_file_refuses_the_commit(self) -> None:
        """Commit takes the index the ``add`` built, so the index must be ours.

        The button stages one path and then commits the index; a file someone
        else had already staged would ride along in a commit labelled "boards".
        """
        self.write_board(STARTER + "# mine\n")
        (self.repo / "other.txt").write_text("someone else's work", encoding="utf-8")
        git(self.repo, "add", "other.txt")
        before = self.commit_count()

        with self.assertRaises(gitops.GitError) as caught:
            gitops.commit(self.file, "boards(test): must not happen")

        self.assertIn("other.txt", str(caught.exception))
        self.assertEqual(self.commit_count(), before)
        self.assertIn("other.txt", git(self.repo, "diff", "--cached", "--name-only"))

    def test_commit_lands_the_board_file_and_leaves_another_change_alone(self) -> None:
        (self.repo / "notes.md").write_text("first\n", encoding="utf-8")
        git(self.repo, "add", "notes.md")
        git(self.repo, "commit", "-qm", "notes")
        self.write_board(STARTER + "# card added\n")
        (self.repo / "notes.md").write_text("second\n", encoding="utf-8")

        result = gitops.commit(self.file, "boards(test): one card")

        self.assertTrue(result["committed"], result)
        self.assertTrue(result["sha"])
        self.assertEqual(self.head_files(), ["boards.yaml"])
        self.assertEqual(self.commit_count(), 3)     # seed + notes + the button
        self.assertIn("notes.md", git(self.repo, "status", "--porcelain=v1"))
        self.assertEqual((self.repo / "notes.md").read_text(encoding="utf-8"), "second\n")
        self.assertFalse(gitops.status(self.file).is_dirty)

    def test_nothing_to_commit_is_a_result_and_not_an_error(self) -> None:
        result = gitops.commit(self.file, "boards(test): nothing changed")

        self.assertFalse(result["committed"])
        self.assertEqual(result["reason"], "nothing to commit")
        self.assertEqual(self.commit_count(), 1)

    def test_an_empty_message_is_refused_before_git_is_asked(self) -> None:
        self.write_board(STARTER + "# pending\n")

        with self.assertRaises(gitops.GitError) as caught:
            gitops.commit(self.file, "   ")

        self.assertIn("message is required", str(caught.exception))
        self.assertEqual(self.commit_count(), 1)

    def test_an_absurdly_long_message_is_refused(self) -> None:
        self.write_board(STARTER + "# pending\n")

        with self.assertRaises(gitops.GitError) as caught:
            gitops.commit(self.file, "boards(test): " + "x" * 2000)

        self.assertIn("too long", str(caught.exception))
        self.assertEqual(self.commit_count(), 1)

    def test_the_subject_names_the_tool_not_a_single_file(self) -> None:
        """The board is a directory now, so no one file's stem describes the commit.

        ``boards(boards.yaml):`` was honest when there was one file; a log line
        naming ``WEB-3`` would describe a fifth of a commit that moved four cards.
        """
        self.write_board(STARTER + "# pending\n")

        gitops.commit(self.file, gitops.default_message(self.file, "ship   the  release"))

        self.assertEqual(git(self.repo, "log", "-1", "--format=%s"),
                         "yamlboard: ship the release")


class TestOutsideRepo(GitRepo):
    def test_a_root_that_does_not_contain_the_board_is_reported_not_ok(self) -> None:
        """``_relative`` is the guard between "root" and "our pathspec".

        Everything git runs is pathspec-scoped to the board; if the board is not
        under the root, a bare ``git commit`` would commit that other tree.
        """
        other = self.outside_repo()

        with mock.patch.object(gitops, "repo_root", return_value=other):
            status = gitops.status(self.file)
            with self.assertRaises(gitops.GitError) as caught:
                gitops.commit(self.file, "boards(test): elsewhere")

        self.assertIn("outside this git repository", status.error or "")
        self.assertIn("outside this git repository", str(caught.exception))
        self.assertEqual(git(other, "rev-list", "--all", "--count"), "0")

    def test_a_board_symlinked_out_of_the_repo_is_never_committed_there(self) -> None:
        """The natural version of the test above: resolve() escapes the repo."""
        elsewhere = self.dir / "elsewhere"
        elsewhere.mkdir()
        real = elsewhere / "real.yaml"
        real.write_text(STARTER, encoding="utf-8")
        (self.repo / "link.yaml").symlink_to(real)

        status = gitops.status(self.repo / "link.yaml")
        with self.assertRaises(gitops.GitError):
            gitops.commit(self.repo / "link.yaml", "boards(test): through a link")

        self.assertFalse(status.is_repo, status.as_dict())
        self.assertEqual(self.commit_count(), 1)


class TestHostileEnvironment(GitRepo):
    def test_ambient_git_dir_does_not_steer_the_status(self) -> None:
        """A stray ``GIT_DIR`` in the shell would talk about another repository.

        The toolbar would then show a branch and a dirty flag for a repo nobody
        is looking at, and Commit would write there.
        """
        decoy = self.outside_repo("decoy")
        (decoy / "boards.yaml").write_text("not your board\n", encoding="utf-8")
        git(decoy, "add", "boards.yaml")
        git(decoy, "commit", "-qm", "decoy seed")
        self.write_board(STARTER + "# edited\n")

        with mock.patch.dict(os.environ, {"GIT_DIR": str(decoy / ".git"),
                                         "GIT_WORK_TREE": str(decoy),
                                         "GIT_INDEX_FILE": str(decoy / ".git" / "index")}):
            status = gitops.status(self.file)

        self.assertEqual(status.root, str(self.repo))
        self.assertEqual(status.branch, "main", status.as_dict())
        self.assertEqual(status.changed, ["boards.yaml"], status.as_dict())

    def test_status_does_not_execute_the_repos_core_fsmonitor(self) -> None:
        """``git status`` runs ``core.fsmonitor``, so serving ran arbitrary code.

        The marker proves the hook is wired up and *would* run: the same call
        without ``-c core.fsmonitor=false`` creates it.
        """
        marker = self.dir / "marker"
        hook = self.dir / "fsmonitor.sh"
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
        hook.chmod(0o755)
        git(self.repo, "config", "core.fsmonitor", str(hook))

        git(self.repo, "status", "--porcelain=v2")
        self.assertTrue(marker.exists(), "the fixture's fsmonitor hook never ran")
        marker.unlink()

        status = gitops.status(self.file)

        self.assertFalse(marker.exists(), "status() ran the repo's fsmonitor hook")
        self.assertIsNone(status.error)

    def test_a_stale_index_lock_is_one_line_about_another_git_command(self) -> None:
        """A SIGKILL mid-commit leaves index.lock; git's own text is six lines.

        The UI shows this in a toast, so the recognised case is reworded instead
        of pasted -- and it has to stay on one line, per P35.
        """
        self.write_board(STARTER + "# edited\n")
        (self.repo / ".git" / "index.lock").write_text("", encoding="utf-8")

        with self.assertRaises(gitops.GitError) as caught:
            gitops.commit(self.file, "boards(test): after a crash")

        message = str(caught.exception)
        self.assertEqual(len(message.splitlines()), 1, message)
        self.assertIn("another git command is running", message)
        self.assertNotIn("index.lock': File exists", message)
        (self.repo / ".git" / "index.lock").unlink()
        self.assertTrue(gitops.commit(self.file, "boards(test): recovered")["committed"])

    def test_the_board_path_is_never_reached_by_the_shell(self) -> None:
        """A filename with a quote and spaces is a pathspec, not a command line."""
        awkward = self.repo / "my board's file.yaml"
        awkward.write_text(STARTER, encoding="utf-8")

        status = gitops.status(awkward)

        self.assertEqual(status.changed, ["my board's file.yaml"], status.as_dict())
        self.assertTrue(gitops.commit(awkward, "boards(boards): awkward name")["committed"])
        self.assertEqual(self.head_files(), ["my board's file.yaml"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
