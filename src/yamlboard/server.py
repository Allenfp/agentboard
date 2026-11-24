"""Static files plus a tiny JSON API, on :mod:`http.server`.

One process, no framework. The browser is a pure renderer: it posts an op with
the per-file ``tokens`` it was last given, the server answers with the whole
document, and the client paints that. Nothing is decided twice, which is the
only reason two tabs and four agents can look at one board without arguing.

Two kinds of refusal both wear 409, and telling them apart is the whole
concurrency story:

* **stale token** - the bytes moved under you (another tab, a ``git pull``, a
  hand edit in Vim). The answer is to reload, so the reply carries the fresh
  document and the UI adopts it.
* **claim denied** - the board did not change under you at all; somebody else
  holds that card. Reloading here would throw away the modal a person is typing
  in to tell them something they already knew, so this reply deliberately
  carries *no* document and ``claim_denied: true``.

Every poll re-reads the store from disk. That is the deliberate weakness: a
board with 200 cards costs 200 small YAML parses every three seconds, per open
tab, where a cache would cost one. The cache is what would lie - a lease expires
on the clock, not on a write, so a cached document would keep calling a free
card "taken" until something else happened to dirty it.

The Host/Origin guard, the loopback-only bind, and the CSRF refusals are
unchanged from the single-file era on purpose: ``is_loopback`` is the entire
security story here, so loosening it by accident while rewriting the data layer
would be the worst possible outcome of a rewrite.
"""

from __future__ import annotations

import contextlib
import errno
import ipaddress
import json
import sys
import threading
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import unquote, urlparse

from . import __version__, api, gitops, model, store
from .model import ValidationError

MAX_BODY = 4 * 1024 * 1024
STATIC_DIR = Path(__file__).resolve().parent / "static"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}
INTERNAL_ERROR = "the server could not complete that; see the terminal yamlboard runs in"
CSP_HTML = "frame-ancestors 'none'"


def is_loopback(host: str) -> bool:
    """True when only this machine can reach ``host``.

    The single definition of the security boundary, because loopback *is* the
    auth here: the bind check, the banner warning, the URL we print and the
    Host/Origin guard below must all agree, or a host one of them calls local
    gets served without the other's CSRF protection. Lives here rather than in
    ``cli`` because ``cli`` imports this module, not the reverse.

    ``ipaddress`` only speaks numbers, so ``localhost`` is named; an unparseable
    hostname is refused as if public, which is the safe direction.
    """
    if host == "localhost":
        return True
    if not host or host == "*":
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def local_hosts(host: str) -> frozenset[str] | None:
    """The Host names acceptable on a bind to ``host``, or None if unguarded.

    On a loopback bind every loopback spelling names this machine and nothing
    else, so all of them are safe to accept - plus the address actually bound,
    which is how ``127.0.0.2`` used to end up with no guard at all: the old
    allowlist was a three-literal tuple, an alias bind matched none of it, and
    the request fell through as if the user had asked for a public port.
    ``0.0.0.0`` and ``::`` answer on every interface, where guarding by name
    would reject the very hosts the user exposed the port for; those get None
    and rely on the explicit ``--allow-public`` decision instead.
    """
    if not is_loopback(host):
        return None
    return frozenset({host, "localhost", "127.0.0.1", "::1"})


class _Answered(Exception):
    """The client already has its answer: stop routing, do not answer twice."""


