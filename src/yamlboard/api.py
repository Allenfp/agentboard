"""Every mutation the CLI and the UI can make, as operations on a store.

Nothing here knows about HTTP: :func:`apply` takes a :class:`Tx` (the store plus
the files one operation is allowed to touch) and a payload, and returns a small
result dict. That keeps the coordination rules testable on their own and lets the
store own locking, validation and atomic writes.

Two rules make a directory of files as safe as the single document this replaced:

**Cards are written before ``board.yaml``.** A multi-card operation (deleting a
person, dropping a column, stripping a label) rewrites the cards that reference
the thing, then removes the thing itself, still under the one store lock. A crash
between the two leaves a store that validates — cards that no longer mention a
person, with the person still listed — whereas the reverse order leaves dangling
references that make every later read fail.

**A claim is coordination, not content.** Nothing under ``claim:`` touches the
card's ``updated:`` stamp, and an operation that changes no bytes performs no
write. Lease traffic that moved ``updated:`` would poison ``git log`` and the
list-view sort for every human reading the board.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Callable

from . import model, store
from .model import ReorderExhausted, ValidationError


class NotFound(Exception):
    """An id in the payload does not exist."""


class BadRequest(Exception):
    """The payload is well-formed but makes no sense."""


class ClaimDenied(Exception):
    """Another agent holds the card, or a path it claimed overlaps.

    Separate from :class:`BadRequest` because the caller's correct response is
    different: a bad argument is fixed by editing the command, a denial is fixed
    by choosing other work. The CLI maps this to exit 3 so a script can branch
    without parsing prose.
    """

    def __init__(self, lines: list[str], detail: dict | None = None):
        self.lines = [str(line) for line in lines]
        self.detail = dict(detail or {})
        super().__init__("\n".join(self.lines))


class Conflict(Exception):
    """The bytes moved under the caller: its view of a file is stale.

    Distinct from :class:`ClaimDenied`, which means *someone else holds this* and
    the fix is to pick other work. A conflict means the fix is to re-read, so the
    browser reloads and the CLI re-prints; answering either with the other's
    advice would send an agent straight back to the command that just failed.
    """

    def __init__(self, paths: list[str]):
        self.paths = [str(p) for p in paths]
        super().__init__("changed since you read them: " + ", ".join(self.paths))


# --------------------------------------------------------------------------- #
# the transaction
# --------------------------------------------------------------------------- #

class Tx:
    """One operation's worth of store access: the board plus the cards it names.

    Card documents are loaded on demand and written back on :meth:`flush`, cards
    first, ``board.yaml`` last. A getter hands back the *live* ruamel mapping, so
    an op reads like an in-place edit of one document while the split into files
    stays the store's business.

    ``tokens`` is what the caller says it looked at: ``{"board": "...", "WEB-1":
    "..."}``. Anything this transaction writes whose bytes have moved since is a
    :class:`Conflict`, checked inside the lock. It is optional
    because an agent editing files directly has no token to offer, and requiring
    one would make the CLI useless — but the browser always sends them, which is
    the only thing that catches a hand edit or a ``git pull`` landing between its
    render and its click.
    """

    def __init__(self, store: Any, snapshot: Any | None = None,
                 *, tokens: dict[str, str] | None = None):
        self.store = store
        self.snapshot = snapshot or store.read_all()
        self.tokens = dict(tokens or {})
        self._extra: dict[str, dict] = {}      # cards created in this tx
        self._dirty_cards: dict[str, dict] = {}
        self._removed: set[str] = set()
        self._renamed: list[tuple[str, str]] = []
        self.board_dirty = False
        self.notes: dict[str, Any] = {}

    @property
    def doc(self) -> dict:
        """``board.yaml`` as a live mapping. Mark it dirty by :meth:`touch`."""
        return self.snapshot.doc

    def touch(self) -> None:
        self.board_dirty = True

    # -- cards -------------------------------------------------------------- #

    def cards(self) -> list:
        """Live card mappings in display order, including any just created."""
        found = [handle.doc for handle in self.snapshot.cards]
        for card in self._extra.values():
            if card not in found:
                found.append(card)
        return model.sort_cards(found)

    def all_cards(self) -> list:
        found = [handle.doc for handle in self.snapshot.all_cards()]
        for card in self._extra.values():
            if card not in found:
                found.append(card)
        return model.sort_cards(found)

    def card_ids(self) -> list[str]:
        return [str(c.get("id")) for c in self.all_cards() if c.get("id")]

    def card(self, card_id: str) -> dict:
        card = self.find_card(card_id)
        if card is None:
            raise NotFound(f"card {card_id!r}")
        return card

    def find_card(self, card_id: str) -> dict | None:
        for handle in self.snapshot.all_cards():
            if handle.doc.get("id") == card_id:
                return handle.doc
        return self._extra.get(card_id)

    def is_archived(self, card_id: str) -> bool:
        for handle in self.snapshot.archived:
            if handle.doc.get("id") == card_id:
                return True
        return False

    def dirty(self, card: dict) -> None:
        """Mark a card for writing. Which file it lands in is the doc's business."""
        ident = str(card.get("id") or "")
        if not ident:
            raise BadRequest("card has no id")
        if ident not in self._extra:
            self._dirty_cards[ident] = card

    def create(self, card: dict) -> None:
        self._extra[str(card.get("id"))] = card

    def remove(self, card_id: str) -> None:
        self._extra.pop(card_id, None)
        self._dirty_cards.pop(card_id, None)
        self._removed.add(card_id)

    def problems(self) -> list[str]:
        """Every validation problem across the store as it now stands."""
        errors = list(model.validate(self.doc))
        # A file whose read failed, or whose root is not a mapping, already
        # carries the verdict from `_card_files` — and that verdict is about the
        # file, not any board. Collecting these inside the board loop below
        # printed each one once per board, so a two-board store reported two
        # copies of the same broken card.
        for handle in self.snapshot.all_cards():
            if handle.result and str(handle.doc.get("id") or "") not in self._dirty_cards:
                errors.extend(str(e) for e in handle.result)
        ids = set(self.card_ids())
        for board in model.boards(self.doc):
            ctx = model.card_context(self.doc, board, ids)
            for handle in self.snapshot.all_cards():
                card = handle.doc
                if handle.result and str(card.get("id") or "") not in self._dirty_cards:
                    continue
                if str(card.get("id") or "") in self._removed:
                    continue
                if model.board_for_card(self.doc, str(card.get("id") or "")) is not board:
                    continue
                errors.extend(model.validate_card(card, board, ctx))
            cycle = model.find_cycle(
                model.dependencies([c for c in self.all_cards()
                                   if model.board_for_card(self.doc, str(c.get("id") or "")) is board])
            )
            if cycle:
                errors.append(f"board {board.get('id')}: blocked_by cycle "
                              + " -> ".join(cycle))
        return errors

    # -- writing ------------------------------------------------------------ #

    def _stale(self) -> list[str]:
        """Files this transaction will write whose bytes moved since the caller saw them.

        Re-stat'd inside the lock rather than compared against the snapshot, so a
        hand edit that landed a microsecond after the read is still caught. Only
        files the caller named a token for are checked: an agent working straight
        from the files has no token, and inventing one would refuse its write for
        a race it could see for itself.
        """
        stale: list[str] = []
        targets = [(self.store.board_path, "board")] if self.board_dirty else []
        for card_id in {**self._extra, **self._dirty_cards}:
            if card_id in self._extra:
                continue                      # a create cannot have moved
            path = self.store.locate(card_id)
            if path is not None:
                targets.append((path, card_id))
        for path, key in targets:
            want = self.tokens.get(key)
            if want is None:
                continue
            stamp = store._stat(path)
            found = "" if stamp == (None, None) else f"{stamp[0]:x}-{stamp[1]:x}"
            if found != want:
                stale.append(key)
        return sorted(stale)

    def flush(self) -> dict:
        """Write this transaction. Cards first, ``board.yaml`` last.

        Validating the *whole* store before the first byte moves is what keeps
        the guarantee honest: an operation that would leave any card invalid
        writes nothing at all, so a bad payload cannot half-apply.
        """
        errors = self.problems()
        if errors:
            raise ValidationError(errors)

        board = self.doc
        written: list[str] = []
        # A cross-board move is a rename, and it arrives as both `remove(old)`
        # and `_renamed`, so two loops unlinked the same path: the second raised
        # `NotFound` after the card was already gone from disk and before its
        # replacement was written -- a deleted card and a traceback where the
        # board used to be. The rename owns its own removal.
        renamed_from = {old for old, _ in self._renamed}
        # one lock for the whole plan: `locked` is re-entrant, so the per-write
        # locks inside the store helpers cost nothing here but the gap between
        # them would let another process mint an id we are about to reuse
        with store.locked(self.store.root):
            stale = self._stale()
            if stale:
                raise Conflict(stale)
            for card_id in sorted(self._removed - renamed_from):
                self.store.remove_card(card_id)
                written.append(card_id)
            for card_id, card in sorted({**self._extra, **self._dirty_cards}.items()):
                self.store.write_card(card, board=board)
                written.append(card_id)
            # last, so a rename is never a moment where the card exists nowhere:
            # the brief overlap of two files is recoverable, an empty gap is not.
            for old, new in self._renamed:
                self.store.remove_card(old)
                written.append(new)
            if self.board_dirty:
                self.store.write_board(board)
                self.snapshot.board = self.store.read_board()
        self.notes["written"] = sorted(set(written))
        return self.notes


# --------------------------------------------------------------------------- #
# payload helpers
# --------------------------------------------------------------------------- #

def _need(args: dict, name: str) -> Any:
    value = args.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise BadRequest(f"{name} is required")
    return value


def _need_text(args: dict, name: str) -> str:
    """A required, non-empty, *string* argument - never ``str(<list>)``."""
    value = _need(args, name)
    if not model.is_str(value):
        raise BadRequest(f"{name} must be text")
    return value.strip()


def _bool_arg(args: dict, name: str, default: bool = False) -> bool:
    value = args.get(name, default)
    if not model.is_bool(value):
        raise BadRequest(f"{name} must be true or false")
    return value


