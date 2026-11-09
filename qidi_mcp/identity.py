"""Who is calling, and what may they do to the printer.

The stdio server has no identity problem: one process, one laptop, one person,
and the machine's owner decided by setting ``QIDI_READ_ONLY``. A shared deployment
does, because one process serves many people and the printer
still has exactly one hotend.

So this module answers two questions, kept deliberately separate:

**Who is calling?** Never from a tool argument -- a model that can pass
``email="someone@example.com"`` is not proof of anything. Identity comes from
request headers an auth proxy in front of this server stamps, and it is
parsed by an auth adapter you supply (a module exposing ``user_from_headers(headers)``;
not shipped), not by us: that adapter verifies the caller's token and
cross-checks the forwarded headers. Rolling
our own header check is the documented way to get spoofed by anything that can
reach the container's port.

**What may they do?** Three roles, and the default is the safe one:

===========  =====================================================  ==========================
Role         May                                                    Source
===========  =====================================================  ==========================
observer     read anything                                          every authenticated caller
operator     + move, heat, print, upload                            ``QIDI_OPERATORS``
maintainer   + irreversible: delete, config writes, calibration     ``QIDI_MAINTAINERS``
===========  =====================================================  ==========================

Everyone who signs in is an observer. Nobody is promoted by signing in, which is
the point: *"read-only for everyone by default, even after login"*. Promotion is
an explicit list, because the printer is a physical machine in a room
and the blast radius of a wrong answer is a scraped bed or a fire.

``emergency_stop`` is never role-gated. An escape hatch that only privileged
callers can reach is not an escape hatch -- the person who can see the machine
smoking is whoever happens to be looking at it.
"""

from __future__ import annotations

import contextvars
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum

from .errors import QidiError
from .policy import Risk

#: Risk tiers an operator may reach. Reads are open to every authenticated
#: caller, so they are not listed; DESTRUCTIVE is maintainer-only.
_OPERATOR_TIERS = frozenset({Risk.LOW, Risk.MOTION, Risk.THERMAL, Risk.PRINT})


class Role(StrEnum):
    """What a caller is allowed to do. Ordered: each includes the one before."""

    OBSERVER = "observer"
    OPERATOR = "operator"
    MAINTAINER = "maintainer"

    @property
    def rank(self) -> int:
        return {"observer": 0, "operator": 1, "maintainer": 2}[self.value]

    def allows(self, risk: Risk) -> bool:
        """Whether this role may run a call of this risk tier."""
        if risk is Risk.EMERGENCY:
            return True  # never role-gated, deliberately
        if risk is Risk.READ:
            return True
        if risk is Risk.DESTRUCTIVE:
            return self is Role.MAINTAINER
        if risk in _OPERATOR_TIERS:
            return self.rank >= Role.OPERATOR.rank
        # An unrecognised tier is treated as the most dangerous one. A new Risk
        # member must not become universally callable by being forgotten here.
        return self is Role.MAINTAINER


class RoleRequired(QidiError):
    """The caller is authenticated but not permitted this tier."""

    code = "role_required"


class IdentityRequired(QidiError):
    """No verified identity on a request that needs one."""

    code = "identity_required"


@dataclass(frozen=True)
class Caller:
    """A verified caller. ``email`` is the key; everything else is decoration."""

    email: str
    user_id: str | None = None
    name: str | None = None
    role: Role = Role.OBSERVER

    @property
    def label(self) -> str:
        """How this caller appears in a lease and in the audit log."""
        return self.email

    def as_dict(self) -> dict[str, object]:
        return {
            "email": self.email,
            "name": self.name or self.email,
            "role": self.role.value,
            "may": {
                "read": True,
                "move_and_heat": self.role.allows(Risk.MOTION),
                "print": self.role.allows(Risk.PRINT),
                "irreversible": self.role.allows(Risk.DESTRUCTIVE),
                "emergency_stop": True,
            },
        }