class Application:
    """The one owner of the store, and of the one-op-one-snapshot rule.

    ``store_root`` is the ``.yamlboard`` directory (or its parent - ``Store``
    accepts either), which is also the git pathspec the Commit button stages.
    One absolute value owns both, because the two disagreeing is how a board
    ended up reported as committed while the file the UI named was elsewhere.
    """

    def __init__(
        self,
        store_root: Path | str,
        *,
        read_only: bool = False,
        allow_git: bool = True,
    ) -> None:
        self.store = store.Store(store_root)
        self.root = self.store.root
        self.read_only = read_only
        self.allow_git = allow_git
        # Serialises writes against commits: both touch the git index, and a
        # commit reading an index another thread is mid-``add`` on produces a
        # 128 that the operator can neither reproduce nor explain.
        self._lock = threading.Lock()

    # -- reading ---------------------------------------------------------- #

    def tree(self) -> api.Tx:
        """A fresh read of the whole store. Never cached; see the module banner."""
        return api.Tx(self.store)

    def dirty_paths(self) -> list[str]:
        """Working-tree changes git can see, for ``conflicts``' third check.

        A claim on ``src/api/**`` while ``src/api/routes.py`` is already modified
        is a collision the board cannot see in its own files, and a merge will
        not catch either because the two agents edited *different* files that the
        glob says are one person's. Reported, never blocked: a dirty tree is
        normal, and refusing to serve over it would make the board useless.

        Repo-wide, not the board directory: a pathspec of ``.yamlboard`` can only
        name board files, so no source path would match and the check would look
        satisfied while being unreachable. ``payload``'s ``git`` stays
        board-scoped, because the toolbar counts what Commit will stage.
        """
        if not self.allow_git:
            return []
        try:
            return gitops.changed_paths(self.root)
        except gitops.GitError:      # a broken repo must still serve a board
            return []

    def payload(self, *, tx: api.Tx | None = None) -> dict:
        """Everything the UI needs: document, leases, conflicts, git status."""
        tx = tx or self.tree()
        payload = api.summary(tx, dirty=self.dirty_paths())
        payload["file"] = str(self.root)
        payload["read_only"] = self.read_only
        payload["git"] = gitops.status(self.root).as_dict() if self.allow_git else None
        return payload

    # -- writing ---------------------------------------------------------- #

    def apply(self, body: dict) -> dict:
        """Run one op against one snapshot; every file it touches is checked.

        ``tokens`` is required, not optional. The CLI can write without them
        because an agent reading the files *is* looking at them; a browser is not
        - it is looking at a render from up to three seconds ago, and a nanosecond
        mtime is the only thing that says whether the card it is editing is still
        the card it displayed. A caller with no tokens has no business writing
        over one that does.
        """
        if self.read_only:
            raise api.BadRequest("this server was started with --read-only")
        seen = body.get("tokens")
        if seen is None:
            raise api.BadRequest("tokens are required; reload the page")
        if not model.is_map(seen):
            raise api.BadRequest("tokens must be the mapping the server sent")
        bad = [key for key, value in seen.items() if not model.is_str(value)]
        if bad:
            raise api.BadRequest(f"token for {bad[0]!r} must be a string")

        payload = {"op": body.get("op"), "args": body.get("args") or {}}
        with self._lock:
            tx = self.tree()
            tx.tokens.update({str(k): v for k, v in seen.items()})
            result = api.apply(self.store, payload, tx=tx)
        return {"ok": True, "result": result["result"], "state": self.payload()}

    def commit(self, body: dict) -> dict:
        if self.read_only:
            raise api.BadRequest("this server was started with --read-only")
        if not self.allow_git:
            raise api.BadRequest("git commits are disabled on this server")
        message = str(body.get("message") or "").strip()
        if not message:
            # outside the lock: payload() reads the store, and the lock below is
            # not re-entrant for a second read of a changed tree
            doc = self.tree().doc
            message = gitops.default_message(self.root, str(doc.get("title") or ""))
        # Two people clicking Commit at the same moment used to quarrel inside
        # git's own index, and the loser came back as a 400 quoting raw
        # ``index.lock`` stderr. The lock serialises the commit against writes
        # and stops the trailing status reading an index mid-commit.
        with self._lock:
            result = gitops.commit(self.root, message)
            return {"ok": True, **result, "git": gitops.status(self.root).as_dict()}

    def git_status(self) -> dict:
        return {"ok": True, "git": gitops.status(self.root).as_dict()}