def _board(tx: Tx, args: dict) -> Any:
    # a missing `board` is a malformed request (400); an unknown one is 404
    if not model.is_str(args.get("board")) or not str(args["board"]).strip():
        raise BadRequest("board is required")
    board = model.find_board(tx.doc, str(args["board"]))
    if board is None:
        raise NotFound(f"board {args['board']!r}")
    return board


def _board_of(tx: Tx, card_id: str) -> Any:
    board = model.board_for_card(tx.doc, card_id)
    if board is None:
        raise BadRequest(f"card {card_id!r}: no board has its key")
    return board


def _section(tx: Tx, key: str) -> list:
    """The live top-level list for ``key``, created in document key order.

    ``doc.setdefault`` would park a first-created ``people:`` after the entire
    ``boards:`` block - exactly the diff noise ``set_field`` exists to prevent.
    """
    doc = tx.doc
    if key not in doc:
        tx.touch()
        model.set_field(doc, "document", key, [])
    section = doc[key]
    if not model.is_list(section):
        raise BadRequest(f"{key} must be a list")
    return section


def _column(board: dict, column_id: str) -> Any:
    column = model.find_column(board, column_id)
    if column is None:
        raise NotFound(f"column {column_id!r}")
    return column


def _text(args: dict, name: str, default: Any = None) -> Any:
    value = args.get(name, default)
    if value is None:
        return default
    if not model.is_str(value):
        raise BadRequest(f"{name} must be text")
    return value


def _one_of(args: dict, name: str, allowed: tuple[str, ...]) -> Any:
    if name not in args:
        return None
    value = args[name]
    if value not in allowed:
        raise BadRequest(f"{name} must be one of {', '.join(allowed)}")
    return value


def _wanted_id(args: dict, derived: str) -> str:
    """The caller's explicit ``id``, checked as a slug; else the derived one.

    A junk id was accepted at the op layer and only died in ``validate`` at
    write time, surfacing as an opaque 422 that named neither the argument nor
    the rule. Message wording follows validate's own so CLI and API agree.
    """
    raw = args.get("id")
    if raw is None or (model.is_str(raw) and not raw.strip()):
        return derived
    ident = str(raw).strip()
    if not model.SLUG_RE.match(ident):
        raise BadRequest(f"id {ident!r} is not a slug like 'bug-triage'")
    return ident


def _people(tx: Tx) -> set[str]:
    return model.person_ids(tx.doc)


def _person_arg(tx: Tx, args: dict, name: str = "assignee") -> str:
    """An agent name the caller may use: must be text and one of the people.

    Claiming as an unknown name is a denial, not a bad request — an agent with a
    stale ``$YAMLBOARD_AGENT`` is in the same position as one that lost a race:
    it should stop and fix its identity rather than assume the payload was wrong.
    """
    value = args.get(name)
    if not model.is_str(value) or not value.strip():
        raise ClaimDenied([f"{name} is required: pass --as <name> or set YAMLBOARD_AGENT"],
                          {"reason": "no_agent"})
    ident = value.strip()
    known = sorted(_people(tx))
    if not model.SLUG_RE.match(ident):
        raise ClaimDenied([f"{name} {ident!r} is not a slug like 'grigory'"],
                          {"reason": "bad_agent"})
    if ident not in known:
        # The advice has to be a command that exists. This used to name a
        # person-adding verb the tool has never had: adding people is
        # deliberately a human act (see identity.py), reachable from the board's
        # Settings, so a claim can never widen its own allowlist.
        raise ClaimDenied(
            [f"{name} {ident!r} is not one of this board's people"
             + (f" ({', '.join(known)})" if known
                else " (the board lists none: a person must add it in Settings)")],
            {"reason": "unknown_agent", "known": known},
        )
    return ident


def _touched(card: dict) -> None:
    """Stamp content changes. Never call this for a claim: see the module banner."""
    card["updated"] = model.now()


def _taken_keys(tx: Tx) -> set[str]:
    return {str(b.get("key")) for b in model.boards(tx.doc)
            if model.is_map(b) and b.get("key")}


def _max_number(board: dict, ids: list[str]) -> int:
    highest = 0
    for card_id in ids:
        match = model.CARD_RE.match(str(card_id or ""))
        if match:
            highest = max(highest, int(match.group(2)))
    return highest


def _link(value: Any) -> dict:
    if not model.is_map(value):
        raise BadRequest("each link must be a mapping")
    url = str(value.get("url") or "").strip()
    if not url:
        raise BadRequest("link.url is required")
    if not model.URL_RE.match(url):
        raise BadRequest(f"link.url must start with http://, https:// or mailto: (got {url!r})")
    link = {"url": url}
    title = value.get("title")
    if title:
        link["title"] = str(title)
    return link


def _subtask(value: Any) -> dict:
    if model.is_str(value):
        return {"text": value.strip(), "done": False}
    if not model.is_map(value):
        raise BadRequest("each subtask must be text or a mapping")
    text = str(value.get("text") or "").strip()
    if not text:
        raise BadRequest("subtask.text is required")
    return {"text": text, "done": bool(value.get("done", False))}


def _custom_values(board: dict, raw: dict, existing: dict | None = None) -> dict:
    """Filter/validate custom field values against the board's declarations.

    ``patch.fields`` replaces the whole mapping, so an update passes the card's
    current values in ``existing``: a required field the client left out keeps
    its stored value instead of vanishing. On create (``existing`` is None)
    every required field must be supplied - that is what ``required`` means,
    and it is enforced here rather than in ``validate`` so a board can gain a
    required field without its existing cards becoming file-wide errors.
    """
    if not model.is_map(raw):
        raise BadRequest("fields must be a mapping")
    specs = {str(f.get("id")): f for f in model.custom_fields(board)}
    out: dict[str, Any] = {}
    for name, value in raw.items():
        spec = specs.get(str(name))
        if spec is None:
            raise BadRequest(f"unknown custom field {name!r}")
        if value in (None, ""):
            if spec.get("required"):
                raise BadRequest(f"{spec.get('name') or name} is required")
            continue
        errors = model.check_custom_value(spec, value, str(name))
        if errors:
            raise BadRequest(errors[0])
        out[str(name)] = model.parse_date(value) if spec.get("type") == "date" else value
    for ident, spec in specs.items():
        if not spec.get("required") or out.get(ident) not in (None, ""):
            continue
        carried = (existing or {}).get(ident)
        if carried not in (None, ""):
            out[ident] = carried
        elif existing is None:
            raise BadRequest(f"{spec.get('name') or ident} is required")
    return out


def _reconcile_fields(card: dict, target: dict) -> None:
    """Make a moved card's custom values legal for the board it landed on."""
    values = card.get("fields")
    if not model.is_map(values) or not values:
        return
    specs = {str(f.get("id")): f for f in model.custom_fields(target)}
    kept: dict[str, Any] = {}
    for name, value in values.items():
        spec = specs.get(str(name))
        if spec is None or model.check_custom_value(spec, value, str(name)):
            continue
        kept[str(name)] = value
    if kept:
        card["fields"] = kept
    else:
        card.pop("fields", None)


# --------------------------------------------------------------------------- #
# path ownership
# --------------------------------------------------------------------------- #

def _patterns(value: Any) -> list[str]:
    """Normalised ``--paths`` values, refusing junk before it is written."""
    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",") if part.strip()]
    if not model.is_list(value):
        raise BadRequest("--paths must be a list of patterns or a comma-separated string")
    out: list[str] = []
    for item in value:
        pattern = model.normalize_pattern(item)
        if pattern not in out:
            out.append(pattern)
    return out


def _holders(tx: Tx, card_id: str) -> list[dict]:
    """Live claims on *other* cards — the set a new claim must not collide with."""
    out: list[dict] = []
    for card in tx.cards():
        if card.get("id") == card_id:
            continue
        claim = model.claim_of(card)
        if claim and model.claim_live(claim):
            out.append({"id": str(card.get("id")), "claim": claim})
    return out


def _collisions(tx: Tx, card_id: str, patterns: list[str]) -> list[dict]:
    """Which held claims overlap these patterns, with the pair that collided."""
    hits: list[dict] = []
    for holder in _holders(tx, card_id):
        for mine in patterns:
            for theirs in (holder["claim"].get("paths") or []):
                try:
                    if not model.patterns_overlap(mine, theirs):
                        continue
                except ValidationError as exc:
                    raise BadRequest(str(exc)) from exc
                hits.append({
                    "card": holder["id"],
                    "agent": holder["claim"].get("agent"),
                    "pattern": str(theirs),
                    "yours": mine,
                    "expires": model.iso(holder["claim"].get("expires")),
                    "left": model.humanize(model.ttl_left(holder["claim"])),
                })
    return hits


# --------------------------------------------------------------------------- #
# ready queue and conflicts (read-only)
# --------------------------------------------------------------------------- #

def state_of(tx: Tx, card: dict, agent: str | None = None) -> str:
    """``yours`` / ``taken`` / ``blocked`` / ``ready`` — the shared vocabulary.

    Both ends of a lease use these four words, which is the only way a CLI
    refusal and a browser badge can mean the same thing.
    """
    claim = model.claim_of(card)
    if claim and model.claim_live(claim):
        return "yours" if agent and claim.get("agent") == agent else "taken"
    edges = model.dependencies(tx.cards())
    done = {
        str(c.get("id")) for c in tx.cards()
        if model.is_done_column(_board_of(tx, str(c.get("id") or "")), str(c.get("column") or ""))
    }
    if model.blocked_by_open(edges, str(card.get("id") or ""), done):
        return "blocked"
    return "ready"


