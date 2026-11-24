"""The verbs an agent actually types: ``yamlboard ready``, ``claim``, ``done``.

Two rules make this usable by something that is not a person:

**Exit 3 means someone else has it.** Not 2, which means "you typed it wrong".
A script can branch on the difference without parsing prose, and prose is
exactly what an agent under pressure retries instead of reading.

**A refusal ends in a command.** Every denial's last line is ``next: yamlboard
ready --as <your name>`` — an actionable command naming the caller's own agent
id. A terse "denied" is what makes an agent loop, retry the same claim, or reach
for a ``--force`` that does not exist on purpose.

The store is found by walking up from the current directory, so the CLI works
from anywhere inside a repository and needs no server: the files are the
arbiter, and ``serve`` is only a window onto them.

``--json`` writes one JSON document to stdout and nothing else, so a scripted
caller parses a single stream. Human output goes to stdout one fact per line,
with the ``as <agent>`` identity banner on stderr so it never pollutes the JSON.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

from . import __version__, api, gitops, identity, model, server, store
from .model import ValidationError

#: Every verb, in the order ``--help`` lists them and the order ``SKILL.md``
#: declares them in ``metadata.commands``. The anti-drift test compares all
#: three, so a verb added here without a skill update fails the suite — which is
#: the point: an agent reading a stale skill is worse than an agent reading none.
VERBS = (
    "init", "serve", "create", "check", "show", "list", "ready", "claim",
    "renew", "release", "done", "dep", "conflicts", "migrate", "skill",
)

#: Verbs that change files, so the anti-drift test can prove the skill documents
#: each one. ``serve`` is deliberately absent: agents are pointed at the files,
#: never at a server that need not be running.
MUTATING = frozenset({
    "init", "create", "claim", "renew", "release", "done", "dep", "conflicts",
    "migrate",
})

EXIT_OK = 0
EXIT_CANNOT_LISTEN = 1
EXIT_USAGE = 2
EXIT_DENIED = 3
EXIT_INTERRUPT = 130
EXIT_PIPE = 141

EPILOG = """\
exit codes:
  0    ok
  1    cannot listen (the port is taken or the bind is refused)
  2    bad argument, refused operation, or a board file that does not validate
  3    claim denied - another agent holds the card, or its paths overlap yours.
       Pick other work; there is no --force for this, and that is deliberate.
  130  interrupted
  141  stdout closed (a pipe ended)

identity:
  --as NAME, else $YAMLBOARD_AGENT, else the local part of `git config
  user.email`. The name must already be one of the board's people: yamlboard
  never invents an identity, because an assignee nobody recognises surfaces
  weeks later in a git blame.

examples:
  yamlboard init --title Web --key WEB
  export YAMLBOARD_AGENT=grigory
  yamlboard ready --paths 'src/api/**'
  yamlboard claim WEB-3 --paths 'src/api/**,tests/test_api.py'
  yamlboard renew WEB-3
  yamlboard done WEB-3
"""


class Usage(Exception):
    """A bad argument, a refused operation, or an unreadable board: exit 2.

    Optional trailing hints let a refusal name the fix beside the problem; only
    the first line carries the ``error:`` prefix, the rest read as advice.
    """

    def __init__(self, message: str, *hints: str):
        self.lines = [message, *[line for line in hints if line]]
        super().__init__(message)


class Denied(Exception):
    """Someone else holds it: exit 3, and the payload ends in a next command."""

    def __init__(self, lines: list[str], detail: dict | None = None):
        self.lines = [line for line in lines if line]
        self.detail = dict(detail or {})
        super().__init__("\n".join(self.lines))


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #

class Out:
    """One place that knows whether this run is for a person or a script."""

    def __init__(self, *, json_mode: bool, stream=sys.stdout):
        self.json = json_mode
        self.stream = stream
        self._data: dict[str, Any] = {}

    def fact(self, text: str) -> None:
        """One line for a human, dropped when ``--json`` owns the stream."""
        if not self.json:
            print(text, file=self.stream)

    def data(self, **fields: Any) -> None:
        """A field for the JSON document. Never printed to a human."""
        self._data.update(fields)

    def emit(self) -> None:
        if self.json:
            json.dump(self._data, self.stream, indent=2, default=str)
            self.stream.write("\n")

    def close(self) -> None:
        """Flush, swallowing a reader that has already gone away.

        A CLI in a pipeline (``yamlboard list | head``) gets EPIPE on the second
        write, and the default handler prints "Exception ignored in <stderr>" for
        it. When ``main`` has already handled that EPIPE and closed the stream,
        this flush lands on a file that is shut and raises ``ValueError`` instead
        -- which would escape the ``finally`` as a traceback with exit 1, the one
        report more confusing than silence. Both errors mean the same thing: the
        reader stopped. Swallow them here rather than hijack the verdict, which
        ``main`` already decided from the command itself: a ``check`` that found a
        broken board must still report that, not "the reader hung up".
        """
        with contextlib.suppress(BrokenPipeError, ValueError):
            self.stream.flush()


class suppress_broken_pipe:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc: type | None, value: BaseException | None, _) -> bool:
        return exc is BrokenPipeError or (
            isinstance(value, OSError) and getattr(value, "errno", None) == errno.EPIPE
        )




# --------------------------------------------------------------------------- #
# shared argument plumbing
# --------------------------------------------------------------------------- #

def _store(args: argparse.Namespace) -> store.Store:
    try:
        return store.Store.discover(args.dir)
    except store.StoreError as exc:
        raise Usage(str(exc)) from exc


def _who(args: argparse.Namespace, doc: dict | None) -> str | None:
    """The agent to act as, or None for a read that needs no identity.

    An unknown name is exit 2, not 3: the spec reserves 3 for "someone else holds
    it", so an agent that mistyped its identity, or exports a name the board has
    never heard of, must not look like it lost a race. Exit 3 has to mean "pick
    other work"; a bad name means fix your name and run the same command.
    """
    try:
        return identity.resolve(doc, as_flag=getattr(args, "agent", None),
                                root=_repo_of(args))
    except identity.UnknownAgent as exc:
        raise Usage(str(exc), "next: yamlboard list   # see this board's people") \
            from exc


def _repo_of(args: argparse.Namespace) -> Path | None:
    """The directory the board's repository starts at, for the git identity read."""
    target = Path(args.dir).resolve() if getattr(args, "dir", None) else Path.cwd()
    try:
        return gitops.repo_root(target)
    except gitops.GitError:
        return None