class Handler(BaseHTTPRequestHandler):
    server_version = "yamlboard"
    protocol_version = "HTTP/1.1"
    # A client that declares a body and stops writing would otherwise hold its
    # thread open forever; ``_route`` turns the resulting TimeoutError into a 408.
    timeout = 15
    app: Application  # set on the subclass built by ``make_server``
    allowed_hosts: frozenset[str] | None = None  # None: bind is not loopback-only

    # -- plumbing --------------------------------------------------------- #

    def version_string(self) -> str:
        """``Server: yamlboard`` alone - the stdlib default appends the Python
        version, which is a free runtime inventory for anyone probing the port."""
        return self.server_version

    def log_message(self, fmt: str, *args: object) -> None:  # quieter default
        if self.server.log_requests:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict, extra: dict | None = None) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", extra)

    def _error(self, status: int, message: str, *, close: bool = False,
               headers: dict | None = None, **extra: Any) -> None:
        """One JSON error answer, and the only place that ends a connection.

        ``close`` is for the answers that leave request bytes unread: the header
        says hang up and the flag stops the keep-alive loop, so nothing
        half-read can come back as the next request.
        """
        sent = dict(headers or {})
        if close:
            self.close_connection = True
            sent["Connection"] = "close"
        self._json(status, {"ok": False, "error": message, **extra}, sent)

    def _fail(self, detail: object) -> None:
        """500 with the real detail kept on the operator's terminal.

        The body tells the reader to look there, so stderr is written directly
        instead of through :meth:`log_message`, which does two things to a
        diagnosis: it honours the quiet default (an error nobody reads is an
        error nobody hears about), and since 3.14 it escapes control characters,
        flattening a whole traceback onto one access-log line with literal
        ``\\x0a`` where the newlines were.
        """
        print(f"yamlboard: {str(detail).rstrip()}", file=sys.stderr, flush=True)
        self._error(HTTPStatus.INTERNAL_SERVER_ERROR, INTERNAL_ERROR)

    def _refuse(self, status: int, message: str, **extra: Any) -> NoReturn:
        """Answer, hang up, and stop routing.

        For any refusal that leaves request bytes unread: keep-alive feeds those
        bytes to the parser as the *next* request, so a rejected
        ``Content-Length: 9999999`` used to be followed by a 200 for a
        ``GET /api/health`` the client had smuggled in behind it.
        """
        self._error(status, message, close=True, **extra)
        raise _Answered

    def _method_not_allowed(self, allowed: str) -> None:
        """405 as JSON like every other answer, plus the verbs that do work.

        The stdlib's 501 text/html made app.js report "unreadable response".
        Nothing here reads a body, so the connection has to end: whatever the
        client sent after the headers would be parsed as the next request.
        """
        self._error(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed", close=True,
                    headers={"Allow": allowed})

    def _body(self) -> dict:
        """The JSON body, or a refusal that ends the connection."""
        if "Transfer-Encoding" in self.headers:
            # Chunk framing needs a decoder we do not have, and its terminator
            # would surface as ``400 Bad request syntax ('0')``.
            self._refuse(HTTPStatus.BAD_REQUEST, "Transfer-Encoding is not supported")
        stated = (self.headers.get("Content-Length") or "0").strip()
        # ``isdigit`` alone would accept non-ASCII digits that ``int`` then
        # refuses, which is the 500 this guard exists to prevent.
        if not (stated.isdigit() and stated.isascii()):
            # A negative length would also read as "no body" and come back as
            # the misleading "tokens are required".
            self._refuse(HTTPStatus.BAD_REQUEST, f"bad Content-Length: {stated!r}")
        length = int(stated)
        if length == 0:
            return {}
        if length > MAX_BODY:
            self._refuse(HTTPStatus.BAD_REQUEST, f"request body over {MAX_BODY} bytes")
        raw = self.rfile.read(length)
        try:
            loaded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise api.BadRequest(f"body must be JSON: {exc}") from exc
        if not model.is_map(loaded):
            raise api.BadRequest("body must be a JSON object")
        return loaded

    # -- routes ----------------------------------------------------------- #
    #
    # `do_GET` and friends are not our casing to fix: `BaseHTTPRequestHandler`
    # dispatches on the literal name `do_<METHOD>`, so snake_case would silently
    # stop routing. Ruff knows the convention and stays quiet, which is why no
    # suppression comment is needed here — the nine N802 noqa markers that were
    # here were dead, and a dead suppression is a comment that eventually lies.

    def do_GET(self) -> None:
        self._route("GET")

    def do_HEAD(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    # Verbs there is nothing for still have to be routed, not left to the
    # stdlib, whose answer is ``501`` with a text/html body.
    def do_PUT(self) -> None:
        self._route("PUT")

    def do_DELETE(self) -> None:
        self._route("DELETE")

    def do_PATCH(self) -> None:
        self._route("PATCH")

    def do_OPTIONS(self) -> None:
        self._route("OPTIONS")

    def do_TRACE(self) -> None:
        self._route("TRACE")

    def _for_us(self) -> bool:
        """Reject requests a browser sent here on another site's behalf.

        The board has no auth - loopback is the whole security story - so
        a DNS-rebinding page (``Host: evil.example`` resolving to 127.0.0.1) or
        any web page blind-POSTing ``text/plain`` to this port would otherwise
        rewrite the file and drive the Commit button (CSRF, proven end to end).
        Browsers always set Host and (for cross-origin) Origin/Referer; a
        non-browser client that omits them all is on the machine already.
        ``make_server`` leaves ``allowed_hosts`` None for public/README binds,
        which have their own documented tradeoff.
        """
        if self.allowed_hosts is None:
            return True
        # ``urlparse`` rather than rsplit: an IPv6 ``Host: [::1]:7474`` has more
        # than one colon, and Origin hostnames arrive bracket-less.
        host = urlparse("//" + (self.headers.get("Host") or "")).hostname or ""
        if host and host not in self.allowed_hosts:
            return False
        if self.command != "POST":
            return True
        for referer in (self.headers.get("Origin"), self.headers.get("Referer")):
            if not referer:
                continue
            origin = urlparse(referer)
            if origin.scheme in ("http", "https") and origin.hostname not in self.allowed_hosts:
                return False
        return True

    def _route(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if not self._for_us():
            # ``close`` because on a POST the body is still unread: handing the
            # rest of the socket to the parser would run it as a second request.
            self._error(HTTPStatus.FORBIDDEN, "this server only answers its own address",
                        close=True)
            return
        allowed = "GET, HEAD, POST" if path.startswith("/api/") else "GET, HEAD"
        try:
            if path.startswith("/api/"):
                if method == "GET":
                    self._api_get(path)
                elif method == "POST":
                    self._api_post(path)
                else:
                    self._method_not_allowed(allowed)
                return
            if method != "GET":
                self._method_not_allowed(allowed)
                return
            self._static(path)
        except _Answered:   # _refuse() answered and closed already
            return
        except api.BadRequest as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except api.NotFound as exc:
            self._error(HTTPStatus.NOT_FOUND, str(exc))
        except ValidationError as exc:
            self._error(HTTPStatus.UNPROCESSABLE_ENTITY, "the change would make the board invalid",
                        errors=exc.errors)
        except api.ClaimDenied as exc:
            # 409 like a stale write, and just as deliberately *not* a reload:
            # nothing moved under the client, it simply cannot have this card.
            # Sending a document here would make app.js adopt it and wipe the
            # modal the person was typing a note into.
            self._json(HTTPStatus.CONFLICT, {
                "ok": False,
                "error": exc.lines[0] if exc.lines else "claim denied",
                "conflict": True,
                "claim_denied": True,
                "lines": exc.lines,
                "detail": exc.detail,
            })
        except api.Conflict as exc:
            # Stale write. Re-read and hand the whole document back so the UI
            # can repaint instead of guessing which of its cards are gone.
            self._json(HTTPStatus.CONFLICT, {
                "ok": False,
                "error": "the board changed on disk (another tab, git, or an "
                         "editor) - reloaded",
                "conflict": True,
                "claim_denied": False,
                "paths": exc.paths,
                "state": self.app.payload(),
            })
        except store.StoreError as exc:
            # ``str(exc)`` names the file and the errno, which is the operator's
            # problem to read, not something to hand a browser.
            self._fail(f"board: {exc}")
        except gitops.GitError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc), git_error=True)
        except TimeoutError:
            # A client that declared a body and stopped writing. Say 408 while
            # the socket will still take it, and close either way: those unread
            # bytes would otherwise be parsed as the next request.
            with contextlib.suppress(OSError):   # pragma: no cover - it hung up
                self._error(HTTPStatus.REQUEST_TIMEOUT, "request timed out", close=True)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - client vanished
            pass
        except Exception:
            self._fail(traceback.format_exc())

    def _api_get(self, path: str) -> None:
        if path == "/api/state":
            self._json(HTTPStatus.OK, self.app.payload())
        elif path == "/api/git":
            self._json(HTTPStatus.OK, self.app.git_status())
        elif path == "/api/health":
            # The liveness reply a script reads: ``version`` is the build, which
            # until now could only be scraped off the ``Server:`` header - the
            # one SV-29 stopped putting a version in. The document schema is
            # ``schema_version``, the name :meth:`Application.payload` uses.
            self._json(HTTPStatus.OK, {"ok": True, "version": __version__,
                                       "schema_version": model.VERSION})
        else:
            self._error(HTTPStatus.NOT_FOUND, f"no such endpoint {path}")

    def _api_post(self, path: str) -> None:
        if path == "/api/apply":
            self._json(HTTPStatus.OK, self.app.apply(self._body()))
        elif path == "/api/commit":
            self._json(HTTPStatus.OK, self.app.commit(self._body()))
        else:
            # The body of a POST to nowhere was never read.
            self._error(HTTPStatus.NOT_FOUND, f"no such endpoint {path}", close=True)

    # -- static ----------------------------------------------------------- #

    def _static(self, path: str) -> None:
        target = resolve_static(path)
        if target is None:
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        kind = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        try:
            body = target.read_bytes()
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.EISDIR, errno.ENOTDIR):
                # vanished, or is a directory, between resolving and reading
                self._error(HTTPStatus.NOT_FOUND, "not found")
                return
            self._fail(f"cannot read {target.name}: {exc}")
            return
        extra = {"Content-Security-Policy": CSP_HTML} if kind.startswith("text/html") else None
        self._send(HTTPStatus.OK, body, kind, extra)