def why_not_ready(tx: Tx, card: dict, agent: str | None = None,
                 patterns: list[str] | None = None) -> list[str]:
    """Every reason this card is not takeable, in the order an agent acts on them.

    A denial that gives one reason invites the agent to retry and hit the next
    one. Listing them all turns a loop into one corrected command.
    """
    lines: list[str] = []
    card_id = str(card.get("id") or "?")
    claim = model.claim_of(card)
    if claim and model.claim_live(claim):
        if agent and claim.get("agent") == agent:
            lines.append(f"{card_id} is already yours until "
                         f"{model.iso(claim.get('expires'))} "
                         f"({model.humanize(model.ttl_left(claim))} left)")
        else:
            lines.append(f"{card_id} is taken — {claim.get('agent')} holds it until "
                         f"{model.iso(claim.get('expires'))} "
                         f"({model.humanize(model.ttl_left(claim))} left)")
    edges = model.dependencies(tx.cards())
    done = {
        str(c.get("id")) for c in tx.cards()
        if model.is_done_column(_board_of(tx, str(c.get("id") or "")), str(c.get("column") or ""))
    }
    for blocker in model.blocked_by_open(edges, card_id, done):
        lines.append(f"{card_id} also waits on {blocker} (still open)")
    # The card's own claim only fences anything while it is *live*: during a
    # renewal the caller's footprint includes the paths already on the card. Once
    # the lease has expired it is no longer a claim, so grafting its old globs
    # would deny a fresh taker for a dead owner's paths — the reverse of expiry.
    wanted = list(patterns or [])
    if claim and model.claim_live(claim):
        wanted += list(claim.get("paths") or [])
    for hit in _collisions(tx, card_id, wanted):
        lines.append(f"your paths collide: {hit['yours']} "
                     f"({hit['agent']} claimed {hit['pattern']} on {hit['card']})")
    return lines


def ready(tx: Tx, agent: str | None = None, patterns: list[str] | None = None) -> list[dict]:
    """Takeable cards: not finished, unclaimed, unblocked, path-free."""
    out: list[dict] = []
    for card in tx.cards():
        # A card sitting in a done column is finished work. It has no claim and
        # no open blocker, so the state test below would call it ``ready`` and an
        # agent would reopen something already merged. Filtering here, rather than
        # inventing a fifth state, keeps the four-word vocabulary the spec pins
        # and keeps ``ready`` membership and ``state == "ready"`` the same fact.
        if _is_done(tx, card):
            continue
        if state_of(tx, card, agent) != "ready":
            continue
        if patterns and _collisions(tx, str(card.get("id") or ""), patterns):
            continue
        out.append(card)
    # ``reversed`` already makes urgent the smallest rank, so the key is the
    # rank itself: negating it here offered ``none`` first and ``urgent`` last,
    # which is backwards for a queue agents take the head of.
    priority = {name: index for index, name in enumerate(reversed(model.PRIORITIES))}
    return sorted(out, key=lambda c: (
        priority.get(str(c.get("priority") or "none"), 0),
        model.iso(c.get("created") or ""), str(c.get("id")),
    ))


def _misnamed(tx: Tx) -> list[dict]:
    """Every card file whose ``id:`` disagrees with its filename.

    One record per file: the handle, the id it declares (``was``), the id its
    name says it should have (``card``), and ``block`` -- the file that makes the
    repair unsafe, or None.

    The filename is the card's address: ``Store.card_ids()`` lists stems,
    ``validate_card`` checks ``blocked_by`` against stems, and ``locate()``
    answers by name. A ``WEB-7.yaml`` declaring ``id: WEB-1`` is one card with
    two addresses and the readers disagree -- the next ``claim WEB-1`` finds it by
    content, writes ``WEB-1.yaml``, and leaves ``WEB-7.yaml`` as a stale twin that
    keeps appearing in ``list`` under an id nothing can load. It arrives from a
    merge: two clones mint ``WEB-1``, git conflicts on ``cards/WEB-1.yaml``, and
    the human keeps both copies, so one lands on a borrowed name still saying
    ``id: WEB-1``. Aligning ``id:`` to the name is the one repair two clones reach
    without talking, because git merges filenames and never a counter line.

    ``block`` is decided from the bytes as read, before any repair, so
    ``conflicts`` and ``conflicts_resolve`` agree: the report never offers a fix
    the resolve will refuse -- which matters because the browser's repair button
    is built from this record.
    """
    cards = tx.snapshot.all_cards()
    found: list[dict] = []
    for handle in cards:
        ident = str(handle.doc.get("id") or "")
        stem = handle.path.stem
        if ident and ident != stem:
            found.append({"handle": handle, "was": ident, "card": stem,
                          "file": _show(handle.path),
                          "block": _misname_block(cards, handle, stem)})
    return found


def _misname_block(cards: list, handle: Any, stem: str) -> Any | None:
    """Another file that makes this repair unsafe, or None.

    The name is claimed twice over, and both count: another file already *named*
    ``stem`` (in either directory, since one id may not live in two places), or
    another file that *declares* it from somewhere else.

    The stem half is not redundant with the id half. ``cards/WEB-2.yaml`` and
    ``archive/WEB-2.yaml`` both declaring ``id: WEB-1`` is two files that want to
    become ``WEB-2``; repairing both yields two cards of one id, and
    ``write_card`` then unlinks the live/archive sibling -- so checking only
    "would this write overwrite a path" lets both through and deletes one of them
    on the write.

    Every clash is refused rather than arbitrated. Picking which claimant keeps a
    name is a question about intent, and the guess costs a card.
    """
    return next((other for other in cards if other is not handle
                 and (other.path.stem == stem
                      or str(other.doc.get("id") or "") == stem)), None)


def _misname_detail(record: dict) -> str:
    """One sentence about one file: what is wrong, and what happens next.

    Names the file, not just the id: "WEB-1 exists 2 times" cannot be acted on
    when neither file is called WEB-1 twice, so the reader has nothing to open.

    Each misnamed file gets its own line even when two of them block each other,
    which prints the pair twice from opposite ends. That is the honest count --
    both files are individually unfixable -- and grouping them into one line would
    claim a single action settles both, when it only settles the one touched.
    """
    other = record["block"]
    if other is None:
        return (f"{record['file']} declares id:{' '}{record['was']!r} but is named "
                f"{record['card']}; the name is the address, so its id becomes "
                f"{record['card']!r}")
    held = str(other.doc.get("id") or "")
    return (f"{record['file']} cannot become {record['card']}: "
            f"{_show(other.path)} holds that name"
            + (f" (it declares id:{' '}{held!r})" if held != record["card"] else "")
            + " - delete or renumber one of the two")


def _show(path: Any) -> str:
    """A card path as ``cards/WEB-2.yaml``, not a 60-character absolute path.

    Two files in different directories share a basename, and a message that
    prints ``WEB-2.yaml collides with WEB-2.yaml`` says nothing about which is
    which; both are named below the store root, which is the part that differs.
    """
    try:
        return str(Path(path).relative_to(Path(path).parents[1]))
    except (IndexError, ValueError):
        return Path(path).name


def conflicts(tx: Tx, dirty: list[str] | None = None) -> list[dict]:
    """Overlaps git could not see, reported without writing anything.

    Detection never mutates: the winner is derived from data already on disk, so
    two clones asking independently get the same answer, and a report cannot
    become a silent edit of somebody else's claim.
    """
    found: list[dict] = []
    live = [
        {"id": str(c.get("id")), "claim": model.claim_of(c)}
        for c in tx.all_cards()
        if model.claim_of(c) and model.claim_live(model.claim_of(c))
    ]
    seen: set[tuple[str, str]] = set()
    for index, left in enumerate(live):
        for right in live[index + 1:]:
            if left["id"] == right["id"]:
                continue
            key = tuple(sorted((left["id"], right["id"])))
            if key in seen:
                continue
            pairs = [
                {"a": str(mine), "b": str(theirs)}
                for mine in (left["claim"].get("paths") or [])
                for theirs in (right["claim"].get("paths") or [])
                if _safe_overlap(mine, theirs)
            ]
            if not pairs:
                continue
            seen.add(key)
            winner, loser = _rank(left, right)
            found.append({
                "kind": "overlap",
                "winner": {"card": winner["id"], "agent": winner["claim"].get("agent"),
                           "at": model.iso(winner["claim"].get("at")),
                           "paths": list(winner["claim"].get("paths") or [])},
                "loser": {"card": loser["id"], "agent": loser["claim"].get("agent"),
                          "at": model.iso(loser["claim"].get("at")),
                          "paths": list(loser["claim"].get("paths") or [])},
                "patterns": pairs,
            })

    misnamed = _misnamed(tx)
    for record in misnamed:
        found.append({"kind": "misnamed", "card": record["card"],
                      "was": record["was"], "file": record["file"],
                      "fixable": record["block"] is None,
                      "detail": _misname_detail(record)})

    counts: dict[str, int] = {}
    for card in tx.all_cards():
        ident = str(card.get("id") or "")
        if ident:
            counts[ident] = counts.get(ident, 0) + 1
    for ident, times in sorted(counts.items()):
        if times > 1:
            found.append({"kind": "duplicate", "card": ident, "times": times,
                          "detail": f"{ident} exists {times} times — two clones minted it"})

    if dirty:
        for holder in live:
            hits: list[str] = []
            files: list[str] = []
            for mine in (holder["claim"].get("paths") or []):
                matched = _matches_any(mine, dirty)
                if matched:
                    hits.append(str(mine))
                    files.extend(matched)
            if hits:
                found.append({
                    "kind": "dirty", "card": holder["id"],
                    "agent": holder["claim"].get("agent"), "patterns": hits,
                    # ``patterns`` is the claim, ``files`` is what to go look at.
                    # Naming only the glob makes the report unfalsifiable — the
                    # reader cannot tell whether the other agent really did edit
                    # ``src/api/routes.py`` or just something far away under
                    # ``src/api/**``, which is the difference between stopping
                    # and carrying on.
                    "files": sorted(set(files)),
                    "detail": f"{holder['id']} claims {', '.join(hits)} but the "
                              f"working tree already has changes in "
                              f"{', '.join(sorted(set(files)))}",
                })
    return found


def _safe_overlap(left: Any, right: Any) -> bool:
    try:
        return model.patterns_overlap(left, right)
    except ValidationError:
        return True                      # an unparseable pattern is a collision