def _paths(value: str | None) -> list[str]:
    if not value:
        return []
    out: list[str] = []
    for part in value.split(","):
        if part.strip() and part.strip() not in out:
            out.append(part.strip())
    return out


def _card(out: Out, tx: api.Tx, card: dict, agent: str | None = None) -> None:
    """One card, one block: id, title, state, and why if that needs saying.

    ``agent`` is who the caller is claiming to be. Without it a card I hold
    prints as ``taken``, and the four-word vocabulary silently becomes three
    words: the flag to ask as myself already exists, so answering as though it
    had not been given is the one way this output can mislead.
    """
    claim = model.claim_of(card)
    state = api.state_of(tx, card, agent)
    ident = str(card.get("id") or "?")
    title = str(card.get("title") or "")
    board = model.board_for_card(tx.doc, ident)
    column = str(card.get("column") or "")
    if board is not None:
        named = model.find_column(board, column)
        if named is not None:
            column = str(named.get("name") or column)
    out.fact(f"{ident}  {title}")
    where = f"  [{board.get('id') if board else '?'}/{column}]" if board else ""
    out.fact(f"  {state}{where}  priority {card.get('priority') or 'none'}")
    if state in ("taken", "yours") and claim:
        holder = "you" if state == "yours" else str(claim.get("agent"))
        out.fact(f"  held by {holder} until "
                 f"{model.iso(claim.get('expires'))} "
                 f"({model.humanize(model.ttl_left(claim))} left)")
        if claim.get("paths"):
            out.fact(f"  paths {', '.join(str(p) for p in claim['paths'])}")
    blockers = model.blocked_by_open(
        model.dependencies(tx.all_cards()), ident,
        {str(c.get("id")) for c in tx.all_cards() if api._is_done(tx, c)},
    )
    if blockers:
        out.fact(f"  blocked by {', '.join(blockers)}")
    if claim and claim.get("overridden_by"):
        out.fact(f"  claim overridden by {claim['overridden_by']} — you lost that overlap")
    out.data(id=ident, title=title, state=state, column=column,
             board=str(board.get("id")) if board else None,
             priority=card.get("priority"),
             claim=model.jsonable(claim) if claim else None,
             live=bool(claim and model.claim_live(claim)),
             blocked_by=blockers)


# --------------------------------------------------------------------------- #
# verbs
# --------------------------------------------------------------------------- #

def verb_init(args: argparse.Namespace, out: Out) -> int:
    root = Path(args.dir or ".").resolve()
    board = store.Store(root)
    if board.exists():
        if args.json:
            out.data(ok=True, path=str(board.root), existed=True)
            return EXIT_OK
        out.fact(f"{board.root} already holds a board - left alone")
        return EXIT_OK
    try:
        board.create(args.title or "Board", key=args.key)
    except (store.StoreError, ValidationError) as exc:
        raise Usage(str(exc)) from exc
    out.fact(f"created {board.root}")
    out.fact("  board.yaml, cards/, archive/")
    if not args.json:
        # The two commands that make the board useful, printed at the exact
        # moment the reader wants them and nowhere else.
        out.fact("")
        out.fact("next:")
        out.fact(f"  yamlboard create \"First card\" --as {args.agent or '<you>'}")
        out.fact("  yamlboard skill install     # teach agents in this repo the protocol")
    out.data(ok=True, path=str(board.root), existed=False)
    return EXIT_OK


def verb_serve(args: argparse.Namespace, out: Out) -> int:
    app = server.Application(_store(args).root, read_only=args.read_only,
                             allow_git=not args.no_git)
    try:
        httpd = server.make_server(app, args.host, args.port,
                                   log_requests=args.verbose,
                                   allow_public=args.allow_public)
    except OSError as exc:
        # Port busy, or a bind the OS refuses. 1, never 2: nothing the caller
        # typed is wrong, the machine simply will not hand over the socket.
        raise SystemExit(f"yamlboard: cannot listen on {args.host}:{args.port}: {exc}") from exc
    shown = "localhost" if server.is_loopback(args.host) else args.host
    out.fact(f"yamlboard {__version__} on http://{shown}:{args.port}")
    out.fact(f"  board {app.root}")
    if args.read_only:
        out.fact("  read-only: writes are refused")
    elif not server.is_loopback(args.host):
        out.fact(f"  WARNING {args.host} is reachable from other machines and there is no auth")
    out.fact("  ctrl-c to stop")
    out.emit()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
    finally:
        httpd.server_close()
    return EXIT_OK


