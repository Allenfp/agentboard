"""Schema, validation and rules for a agentboard store.

The parsed YAML mapping *is* the model: hand-editing a card stays possible, and
every write goes through this same document so comments and key order survive.
There is deliberately no dataclass mirror to keep in sync.

This module owns the rules that must be identical whether a change arrives from
the CLI or the browser: the closed key sets, claim liveness, path-pattern
intersection, and the dependency graph. It reads and writes nothing - a store
lives in ``store.py`` - and every decision about *time* goes through :func:`now`
so lease behaviour is testable without sleeping.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import math
import re
from pathlib import Path
from typing import Any, TypeGuard

#: What this build writes, and the highest version it will read. A file at a
#: different version is refused, never upgraded in place: there is no second
#: reader and no migration bridge, because a silently-misread board is worse
#: than a loud refusal.
VERSION = 2

PRIORITIES = ("none", "low", "med", "high", "urgent")
FIELD_TYPES = ("text", "select", "date", "number", "bool")

#: How far apart freshly created cards sit in the ordering space, and the legal
#: lease range. Wide spacing means an insert writes one card, not a column.
ORDER_GAP = 1024.0
TTL_DEFAULT = "30m"
TTL_MIN = 30
TTL_MAX = 24 * 3600

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
KEY_RE = re.compile(r"^[A-Z][A-Z0-9]{1,7}$")
CARD_RE = re.compile(r"^([A-Z][A-Z0-9]{1,7})-(\d{1,9})$")
URL_RE = re.compile(r"^(https?://|mailto:)", re.I)
TTL_RE = re.compile(r"^(\d+)([smhd])$")

# The closed key sets. `_check_keys` rejects anything not listed here *on read*,
# so a key used in code but missing from a tuple makes a real person's card
# permanently unreadable - add the key and its test in the same commit.
TOP_KEYS = ("version", "title", "people", "labels", "boards")
PEOPLE_KEYS = ("id", "name", "color")
LABEL_KEYS = ("id", "name", "color")
COLUMN_KEYS = ("id", "name", "color", "wip_limit", "is_done")
#: No `cards:` here on purpose - cards are files. `next_card` stays: minting an
#: id is board-level state, and `lease_ttl` lets a board set its own default.
BOARD_KEYS = ("id", "key", "title", "columns", "custom_fields", "next_card", "lease_ttl")
CARD_KEYS = (
    "id", "title", "desc", "column", "order", "labels", "priority", "assignee",
    "due", "estimate", "links", "subtasks", "comments", "fields", "claim",
    "blocked_by", "released_by", "archived", "created", "updated",
)
#: The complete claim vocabulary. There is no `status` key: a second source of
#: truth for "who holds this" is how a board starts lying about itself. A claim
#: is live iff `expires` is in the future and `overridden_by` is unset.
CLAIM_KEYS = (
    "agent", "at", "expires", "paths", "note", "preempted", "overridden_by",
)
LINK_KEYS = ("url", "title")
SUBTASK_KEYS = ("text", "done")
COMMENT_KEYS = ("at", "by", "text")
CUSTOM_KEYS = ("id", "name", "type", "options", "required")


class ValidationError(Exception):
    """Raised when a data file or an API payload is not usable.

    ``errors`` holds one human-readable message per problem, each prefixed with
    the YAML path it came from so a person can find and fix it by hand.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = [str(e) for e in errors] or ["invalid data"]
        super().__init__("; ".join(self.errors))


# --------------------------------------------------------------------------- #
# type predicates
# --------------------------------------------------------------------------- #

#: Each one is a TypeGuard, not a bool: the payload a caller reads out of a card
#: is `Any`, and a predicate that answers only "yes" leaves a type checker blind
#: to what the caller then does with it. Narrowing here is what lets pyright see
#: that `if not is_map(patch): raise` means `patch` is a mapping for the rest of
#: the function. The names stay `is_*`, because they are published surface (P13).
def is_str(value: Any) -> TypeGuard[str]:
    return isinstance(value, str)


def is_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def is_num(value: Any) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_bool(value: Any) -> TypeGuard[bool]:
    return isinstance(value, bool)


def is_list(value: Any) -> TypeGuard[list]:
    return isinstance(value, list)


def is_map(value: Any) -> TypeGuard[dict]:
    return isinstance(value, dict)


def slugify(text: str, fallback: str = "x") -> str:
    """Turn a display name into an id slug safe for :data:`SLUG_RE`.

    The fallback is sanitised the same way as the text, so every return value
    matches ``SLUG_RE`` for any input (``slugify("!!!", "-")`` was an
    ``IndexError``-shaped trap).
    """
    # The fallback is sanitised like the text, and then stripped of leading
    # punctuation: `SLUG_RE` wants an alphanumerical first character, so a
    # fallback of "-" would fail the one guarantee this function exists to give.
    safe = re.sub(r"[^a-z0-9_-]", "", fallback or "").lstrip("-_") or "x"
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")[:40]
    slug = slug.strip("-") or safe
    if slug[0].isdigit():
        slug = f"{safe}-{slug}"
    return slug


def board_key(title: str, taken: set[str] | None = None) -> str:
    """Derive a card-key prefix (``"Website"`` -> ``WEB``) avoiding collisions."""
    words = re.findall(r"[A-Za-z]+", title or "")
    base = (words[0][:3] if words else "BD").upper()
    if not KEY_RE.match(base):
        base = "BD"
    taken = taken or set()
    if base not in taken:
        return base
    for n in range(2, 1000):
        candidate = f"{base[:2]}{n}"
        if candidate not in taken:
            return candidate
    raise ValidationError([f"could not allocate a board key near {title!r}"])


