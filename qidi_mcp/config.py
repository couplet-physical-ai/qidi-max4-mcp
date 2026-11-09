"""Runtime configuration, sourced from environment variables.

Everything is env-driven so the MCP entry in your agent's config is the single
place a user configures the server. No secrets are required for a stock QIDI
printer on the LAN (Moonraker's ``trusted_clients`` covers private subnets),
but an API key / JWT is supported for hardened setups.

A note on the live-printer guard: ``QIDI_TEST_MODE=1`` forces the target host
to loopback. The test suite sets it and additionally blocks non-loopback
sockets outright, so no test run can reach a real machine.

This module covers the single-process, local-stdio shape: one server, one
printer, one caller. (An earlier revision of this project also had a
multi-printer "fleet" mode and a hosted multi-user deployment layered on top;
those depended on internal services and are not part of this public release.
``QIDI_IDENTITY_ENFORCE`` and the ``Role``/``RoleTable`` machinery in
``identity.py`` remain generic and usable on their own if you want to put an
auth proxy in front of this server yourself.)
"""

from __future__ import annotations

import ipaddress
import math
import os
from dataclasses import dataclass, field, replace
from urllib.parse import urlparse, urlsplit, urlunsplit

from .errors import InvalidRequest
from .limits import (
    DEFAULT_SAFETY,
    MAX4,
    HardwareProfile,
    HeaterLimits,
    SafetyEnvelope,
    clamp_envelope,
)