def verb_create(args: argparse.Namespace, out: Out) -> int:
    st = _store(args)
    tx = api.Tx(st)
    # No identity check: creating a card is not claiming it, and a board must be
    # fillable by someone who has not decided who they are yet. `--as` here only
    # sets the banner, so an unset `$YAMLBOARD_AGENT` must not cost you the card.
    agent = getattr(args, "agent", None)
    board_id = args.board or str((model.boards(tx.doc) or [{}])[0].get("id") or "")
    result = _run_op(st, out, agent, "card.create", {
        "board": board_id, "title": args.title, "column": args.column,
        "priority": args.priority, "desc": args.desc, "labels": args.labels or [],
        "due": args.due, "estimate": args.estimate,
    })
    out.fact(f"created {result['id']}  {args.title}")
    out.fact(f"  next: yamlboard claim {result['id']} --paths '<glob>'")
    out.data(ok=True, id=str(result["id"]), title=args.title)
    return EXIT_OK


def verb_check(args: argparse.Namespace, out: Out) -> int:
    st = _store(args)
    tx = api.Tx(st)
    problems = tx.problems()
    # Deliberately not folded into `problems`: that list gates every write in
    # `Tx.flush`, and an unreadable file beside the cards is not a reason to
    # refuse an unrelated claim. It is still exit 2, because a card nobody can
    # read is the board quietly losing work — the one outcome this store exists
    # to avoid — and a CI gate should hear about it.
    strays = st.strays()
    payload = api.summary(tx, dirty=_dirty(st))
    reports = payload["conflicts"]
    cards = sum(len(b.get("cards") or []) for b in model.boards(payload["doc"]))
    out.data(ok=not problems, path=str(st.root), problems=problems, strays=strays,
             boards=[str(b.get("id")) for b in model.boards(tx.doc)],
             cards=cards, conflicts=reports)
    out.fact(f"{st.root}")
    out.fact(f"  {len(model.boards(tx.doc))} board(s), {cards} card(s), "
             f"{len(model.people(tx.doc))} people")
    for problem in problems:
        out.fact(f"  invalid: {problem}")
    for report in reports:
        out.fact(f"  conflict: {_conflict_line(report)}")
    for name in strays:
        out.fact(f"  unreadable: cards/{name} — not named like a card (WEB-1.yaml), "
                 "so no command will ever read it")
    if args.json:
        return EXIT_USAGE if (problems or strays) else EXIT_OK
    if problems:
        out.fact("")
        out.fact("the board does not validate; nothing else will write to it until it does")
        return EXIT_USAGE
    if reports:
        out.fact("")
        out.fact(f"{len(reports)} conflict(s): yamlboard conflicts --resolve settles "
                 "an overlap by hand")
        return EXIT_USAGE        # CI should fail on a board that is lying
    if strays:
        out.fact("")
        out.fact(f"{len(strays)} file(s) in cards/ that no command can read; rename to "
                 "an id like WEB-9.yaml or move it out of the store")
        return EXIT_USAGE
    out.fact("  ok")
    return EXIT_OK


def verb_show(args: argparse.Namespace, out: Out) -> int:
    st = _store(args)
    tx = api.Tx(st)
    agent = _who(args, tx.doc) if _identity_present(args) else None
    card = tx.find_card(args.id)
    if card is None:
        raise Usage(f"no card {args.id!r} in {st.root} (try: yamlboard list)")
    _card(out, tx, card, agent)
    reasons = api.why_not_ready(tx, card, agent, _paths(args.paths))
    if reasons:
        # `show` is a read, but "you cannot take this" is exactly what exit 3
        # means, so an agent can use it as a cheap pre-flight. The lines belong
        # to the Denied handler; printing them here would say it twice.
        raise Denied(reasons + [_next(agent)], {"reason": "ready_check", "card": args.id})
    out.fact(f"  next: yamlboard claim {args.id} --paths '<glob>' --as {agent}"
             if agent else f"  next: yamlboard claim {args.id} --paths '<glob>'")
    out.data(ok=True, id=args.id, state=api.state_of(tx, card, agent), claimable=True)
    return EXIT_OK


def verb_list(args: argparse.Namespace, out: Out) -> int:
    st = _store(args)
    tx = api.Tx(st)
    agent = _who(args, tx.doc) if _identity_present(args) else None
    payload = api.summary(tx, agent=agent)
    boards = model.boards(payload["doc"])
    if args.board:
        wanted = [b for b in boards if str(b.get("id")) == args.board]
        if not wanted:
            raise Usage(f"no board {args.board!r} (have: "
                        f"{', '.join(str(b.get('id')) for b in boards)})")
        boards = wanted
    rows: list[dict] = []
    for board in boards:
        if not args.all:
            archived = set(payload["archived"])
            rows.extend(c for c in board.get("cards") or [] if str(c.get("id")) not in archived)
        else:
            rows.extend(board.get("cards") or [])
    for card in rows:
        card_id = str(card.get("id") or "?")
        claim = card.get("claim") if isinstance(card.get("claim"), dict) else None
        held = f" @{claim.get('agent')}" if card.get("live") and claim else ""
        flag = {"taken": "·", "blocked": "✻", "ready": "○", "yours": "✓"}.get(
            str(card.get("state")), " ")
        out.fact(f"{flag} {card_id:<9} {str(card.get('column') or ''):<12} "
                 f"{str(card.get('priority') or '-'):<6}{held:<12} {card.get('title') or ''}")
    out.data(ok=True, cards=rows, ready=payload["ready"],
             agents=payload["agents"], conflicts=payload["conflicts"])
    return EXIT_OK