# --------------------------------------------------------------------------- #
# lookups
# --------------------------------------------------------------------------- #

def boards(doc: dict) -> list:
    """The board list as a **read-only copy** — mutating it changes nothing.

    Writers must go through ``doc.setdefault("boards", [])``; the individual
    board mappings returned by :func:`find_board` are live and safe to mutate.
    """
    return list(doc.get("boards") or [])


def find_board(doc: dict, board_id: str) -> Any | None:
    for board in boards(doc):
        if is_map(board) and board.get("id") == board_id:
            return board
    return None


def board_for_card(doc: dict, card_id: str) -> Any | None:
    """The board whose ``key`` prefixes ``card_id`` — a card's home by its id."""
    match = CARD_RE.match(str(card_id or ""))
    if not match:
        return None
    for board in boards(doc):
        if is_map(board) and board.get("key") == match.group(1):
            return board
    return None


def columns(board: dict) -> list:
    return [c for c in (board.get("columns") or []) if is_map(c)]


def column_ids(board: dict) -> list[str]:
    """Column ids as written. No coercion: ``validate`` requires slugs, so a
    non-string id is an error the file must fix, not something to launder."""
    return [c.get("id") for c in columns(board)]


def find_column(board: dict, column_id: str) -> Any | None:
    for column in columns(board):
        if column.get("id") == column_id:
            return column
    return None


def is_done_column(board: dict, column_id: str) -> bool:
    column = find_column(board, column_id)
    return bool(column and column.get("is_done"))


def people(doc: dict) -> list:
    """Read-only copy of the people list (see :func:`boards`)."""
    return [p for p in (doc.get("people") or []) if is_map(p)]


def person_ids(doc: dict) -> set[str]:
    """The agent allowlist. An identity absent from here is refused, never created."""
    return {str(p.get("id")) for p in people(doc) if is_str(p.get("id"))}


def labels(doc: dict) -> list:
    return [lbl for lbl in (doc.get("labels") or []) if is_map(lbl)]


def custom_fields(board: dict) -> list:
    return [f for f in (board.get("custom_fields") or []) if is_map(f)]


def unique_id(wanted: str, taken: set[str]) -> str:
    """Append ``-2``, ``-3``... until ``wanted`` is free.

    Rejects a non-slug up front so the error names the payload the person just
    typed, instead of a 422 three layers later blaming ``people[3].id``.
    """
    if not is_str(wanted) or not SLUG_RE.match(wanted):
        raise ValidationError([f"id {wanted!r} must be a slug like 'bug-triage'"])
    if wanted not in taken:
        return wanted
    for n in range(2, 1000):
        candidate = f"{wanted}-{n}"
        if candidate not in taken:
            return candidate
    raise ValidationError([f"could not allocate a unique id near {wanted!r}"])


def next_card_id(board: dict, existing: list[str] | None = None) -> str:
    """Issue the next ``KEY-n`` id for ``board`` and advance its counter.

    ``next_card`` means "the id the next card gets". The counter is raised past
    every id already on the board - which since v2 means ids the *caller* found
    by listing files, since the board no longer carries a card list - so an id
    written by hand can never be handed out twice. Two clones can still mint the
    same id; that is a duplicate filename for ``conflicts`` to report, not
    something a counter here can prevent.
    """
    key = str(board.get("key") or "BD")
    counter = board.get("next_card")
    counter = counter if is_int(counter) and counter > 0 else 1
    highest = 0
    for card_id in existing or []:
        match = CARD_RE.match(str(card_id or ""))
        if match and match.group(1) == key:
            highest = max(highest, int(match.group(2)))
    issued = max(counter, highest + 1)
    if issued > 10 ** 9 - 1:
        raise ValidationError([f"board {key}: card id sequence exhausted"])
    set_field(board, "board", "next_card", issued + 1)
    return f"{key}-{issued}"


def now() -> dt.datetime:
    """Timestamp for ``created``/``updated``/``at``, and the only clock there is.

    A real datetime, not a string: ruamel writes it back unquoted
    (``created: 2026-11-22T19:50:48``), matching how these files spell dates by
    hand. Convention: naive *local* time - every stamp the app writes shares one
    clock. ``validate`` accepts naive or aware stamps so a hand-edited UTC offset
    survives, but nothing may compare stamps across the two spellings.

    Lease liveness reads time *only* through here, which is what makes an expiry
    test by patching one function rather than sleeping in the suite.
    """
    return dt.datetime.now().replace(microsecond=0)


# --------------------------------------------------------------------------- #
# leases
# --------------------------------------------------------------------------- #

_TTL_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_ttl(text: Any, default: str = TTL_DEFAULT) -> int:
    """Seconds for a ``90s``/``5m``/``2h``/``1d`` lease, or a refusal.

    Out-of-range is refused rather than silently clamped: a caller who asks for
    one second and gets thirty has written a lease that outlives the command they
    were guarding, and nothing told them.
    """
    text = str(text or default).strip().lower()
    match = TTL_RE.match(text)
    if not match:
        raise ValidationError(
            [f"--ttl {text!r}: expected a duration like 90s, 5m, 2h or 1d"]
        )
    seconds = int(match.group(1)) * _TTL_UNIT[match.group(2)]
    if not TTL_MIN <= seconds <= TTL_MAX:
        raise ValidationError(
            [f"--ttl {text!r}: {seconds}s is outside the range "
             f"{TTL_MIN}s to {TTL_MAX // 3600}h"]
        )
    return seconds