def _split(raw: str | None) -> set[str]:
    """Parse an allowlist env var. Commas, spaces or newlines; case-insensitive."""
    if not raw:
        return set()
    out: set[str] = set()
    for chunk in raw.replace(",", " ").split():
        value = chunk.strip().lower()
        if value:
            out.add(value)
    return out


@dataclass(frozen=True)
class RoleTable:
    """The promotion lists, read once per request from the environment.

    Env-var driven rather than a database table on purpose: ``set_app_env`` can
    change it without a deploy, and there is no schema to migrate when this is
    replaced by a directory-service group lookup.
    """

    operators: frozenset[str]
    maintainers: frozenset[str]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> RoleTable:
        src = env if env is not None else os.environ
        return cls(
            operators=frozenset(_split(src.get("QIDI_OPERATORS"))),
            maintainers=frozenset(_split(src.get("QIDI_MAINTAINERS"))),
        )

    def role_for(self, email: str) -> Role:
        key = (email or "").strip().lower()
        if not key:
            return Role.OBSERVER
        if key in self.maintainers:
            return Role.MAINTAINER
        # A maintainer is implicitly an operator; listing someone in both is
        # allowed but redundant.
        if key in self.operators:
            return Role.OPERATOR
        return Role.OBSERVER


def resolve_caller(
    headers: Mapping[str, str],
    *,
    table: RoleTable | None = None,
) -> Caller | None:
    """Identify the caller from request headers, or return None.

    Delegates verification to an auth adapter you supply (a module exposing
    ``user_from_headers(headers)``; not shipped). Returning None means "this
    request carried no identity this server is willing to believe" -- which the
    caller of this function must treat as a refusal, not as an anonymous
    success.
    """
    try:
        from your_auth_adapter import user_from_headers  # not shipped; see module docstring
    except ImportError:
        # Not installed: this is a local stdio checkout, not the deployed app.
        # Enforcement is off in that shape (see identity_enforced), so an
        # ImportError here is not reachable from an enforcing path.
        return None

    user = user_from_headers(headers)
    if user is None:
        return None
    email = getattr(user, "email", None)
    if not email:
        return None
    roles = table or RoleTable.from_env()
    return Caller(
        email=email,
        user_id=getattr(user, "sub", None) or getattr(user, "user_id", None),
        name=getattr(user, "name", None),
        role=roles.role_for(email),
    )


def identity_enforced(env: Mapping[str, str] | None = None) -> bool:
    """Whether to require a verified caller and apply roles.

    Off by default so a local stdio checkout behaves exactly as it always has:
    one person, their own printer envelope, ``QIDI_READ_ONLY`` as the switch.
    The deployed app sets ``QIDI_IDENTITY_ENFORCE=1`` in its manifest, where the
    opposite default is the right one.
    """
    src = env if env is not None else os.environ
    return str(src.get("QIDI_IDENTITY_ENFORCE", "0")).strip().lower() in {"1", "true", "yes"}


# -- the current caller ------------------------------------------------------
#
# A ContextVar rather than a parameter threaded through 96 tool signatures.
# That is not laziness: the tools are the *domain* layer and identity is a
# property of the transport. The MCP middleware sets this once per inbound
# request and the safety gate reads it, so a tool body cannot accidentally
# authorize against an identity it was handed as an argument.
#
# ContextVars are the right primitive because the ASGI server handles requests
# concurrently in one event loop: each task gets its own copy, so two callers
# in flight at once cannot see each other's identity.

_CURRENT: contextvars.ContextVar[Caller | None] = contextvars.ContextVar(
    "qidi_caller", default=None
)


def current_caller() -> Caller | None:
    """The verified caller for the request being handled, if any."""
    return _CURRENT.get()


@contextmanager
def acting_as(caller: Caller | None) -> Iterator[None]:
    """Bind a caller for the duration of one request.

    Always resets, including on exception -- a leaked identity would be
    attributed to the next caller on the same task.
    """
    token = _CURRENT.set(caller)
    try:
        yield
    finally:
        _CURRENT.reset(token)