def verb_ready(args: argparse.Namespace, out: Out) -> int:
    st = _store(args)
    tx = api.Tx(st)
    agent = _who(args, tx.doc) if _identity_present(args) else None
    paths = _paths(args.paths)
    cards = api.ready(tx, agent, paths)
    for card in cards:
        _card(out, tx, card)
    out.data(ok=True, ready=[str(c.get("id")) for c in cards],
             cards=model.jsonable(cards), paths=paths)
    if not cards:
        out.fact("nothing is ready.")
        if paths:
            out.fact("  every unclaimed card is either blocked or touches a path "
                     "someone already holds - try without --paths to see why")
        out.fact("  next: yamlboard conflicts")
        out.fact(f"  next: yamlboard list")
    return EXIT_OK


def verb_claim(args: argparse.Namespace, out: Out) -> int:
    st = _store(args)
    doc = api.Tx(st).doc
    agent = _who(args, doc)
    result = _run_op(st, out, agent, "claim.create", {
        "id": args.id, "agent": agent, "paths": _paths(args.paths),
        "ttl": args.ttl, "note": args.note,
    })
    if result.get("unchanged"):
        out.fact(f"{args.id} is already yours until {result.get('expires')} - nothing written")
    else:
        out.fact(f"claimed {args.id} as {agent} until {result.get('expires')}")
        if result.get("paths"):
            out.fact(f"  paths {', '.join(result['paths'])}")
        if result.get("preempted"):
            out.fact(f"  took it from {result['preempted']}, whose lease had expired")
    out.fact(f"  next: renew before anything long - yamlboard renew {args.id}")
    out.data(ok=True, **result)
    return EXIT_OK


def verb_renew(args: argparse.Namespace, out: Out) -> int:
    st = _store(args)
    agent = _who(args, api.Tx(st).doc)
    result = _run_op(st, out, agent, "claim.renew", {"id": args.id, "agent": agent,
                                                     "ttl": args.ttl})
    if result.get("retaken"):
        out.fact(f"{args.id} had expired and is yours again until {result['expires']} "
                 f"(preempted: {agent})")
    else:
        out.fact(f"{args.id} extended to {result['expires']}")
    out.data(ok=True, **result)
    return EXIT_OK


def verb_release(args: argparse.Namespace, out: Out) -> int:
    st = _store(args)
    agent = _who(args, api.Tx(st).doc)
    result = _run_op(st, out, agent, "claim.release", {"id": args.id, "agent": agent,
                                                       "force": args.force})
    if result.get("released"):
        out.fact(f"released {args.id}"
                 + (f" (was {result['was']})" if result.get("was") else ""))
    else:
        out.fact(f"{args.id} {result.get('reason', 'was not claimed')}")
    out.data(ok=True, **result)
    return EXIT_OK


def verb_done(args: argparse.Namespace, out: Out) -> int:
    st = _store(args)
    # Finishing is usually the holder closing their own card, but the human in the
    # browser moves cards too, and an agent that has already released its lease
    # must still be able to file the work. So identity is read if present, never
    # demanded.
    agent = _who(args, api.Tx(st).doc) if _identity_present(args) else None
    result = _run_op(st, out, agent, "card.done", {"id": args.id, "column": args.column})
    out.fact(f"{args.id} is in {result['column']}")
    out.fact("  lease released" + (f" by {agent}" if agent else ""))
    out.data(ok=True, **result)
    return EXIT_OK


def verb_dep(args: argparse.Namespace, out: Out) -> int:
    st = _store(args)
    # Wiring up dependencies is board shape, not a claim on anybody's work, so it
    # needs no identity: refusing `dep add` without `--as` would stop a person
    # organising a queue they have no intention of working in.
    agent = getattr(args, "agent", None)
    op = {"add": "card.dep.add", "rm": "card.dep.remove",
          "remove": "card.dep.remove"}[args.action]
    result = _run_op(st, out, agent, op, {"id": args.id, "blocked_by": args.blocked_by})
    edge = "now waits on" if args.action == "add" else "no longer waits on"
    out.fact(f"{args.id} {edge} {args.blocked_by}")
    if args.action == "add":
        out.fact(f"  it will not appear in `ready` until {args.blocked_by} is in a done column")
    out.data(ok=True, **result)
    return EXIT_OK


