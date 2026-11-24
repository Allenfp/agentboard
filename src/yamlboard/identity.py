"""Who is asking: resolving the agent an operation is performed as.

Identity here is honour-system by choice, not by omission. There is no
authentication anywhere in this project — ``server.is_loopback`` is the entire
security story — so a self-declared agent name is consistent with the trust
model, and a per-agent token would imply a guarantee this design does not make.

What *is* enforced: the name must be a slug, and it must already be one of the
board's ``people``. That list is the allowlist. Nothing here creates a person,
ever — an agent that invents an identity would otherwise leave the file with an
assignee nobody recognises, and the mistake would only surface weeks later in a
``git blame``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import gitops, model

ENV_VAR = "YAMLBOARD_AGENT"


class UnknownAgent(Exception):
    """The caller named — or defaulted to — an identity the board does not know.

    Distinct from a claim denial: this is "you are not allowed to speak", not
    "someone else is holding that card", and an agent that confuses the two will
    retry a refusal that no retry can fix.
    """

    def __init__(self, message: str, known: list[str] | None = None):
        self.known = list(known or [])
        super().__init__(message)


def from_git_email(root: Path | None) -> str | None:
    """The local part of ``git config user.email``, or None.

    Only the local part, because that is what a person recognises: an agent
    running under ``grigory@work.example`` means *grigory*, and the other
    ninety percent of the address is a detail nobody needs in a card's
    ``assignee``.
    """
    if root is None:
        return None
    try:
        email = gitops.config_value(root, "user.email")
    except gitops.GitError:
        return None
    if not email or "@" not in email:
        return None
    return email.split("@", 1)[0].strip().lower() or None


def candidate(
    *,
    as_flag: str | None = None,
    root: Path | None = None,
    env: dict[str, str] | None = None,
) -> str | None:
    """The name this run would act as, before the board is asked to confirm it.

    Slug-checked but NOT allowlist-checked, which is the whole difference between
    this and :func:`resolve`. It exists for the one moment where the allowlist
    cannot answer: a brand-new board has no people yet, so validating against it
    would reject the very identity that has to seed it. Returning None means
    nobody is named, and callers decide whether that is a default or an error.
    """
    env = os.environ if env is None else env
    raw = as_flag or env.get(ENV_VAR) or from_git_email(root)
    text = str(raw).strip() if raw else ""
    return text if model.SLUG_RE.match(text) else None


def resolve(
    doc: dict | None,
    *,
    as_flag: str | None = None,
    root: Path | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """The agent to act as: ``--as``, else ``$YAMLBOARD_AGENT``, else git config.

    Raises :class:`UnknownAgent` rather than defaulting to a name. "Just call it
    *agent*" would put a phantom person on the board and make the claim
    unattributable, which defeats the point of recording it at all.
    """
    env = os.environ if env is None else env
    raw = as_flag or env.get(ENV_VAR) or from_git_email(root)
    if not raw or not str(raw).strip():
        raise UnknownAgent(
            f"no agent: pass --as <name> or set {ENV_VAR}",
            sorted(model.person_ids(doc or {})),
        )
    return check(doc, str(raw).strip())


def check(doc: dict | None, name: str) -> str:
    """Validate one name against the grammar and the board's people."""
    known = sorted(model.person_ids(doc or {}))
    if not model.SLUG_RE.match(name):
        raise UnknownAgent(
            f"agent {name!r} is not a slug like 'grigory' (lowercase letters, "
            "digits, '-' or '_')", known,
        )
    if name not in known:
        raise UnknownAgent(
            f"agent {name!r} is not one of this board's people"
            + (f" ({', '.join(known)})" if known else " (the board lists none)"),
            known,
        )
    return name


def banner(name: str, *, source: str | None = None) -> str:
    """The line printed before a mutating write, naming the identity in use.

    Printed *first* on purpose: an agent whose ``$YAMLBOARD_AGENT`` is stale
    should see that before the wrong name reaches a commit message.
    """
    return f"as {name}" + (f" ({source})" if source else "")


def person(doc: dict, name: str) -> dict | None:
    """The ``people`` entry for ``name``: colour and display name for the chip."""
    for entry in model.people(doc):
        if entry.get("id") == name:
            return entry
    return None