def _matches_any(pattern: str, paths: list[str]) -> list[str]:
    import fnmatch
    out: list[str] = []
    for path in paths:
        clean = str(path).replace("\\", "/").strip("/")
        try:
            pattern = model.normalize_pattern(pattern)
        except ValidationError:
            out.append(clean)
            continue
        if fnmatch.fnmatch(clean, pattern) or fnmatch.fnmatch(clean, pattern.rstrip("/") + "/**"):
            out.append(clean)
    return out


def _rank(left: dict, right: dict) -> tuple[dict, dict]:
    """Earliest ``at`` wins; ties by the lexicographically smaller agent name.

    Deterministic so every clone names the same winner without talking to each
    other, which is the only arbitration available between two machines that
    merged cleanly.
    """
    def key(item: dict) -> tuple[str, str]:
        claim = item["claim"]
        return (str(model.iso(claim.get("at")) or ""), str(claim.get("agent") or ""))
    a, b = key(left), key(right)
    return (left, right) if a <= b else (right, left)


# --------------------------------------------------------------------------- #
# claims
# --------------------------------------------------------------------------- #

def claim_create(tx: Tx, args: dict) -> dict:
    """Take a card for a bounded time, declaring the paths the work will touch.

    Three refusals, each actionable: the card is held, a path is spoken for, the
    card is blocked. All the reasons are reported at once — see
    :func:`why_not_ready`.
    """
    card_id = str(_need(args, "id"))
    card = tx.card(card_id)
    agent = _person_arg(tx, args, "agent")
    patterns = _patterns(args.get("paths"))
    for pattern in patterns:
        if model.pattern_is_loose(pattern, tx.store.root.parent):
            raise BadRequest(
                f"--paths {pattern!r} is a directory: it would claim every file "
                f"under it. Name the files, or write {pattern.rstrip('/')}/**"
            )

    existing = model.claim_of(card)
    if existing and model.claim_live(existing):
        if existing.get("agent") == agent and _covers(existing.get("paths"), patterns):
            return {"id": card_id, "agent": agent, "unchanged": True,
                    "expires": model.iso(existing.get("expires"))}
        raise ClaimDenied(
            why_not_ready(tx, card, agent, patterns)
            + [_next(tx, agent)],
            {"reason": "taken", "holder": existing.get("agent"),
             "expires": model.iso(existing.get("expires"))},
        )

    blockers = why_not_ready(tx, card, agent, patterns)
    if blockers:
        raise ClaimDenied(blockers + [_next(tx, agent)],
                          {"reason": "ready_check", "card": card_id})

    board = _board_of(tx, card_id)
    seconds = model.lease_seconds(args.get("ttl"), board)
    at = model.now()
    claim: dict[str, Any] = {
        "agent": agent,
        "at": at,
        "expires": at + datetime.timedelta(seconds=seconds),
    }
    if patterns:
        claim["paths"] = patterns
    if args.get("note"):
        claim["note"] = str(args["note"]).strip()
    if existing and existing.get("agent") and existing.get("agent") != agent:
        claim["preempted"] = existing["agent"]

    model.set_field(card, "card", "claim", claim)
    model.set_field(card, "card", "assignee", agent)
    tx.dirty(card)                        # deliberately no _touched: see banner
    return {"id": card_id, "agent": agent, "expires": model.iso(claim["expires"]),
            "paths": patterns, "preempted": claim.get("preempted")}


def _covers(held: Any, wanted: list[str]) -> bool:
    """Do the already-held paths already include everything being re-asked for?"""
    held = [str(p) for p in (held or [])]
    return all(any(model.patterns_overlap(want, have) for have in held) for want in wanted)


def _next(tx: Tx, agent: str | None) -> str:
    return f"next: yamlboard ready --as {agent}" if agent else "next: yamlboard ready"


def claim_renew(tx: Tx, args: dict) -> dict:
    card_id = str(_need(args, "id"))
    card = tx.card(card_id)
    agent = _person_arg(tx, args, "agent")
    claim = model.claim_of(card)
    if not claim or not claim.get("agent"):
        raise ClaimDenied([f"{card_id} is not claimed — nothing to renew", _next(tx, agent)],
                          {"reason": "unclaimed"})
    if claim.get("agent") != agent:
        raise ClaimDenied(
            [f"{card_id} is held by {claim.get('agent')}, not {agent}", _next(tx, agent)],
            {"reason": "not_yours"},
        )
    board = _board_of(tx, card_id)
    seconds = model.lease_seconds(args.get("ttl"), board)
    before = model.iso(claim.get("expires"))
    at = model.now()
    live = model.claim_live(claim, at)
    model.set_field(claim, "claim", "expires", at + datetime.timedelta(seconds=seconds))
    if not live:
        model.set_field(claim, "claim", "preempted", agent)
        model.set_field(claim, "claim", "at", at)
    tx.dirty(card)
    return {"id": card_id, "agent": agent, "was": before,
            "expires": model.iso(claim["expires"]), "retaken": not live}


def claim_release(tx: Tx, args: dict) -> dict:
    card_id = str(_need(args, "id"))
    card = tx.card(card_id)
    agent = _person_arg(tx, args, "agent")
    force = _bool_arg(args, "force", False)
    claim = model.claim_of(card)
    if not claim or not claim.get("agent"):
        return {"id": card_id, "released": False, "reason": "was not claimed"}
    holder = claim.get("agent")
    if holder != agent:
        if not model.claim_live(claim):
            pass                                  # an expired claim is not theirs either
        elif not force:
            raise ClaimDenied(
                [f"{card_id} is held by {holder}, not {agent} — releasing someone "
                 "else's live claim needs --force", _next(tx, agent)],
                {"reason": "not_yours", "holder": holder},
            )
        else:
            model.set_field(card, "card", "released_by", agent)
    card.pop("claim", None)
    if card.get("assignee") == holder:
        card.pop("assignee", None)
    tx.dirty(card)
    return {"id": card_id, "released": True, "was": holder}


def _repair_misnames(tx: Tx, wanted: str) -> tuple[list[dict], list[str]]:
    """Rewrite each misnamed card's ``id:`` so it matches its filename.

    Returns ``(repairs, needs_human)``. A repair is a content edit, not a file
    move: once ``id:`` says ``WEB-7`` the doc names the file it already lives in,
    so ``write_card`` rewrites the path it was read from and no rename, counter
    bump, or archive dance is involved.

    Which cards may be repaired comes from :func:`_misnamed`, the same decision
    ``conflicts`` reports, so the browser never renders a repair button for a card
    this function will refuse.

    ``blocked_by`` holds ids that validation resolves against the card set, so
    references follow the id only when the old id is left with no owner: in the
    usual merge pair (``WEB-1.yaml`` correct, ``WEB-2.yaml`` misnamed) ``WEB-1``
    still resolves, and rewriting a dependency to ``WEB-2`` would silently
    re-point it at a different card.
    """
    repairs: list[dict] = []
    needs_human: list[str] = []
    cards = tx.snapshot.all_cards()
    for record in _misnamed(tx):
        handle, ident, stem = record["handle"], record["was"], record["card"]
        if wanted and stem != wanted:
            continue
        if record["block"] is not None:
            needs_human.append(_misname_detail(record))
            continue
        card = handle.doc
        adopted = any(other is not handle and str(other.doc.get("id") or "") == ident
                      for other in cards)
        model.set_field(card, "card", "id", stem)
        model.set_field(card, "card", "updated", model.now())
        repointed = 0
        if not adopted:
            for other in cards:
                blockers = other.doc.get("blocked_by")
                if model.is_list(blockers) and ident in blockers:
                    other.doc["blocked_by"] = [stem if b == ident else b
                                               for b in blockers]
                    repointed += 1
                    tx.dirty(other.doc)
        tx.dirty(card)
        repairs.append({"kind": "misnamed", "card": stem, "was": ident,
                        "file": record["file"], "repointed": repointed})
    return repairs, needs_human


def conflicts_resolve(tx: Tx, args: dict) -> dict:
    """Settle what the board can settle: free the overlap loser, fix bad names.

    Explicit only, and idempotent: re-running after the winner's file has already
    arrived from a pull changes nothing, because the loser's claim is no longer
    live and therefore no longer reported, and a card whose ``id:`` already
    matches its filename is not misnamed.
    """
    wanted = str(args.get("card") or "").strip()
    resolved: list[dict] = []
    for report in conflicts(tx):
        if report["kind"] != "overlap":
            continue
        loser = report["loser"]
        if wanted and loser["card"] != wanted:
            continue
        card = tx.find_card(loser["card"])
        claim = model.claim_of(card)
        if card is None or not claim:
            continue
        if claim.get("overridden_by"):
            continue
        model.set_field(claim, "claim", "overridden_by", report["winner"]["agent"])
        if card.get("assignee") == claim.get("agent"):
            card.pop("assignee", None)
        tx.dirty(card)
        resolved.append({"kind": "overlap", "card": loser["card"],
                         "agent": claim.get("agent"),
                         "overridden_by": report["winner"]["agent"]})

    renamed, needs_human = _repair_misnames(tx, wanted)
    resolved.extend(renamed)
    if wanted and not resolved:
        raise BadRequest(f"no unresolved conflict on card {wanted!r}")
    return {"resolved": resolved, "count": len(resolved), "needs_human": needs_human}


# --------------------------------------------------------------------------- #
# cards
# --------------------------------------------------------------------------- #