def verb_conflicts(args: argparse.Namespace, out: Out) -> int:
    st = _store(args)
    tx = api.Tx(st)
    reports = api.conflicts(tx, _dirty(st))
    if args.resolve:
        if not reports:
            raise Usage("no conflicts to resolve")
        agent = _who(args, tx.doc)
        result = _run_op(st, out, agent, "conflicts.resolve", {})
        for item in result["resolved"]:
            out.fact(f"{item['card']}: {item['agent']} overridden by {item['overridden_by']}")
        out.fact(f"{result['count']} claim(s) released; those cards are ready again")
        out.data(ok=True, resolved=result["resolved"], count=result["count"])
        return EXIT_OK
    for report in reports:
        out.fact(_conflict_line(report))
        for detail in _conflict_detail(report):
            out.fact(f"  {detail}")
    out.data(ok=not reports, conflicts=reports)
    if not reports:
        out.fact("no conflicts - no two live claims share a path, and no ids collide.")
        return EXIT_OK
    out.fact("")
    out.fact(f"next: yamlboard conflicts --resolve   # the earliest claim wins, "
             "the other is marked overridden")
    return EXIT_USAGE     # reported, not acted on: a CI gate should see this


def verb_migrate(args: argparse.Namespace, out: Out) -> int:
    source = Path(args.file).expanduser().resolve()
    if not source.is_file():
        raise Usage(f"{source} is not a file")
    target = store.Store(args.dir or source.parent)
    if target.exists() and not args.force:
        raise Usage(f"{target.root} already holds a board - pass --force to add to it")
    try:
        raw = store._read_file(source, None)
    except store.StoreError as exc:
        raise Usage(str(exc)) from exc
    plan = _translate(raw.doc, source, target)
    if args.dry_run:
        out.fact(f"would write {len(plan['cards'])} card(s) to {target.root}")
        for line in plan["notes"]:
            out.fact(f"  {line}")
        out.data(ok=True, dry_run=True, cards=len(plan["cards"]), notes=plan["notes"])
        return EXIT_OK
    fresh = not target.exists()
    if fresh:
        target.create(args.title or str(raw.doc.get("title") or "Board"))
    tx = api.Tx(target)
    wanted = plan["board"]
    board = (model.boards(tx.doc)[0] if fresh
             else model.find_board(tx.doc, wanted["id"])
             or next((b for b in model.boards(tx.doc)
                      if str(b.get("key")) == wanted["key"]), None))
    if board is None:
        raise Usage(
            f"{target.root} has no board with the key {wanted['key']!r} to hold "
            f"{wanted['key']}-N cards; its boards are "
            f"{', '.join(str(b.get('key')) for b in model.boards(tx.doc)) or '(none)'}. "
            "Migrate into an empty directory instead: renaming a live board would "
            "orphan every card already named for the old key"
        )
    if fresh:
        # The key is the id prefix and the id is the filename: a board keyed LEG
        # cannot own WEB-1, and `write_card` refuses it for exactly that reason.
        # Renaming is only ever safe on a store we just made.
        board["id"] = wanted["id"]
        board["key"] = wanted["key"]
        board["title"] = wanted["title"]
    for key in ("people", "labels"):
        section = api._section(tx, key)
        have = {str(item.get("id")) for item in section if model.is_map(item)}
        for item in plan[key]:
            # onto a fresh store this copies every person across; onto an existing
            # one a duplicate id would fail validation and lose the whole migration
            if str(item.get("id")) not in have:
                section.append(item)
                have.add(str(item.get("id")))
    board["custom_fields"] = plan["custom_fields"]
    board["columns"] = plan["columns"]
    highest = max([int(str(c["id"]).rsplit("-", 1)[1]) for c in plan["cards"]],
                  default=0)
    wanted_next = wanted.get("next_card")
    board["next_card"] = max(wanted_next if model.is_int(wanted_next) and wanted_next > 0 else 1,
                             highest + 1)
    tx.touch()
    for card in plan["cards"]:
        model.set_field(card, "card", "order",
                        float(model.ORDER_GAP * (1 + len(tx.card_ids()))))
        tx.create(card)
    tx.flush()
    out.fact(f"migrated {len(plan['cards'])} card(s) from {source}")
    out.fact(f"  into {target.root}; {source} was left exactly as it was")
    for line in plan["notes"]:
        out.fact(f"  {line}")
    out.fact("")
    out.fact("the old file is not deleted and is not read again: v2 does not read v1.")
    out.data(ok=True, cards=len(plan["cards"]), notes=plan["notes"], path=str(target.root))
    return EXIT_OK


def verb_skill(args: argparse.Namespace, out: Out) -> int:
    bundled = skill_path()
    if bundled is None:
        raise Usage("the bundled SKILL.md is missing from the installed package - "
                    "this install is broken, reinstall with `uv tool install --force`")
    if args.action == "path":
        out.fact(str(bundled))
        out.data(ok=True, path=str(bundled))
        return EXIT_OK

    here = Path(args.dir or ".").resolve()
    repo = gitops.repo_root(here / "dummy") or here
    dest = repo / ".agents" / "skills" / "yamlboard" / "SKILL.md"
    payload = bundled.read_bytes()
    out.data(ok=True, source=str(bundled), target=str(dest))
    if dest.exists() and dest.read_bytes() == payload:
        out.fact(f"{dest} is already current")
        return EXIT_OK
    if dest.exists() and not args.force:
        raise Usage(
            f"{dest} exists and differs from the bundled copy ({bundled}) - "
            "pass --force to overwrite it, or diff them first: someone may have "
            "edited it on purpose"
        )
    if not args.json:
        out.fact(f"writing {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(payload)
    out.data(written=True)
    out.fact("")
    out.fact("paste this into AGENTS.md so runtimes that read that file find the skill:")
    out.fact("")
    for line in AGENTS_BLOCK.splitlines():
        out.fact(f"  {line}")
    out.fact("")
    out.fact(f"(also saved to {dest.parent / 'AGENTS-block.md'})")
    (dest.parent / "AGENTS-block.md").write_text(AGENTS_BLOCK, encoding="utf-8")
    return EXIT_OK


AGENTS_BLOCK = """\
## Coordination

This repo uses `yamlboard`. Before editing files, check `.yamlboard/`: claim a
card with `yamlboard claim <ID> --paths '<globs you will touch>'` and release it
when done. Exit code 3 means another agent holds it - take different work, never
retry. Full protocol: `.agents/skills/yamlboard/SKILL.md`.
"""


def skill_path() -> Path | None:
    """The ``SKILL.md`` shipped inside the installed package, or None.

    Resolved through the package rather than a repo-relative path, because after
    ``uv tool install`` there is no checkout: a path built from ``cwd`` would
    point at whatever repository the caller happened to be standing in.
    """
    candidate = Path(__file__).resolve().parent / "skills" / "yamlboard" / "SKILL.md"
    return candidate if candidate.is_file() else None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _identity_present(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "agent", None) or os.environ.get(identity.ENV_VAR))


