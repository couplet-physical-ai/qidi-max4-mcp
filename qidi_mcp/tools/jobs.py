"""Print job lifecycle: start, pause, resume, cancel, history, queue.

Safety notes specific to this module:

*   ``start_print`` is the single most consequential tool here — it commits
    hours of machine time and a spool of filament — so it verifies the file
    exists, reports what it is about to print (including the slicer's own
    temperature/time estimates), and requires the two-step token.
*   Pause/resume/cancel require a job to actually be active, so an agent
    "cleaning up" cannot cancel a print that already finished, or resume one
    that was never paused.
*   Cancel is irreversible in the sense that matters (the print is ruined), so
    it is a two-step token as well.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..box import box_reader, brief
from ..errors import ConfirmationRequired, InvalidRequest, NotSupported, QidiError
from ..gcode_names import resolve_gcode_names
from ..policy import Interlocks, Risk
from ..printfit import box_slots, fit_summary
from ..runtime import Runtime, guarded

JOB_ANN = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False)
READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True)


_TRUTHY = {"true", "1", "yes", "on"}


def _secs(value: Any) -> float:
    """Seconds from slicer metadata; junk or missing counts as zero, never as a crash."""
    try:
        out = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return out if out == out and out >= 0 else 0.0  # NaN and negatives are not durations


def _hours(seconds: Any) -> float | None:
    value = _secs(seconds)
    return round(value / 3600, 2) if value else None


def _short_fit(fit: dict[str, Any] | None) -> dict[str, Any] | None:
    """The verdict and one-line reason; the full per-tool report is check_print_fit's job."""
    if fit is None:
        return None
    return {"verdict": fit["verdict"], "summary": fit["summary"]}


