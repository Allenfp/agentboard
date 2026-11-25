"""Commit the data file, so board history is git history.

Deliberately narrow: git runs only through :func:`_run`, with a fixed argv list,
no shell, and always rooted at the directory :func:`repo_root` found.  The
caller picks that directory by launching yamlboard against a file inside a repo
they own.  That is a *convention*, not a sandbox: a repo you did not write can
still run code through git, so every call passes ``-c core.fsmonitor=false`` --
``git status`` executes ``core.fsmonitor`` and this module calls ``status`` on
every poll, which made merely *serving* a board a code-execution path.

Repo hooks (``.git/hooks``, ``core.hooksPath``) still run on Commit, on purpose:
a hook the user installed is theirs, and skipping it to guard against a hostile
repo would also switch off a check they asked for.  Pointing yamlboard at a
directory you do not control is therefore unsupported, full stop.  ``_env``
drops the ambient variables that would point git at another repository.
"""

from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

GIT_TIMEOUT = 20

# Ambient git state is not ours to honour. ``GIT_DIR``/``GIT_WORK_TREE`` would
# make every call talk about a different repository than the one on screen;
# the config files could re-point fsmonitor or re-enable prompts.
_DROP_ENV = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_INDEX_VERSION",
)


class GitError(Exception):
    """git is missing, or refused to do what we asked."""


@dataclass
class RepoStatus:
    is_repo: bool = False
    is_dirty: bool = False
    branch: str | None = None
    changed: list[str] = field(default_factory=list)
    ahead: int | None = None
    root: str | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "is_repo": self.is_repo,
            "is_dirty": self.is_dirty,
            "branch": self.branch,
            "changed": self.changed,
            "ahead": self.ahead,
            "root": self.root,
            "error": self.error,
        }


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _DROP_ENV}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"          # read-only calls must not take the index lock
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def _config() -> list[str]:
    """``-c`` overrides that make a surprising repo inert on the read path."""
    return ["-c", "core.fsmonitor=false"]