def _next(agent: str | None) -> str:
    return f"next: yamlboard ready --as {agent}" if agent else "next: yamlboard ready"


def _dirty(st: store.Store) -> list[str]:
    """Repo-wide working-tree paths, for the checks that compare a claim to them.

    Board-scoped would be useless here: ``conflicts`` is asked whether a claim's
    globs cover files *outside* the board that somebody else has already edited.
    """
    try:
        return gitops.changed_paths(st.root)
    except gitops.GitError:
        return []


def _run_op(st: store.Store, out: Out, agent: str | None, op: str,
            args: dict) -> dict:
    """One op, with the CLI's exit-code translation and the identity banner.

    The banner is written before the write, not after: an agent that discovers
    afterwards which identity it used has already mislabelled the card.
    """
    if agent and not out.json:
        print(f"  {identity.banner(agent)}", file=sys.stderr)
    payload = {"op": op, "args": {k: v for k, v in args.items() if v is not None}}
    try:
        result = api.apply(st, payload)["result"]
    except api.ClaimDenied as exc:
        raise Denied(list(exc.lines), exc.detail) from exc
    # `BadRequest` is the layer's "refused operation" — a cycle, a directory
    # passed to --paths, an edge that already exists. It is exit 2, and mapping it
    # here rather than letting it escape keeps a traceback out of an agent's
    # terminal: exit 1 has to stay reserved for "the port is taken", so a crash
    # masquerading as a listen failure would send an agent hunting the wrong thing.
    except api.BadRequest as exc:
        raise Usage(str(exc)) from exc
    except api.NotFound as exc:
        raise Usage(f"no such thing: {exc}") from exc
    except api.Conflict as exc:
        raise Usage(
            f"{', '.join(exc.paths)} changed on disk while this was running - the files "
            "are the arbiter, so re-read and repeat"
        ) from exc
    except ValidationError as exc:
        raise Usage(store.describe(exc.errors)) from exc
    except store.StoreError as exc:
        raise Usage(str(exc)) from exc
    return result

def _conflict_line(report: dict) -> str:
    kind = report.get("kind")
    if kind == "overlap":
        win, lose = report["winner"], report["loser"]
        return (f"overlap: {win['card']} ({win['agent']}) and "
                f"{lose['card']} ({lose['agent']}) claim the same files")
    if kind == "duplicate":
        return f"duplicate id: {report['detail']}"
    return str(report.get("detail") or kind)


def _conflict_detail(report: dict) -> list[str]:
    kind = report.get("kind")
    if kind == "overlap":
        win, lose = report["winner"], report["loser"]
        return [
            # `patterns` pairs are already whole glob strings; joining one with
            # "/" spells it out a character at a time (s/r/c///a/p/i///*/*)
            f"{pair['a']} ∩ {pair['b']}"
            for pair in report.get("patterns") or []
        ] + [
            f"winner {win['agent']} on {win['card']} (claimed {win['at']})",
            f"loser  {lose['agent']} on {lose['card']} (claimed {lose['at']})",
            "earliest `at` wins; a tie goes to the alphabetically first name",
        ]
    return [str(report.get("detail"))] if report.get("detail") else []


# --------------------------------------------------------------------------- #
# v1 -> v2
# --------------------------------------------------------------------------- #