def resolve_static(path: str) -> Path | None:
    """Map a URL path onto a file inside :data:`STATIC_DIR`, or ``None``.

    Symlinks are not followed: the fully-resolved target must still live inside
    the static directory, so ``/../../etc/passwd`` and friends are refused.
    """
    clean = path.strip("/") or "index.html"
    if "\x00" in clean:   # a dot-dot prefix needs no guard: resolve() ends it
        return None
    candidate = (STATIC_DIR / clean).resolve()
    root = STATIC_DIR.resolve()
    if candidate != root and root not in candidate.parents:
        return None
    if candidate.is_dir():
        candidate = candidate / "index.html"
    return candidate if candidate.is_file() else None


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.log_requests = False

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Hang up quietly when the client hung up first.

        A reset lands in ``http.server``'s own keep-alive readline, outside
        anything ``_route`` can catch, and the stdlib's answer is a traceback
        for every abandoned connection. Anything else still prints.
        """
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            self.close_request(request)
            return
        super().handle_error(request, client_address)


def make_server(
    app: Application,
    host: str = "127.0.0.1",
    port: int = 7474,
    *,
    log_requests: bool = False,
    allow_public: bool = False,
) -> Server:
    if not is_loopback(host) and not (app.read_only or allow_public):
        # Anyone reachable on that interface could rewrite the board: there
        # is no authentication here, so a LAN bind has to be asked for twice.
        raise RuntimeError(
            f"refusing to serve a writable board on {host}: there is no auth, so "
            "anyone who can reach the port can edit your file. Pass --allow-public "
            "if that is what you want, or --read-only to make it safe."
        )
    # Only a loopback bind gets the Host/Origin guard: on one the user asked to
    # reach from another machine, that machine's own hostname varies and the
    # decision to expose the port was already explicit.
    allowed = local_hosts(host)
    handler = type("BoundHandler", (Handler,), {"app": app, "allowed_hosts": allowed})
    server = Server((host, port), handler)
    server.log_requests = log_requests
    return server
