"""Read/write a ``.yamlboard/`` store: round-trip YAML, atomic replace, lock.

A store is a directory inside the repository it coordinates: ``board.yaml`` for
the rare board-level edits, ``cards/<ID>.yaml`` for one card per file, and
``archive/<ID>.yaml`` for retired cards. The split is the whole point — *editing*
a card touches exactly one path, so git's own merge is the concurrency control for
unrelated work and there is no shared byte for two agents to contend over.

Creating a card is the exception, and the weakness this module accepts on purpose:
the id has to come from somewhere, so ``next_card`` moves in ``board.yaml`` beside
the new card file. Two clones creating from one commit mint the same id and collide
in both paths. A per-clone suffix would end that and cost every card the readable
``WEB-7``; the counter is cheap to dodge (pull before you create) and ``conflicts``
names the pair when someone forgets. Do not "fix" it by moving the counter into a
card file, which only relocates the contended byte.

ruamel's round-trip loader keeps every file a human artefact: comments, key order
and block scalars survive a save made by the browser. Writes go to a sibling temp
file and ``os.replace``, so a crash cannot truncate a card. The deliberate
simplification: card files carry no comments to preserve (they are coordination
state an agent writes), while ``board.yaml`` — the file a human reviews in a pull
request — keeps the adaptive indent the single-file store used.

There is no revision counter any more. ``rev`` was one integer for the whole
document, so two clones touching unrelated cards collided in the app and again in
git on line 1. Concurrency here is per file: each read hands back an opaque
token for *those* bytes, and a write carrying a stale token is refused.
"""

from __future__ import annotations

import datetime as dt
import io
import os
import re
import tempfile
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from ruamel.yaml.timestamp import TimeStamp

try:  # POSIX only; without it the lock is a no-op and tokens still guard
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

from . import model
from .model import CARD_RE, ValidationError

LOCK_TIMEOUT = 5.0

BOARD_FILE = "board.yaml"
CARDS_DIR = "cards"
ARCHIVE_DIR = "archive"
LOCK_NAME = ".lock"
TEMP_PREFIX = ".tmp-"
TEMP_SUFFIX = ".yaml"

IGNORE_NAME = ".gitignore"
# Patterns are relative to this file, so no `**/` here — the store's own ignore
# covers exactly the two per-machine artefacts written beside it.
IGNORE_BODY = (
    "# yamlboard runtime scratch — written by yamlboard, never board state.\n"
    "# The directory itself is tracked: it IS the board.\n"
    f"{LOCK_NAME}\n"
    f"{TEMP_PREFIX}*{TEMP_SUFFIX}\n"
)


class StoreError(Exception):
    """Anything that stops the store being read or written."""


class NotFound(StoreError):
    """No card file by that id, in the board or the archive."""


@dataclass
class File:
    """A parsed file plus the identity it was read under.

    ``mtime`` is nanoseconds: a hand edit landing in the same second as the last
    save, or one an rsync ``--size-only`` rewrite leaves alone, still moves it,
    so the pair is sound as a conflict token.
    """

    path: Path
    doc: dict
    mtime: int | None
    size: int | None
    result: Any = None

    def stamp(self) -> tuple[int | None, int | None]:
        return (self.mtime, self.size)

    def token(self) -> str:
        """Wire identity for these exact bytes; ``""`` when there is no file.

        A nanosecond mtime is past what a JavaScript number can hold, so the pair
        crosses the wire as one opaque string the client echoes back. That proves
        the writer is writing over the file it actually looked at — the only
        thing that catches a hand edit or a ``git pull``.
        """
        if self.mtime is None:
            return ""
        return f"{self.mtime:x}-{self.size:x}"


@dataclass
class Snapshot:
    """A whole store read at one moment: the board plus every card file."""

    board: File
    cards: list = field(default_factory=list)
    archived: list = field(default_factory=list)

    @property
    def doc(self) -> dict:
        return self.board.doc

    def card(self, card_id: str) -> File | None:
        for handle in self.cards:
            if handle.doc.get("id") == card_id:
                return handle
        return None

    def all_cards(self) -> list:
        return [*self.cards, *self.archived]


# --------------------------------------------------------------------------- #
# yaml plumbing
# --------------------------------------------------------------------------- #

def _iso_timestamp(rep, data):
    """Write dates and datetimes in ISO, matching how these files spell them.

    ruamel's default emits ``2026-11-22 19:58:03`` with a space. Values read back
    from a file arrive as ``TimeStamp`` (a ``datetime`` subclass), so it is
    registered alongside the plain types or those would keep the old spelling.
    """
    return rep.represent_scalar("tag:yaml.org,2002:timestamp", model.iso(data))