def _translate(doc: dict, source: Path, target: store.Store) -> dict:
    """A v1 single-file document as v2 files.

    v2 never reads v1, deliberately: a reader that accepts two shapes is how a
    board ends up silently ignored - parsed by the wrong branch, looking empty,
    while somebody's edits sit in the other format. So this runs once, says what
    it dropped, and leaves the original file alone.
    """
    notes: list[str] = []
    version = doc.get("version", 1)
    if model.is_int(version) and version >= model.VERSION:
        raise Usage(f"{source} says version {version}; this reads version 1 boards. "
                    "If it is already a v2 store, point at its parent directory instead")

    boards = [b for b in (doc.get("boards") or []) if model.is_map(b)]
    if len(boards) != 1:
        raise Usage(
            f"{source} has {len(boards)} boards; migrate writes one store per board, "
            "so run it once per board (or hand-move the files if you want them together)"
        )
    board = boards[0]

    cards: list[dict] = []
    for index, raw in enumerate(board.get("cards") or []):
        if not model.is_map(raw):
            notes.append(f"dropped card #{index}: not a mapping")
            continue
        card = {key: value for key, value in raw.items()
                if key in model.CARD_KEYS}
        card.pop("cards", None)
        if not card.get("id"):
            notes.append(f"dropped card #{index}: no id")
            continue
        if not model.CARD_RE.match(str(card["id"])):
            notes.append(f"dropped {card['id']}: not a KEY-n id, and the id is the filename")
            continue
        if "order" not in card:
            card["order"] = float(model.ORDER_GAP * (index + 1))
        card["archived"] = bool(card.get("archived"))
        cards.append(card)

    archived_lanes = {str(c.get("id")) for c in model.columns(board)
                      if c.get("is_done")}
    return {
        "cards": cards,
        # carried, not merely validated: the caller must build a board whose key
        # matches the ids of the cards it is about to write
        "board": {
            "id": str(board.get("id") or model.slugify(str(board.get("title") or "board"))),
            "key": str(board.get("key") or model.board_key(str(board.get("title") or "Board"))),
            "title": str(board.get("title") or "Board"),
            "next_card": board.get("next_card"),
        },
        "people": [dict(p) for p in (doc.get("people") or []) if model.is_map(p)],
        "labels": [dict(lbl) for lbl in (doc.get("labels") or []) if model.is_map(lbl)],
        "columns": [model.jsonable(c) for c in model.columns(board)],
        "custom_fields": [model.jsonable(f) for f in model.custom_fields(board)],
        "notes": notes + ([
            f"{len(archived_lanes)} done column(s); v2 keeps done cards in the "
            "board and uses archive/ only for retired ones"
        ] if archived_lanes else []),
    }


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yamlboard",
        description="A board in your repo that tells concurrent agents who holds what.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"yamlboard {__version__}")
    sub = parser.add_subparsers(dest="verb", metavar="<verb>", required=True)

    def add(name: str, help_text: str, **kwargs: Any):
        return sub.add_parser(name, help=help_text, description=help_text, **kwargs)

    def store_dir(command, default_doc: str):
        command.add_argument("--dir", metavar="PATH",
                             help=default_doc)
        return command

    as_help = "agent to act as (default: $YAMLBOARD_AGENT, then git user.email)"

    init = store_dir(add("init", "create .yamlboard/ in this repository"),
                     "where to create the board (default: here)")
    init.add_argument("--title", help="board title (default: Board)")
    init.add_argument("--key", help="card id prefix, 2-8 characters like WEB")
    init.add_argument("--as", dest="agent", help=as_help)
    init.add_argument("--json", action="store_true", help="one JSON document on stdout")
    init.add_argument("--force", action="store_true", help="accepted and ignored; init never overwrites")

    serve = store_dir(add("serve", "show the board in a browser"),
                      "directory containing .yamlboard/ (default: search upwards)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=7474)
    serve.add_argument("--read-only", action="store_true", help="refuse every write")
    serve.add_argument("--allow-public", action="store_true",
                       help="let a non-loopback bind serve a writable board (no auth)")
    serve.add_argument("--no-git", action="store_true", help="skip git status and the Commit button")
    serve.add_argument("-v", "--verbose", action="store_true", help="log each request")
    serve.add_argument("--json", action="store_true", help="print the startup line as JSON")

    create = store_dir(add("create", "add a card"), "directory containing .yamlboard/")
    create.add_argument("title")
    create.add_argument("--board", help="board id (default: the first board)")
    create.add_argument("--column", help="column id (default: the board's first column)")
    create.add_argument("--priority", choices=model.PRIORITIES)
    create.add_argument("--desc")
    create.add_argument("--label", dest="labels", action="append", help="repeatable")
    create.add_argument("--due", help="YYYY-MM-DD")
    create.add_argument("--estimate", type=int)
    create.add_argument("--as", dest="agent", help=as_help)
    create.add_argument("--json", action="store_true")

    check = store_dir(add("check", "validate the board and report conflicts"),
                      "directory containing .yamlboard/")
    check.add_argument("--json", action="store_true")

    show = store_dir(add("show", "can I take this card, and if not why"),
                     "directory containing .yamlboard/")
    show.add_argument("id")
    # `dest="agent"` everywhere: `as` is a keyword, and one attribute name for
    # the same concept across fifteen verbs is what lets `_who` read it back.
    show.add_argument("--as", dest="agent", help=as_help)
    show.add_argument("--paths", help="globs you intend to touch, to test them first")
    show.add_argument("--json", action="store_true")

    listing = store_dir(add("list", "every card, newest state first"),
                        "directory containing .yamlboard/")
    listing.add_argument("--board", help="one board only")
    listing.add_argument("--all", action="store_true", help="include archived cards")
    listing.add_argument("--as", dest="agent", help=as_help)
    listing.add_argument("--json", action="store_true")

    ready = store_dir(add("ready", "cards you can start right now"),
                      "directory containing .yamlboard/")
    ready.add_argument("--paths", help="only cards whose claim would not collide with these")
    ready.add_argument("--as", dest="agent", help=as_help)
    ready.add_argument("--json", action="store_true")

    claim = store_dir(add("claim", "take a card for a bounded time"),
                      "directory containing .yamlboard/")
    claim.add_argument("id")
    claim.add_argument("--paths", help="files or globs you will edit: 'src/api/**,tests/x.py'")
    claim.add_argument("--ttl", help="lease length like 30m or 2h (default: the board's)")
    claim.add_argument("--note", help="one line on what you are doing")
    claim.add_argument("--as", dest="agent", help=as_help)
    claim.add_argument("--json", action="store_true")

    renew = store_dir(add("renew", "extend your lease"), "directory containing .yamlboard/")
    renew.add_argument("id")
    renew.add_argument("--ttl", help="new length from now, like 30m")
    renew.add_argument("--as", dest="agent", help=as_help)
    renew.add_argument("--json", action="store_true")

    release = store_dir(add("release", "put a card back"), "directory containing .yamlboard/")
    release.add_argument("id")
    release.add_argument("--force", action="store_true",
                         help="release a live claim held by someone else")
    release.add_argument("--as", dest="agent", help=as_help)
    release.add_argument("--json", action="store_true")

    done = store_dir(add("done", "finish a card and free its lease"),
                     "directory containing .yamlboard/")
    done.add_argument("id")
    done.add_argument("--column", help="a done column other than the board's default")
    done.add_argument("--as", dest="agent", help=as_help)
    done.add_argument("--json", action="store_true")

    dep = store_dir(add("dep", "add or remove a blocked_by edge"),
                    "directory containing .yamlboard/")
    dep.add_argument("action", choices=("add", "rm", "remove"))
    dep.add_argument("id", help="the card that will wait")
    # The blocker is a flag, not a second positional: `dep add WEB-1 WEB-2` does
    # not say which way the edge points, and a wrong guess silently blocks the
    # wrong agent's work.
    dep.add_argument("--blocked-by", metavar="ID", required=True,
                     help="the card that must finish first, like WEB-2")
    dep.add_argument("--as", dest="agent", help=as_help)
    dep.add_argument("--json", action="store_true")

    conflicts = store_dir(add("conflicts", "report overlaps git could not see"),
                          "directory containing .yamlboard/")
    conflicts.add_argument("--resolve", action="store_true",
                           help="mark every loser overridden, freeing its card")
    conflicts.add_argument("--as", dest="agent", help=as_help)
    conflicts.add_argument("--json", action="store_true")

    migrate = add("migrate", "import a v1 single-file board into a v2 store")
    migrate.add_argument("file", help="the old boards.yaml")
    migrate.add_argument("--dir", help="where to write .yamlboard/ (default: beside the file)")
    migrate.add_argument("--title", help="board title for the new store")
    migrate.add_argument("--dry-run", action="store_true", help="report, write nothing")
    migrate.add_argument("--force", action="store_true", help="add to an existing store")
    migrate.add_argument("--json", action="store_true")

    skill = add("skill", "install or locate the agent skill file")
    skill.add_argument("action", choices=("install", "path"))
    skill.add_argument("--dir", help="repo to install into (default: here)")
    skill.add_argument("--force", action="store_true", help="overwrite a local edit")
    skill.add_argument("--json", action="store_true")

    return parser


VERB_FUNCTIONS: dict[str, Callable[[argparse.Namespace, Out], int]] = {
    "init": verb_init,
    "serve": verb_serve,
    "create": verb_create,
    "check": verb_check,
    "show": verb_show,
    "list": verb_list,
    "ready": verb_ready,
    "claim": verb_claim,
    "renew": verb_renew,
    "release": verb_release,
    "done": verb_done,
    "dep": verb_dep,
    "conflicts": verb_conflicts,
    "migrate": verb_migrate,
    "skill": verb_skill,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    out = Out(json_mode=bool(getattr(args, "json", False)))
    code = EXIT_OK
    try:
        if args.verb not in VERB_FUNCTIONS:            # pragma: no cover - argparse stops it
            raise Usage(f"unknown verb {args.verb!r}")
        code = VERB_FUNCTIONS[args.verb](args, out)
        out.emit()
    except Denied as exc:
        # Denials go to stderr like every other failure (one place to read
        # errors from), except under --json, where stdout carries one parseable
        # document and mixing prose into it would break the caller's parser.
        out.data(ok=False, error=exc.lines[0] if exc.lines else "denied",
                 lines=exc.lines, **exc.detail)
        if out.json:
            out.emit()
        else:
            for line in exc.lines:
                print(line, file=sys.stderr)
        code = EXIT_DENIED
    except Usage as exc:
        # `lines`, not `str`: an identity refusal is one sentence plus the single
        # command that fixes it, and a caller reading only stderr should see both.
        lines = getattr(exc, "lines", [str(exc)])
        out.data(ok=False, error=lines[0], lines=lines)
        if out.json:
            out.emit()
        else:
            print(f"error: {lines[0]}", file=sys.stderr)
            for line in lines[1:]:
                print(line, file=sys.stderr)
        code = EXIT_USAGE
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        code = EXIT_INTERRUPT
    except BrokenPipeError:
        with suppress_broken_pipe():
            sys.stdout.close()
        code = EXIT_PIPE
    except SystemExit:
        out.emit()
        raise
    finally:
        # ``close`` never raises. The verdict was already decided by the command
        # (or here, on an EPIPE mid-write), and a late flush landing on a reader
        # that stopped must not rewrite it -- a CI job running ``check`` has to
        # hear the board is broken, not "the reader hung up".
        out.close()
    return code


if __name__ == "__main__":   # pragma: no cover
    sys.exit(main())