def board_ttl(board: dict | None) -> int:
    """The board's own default lease, or the global default."""
    wanted = (board or {}).get("lease_ttl") or TTL_DEFAULT
    return parse_ttl(wanted)


def lease_seconds(requested: Any, board: dict | None) -> int:
    """Seconds for one lease: the caller's ``--ttl``, else the board's default.

    Split from :func:`parse_ttl` because the two arguments have different
    provenance — ``--ttl`` is a person's mistake if malformed, while
    ``lease_ttl`` in ``board.yaml`` is already validated on read. Falling back to
    the board's value rather than the global constant is the point: a board that
    says ``lease_ttl: 2h`` has said so about every card on it.
    """
    if requested in (None, ""):
        return board_ttl(board)
    return parse_ttl(requested)


def instant(value: Any) -> dt.datetime | None:
    """A written timestamp as a naive *local* datetime, or None if it is not one.

    Naive local because that is what :func:`now` returns, and every lease
    comparison below subtracts one from the other. An aware stamp is converted,
    not truncated: dropping ``tzinfo`` on ``...T12:15:39+00:00`` read as 12:15
    local, which here is a lease eight and a half hours longer than the one that
    was written — a card held long after its owner stopped working, with a
    countdown the browser computed correctly from the very same offset.
    """
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day)
    elif is_str(value):
        try:
            parsed = dt.datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def claim_of(card: dict | None) -> dict | None:
    claim = (card or {}).get("claim")
    return claim if is_map(claim) else None


def claim_live(claim: dict | None, at: dt.datetime | None = None) -> bool:
    """Is this claim still a claim?

    False for no claim, an overridden claim (the loser of a cross-machine
    overlap, kept for the audit trail but inert), or an expired one. Expiry *is*
    release: no daemon rewrites bytes to free a card, so liveness is decided here
    at read time and a board whose agents all died needs no janitor.
    """
    if not is_map(claim) or not claim.get("agent"):
        return False
    if claim.get("overridden_by"):
        return False
    expires = instant(claim.get("expires"))
    if expires is None:
        return False
    return expires > (at or now())


def ttl_left(claim: dict, at: dt.datetime | None = None) -> int:
    """Whole seconds of lease remaining, never negative."""
    expires = instant(claim.get("expires"))
    if expires is None:
        return 0
    return max(0, int((expires - (at or now())).total_seconds()))


def claim_span(claim: dict) -> int:
    """That claim's own full TTL in seconds, for the UI's amber threshold."""
    taken, expires = instant(claim.get("at")), instant(claim.get("expires"))
    if not taken or not expires:
        return TTL_MIN
    return max(1, int((expires - taken).total_seconds()))


def humanize(seconds: int) -> str:
    """`125` -> `2m`, `59` -> `59s`, `7300` -> `2h` - one unit, terse."""
    if seconds >= 3600:
        return f"{seconds // 3600}h{'' if seconds % 3600 < 60 else f'{seconds % 3600 // 60}m'}"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{max(0, seconds)}s"


# --------------------------------------------------------------------------- #
# path patterns
# --------------------------------------------------------------------------- #

def normalize_pattern(pattern: Any) -> str:
    """A usable relative pattern, or a refusal naming what was wrong.

    Absolute paths and ``..`` are refused rather than normalised: a claim is a
    promise other agents rely on, and ``../secrets/**`` silently rewriting to a
    relative path would be a promise nobody read.

    ``.`` and empty segments *are* collapsed, and that is a safety property, not
    tidiness: ``patterns_overlap`` compares segment lists, so a stored
    ``./src/api/**`` shares no leading segment with a live ``src/api/**`` and the
    two read as disjoint. Two agents then both claim the same tree, the exact
    loss the claim layer exists to prevent, and the ``./`` spelling is what a
    shell completion or an agent copying a relative path produces.
    """
    text = str(pattern or "").strip().replace("\\", "/")
    if not text:
        raise ValidationError(["--paths: empty pattern"])
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise ValidationError([f"--paths {text!r}: use a path relative to the repo root"])
    parts = text.split("/")
    if any(part == ".." for part in parts):
        raise ValidationError([f"--paths {text!r}: cannot reach outside the repo"])
    kept = [part for part in parts if part not in ("", ".")]
    return "/".join(kept) or "."


def patterns_overlap(left: Any, right: Any) -> bool:
    """Could these two patterns ever match the same file?

    Three ways to collide, and a claim only has to match one:

    * one *is* the other;
    * one covers the other as a subtree — ``src/api/**`` against
      ``src/api/routes.py``, where a bare directory path stands for everything
      under it. Deliberately loose: a ``*`` may name a directory, so ``src/*``
      counts as covering ``src/api/routes.py``;
    * the two pattern languages intersect without either containing the other —
      ``src/**`` and ``**/routes.py`` both match ``src/routes.py``. Not covered
      by the second case, which asks whether *every* path one pattern names is
      named by the other; a collision needs only one shared file.

    Ambiguity resolves to *overlap* — a false refusal costs the caller one extra
    ``ready`` call, a false accept costs two agents their work.
    """
    a, b = normalize_pattern(left), normalize_pattern(right)
    if a == b:
        return True
    return _covers(a, b) or _covers(b, a) or _intersect(a, b)


def _covers(pattern: str, other: str) -> bool:
    """Does ``pattern`` match every path ``other`` could name (or start with)?"""
    pat = pattern.rstrip("/").split("/")
    that = other.rstrip("/").split("/")
    return _seg(pat, that)