ENV_PREFIX = "QIDI_"


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(ENV_PREFIX + name, default)


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    """Read a float setting, rejecting values that are not real numbers.

    ``float()`` happily accepts ``"inf"`` and ``"nan"``, and both are actively
    dangerous here. ``nan`` breaks every comparison it touches -- ``nan > limit``
    is False, so a NaN would sail through a bounds check -- and ``inf`` as a
    confirmation TTL makes tickets immortal, defeating the point of a short-lived
    token. Neither is ever a meaningful configuration value, so they are refused
    at load time rather than surfacing later as a mysterious internal error.
    """
    raw = _env(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise InvalidRequest(
            f"{ENV_PREFIX}{name} must be a number, got {raw!r}"
        ) from exc
    if not math.isfinite(value):
        raise InvalidRequest(
            f"{ENV_PREFIX}{name} must be a finite number, got {raw!r}.",
            hint="Infinity and NaN are refused: they would disable the checks they feed.",
        )
    if minimum is not None and value < minimum:
        raise InvalidRequest(
            f"{ENV_PREFIX}{name} must be at least {minimum:g}, got {value:g}."
        )
    if maximum is not None and value > maximum:
        raise InvalidRequest(
            f"{ENV_PREFIX}{name} must be at most {maximum:g}, got {value:g}."
        )
    return value


def _is_loopback(host: str) -> bool:
    if host in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


#: A printer id: short, lowercase, URL- and filename-safe. Just a label today
#: (this release is single-printer); kept so audit-log entries and a future
#: multi-printer setup have somewhere to read it from.
def _printer_id(raw: str) -> str:
    value = (raw or "max4").strip().lower()
    return value or "max4"


@dataclass(frozen=True)
class Settings:
    """Fully resolved server configuration."""

    #: Base URL of Moonraker, e.g. http://192.0.2.10 (port 7125 added if absent).
    base_url: str
    #: Optional Moonraker API key (X-Api-Key) or JWT bearer token.
    api_key: str | None
    bearer_token: str | None

    hardware: HardwareProfile
    safety: SafetyEnvelope

    #: Refuse every mutating tool. The safest way to hand a printer to an agent.
    read_only: bool
    #: Require two-step ticket confirmation for *all* guarded actions, not just
    #: destructive ones.
    strict_confirm: bool
    #: Permit motion/thermal changes while a print is active.
    allow_during_print: bool
    #: Operator-side switch for deny-listed raw G-code (SAVE_CONFIG, FORCE_MOVE,
    #: ...). ``allow_dangerous=true`` is an argument the *agent* supplies, behind
    #: the same token the agent redeems, so on its own it is not an operator
    #: decision. With this off (the default) deny-listed commands are refused
    #: whatever the agent passes; with it on, they still need allow_dangerous=true
    #: and the two-step token.
    allow_dangerous_gcode: bool
    #: Seconds a confirmation ticket stays valid.
    confirm_ttl: float

    http_timeout: float
    long_timeout: float
    #: Path for the JSONL audit log; None disables file logging.
    audit_log: str | None
    #: How this agent identifies itself in the shared lease. Blank means user@host.
    agent_name: str
    #: Require a verified caller identity and apply per-person roles. Off by
    #: default: a local stdio checkout is one user, no edge to authenticate
    #: against. If you front this server with your own auth proxy, turn this
    #: on and have it populate the headers ``identity.resolve_caller`` reads.
    identity_enforce: bool
    #: Refuse mutating actions while another agent holds the printer lease.
    lease_enforce: bool
    #: Require holding the lease before starting a print or releasing the queue.
    lease_required_for_print: bool
    #: Default lease duration in minutes when a caller does not say.
    lease_minutes: float
    #: Rotate the audit log once it reaches this size. 0 disables rotation.
    audit_max_bytes: int
    #: How many rotated audit files to keep (audit.jsonl.1 ... .N).
    audit_keep: int
    #: Camera snapshot URL. Defaults to nginx's /webcam/ proxy on :80, which is
    #: what answers on this firmware (:8080 from crowsnest.conf is closed).
    webcam_url: str | None
    #: Max bytes returned by file/log reads, to protect the agent's context.
    max_read_bytes: int
    #: Max bytes accepted for a G-code upload.
    max_upload_bytes: int

    test_mode: bool

    #: Secret that makes successive server processes one lease holder (see
    #: ``qidi_mcp.lease.holder_id_for``). Blank means a fresh holder per process.
    lease_holder_key: str = field(default="", repr=False)

    #: Which printer this is, for audit-log labelling. Blank/default "max4" in
    #: this single-printer release; a multi-printer deployment is a natural
    #: extension point but is not part of this package.
    printer_id: str = "max4"

    @property
    def host(self) -> str:
        return urlparse(self.base_url).hostname or ""

    def with_safety(self, **kw: HeaterLimits | float) -> Settings:
        return replace(self, safety=replace(self.safety, **kw))  # type: ignore[arg-type]


def _normalise_base_url(raw: str, *, test_mode: bool) -> str:
    raw = raw.strip()
    if not raw:
        raise InvalidRequest(
            f"No printer address configured. Set {ENV_PREFIX}PRINTER_URL "
            "(e.g. http://192.0.2.10, or http://127.0.0.1:<port> for the mock) "
            "in the MCP server entry."
        )
    if "://" not in raw:
        raw = "http://" + raw
    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https"}:
        raise InvalidRequest(f"Printer URL scheme must be http or https, got {parts.scheme!r}")
    if not parts.hostname:
        raise InvalidRequest(f"Could not parse a host out of {raw!r}")

    host = parts.hostname
    port = parts.port

    if test_mode and not _is_loopback(host):
        # Hard guard: in test mode we never speak to a real machine.
        raise InvalidRequest(
            f"{ENV_PREFIX}TEST_MODE=1 refuses a non-loopback host ({host}). "
            "Point the tests at 127.0.0.1 or unset test mode."
        )

    # Moonraker's own port. Stock QIDI also reverse-proxies it on :80 alongside
    # Fluidd, but :7125 is the documented, always-present endpoint.
    netloc = f"{host}:7125" if port is None else f"{host}:{port}"
    return urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """Build ``Settings`` from the environment (or an explicit mapping)."""
    if env is not None:
        saved = dict(os.environ)
        os.environ.clear()
        os.environ.update(env)
        try:
            return load_settings()
        finally:
            os.environ.clear()
            os.environ.update(saved)

    test_mode = _env_bool("TEST_MODE", False)
    base_url = _normalise_base_url(_env("PRINTER_URL", "") or "", test_mode=test_mode)

    hardware = MAX4  # only profile shipped today; Plus4/Q2 slot in here later

    safety = clamp_envelope(
        replace(
            DEFAULT_SAFETY,
            extruder=HeaterLimits(
                min=0.0,
                max=_env_float("MAX_EXTRUDER_TEMP", DEFAULT_SAFETY.extruder.max, minimum=0.0),
                confirm_above=_env_float(
                    "CONFIRM_EXTRUDER_TEMP", DEFAULT_SAFETY.extruder.confirm_above
                ),
            ),
            bed=HeaterLimits(
                min=0.0,
                max=_env_float("MAX_BED_TEMP", DEFAULT_SAFETY.bed.max, minimum=0.0),
                confirm_above=_env_float("CONFIRM_BED_TEMP", DEFAULT_SAFETY.bed.confirm_above),
            ),
            chamber=HeaterLimits(
                min=0.0,
                max=_env_float("MAX_CHAMBER_TEMP", DEFAULT_SAFETY.chamber.max, minimum=0.0),
                confirm_above=_env_float(
                    "CONFIRM_CHAMBER_TEMP", DEFAULT_SAFETY.chamber.confirm_above
                ),
            ),
            min_jog_z=_env_float("MIN_JOG_Z", DEFAULT_SAFETY.min_jog_z),
            max_jog_distance=_env_float("MAX_JOG_DISTANCE", DEFAULT_SAFETY.max_jog_distance),
            max_extrude_distance=_env_float(
                "MAX_EXTRUDE_DISTANCE", DEFAULT_SAFETY.max_extrude_distance
            ),
            max_move_feedrate=_env_float("MAX_FEEDRATE", DEFAULT_SAFETY.max_move_feedrate),
            max_velocity=_env_float("MAX_VELOCITY", DEFAULT_SAFETY.max_velocity),
            max_accel=_env_float("MAX_ACCEL", DEFAULT_SAFETY.max_accel),
            max_square_corner_velocity=_env_float(
                "MAX_SQUARE_CORNER_VELOCITY", DEFAULT_SAFETY.max_square_corner_velocity
            ),
        ),
        hardware,
    )

    webcam = _env("WEBCAM_URL")
    if webcam is None:
        host = urlparse(base_url).hostname
        # MEASURED default, not the crowsnest.conf one. crowsnest.conf advertises
        # :8080, but on the reference Max4 that port is closed and the snapshot is
        # served by nginx at /webcam/ on :80. Defaulting to the URL that actually
        # answers means get_webcam_snapshot works without anyone setting
        # QIDI_WEBCAM_URL.
        webcam = f"http://{host}/webcam/?action=snapshot" if host else None

    return Settings(
        base_url=base_url,
        api_key=_env("API_KEY") or None,
        bearer_token=_env("BEARER_TOKEN") or None,
        hardware=hardware,
        safety=safety,
        read_only=_env_bool("READ_ONLY", False),
        strict_confirm=_env_bool("STRICT_CONFIRM", False),
        allow_during_print=_env_bool("ALLOW_DURING_PRINT", False),
        allow_dangerous_gcode=_env_bool("ALLOW_DANGEROUS_GCODE", False),
        # Bounded deliberately: an unbounded TTL yields a ticket that can be
        # redeemed hours later against a printer in a completely different state,
        # and a zero/negative TTL makes every destructive tool permanently
        # unusable.
        confirm_ttl=_env_float("CONFIRM_TTL", 120.0, minimum=5.0, maximum=3600.0),
        http_timeout=_env_float("HTTP_TIMEOUT", 10.0),
        long_timeout=_env_float("LONG_TIMEOUT", 120.0),
        # One shared printer, one agent at a time. Enforcement only ever blocks a
        # caller when somebody *else* holds the lease, so a lone user never meets
        # it -- which is why it can default to on without adding friction.
        agent_name=(_env("AGENT_NAME") or "").strip()[:120],
        # Off by default so a laptop checkout behaves as a single-user tool. See
        # the module docstring for how to use this if you add your own auth edge.
        identity_enforce=_env_bool("IDENTITY_ENFORCE", False),
        lease_enforce=_env_bool("LEASE_ENFORCE", True),
        lease_required_for_print=_env_bool("REQUIRE_LEASE_FOR_PRINT", True),
        lease_minutes=_env_float("LEASE_MINUTES", 60.0, minimum=1.0, maximum=1440.0),
        audit_log=_env("AUDIT_LOG") or None,
        # A long-lived server writing an unbounded log eventually fills the disk,
        # and the audit trail is the last thing that should take the machine out.
        # 0 disables rotation for anyone shipping the file somewhere else.
        audit_max_bytes=int(_env_float("AUDIT_MAX_BYTES", 5_000_000, minimum=0.0)),
        audit_keep=int(_env_float("AUDIT_KEEP", 3, minimum=0.0, maximum=100.0)),
        webcam_url=webcam,
        max_read_bytes=int(_env_float("MAX_READ_BYTES", 200_000)),
        max_upload_bytes=int(_env_float("MAX_UPLOAD_BYTES", 300_000_000)),
        test_mode=test_mode,
        lease_holder_key=(_env("LEASE_HOLDER_KEY") or "").strip(),
        printer_id=_printer_id(_env("PRINTER_ID") or ""),
    )