def _yaml(sequence: int = 4, offset: int = 2) -> YAML:
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 4096           # never re-wrap a hand-formatted description
    yaml.indent(mapping=2, sequence=sequence, offset=offset)
    rep = yaml.representer
    # Shadow the mapping on this instance: add_representer() walks up to the
    # class dict, which RoundTripRepresenter already owns, and would leak the
    # spelling into every other YAML() in the process.
    rep.yaml_representers = dict(rep.yaml_representers)
    for kind in (dt.datetime, dt.date, TimeStamp):
        rep.yaml_representers[kind] = _iso_timestamp
    return yaml


#: A sequence dash in column 0 can only be a top-level list item - block
#: scalars are indented - so its presence is the file asking for the compact
#: ``people:\n- id:`` layout rather than the indented default.
_ZERO_INDENT = re.compile(r"^- ", re.MULTILINE)


def _indent_for(path: Path) -> tuple[int, int]:
    """The sequence indent the file itself uses, so a save keeps the layout."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return (4, 2)
    return (2, 0) if _ZERO_INDENT.search(text) else (4, 2)


def _stat(path: Path) -> tuple[int | None, int | None]:
    try:
        info = path.stat()
    except OSError:
        return (None, None)
    return (info.st_mtime_ns, info.st_size)


def _file_id(path: Path) -> str | None:
    """The ``id:`` a card file declares, or None if it cannot be read.

    Used to prove a file is the card being moved rather than a stranger that
    merely shares its name. None on any failure, deliberate: an unreadable
    sibling is an unprovable one, and the caller keeps it.
    """
    try:
        doc = _read_file(path, None).doc
    except StoreError:
        return None
    ident = doc.get("id") if model.is_map(doc) else None
    return str(ident) if model.is_str(ident) else None


def _is_path(source: Path | None, target: Path) -> bool:
    """Is ``target`` the very file ``source`` names? Missing is not equal.

    ``resolve`` because the store's root arrives through whatever spelling the
    caller had — on macOS a ``/var`` symlink means two strings for one file, and
    comparing those would let an in-place rewrite look like a move into a
    stranger and refuse a write that is simply saving a card where it lives.
    """
    if source is None:
        return False
    try:
        return Path(source).resolve() == target.resolve()
    except OSError:
        return False


def _dumps(doc: dict, sequence: int, offset: int) -> bytes:
    buffer = io.StringIO()
    _yaml(sequence, offset).dump(doc, buffer)
    return buffer.getvalue().encode("utf-8")


def _fsync_dir(directory: Path) -> None:
    """Persist the rename itself; silently skipped where not permitted."""
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- #
# store root
# --------------------------------------------------------------------------- #

class Store:
    """A ``.yamlboard/`` directory, and the only way in to one.

    Every CLI verb and every HTTP request goes through this class, which is what
    makes "the files are the arbiter" true: the CLI needs no server because the
    server owns no state beyond these files.
    """

    DIRNAME = ".yamlboard"

    def __init__(self, root: Path | str):
        self.root = Path(root)
        if self.root.name != self.DIRNAME:
            self.root = self.root / self.DIRNAME

    @classmethod
    def discover(cls, start: Path | str | None = None) -> "Store":
        """Walk up from ``start`` (default cwd) to the nearest store.

        Agents run inside the repository they are coordinating, so "find the
        board" has to mean "find *this* repository's board" - and a clone with no
        board is a real state worth naming, not something to silently create.
        """
        here = Path(start or Path.cwd()).resolve()
        for candidate in (here, *here.parents):
            store = cls(candidate)
            if (store.root / BOARD_FILE).is_file():
                return store
        raise StoreError(
            f"no .yamlboard/ store found in {here} or any parent directory - "
            "run 'yamlboard init' first"
        )

    @property
    def board_path(self) -> Path:
        return self.root / BOARD_FILE

    @property
    def cards_dir(self) -> Path:
        return self.root / CARDS_DIR

    @property
    def archive_dir(self) -> Path:
        return self.root / ARCHIVE_DIR

    def card_path(self, card_id: str, *, archived: bool = False) -> Path:
        """``cards/<ID>.yaml``. A malformed id is refused, never path-joined.

        The id arrives from argv and from the browser. Joining it to a directory
        unchecked is how ``../../etc/passwd`` becomes a write target; ``CARD_RE``
        is the same grammar the validator enforces, so a refusal here cannot
        reject anything a real card could be named.
        """
        if not model.is_str(card_id) or not CARD_RE.match(card_id):
            raise ValidationError([f"card id {card_id!r}: expected an id like 'WEB-1'"])
        directory = self.archive_dir if archived else self.cards_dir
        return directory / f"{card_id}.yaml"

    def locate(self, card_id: str) -> Path | None:
        """Where this card lives now, or None if it does not exist."""
        for path in (self.card_path(card_id), self.card_path(card_id, archived=True)):
            if path.is_file():
                return path
        return None

    def exists(self) -> bool:
        return self.board_path.is_file()

    def card_ids(self) -> list[str]:
        """Every card id on disk, live and archived, sorted by id.

        Ids only — a file that is not named like a card is not a card, and
        inventing a marker string for it here would hand a fake id to
        :func:`model.card_context`, which treats its input as a set of real
        cards. :meth:`strays` reports those files where a human will see them.
        """
        found: list[str] = []
        for directory in (self.cards_dir, self.archive_dir):
            try:
                entries = sorted(directory.glob("*.yaml"))
            except OSError:
                continue
            found += [path.stem for path in entries if CARD_RE.match(path.stem)]
        return found

    def strays(self) -> list[str]:
        """``cards/*.yaml`` that cannot be a card, by filename.

        A card is found by its filename, so ``cards/notes.yaml`` is work
        somebody wrote that no command will ever read. Silently skipping it
        loses that work without a trace; these names are what ``check`` prints
        so the owner learns the file is invisible.

        ``.tmp-*`` scratch is deliberately absent. It is yamlboard's own debris
        from a killed save, it is git-ignored, and it means nothing to the
        board — telling someone to rename it to ``WEB-9.yaml`` would be advice
        to corrupt their own store. Files without a ``.yaml`` extension are
        absent for the same reason in the other direction: a ``README.md`` left
        beside the cards is not a card by any reading, and reporting it would
        train people to ignore the warning.
        """
        try:
            entries = sorted(self.cards_dir.glob("*.yaml"))
        except OSError:
            return []
        return sorted(
            path.name for path in entries
            if not path.name.startswith(TEMP_PREFIX) and not CARD_RE.match(path.stem)
        )

    # -- reading ------------------------------------------------------------ #

    def read_board(self) -> File:
        """``board.yaml``, validated. A missing store is an error, not an empty board."""
        if not self.board_path.is_file():
            raise StoreError(f"{self.board_path} does not exist - run 'yamlboard init'")
        return _read_file(self.board_path, model.validate)

    def read_card(self, card_id: str) -> File:
        path = self.locate(card_id)
        if path is None:
            raise NotFound(f"no card {card_id!r}")
        return _read_file(path, None)

    def _card_files(self, directory: Path, board: dict | None = None,
                    ids: set[str] | None = None) -> list[File]:
        """Every card file in one directory, each tagged with its problems.

        An invalid card is *kept*, its errors parked on ``result``: a board whose
        one hand-edited card is broken must still open, and the problem belongs
        in ``check`` and the payload's problem list. Dropping it would be worse
        than showing it — a missing card silently changes what ``conflicts`` and
        the ready queue claim about who holds what.

        A file whose root is not a mapping is the same accident one level down
        and gets the same treatment, so every ``File.doc`` returned here is a
        mapping — the invariant ``_ordered``, ``summary`` and ``conflicts`` all
        assume. Without this guard a hand-typed list in ``cards/PROD-9.yaml``
        took down ``check``, ``serve`` and every write with an
        ``AttributeError``, which is exactly the brick this method exists to
        prevent.

        ``ids`` is the id set every card's ``blocked_by`` is checked against, and
        it has to be the whole store's: built from the one directory being read,
        archiving a blocker makes its dependents' edges dangle. That verdict is
        then frozen on the handle, so the false "is not a card in this store"
        refuses every later write to that card — including the
        ``dep remove`` that would have corrected it. Fallback stays per-directory
        for callers that read one directory in isolation.
        """
        # ``_card_paths`` yields nothing for a missing directory, which is the
        # same answer as the old ``is_dir`` guard: a store with no archive yet.
        found: list[File] = []
        paths = self._card_paths(directory)
        if ids is None:
            ids = {p.stem for p in paths}
        for path in paths:
            try:
                handle = _read_file(path, None)
            except StoreError as exc:
                found.append(self._malformed(path, f"{path.name}: {exc}"))
                continue
            if not model.is_map(handle.doc):
                found.append(self._malformed(
                    path,
                    f"{path.name}: expected a card mapping, found "
                    f"{type(handle.doc).__name__} — a card file is one YAML "
                    "mapping whose first key is `id:`",
                ))
                continue
            if board is not None:
                home = model.board_for_card(board, path.stem)
                if home is None:
                    handle.result = [
                        f"{path.name}: no board has the key {path.stem.split('-')[0]!r}"
                    ]
                else:
                    handle.result = model.validate_card(
                        handle.doc, home, model.card_context(board, home, ids)
                    ) or None
            found.append(handle)
        return found

    @staticmethod
    def _malformed(path: Path, problem: str) -> File:
        """A card file that will not load, shaped like the card it should have been.

        The id comes from the filename, which is the one fact still trustworthy:
        it keeps the card visible in ``conflicts`` and the ready queue, so a
        second agent cannot quietly take work somebody may be holding.
        """
        handle = File(path, {"id": path.stem, "title": "(unreadable)"}, *_stat(path))
        handle.result = [problem]
        return handle

    @staticmethod
    def _card_paths(directory: Path) -> list[Path]:
        """The files in one directory that are named like cards."""
        try:
            return [p for p in sorted(directory.glob("*.yaml")) if CARD_RE.match(p.stem)]
        except OSError:
            return []

    def read_all(self) -> Snapshot:
        """Board plus every card file, each with its own token."""
        board = self.read_board()
        # One id set for both directories. An edge pointing into ``archive/``
        # still points at a card that exists; validated per-directory, archiving
        # a blocker makes every dependent's ``blocked_by`` read as dangling, and
        # that false verdict then blocks the write that would correct it.
        card_ids = {p.stem for d in (self.cards_dir, self.archive_dir)
                    for p in self._card_paths(d)}
        return Snapshot(
            board,
            _ordered(self._card_files(self.cards_dir, board.doc, card_ids)),
            _ordered(self._card_files(self.archive_dir, board.doc, card_ids)),
        )

    # -- writing ------------------------------------------------------------ #

    def write_card(self, doc: dict, *, board: dict | None = None,
                   source: Path | None = None) -> File:
        """Write a card document where its ``archived`` flag says it belongs.

        The directory is part of the document's meaning, so archiving is one
        write here rather than a separate move call the caller must remember:
        ``card.archive`` flipping ``archived:`` and a hand-edit that set the key
        in an editor then both land the file in ``archive/`` without a second
        code path.

        ``source`` is the file this document was read from, when it was read from
        one. It is what tells an in-place rewrite from a move, and the difference
        is data loss: see the guard on ``target`` below.
        """
        card_id = str((doc or {}).get("id") or "")
        archived = bool((doc or {}).get("archived"))
        with locked(self.root):
            doc_for_home = board if board is not None else self.read_board().doc
            home = model.board_for_card(doc_for_home, card_id)
            if home is None:
                raise ValidationError(
                    [f"card {card_id!r}: no board has the key {card_id.split('-')[0]!r}"]
                )
            others = set(self.card_ids()) - {card_id}
            errors = model.validate_card(
                doc, home, model.card_context(doc_for_home, home, others)
            )
            if errors:
                raise ValidationError(errors)
            target = self.card_path(card_id, archived=archived)
            stale = self.card_path(card_id, archived=not archived)
            # The sibling gets proven before it is deleted; the target needs the
            # same proof, because a card read from one directory can be written
            # into the other. Two clones that each archived a different card to
            # the same basename merge into `archive/WEB-9.yaml` holding `id:
            # WEB-8` while `cards/WEB-9.yaml` holds `id: WEB-9` — a state
            # `conflicts` reports and refuses to arbitrate. Without this guard,
            # editing the live card's *title* wrote over the archived stranger
            # and deleted it: the clobber came from the plain `card.update` path,
            # nowhere near the repair that had correctly declined to choose.
            # Unreadable is unprovable, so it is refused for the same reason.
            if target.is_file() and not _is_path(source, target):
                ahead = _file_id(target)
                if ahead != card_id:
                    where = "the archive" if archived else "the live cards"
                    raise ValidationError([
                        f"card {card_id!r}: {target.name} already holds card "
                        f"{ahead!r} in {where} — two merged files want one name. "
                        "Renaming one is a human call; `yamlboard conflicts` "
                        "names the pair."])
            # The sibling path holds the same id in the other directory, which is
            # normally this card changing between live and archived -- ours to
            # remove. "Normally" is doing the work there: two files can carry one
            # id without being one card (a merge that kept both copies of
            # WEB-1.yaml), and then the sibling is somebody else's card that
            # merely happens to share a name. Reading it and matching its `id:`
            # is the difference between finishing a move and deleting a stranger;
            # unreadable means unprovable, so it stays.
            if stale.is_file() and _file_id(stale) == card_id:
                written = _write_file(target, doc)
                stale.unlink()
                _fsync_dir(stale.parent)
                return written
            return _write_file(target, doc)

    def write_board(self, doc: dict) -> File:
        """Write ``board.yaml`` as-is; the caller has already validated it."""
        with locked(self.root):
            return _write_file(self.board_path, doc)

    def remove_card(self, card_id: str) -> None:
        with locked(self.root):
            path = self.locate(card_id)
            if path is None:
                raise NotFound(f"no card {card_id!r}")
            path.unlink()

    # -- creation ------------------------------------------------------------ #

    def create(self, title: str = "Board", *, key: str | None = None,
               people: Sequence[str] = ()) -> "Snapshot":
        """Write a valid starter store. Refuses if one is already there.

        Refusing rather than merging is the whole safety story for ``init``: an
        over-written board is unrecoverable, whereas "already initialised" costs
        the caller one confused second.
        """
        if self.exists():
            raise StoreError(f"{self.root} already holds a board - not overwriting it")
        self.cards_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        doc = new_board(title, key=key, people=people)
        errors = model.validate(doc)
        if errors:
            raise ValidationError(errors)
        _write_file(self.board_path, doc)
        self.write_ignore()
        return self.read_all()

    def write_ignore(self) -> bool:
        """Seed ``.yamlboard/.gitignore`` so the lock never reaches git.

        The store writes two per-machine artefacts inside itself: the advisory
        flock and the scratch of a save killed mid-write. Neither is board state,
        and a clone that carries a 0-byte ``.lock`` in its history has that
        mistake forever.

        They are ignored by a file the store owns rather than by the repository's
        root ``.gitignore``: the board gets committed into somebody else's repo,
        and editing that repo's root file unasked is not ours to do. A nested
        ``.gitignore`` travels with the directory, so ``git add .`` is safe in
        every consuming repo including ones that have never heard of yamlboard —
        and without it the Commit button refuses, because its own ``git add``
        stages the lock and then the foreign-file guard rightly sees a file it
        did not put there.

        Returns True when the file was created, False when one is already there:
        an owner who has narrowed or widened these rules keeps theirs.
        """
        path = self.root / IGNORE_NAME
        if path.exists():
            return False
        path.write_text(IGNORE_BODY, encoding="utf-8")
        _fsync_dir(path.parent)
        return True


def _read_file(path: Path, validate: Callable[[dict], list[str]] | None) -> File:
    stamp = _stat(path)
    if stamp == (None, None):
        raise NotFound(f"{path} does not exist")
    try:
        with path.open("r", encoding="utf-8") as handle:
            loaded = _yaml().load(handle)
    except YAMLError as exc:
        raise StoreError(f"{path}: not valid YAML: {exc}") from exc
    except OSError as exc:
        raise StoreError(f"cannot read {path}: {exc}") from exc
    doc: Any = {} if loaded is None else loaded
    if validate is not None:
        errors = validate(doc)
        if errors:
            raise ValidationError(errors)
    return File(path, doc, *stamp)


def _write_file(path: Path, doc: dict) -> File:
    """Serialise, compare, and only then replace.

    The byte compare is not an optimisation: lease renewals and no-op saves go
    through here, and without it every redundant claim would move an mtime and
    put a one-line change in a human's ``git status``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    sequence, offset = _indent_for(path)
    try:
        payload = _dumps(doc, sequence, offset)
    except YAMLError as exc:
        raise StoreError(f"cannot serialise to YAML: {exc}") from exc

    try:
        with path.open("rb") as existing:
            if existing.read() == payload:
                return File(path, doc, *_stat(path))
    except OSError:
        pass

    handle = None
    tmp_path = None
    try:
        handle = tempfile.NamedTemporaryFile(
            "wb", dir=str(path.parent), prefix=TEMP_PREFIX, suffix=TEMP_SUFFIX,
            delete=False,
        )
        tmp_path = Path(handle.name)
        handle.write(payload)        # LF by decree, not by platform
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        os.replace(str(tmp_path), str(path))
        tmp_path = None
        _fsync_dir(path.parent)
    except OSError as exc:
        raise StoreError(f"cannot write {path}: {exc}") from exc
    finally:
        if handle is not None:
            handle.close()
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass
    return File(path, doc, *_stat(path))


def _ordered(files: list[File]) -> list[File]:
    """Card files in display order, so the browser and CLI agree unprompted.

    Returns every file it was given, because two files carrying the same ``id:``
    is exactly the state ``conflicts`` exists to report: keying a dict by id
    (as this did) made the second overwrite the first, so a board with a
    duplicate id showed one card twice and hid the other from ``list``,
    ``ready``, and every command that looks cards up here. Sorting the handles
    rather than a lookup table keeps the pair visible and the count honest.
    """
    by_doc = {id(h.doc): h for h in files}
    return [by_doc[id(doc)] for doc in model.sort_cards([h.doc for h in files])
            if id(doc) in by_doc]


# --------------------------------------------------------------------------- #
# skeletons
# --------------------------------------------------------------------------- #

def new_board(title: str = "Board", *, key: str | None = None,
              people: Sequence[str] = ()) -> dict:
    """A valid one-board store: enough columns to work in, nothing else.

    ``people`` is the starting roster, and the caller decides whether it has one.
    An empty roster is not a broken board — it is a board whose humans have not
    been named yet — but a board nobody can claim from is, so ``init`` passes the
    identity it was invoked with. See :func:`yamlboard.cli.verb_init`.
    """
    key = (key or model.board_key(title)).upper()
    return {
        "version": model.VERSION,
        "title": title,
        "people": [{"id": str(pid), "name": str(pid)} for pid in people],
        "labels": [],
        "boards": [{
            "id": model.slugify(title, "board"),
            "key": re.sub(r"[^A-Z0-9]", "", key)[:8] or "BD",
            "title": title,
            "columns": [
                {"id": "backlog", "name": "Backlog", "color": "#7c8797"},
                {"id": "ready", "name": "Ready", "color": "#4f7cac"},
                {"id": "doing", "name": "In Progress", "color": "#d4a017",
                 "wip_limit": 3},
                {"id": "done", "name": "Done", "color": "#2f6f4f", "is_done": True},
            ],
            "custom_fields": [],
            "next_card": 1,
        }],
    }


# --------------------------------------------------------------------------- #
# locking
# --------------------------------------------------------------------------- #
# ``flock`` locks belong to the open file description, not the process: a second
# ``open`` of the same file in the *same* process is refused by the lock the first
# one holds. The server reads a snapshot and then writes under one outer lock, and
# the write helpers each take their own, so nesting has to be free or every request
# deadlocks against itself. The counter keeps the real lock held until the
# outermost scope exits, which is the only guarantee the callers care about.
_HELD = threading.local()


def _held() -> set[str]:
    paths = getattr(_HELD, "paths", None)
    if paths is None:
        paths = _HELD.paths = set()
    return paths


class locked:
    """Exclusive advisory lock on ``.yamlboard/.lock``.

    The artefact is named for *where* it lives, not for a card, so one ignore
    line covers it whatever the board is called. It locks the directory rather
    than the file: one agent editing two cards takes turns with itself, which is
    strictly conservative and never wrong. What stays independent is the
    *conflict token* - two clones writing different files still never see each
    other's bytes, which was the point of the split.

    On Darwin/Linux this is ``fcntl``; where that is unavailable the class
    degrades to a no-op and per-file tokens still catch lost updates.
    """

    def __init__(self, root: Path | str, timeout: float = LOCK_TIMEOUT) -> None:
        self.root = Path(root)
        self.timeout = timeout
        self._fd: int | None = None
        self._nested = False

    @property
    def path(self) -> Path:
        return self.root / LOCK_NAME

    def __enter__(self) -> "locked":
        self.root.mkdir(parents=True, exist_ok=True)
        key = str(self.root.resolve())
        held = _held()
        if fcntl is None or key in held:
            self._nested = True
            return self
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise StoreError(
                        f"{self.root} is locked by another yamlboard process"
                    ) from None
                time.sleep(0.02)
        self._fd = fd
        held.add(key)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(self._fd)
            _held().discard(str(self.root.resolve()))
            self._fd = None


def describe(errors: list[str]) -> str:
    return "\n".join(f"  - {e}" for e in errors)