def _seg(pat: list[str], that: list[str]) -> bool:
    if not pat:
        return True                                  # a prefix covers a longer path
    head, rest = pat[0], pat[1:]
    if head == "**":
        if not rest:
            return True
        return any(_seg(rest, that[i:]) for i in range(len(that) + 1))
    if not that:
        return False
    if head == "*" or fnmatch.fnmatchcase(that[0], head):
        return _seg(rest, that[1:])
    return False


def _intersect(left: str, right: str) -> bool:
    """True when at least one concrete path matches both patterns.

    The patterns run as one product automaton whose states are
    ``(index in left, index in right)``. One step consumes a single path segment
    that both sides accept; a ``**`` may instead swallow that segment alone
    while the other side matches it, and it may match nothing at all (the
    epsilon step below). Only a *shared* path counts, which is what keeps
    ``a/secret.py`` and ``a/*.md`` apart — no path satisfies both — while
    ``src/**`` and ``**/routes.py`` meet at ``src/routes.py``, a collision
    containment alone misses because neither glob is a subset of the other.

    Every step advances an index, so the reachable set is finite and the walk
    needs no depth limit.
    """
    a = left.rstrip("/").split("/")
    b = right.rstrip("/").split("/")

    def closure(i: int, j: int) -> set[tuple[int, int]]:
        """Positions reachable from (i, j) with each ``**`` matching no segment."""
        return {(x, y) for x in _past_stars(a, i) for y in _past_stars(b, j)}

    states = closure(0, 0)
    if (len(a), len(b)) in states:
        return True
    seen = set(states)
    while states:
        nxt: set[tuple[int, int]] = set()
        for i, j in states:
            if i < len(a) and a[i] == "**" and j < len(b):
                nxt |= closure(i, j + 1)             # left's ** swallows the segment
            if j < len(b) and b[j] == "**" and i < len(a):
                nxt |= closure(i + 1, j)
            if i < len(a) and j < len(b) and a[i] != "**" and b[j] != "**" \
                    and _both_match(a[i], b[j]):
                nxt |= closure(i + 1, j + 1)
        if (len(a), len(b)) in nxt:
            return True
        states = nxt - seen
        seen |= states
    return False


def _past_stars(pat: list[str], i: int) -> list[int]:
    """``i`` plus every index after a run of ``**`` starting at ``i``."""
    out = [i]
    while i < len(pat) and pat[i] == "**":
        i += 1
        out.append(i)
    return out


def _both_match(one: str, other: str) -> bool:
    """Could one path segment satisfy both single-segment patterns?

    Two wildcards have to have nestable literals or nothing matches both. The
    leading literals are compared with ``startswith`` and the trailing ones with
    ``endswith``: ``a*.py`` and ``*b.py`` do meet at ``ab.py`` because ``b.py``
    ends with ``.py``, and reading that comparison the wrong way round silently
    unblocks two agents editing the same file.
    """
    if "*" not in one and "*" not in other:
        return one == other
    if "*" not in other:
        return fnmatch.fnmatchcase(other, one)
    if "*" not in one:
        return fnmatch.fnmatchcase(one, other)
    return _prefix_ok(one.split("*")[0], other.split("*")[0]) and _suffix_ok(
        one.rsplit("*", 1)[-1], other.rsplit("*", 1)[-1]
    )


def _prefix_ok(one: str, other: str) -> bool:
    return one.startswith(other) or other.startswith(one)


def _suffix_ok(one: str, other: str) -> bool:
    return one.endswith(other) or other.endswith(one)



def pattern_is_loose(pattern: Any, root: Any = None) -> bool:
    """True for a wildcard-free path that is a directory: ``src/api``, say.

    A directory pattern is a trap. It reads like the name of one file and, via
    :func:`_covers`, claims every file beneath it — 41 files for ``src/api``,
    which is the difference between a narrow claim and blocking the board.
    Refusing it only makes sense against a real tree, since a wildcard-free path
    that is *not* a directory is just a file, and a plain file path is the most
    precise claim an agent can make.
    """
    text = normalize_pattern(pattern)
    if "*" in text or root is None:
        return False
    return (Path(str(root)) / text).is_dir()


# --------------------------------------------------------------------------- #
# ordering
# --------------------------------------------------------------------------- #

def sort_cards(cards: list) -> list:
    """Display order: ``order`` then id, so a tie is still deterministic.

    Two clones that wrote the same position by coincidence must not render the
    column differently, and there is no shared array left to arbitrate it.
    """
    return sorted(
        (c for c in cards if is_map(c)),
        key=lambda c: (_order_of(c), str(c.get("id") or "")),
    )


def _order_of(card: dict) -> float:
    value = card.get("order")
    if is_num(value) and math.isfinite(float(value)):
        return float(value)
    return float(ORDER_GAP * 1000)


def order_between(before: Any, after: Any) -> float:
    """A position strictly between two neighbours.

    ``inf`` sentinels mean "start"/"end" of the column, so both drag-to-end and
    insert-in-the-middle are the same call.
    """
    low = _order_of(before) if is_map(before) else float("-inf")
    high = _order_of(after) if is_map(after) else float("inf")
    if low == float("-inf") and high == float("inf"):
        return ORDER_GAP
    if low == float("-inf"):
        return high - ORDER_GAP if high - ORDER_GAP > high / 2 else high / 2
    if high == float("inf"):
        return low + ORDER_GAP
    mid = (low + high) / 2
    if mid <= low or mid >= high:
        raise ReorderExhausted("no room left between these two cards")
    return mid


class ReorderExhausted(Exception):
    """Too many inserts in one gap to number between them.

    Not an error a person sees: the caller renumbers the column and retries. It
    is a separate exception rather than a bool so the retry cannot be forgotten.
    """