def card_create(tx: Tx, args: dict) -> dict:
    board = _board(tx, args)
    column_id = str(args.get("column") or (model.column_ids(board) or [""])[0])
    _column(board, column_id)

    priority = _one_of(args, "priority", model.PRIORITIES) or "none"
    assignee = args.get("assignee")
    if assignee in ("", None):
        assignee = None
    elif not model.is_str(assignee):
        raise BadRequest("assignee must be text")
    elif assignee not in _people(tx):
        raise BadRequest(f"assignee {assignee!r} is not a person")

    labels = args.get("labels") or []
    if not model.is_list(labels):
        raise BadRequest("labels must be a list")
    known = {str(lbl.get("id")) for lbl in model.labels(tx.doc)}
    for value in labels:
        if value not in known:
            raise BadRequest(f"label {value!r} is not defined")

    neighbours = [c for c in tx.cards()
                  if c.get("column") == column_id
                  and model.board_for_card(tx.doc, str(c.get("id") or "")) is board]
    anchor = None
    after = args.get("after")
    if after:
        anchor = next((c for c in neighbours if c.get("id") == after), None)
    order = (model.order_between(anchor, None) if anchor
             else float(model.ORDER_GAP * (len(neighbours) + 1)))

    card: dict[str, Any] = {
        "id": "",                                 # allocated after validation
        "title": _need_text(args, "title"),
        "desc": _text(args, "desc", "") or "",
        "column": column_id,
        "order": float(order),
        "labels": list(labels),
        "priority": priority,
        "archived": False,
        "created": model.now(),
        "updated": model.now(),
    }
    if assignee is not None:
        model.set_field(card, "card", "assignee", assignee)
    if args.get("due"):
        due = model.parse_date(args["due"])
        if due is None:
            raise BadRequest(f"due must look like 2026-12-01 (got {args['due']!r})")
        model.set_field(card, "card", "due", due)
    if args.get("estimate") not in (None, ""):
        estimate = args["estimate"]
        if not model.is_int(estimate) or estimate < 0:
            raise BadRequest("estimate must be a whole number of 0 or more")
        model.set_field(card, "card", "estimate", estimate)
    if args.get("links"):
        if not model.is_list(args["links"]):
            raise BadRequest("links must be a list")
        model.set_field(card, "card", "links", [_link(link) for link in args["links"]])
    if args.get("subtasks"):
        if not model.is_list(args["subtasks"]):
            raise BadRequest("subtasks must be a list")
        model.set_field(card, "card", "subtasks", [_subtask(s) for s in args["subtasks"]])
    fields = _custom_values(board, args.get("fields") or {})
    if fields:
        model.set_field(card, "card", "fields", fields)
    blockers = _blockers_arg(tx, args.get("blocked_by") or [], "")
    if blockers:
        model.set_field(card, "card", "blocked_by", blockers)

    # the id is issued only now that every guard has passed: next_card_id
    # advances the board's counter, and a rejected create must not burn a number
    card["id"] = model.next_card_id(board, tx.card_ids())
    tx.touch()
    tx.create(card)
    return {"id": card["id"]}


CARD_PATCH_KEYS = (
    "title", "desc", "column", "labels", "priority", "assignee", "due",
    "estimate", "links", "subtasks", "fields", "blocked_by",
)


def card_update(tx: Tx, args: dict) -> dict:
    card_id = str(_need(args, "id"))
    card = tx.card(card_id)
    board = _board_of(tx, card_id)
    patch = args.get("patch")
    if not model.is_map(patch):
        raise BadRequest("patch must be a mapping")
    # an unknown key here was silently dropped, so `patch: {colour: red}`
    # answered 200 while changing nothing
    unknown = sorted(str(k) for k in patch if str(k) not in CARD_PATCH_KEYS)
    if unknown:
        raise BadRequest(f"unknown patch key {unknown[0]!r} (known: {', '.join(CARD_PATCH_KEYS)})")
    for name in ("title", "desc"):
        if name in patch:
            value = patch[name]
            if value is None:
                card[name] = ""
            elif not model.is_str(value):
                raise BadRequest(f"{name} must be text")
            else:
                card[name] = value
    if "due" in patch:
        # Stored as a native YAML date so `due: 2026-11-24` stays unquoted in the
        # file; a blank means "no due date", not "empty string".
        value = patch["due"]
        if value in (None, ""):
            card.pop("due", None)
        else:
            due = model.parse_date(value)
            if due is None:
                raise BadRequest(f"due must look like 2026-12-01 (got {value!r})")
            model.set_field(card, "card", "due", due)
    if "column" in patch:
        column_id = patch["column"]
        if not model.is_str(column_id):
            raise BadRequest("column must be text")
        _column(board, column_id)
        card["column"] = column_id
        if model.is_done_column(board, column_id):
            _release_here(tx, card, "done")
    if "priority" in patch:
        card["priority"] = _one_of({"priority": patch["priority"]}, "priority", model.PRIORITIES)
    if "assignee" in patch:
        value = patch["assignee"]
        if value in ("", None):
            card.pop("assignee", None)
        elif not model.is_str(value):
            raise BadRequest("assignee must be text")
        elif value not in _people(tx):
            raise BadRequest(f"assignee {value!r} is not a person")
        else:
            model.set_field(card, "card", "assignee", value)
    if "labels" in patch:
        values = patch["labels"] or []
        if not model.is_list(values):
            raise BadRequest("labels must be a list")
        known = {str(lbl.get("id")) for lbl in model.labels(tx.doc)}
        for item in values:
            if not model.is_str(item) or item not in known:
                raise BadRequest(f"label {item!r} is not defined")
        model.set_list(card, "card", "labels", list(values))
    if "estimate" in patch:
        value = patch["estimate"]
        if value in ("", None):
            card.pop("estimate", None)
        elif not model.is_int(value) or value < 0:
            raise BadRequest("estimate must be a whole number of 0 or more")
        else:
            model.set_field(card, "card", "estimate", value)
    if "links" in patch:
        if patch["links"] and not model.is_list(patch["links"]):
            raise BadRequest("links must be a list")
        model.set_list(card, "card", "links", [_link(item) for item in (patch["links"] or [])])
    if "subtasks" in patch:
        if patch["subtasks"] and not model.is_list(patch["subtasks"]):
            raise BadRequest("subtasks must be a list")
        model.set_list(card, "card", "subtasks", [_subtask(s) for s in (patch["subtasks"] or [])])
    if "fields" in patch:
        values = _custom_values(board, patch["fields"] or {}, existing=card.get("fields"))
        if values:
            model.set_field(card, "card", "fields", values)
        else:
            card.pop("fields", None)
    if "blocked_by" in patch:
        blockers = _blockers_arg(tx, patch["blocked_by"] or [], card_id)
        if blockers:
            model.set_field(card, "card", "blocked_by", blockers)
        else:
            card.pop("blocked_by", None)
        _require_acyclic(tx)

    _touched(card)
    tx.dirty(card)
    return {"id": card["id"]}


def _release_here(tx: Tx, card: dict, why: str) -> None:
    """Dropping into a done column frees the card without a second call."""
    claim = model.claim_of(card)
    if not claim:
        return
    card.pop("claim", None)
    if claim.get("agent") and card.get("assignee") == claim.get("agent"):
        card.pop("assignee", None)
    tx.notes.setdefault("released", []).append({"card": str(card.get("id")), "why": why})


def card_delete(tx: Tx, args: dict) -> dict:
    card_id = str(_need(args, "id"))
    tx.card(card_id)
    board = _board_of(tx, card_id)
    tx.remove(card_id)
    _unblock_everyone(tx, card_id)
    return {"id": card_id, "board": board.get("id")}


def card_move(tx: Tx, args: dict) -> dict:
    """Move a card to ``column`` in front of ``before`` (or to the end).

    Position is a number on the card, so a drag rewrites one file: a drag used to
    relocate the card's whole block in a shared array, which meant two people
    reordering one stretch always conflicted in git.
    """
    card_id = str(_need(args, "id"))
    card = tx.card(card_id)
    source = _board_of(tx, card_id)
    target = _board(tx, args) if args.get("board") else source
    column_id = str(_need(args, "column"))
    _column(target, column_id)

    cross = target is not source
    if cross:
        target_key = str(target.get("key"))
        if any(str(c.get("id")) == f"{target_key}-{card_id.split('-')[-1]}" for c in tx.all_cards()):
            raise BadRequest(f"{target_key}-{card_id.split('-')[-1]} already exists")

    # Already sorted by `tx.cards()`; the moved card is excluded so the gap is
    # computed against the column as it will read afterwards.
    neighbours = [c for c in tx.cards()
                  if c.get("column") == column_id and c.get("id") != card_id
                  and model.board_for_card(tx.doc, str(c.get("id") or "")) is target]
    before = args.get("before")
    # `before` names the card the moved one goes in FRONT of: that is what a drag
    # means, since the drop hint is drawn above a card, and it is what the word
    # says. It was read as the predecessor -- `order_between(anchor, None)` lands
    # one slot after its anchor -- so every drag into the middle of a column
    # landed one place below the gap the reader aimed at, and a drop above the
    # last card silently appended.
    if before and str(before) == card_id and not cross:
        # "Put X in front of X" asks for no movement, and the card is excluded
        # from `neighbours` so it would otherwise resolve to no-target and take
        # the append below -- a silent trip to the bottom of the column. Leaving
        # `order` None writes no position field, which is the one reading that
        # cannot misplace anybody.
        order = None
    else:
        index = next((i for i, c in enumerate(neighbours)
                      if c.get("id") == str(before)), None) if before else None
        if index is None:
            # No target named, or the named card has gone (deleted, or moved
            # column between the drag and the write). Appending is the only
            # fallback that cannot misplace somebody else's card.
            left, right = (neighbours[-1] if neighbours else None), None
        else:
            left = neighbours[index - 1] if index > 0 else None
            right = neighbours[index]
        try:
            order = model.order_between(left, right)
        except ReorderExhausted:
            order = None        # the column is renumbered after the move lands

    card["column"] = column_id
    if order is not None:
        model.set_field(card, "card", "order", float(order))
    if cross:
        old_id = card_id
        card["id"] = f"{target.get('key')}-{card_id.split('-')[-1]}"
        _reconcile_fields(card, target)
        _retarget_dependencies(tx, old_id, card["id"])
        tx.remove(old_id)
        tx.create(card)
        tx._renamed.append((old_id, card["id"]))
        tx.touch()
    else:
        tx.dirty(card)
    if model.is_done_column(target, column_id):
        _release_here(tx, card, "done")
    _rebalance_if_needed(tx, target, column_id)
    _touched(card)
    return {"id": card["id"], "cross_board": cross}