def _identity_args(root: Path) -> list[str]:
    """Pass the committer's own configured identity to a write.

    ``_env`` points ``GIT_CONFIG_GLOBAL`` at ``/dev/null`` so a stray global
    config cannot hijack a read, but a commit needs the user's identity, and
    without it git either refuses (Linux: exit 128) or invents one from
    hostname+username (macOS: a real commit authored ``<user>@<host>.local``).
    Reading the effective value through :func:`config_value` — the same lookup
    the identity resolver uses — and re-applying it with ``-c`` gives git back
    the person it would have used anyway, precedence intact: ``config_value``
    already returns repo-local over global, so where a repo *has* a local
    identity, injecting that same value changes nothing. If neither is set, this
    returns empty and git refuses with its own ``who you are`` message, which is
    honest; silently forging an identity is not.
    """
    args: list[str] = []
    for key in ("user.name", "user.email"):
        try:
            value = config_value(root, key)
        except GitError:
            return args            # partial identity is worse than none
        if value:
            args += ["-c", f"{key}={value}"]
    return args


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """One git call, killed politely if it wedges.

    A SIGKILL mid-``git commit`` leaves ``.git/index.lock`` behind, which then
    breaks the *user's* own git as well as ours, so the process group gets
    SIGTERM first and only then SIGKILL.
    """
    argv = [args[0], *_config(), *args[1:]]
    with subprocess.Popen(
        argv,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        stdin=subprocess.DEVNULL,
        env=_env(),
        start_new_session=True,
    ) as proc:
        try:
            out, err = proc.communicate(timeout=GIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            _terminate(proc)
            out, err = proc.communicate()
            detail = _one_line(err) or " ".join(argv)
            raise GitError(f"git timed out after {GIT_TIMEOUT}s: {detail}") from None
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _terminate(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()


def _one_line(text: str | None) -> str:
    """First non-empty line of git's output, which is all the UI should ever show."""
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _friendly(stderr: str) -> str:
    """git's message, shortened; the lock case in particular is worth naming."""
    flat = (stderr or "").strip()
    if "index.lock" in flat and ("Unable to create" in flat or "cannot lock" in flat.lower()):
        return "another git command is running (git's index.lock exists) - try again in a moment"
    return _one_line(flat)


# Tokens before the trailing path, per porcelain-v2 record type (the path is the
# only field that may contain spaces, hence bounding the split rather than
# guessing an offset): "1 <XY> <sub> <3 modes> <2 oids> <path>", etc.
_PATH_FIELD = {"1": 8, "u": 9, "2": 9, "?": 1, "!": 1}


def _path_field(entry: str) -> str:
    """The trailing path of a porcelain-v2 record, spaces and all."""
    parts = entry.split(" ", _PATH_FIELD.get(entry[:1], 1))
    return parts[-1]


def _count(fields: list[str]) -> int | None:
    """The "+N" of a ``# branch.ab +N -M`` line, or ``None`` if unreadable."""
    if not fields or not fields[0].startswith("+"):
        return None
    try:
        return int(fields[0][1:])
    except ValueError:
        return None


def repo_root(data_file: Path) -> Path | None:
    """Nearest directory containing ``.git`` at or above ``data_file``.

    ``data_file`` itself is a candidate: a store committed at the repository
    root is the common layout, and scanning only ``.parents`` reported "no
    repository" for exactly that tree -- which silently switched off the
    ``git config user.email`` identity fallback and hid a working tree that
    was sitting right there.
    """
    data_file = data_file.resolve()
    for candidate in (data_file, *data_file.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _relative(data_file: Path, root: Path) -> str | None:
    """``data_file`` as seen from ``root``; ``None`` when it is not inside it."""
    try:
        relative = os.path.relpath(data_file.resolve(), root)
    except ValueError:
        return None
    if relative == ".." or relative.startswith(".." + os.sep):
        return None
    return relative


def config_value(root: Path, key: str) -> str | None:
    """One git config value, read from the *user's* own configuration.

    The only call in this module that does not go through :func:`_env`, and it has
    to be that way: ``_env`` points ``GIT_CONFIG_GLOBAL`` at ``/dev/null``
    precisely so a hostile repository cannot execute anything, and the value we
    need here — whose email is this — lives in exactly that file. Reading it is a
    lookup, not a working-tree operation: no fsmonitor, no index. The ``GIT_*``
    path overrides are still dropped, so a repository cannot redirect the read at
    another repository's config.
    """
    env = {k: v for k, v in os.environ.items() if k not in _DROP_ENV}
    env["GIT_TERMINAL_PROMPT"] = "0"
    # check=False because 1 is a normal answer here, not a failure: it means the
    # key is unset, which is how "this repo has no user.email" arrives.
    done = subprocess.run(
        ["git", "config", "--get", key], check=False,
        cwd=str(root), capture_output=True, text=True,
        stdin=subprocess.DEVNULL, env=env, timeout=GIT_TIMEOUT,
    )
    if done.returncode not in (0, 1):        # 1 simply means "not set"
        raise GitError(_friendly(done.stderr) or f"git config {key} failed")
    return done.stdout.strip() or None


def status(target: Path) -> RepoStatus:
    """Repo shape for the toolbar: branch, dirty-or-not, commits ahead.

    ``target`` is the ``.yamlboard`` directory now rather than one data file, and
    that is the whole point of the split: a pathspec on a directory makes every
    card file individually visible in git status, where the old single file made
    two agents' unrelated edits land on the same line.

    One ``git status --porcelain=v2`` call answers all three.  The flags matter:
    ``--ignored=matching`` is what makes an *ignored* board visible (``!``)
    instead of invisible, and a board the user gitignored is exactly the board
    that used to look permanently "Committed" while its edits sat unsaved.
    ``-z`` because git quotes paths containing spaces in every other mode.
    """
    root = repo_root(target)
    if root is None:
        return RepoStatus()
    relative = _relative(target, root)
    base = RepoStatus(is_repo=True, root=str(root))
    if relative is None:
        base.error = "the board directory is outside this git repository"
        return base

    done = _run(
        ["git", "status", "--porcelain=v2", "--ignored=matching", "--branch",
         "--ahead-behind", "-z", "--untracked-files=all", "--", relative], root,
    )
    if done.returncode != 0:
        base.error = _friendly(done.stderr) or "git status failed"
        return base

    entries = [chunk for chunk in done.stdout.split("\0") if chunk]
    changed: list[str] = []
    branch: str | None = None
    ahead: int | None = None
    skip = False
    # Field counts are fixed per record type, and the path is always last, so
    # split a bounded number of times: git leaves spaces inside a path alone
    # under -z, which is why slicing at a guessed offset would corrupt
    # "my board.yaml".
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if entry.startswith("# branch.head "):
            branch = entry[len("# branch.head "):].strip() or None
        elif entry.startswith("# branch.ab "):
            # "+2 -3": commits ahead of upstream, commits behind it. Absent when
            # the branch has no upstream, which is not the same as "0 ahead".
            ahead = _count(entry[len("# branch.ab "):].split())
        elif entry[:2] in ("1 ", "u ", "? ", "! ", "2 "):
            # A `2 ` rename is followed by the origin path as its own field.
            if entry[:2] == "2 ":
                index += 1
            if entry[:2] == "! ":
                # ``--ignored=matching`` reports the scratch we ignore on
                # purpose -- the ``.lock`` every read leaves, an interrupted
                # save's ``.tmp-*.yaml``. Those name a child of the board, and
                # counting them made a committed, saved board read "uncommitted"
                # forever. An ignored record is dirty only when it hides the
                # board *itself*: git collapses a never-committed, fully ignored
                # directory to the pathspec, which is the gitignore footgun
                # worth warning about -- edits that can never be committed.
                if _path_field(entry).rstrip("/") == relative:
                    changed.append(relative)
            else:
                changed.append(_path_field(entry))


    # No record means "nothing a path-scoped status reports", which conflates
    # tracked-and-clean with tracked-and-skip-worktree -- and the latter diverges
    # forever without ever asking to be committed. Only the clean case pays for
    # the second call.
    if not changed and _skip_worktree(relative, root):
        changed.append(relative)
        skip = True

    base.branch = None if branch in ("(detached)", "HEAD") else branch
    base.changed = changed
    base.is_dirty = bool(changed)
    base.ahead = ahead
    if skip:
        base.error = ("git is ignoring this file's contents (skip-worktree); run "
                      "'git update-index --no-skip-worktree " + relative
                      + "' to track changes again")
    return base


def changed_paths(board_dir: Path) -> list[str]:
    """Every path modified or untracked in the working tree, repo-wide.

    This is what ``api.conflicts`` needs for its third check: a live claim on
    ``src/api/**`` colliding with *someone else's uncommitted edit* to
    ``src/api/routes.py`` is invisible in the board's own files and stays clean
    through the merge -- the report is the only thing that stops the second
    agent overwriting the first. ``status()`` cannot answer it: its pathspec is
    deliberately the board directory so the toolbar's Commit count matches what
    Commit will stage, and that same scoping hides every source file.

    Ignored records are dropped rather than interpreted. The scratch we ignore
    on purpose (``.lock``, ``.tmp-*.yaml``) would otherwise blame agents for
    files nothing in git will ever see, and a glob matching them is a board
    misclaim, not a collision.
    """
    root = repo_root(board_dir)
    if root is None:
        return []
    done = _run(
        ["git", "status", "--porcelain=v2", "-z", "--untracked-files=all"], root,
    )
    if done.returncode != 0:
        return []
    paths: list[str] = []
    entries = [chunk for chunk in done.stdout.split("\0") if chunk]
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if entry[:2] in ("1 ", "u ", "? ", "2 "):
            if entry[:2] == "2 ":
                index += 1              # rename carries its origin as a field
            paths.append(_path_field(entry))
    return paths


def _skip_worktree(relative: str, root: Path) -> bool:
    done = _run(["git", "ls-files", "-v", "-z", "--", relative], root)
    if done.returncode != 0:
        return False
    for record in done.stdout.split("\0"):
        if record.endswith(relative) and record.startswith("S "):
            return True
    return False


def commit(target: Path, message: str) -> dict:
    """Stage only the board directory and commit it; ``nothing to commit`` is not an error.

    The commit takes the index the previous ``add`` built rather than repeating
    our pathspec: ``git commit -- <path>`` re-stages from the worktree, which
    would silently fold in whatever else changed between the two calls.  That is
    only safe once we know the index holds nothing else, so the staged list is
    checked and a foreign staged file refuses the commit — which is exactly the
    quarrel that matters now: an agent staging source files while a human clicks
    Commit would otherwise ship someone else's half-finished work.

    No ``--force`` on the ``add``, deliberately. The old store was one file a
    user might gitignore on purpose, and forcing was the only way to commit it.
    Now that the lock and the write scratch live *inside* the same directory,
    forcing would commit `.lock` and any `.tmp-*.yaml` caught mid-write. Plain
    add honours `.gitignore`, which is why that file names the scratch rather
    than the directory; a board the user gitignores wholesale is reported as
    ignored by :func:`status` instead of being committed against their wishes.
    """
    message = (message or "").strip()
    if not message:
        raise GitError("commit message is required")
    if len(message) > 2000:
        raise GitError("commit message is too long")

    root = repo_root(target)
    if root is None:
        raise GitError("the board is not inside a git repository")
    relative = _relative(target, root)
    if relative is None:
        raise GitError("the board is outside this git repository")
    inside = relative.rstrip("/") + "/"

    try:
        staged = _run(["git", "add", "--", relative], root)
        if staged.returncode != 0:
            raise GitError(_friendly(staged.stderr) or "git add failed")

        pending = _run(["git", "diff", "--cached", "--name-only", "-z"], root)
        if pending.returncode != 0:
            raise GitError(_friendly(pending.stderr) or "git diff failed")
        names = [name for name in pending.stdout.split("\0") if name]
        if not names:
            return {"committed": False, "reason": "nothing to commit",
                    **status(target).as_dict()}
        foreign = [name for name in names if name != relative and not name.startswith(inside)]
        if foreign:
            shown = ", ".join(sorted(foreign)[:3])
            raise GitError(
                f"the git index already has other files staged ({shown}) - "
                "commit or unstage them first, so this button cannot commit someone else's work"
            )

        # -m only, no editor: the UI has already supplied the message. The
        # identity args sit before the subcommand, so the commit is authored by
        # the person who set up git, not by a hostname guess.
        committed = _run(["git", *_identity_args(root), "commit", "-m", message], root)
        if committed.returncode != 0:
            output = (committed.stderr or committed.stdout).strip()
            if "nothing to commit" in output or "no changes added" in output:
                return {"committed": False, "reason": "nothing to commit",
                        **status(target).as_dict()}
            raise GitError(_friendly(output) or "git commit failed")
    except FileNotFoundError as exc:
        raise GitError("git is not installed") from exc

    sha = _run(["git", "rev-parse", "--short", "HEAD"], root)
    return {
        "committed": True,
        "sha": sha.stdout.strip() if sha.returncode == 0 else None,
        **status(target).as_dict(),
    }


def default_message(target: Path, summary: str) -> str:
    """``yamlboard: claim WEB-3 - the API layer``.

    Prefixed with the tool rather than the filename the way the single-file store
    did (``boards(boards.yaml): ...``): the board is a directory now, its files
    are named per card, and a log line naming one of them would describe only a
    fifth of the commit. ``yamlboard:`` is what a reader greps for.
    """
    summary = " ".join(summary.split())[:120]
    return f"yamlboard: {summary}" if summary else "yamlboard: update"