def rebalance(cards: list) -> list[dict]:
    """Whole-column renumbering, returned as ``[{id, order}]`` updates to write."""
    updates: list[dict] = []
    for index, card in enumerate(sort_cards(cards), start=1):
        wanted = float(index * ORDER_GAP)
        if _order_of(card) != wanted:
            updates.append({"id": str(card.get("id")), "order": wanted})
    return updates


# --------------------------------------------------------------------------- #
# dependency graph
# --------------------------------------------------------------------------- #

def dependencies(cards: list) -> dict[str, list[str]]:
    """``{card id: [ids it waits on]}``, ignoring junk a caller should not send."""
    edges: dict[str, list[str]] = {}
    for card in cards:
        if not is_map(card) or not is_str(card.get("id")):
            continue
        wanted = card.get("blocked_by") or []
        edges[card["id"]] = [b for b in wanted if is_str(b)] if is_list(wanted) else []
    return edges


def find_cycle(edges: dict[str, list[str]], start: str | None = None) -> list[str] | None:
    """A cycle reachable from ``start`` (or anywhere), as an ordered id path.

    The path is named in full because "refused: cycle" on a board with forty
    cards tells nobody which two edges to untangle.
    """
    path: list[str] = []
    settled: set[str] = set()

    def walk(node: str) -> list[str] | None:
        if node in path:
            return path[path.index(node):] + [node]
        if node in settled or node not in edges:
            return None
        path.append(node)
        for nxt in edges[node]:
            found = walk(nxt)
            if found:
                return found
        path.pop()
        settled.add(node)
        return None

    for root in ([start] if start else list(edges)):
        found = walk(root)
        if found:
            return found
    return None


def blocked_by_open(edges: dict[str, list[str]], card_id: str, done: set[str]) -> list[str]:
    """Blockers that are not finished - the reason a card is not ``ready``."""
    return [b for b in edges.get(card_id, []) if b not in done]


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #

def _dupe_ids(items: list, label: str, path: str, errors: list[str]) -> None:
    seen: set[str] = set()
    for index, item in enumerate(items):
        if not is_map(item):
            continue
        ident = item.get("id")
        if not is_str(ident):
            continue
        if ident in seen:
            errors.append(f"{path}[{index}].id: duplicate {label} id {ident!r}")
        seen.add(ident)


def _check_keys(obj: dict, allowed: tuple[str, ...], path: str, errors: list[str]) -> None:
    for key in obj:
        if key not in allowed:
            errors.append(f"{path}: unknown key {key!r} (allowed: {', '.join(allowed)})")