def _rebalance_if_needed(tx: Tx, board: dict, column_id: str) -> None:
    """Renumber one column when two neighbours have no room left between them."""
    column = [c for c in tx.cards()
              if c.get("column") == column_id
              and model.board_for_card(tx.doc, str(c.get("id") or "")) is board]
    ordered = model.sort_cards(column)
    for left, right in zip(ordered, ordered[1:]):
        try:
            model.order_between(left, right)
        except ReorderExhausted:
            for update in model.rebalance(ordered):
                card = tx.find_card(update["id"])
                if card is not None:
                    model.set_field(card, "card", "order", update["order"])
                    tx.dirty(card)
            return


def _retarget_dependencies(tx: Tx, old_id: str, new_id: str) -> None:
    """Keep ``blocked_by`` edges pointing at a card that still exists."""
    for card in tx.all_cards():
        blockers = card.get("blocked_by") or []
        if old_id in blockers:
            model.set_list(card, "card", "blocked_by",
                           [new_id if b == old_id else b for b in blockers])
            tx.dirty(card)


def _unblock_everyone(tx: Tx, gone: str) -> None:
    """A deleted blocker must not leave cards waiting on nothing.

    ``validate`` rejects an unknown id in ``blocked_by``, so leaving the edge
    would make every surviving card that referenced it unreadable.

    An emptied list loses the key rather than becoming ``blocked_by: []``, the
    way ``card.dep.remove`` already behaves: a card that waits on nothing has no
    such field, and one that says ``[]`` is diff noise meaning exactly what its
    absent neighbour means.
    """
    for card in tx.all_cards():
        blockers = card.get("blocked_by") or []
        if gone not in blockers:
            continue
        left = [b for b in blockers if b != gone]
        if left:
            model.set_list(card, "card", "blocked_by", left)
        else:
            card.pop("blocked_by", None)
        tx.dirty(card)


def card_archive(tx: Tx, args: dict) -> dict:
    card_id = str(_need(args, "id"))
    card = tx.card(card_id)
    wanted = _bool_arg(args, "archived", True)
    model.set_field(card, "card", "archived", wanted)
    if wanted:
        _release_here(tx, card, "archived")
    tx.dirty(card)
    return {"id": card_id, "archived": wanted,
            "moved": tx.store.archive(card_id).name if wanted else None}


def card_unarchive(tx: Tx, args: dict) -> dict:
    card_id = str(_need(args, "id"))
    card = tx.card(card_id)
    model.set_field(card, "card", "archived", False)
    tx.dirty(card)
    return {"id": card_id, "archived": False}


def card_done(tx: Tx, args: dict) -> dict:
    """Finish a card: to the board's done column, releasing its lease."""
    card_id = str(_need(args, "id"))
    card = tx.card(card_id)
    board = _board_of(tx, card_id)
    column_id = str(args.get("column") or "").strip()
    if column_id:
        _column(board, column_id)
    else:
        column_id = next((str(c.get("id")) for c in model.columns(board)
                          if c.get("is_done")), model.column_ids(board)[-1])
    card["column"] = column_id
    _release_here(tx, card, "done")
    _touched(card)
    tx.dirty(card)
    return {"id": card_id, "column": column_id, "released": True}


def card_comment(tx: Tx, args: dict) -> dict:
    card_id = str(_need(args, "id"))
    card = tx.card(card_id)
    text = _need_text(args, "text")
    author = args.get("by")
    if author in (None, ""):
        author = None
    elif not model.is_str(author):
        raise BadRequest("author must be text")
    elif author not in _people(tx):
        raise BadRequest(f"author {author!r} is not a person")
    comment = {"at": model.now(), "text": text}
    if author is not None:
        comment["by"] = author
    comments = card.get("comments")
    if comments is None:
        comments = []
        model.set_field(card, "card", "comments", comments)
    comments.append(comment)
    _touched(card)
    tx.dirty(card)
    return {"id": card_id, "count": len(comments)}


def card_comment_delete(tx: Tx, args: dict) -> dict:
    """Delete by the comment's own ``at`` stamp, falling back to ``index``.

    Positions shift when another agent posts or deletes between this client's
    render and its click; the stamp is the comment the person actually saw.
    """
    card_id = str(_need(args, "id"))
    card = tx.card(card_id)
    comments = card.get("comments") or []
    at = args.get("at")
    if at is not None:
        wanted = str(at)
        matches = [offset for offset, entry in enumerate(comments)
                   if model.is_map(entry) and model.iso(entry.get("at")) == wanted]
        if len(matches) != 1:
            raise BadRequest("comment not found" if not matches else "ambiguous comment")
        comments.pop(matches[0])
        _touched(card)
        tx.dirty(card)
        return {"id": card_id}
    index = args.get("index")
    if not model.is_int(index) or not 0 <= index < len(comments):
        raise BadRequest("index out of range")
    comments.pop(index)
    _touched(card)
    tx.dirty(card)
    return {"id": card_id}


def card_subtask_toggle(tx: Tx, args: dict) -> dict:
    card_id = str(_need(args, "id"))
    card = tx.card(card_id)
    subtasks = card.get("subtasks") or []
    index = args.get("index")
    if not model.is_int(index) or not 0 <= index < len(subtasks):
        raise BadRequest("index out of range")
    subtask = subtasks[index]
    subtask["done"] = bool(args.get("done", not subtask.get("done", False)))
    _touched(card)
    tx.dirty(card)
    return {"id": card_id, "done": subtask["done"]}


def _blockers_arg(tx: Tx, raw: Any, self_id: str) -> list[str]:
    if not model.is_list(raw):
        raise BadRequest("blocked_by must be a list of card ids")
    out: list[str] = []
    for value in raw:
        if not model.is_str(value) or not model.CARD_RE.match(value):
            raise BadRequest(f"blocked_by {value!r} is not a card id like WEB-1")
        if value == self_id:
            raise BadRequest("a card cannot block itself")
        if tx.find_card(value) is None:
            raise BadRequest(f"blocked_by {value!r} is not a card in this store")
        if value not in out:
            out.append(value)
    return out


def _edges(tx: Tx) -> dict[str, list[str]]:
    return model.dependencies(tx.all_cards())


def _require_acyclic(tx: Tx, start: str | None = None) -> None:
    cycle = model.find_cycle(_edges(tx), start)
    if cycle:
        raise BadRequest("blocked_by would form a cycle: " + " -> ".join(cycle))


def card_dep_add(tx: Tx, args: dict) -> dict:
    card_id = str(_need(args, "id"))
    card = tx.card(card_id)
    blocker = str(_need(args, "blocked_by"))
    if not model.CARD_RE.match(blocker):
        raise BadRequest(f"blocked_by {blocker!r} is not a card id like WEB-1")
    if blocker == card_id:
        raise BadRequest("a card cannot block itself")
    if tx.find_card(blocker) is None:
        raise BadRequest(f"blocked_by {blocker!r} is not a card in this store")
    blockers = [str(b) for b in (card.get("blocked_by") or [])]
    if blocker in blockers:
        return {"id": card_id, "blocked_by": blockers, "unchanged": True}
    blockers.append(blocker)
    model.set_list(card, "card", "blocked_by", blockers)
    _require_acyclic(tx, card_id)
    _touched(card)
    tx.dirty(card)
    return {"id": card_id, "blocked_by": blockers}


def card_dep_remove(tx: Tx, args: dict) -> dict:
    card_id = str(_need(args, "id"))
    card = tx.card(card_id)
    blocker = str(_need(args, "blocked_by"))
    blockers = [str(b) for b in (card.get("blocked_by") or []) if str(b) != blocker]
    if blockers:
        model.set_list(card, "card", "blocked_by", blockers)
    else:
        card.pop("blocked_by", None)
    _touched(card)
    tx.dirty(card)
    return {"id": card_id, "blocked_by": blockers}


# --------------------------------------------------------------------------- #
# columns
# --------------------------------------------------------------------------- #

def column_create(tx: Tx, args: dict) -> dict:
    board = _board(tx, args)
    name = str(_need(args, "name")).strip()
    column_id = model.unique_id(
        _wanted_id(args, model.slugify(name, "column")),
        set(model.column_ids(board)),
    )
    column: dict[str, Any] = {"id": column_id, "name": name}
    color = args.get("color")
    if color:
        column["color"] = str(color)
    if args.get("wip_limit") not in (None, ""):
        limit = args["wip_limit"]
        if not model.is_int(limit) or limit < 1:
            raise BadRequest("wip_limit must be a whole number of 1 or more")
        column["wip_limit"] = limit
    if args.get("is_done"):
        column["is_done"] = True

    columns = board.setdefault("columns", [])
    index = len(columns)
    after = args.get("after")
    if after:
        for offset, existing in enumerate(columns):
            if model.is_map(existing) and existing.get("id") == after:
                index = offset + 1
                break
    columns.insert(index, column)
    tx.touch()
    return {"id": column_id}


def column_update(tx: Tx, args: dict) -> dict:
    board = _board(tx, args)
    column = _column(board, str(_need(args, "id")))
    if "name" in args and str(args["name"]).strip():
        column["name"] = str(args["name"]).strip()
    if "color" in args:
        color = args["color"]
        if color in (None, ""):
            column.pop("color", None)
        else:
            column["color"] = str(color)
    if "wip_limit" in args:
        limit = args["wip_limit"]
        if limit in (None, "", 0):
            column.pop("wip_limit", None)
        elif not model.is_int(limit) or limit < 1:
            raise BadRequest("wip_limit must be a whole number of 1 or more")
        else:
            column["wip_limit"] = limit
    if "is_done" in args:
        if args["is_done"]:
            column["is_done"] = True
        else:
            column.pop("is_done", None)
    tx.touch()
    return {"id": column["id"]}


