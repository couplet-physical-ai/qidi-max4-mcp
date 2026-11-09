"""The dashboard's single HTML page.

Kept in its own module so ``dashboard.py`` stays readable, and so the markup can
be imported by a test without starting a server.

Design notes, since they are not obvious from the CSS:

*   Accent ``#f2754e`` is the spool colour recorded in the slicer metadata of the
    first part printed through this server, not a decorative pick.
*   Every heater meter shows three things on one track: the reading, the target,
    and the ceiling this server enforces. That comparison is the reason the
    project exists, so it is the visual focus rather than a footnote.
*   State is never colour alone -- each badge carries a glyph and a word, so it
    survives greyscale and colour-blindness.
*   No web fonts and no CDN: the runtime may have no route to the internet, and
    a dashboard that degrades to Times because a font failed is worse than one
    that was always going to use the system stack.
*   Controls appear only when ``/api/whoami`` names a caller whose role allows
    them, so the local ``qidi-dashboard`` (which has no such route) stays the
    pure window it always was. Hiding a button is presentation, not security:
    every one of them is a gated tool call on the server.
*   Untrusted strings (file names, emails, refusal messages) only ever reach the
    page through ``textContent``, never ``innerHTML``.
"""

from __future__ import annotations

PAGE = r"""<!DOCTYPE html>
<html lang="en" data-theme="">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>QIDI Max 4 — live</title>
<style>
  :root {
    --bg: #f6f7f9; --surface: #ffffff; --sunk: #eef1f4;
    --line: #dde3e9; --text: #19212a; --muted: #5d6975;
    --accent: #c9502a; --ok: #2c8560; --warn: #9a6c1f; --crit: #bf3f39;
    --shadow: 0 1px 2px rgba(16,24,32,.06), 0 4px 12px rgba(16,24,32,.04);
    --r: 10px;
    --f-ui: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    --f-num: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #11151a; --surface: #191f26; --sunk: #131920;
      --line: #2a333d; --text: #e4e9ee; --muted: #92a0ad;
      --accent: #f2754e; --ok: #46b483; --warn: #dfa63f; --crit: #e2635c;
      --shadow: none;
    }
  }
  :root[data-theme="dark"] {
    --bg: #11151a; --surface: #191f26; --sunk: #131920;
    --line: #2a333d; --text: #e4e9ee; --muted: #92a0ad;
    --accent: #f2754e; --ok: #46b483; --warn: #dfa63f; --crit: #e2635c;
    --shadow: none;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text); font-family: var(--f-ui);
    line-height: 1.45; -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 1100px; margin: 0 auto; padding: 20px 18px 48px; }

  header.bar {
    display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap;
    padding-bottom: 14px; border-bottom: 1px solid var(--line); margin-bottom: 20px;
  }
  h1 { font-size: 1.15rem; margin: 0; letter-spacing: -.01em; text-wrap: balance; }
  .host { font-family: var(--f-num); font-size: .78rem; color: var(--muted);
          min-width: 0; overflow-wrap: anywhere; }
  .bar .spacer { flex: 1 1 auto; }
  .host.stale-note { color: var(--crit); font-weight: 600; }
  /* Readings that may be old are dimmed, never hidden: still useful, not live. */
  body.stale #heaters, body.stale #h-job + .grid > .card:first-child,
  body.stale #h-mach + .grid { opacity: .5; transition: opacity .3s; }
  @media (max-width: 620px) {
    /* Stack the header: the monospace URL plus three nowrap badges cannot share
       one line at phone width, and the spacer would push a badge off-screen. */
    header.bar { gap: 8px 10px; }
    .host { flex: 1 1 100%; }
    .bar .spacer { display: none; }
  }
  button.theme {
    font: inherit; font-size: .78rem; color: var(--muted); background: var(--surface);
    border: 1px solid var(--line); border-radius: 999px; padding: 4px 12px; cursor: pointer;
  }
  button.theme:hover { color: var(--text); }
  button.theme:focus-visible, a:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

  [hidden] { display: none !important; }
  .badge {
    display: inline-flex; align-items: center; gap: 6px; font-size: .74rem;
    font-weight: 600; letter-spacing: .04em; text-transform: uppercase;
    border: 1px solid var(--line); border-radius: 999px; padding: 3px 10px;
    background: var(--surface); white-space: nowrap;
  }
  .badge .dot { width: 7px; height: 7px; border-radius: 2px; background: currentColor; flex: none; }
  .badge.ok { color: var(--ok); } .badge.warn { color: var(--warn); }
  .badge.crit { color: var(--crit); } .badge.idle { color: var(--muted); }
  .badge.live { color: var(--accent); }
  .badge.live .dot { border-radius: 999px; animation: pulse 1.6s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity: .35; } }
  @media (prefers-reduced-motion: reduce) { .badge.live .dot { animation: none; } }

  section { margin-bottom: 22px; }
  h2 {
    font-size: .76rem; text-transform: uppercase; letter-spacing: .07em;
    color: var(--muted); margin: 0 0 10px; font-weight: 600;
  }
  .grid { display: grid; gap: 12px; }
  .g3 { grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }
  .g2 { grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); }

  .card {
    background: var(--surface); border: 1px solid var(--line); border-radius: var(--r);
    padding: 14px; box-shadow: var(--shadow);
  }
  .card h3 {
    margin: 0 0 2px; font-size: .8rem; font-weight: 600; color: var(--muted);
    display: flex; justify-content: space-between; align-items: baseline; gap: 8px;
  }
  .big { font-family: var(--f-num); font-size: 1.7rem; letter-spacing: -.02em; }
  .big .unit { font-size: .8rem; color: var(--muted); margin-left: 2px; }
  .sub { font-size: .78rem; color: var(--muted); font-family: var(--f-num); }

  /* Heater meter: reading fill, target tick, policy ceiling tick. */
  .meter { position: relative; height: 8px; background: var(--sunk);
           border-radius: 999px; margin: 10px 0 6px; overflow: hidden; }
  .meter .fill { position: absolute; inset: 0 auto 0 0; background: var(--accent);
                 border-radius: 999px; transition: width .4s ease; }
  @media (prefers-reduced-motion: reduce) { .meter .fill { transition: none; } }
  /* overflow-x guard: a tick label sitting at ~100% would otherwise push past the
     card and give the whole document a horizontal scrollbar on narrow screens. */
  .ticks { position: relative; height: 14px; overflow: hidden; }
  .tick { position: absolute; top: -4px; transform: translateX(-50%);
          font-size: .66rem; color: var(--muted); font-family: var(--f-num); white-space: nowrap; }
  .tick.at-end { transform: none; }
  .tick.at-end::before { left: auto; right: 2px; }
  .tick.at-start { transform: none; }
  .tick.at-start::before { left: 2px; }
  .tick::before { content: ""; position: absolute; top: -6px; left: 50%;
                  width: 1px; height: 5px; background: var(--muted); }
  .tick.tgt { color: var(--accent); } .tick.tgt::before { background: var(--accent); }

  .rows { display: grid; gap: 8px; }
  .row { display: flex; justify-content: space-between; align-items: baseline;
         gap: 10px; font-size: .85rem; }
  .row dt { color: var(--muted); margin: 0; }
  .row dd { margin: 0; font-family: var(--f-num); text-align: right; }
  dl { margin: 0; }

  .prog { height: 10px; background: var(--sunk); border-radius: 999px; overflow: hidden; }
  .prog .fill { height: 100%; background: var(--accent); width: 0; transition: width .5s ease; }
  @media (prefers-reduced-motion: reduce) { .prog .fill { transition: none; } }

  .note { font-size: .8rem; color: var(--muted); margin: 8px 0 0; }
  .err { color: var(--crit); font-family: var(--f-num); font-size: .8rem; }
  footer { color: var(--muted); font-size: .74rem; border-top: 1px solid var(--line);
           padding-top: 12px; margin-top: 26px; }
  .tabular { font-variant-numeric: tabular-nums; }

  .btn {
    font: inherit; font-size: .8rem; font-weight: 600; color: var(--text);
    background: var(--surface); border: 1px solid var(--line); border-radius: 8px;
    padding: 6px 12px; cursor: pointer;
  }
  .btn:hover:not(:disabled) { border-color: var(--muted); }
  .btn:disabled { opacity: .5; cursor: default; }
  .btn.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  .btn.danger { color: var(--crit); border-color: var(--crit); }
  .btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
  .g-job { grid-template-columns: repeat(auto-fit, minmax(min(320px, 100%), 1fr)); }

  .cam { position: relative; background: var(--sunk); border-radius: 8px; overflow: hidden;
         aspect-ratio: 16 / 9; display: grid; place-items: center; margin-top: 8px; }
  .cam img { width: 100%; height: 100%; object-fit: contain; display: block; }
  .cam img { cursor: zoom-in; }
  .cam img.frozen, .stage img.frozen { opacity: .45; filter: grayscale(1); }
  .cam .msg, .stage .msg { position: absolute; inset: auto 8px 8px 8px; font-size: .78rem;
              color: #fff; background: rgba(0,0,0,.6); border-radius: 6px; padding: 4px 8px; }
  .cam-open { position: absolute; top: 8px; right: 8px; font: inherit; font-size: .74rem; font-weight: 600;
              color: #fff; background: rgba(0,0,0,.55); border: 0; border-radius: 6px;
              padding: 4px 9px; cursor: pointer; }
  .cam-open:hover { background: rgba(0,0,0,.75); }
  .cam-open:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

  /* The enlarged camera. Zoom is a transform on the picture inside a clipped stage,
     so the 2s refresh swaps the frame without losing where you were looking. */
  dialog.viewer { width: calc(100vw - 24px); max-width: 1600px; height: calc(100vh - 24px);
                  max-height: none; padding: 10px; overflow: hidden; }
  dialog.viewer[open] { display: flex; flex-direction: column; gap: 8px; }
  .viewer .vbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .viewer .vbar h3 { margin: 0; flex: 1 1 auto; }
  .viewer .zoom-level { font-family: var(--f-num); font-size: .78rem; color: var(--muted);
                        min-width: 5ch; text-align: right; }
  .viewer .hint { margin: 0; font-size: .74rem; color: var(--muted); }
  .stage { position: relative; flex: 1 1 auto; min-height: 0; background: #000; border-radius: 8px;
           overflow: hidden; touch-action: none; user-select: none; cursor: zoom-in; }
  .stage.zoomed { cursor: grab; }
  .stage.dragging { cursor: grabbing; }
  .stage img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain;
               transform-origin: 0 0; -webkit-user-drag: none; }

  .drop {
    display: block; border: 2px dashed var(--line); border-radius: var(--r); padding: 22px 14px;
    text-align: center; color: var(--muted); font-size: .85rem; cursor: pointer;
  }
  .drop.over { border-color: var(--accent); color: var(--text); background: var(--sunk); }
  .drop strong { color: var(--text); }
  .files { list-style: none; margin: 8px 0 0; padding: 0; display: grid; gap: 6px; }
  .files li { display: flex; align-items: center; gap: 10px; font-size: .84rem; }
  .files .name { flex: 1 1 auto; min-width: 0; overflow-wrap: anywhere; font-family: var(--f-num); }
  .files .meta { color: var(--muted); font-size: .76rem; white-space: nowrap; }
  .status-line { font-size: .8rem; margin: 10px 0 0; min-height: 1.2em; }
  .status-line.err { color: var(--crit); }

  dialog { border: 1px solid var(--line); border-radius: var(--r); background: var(--surface);
           color: var(--text); padding: 18px; width: min(520px, calc(100vw - 32px)); }
  /* visible, not the UA's auto: the people dropdown must hang below the dialog
     rather than be clipped by it. */
  dialog { overflow: visible; }
  dialog::backdrop { background: rgba(0,0,0,.45); }
  dialog h3 { margin: 0 0 10px; font-size: 1rem; overflow-wrap: anywhere; }
  dialog label { display: block; font-size: .8rem; color: var(--muted); margin: 14px 0 4px; }
  input.txt { width: 100%; font: inherit; font-size: .85rem; padding: 6px 8px;
              border: 1px solid var(--line); border-radius: 6px; background: var(--bg); color: var(--text); }
  .warn-box { border-left: 3px solid var(--warn); padding: 6px 10px; font-size: .8rem;
              background: var(--sunk); margin-top: 10px; }
  .follow-row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-top: 10px; }
  .follow-row .picker { flex: 1 1 200px; }
  .follow-row input.txt { font-size: .8rem; padding: 5px 8px; }

  .picker { position: relative; }
  .pick-list { position: absolute; left: 0; right: 0; top: calc(100% + 4px); z-index: 5;
               list-style: none; margin: 0; padding: 4px; max-height: 264px; overflow-y: auto;
               background: var(--surface); border: 1px solid var(--line); border-radius: 8px;
               box-shadow: 0 6px 20px rgba(16,24,32,.14); }
  .pick { display: flex; gap: 10px; align-items: center; padding: 6px 8px; border-radius: 6px;
          cursor: pointer; font-size: .82rem; }
  .pick.active, .pick:hover { background: var(--sunk); }
  .pick.empty { color: var(--muted); cursor: default; }
  .pick .av { flex: none; width: 26px; height: 26px; border-radius: 999px; display: grid;
              place-items: center; font-size: .68rem; font-weight: 700; color: #fff; background: var(--accent); }
  .pick .who { min-width: 0; display: grid; }
  .pick .who strong { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .pick .who span { color: var(--muted); font-size: .74rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
  .chip { display: inline-flex; align-items: center; gap: 2px; font-size: .78rem; border: 1px solid var(--line);
          border-radius: 999px; padding: 2px 2px 2px 10px; background: var(--sunk); max-width: 100%; }
  .chip button { border: 0; background: none; color: var(--muted); cursor: pointer; font-size: .95rem;
                 line-height: 1; padding: 2px 6px; border-radius: 999px; }
  .chip button:hover { color: var(--text); }

  .tblwrap { overflow-x: auto; }

  .fleet { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(220px, 100%), 1fr));
           gap: 10px; margin: -6px 0 20px; }
  .fleet-btn { font: inherit; text-align: left; display: grid; gap: 4px; padding: 10px 12px;
               background: var(--surface); color: var(--text); border: 1px solid var(--line);
               border-radius: var(--r); cursor: pointer; justify-items: start; min-width: 0; }
  .fleet-btn:hover { border-color: var(--muted); }
  .fleet-btn.on { border-color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent); }
  .fleet-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .fleet-btn small { color: var(--muted); font-size: .74rem; overflow-wrap: anywhere; }
  /* minmax(0, 1fr), not the implicit auto track: an auto track grows to the table's
     min-content width and scrolls the whole page sideways on a phone. */
  .g1 { grid-template-columns: minmax(0, 1fr); }
  .tbl { width: 100%; border-collapse: collapse; font-size: .82rem; }
  .tbl th { text-align: left; font-weight: 600; color: var(--muted); font-size: .74rem;
            text-transform: uppercase; letter-spacing: .04em; }
  .tbl th, .tbl td { padding: 7px 6px; border-bottom: 1px solid var(--line); vertical-align: middle; }
  .tbl .who strong { display: block; font-weight: 600; }
  .tbl .who small, .tbl .src { color: var(--muted); font-size: .74rem; }
  .tbl .acts { text-align: right; white-space: nowrap; }
  .tbl .acts .btn { margin-left: 4px; }
  .matrix th:not(:first-child), .matrix td:not(:first-child) { text-align: center; width: 18%; }
  /* Before the phone rule below, which must win: same specificity, so order decides. */
  .matrix .short { display: none; }
  @media (max-width: 620px) {
    /* On a phone the roster becomes a list of cards. Four columns do not fit, and a
       table you scroll sideways hides the very buttons a maintainer came to press. */
    #roster tr:first-child { display: none; }
    #roster, #roster tbody, #roster tr, #roster td { display: block; width: auto; }
    #roster tr { border-bottom: 1px solid var(--line); padding: 10px 0; }
    #roster td { border: 0; padding: 2px 0; }
    #roster td.acts { text-align: left; white-space: normal; margin-top: 6px; }
    #roster td.acts .btn { margin: 0 6px 6px 0; }
    /* Three full-width role columns left the capability text a sliver, wrapping
       every row onto three or four lines. Narrow the check columns instead. */
    .matrix .long { display: none; }
    .matrix .short { display: inline; }
    .matrix th:not(:first-child), .matrix td:not(:first-child) { width: 13%; padding: 7px 2px; }
  }
  .matrix .yes { color: var(--ok); font-weight: 700; }
  .matrix .no { color: var(--muted); }
  .btn.sm { padding: 3px 8px; font-size: .74rem; }
  .add-user { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-top: 12px; }
  .add-user .picker { flex: 1 1 220px; }
  .add-user .chips { margin: 0; }
  .sel { font: inherit; font-size: .82rem; padding: 6px 8px; border: 1px solid var(--line);
         border-radius: 6px; background: var(--bg); color: var(--text); }
  .queue { margin: 8px 0 0; padding-left: 20px; display: grid; gap: 6px; font-size: .84rem; }
  .queue .name { font-family: var(--f-num); overflow-wrap: anywhere; }
  .queue .meta { color: var(--muted); font-size: .76rem; }
  .queue .warn-fit { color: var(--warn); font-size: .76rem; }
  .cam-detail { margin-top: 8px; font-size: .76rem; color: var(--muted); }
  .cam-detail ul { margin: 6px 0 0; padding-left: 18px; font-family: var(--f-num); overflow-wrap: anywhere; }
</style>
</head>
<body>
<div class="wrap">
  <header class="bar">
    <h1 id="title">QIDI Max 4</h1>
    <span class="host" id="host">connecting…</span>
    <span class="spacer"></span>
    <span class="badge ok" id="ro" title="What you may do from this page">
      <span class="dot" aria-hidden="true"></span>read-only</span>
    <span class="badge idle" id="lease" title="Who has claimed this printer">
      <span class="dot" aria-hidden="true"></span>…</span>
    <span class="badge idle" id="conn"><span class="dot" aria-hidden="true"></span>…</span>
    <button class="theme" id="theme" type="button">theme: auto</button>
  </header>

  <nav class="fleet" id="fleet" aria-label="Printers" hidden></nav>

  <section aria-labelledby="h-job">
    <h2 id="h-job">Job</h2>
    <div class="grid g-job">
    <div class="card">
      <h3><span id="job-file">—</span><span class="badge idle" id="job-state">
        <span class="dot" aria-hidden="true"></span>—</span></h3>
      <div class="big tabular" id="job-pct">—<span class="unit">%</span></div>
      <div class="prog" role="progressbar" aria-labelledby="h-job"
           aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" id="job-bar-outer">
        <div class="fill" id="job-bar"></div>
      </div>
      <dl class="rows" style="margin-top:12px">
        <div class="row"><dt>Layer</dt><dd id="job-layer">—</dd></div>
        <div class="row"><dt>Elapsed</dt><dd id="job-elapsed">—</dd></div>
        <div class="row"><dt>Remaining</dt><dd id="job-left">—</dd></div>
        <div class="row"><dt>Filament used</dt><dd id="job-fil">—</dd></div>
      </dl>
      <div class="actions" id="job-actions" hidden>
        <button class="btn" type="button" id="btn-pause" hidden>Pause</button>
        <button class="btn" type="button" id="btn-resume" hidden>Resume</button>
        <button class="btn danger" type="button" id="btn-cancel" hidden>Cancel print…</button>
      </div>
      <div id="follow" hidden>
        <dl class="rows" style="margin-top:12px">
          <div class="row"><dt>Followers</dt><dd id="follow-list">—</dd></div>
        </dl>
        <div class="follow-row">
          <button class="btn" type="button" id="btn-follow">Follow this print</button>
          <div class="picker" id="follow-picker" hidden>
            <input class="txt" type="text" id="follow-email" placeholder="Add someone: search by name"
                   aria-label="Add a follower: search people by name or email" autocomplete="off">
            <ul class="pick-list" id="follow-list-pick" role="listbox" hidden></ul>
          </div>
        </div>
        <p class="note" id="follow-note"></p>
      </div>
      <p class="status-line" id="job-msg" role="status"></p>
    </div>
    <div class="card" id="queue-card" hidden>
      <h3><span>Queue</span><span class="badge idle" id="queue-badge">
        <span class="dot" aria-hidden="true"></span>…</span></h3>
      <ol class="queue" id="queue-list"></ol>
      <p class="sub" id="queue-total"></p>
      <p class="note" id="queue-note"></p>
      <div class="actions" id="queue-actions" hidden>
        <button class="btn" type="button" id="btn-queue-pause">Pause the queue</button>
      </div>
      <p class="status-line" id="queue-msg" role="status"></p>
    </div>
    <div class="card" id="cam-card" hidden>
      <h3><span>Camera</span><span class="badge idle" id="cam-badge">
        <span class="dot" aria-hidden="true"></span>…</span></h3>
      <div class="cam"><img id="cam-img" alt="Live view of the print bed" hidden>
        <button class="cam-open" type="button" id="cam-open" hidden>Expand</button>
        <span class="msg" id="cam-msg" hidden></span></div>
      <details class="cam-detail" id="cam-detail" hidden>
        <summary>What the app tried</summary><ul></ul>
      </details>
      <div class="actions" id="cam-actions" hidden>
        <button class="btn" type="button" id="btn-light">Turn the case light on</button>
      </div>
    </div>
    </div>
  </section>

  <section aria-labelledby="h-print" id="print-section" hidden>
    <h2 id="h-print">Print a file</h2>
    <div class="card">
      <label class="drop" id="drop" for="file-input">
        <strong>Drop a G-code file here</strong> or click to choose one.<br>
        It is uploaded to the printer; nothing starts until you confirm.
      </label>
      <input type="file" id="file-input" accept=".gcode,.gco,.g,.3mf,.ufp,.nc" hidden>
      <div class="prog" id="up-prog" hidden style="margin-top:10px"><div class="fill" id="up-bar"></div></div>
      <p class="status-line" id="print-msg" role="status"></p>
      <h3 style="margin-top:14px">On the printer</h3>
      <ul class="files" id="files"></ul>
    </div>
  </section>

  <section aria-labelledby="h-admin" id="admin-section" hidden>
    <h2 id="h-admin">Users &amp; roles</h2>
    <div class="grid g1">
      <div class="card">
        <h3>People who can do more than watch</h3>
        <div class="tblwrap"><table class="tbl" id="roster"></table></div>
        <div class="add-user">
          <div class="picker">
            <input class="txt" type="text" id="admin-person" placeholder="Add someone: search by name"
                   aria-label="Add someone: search people by name or email" autocomplete="off">
            <ul class="pick-list" id="admin-person-list" role="listbox" hidden></ul>
          </div>
          <div class="chips" id="admin-picked" hidden></div>
          <select class="sel" id="admin-role" aria-label="Role to grant">
            <option value="operator">Operator</option>
            <option value="maintainer">Maintainer</option>
          </select>
          <button class="btn primary" type="button" id="admin-grant" disabled>Grant</button>
        </div>
        <p class="status-line" id="admin-msg" role="status"></p>
        <p class="note">Grants made here apply at once, to this dashboard and to agents through the
          MCP, and every change is in the audit log. People listed in the server configuration are locked
          here: change them with a reviewed PR. You cannot change your own role.</p>
      </div>
      <div class="card">
        <h3>What each role can do</h3>
        <div class="tblwrap"><table class="tbl matrix" id="matrix"></table></div>
        <p class="note">Computed from the same checks that enforce it. Everyone signed in is an observer.</p>
      </div>
    </div>
  </section>

  <dialog id="confirm" aria-labelledby="cf-title">
    <h3 id="cf-title">Start this print?</h3>
    <dl class="rows" id="cf-rows"></dl>
    <div id="cf-warn"></div>
    <label for="cf-followers" id="cf-followers-label">Followers (you are included)</label>
    <div class="chips" id="cf-chips" hidden></div>
    <div class="picker">
      <input class="txt" type="text" id="cf-followers" placeholder="Search people by name or email"
             autocomplete="off">
      <ul class="pick-list" id="cf-list" role="listbox" hidden></ul>
    </div>
    <p class="note">Is the plate clear and the right filament loaded? Nothing here can check that for you.</p>
    <div class="actions">
      <button class="btn primary" type="button" id="cf-go">Start print</button>
      <button class="btn" type="button" id="cf-back">Not now</button>
    </div>
    <p class="status-line" id="cf-msg" role="status"></p>
  </dialog>

  <dialog class="viewer" id="cam-view" aria-labelledby="cv-title">
    <div class="vbar">
      <h3 id="cv-title">Camera</h3>
      <span class="badge idle" id="cv-badge"><span class="dot" aria-hidden="true"></span>…</span>
      <span class="zoom-level" id="cv-level">100%</span>
      <button class="btn sm" type="button" id="cv-out" aria-label="Zoom out">−</button>
      <button class="btn sm" type="button" id="cv-in" aria-label="Zoom in">+</button>
      <button class="btn sm" type="button" id="cv-fit">Fit</button>
      <button class="btn sm" type="button" id="cv-close">Close</button>
    </div>
    <div class="stage" id="cv-stage">
      <img id="cv-img" alt="Live view of the print bed, enlarged" draggable="false">
      <span class="msg" id="cv-msg" hidden></span>
    </div>
    <p class="hint">Click to zoom in on that spot, click again to fit. Scroll or pinch to zoom,
      drag to look around. Esc closes.</p>
  </dialog>


  <section aria-labelledby="h-temp">
    <h2 id="h-temp">Temperatures — reading, target, and the ceiling this server enforces</h2>
    <div class="grid g3" id="heaters"></div>
  </section>

  <section aria-labelledby="h-mach">
    <h2 id="h-mach">Machine</h2>
    <div class="grid g2">
      <div class="card">
        <h3>Motion</h3>
        <dl class="rows">
          <div class="row"><dt>Homed axes</dt><dd id="m-homed">—</dd></div>
          <div class="row"><dt>Position X Y Z</dt><dd id="m-pos">—</dd></div>
          <div class="row"><dt>Speed factor</dt><dd id="m-speed">—</dd></div>
          <div class="row"><dt>Flow factor</dt><dd id="m-flow">—</dd></div>
          <div class="row"><dt>Z offset</dt><dd id="m-zoff">—</dd></div>
          <div class="row"><dt>Filament sensor</dt><dd id="m-fil">—</dd></div>
        </dl>
      </div>
      <div class="card">
        <h3>AI failure detection</h3>
        <dl class="rows" id="ai-rows"></dl>
        <p class="note" id="ai-note"></p>
      </div>
      <div class="card" id="box-card" hidden>
        <h3>Filament Box</h3>
        <dl class="rows" id="box-rows"></dl>
        <p class="note" id="box-note"></p>
      </div>
      <div class="card">
        <h3>Who is driving</h3>
        <dl class="rows" id="lease-rows"></dl>
        <p class="note" id="lease-note"></p>
      </div>
    </div>
  </section>

  <footer>
    <span id="foot">Polling every 2s. Nothing on this page can change the printer.</span>
  </footer>
</div>

<script>
"use strict";
const $ = (id) => document.getElementById(id);
const txt = (id, v) => { const e = $(id); if (e) e.textContent = (v === null || v === undefined || v === "") ? "—" : String(v); };

// --- the fleet: one dashboard, several printers
//
// Every per-printer request names its printer (?printer=). The server refuses a
// request that does not when there are several, so a button can never act on a
// printer other than the one on screen.
const qs = new URLSearchParams(location.search);
let PRINTER = qs.get("printer") || localStorage.getItem("qidi-printer") || "";
// `id` is explicit wherever an action spans more than one request (upload ->
// prepare -> start, cancel -> confirm): it is the printer the action began on,
// never whichever printer happens to be on screen when a later request is sent.
const P = (path, id = PRINTER) => id
  ? path + (path.includes("?") ? "&" : "?") + "printer=" + encodeURIComponent(id) : path;
const LABELS = {};
const labelOf = (id) => LABELS[id] || id;

// --- theme: auto -> light -> dark, explicit choice wins over the OS in both directions
const THEMES = ["", "light", "dark"];
// ?theme=light|dark pins the theme, so a screenshot or a shared link renders the
// same way regardless of the viewer's OS setting.
const forced = new URLSearchParams(location.search).get("theme");
let ti = THEMES.indexOf(forced && THEMES.includes(forced) ? forced
  : (localStorage.getItem("qidi-theme") || ""));
if (ti < 0) ti = 0;
function applyTheme() {
  const t = THEMES[ti];
  document.documentElement.setAttribute("data-theme", t);
  localStorage.setItem("qidi-theme", t);
  $("theme").textContent = "theme: " + (t === "" ? "auto" : t);
}
$("theme").addEventListener("click", () => { ti = (ti + 1) % THEMES.length; applyTheme(); });
applyTheme();

const dur = (s) => {
  if (s === null || s === undefined) return "—";
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}m ${String(s % 60).padStart(2, "0")}s`;
};
const num = (v, d = 1) => (typeof v === "number" ? v.toFixed(d) : null);

// Place a tick label without letting it hang off either edge of the track.
function tickLabel(pct, label, extra) {
  if (pct >= 92) return `<span class="tick at-end ${extra}" style="right:0">${label}</span>`;
  if (pct <= 8) return `<span class="tick at-start ${extra}" style="left:0">${label}</span>`;
  return `<span class="tick ${extra}" style="left:${pct}%">${label}</span>`;
}

function badge(el, cls, label) {
  el.className = "badge " + cls;
  el.innerHTML = '<span class="dot" aria-hidden="true"></span>';
  el.appendChild(document.createTextNode(label));
}

// Heater cards are rebuilt from data so a machine without a chamber simply shows fewer.
function heaterCard(name, label, cur, tgt, ceiling, firmwareMax, confirmAbove) {
  const top = Math.max(ceiling || 0, firmwareMax || 0, cur || 0, tgt || 0, 1);
  const pct = (v) => Math.max(0, Math.min(100, ((v || 0) / top) * 100));
  const heating = (tgt || 0) > 0 && (cur || 0) < (tgt || 0) - 2;
  const at = (tgt || 0) > 0 && !heating;
  const state = heating ? ["warn", "heating"] : at ? ["live", "at target"] : ["idle", "off"];
  const card = document.createElement("div");
  card.className = "card";
  card.innerHTML =
    `<h3><span>${label}</span><span class="badge ${state[0]}">` +
    `<span class="dot" aria-hidden="true"></span>${state[1]}</span></h3>` +
    `<div class="big tabular">${num(cur) ?? "—"}<span class="unit">°C</span></div>` +
    `<div class="sub">target ${(tgt || 0) > 0 ? num(tgt, 0) + " °C" : "off"}</div>` +
    `<div class="meter"><div class="fill" style="width:${pct(cur)}%"></div></div>` +
    `<div class="ticks">` +
    ((tgt || 0) > 0 ? tickLabel(pct(tgt), "target", "tgt") : "") +
    (ceiling ? tickLabel(pct(ceiling), "cap " + num(ceiling, 0), "") : "") +
    `</div>` +
    (ceiling && firmwareMax && firmwareMax > ceiling
      ? `<p class="note">firmware allows ${num(firmwareMax, 0)} °C; this server stops at ` +
        `${num(ceiling, 0)} °C` +
        (confirmAbove ? `, and asks above ${num(confirmAbove, 0)} °C` : "") + `</p>`
      : "");
  return card;
}

// --- freshness: how old the numbers on screen are
//
// A failed poll leaves the last readings in place, which is right (a blank page
// helps nobody) but they must not pass for live. After STALE_MS without a good
// reading the readings dim and the header says how old they are.
const STALE_MS = 10000;
let lastGood = 0;
function freshness() {
  const host = $("host"), age = lastGood ? Date.now() - lastGood : null;
  const stale = age === null || age > STALE_MS;
  document.body.classList.toggle("stale", !!lastGood && stale);
  host.classList.toggle("stale-note", stale);
  host.textContent = age === null ? "waiting for the printer…"
    : !stale ? "live · updated just now"
    : `no reading for ${age < 60000 ? Math.round(age / 1000) + "s" : dur(age / 1000)} · showing the last one`;
}
setInterval(freshness, 1000);

async function tick() {
  let d;
  const asked = PRINTER;
  try {
    const r = await fetch(P("/api/state", asked), { cache: "no-store" });
    d = await r.json();
    badge($("conn"), "ok", "connected");
  } catch (e) {
    badge($("conn"), "crit", "no server");
    return;
  }
  // An answer about a printer that is no longer on screen is dropped, never drawn.
  if (asked !== PRINTER || (d.printer && PRINTER && d.printer.id !== PRINTER)) return;
  $("host").title = d.printer_url || "";
  if (d.printer && d.printer.label) {
    $("title").textContent = d.printer.label;
    document.title = d.printer.label + " — live";
  }

  const st = d.get_printer_status || {}, job = d.get_job_status || {};
  if (st.ok === false) {
    badge($("conn"), "crit", "printer unreachable");
    // Keep the last good readings on screen (dimmed and aged by freshness())
    // rather than overwrite them with blanks: "nozzle was 210 °C 20 s ago" is
    // useful, an empty page is not. Before any good reading, render the errors.
    if (lastGood) { freshness(); return; }
  } else lastGood = Date.now();
  freshness();

  // --- job
  const state = job.state || (st.job || {}).state || "unknown";
  const printing = state === "printing";
  jobState = state; serverReadOnly = d.read_only === true;
  badge($("job-state"), printing ? "live" : state === "error" ? "crit" :
        state === "paused" ? "warn" : state === "complete" ? "ok" : "idle", state);
  txt("job-file", job.filename || "no file loaded");
  const pct = job.progress_percent;
  $("job-pct").innerHTML = (num(pct) ?? "—") + '<span class="unit">%</span>';
  $("job-bar").style.width = (pct || 0) + "%";
  $("job-bar-outer").setAttribute("aria-valuenow", String(Math.round(pct || 0)));
  // Klipper keeps print_stats.info after a job, so an idle printer still reports the
  // last print's layers. Show the per-job numbers only for a job that is running or
  // has just ended with its file still loaded, never beside "no file loaded".
  const live = state === "printing" || state === "paused";
  const ended = ["complete", "cancelled", "error"].includes(state) && !!job.filename;
  const jobRows = live || ended;
  txt("job-layer", jobRows && job.current_layer != null
    ? `${job.current_layer} / ${job.total_layers ?? "?"}` : "—");
  txt("job-elapsed", jobRows && job.print_duration_s ? dur(job.print_duration_s) : "—");
  // Remaining, plus the wall-clock time it should be done: "1h 35m · done ~2:40 PM"
  // answers "should I wait or come back?" without arithmetic.
  if (live && job.estimated_remaining_s != null) {
    const done = new Date(Date.now() + job.estimated_remaining_s * 1000)
      .toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    txt("job-left", dur(job.estimated_remaining_s) + " · done ~" + done +
        (job.estimated_remaining_source === "slicer" ? " (slicer estimate)" : ""));
  } else txt("job-left", "—");
  txt("job-fil", jobRows && job.filament_used_mm ? (job.filament_used_mm / 1000).toFixed(2) + " m" : "—");

  // --- heaters, with the policy ceiling from get_safety_policy and firmware max from status
  const pol = d.get_safety_policy || {};
  const lim = pol.temperature_limits_c || {};   // what THIS server enforces
  const hw = pol.hardware_ceilings_c || {};     // what the firmware would allow
  const temps = st.temperatures || {};
  const host = $("heaters"); host.innerHTML = "";
  const order = [["extruder", "Nozzle"], ["bed", "Bed"], ["chamber", "Chamber"]];
  for (const [key, label] of order) {
    const t = temps[key];
    if (!t) continue;
    const c = lim[key] || {};
    host.appendChild(heaterCard(key, label, t.temperature, t.target,
      c.max ?? null, typeof hw[key] === "number" ? hw[key] : null, c.confirm_above ?? null));
  }
  if (!host.children.length) host.innerHTML = '<p class="err">No heater data.</p>';

  // --- motion
  const m = st.motion || {};
  txt("m-homed", m.homed_axes ? m.homed_axes.toUpperCase() : "none — not homed");
  txt("m-pos", Array.isArray(m.position)
    ? m.position.slice(0, 3).map((v) => (typeof v === "number" ? v.toFixed(1) : v)).join("  ") : "—");
  txt("m-speed", m.speed_factor_percent != null ? m.speed_factor_percent + " %" : "—");
  txt("m-flow", m.extrude_factor_percent != null ? m.extrude_factor_percent + " %" : "—");
  txt("m-zoff", m.z_offset != null ? m.z_offset.toFixed(3) + " mm" : "—");
  const f = st.filament || {};
  txt("m-fil", f.detected == null ? "—"
    : (f.detected ? "filament present" : "no filament") + (f.sensor_enabled ? "" : " (sensor off)"));

  // --- Who is driving
  //
  // In the header as well as in a card, because it is the first question a person
  // walking up to a shared printer actually has, and the answer decides whether
  // they should touch anything.
  const ls = d.get_printer_lease || {}, lrows = $("lease-rows"), lbadge = $("lease");
  lrows.innerHTML = "";
  const lrow = (k, v) => {
    const row = document.createElement("div"); row.className = "row";
    const dt = document.createElement("dt"); dt.textContent = k;
    const dd = document.createElement("dd"); dd.textContent = v;
    row.append(dt, dd); lrows.appendChild(row);
  };
  if (ls.coordination === "unavailable") {
    badge(lbadge, "warn", "no coordination");
    lrows.innerHTML = '<p class="err">This printer cannot store a lease.</p>';
    $("lease-note").textContent = ls.note || "";
  } else if (ls.held && ls.lease && ls.lease.manual) {
    // A print started at the touchscreen, from Fluidd or a slicer: a deployment
    // claimed the machine for it, so it reads as taken rather than free.
    badge(lbadge, "warn", "taken · manual print");
    lrow("Holder", "manual print at the machine");
    lrow("Why", ls.lease.reason || "");
    lrow("Until", "the print ends (~" + dur(ls.lease.seconds_remaining) + ")");
    $("lease-note").textContent =
      "Started on the printer, not through this app, so nobody here can pause or cancel it. " +
      "A maintainer can take it over; emergency stop always works.";
  } else if (ls.held && ls.lease) {
    const who = ls.lease.owner || "someone";
    badge(lbadge, ls.you_hold_it ? "ok" : "crit",
          (ls.you_hold_it ? "yours" : who) + " · " + dur(ls.lease.seconds_remaining));
    lrow("Holder", who + (ls.you_hold_it ? " (you)" : ""));
    if (ls.lease.reason) lrow("Reason", ls.lease.reason);
    // Durations, not "126.9 min": the same h/m formatter the job card uses.
    lrow("Expires in", dur(ls.lease.seconds_remaining));
    $("lease-note").textContent = ls.you_hold_it
      ? "You hold the printer. It is released when the time runs out, or release it when you are done."
      : `${who} is using the printer, so starting, pausing and cancelling are refused until ` +
        "they finish. Ask them first; emergency stop always works.";
  } else {
    badge(lbadge, "idle", "unclaimed");
    lrow("Holder", "nobody");
    $("lease-note").textContent = ls.required_for_print
      ? "Free. Whoever starts a print claims it first, from here or from an agent."
      : "Free.";
  }

  // --- Filament Box
  //
  // Hidden on a printer with no Box, which is every Max4 that has not had one fitted.
  // Shown for a Box with a fault too: "box_count says 1 but Klipper has no box_extras"
  // is exactly what someone needs to see when their new Box does nothing.
  const bx = d.get_box_status || {}, brows = $("box-rows"), bcard = $("box-card");
  const boxProblems = Array.isArray(bx.problems) ? bx.problems : [];
  bcard.hidden = !(bx.ok && (bx.detected || boxProblems.length));
  if (!bcard.hidden) {
    brows.innerHTML = "";
    const addRow = (k, node) => {
      const row = document.createElement("div"); row.className = "row";
      const dt = document.createElement("dt"); dt.textContent = k;
      const dd = document.createElement("dd");
      dd.appendChild(typeof node === "string" ? document.createTextNode(node) : node);
      row.append(dt, dd); brows.appendChild(row);
    };
    const bb = document.createElement("span");
    badge(bb, bx.ready ? "ok" : "warn", bx.ready ? "ready" : "attention");
    addRow("Status", bb);
    for (const s of (bx.slots || [])) {
      const cell = document.createElement("span");
      // The colour comes from QIDI's own colour table, never guessed; a value that is
      // not a plain #RRGGBB is ignored rather than written into a style.
      if (/^#[0-9a-fA-F]{6}$/.test(s.color_hex || "") && s.loaded) {
        const sw = document.createElement("span");
        sw.style.cssText = "display:inline-block;width:.8em;height:.8em;border-radius:2px;margin-right:.5em;border:1px solid var(--line)";
        sw.style.background = s.color_hex;
        cell.appendChild(sw);
      }
      const name = s.loaded
        ? (((s.filament && s.filament.name) || ("filament id " + s.filament_id)) +
           (s.color_name ? ", " + s.color_name : ""))
        : "empty";
      cell.appendChild(document.createTextNode(name + (s.in_extruder ? " \u00b7 in extruder" : "")));
      addRow("Unit " + s.unit + " \u00b7 slot " + s.position, cell);
    }
    $("box-note").textContent = boxProblems.length ? boxProblems[0] : (bx.note || "");
  }

  // --- AI detection
  const ai = d.get_ai_detection_status || {}, rows = $("ai-rows");
  rows.innerHTML = "";
  if (ai.ok && ai.detectors) {
    for (const [k, v] of Object.entries(ai.detectors)) {
      const row = document.createElement("div");
      row.className = "row";
      const dt = document.createElement("dt");
      dt.textContent = k === "noodle" ? "Spaghetti" : "Foreign object";
      const dd = document.createElement("dd");
      const b = document.createElement("span");
      badge(b, v.enabled ? "ok" : "warn", v.enabled ? "armed · " + (v.sensitivity || "") : "off");
      dd.appendChild(b);
      row.append(dt, dd); rows.appendChild(row);
    }
    $("ai-note").textContent = ai.note || "";
  } else {
    rows.innerHTML = '<p class="err">' + (ai.error ? String(ai.error) : "unavailable") + "</p>";
    $("ai-note").textContent = "";
  }
}


// --- controls -----------------------------------------------------------------
//
// Shown only to a signed-in caller whose role allows them. The local dashboard
// has no /api/whoami, so it never gets here and stays a window. None of this is
// the security boundary: each button is a tool call the server gates by role,
// lease and token, exactly as it gates an agent.
let me = null, jobState = "unknown", serverReadOnly = false, followView = null;
let camUrl = null, camDark = false, knownFiles = new Set(), pending = null;
const may = (k) => !!(me && me.may && me.may[k]) && !serverReadOnly;
const say = (id, text, bad) => {
  const e = $(id); e.textContent = text || ""; e.classList.toggle("err", !!bad);
};
const why = (out) => (out && (out.message || out.error)) || "Something went wrong.";

async function api(path, payload, printer = PRINTER) {
  let r;
  // Roles are app-wide; everything else is about one printer -- by default the
  // one on screen, or the one an action in progress began on.
  if (!path.startsWith("/api/admin/")) path = P(path, printer);
  try {
    r = await fetch(path, {
      method: "POST", cache: "no-store",
      // The header is what a cross-site form cannot send; the server refuses writes without it.
      headers: { "Content-Type": "application/json", "X-Qidi-Request": "dashboard" },
      body: JSON.stringify(payload || {}),
    });
  } catch (e) {
    return { ok: false, message: "Could not reach the server." };
  }
  try { return await r.json(); } catch (e) { return { ok: false, message: "HTTP " + r.status }; }
}

async function whoami() {
  try {
    const r = await fetch("/api/whoami", { cache: "no-store" });
    if (!r.ok) return;
    me = (await r.json()).caller || null;
  } catch (e) { return; }
  if (!me) return;
  badge($("ro"), me.role === "observer" ? "idle" : "ok", me.role);
  $("ro").title = "Signed in as " + me.email +
    (me.role === "observer" ? ". Ask to be added as an operator to print." : "");
  $("foot").textContent = me.role === "observer"
    ? "Polling every 2s. You can watch and follow prints; printing is granted per person."
    : "Polling every 2s. Every button runs the same gated tool an agent would, as you.";
  syncControls(); refreshFiles(); refreshFollowers(); refreshAdmin();
}

function syncControls() {
  const canPrint = may("print");
  const active = jobState === "printing" || jobState === "paused";
  $("print-section").hidden = !canPrint;
  $("job-actions").hidden = !(canPrint && active);
  $("btn-pause").hidden = jobState !== "printing";
  $("btn-resume").hidden = jobState !== "paused";
  $("btn-cancel").hidden = !active;
  $("follow").hidden = !(me && active);
  $("cam-actions").hidden = !(canPrint && camDark);
}

// --- camera
function camMessage(text) { const m = $("cam-msg"); m.hidden = !text; m.textContent = text || ""; }
// A frame is "live" only while new ones keep arriving. When fetches stop
// succeeding (network drop, app restart) the last frame stays on screen, but
// dimmed and labelled with its age, so a frozen picture never passes for live.
const CAM_STALE_MS = 8000;
let camAt = 0;
function camFreshness() {
  if (!camAt || $("cam-img").hidden) return;
  const age = Date.now() - camAt, frozen = age > CAM_STALE_MS;
  $("cam-img").classList.toggle("frozen", frozen);
  if (frozen) {
    badge($("cam-badge"), "warn", "frozen");
    camMessage(`No new frame for ${age < 60000 ? Math.round(age / 1000) + "s" : dur(age / 1000)}: ` +
               "this picture is from then, not now.");
  }
  camMirror();
}
setInterval(camFreshness, 1000);

// The enlarged view shows the card's frame and the card's verdict, so a frozen or
// dark picture is labelled there exactly as it is on the card.
function camMirror() {
  const img = $("cam-img"), vi = $("cv-img"), m = $("cam-msg"), vm = $("cv-msg");
  const shown = !img.hidden && !!img.getAttribute("src");
  $("cam-open").hidden = !shown;
  if ($("cam-view").open && ($("cam-card").hidden || !shown)) $("cam-view").close();
  $("cv-badge").className = $("cam-badge").className;
  $("cv-badge").innerHTML = $("cam-badge").innerHTML;
  vm.hidden = m.hidden; vm.textContent = m.textContent;
  vi.classList.toggle("frozen", img.classList.contains("frozen"));
}

async function refreshCamera() { await fetchCamera(); camMirror(); }

async function fetchCamera() {
  if (document.hidden) return;
  let r;
  const asked = PRINTER;
  try { r = await fetch(P("/api/camera.jpg", asked), { cache: "no-store" }); } catch (e) { camFreshness(); return; }
  if (asked !== PRINTER) return;    // never show one printer's picture under another's name
  if (r.status === 404) { $("cam-card").hidden = true; return; }
  $("cam-card").hidden = false;
  if (!r.ok) {
    let info = {};
    try { info = await r.json(); } catch (e) { /* not JSON: keep the generic message */ }
    badge($("cam-badge"), "warn", "no camera");
    $("cam-img").hidden = true;
    camMessage(info.message || "The printer's camera did not answer.");
    // The addresses and errors are for whoever can get them fixed.
    const det = $("cam-detail"), attempts = info.attempts || [];
    det.hidden = !(me && me.role === "maintainer" && attempts.length);
    const ul = det.querySelector("ul"); ul.innerHTML = "";
    for (const a of attempts) {
      const li = document.createElement("li"); li.textContent = `${a.url}: ${a.error}`; ul.appendChild(li);
    }
    return;
  }
  $("cam-detail").hidden = true;
  $("cam-img").hidden = false;
  camDark = r.headers.get("X-Qidi-Frame-Dark") === "1";
  const url = URL.createObjectURL(await r.blob());
  $("cam-img").src = url;
  $("cv-img").src = url;            // both pictures move off the old URL before it is revoked
  if (camUrl) URL.revokeObjectURL(camUrl);
  camUrl = url;
  camAt = Date.now();
  $("cam-img").classList.remove("frozen");
  badge($("cam-badge"), camDark ? "warn" : "live", camDark ? "dark" : "live");
  camMessage(camDark ? "This frame is dark: the case light is off, so it cannot show the bed." : "");
  syncControls();
}
$("btn-light").addEventListener("click", async () => {
  const out = await api("/api/action", { tool: "set_case_light", args: { on: true } });
  if (!out.ok) say("job-msg", why(out), true);
  else setTimeout(refreshCamera, 1500);
});

// --- camera viewer: click zooms in on that spot and clicks again to fit; the wheel
// and a pinch zoom around the pointer; dragging pans while zoomed in.
const ZOOM_MAX = 6, ZOOM_CLICK = 2.5, ZOOM_BUTTON = 1.5;
let zs = 1, zx = 0, zy = 0;         // scale, and the picture's offset in stage pixels
// Panning stops at the picture's own edges, not the stage's: the frame is letterboxed
// (object-fit: contain), and nobody wants to drag into the black bars. An axis the
// picture does not fill stays centred.
function clampAxis(t, stageLen, picLen) {
  const pad = (stageLen - picLen) / 2, len = picLen * zs;
  if (len <= stageLen) return (stageLen - len) / 2 - pad * zs;
  return Math.min(-pad * zs, Math.max(stageLen - (pad + picLen) * zs, t));
}
function zoomApply() {
  const st = $("cv-stage"), w = st.clientWidth, h = st.clientHeight, img = $("cv-img");
  zs = Math.min(ZOOM_MAX, Math.max(1, zs));
  const fit = img.naturalWidth && img.naturalHeight
    ? Math.min(w / img.naturalWidth, h / img.naturalHeight) : 0;
  zx = clampAxis(zx, w, fit ? img.naturalWidth * fit : w);
  zy = clampAxis(zy, h, fit ? img.naturalHeight * fit : h);
  $("cv-img").style.transform = `translate(${zx}px, ${zy}px) scale(${zs})`;
  st.classList.toggle("zoomed", zs > 1);
  $("cv-level").textContent = Math.round(zs * 100) + "%";
  $("cv-out").disabled = $("cv-fit").disabled = zs <= 1;
  $("cv-in").disabled = zs >= ZOOM_MAX;
}
// Zoom to `scale` keeping the point (px, py) of the stage under the pointer.
function zoomAt(scale, px, py) {
  const next = Math.min(ZOOM_MAX, Math.max(1, scale));
  zx = px - (px - zx) * (next / zs);
  zy = py - (py - zy) * (next / zs);
  zs = next;
  zoomApply();
}
function zoomCentre(scale) { const st = $("cv-stage"); zoomAt(scale, st.clientWidth / 2, st.clientHeight / 2); }
function stagePoint(e) { const r = $("cv-stage").getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; }

function openViewer() {
  if ($("cam-img").hidden || !$("cam-img").getAttribute("src")) return;
  $("cv-title").textContent = $("title").textContent + " · camera";
  zs = 1; zx = 0; zy = 0;
  camMirror();
  $("cam-view").showModal();
  zoomApply();
}
$("cam-img").addEventListener("click", openViewer);
$("cam-open").addEventListener("click", openViewer);
$("cv-close").addEventListener("click", () => $("cam-view").close());
$("cv-in").addEventListener("click", () => zoomCentre(zs * ZOOM_BUTTON));
$("cv-out").addEventListener("click", () => zoomCentre(zs / ZOOM_BUTTON));
$("cv-fit").addEventListener("click", () => zoomCentre(1));
$("cam-view").addEventListener("keydown", (e) => {
  if (e.key === "+" || e.key === "=") zoomCentre(zs * ZOOM_BUTTON);
  else if (e.key === "-") zoomCentre(zs / ZOOM_BUTTON);
  else if (e.key === "0") zoomCentre(1);
  else return;
  e.preventDefault();
});
window.addEventListener("resize", () => { if ($("cam-view").open) zoomApply(); });

const stage = $("cv-stage"), ptrs = new Map();   // pointerId -> last [x, y] on the stage
let downAt = null, moved = false, pinchGap = 0;
function gap() { const [a, b] = [...ptrs.values()]; return Math.hypot(a[0] - b[0], a[1] - b[1]); }
stage.addEventListener("pointerdown", (e) => {
  if (e.button !== 0) return;
  stage.setPointerCapture(e.pointerId);
  if (!ptrs.size) { downAt = stagePoint(e); moved = false; }
  ptrs.set(e.pointerId, stagePoint(e));
  if (ptrs.size === 2) { pinchGap = gap(); moved = true; }
});
stage.addEventListener("pointermove", (e) => {
  if (!ptrs.has(e.pointerId)) return;
  const [px, py] = stagePoint(e), [ox, oy] = ptrs.get(e.pointerId);
  ptrs.set(e.pointerId, [px, py]);
  if (ptrs.size === 2) {
    const g = gap(), [a, b] = [...ptrs.values()];
    if (pinchGap) zoomAt(zs * g / pinchGap, (a[0] + b[0]) / 2, (a[1] + b[1]) / 2);
    pinchGap = g;
    return;
  }
  // A few pixels of wobble is still a click, not a drag.
  if (!moved && Math.hypot(px - downAt[0], py - downAt[1]) > 4) moved = true;
  if (moved && zs > 1) {
    stage.classList.add("dragging");
    zx += px - ox; zy += py - oy; zoomApply();
  }
});
function pointerEnd(e) {
  if (!ptrs.delete(e.pointerId)) return;
  if (ptrs.size < 2) pinchGap = 0;
  if (ptrs.size) return;
  stage.classList.remove("dragging");
  if (e.type !== "pointerup" || moved) return;
  if (zs > 1) zoomCentre(1);
  else { const [px, py] = stagePoint(e); zoomAt(ZOOM_CLICK, px, py); }
}
stage.addEventListener("pointerup", pointerEnd);
stage.addEventListener("pointercancel", pointerEnd);
stage.addEventListener("wheel", (e) => {
  e.preventDefault();
  const [px, py] = stagePoint(e);
  zoomAt(zs * Math.exp(-e.deltaY * 0.0015), px, py);
}, { passive: false });

// --- files and upload
async function refreshFiles() {
  if (!may("print")) return;
  let out;
  const asked = PRINTER;
  try { out = await (await fetch(P("/api/files", asked), { cache: "no-store" })).json(); } catch (e) { return; }
  if (asked !== PRINTER) return;
  const files = out.files || [];
  knownFiles = new Set(files.map((f) => f.path));
  const ul = $("files"); ul.innerHTML = "";
  for (const f of files.slice(0, 12)) {
    const li = document.createElement("li");
    const n = document.createElement("span"); n.className = "name"; n.textContent = f.path;
    const m = document.createElement("span"); m.className = "meta";
    m.textContent = [f.estimated_time_s ? dur(f.estimated_time_s) : null, f.filament_type]
      .filter(Boolean).join(" · ");
    const b = document.createElement("button");
    b.className = "btn"; b.type = "button"; b.textContent = "Print…";
    // Every row's button reads "Print…"; the accessible name says which file.
    b.setAttribute("aria-label", "Print " + f.path);
    b.addEventListener("click", () => prepare(f.path));
    li.append(n, m, b); ul.appendChild(li);
  }
  if (!ul.children.length) {
    const li = document.createElement("li"); li.className = "meta";
    li.textContent = out.ok === false ? why(out) : "No files on the printer yet.";
    ul.appendChild(li);
  }
}

// Uploads never overwrite (that is maintainer work), so a clashing name gets a suffix.
// ".gcode.3mf" stays together: it is the format QIDI's own slicer writes.
function freeName(name) {
  if (!knownFiles.has(name)) return name;
  const m = name.match(/^(.*?)((?:\.gcode)?\.[^.]+)$/i);
  const stem = m ? m[1] : name, ext = m ? m[2] : "";
  for (let i = 2; i < 1000; i++) {
    const c = `${stem}-${i}${ext}`;
    if (!knownFiles.has(c)) return c;
  }
  return name;
}

function upload(file, attempt = 0, target = PRINTER) {
  if (!may("print") || !file) return;
  const name = freeName(file.name.replace(/[\\/]/g, "_"));
  $("up-prog").hidden = false; $("up-bar").style.width = "0%";
  say("print-msg", name === file.name ? `Uploading ${name}…`
    : `${file.name} is already on the printer, so this copy is uploading as ${name}…`);
  const xhr = new XMLHttpRequest();
  xhr.open("POST", P("/api/upload?filename=" + encodeURIComponent(name), target));
  xhr.setRequestHeader("X-Qidi-Request", "dashboard");
  xhr.setRequestHeader("Content-Type", "application/octet-stream");
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) $("up-bar").style.width = (100 * e.loaded / e.total) + "%";
  };
  xhr.onload = () => {
    $("up-prog").hidden = true;
    let out;
    try { out = JSON.parse(xhr.responseText); } catch (e) { out = { ok: false, message: "HTTP " + xhr.status }; }
    if (!out.ok && /already exists/.test(out.message || "") && attempt < 3) {
      knownFiles.add(name); upload(file, attempt + 1, target); return;   // older than the list showed
    }
    if (!out.ok) { say("print-msg", why(out), true); return; }
    if (target !== PRINTER) {
      // The person switched printers while it uploaded. Do not open a start dialog
      // for the printer now on screen, which may hold a different file by that name.
      say("print-msg", `Uploaded ${name} to ${labelOf(target)}. Switch back to it to start the print.`);
      return;
    }
    say("print-msg", `Uploaded ${name}.`);
    refreshFiles(); prepare(name, target);
  };
  xhr.onerror = () => { $("up-prog").hidden = true; say("print-msg", "Upload failed: the connection dropped.", true); };
  xhr.send(file);
}
const drop = $("drop");
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => {
  e.preventDefault(); drop.classList.add("over");
}));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, () => drop.classList.remove("over")));
drop.addEventListener("drop", (e) => { e.preventDefault(); upload(e.dataTransfer.files[0]); });
$("file-input").addEventListener("change", (e) => { upload(e.target.files[0]); e.target.value = ""; });

// --- starting a print: claim, review the ticket, confirm
async function prepare(filename, target = PRINTER) {
  say("print-msg", `Checking ${filename}…`);
  const t = await api("/api/print/prepare", { filename }, target);
  const lease = !!t.lease_acquired;
  if (target !== PRINTER) {
    // Switched away while the ticket was being issued: give back any claim it took.
    if (lease) await api("/api/action", { tool: "release_printer_lease", args: {} }, target);
    say("print-msg", ""); return;
  }
  if (!t.confirm_token) { say("print-msg", why(t), true); return; }
  say("print-msg", "");
  pending = { filename, token: t.confirm_token, leaseAcquired: lease, printer: target };
  const a = t.about_to_print || {};
  // Name the printer: two printers can hold files with the same name.
  $("cf-title").textContent = "Start " + filename + (Object.keys(LABELS).length > 1
    ? " on " + labelOf(target) : "") + "?";
  // Say what following will actually do. Until chat is connected the list is kept
  // but nobody is pinged, and a label promising a ping would be a broken promise.
  await refreshFollowers();
  const fv = followView || {};
  $("cf-followers-label").textContent = fv.slack_enabled
    ? `@mention these people in ${fv.channel || "chat"} when it ends (you are included)`
    : "Followers (you are included). Chat is not connected yet, so the list is kept but nobody is pinged when it ends.";
  const rows = $("cf-rows"); rows.innerHTML = "";
  const add = (k, v) => {
    if (v === null || v === undefined || v === "") return;
    const row = document.createElement("div"); row.className = "row";
    const dt = document.createElement("dt"); dt.textContent = k;
    const dd = document.createElement("dd"); dd.textContent = v;
    row.append(dt, dd); rows.appendChild(row);
  };
  add("Estimated time", a.estimated_time_s ? dur(a.estimated_time_s) : null);
  add("Filament", [a.filament_type,
    typeof a.filament_weight_g === "number" ? a.filament_weight_g.toFixed(1) + " g" : null]
    .filter(Boolean).join(" · "));
  add("Nozzle / bed", a.first_layer_extruder_temp_c || a.first_layer_bed_temp_c
    ? `${a.first_layer_extruder_temp_c ?? "?"} °C / ${a.first_layer_bed_temp_c ?? "?"} °C` : null);
  add("Chamber", a.chamber_temp_c ? a.chamber_temp_c + " °C" : null);
  add("Layer height", a.layer_height_mm ? a.layer_height_mm + " mm" : null);
  add("Sliced with", a.slicer);
  const warn = $("cf-warn"); warn.innerHTML = "";
  const fit = t.box_fit;
  if (fit && (fit.verdict === "mismatch" || fit.verdict === "check")) {
    const w = document.createElement("div"); w.className = "warn-box";
    w.textContent = "Filament Box: " + (fit.summary || "the loaded filament may not match this file.");
    warn.appendChild(w);
  }
  $("cf-followers").value = ""; cfPicked = []; renderChips();
  say("cf-msg", "");
  $("cf-go").disabled = false;
  $("confirm").showModal();
}
async function abandon() {
  const p = pending; pending = null;
  if ($("confirm").open) $("confirm").close();
  // Do not leave a claim behind for a print that never started.
  if (p && p.leaseAcquired) await api("/api/action", { tool: "release_printer_lease", args: {} }, p.printer);
}
$("cf-back").addEventListener("click", abandon);
$("confirm").addEventListener("cancel", (e) => { e.preventDefault(); abandon(); });
$("cf-go").addEventListener("click", async () => {
  if (!pending) return;
  $("cf-go").disabled = true;
  const followers = cfPicked.map((p) => p.email);
  const out = await api("/api/print/start",
    { filename: pending.filename, confirm_token: pending.token, followers }, pending.printer);
  if (!out.ok) { say("cf-msg", why(out), true); $("cf-go").disabled = false; return; }
  const name = pending.filename; pending = null; $("confirm").close();
  say("print-msg", `Started ${name}. ` + (out.slack
    ? `It is posted in ${out.channel || "chat"}, and followers are @mentioned when it ends.`
    : "Chat is not connected yet, so nothing is posted."));
  tick().then(syncControls); refreshFollowers();
});

// --- pause / resume / cancel
async function act(tool, args, done, target = PRINTER) {
  const out = await api("/api/action", { tool, args }, target);
  say("job-msg", out.ok ? done : why(out), !out.ok);
  tick().then(syncControls);
  return out;
}
$("btn-pause").addEventListener("click", () => act("pause_print", { confirm: true }, "Paused."));
$("btn-resume").addEventListener("click", () => act("resume_print", { confirm: true }, "Resumed."));
$("btn-cancel").addEventListener("click", async () => {
  const target = PRINTER;
  const t = await api("/api/action", { tool: "cancel_print", args: {} }, target);
  if (!t.confirm_token) { say("job-msg", why(t), true); return; }
  const a = t.about_to_cancel || {};
  const where = Object.keys(LABELS).length > 1 ? ` on ${labelOf(target)}` : "";
  const ok = window.confirm(`Cancel ${a.filename || "this print"}${where}? It is ${a.progress_percent ?? "?"}% done, ` +
    "and a cancelled print cannot be resumed.");
  if (ok) act("cancel_print", { confirm_token: t.confirm_token }, "Cancelled.", target);
});

// --- followers
async function refreshFollowers() {
  if (!me) return;
  let v;
  const asked = PRINTER;
  try { v = await (await fetch(P("/api/followers", asked), { cache: "no-store" })).json(); } catch (e) { return; }
  if (asked !== PRINTER) return;
  followView = v;
  const names = v.names || {};
  const fl = $("follow-list"); fl.innerHTML = "";
  (v.followers || []).forEach((email, i) => {
    const s = document.createElement("span");
    s.textContent = (names[email] || email) + (i < v.followers.length - 1 ? ", " : "");
    s.title = email; fl.appendChild(s);
  });
  if (!fl.childNodes.length) fl.textContent = "nobody yet";
  $("btn-follow").textContent = v.you_follow ? "Unfollow" : "Follow this print";
  $("follow-picker").hidden = !may("print");
  const notes = [v.slack_enabled
    ? `Prints are posted in ${v.channel || "chat"}; followers are @mentioned when this one ends.`
    : "Chat is not connected yet: the list is kept, but nothing is posted."];
  if (v.always) notes.push(`${v.always} more ${v.always === 1 ? "person is" : "people are"} mentioned on every print.`);
  $("follow-note").textContent = notes.join(" ");
}
$("btn-follow").addEventListener("click", async () => {
  const out = await api("/api/followers",
    { action: followView && followView.you_follow ? "unfollow" : "follow" });
  if (!out.ok) say("job-msg", why(out), true);
  refreshFollowers();
});
// --- people picker: a typeahead over a people directory
//
// Typing an address from memory is how a follower list gets a typo nobody notices
// until the part is done. So the field searches a directory (through /api/people) and
// keeps the address the directory returned. A full address the directory does not
// know is still offered, so a missing directory never blocks a print.
const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const initials = (s) => (s || "?").split(/[\s@._-]+/).filter(Boolean).slice(0, 2)
  .map((w) => w[0].toUpperCase()).join("");

function peoplePicker(input, list, onPick) {
  let items = [], active = -1, timer = null, seq = 0, directoryDown = false;
  const close = () => {
    list.hidden = true; active = -1;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
  };
  const render = () => {
    list.innerHTML = "";
    items.forEach((p, i) => {
      const li = document.createElement("li");
      li.id = `${list.id}-${i}`; li.className = "pick" + (i === active ? " active" : "");
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", i === active ? "true" : "false");
      const av = document.createElement("span"); av.className = "av"; av.setAttribute("aria-hidden", "true");
      av.textContent = initials(p.name || p.email);
      const who = document.createElement("span"); who.className = "who";
      const n = document.createElement("strong"); n.textContent = p.name || p.email;
      const sub = document.createElement("span");
      sub.textContent = p.note || [p.name ? p.email : null, p.title, p.team].filter(Boolean).join(" · ");
      who.append(n, sub); li.append(av, who);
      // mousedown, not click: click fires after the input's blur has closed the list.
      li.addEventListener("mousedown", (e) => { e.preventDefault(); choose(i); });
      list.appendChild(li);
    });
    if (!items.length && input.value.trim().length >= 2) {
      const li = document.createElement("li"); li.className = "pick empty";
      // "No one found" when the directory is not even connected sends people
      // hunting for a typo that is not there. Say which it is.
      li.textContent = directoryDown
        ? "Name search is not connected yet. Type their full email address (name@example.com) to add them."
        : "No one found. Type their full email address to add it.";
      list.appendChild(li);
    }
    const open = list.children.length > 0;
    list.hidden = !open;
    input.setAttribute("aria-expanded", String(open));
    if (active >= 0) input.setAttribute("aria-activedescendant", `${list.id}-${active}`);
    else input.removeAttribute("aria-activedescendant");
  };
  const choose = (i) => {
    const p = items[i];
    if (!p) return;
    onPick({ email: p.email, name: p.name || "" });
    input.value = ""; items = []; close();
  };
  async function search(q) {
    const mine = ++seq;
    let out;
    try { out = await (await fetch("/api/people?q=" + encodeURIComponent(q), { cache: "no-store" })).json(); }
    catch (e) { out = { people: [] }; }
    if (mine !== seq) return;   // a newer keystroke already asked
    directoryDown = out.ok === false;
    items = (out.people || []).slice(0, 8);
    if (EMAIL.test(q) && !items.some((p) => p.email === q.toLowerCase())) {
      items.push({ email: q.toLowerCase(), name: "", note: out.ok === false
        ? "add this address (directory unavailable)" : "add this address" });
    }
    active = items.length ? 0 : -1;
    render();
  }
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-controls", list.id);
  input.setAttribute("aria-expanded", "false");
  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { seq++; items = []; close(); return; }
    timer = setTimeout(() => search(q), 180);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" && items.length) {
      active = (active + 1) % items.length; render(); e.preventDefault();
    } else if (e.key === "ArrowUp" && items.length) {
      active = (active - 1 + items.length) % items.length; render(); e.preventDefault();
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (active >= 0) choose(active);
    } else if (e.key === "Escape" && !list.hidden) {
      // Close the list, not the dialog around it.
      close(); e.preventDefault(); e.stopPropagation();
    }
  });
  input.addEventListener("blur", () => setTimeout(close, 150));
}

// Chosen followers in the start dialog, shown as removable chips.
let cfPicked = [];
function renderChips() {
  const box = $("cf-chips"); box.innerHTML = "";
  for (const p of cfPicked) {
    const chip = document.createElement("span"); chip.className = "chip";
    chip.title = p.email;
    chip.appendChild(document.createTextNode(p.name || p.email));
    const x = document.createElement("button"); x.type = "button"; x.textContent = "×";
    x.setAttribute("aria-label", "Remove " + (p.name || p.email));
    x.addEventListener("click", () => { cfPicked = cfPicked.filter((q) => q.email !== p.email); renderChips(); });
    chip.appendChild(x); box.appendChild(chip);
  }
  box.hidden = !cfPicked.length;
}
peoplePicker($("cf-followers"), $("cf-list"), (p) => {
  if (me && p.email === me.email.toLowerCase()) return;   // the starter is always included
  if (!cfPicked.some((q) => q.email === p.email)) cfPicked.push(p);
  renderChips();
});
peoplePicker($("follow-email"), $("follow-list-pick"), async (p) => {
  const out = await api("/api/followers", { action: "follow", email: p.email });
  say("job-msg", out.ok ? `${p.name || p.email} will be pinged when it ends.` : why(out), !out.ok);
  refreshFollowers();
});
// --- users and roles (maintainers)
//
// Shown only to maintainers, and only presentation: the server refuses anyone
// else, and refuses changes to people listed in the server configuration.
let adminPick = null;
async function refreshAdmin() {
  const sec = $("admin-section");
  if (!(me && me.role === "maintainer")) { sec.hidden = true; return; }
  let out;
  try { out = await (await fetch("/api/admin/roles", { cache: "no-store" })).json(); } catch (e) { return; }
  if (!out.ok) { sec.hidden = true; return; }
  sec.hidden = false;
  renderRoster(out.users || []);
  renderMatrix(out.matrix || []);
}
const cell = (tag, text, cls) => {
  const e = document.createElement(tag);
  if (text !== undefined && text !== null) e.textContent = text;
  if (cls) e.className = cls;
  return e;
};
function renderRoster(users) {
  const t = $("roster"); t.innerHTML = "";
  const head = document.createElement("tr");
  ["Person", "Role", "Where it comes from", ""].forEach((h) => head.appendChild(cell("th", h)));
  t.appendChild(head);
  for (const p of users) {
    const tr = document.createElement("tr");
    const person = cell("td", null, "who");
    person.appendChild(cell("strong", p.name || p.email));
    if (p.name) person.appendChild(cell("small", p.email));
    const role = cell("td");
    const b = document.createElement("span");
    badge(b, p.role === "maintainer" ? "live" : "ok", p.role);
    role.appendChild(b);
    const when = p.granted_at ? new Date(p.granted_at * 1000).toLocaleDateString() : "";
    const source = cell("td", p.dashboard_role
      ? `dashboard, by ${p.granted_by || "?"}${when ? " on " + when : ""}` +
        (p.manifest_role ? ` (manifest: ${p.manifest_role})` : "")
      : "server configuration (change by PR)", "src");
    const acts = cell("td", null, "acts");
    const label = p.name || p.email;
    const isMe = me && p.email === me.email.toLowerCase();
    const button = (text, role, cls) => {
      const btn = cell("button", text, "btn sm" + (cls ? " " + cls : ""));
      btn.type = "button";
      // "Revoke" alone, once per row, tells a screen reader nothing about whom.
      btn.setAttribute("aria-label", `${text}: ${label}`);
      btn.addEventListener("click", () => setRole(p.email, role, label));
      acts.appendChild(btn);
    };
    if (isMe) acts.appendChild(cell("span", "you", "src"));
    else {
      if (p.role === "operator") button("Make maintainer", "maintainer");
      if (p.role === "maintainer" && p.dashboard_role && p.manifest_role !== "maintainer") {
        button("Make operator", p.manifest_role === "operator" ? "none" : "operator");
      }
      if (p.dashboard_role) button("Revoke", "none", "danger");
      if (!p.dashboard_role && p.role === "maintainer") acts.appendChild(cell("span", "locked", "src"));
    }
    tr.append(person, role, source, acts);
    t.appendChild(tr);
  }
  if (!users.length) {
    const tr = document.createElement("tr");
    const td = cell("td", "Nobody yet: everyone signed in is an observer.", "src");
    td.colSpan = 4; tr.appendChild(td); t.appendChild(tr);
  }
}
function renderMatrix(rows) {
  const t = $("matrix"); t.innerHTML = "";
  const head = document.createElement("tr");
  head.appendChild(cell("th", "Can"));
  // Full role names on a wide screen, short ones on a phone (CSS picks); the full
  // name stays the accessible name either way.
  for (const [long, short] of [["Observer", "Obs."], ["Operator", "Op."], ["Maintainer", "Maint."]]) {
    const th = cell("th");
    th.title = long; th.setAttribute("aria-label", long);
    th.append(cell("span", long, "long"), cell("span", short, "short"));
    head.appendChild(th);
  }
  t.appendChild(head);
  for (const r of rows) {
    const tr = document.createElement("tr");
    tr.appendChild(cell("td", r.label));
    for (const role of ["observer", "operator", "maintainer"]) {
      const yes = !!(r.may || {})[role];
      const td = cell("td", yes ? "✓" : "—", yes ? "yes" : "no");
      td.setAttribute("aria-label", yes ? "yes" : "no");
      tr.appendChild(td);
    }
    t.appendChild(tr);
  }
}
async function setRole(email, role, label) {
  if (role === "maintainer" && !window.confirm(
    `Make ${label} a maintainer? Maintainers can delete files, restart the printer, ` +
    "take over prints and change everyone's roles.")) return;
  if (role === "none" && !window.confirm(`Revoke ${label}'s dashboard role?`)) return;
  const out = await api("/api/admin/roles", { email, role });
  if (!out.ok) { say("admin-msg", why(out), true); return; }
  const ch = out.change || {};
  const as = { observer: "an observer", operator: "an operator", maintainer: "a maintainer" };
  say("admin-msg", ch.changed ? `${label} is now ${as[ch.role] || ch.role}.` : "Nothing to change.");
  renderRoster(out.users || []);
}
function showAdminPick() {
  const box = $("admin-picked"); box.innerHTML = "";
  box.hidden = !adminPick;
  $("admin-grant").disabled = !adminPick;
  if (!adminPick) return;
  const chip = cell("span", null, "chip");
  chip.title = adminPick.email;
  chip.appendChild(document.createTextNode(adminPick.name || adminPick.email));
  const x = cell("button", "×"); x.type = "button";
  x.setAttribute("aria-label", "Clear");
  x.addEventListener("click", () => { adminPick = null; showAdminPick(); });
  chip.appendChild(x); box.appendChild(chip);
}
peoplePicker($("admin-person"), $("admin-person-list"), (p) => { adminPick = p; showAdminPick(); });
$("admin-grant").addEventListener("click", async () => {
  if (!adminPick) return;
  await setRole(adminPick.email, $("admin-role").value, adminPick.name || adminPick.email);
  adminPick = null; showAdminPick();
});
// --- the job queue: what is lined up, and whether it advances by itself
async function refreshQueue() {
  let r;
  const asked = PRINTER;
  try { r = await fetch(P("/api/queue", asked), { cache: "no-store" }); } catch (e) { return; }
  if (asked !== PRINTER) return;
  const card = $("queue-card");
  if (r.status === 404) { card.hidden = true; return; }      // the local dashboard
  let q;
  try { q = await r.json(); } catch (e) { return; }
  if (q.ok === false) {
    // No job_queue component on this printer: nothing to show, not an error.
    card.hidden = q.error === "not_supported";
    $("queue-list").innerHTML = "";
    $("queue-total").textContent = "";
    $("queue-note").textContent = q.error === "not_supported" ? "" : why(q);
    return;
  }
  card.hidden = false;
  const jobs = q.queued_jobs || [];
  const auto = !!(q.advance && q.advance.automatic_transition);
  badge($("queue-badge"), !jobs.length ? "idle" : auto ? "warn" : "ok",
        !jobs.length ? "empty" : `${jobs.length} queued`);
  const ol = $("queue-list"); ol.innerHTML = "";
  for (const j of jobs) {
    const li = document.createElement("li");
    const n = document.createElement("span"); n.className = "name"; n.textContent = j.filename;
    li.appendChild(n);
    const bits = [j.estimated_time_h ? dur(j.estimated_time_h * 3600) : null, j.filament_type].filter(Boolean);
    if (bits.length) {
      const m = document.createElement("div"); m.className = "meta"; m.textContent = bits.join(" · ");
      li.appendChild(m);
    }
    if (j.box_fit && j.box_fit.verdict && j.box_fit.verdict !== "ok") {
      const w = document.createElement("div"); w.className = "warn-fit";
      w.textContent = "Filament Box: " + (j.box_fit.summary || j.box_fit.verdict);
      li.appendChild(w);
    }
    ol.appendChild(li);
  }
  $("queue-total").textContent = jobs.length && q.total_estimated_time_h
    ? "about " + dur(q.total_estimated_time_h * 3600) + " in total" : "";
  $("queue-note").textContent = !jobs.length
    ? "Nothing queued. Line prints up from an agent with queue_jobs, e.g. \u201cqueue benchy and the bracket\u201d."
    : auto
      ? "Starts the next print by itself the moment one ends, on top of whatever is still on " +
        "the plate. Make sure someone will be there to clear it, or pause the queue."
      : "Waits after each job until someone clears the plate and starts the queue.";
  // A queue that advances by itself can print onto a finished part: that is a
  // hazard to the machine, not a footnote, so it gets the warning box.
  $("queue-note").className = jobs.length && auto ? "warn-box" : "note";
  $("queue-note").setAttribute("role", jobs.length && auto ? "alert" : "status");
  // Operators can hold a live queue from here. Starting it stays with agents.
  const live = (q.queue_state || "").toLowerCase() === "ready";
  $("queue-actions").hidden = !(may("print") && jobs.length && live);
}
$("btn-queue-pause").addEventListener("click", async () => {
  const out = await api("/api/action", { tool: "control_job_queue", args: { action: "pause", confirm: true } });
  say("queue-msg", out.ok ? "Queue paused: nothing starts until it is released." : why(out), !out.ok);
  refreshQueue();
});
refreshQueue();
setInterval(refreshQueue, 10000);
let fleetBusy = false;
async function refreshFleet() {
  if (fleetBusy) return;          // never stack polls behind a slow printer
  fleetBusy = true;
  let f;
  try { f = await (await fetch("/api/printers", { cache: "no-store" })).json(); }
  catch (e) { return; } finally { fleetBusy = false; }
  if (!f.ok) return;
  const list = f.printers || [];
  for (const p of list) LABELS[p.id] = p.label;
  const known = list.map((p) => p.id);
  if (!known.includes(PRINTER)) { choosePrinter(known[0] || "", false); }
  const nav = $("fleet");
  nav.hidden = !f.multiple;
  if (!f.multiple) return;
  nav.innerHTML = "";
  for (const p of list) {
    const b = document.createElement("button");
    b.type = "button"; b.className = "fleet-btn" + (p.id === PRINTER ? " on" : "");
    b.setAttribute("aria-pressed", String(p.id === PRINTER));
    const name = document.createElement("strong"); name.textContent = p.label;
    const st = document.createElement("span");
    badge(st, !p.reachable ? "crit" : p.state === "printing" ? "live" : p.state === "paused" ? "warn" : "idle",
          !p.reachable ? "offline" : (p.state || "unknown") +
          (p.state === "printing" && p.progress_percent != null ? " · " + Math.round(p.progress_percent) + "%" : ""));
    const sub = document.createElement("small");
    sub.textContent = p.lease ? (p.lease.manual ? "taken · manual print"
      : (p.lease.yours ? "yours" : p.lease.owner || "taken")) : (p.filename || "free");
    b.append(name, st, sub);
    b.addEventListener("click", () => choosePrinter(p.id, true));
    nav.appendChild(b);
  }
}

function choosePrinter(id, refresh) {
  if (!id || id === PRINTER) { PRINTER = id || PRINTER; return; }
  PRINTER = id;
  localStorage.setItem("qidi-printer", id);
  const u = new URL(location.href); u.searchParams.set("printer", id);
  history.replaceState(null, "", u);
  if (!refresh) return;
  // A start dialog belongs to the printer it was opened for; close it (releasing
  // that printer's claim) rather than leave it on screen over another printer.
  if (pending || $("confirm").open) abandon();
  // Nothing from the previous printer may linger on screen as if it were this one's.
  lastGood = 0; camAt = 0; $("cam-img").removeAttribute("src"); $("cv-img").removeAttribute("src");
  if ($("cam-view").open) $("cam-view").close();
  ["files", "queue-list", "follow-list"].forEach((i) => { const e = $(i); if (e) e.innerHTML = ""; });
  say("print-msg", ""); say("job-msg", "");
  refreshFleet();
  tick().then(syncControls); refreshCamera(); refreshFiles(); refreshFollowers(); refreshQueue();
}
refreshFleet();
setInterval(refreshFleet, 5000);

document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshCamera(); });

tick().then(syncControls);
setInterval(() => { tick().then(syncControls); }, 2000);
whoami();
refreshCamera();
setInterval(refreshCamera, 2000);
setInterval(refreshFollowers, 10000);
</script>
</body>
</html>
"""
