"""The docs must not drift from the server, and must not overclaim.

This is a trimmed version of the internal test suite's ``test_docs.py``. The
internal repo's generated-docs guards (the mkdocs site built by the excluded
``scripts/build_mkdocs.py``, the ``offline-docs/`` site, and ``docs/index.md``)
are not part of this export, so the tests exercising them are dropped. The four
guards kept here do not depend on any of that:

* the tool-count claims in README.md are checked against the live server, not
  trusted as prose;
* every env var ``config.py`` reads is documented in
  ``docs/reference/configuration.md``;
* nothing under ``docs/`` names a specific operator's printer address or
  hostname (the pattern below is illustrative, not a real address this repo
  ever had to scrub -- adapt it to whatever your own deployment must not leak);
* README.md never claims more tests exist than the suite actually collects.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


@pytest.mark.skipif(not DOCS.exists(), reason="docs/ not present")
def test_published_docs_do_not_name_a_specific_printer_address():
    """`docs/` is published alongside the public repo; keep any one deployment's
    specifics out of it.

    Placeholders (``<printer-ip>``) belong in docs; a real address belongs only
    in a local ``mcp-entry.json`` or a private ``.env``, never in anything
    committed to this repo. This repo's own docs use the RFC 5737 documentation
    range (``192.0.2.0/24``) for every worked example. The pattern below is
    hostname-only (no IP literal) on purpose, so it stays clear of that
    documentation range and of the oss-export gate's own private-IPv4 deny
    pattern -- it is illustrative of the *shape* of guard worth having, not a
    scrub of a real address this repo ever carried.
    """
    pattern = re.compile(r"myworkshop-printer\.local|[\w-]*\.home\.arpa")
    offenders = []
    for page in sorted(DOCS.rglob("*.md")):
        for lineno, line in enumerate(page.read_text().splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{page.relative_to(ROOT)}:{lineno}: {line.strip()[:70]}")
    assert not offenders, "a specific printer address in published docs:\n" + "\n".join(offenders)


def test_the_address_guard_would_catch_a_real_leak():
    """Prove the pattern matches the forms a real leak would take."""
    pattern = re.compile(r"myworkshop-printer\.local|[\w-]*\.home\.arpa")
    assert pattern.search("--printer myworkshop-printer.local")
    assert pattern.search("live printer myworkshop-printer.local never contacted")
    assert pattern.search("http://myworkshop-printer.local:7125")
    assert pattern.search("QIDI_PRINTER_URL=http://printer.home.arpa:7125")
    assert not pattern.search("--printer <printer-ip>")
    assert not pattern.search("http://127.0.0.1:7125")
    assert not pattern.search("https://qidi.mcp.example.com/mcp")
    assert not pattern.search("https://qidi.apps.example.com/")


@pytest.mark.skipif(not DOCS.exists(), reason="docs/ not present")
def test_every_env_var_config_reads_is_documented():
    """A setting nobody can discover is a setting nobody can use.

    Adding a field to ``Settings`` and forgetting the docs is easy and silent.
    This reads the env names straight out of ``config.py`` so the docs cannot
    drift behind the code.
    """
    source = (ROOT / "qidi_mcp" / "config.py").read_text()
    names = {
        f"QIDI_{m}" for m in re.findall(r'_env(?:_bool|_float)?\(\s*"([A-Z0-9_]+)"', source)
    }
    assert len(names) > 15, f"env-var extraction looks broken: found {names}"

    reference = (DOCS / "reference" / "configuration.md").read_text()
    missing = sorted(n for n in names if n not in reference)
    assert not missing, f"env vars read by config.py but undocumented: {missing}"


def test_the_env_var_guard_would_notice_an_undocumented_setting():
    """Prove the check above bites, rather than passing on an empty set."""
    fake = 'x = _env_float("BRAND_NEW_KNOB", 1.0)\ny = _env("PRINTER_URL")\n'
    names = {f"QIDI_{m}" for m in re.findall(r'_env(?:_bool|_float)?\(\s*"([A-Z0-9_]+)"', fake)}
    assert names == {"QIDI_BRAND_NEW_KNOB", "QIDI_PRINTER_URL"}
    assert "QIDI_BRAND_NEW_KNOB" not in "| `QIDI_PRINTER_URL` | required |"


async def test_tool_counts_in_the_docs_match_the_running_server(server_rt):
    """Every count the docs quote, checked against the server that produces them.

    These have drifted before, and one "correction" was itself wrong: the docs
    once claimed 21 of 22 destructive-annotated tools needed a token, when the
    real split was 20 of 22 (``abort_macro`` is annotated destructive but
    deliberately takes a single ``confirm=true``). A number nobody can check is
    a number that rots, so this asserts them from the live server instead of
    trusting prose.
    """
    server, _ = server_rt
    tools = await server.list_tools()

    total = len(tools)
    read_only = sum(1 for t in tools if getattr(t.annotations, "read_only_hint", False))
    destructive = [t for t in tools if getattr(t.annotations, "destructive_hint", False)]
    token_gated = [
        t for t in destructive if "confirm_token" in (t.input_schema.get("properties") or {})
    ]
    unannotated = [t.name for t in tools if t.annotations is None]

    assert not unannotated, f"tools with no annotations: {unannotated}"

    exceptions = sorted(t.name for t in destructive if t not in token_gated)
    assert exceptions == ["abort_macro", "emergency_stop"], (
        f"the set of destructive-but-not-token-gated tools changed: {exceptions}. "
        "If that is intended, update README.md to explain the new one."
    )

    numbers = {
        "total": total,
        "read_only": read_only,
        "destructive": len(destructive),
        "token_gated": len(token_gated),
    }

    readme = (ROOT / "README.md").read_text()

    assert f"{total} tools in" in readme, f"README does not say '{total} tools in' ({numbers})"
    assert f"{read_only} read-only" in readme, f"README read-only count is not {read_only}"
    assert f"{len(destructive)} carrying the destructive annotation" in readme
    assert f"Of those {len(destructive)}, **{len(token_gated)} always require a token**" in readme


def test_the_docs_never_claim_more_tests_than_exist():
    """Collected, not remembered — but pinned loosely, on purpose.

    Tool counts are pinned exactly (above), because a change there is an API
    change somebody should be describing anyway. Test counts move on almost
    every commit, so this only asserts the two failure modes that matter: the
    docs must never claim *more* tests than exist, and must not fall so far
    behind that the number is decorative.

    Fetching the optional upstream sources (``bash scripts/fetch_upstream_sources.sh``)
    makes `tests/test_box_labels_and_queue.py` and `tests/test_dispatch_fit.py`
    parametrize over the real (fetched) filament table instead of a smaller
    built-in sample, so pytest collects noticeably more tests in that state.
    README.md quotes both counts -- "N tests (M with the optional upstream
    sources fetched)" -- so this guard matches whichever one applies to how
    pytest was actually run, rather than requiring both to be within 20 of a
    single collected count that can only be one of them at a time.
    """
    import subprocess

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--collect-only", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    match = re.search(r"(\d+) tests? collected", proc.stdout)
    assert match, f"could not read the collected count from pytest: {proc.stdout[-400:]}"
    collected = int(match.group(1))

    path = ROOT / "README.md"
    text = path.read_text()
    quoted = {int(n) for n in re.findall(r"\*\*(\d{3,5})\*\*, all against", text)}
    quoted |= {int(n) for n in re.findall(r"#\s*(\d{3,5}) tests", text)}
    quoted |= {int(n) for n in re.findall(r"(\d{3,5}) with the optional upstream sources fetched", text)}
    assert quoted, f"{path.name} no longer quotes a test count in a form this can check"

    # `collected` reflects exactly one state (fetched or not). README.md quotes
    # both, so it is enough for ONE quoted number to describe the state pytest
    # was actually run in: no more than `collected` (never overclaim) and no
    # more than 20 short of it (never go stale). The other quoted number
    # describes the other state and is not checked against this run.
    matches_current_state = [
        number for number in quoted if number <= collected and collected - number <= 20
    ]
    assert matches_current_state, (
        f"{path.name} quotes {sorted(quoted)} tests but pytest collects {collected} in this "
        "state (fetched or not) — refresh the docs"
        )