def column_reorder(tx: Tx, args: dict) -> dict:
    """Move ``id`` to the slot ``before`` held in the list as the caller saw it.

    The anchor's *original index* is the target: removing the moving column
    first, then inserting at that same index, lands "move right onto my right
    neighbour" after the neighbour, which is what every caller means by it.
    Scanning the post-removal list instead made every right-move a silent no-op.
    """
    board = _board(tx, args)
    column_id = str(_need(args, "id"))
    columns = board.get("columns") or []
    moving = _column(board, column_id)
    before = args.get("before")
    if before == column_id:
        return {"id": column_id}
    position = None
    for offset, existing in enumerate(columns):
        if model.is_map(existing) and existing.get("id") == before:
            position = offset
            break
    columns.remove(moving)
    if position is not None:
        position = min(position, len(columns))
        columns.insert(position, moving)
    else:
        columns.append(moving)
    tx.touch()
    return {"id": column_id}


def column_delete(tx: Tx, args: dict) -> dict:
    """Delete a column; its cards move to ``to`` (default: the first column).

    Cards are rewritten before ``board.yaml`` loses the column, so a crash
    between the writes leaves a valid store rather than cards pointing at a
    column that no longer exists.
    """
    board = _board(tx, args)
    column_id = str(_need(args, "id"))
    column = _column(board, column_id)
    remaining = [str(c.get("id")) for c in model.columns(board) if c.get("id") != column_id]
    if not remaining:
        raise BadRequest("a board needs at least one column")
    target = str(args.get("to") or remaining[0])
    if target not in remaining:
        raise BadRequest(f"column {target!r} does not exist")

    moved = 0
    for card in tx.all_cards():
        if (model.is_map(card) and card.get("column") == column_id
                and model.board_for_card(tx.doc, str(card.get("id") or "")) is board):
            card["column"] = target
            tx.dirty(card)
            moved += 1
    columns = board.get("columns") or []
    columns.remove(column)
    tx.touch()
    return {"id": column_id, "moved": moved, "to": target}


# --------------------------------------------------------------------------- #
# boards
# --------------------------------------------------------------------------- #

DEFAULT_COLUMNS = (("backlog", "Backlog"), ("doing", "In Progress"), ("done", "Done"))


def board_create(tx: Tx, args: dict) -> dict:
    title = _need_text(args, "title")
    existing = model.boards(tx.doc)
    board_id = model.unique_id(
        _wanted_id(args, model.slugify(title, "board")),
        {str(b.get("id")) for b in existing if model.is_map(b)},
    )
    key = str(args.get("key") or model.board_key(title, _taken_keys(tx)))
    key = key.strip().upper()
    if not model.KEY_RE.match(key):
        raise BadRequest("key must be 2-8 letters/digits starting with a letter")
    if key in _taken_keys(tx):
        raise BadRequest(f"key {key!r} is already used by another board")

    columns = args.get("columns")
    if columns is None:
        column_specs = [{"id": cid, "name": name} for cid, name in DEFAULT_COLUMNS]
        column_specs[-1]["is_done"] = True
    else:
        if not model.is_list(columns) or not columns:
            raise BadRequest("columns must be a non-empty list")
        column_specs = []
        for spec in columns:
            if model.is_str(spec):
                wanted, label = model.slugify(spec, "column"), spec.strip()
            elif model.is_map(spec) and spec.get("name"):
                wanted = model.slugify(str(spec.get("id") or spec["name"]), "column")
                label = str(spec["name"]).strip()
            else:
                raise BadRequest("each column needs a name")
            # ["In Progress", "in-progress"] both slugified to `in-progress`
            # and the whole create died in validate; a suffix keeps both
            column_specs.append({
                "id": model.unique_id(wanted, {c["id"] for c in column_specs}),
                "name": label,
            })

    board: dict[str, Any] = {
        "id": board_id,
        "key": key,
        "title": title,
        "columns": column_specs,
        "next_card": 1,
    }
    tx.doc.setdefault("boards", []).append(board)
    tx.touch()
    return {"id": board_id, "key": key}


def board_update(tx: Tx, args: dict) -> dict:
    board = _board(tx, args)
    if "title" in args and str(args["title"]).strip():
        board["title"] = str(args["title"]).strip()
    if "lease_ttl" in args:
        value = args["lease_ttl"]
        if value in (None, ""):
            board.pop("lease_ttl", None)
        else:
            try:
                model.parse_ttl(value)
            except ValidationError as exc:
                raise BadRequest(str(exc)) from exc
            model.set_field(board, "board", "lease_ttl", str(value))
    if "key" in args:
        key = str(args["key"]).strip().upper()
        if not model.KEY_RE.match(key):
            raise BadRequest("key must be 2-8 letters/digits starting with a letter")
        if key in _taken_keys(tx) and key != board.get("key"):
            raise BadRequest(f"key {key!r} is already used by another board")
        mine = [str(c.get("id")) for c in tx.all_cards()
                if model.board_for_card(tx.doc, str(c.get("id") or "")) is board]
        if mine and key != board.get("key"):
            raise BadRequest(
                f"cannot rename board {board.get('id')!r} to key {key!r} while it "
                f"holds {len(mine)} card(s) — each id is its filename, so renaming "
                "means renaming every file and rewriting every blocked_by edge"
            )
        old = str(board.get("key"))
        board["key"] = key
        highest = _max_number(board, mine)
        current = board.get("next_card")
        floor = current if model.is_int(current) and current > 0 else 0
        model.set_field(board, "board", "next_card", max(highest + 1, floor))
    tx.touch()
    return {"id": board["id"], "key": board.get("key"), "old_key": old if "key" in args else None}


def board_delete(tx: Tx, args: dict) -> dict:
    board = _board(tx, args)
    mine = [str(c.get("id")) for c in tx.all_cards()
            if model.board_for_card(tx.doc, str(c.get("id") or "")) is board]
    if mine and not _bool_arg(args, "force", False):
        raise BadRequest(
            f"board {board.get('id')!r} still holds {len(mine)} card(s) — pass force "
            "to delete them with it"
        )
    for card_id in mine:
        tx.remove(card_id)
        _unblock_everyone(tx, card_id)
    boards = tx.doc.setdefault("boards", [])
    target = next((b for b in boards if b.get("id") == board.get("id")), None)
    if target is not None:
        del boards[boards.index(target)]
    tx.touch()
    return {"id": board["id"], "deleted_cards": len(mine)}


# --------------------------------------------------------------------------- #
# people and labels
# --------------------------------------------------------------------------- #

def person_create(tx: Tx, args: dict) -> dict:
    name = _need_text(args, "name")
    ident = model.unique_id(
        _wanted_id(args, model.slugify(name, "person")),
        _people(tx),
    )
    person: dict[str, Any] = {"id": ident, "name": name}
    if args.get("color"):
        person["color"] = str(args["color"])
    _section(tx, "people").append(person)
    tx.touch()
    return {"id": ident}


def person_update(tx: Tx, args: dict) -> dict:
    person = _person(tx, args)
    if "name" in args and str(args["name"]).strip():
        person["name"] = str(args["name"]).strip()
    if "color" in args:
        if args["color"] in (None, ""):
            person.pop("color", None)
        else:
            person["color"] = str(args["color"])
    tx.touch()
    return {"id": person["id"]}


def person_delete(tx: Tx, args: dict) -> dict:
    """Remove a person, clearing every reference first.

    Order matters: cards are written before ``board.yaml`` drops the entry, so an
    interrupted run leaves a person nobody references rather than a card pointing
    at someone the file does not define — the second makes every later read fail.
    """
    person = _person(tx, args)
    unassigned = 0
    for card in tx.all_cards():
        if not model.is_map(card):
            continue
        touched = False
        if card.get("assignee") == person["id"]:
            card.pop("assignee", None)
            unassigned += 1
            touched = True
        for comment in card.get("comments") or []:
            if model.is_map(comment) and comment.get("by") == person["id"]:
                comment.pop("by", None)
                touched = True
        claim = model.claim_of(card)
        if claim and claim.get("agent") == person["id"]:
            card.pop("claim", None)
            touched = True
        for key in ("preempted", "overridden_by", "released_by"):
            if claim is not None and claim.get(key) == person["id"]:
                claim.pop(key, None)
                touched = True
        if touched:
            tx.dirty(card)
    tx.doc.get("people", []).remove(person)
    tx.touch()
    return {"id": person["id"], "unassigned": unassigned}


def _person(tx: Tx, args: dict) -> Any:
    ident = _need_text(args, "id")
    for person in model.people(tx.doc):
        if person.get("id") == ident:
            return person
    raise NotFound(f"person {ident!r}")


def label_create(tx: Tx, args: dict) -> dict:
    name = _need_text(args, "name")
    ident = model.unique_id(
        _wanted_id(args, model.slugify(name, "label")),
        {str(lbl.get("id")) for lbl in model.labels(tx.doc)},
    )
    label: dict[str, Any] = {"id": ident, "name": name}
    if args.get("color"):
        label["color"] = str(args["color"])
    _section(tx, "labels").append(label)
    tx.touch()
    return {"id": ident}


def label_update(tx: Tx, args: dict) -> dict:
    label = _label(tx, args)
    if "name" in args and str(args["name"]).strip():
        label["name"] = str(args["name"]).strip()
    if "color" in args:
        if args["color"] in (None, ""):
            label.pop("color", None)
        else:
            label["color"] = str(args["color"])
    tx.touch()
    return {"id": label["id"]}


def label_delete(tx: Tx, args: dict) -> dict:
    label = _label(tx, args)
    stripped = 0
    for card in tx.all_cards():
        if model.is_map(card) and label["id"] in (card.get("labels") or []):
            model.set_list(card, "card", "labels",
                           [kept for kept in card["labels"] if kept != label["id"]])
            tx.dirty(card)
            stripped += 1
    tx.doc.get("labels", []).remove(label)
    tx.touch()
    return {"id": label["id"], "removed_from": stripped}


def _label(tx: Tx, args: dict) -> Any:
    ident = str(_need(args, "id"))
    for label in model.labels(tx.doc):
        if label.get("id") == ident:
            return label
    raise NotFound(f"label {ident!r}")


# --------------------------------------------------------------------------- #
# custom fields
# --------------------------------------------------------------------------- #