def register(server: MCPServer, rt: Runtime) -> None:
    async def _metadata(name: str) -> dict[str, Any] | None:
        """Slicer metadata for one file, or None when Moonraker has none (yet)."""
        try:
            return await rt.client.file_metadata(name) or {}
        except QidiError:
            return None

    async def _queue_advance() -> dict[str, Any] | None:
        """Whether Moonraker moves to the next queued job on its own.

        That is the question that decides whether a queue is safe to leave: with
        ``automatic_transition`` the next print starts the moment the last one ends,
        and nothing has cleared the build plate. Moonraker's default is *off*, so the
        queue pauses after each job. Read from the printer's own config, and reported
        with where the answer came from; None when it cannot be read.
        """
        try:
            cfg = (await rt.client.server_config() or {}).get("config") or {}
        except QidiError:
            return None
        section = cfg.get("job_queue") or {}
        explicit = "automatic_transition" in section
        automatic = str(section.get("automatic_transition", "false")).strip().lower() in _TRUTHY
        return {
            "automatic_transition": automatic,
            "source": "the printer's Moonraker config" if explicit else "Moonraker's default (not set)",
            "meaning": (
                "WARNING: the queue moves to the next job by itself. Nothing clears the "
                "build plate in between, so the next print starts on top of the finished one."
                if automatic
                else "After each job the queue pauses by itself, so the next one waits until "
                "you clear the plate and release the queue (control_job_queue action='start')."
            ),
        }

    @server.tool(
        name="start_print",
        description=(
            "Start printing a G-code file already on the printer (path relative to the "
            "gcodes root, e.g. 'benchy.gcode' or 'projects/bracket.gcode'). Two-step "
            "confirmation: the first call returns the file's slicer metadata plus a token so "
            "you can check you are printing the right thing, and the second call with that "
            "token starts it. Refused if a job is already running."
        ),
        annotations=JOB_ANN,
    )
    @guarded
    async def start_print(
        filename: str, confirm: bool = False, confirm_token: str | None = None
    ) -> dict[str, Any]:
        name = filename.strip().lstrip("/")
        if not name:
            raise InvalidRequest("filename is required.")

        # Verify the file exists and read its metadata before committing.
        meta: dict[str, Any] = {}
        try:
            meta = await rt.client.file_metadata(name) or {}
        except QidiError as exc:
            raise InvalidRequest(
                f"Cannot print {name!r}: {exc.message}",
                hint="Use list_gcode_files to see what is on the printer.",
            ) from exc

        # Bind the ticket to the file's CONTENT identity, not just its name.
        #
        # The confirmation step exists so a human/agent reviews this specific job
        # — its temperatures, duration, filament — and then commits to it. A
        # fingerprint over the name alone does not deliver that: upload_gcode
        # (overwrite=True), move_file and copy_file are all unconfirmed LOW-tier
        # operations, so within the token's lifetime the bytes behind the name can
        # be replaced with anything and the already-issued token still redeems.
        # Approval of "G28 / M117 hello" would then start arbitrary G-code, which
        # also sidesteps run_gcode's command screen entirely (file contents are
        # never screened). Including size and modified time means any such swap
        # changes the fingerprint and the token is rejected.
        args = {
            "filename": name,
            "size": meta.get("size"),
            "modified": meta.get("modified"),
        }

        state = await rt.state(fresh=True)
        if state.job_active:
            raise InvalidRequest(
                f"A job is already {state.print_state}"
                + (f" ({state.filename})" if state.filename else "")
                + ".",
                hint="Cancel or wait for the current job before starting another.",
            )

        ticket = await rt.authorize(
            "start_print",
            Risk.DESTRUCTIVE,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(forbid_during_print=True),
        )
        if ticket is not None:
            ticket["about_to_print"] = {
                "filename": name,
                "estimated_time_s": meta.get("estimated_time"),
                "estimated_time_h": (
                    round(float(meta["estimated_time"]) / 3600, 2)
                    if meta.get("estimated_time")
                    else None
                ),
                "filament_total_mm": meta.get("filament_total"),
                "filament_weight_g": meta.get("filament_weight_total"),
                "filament_type": meta.get("filament_type"),
                "filament_colors": meta.get("filament_colors"),
                "referenced_tools": meta.get("referenced_tools"),
                "first_layer_bed_temp_c": meta.get("first_layer_bed_temp"),
                "first_layer_extruder_temp_c": meta.get("first_layer_extr_temp"),
                "chamber_temp_c": meta.get("chamber_temp"),
                "layer_height_mm": meta.get("layer_height"),
                "object_height_mm": meta.get("object_height"),
                "nozzle_diameter_mm": meta.get("nozzle_diameter"),
                "slicer": meta.get("slicer"),
                "size_bytes": meta.get("size"),
            }
            # With a Box attached the spool question has an answer the envelope never
            # had: the Box records what is in each slot. Best effort and additive -- a
            # printer with no Box, or a Box read that fails, must not change or block
            # the ticket.
            try:
                reader = box_reader(rt)
                box_state = await reader.state()
                if box_state.box_count > 0 or box_state.extras is not None:
                    ticket["box"] = brief(box_state, await reader.table())
            except QidiError:
                pass
            fit = fit_summary(meta, await box_slots(rt))
            if fit is not None:
                ticket["box_fit"] = fit
            ticket["why"] = (
                "Starting a print commits the machine for hours and consumes filament. "
                "Check the metadata above matches what you intend, then re-call with the token. "
                "The token is bound to this file's exact size and modification time, so if the "
                "file is replaced before you confirm, the token will be rejected and you will "
                "need to review the new file."
            )
            return ticket

        await rt.client.print_start(name)
        new_state = await rt.state(fresh=True)
        return rt.ok(
            "start_print",
            Risk.PRINT,
            args,
            {
                "started": name,
                "print_state": new_state.print_state,
                "estimated_time_s": meta.get("estimated_time"),
                "note": "Track it with get_job_status.",
            },
            detail=name,
        )

    @server.tool(
        name="pause_print",
        description=(
            "Pause the running print. On the Max4 this runs QIDI's PAUSE macro, which parks "
            "the toolhead and retracts. Requires confirm=true and an active print."
        ),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False),
    )
    @guarded
    async def pause_print(confirm: bool = False, confirm_token: str | None = None) -> dict[str, Any]:
        args: dict[str, Any] = {}
        ticket = await rt.authorize(
            "pause_print",
            Risk.PRINT,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(require_job_active=True),
        )
        if ticket is not None:
            return ticket
        state = await rt.state()
        if state.is_paused:
            return {"ok": True, "already_paused": True, "print_state": state.print_state}
        await rt.client.print_pause()
        new_state = await rt.state(fresh=True)
        return rt.ok(
            "pause_print",
            Risk.PRINT,
            args,
            {"print_state": new_state.print_state, "note": "Heaters stay on while paused."},
        )

    @server.tool(
        name="resume_print",
        description="Resume a paused print. Requires confirm=true and a paused job.",
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False),
    )
    @guarded
    async def resume_print(confirm: bool = False, confirm_token: str | None = None) -> dict[str, Any]:
        args: dict[str, Any] = {}
        ticket = await rt.authorize(
            "resume_print",
            Risk.PRINT,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(require_job_active=True),
        )
        if ticket is not None:
            return ticket
        state = await rt.state()
        if not state.is_paused:
            raise InvalidRequest(
                f"The print is {state.print_state}, not paused.",
                hint="Only a paused print can be resumed.",
            )
        await rt.client.print_resume()
        new_state = await rt.state(fresh=True)
        return rt.ok("resume_print", Risk.PRINT, args, {"print_state": new_state.print_state})

    @server.tool(
        name="cancel_print",
        description=(
            "Cancel the running or paused print. This ruins the part — the job cannot be "
            "resumed afterwards. Two-step confirmation, and it reports how far along the "
            "print was so you can see what you are throwing away."
        ),
        annotations=JOB_ANN,
    )
    @guarded
    async def cancel_print(confirm: bool = False, confirm_token: str | None = None) -> dict[str, Any]:
        args: dict[str, Any] = {}
        ticket = await rt.authorize(
            "cancel_print",
            Risk.DESTRUCTIVE,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(require_job_active=True),
        )
        if ticket is not None:
            status = await rt.client.query(
                {"print_stats": None, "display_status": ["progress"]}
            )
            stats = status.get("print_stats", {}) or {}
            ticket["about_to_cancel"] = {
                "filename": stats.get("filename"),
                "progress_percent": round(
                    float((status.get("display_status") or {}).get("progress", 0.0) or 0.0) * 100, 1
                ),
                "print_duration_s": stats.get("print_duration"),
                "filament_used_mm": stats.get("filament_used"),
                "current_layer": (stats.get("info") or {}).get("current_layer"),
                "total_layers": (stats.get("info") or {}).get("total_layer"),
            }
            ticket["why"] = "Cancelling destroys the part in progress and cannot be undone."
            return ticket

        await rt.client.print_cancel()
        new_state = await rt.state(fresh=True)
        return rt.ok(
            "cancel_print",
            Risk.DESTRUCTIVE,
            args,
            {
                "print_state": new_state.print_state,
                "note": "QIDI's CANCEL_PRINT macro parks the toolhead and turns off heaters.",
            },
        )

    @server.tool(
        name="get_print_history",
        description=(
            "Past print jobs with status, duration, filament used and timestamps. Useful for "
            "'did the last print succeed?' and for spotting a pattern of failures."
        ),
        annotations=READ,
    )
    @guarded
    async def get_print_history(limit: int = 20, start: int = 0) -> dict[str, Any]:
        caps = await rt.capabilities()
        caps.require_component("history", "Print history")
        result = await rt.client.history_list(limit=max(1, min(limit, 100)), start=start)
        jobs = (result or {}).get("jobs", [])
        slim = [
            {
                "job_id": j.get("job_id"),
                "filename": j.get("filename"),
                "status": j.get("status"),
                "start_time": j.get("start_time"),
                "end_time": j.get("end_time"),
                "print_duration_s": j.get("print_duration"),
                "total_duration_s": j.get("total_duration"),
                "filament_used_mm": j.get("filament_used"),
                "exists": j.get("exists"),
            }
            for j in jobs
        ]
        return {"ok": True, "count": (result or {}).get("count"), "jobs": slim}

    @server.tool(
        name="get_history_totals",
        description=(
            "Lifetime totals for the machine: jobs completed, total print time, longest job, "
            "and total filament used."
        ),
        annotations=READ,
    )
    @guarded
    async def get_history_totals() -> dict[str, Any]:
        caps = await rt.capabilities()
        caps.require_component("history", "Print history totals")
        result = await rt.client.history_totals()
        return {"ok": True, **(result or {})}

    @server.tool(
        name="get_job_queue",
        description=(
            "The pending job queue: what is lined up to print next and whether the queue is "
            "started or paused."
        ),
        annotations=READ,
    )
    @guarded
    async def get_job_queue() -> dict[str, Any]:
        caps = await rt.capabilities()
        if not caps.has_component("job_queue"):
            raise NotSupported(
                "This Moonraker build has no job_queue component.",
                hint="Print files one at a time with start_print.",
            )
        result = dict(await rt.client.job_queue_status() or {})
        jobs = list(result.get("queued_jobs") or [])
        slots = await box_slots(rt) if jobs else None
        summary: list[str] = []
        total_s = 0.0
        for n, job in enumerate(jobs[:20], start=1):
            meta = await _metadata(str(job.get("filename", "")))
            if meta is None:
                summary.append(f"{n}. {job.get('filename')} (no slicer metadata)")
                continue
            job["estimated_time_h"] = _hours(meta.get("estimated_time"))
            job["filament_type"] = meta.get("filament_type")
            job["box_fit"] = _short_fit(fit_summary(meta, slots))
            total_s += _secs(meta.get("estimated_time"))
            bits = [b for b in (
                f"{job['estimated_time_h']} h" if job["estimated_time_h"] else None,
                job["filament_type"],
            ) if b]
            flag = (
                f" [{job['box_fit']['verdict']}: {job['box_fit']['summary']}]"
                if job["box_fit"] and job["box_fit"]["verdict"] != "ok"
                else ""
            )
            summary.append(f"{n}. {job.get('filename')}" + (f" -- {', '.join(bits)}" if bits else "") + flag)
        return {
            "ok": True,
            **result,
            "summary": summary,
            "total_estimated_time_h": _hours(total_s),
            "advance": await _queue_advance(),
        }

    @server.tool(
        name="check_print_fit",
        description=(
            "Before printing or queueing: does what the QIDI Box records in its slots suit "
            "these files? Compares each file's filament type and colours (from its slicer "
            "metadata) with the material and colour recorded per slot, and says which slots "
            "could serve each one. Names may be loose ('benchy'); an ambiguous or unknown "
            "name is an error that lists candidates. Advisory only: it never blocks, and it "
            "does not claim to know which slot the printer feeds for which tool. Returns "
            "nothing to compare on a printer with no Box."
        ),
        annotations=READ,
    )
    @guarded
    async def check_print_fit(filenames: list[str]) -> dict[str, Any]:
        available = [str(f.get("path")) for f in await rt.client.file_list() if f.get("path")]
        resolved = resolve_gcode_names(filenames, available)
        slots = await box_slots(rt)
        files = []
        for item in resolved:
            meta = await _metadata(item["file"])
            files.append(
                {
                    **item,
                    "estimated_time_h": _hours((meta or {}).get("estimated_time")),
                    "filament_type": (meta or {}).get("filament_type"),
                    "fit": (
                        fit_summary(meta, slots)
                        if meta is not None
                        else {"verdict": "unknown", "summary": "Moonraker has no slicer metadata for this file yet."}
                    ),
                }
            )
        order = {"ok": 0, "unknown": 1, "check": 2, "mismatch": 3}
        verdicts = [f["fit"]["verdict"] for f in files if f["fit"]]
        return {
            "ok": True,
            "box_present": slots is not None,
            "overall": max(verdicts, key=order.__getitem__) if verdicts else None,
            "files": files,
            "note": None if slots is not None else "No Box is attached, so there is nothing to compare; the extruder's own filament is what prints.",
        }

    @server.tool(
        name="queue_jobs",
        description=(
            "Add one or more G-code files to the shared print queue. Names may be loose "
            "('benchy' finds benchy.gcode); an unknown or ambiguous name fails the whole call "
            "with the candidates. The first call (no confirm) shows what each name resolved "
            "to, its print time, and whether the Box's filament suits it; re-call with "
            "confirm=true to queue. This is the cooperative "
            "way to share the machine: when somebody else is printing, queue your file so it "
            "runs after theirs instead of competing for the printer. Requires confirm=true. "
            "If the queue is already 'ready' it auto-starts when the printer goes idle, so in "
            "that state this counts as starting a print and also needs the printer lease."
        ),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False),
    )
    @guarded
    async def queue_jobs(
        filenames: list[str],
        reset: bool = False,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        if not filenames:
            raise InvalidRequest("Give at least one filename.")
        caps = await rt.capabilities()
        if not caps.has_component("job_queue"):
            raise NotSupported("This Moonraker build has no job_queue component.")

        # Resolve loose names ("benchy") to real paths BEFORE anything else, and fail the
        # whole call on any that are unknown or ambiguous: queueing the wrong file is a
        # print, not a typo. Everything below -- the audit args, the lease decision, the
        # post -- sees the resolved paths.
        available = [str(f.get("path")) for f in await rt.client.file_list() if f.get("path")]
        resolved = resolve_gcode_names(filenames, available)
        names = [r["file"] for r in resolved]

        # Read the queue state BEFORE authorising, and pass it through the gate.
        #
        # A queue in state 'ready' starts the next job the moment the printer is
        # idle, so appending to it is a print start wearing a quieter name. The
        # lease requirement keys off this value (see runtime.lease_required), which
        # only works if it is in the audit args -- and it belongs there anyway,
        # because "queued a file" and "queued a file onto a live queue" are
        # different events and the log should be able to tell them apart.
        status = await rt.client.job_queue_status() or {}
        queue_state = str(status.get("queue_state", "") or "")
        live = queue_state.lower() == "ready"
        args = {"filenames": names, "reset": reset, "queue_state": queue_state}

        slots = await box_slots(rt)
        report = []
        total_s = 0.0
        for item in resolved:
            meta = await _metadata(item["file"])
            row: dict[str, Any] = {**item}
            if meta is None:
                row["warning"] = "Moonraker has no slicer metadata for this file (yet)."
            else:
                total_s += _secs(meta.get("estimated_time"))
                row.update(
                    estimated_time_h=_hours(meta.get("estimated_time")),
                    filament_type=meta.get("filament_type"),
                    box_fit=_short_fit(fit_summary(meta, slots)),
                )
            report.append(row)
        advance = await _queue_advance()

        preview: dict[str, Any] = {
            "queue_state": queue_state,
            "will_start_automatically": live,
            "will_queue": report,
            "total_estimated_time_h": _hours(total_s),
            "already_queued": [j.get("filename") for j in status.get("queued_jobs") or []],
            "advance": advance,
        }
        if live:
            preview["warning"] = (
                "The queue is 'ready', so the printer will begin this job as soon as it "
                "is idle — this is a print start, not just a note for later. Pause the "
                "queue first (control_job_queue action='pause') if you only meant to line "
                "it up."
            )
        try:
            ticket = await rt.authorize(
                "queue_jobs",
                Risk.PRINT,
                args,
                confirm=confirm,
                confirm_token=confirm_token,
                interlocks=Interlocks(),
            )
        except ConfirmationRequired as exc:
            # Answer "what would this queue?" in the same call that refuses to do it.
            exc.extra.update(preview)
            raise
        if ticket is not None:  # strict-confirm mode answers with a ticket instead
            ticket.update(preview)
            return ticket

        result = await rt.client.job_queue_post(names, reset=reset)
        return rt.ok(
            "queue_jobs",
            Risk.PRINT,
            args,
            {
                "queue": result,
                "queued": report,
                "total_estimated_time_h": _hours(total_s),
                "will_start_automatically": live,
                "advance": advance,
            },
        )

    @server.tool(
        name="remove_queued_jobs",
        description=(
            "Remove jobs from the print queue by job_id, or clear the whole queue with "
            "all=true. Requires confirm=true."
        ),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True),
    )
    @guarded
    async def remove_queued_jobs(
        job_ids: list[str] | None = None,
        all: bool = False,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        if not job_ids and not all:
            raise InvalidRequest("Give job_ids, or all=true to clear the queue.")
        caps = await rt.capabilities()
        if not caps.has_component("job_queue"):
            raise NotSupported("This Moonraker build has no job_queue component.")
        args = {"job_ids": job_ids, "all": all}
        ticket = await rt.authorize(
            "remove_queued_jobs",
            Risk.PRINT,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(),
        )
        if ticket is not None:
            return ticket
        result = await rt.client.job_queue_delete(job_ids, all_=all)
        return rt.ok("remove_queued_jobs", Risk.PRINT, args, {"queue": result})

    @server.tool(
        name="control_job_queue",
        description=(
            "Start or pause the shared job queue. action='start' releases it so queued jobs "
            "begin printing as soon as the printer is idle — that needs the printer lease, "
            "since it commits the machine. action='pause' holds them, which is always allowed: "
            "preventing surprise starts is the safe direction. Requires confirm=true. Neither "
            "affects a job that is already printing."
        ),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False),
    )
    @guarded
    async def control_job_queue(
        action: str, confirm: bool = False, confirm_token: str | None = None
    ) -> dict[str, Any]:
        act = action.strip().lower()
        if act not in {"start", "pause"}:
            raise InvalidRequest("action must be 'start' or 'pause'.")
        caps = await rt.capabilities()
        if not caps.has_component("job_queue"):
            raise NotSupported("This Moonraker build has no job_queue component.")
        args = {"action": act}
        ticket = await rt.authorize(
            "control_job_queue",
            Risk.PRINT,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(),
        )
        if ticket is not None:
            return ticket
        result = (
            await rt.client.job_queue_start() if act == "start" else await rt.client.job_queue_pause()
        )
        return rt.ok("control_job_queue", Risk.PRINT, args, {"queue": result})

    _ = (
        start_print,
        pause_print,
        resume_print,
        cancel_print,
        get_print_history,
        get_history_totals,
        get_job_queue,
        check_print_fit,
        queue_jobs,
        remove_queued_jobs,
        control_job_queue,
    )