def _check_color(value: Any, path: str, errors: list[str]) -> None:
    if value is None:
        return
    if not is_str(value) or not re.match(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$", value):
        errors.append(f"{path}: color must be a hex string like '#2f6f4f'")


def _check_text(item: dict, key: str, path: str, errors: list[str]) -> None:
    """``item[key]`` has to be present, text, and not just spaces.

    It takes the mapping and the key rather than the value so the rule and its
    message live once: the six required-text fields on a board were six copies
    of the same two lines, which is how one of them ends up saying "required"
    in different words from the others.
    """
    value = item.get(key)
    if not is_str(value) or not value.strip():
        errors.append(f"{path}.{key}: required, non-empty text")


def _check_when(value: Any, path: str, errors: list[str]) -> None:
    """created/updated/claim.at: a datetime, a date, or a strict ISO string."""
    if value is None:
        return
    if isinstance(value, (dt.datetime, dt.date)) or instant(value) is not None:
        return
    errors.append(f"{path}: expected a timestamp like 2026-11-18T09:12:00")


def _check_person(
    value: Any, path: str, known: set[str], errors: list[str], *, what: str = "assignee"
) -> None:
    # Only null/empty means "nobody": "none" is a legal person id, and
    # priority already spells its own "none" value.
    if value is None or value == "":
        return
    if not is_str(value) or value not in known:
        errors.append(f"{path}: {what} {value!r} is not a person")


def check_custom_value(spec: dict, value: Any, path: str) -> list[str]:
    if value in (None, ""):
        return []
    kind = spec.get("type", "text")
    if kind == "select":
        options = spec.get("options") or []
        if value not in options:
            return [f"{path}: expected one of {', '.join(str(o) for o in options)}"]
    elif kind == "number":
        # NaN and Infinity are valid YAML floats but invalid JSON: storing one
        # makes every later /api/state unparsable by the browser.
        if not (is_int(value) or (isinstance(value, float) and math.isfinite(value))):
            return [f"{path}: expected a number"]
    elif kind == "bool":
        if not is_bool(value):
            return [f"{path}: expected true or false"]
    elif kind == "date":
        if parse_date(value) is None:
            return [f"{path}: expected a date like 2026-12-01"]
    elif not is_str(value):
        return [f"{path}: expected text"]
    return []


def _validate_claim(claim: Any, path: str, people_known: set[str], errors: list[str]) -> None:
    if not is_map(claim):
        errors.append(f"{path}: expected a mapping")
        return
    _check_keys(claim, CLAIM_KEYS, path, errors)
    _check_person(claim.get("agent"), f"{path}.agent", people_known, errors, what="agent")
    _check_when(claim.get("at"), f"{path}.at", errors)
    _check_when(claim.get("expires"), f"{path}.expires", errors)
    if claim.get("expires") is None:
        errors.append(f"{path}.expires: a claim without an expiry never frees the card")
    paths = claim.get("paths", [])
    if not is_list(paths):
        errors.append(f"{path}.paths: expected a list of path patterns")
    else:
        for index, pattern in enumerate(paths):
            try:
                normalize_pattern(pattern)
            except ValidationError as exc:
                errors.extend(f"{path}.paths[{index}]: {e}" for e in exc.errors)
    for key in ("preempted", "overridden_by"):
        if claim.get(key) is not None:
            _check_person(claim[key], f"{path}.{key}", people_known, errors, what=key)


def _validate_card(
    card: Any, board: dict, path: str, ctx: dict, errors: list[str]
) -> None:
    if not is_map(card):
        errors.append(f"{path}: expected a mapping")
        return
    _check_keys(card, CARD_KEYS, path, errors)

    card_id = card.get("id")
    if not is_str(card_id) or not CARD_RE.match(card_id):
        errors.append(f"{path}.id: expected an id like 'WEB-1'")
    elif card_id.split("-")[0] != board.get("key"):
        errors.append(
            f"{path}.id: {card_id!r} does not start with this board's key "
            f"{board.get('key')!r}"
        )

    _check_text(card, "title", path, errors)
    if not is_str(card.get("desc")) and card.get("desc") is not None:
        errors.append(f"{path}.desc: expected text")

    column = card.get("column")
    if not is_str(column) or column not in ctx["columns"]:
        errors.append(f"{path}.column: {column!r} is not a column of this board")

    order = card.get("order")
    if order is not None and (not is_num(order) or not math.isfinite(float(order))):
        errors.append(f"{path}.order: expected a finite number")

    if not is_bool(card.get("archived", False)) and card.get("archived") is not None:
        errors.append(f"{path}.archived: expected true or false")

    priority = card.get("priority", "none")
    if priority not in PRIORITIES:
        errors.append(f"{path}.priority: expected one of {', '.join(PRIORITIES)}")

    _check_person(card.get("assignee"), f"{path}.assignee", ctx["people"], errors)

    card_labels = card.get("labels", [])
    if not is_list(card_labels):
        errors.append(f"{path}.labels: expected a list")
    else:
        for value in card_labels:
            if not is_str(value) or value not in ctx["labels"]:
                errors.append(f"{path}.labels: {value!r} is not in labels")

    estimate = card.get("estimate")
    if estimate is not None and (not is_int(estimate) or estimate < 0):
        errors.append(f"{path}.estimate: expected a whole number of 0 or more")

    due = card.get("due")
    if due is not None and parse_date(due) is None:
        errors.append(f"{path}.due: expected a date like 2026-12-01")

    for index, link in enumerate(card.get("links") or []):
        subpath = f"{path}.links[{index}]"
        if not is_map(link):
            errors.append(f"{subpath}: expected a mapping")
            continue
        _check_keys(link, LINK_KEYS, subpath, errors)
        url = link.get("url")
        if not is_str(url) or not URL_RE.match(url):
            errors.append(f"{subpath}.url: expected an http(s):// or mailto: URL")

    for index, subtask in enumerate(card.get("subtasks") or []):
        subpath = f"{path}.subtasks[{index}]"
        if not is_map(subtask):
            errors.append(f"{subpath}: expected a mapping")
            continue
        _check_keys(subtask, SUBTASK_KEYS, subpath, errors)
        if not is_str(subtask.get("text")):
            errors.append(f"{subpath}.text: expected text")
        if not is_bool(subtask.get("done", False)) and subtask.get("done") is not None:
            errors.append(f"{subpath}.done: expected true or false")

    for index, comment in enumerate(card.get("comments") or []):
        subpath = f"{path}.comments[{index}]"
        if not is_map(comment):
            errors.append(f"{subpath}: expected a mapping")
            continue
        _check_keys(comment, COMMENT_KEYS, subpath, errors)
        if not is_str(comment.get("text")):
            errors.append(f"{subpath}.text: expected text")
        _check_when(comment.get("at"), f"{subpath}.at", errors)
        _check_person(comment.get("by"), f"{subpath}.by", ctx["people"], errors,
                      what="author")

    fields = card.get("fields")
    if fields is not None:
        if not is_map(fields):
            errors.append(f"{path}.fields: expected a mapping of custom field id to value")
        else:
            known = {f.get("id"): f for f in custom_fields(board)}
            for name, value in fields.items():
                if name not in known:
                    errors.append(f"{path}.fields: unknown custom field {name!r}")
                    continue
                errors.extend(check_custom_value(known[name], value, f"{path}.fields.{name}"))

    blockers = card.get("blocked_by", [])
    if not is_list(blockers):
        errors.append(f"{path}.blocked_by: expected a list of card ids")
    else:
        for value in blockers:
            if not is_str(value) or not CARD_RE.match(value):
                errors.append(f"{path}.blocked_by: {value!r} is not a card id")
            elif value == card_id:
                errors.append(f"{path}.blocked_by: a card cannot block itself")
            elif ctx.get("ids") is not None and value not in ctx["ids"]:
                errors.append(f"{path}.blocked_by: {value!r} is not a card in this store")

    if card.get("released_by") is not None:
        _check_person(card.get("released_by"), f"{path}.released_by", ctx["people"],
                      errors, what="released_by")

    if "claim" in card and card["claim"] is not None:
        _validate_claim(card["claim"], f"{path}.claim", ctx["people"], errors)

    _check_when(card.get("created"), f"{path}.created", errors)
    _check_when(card.get("updated"), f"{path}.updated", errors)


def validate(doc: Any) -> list[str]:
    """Every problem in a ``board.yaml`` document; empty means usable.

    Cards are not here - they are files, checked by :func:`validate_card`. This
    is pure: it never writes to ``doc``. An earlier version back-filled defaults
    here, so loading and saving stamped ``desc: ''`` onto cards nobody touched.
    """
    errors: list[str] = []
    if not is_map(doc):
        return ["board file must contain a mapping at the top level"]
    _check_keys(doc, TOP_KEYS, "document", errors)

    version = doc.get("version", VERSION)
    if not is_int(version) or version < 1:
        errors.append(f"version: expected {VERSION}, file says {version!r}")
    elif version > VERSION:
        errors.append(f"version: file says {version}, this build understands up to {VERSION}")
    elif version < VERSION:
        errors.append(
            f"version: file says {version}, this build writes {VERSION} - "
            "nothing was read; start a new board, or restore a backup written "
            f"by a build that wrote version {version}"
        )

    if "title" in doc and not is_str(doc["title"]):
        errors.append("title: expected text")

    people_list: list = doc.get("people", [])
    if not is_list(people_list):
        errors.append("people: expected a list")
        people_list = []
    for index, person in enumerate(people_list):
        path = f"people[{index}]"
        if not is_map(person):
            errors.append(f"{path}: expected a mapping")
            continue
        _check_keys(person, PEOPLE_KEYS, path, errors)
        if not is_str(person.get("id")) or not SLUG_RE.match(str(person.get("id"))):
            errors.append(f"{path}.id: expected a slug like 'allen'")
        _check_text(person, "name", path, errors)
        _check_color(person.get("color"), path, errors)
    _dupe_ids(people_list, "person", "document.people", errors)

    label_list: list = doc.get("labels", [])
    if not is_list(label_list):
        errors.append("labels: expected a list")
        label_list = []
    for index, label in enumerate(label_list):
        path = f"labels[{index}]"
        if not is_map(label):
            errors.append(f"{path}: expected a mapping")
            continue
        _check_keys(label, LABEL_KEYS, path, errors)
        if not is_str(label.get("id")) or not SLUG_RE.match(str(label.get("id"))):
            errors.append(f"{path}.id: expected a slug like 'bug'")
        _check_text(label, "name", path, errors)
        _check_color(label.get("color"), path, errors)
    _dupe_ids(label_list, "label", "document.labels", errors)

    board_list: list = doc.get("boards", [])
    if not is_list(board_list):
        errors.append("boards: expected a list")
        board_list = []

    board_ids: set[str] = set()
    board_keys: set[str] = set()
    for index, board in enumerate(board_list):
        path = f"boards[{index}]"
        if not is_map(board):
            errors.append(f"{path}: expected a mapping")
            continue
        _check_keys(board, BOARD_KEYS, path, errors)

        board_id = board.get("id")
        if not is_str(board_id) or not SLUG_RE.match(str(board_id)):
            errors.append(f"{path}.id: expected a slug like 'website'")
        elif board_id in board_ids:
            errors.append(f"{path}.id: duplicate board id {board_id!r}")
        board_ids.add(str(board_id))

        board_key_value = board.get("key")
        if not is_str(board_key_value) or not KEY_RE.match(str(board_key_value)):
            errors.append(f"{path}.key: expected 2-8 capitals like 'WEB'")
        elif board_key_value in board_keys:
            errors.append(f"{path}.key: duplicate board key {board_key_value!r}")
        else:
            board_keys.add(board_key_value)
        _check_text(board, "title", path, errors)

        if not is_list(board.get("columns")) or not board.get("columns"):
            errors.append(f"{path}.columns: expected a non-empty list")
            column_list: list = []
        else:
            column_list = board["columns"]
        for c_index, column in enumerate(column_list):
            c_path = f"{path}.columns[{c_index}]"
            if not is_map(column):
                errors.append(f"{c_path}: expected a mapping")
                continue
            _check_keys(column, COLUMN_KEYS, c_path, errors)
            if not is_str(column.get("id")) or not SLUG_RE.match(str(column.get("id"))):
                errors.append(f"{c_path}.id: expected a slug like 'doing'")
            _check_text(column, "name", c_path, errors)
            _check_color(column.get("color"), c_path, errors)
            limit = column.get("wip_limit")
            if limit is not None and (not is_int(limit) or limit < 1):
                errors.append(f"{c_path}.wip_limit: expected a whole number of 1 or more")
            if not is_bool(column.get("is_done", False)) and column.get("is_done") is not None:
                errors.append(f"{c_path}.is_done: expected true or false")
        _dupe_ids(column_list, "column", f"{path}.columns", errors)

        if not is_list(board.get("custom_fields", [])):
            errors.append(f"{path}.custom_fields: expected a list")
            field_list: list = []
        else:
            field_list = board.get("custom_fields") or []
        for f_index, spec in enumerate(field_list):
            f_path = f"{path}.custom_fields[{f_index}]"
            if not is_map(spec):
                errors.append(f"{f_path}: expected a mapping")
                continue
            _check_keys(spec, CUSTOM_KEYS, f_path, errors)
            if not is_str(spec.get("id")) or not SLUG_RE.match(str(spec.get("id"))):
                errors.append(f"{f_path}.id: expected a slug like 'customer'")
            _check_text(spec, "name", f_path, errors)
            if spec.get("type", "text") not in FIELD_TYPES:
                errors.append(f"{f_path}.type: expected one of {', '.join(FIELD_TYPES)}")
            if not is_list(spec.get("options", [])):
                errors.append(f"{f_path}.options: expected a list")
            if spec.get("type") == "select" and not (spec.get("options") or []):
                errors.append(f"{f_path}.options: a select field needs at least one option")
            if not is_bool(spec.get("required", False)) and spec.get("required") is not None:
                errors.append(f"{f_path}.required: expected true or false")
        _dupe_ids(field_list, "custom field", f"{path}.custom_fields", errors)

        counter = board.get("next_card", 1)
        if counter is not None and (
            not is_int(counter) or counter < 1 or counter > 10 ** 9 - 1
        ):
            errors.append(f"{path}.next_card: expected a whole number from 1 to 999999999")

        if board.get("lease_ttl") is not None:
            try:
                parse_ttl(board.get("lease_ttl"))
            except ValidationError as exc:
                errors.extend(f"{path}.lease_ttl: {e}" for e in exc.errors)

        # `cards:` is what a hand-edit reaches for when it expects the cards to
        # live inside the board. It is not in BOARD_KEYS, so the unknown-key
        # check above already said so - this line points at the fix instead of
        # leaving a person to guess.
        if "cards" in board:
            errors.append(
                f"{path}.cards: cards live in cards/<ID>.yaml, one file each - "
                "a board never holds them"
            )

    return errors


def card_context(doc: dict, board: dict, ids: set[str] | None = None) -> dict:
    """What :func:`validate_card` needs resolved once for a whole board."""
    return {
        "columns": set(column_ids(board)),
        "people": person_ids(doc),
        "labels": {lbl.get("id") for lbl in labels(doc) if is_str(lbl.get("id"))},
        "ids": ids,
    }


def validate_card(card: Any, board: dict, ctx: dict) -> list[str]:
    """Problems with one card file, named by its id."""
    errors: list[str] = []
    ident = card.get("id") if is_map(card) else None
    _validate_card(card, board, f"card {ident or '?'}", ctx, errors)
    return errors


# --------------------------------------------------------------------------- #
# writing helpers
# --------------------------------------------------------------------------- #

#: Key order for each mapping kind, so a save writes the layout a person would:
#: a diff shows the field that changed rather than a reshuffled mapping.
KEY_ORDER = {
    "card": CARD_KEYS,
    "board": BOARD_KEYS,
    "claim": CLAIM_KEYS,
    "document": TOP_KEYS,
}


def set_field(item: dict, kind: str, key: str, value: Any) -> None:
    """Set ``item[key]``, keeping the declared key order for ``kind``.

    A plain ``item[k] = v`` appends to a ruamel CommentedMap, which parks a new
    ``assignee`` under ``updated:`` and a new ``next_card`` after the entire
    ``cards:`` list - each one a stray block at the bottom of the diff.
    """
    if key in item:
        item[key] = value
        return
    order = KEY_ORDER[kind]
    wanted = order.index(key) if key in order else len(order)
    position = len(item)
    for index, existing in enumerate(item):
        rank = order.index(existing) if existing in order else len(order)
        if rank > wanted:
            position = index
            break
    insert = getattr(item, "insert", None)
    if callable(insert):
        insert(position, key, value)
        return
    # a plain dict (built before its first save) can only order itself by
    # rebuilding: reassigning an existing key does not move it.
    snapshot = dict(item)
    keys = list(snapshot)
    keys.insert(position, key)
    item.clear()
    for name in keys:
        item[name] = value if name == key else snapshot[name]


def set_list(item: dict, kind: str, key: str, values: list) -> None:
    """Replace the contents of ``item[key]`` without replacing the list itself.

    Assigning a fresh ``list`` onto a loaded ``CommentedSeq`` throws away the
    node the file was written with, so ``labels: [bug, feat]  # keep me`` came
    back as a four-line block sequence on the next save - diff noise for a
    one-item edit, and a broken comment anchor. Swapping the items in place
    keeps flow style and comments exactly as the author wrote them.
    """
    current = item.get(key)
    if is_list(current) and hasattr(current, "extend"):
        del current[:]
        current.extend(values)
        return
    set_field(item, kind, key, list(values))


def iso(value: Any) -> Any:
    """Render a date/datetime as its ISO string; pass anything else through.

    ``str(datetime)`` puts a space where the ``T`` should be, which the UI's
    ``Date.parse`` rejects, so ISO is spelled out rather than delegated. The
    value's own precision is kept: a hand-written ``created: ...T10:00:00.123456``
    must survive an unrelated save unchanged, or the round trip is a lie. This
    also puts the fraction on the wire, which is harmless - the UI slices
    stamps to the minute.
    """
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return value


def jsonable(value: Any) -> Any:
    """Detach a ruamel document into plain JSON types.

    Also a defensive copy: a payload on its way to one client cannot be changed
    from under it by the next op, since the live document is mutated in place.
    Non-finite floats degrade to ``null``: ``json.dumps`` would write bare
    ``NaN``, which makes the whole response unparsable and blanks the board for
    a file already on disk that an older build wrote.
    """
    if isinstance(value, (dt.datetime, dt.date)):
        return iso(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def _parse_iso(text: str) -> dt.date | None:
    """Parse exactly ``YYYY-MM-DD`` (optionally with a time) - no truncation."""
    try:
        stamp = dt.datetime.fromisoformat(text.strip())
    except (TypeError, ValueError):
        return None
    return stamp.date()


def parse_date(value: Any) -> dt.date | None:
    """Parse ``YYYY-MM-DD`` (or a datetime) into a date, or None if unparseable.

    Dates are stored in the file as native YAML dates rather than strings, which
    is what makes ``due: 2026-11-24`` stay unquoted across round trips. Nothing
    is truncated: ``2026-11-24 (EOD)`` used to parse as a real date, so a typo
    silently became a deadline instead of an error.
    """
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if not is_str(value):
        return None
    return _parse_iso(value)