def field_create(tx: Tx, args: dict) -> dict:
    board = _board(tx, args)
    name = str(_need(args, "name")).strip()
    kind = _one_of(args, "type", model.FIELD_TYPES) or "text"
    options = args.get("options") or []
    if not model.is_list(options):
        raise BadRequest("options must be a list")
    options = [str(o).strip() for o in options if str(o).strip()]
    if kind == "select" and not options:
        raise BadRequest("a select field needs at least one option")

    specs = board.setdefault("custom_fields", [])
    ident = model.unique_id(
        _wanted_id(args, model.slugify(name, "field")),
        {str(f.get("id")) for f in model.custom_fields(board)},
    )
    spec: dict[str, Any] = {"id": ident, "name": name, "type": kind}
    if options:
        spec["options"] = options
    if _bool_arg(args, "required", False):
        spec["required"] = True
    specs.append(spec)
    tx.touch()
    return {"id": ident}


def field_update(tx: Tx, args: dict) -> dict:
    board = _board(tx, args)
    ident = _need_text(args, "id")
    spec = next((f for f in model.custom_fields(board) if f.get("id") == ident), None)
    if spec is None:
        raise NotFound(f"custom field {ident!r}")
    if "name" in args and str(args["name"]).strip():
        spec["name"] = str(args["name"]).strip()
    if "options" in args:
        options = [str(o).strip() for o in (args["options"] or []) if str(o).strip()]
        # Only a select has options. The settings panel sends `options: []`
        # for every field type, and blanking card values on that key alone
        # silently wiped a text field's values on a plain rename.
        if spec.get("type") == "select":
            if not options:
                raise BadRequest("a select field needs at least one option")
            if options != [str(o) for o in (spec.get("options") or [])]:
                for card in tx.all_cards():
                    if not model.is_map(card):
                        continue
                    if model.board_for_card(tx.doc, str(card.get("id") or "")) is not board:
                        continue
                    value = (card.get("fields") or {}).get(ident)
                    if value not in (None, "") and value not in options:
                        card["fields"][ident] = ""
                        tx.dirty(card)
                model.set_list(spec, "card", "options", options)
    if "required" in args:
        # Flipping `required` never rewrites existing cards: like Jira's
        # required fields, it binds the cards created from now on
        # (_custom_values enforces it on card.create).
        if _bool_arg(args, "required", False):
            spec["required"] = True
        else:
            spec.pop("required", None)
    tx.touch()
    return {"id": ident}


def field_delete(tx: Tx, args: dict) -> dict:
    board = _board(tx, args)
    ident = _need_text(args, "id")
    spec = next((f for f in model.custom_fields(board) if f.get("id") == ident), None)
    if spec is None:
        raise NotFound(f"custom field {ident!r}")
    for card in tx.all_cards():
        if not model.is_map(card) or ident not in (card.get("fields") or {}):
            continue
        if model.board_for_card(tx.doc, str(card.get("id") or "")) is not board:
            continue
        card["fields"].pop(ident, None)
        if not card["fields"]:
            card.pop("fields", None)    # `fields: {}` is diff noise, not data
        tx.dirty(card)
    board.get("custom_fields", []).remove(spec)
    tx.touch()
    return {"id": ident}


# --------------------------------------------------------------------------- #
# board-level document
# --------------------------------------------------------------------------- #

def document_update(tx: Tx, args: dict) -> dict:
    doc = tx.doc
    if "title" in args:
        title = args["title"]
        if title in (None, ""):
            doc.pop("title", None)
        elif not model.is_str(title):
            raise BadRequest("title must be text")
        elif not title.strip():
            raise BadRequest("title must not be blank")
        else:
            doc["title"] = title
    tx.touch()
    return {"title": doc.get("title")}


def summary(tx: Tx, *, dirty: list[str] | None = None,
            agent: str | None = None) -> dict:
    """The whole store as one document: the board, its cards, and who holds what.

    Cards are injected into ``doc.boards[<id>].cards`` rather than returned as a
    sibling list, because that is the shape the browser already renders — the
    split into files is a storage decision, and there is no reason to spend a UI
    rewrite announcing it. The derived keys each card carries (``state``,
    ``live``, ``overlaps``, ``open_blockers``, ``claim.left``) are computed here
    so the CLI and the browser cannot disagree about who holds what: both ask
    this one function. ``agent`` is the caller's own identity; pass it and a
    card that caller holds reads ``yours`` instead of ``taken``. The browser
    leaves it empty, because which identity a person is using is a per-tab
    choice that app.js already applies to its own rendering — baking one
    viewer's choice into a shared payload would misreport it to the next.
    """
    doc = tx.doc
    edges = _edges(tx)
    done = {str(c.get("id")) for c in tx.all_cards() if _is_done(tx, c)}
    reports = conflicts(tx, dirty)
    overlapping = {
        who for report in reports if report["kind"] == "overlap"
        for who in (report["winner"]["card"], report["loser"]["card"])
    }

    json_doc = model.jsonable(doc)
    by_board: dict[str, list] = {str(b.get("id")): [] for b in model.boards(json_doc)}
    archived: list[str] = []
    for card in tx.all_cards():
        card_id = str(card.get("id") or "")
        entry = model.jsonable(card)
        entry["state"] = state_of(tx, card, agent)
        claim = model.claim_of(card)
        live = bool(claim and model.claim_live(claim))
        entry["live"] = live
        entry["overlaps"] = card_id in overlapping
        entry["open_blockers"] = model.blocked_by_open(edges, card_id, done)
        entry["archived"] = tx.is_archived(card_id) or bool(card.get("archived"))
        if live:
            entry["claim"] = dict(entry.get("claim") or {})
            entry["claim"]["left"] = model.ttl_left(claim)
            entry["claim"]["span"] = model.claim_span(claim)
        if entry["archived"]:
            archived.append(card_id)
            continue
        home = model.board_for_card(doc, card_id)
        bucket = by_board.get(str(home.get("id")) if home else "")
        if bucket is not None:
            bucket.append(entry)
    for bucket in by_board.values():
        bucket.sort(key=lambda c: (float(c.get("order") or 0), str(c.get("id"))))
    for board in model.boards(json_doc):
        board["cards"] = by_board.get(str(board.get("id")), [])

    agents: dict[str, dict] = {}
    for card in tx.all_cards():
        claim = model.claim_of(card)
        if claim and model.claim_live(claim):
            info = agents.setdefault(str(claim.get("agent")), {"cards": []})
            info["cards"].append(str(card.get("id")))
    for holder, info in agents.items():
        person = model.jsonable(next(
            (p for p in model.people(doc) if p.get("id") == holder), {}))
        info["name"] = person.get("name") or holder
        info["color"] = person.get("color")

    return {
        "schema_version": model.VERSION,
        "doc": json_doc,
        "ready": [str(c.get("id")) for c in ready(tx, agent)],
        "agents": agents,
        "conflicts": reports,
        "archived": sorted(archived),
        "tokens": dict(tx.tokens) or tokens_of(tx),
    }


def tokens_of(tx: Tx) -> dict[str, str]:
    """Every file's current identity, keyed ``"board"`` or by card id.

    The browser echoes these back with its next op: the only check that catches a
    hand edit or a ``git pull`` landing between a render and a click, which is why
    they are per file and not one number for the whole store.
    """
    out = {"board": tx.snapshot.board.token()}
    for handle in tx.snapshot.all_cards():
        out[str(handle.doc.get("id") or handle.path.stem)] = handle.token()
    return out


def _is_done(tx: Tx, card: dict) -> bool:
    board = model.board_for_card(tx.doc, str(card.get("id") or ""))
    return bool(board and model.is_done_column(board, str(card.get("column") or "")))


# --------------------------------------------------------------------------- #
# the registry
# --------------------------------------------------------------------------- #

#: Every operation, mutating and read-only. ``cli.VERBS`` is checked against the
#: mutating half of this by ``test_api``; an op added here without a CLI verb, or
#: the other way round, fails the suite.
OPS: dict[str, Callable[[Tx, dict], dict]] = {
    "card.create": card_create,
    "card.update": card_update,
    "card.delete": card_delete,
    "card.move": card_move,
    "card.archive": card_archive,
    "card.unarchive": card_unarchive,
    "card.done": card_done,
    "card.comment": card_comment,
    "card.comment.delete": card_comment_delete,
    "card.subtask.toggle": card_subtask_toggle,
    "card.dep.add": card_dep_add,
    "card.dep.remove": card_dep_remove,
    "claim.create": claim_create,
    "claim.renew": claim_renew,
    "claim.release": claim_release,
    "conflicts.resolve": conflicts_resolve,
    "column.create": column_create,
    "column.update": column_update,
    "column.reorder": column_reorder,
    "column.delete": column_delete,
    "board.create": board_create,
    "board.update": board_update,
    "board.delete": board_delete,
    "person.create": person_create,
    "person.update": person_update,
    "person.delete": person_delete,
    "label.create": label_create,
    "label.update": label_update,
    "label.delete": label_delete,
    "field.create": field_create,
    "field.update": field_update,
    "field.delete": field_delete,
    "document.update": document_update,
}


def apply(store: Any, payload: dict, *, tx: Tx | None = None) -> dict:
    """Run one op against a store and return ``{"op", "result"}``.

    Always commits: one payload, one snapshot, one write. There is no way to
    batch two ops behind one ``Tx`` on purpose — a caller that wanted that would
    have to decide what "the first landed and the second was refused" means, and
    every answer to that is worse than two writes. Raising anything at all leaves
    the store untouched, because :meth:`Tx.flush` validates the whole store
    before its first byte moves.
    """
    if not model.is_map(payload):
        raise BadRequest("payload must be a JSON object")
    name = payload.get("op")
    if not model.is_str(name):
        raise BadRequest("op is required")
    handler = OPS.get(name)
    if handler is None:
        raise BadRequest(f"unknown op {name!r} (known: {', '.join(sorted(OPS))})")
    args = payload.get("args") or {}
    if not model.is_map(args):
        raise BadRequest("args must be a mapping")
    tx = tx or Tx(store)
    result = handler(tx, args)
    tx.flush()
    return {"op": name, "result": result or {}}
