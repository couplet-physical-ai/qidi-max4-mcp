"""File management on the printer: list, inspect, upload, download, move, delete.

Safety notes specific to this module:

*   Deletes are two-step tokens, and directory deletes report what is inside
    before you can force them.
*   Uploads are size-capped and refuse to silently overwrite: ``overwrite`` must
    be set explicitly if the target exists.
*   ``upload_gcode`` will not use Moonraker's ``print=true`` shortcut. Uploading
    and starting a print are separate decisions with separate confirmations;
    fusing them would let one call commit the machine.
*   Downloads are truncated to a byte budget so a 200 MB G-code file cannot be
    pulled into an agent's context.
*   Paths are screened for traversal (``..``) before they reach Moonraker.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..errors import (
    FileNotFound,
    InvalidRequest,
    MoonrakerHTTPError,
    NotSupported,
    QidiError,
)
from ..policy import Interlocks, Risk
from ..runtime import Runtime, guarded

READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)

_VALID_ROOTS = {"gcodes", "config", "logs", "timelapse"}

#: Suffixes we accept as printable. ``.gcode.3mf`` is the native format of a
#: stock Max4 -- OrcaSlicer exports it and QIDI's screen writes it -- so it must
#: be uploadable. Note a bare ``.3mf`` is deliberately absent: that is a project
#: file which may carry no toolpath at all, and Klipper cannot print it.
_GCODE_SUFFIXES = (".gcode", ".g", ".gco", ".gcode.3mf", ".ufp", ".nc")


def _clean_path(path: str, *, allow_empty: bool = False) -> str:
    p = (path or "").strip().lstrip("/")
    if not p and not allow_empty:
        raise InvalidRequest("A path is required.")
    if ".." in p.split("/"):
        raise InvalidRequest(
            "Path traversal ('..') is not allowed.",
            hint="Use a path relative to the root, e.g. 'projects/part.gcode'.",
        )
    if "\x00" in p:
        raise InvalidRequest("Null bytes are not allowed in a path.")
    return p


def _check_root(root: str) -> str:
    r = (root or "gcodes").strip().lower()
    if r not in _VALID_ROOTS:
        raise InvalidRequest(
            f"Unknown root {root!r}.",
            hint=f"Valid roots: {sorted(_VALID_ROOTS)} (use list_file_roots to confirm).",
        )
    return r


def _split_root(path: str) -> tuple[str, str]:
    """``'gcodes/a/b.gcode'`` -> ``('gcodes', 'a/b.gcode')``; no root means gcodes."""
    head, _, rest = path.partition("/")
    if head in _VALID_ROOTS and rest:
        return head, rest
    return "gcodes", path


def register(server: MCPServer, rt: Runtime) -> None:
    async def _file_exists(root: str, rel: str) -> bool:
        """Whether a file sits at ``root/rel``, judged by its directory listing.

        Deliberately not ``file_metadata``: that only answers for files Moonraker has
        parsed, and only under the gcodes root, with root-relative paths. Asking it
        about ``gcodes/x.gcode`` (the form move/copy take) or about a freshly
        uploaded file it has not parsed yet says "absent" for a file that is there --
        and "absent" is what downgrades an overwrite from DESTRUCTIVE to LOW.
        """
        parent, _, base = rel.rpartition("/")
        try:
            listing = await rt.client.directory_info(
                path=f"{root}/{parent}" if parent else root
            )
        except (NotSupported, FileNotFound):
            return False  # 404: the parent directory does not exist either
        except MoonrakerHTTPError as exc:
            # Upstream Moonraker reports "Directory does not exist" with its
            # default error status, 400, rather than 404.
            if exc.status == 400:
                return False
            raise
        files = (listing or {}).get("files") or []
        return any(f.get("filename") == base for f in files if isinstance(f, dict))

    @server.tool(
        name="list_gcode_files",
        description=(
            "List G-code files on the printer with size, modified time, and (when scanned) "
            "estimated print time and filament use. Filter with `contains`, sort by name or "
            "modified time. This is where start_print's filenames come from."
        ),
        annotations=READ,
    )
    @guarded
    async def list_gcode_files(
        contains: str | None = None,
        limit: int = 50,
        sort_by: str = "modified",
        root: str = "gcodes",
    ) -> dict[str, Any]:
        r = _check_root(root)
        files = await rt.client.file_list(root=r)
        if contains:
            needle = contains.lower()
            files = [f for f in files if needle in str(f.get("path", "")).lower()]
        key = sort_by.strip().lower()
        if key == "name":
            files.sort(key=lambda f: str(f.get("path", "")).lower())
        elif key == "size":
            files.sort(key=lambda f: float(f.get("size", 0) or 0), reverse=True)
        else:
            files.sort(key=lambda f: float(f.get("modified", 0) or 0), reverse=True)
        total = len(files)
        files = files[: max(1, min(limit, 500))]
        slim = [
            {
                "path": f.get("path"),
                "size_bytes": f.get("size"),
                "size_mb": round(float(f.get("size", 0) or 0) / 1e6, 2),
                "modified": f.get("modified"),
                "estimated_time_s": f.get("estimated_time"),
                "filament_total_mm": f.get("filament_total"),
                "filament_type": f.get("filament_type"),
                "object_height_mm": f.get("object_height"),
                "slicer": f.get("slicer"),
            }
            for f in files
        ]
        return {"ok": True, "root": r, "total_matching": total, "returned": len(slim), "files": slim}

    @server.tool(
        name="get_file_metadata",
        description=(
            "Full slicer metadata for one G-code file: estimated time, filament length and "
            "weight, layer heights, per-object height, nozzle diameter, filament type, "
            "first-layer and chamber temperatures, thumbnails present, and the slicer used. "
            "Read this before start_print to confirm the file matches the loaded filament."
        ),
        annotations=READ,
    )
    @guarded
    async def get_file_metadata(filename: str) -> dict[str, Any]:
        name = _clean_path(filename)
        meta = await rt.client.file_metadata(name) or {}
        thumbs = meta.get("thumbnails") or []
        return {
            "ok": True,
            "filename": meta.get("filename", name),
            "size_bytes": meta.get("size"),
            "modified": meta.get("modified"),
            "estimated_time_s": meta.get("estimated_time"),
            "estimated_time_h": (
                round(float(meta["estimated_time"]) / 3600, 2) if meta.get("estimated_time") else None
            ),
            "filament_total_mm": meta.get("filament_total"),
            "filament_weight_total_g": meta.get("filament_weight_total"),
            "filament_type": meta.get("filament_type"),
            "filament_name": meta.get("filament_name"),
            # Per-filament lists for multi-colour files, as Moonraker parses them from the
            # slicer. Compare with get_box_status before printing through a Box.
            "filament_colors": meta.get("filament_colors"),
            "filament_temps_c": meta.get("filament_temps"),
            "referenced_tools": meta.get("referenced_tools"),
            "layer_height_mm": meta.get("layer_height"),
            "first_layer_height_mm": meta.get("first_layer_height"),
            "layer_count": meta.get("layer_count"),
            "object_height_mm": meta.get("object_height"),
            "nozzle_diameter_mm": meta.get("nozzle_diameter"),
            "first_layer_bed_temp_c": meta.get("first_layer_bed_temp"),
            "first_layer_extr_temp_c": meta.get("first_layer_extr_temp"),
            "chamber_temp_c": meta.get("chamber_temp"),
            "slicer": meta.get("slicer"),
            "slicer_version": meta.get("slicer_version"),
            "gcode_start_byte": meta.get("gcode_start_byte"),
            "gcode_end_byte": meta.get("gcode_end_byte"),
            "thumbnail_count": len(thumbs),
            "thumbnails": [
                {"width": t.get("width"), "height": t.get("height"), "size": t.get("size")}
                for t in thumbs
            ],
            "print_start_time": meta.get("print_start_time"),
            "job_id": meta.get("job_id"),
        }

    @server.tool(
        name="list_file_roots",
        description=(
            "The file roots this printer exposes (gcodes, config, logs, timelapse) with their "
            "permissions. Tells you where you may read and write."
        ),
        annotations=READ,
    )
    @guarded
    async def list_file_roots() -> dict[str, Any]:
        roots = await rt.client.file_roots()
        return {"ok": True, "roots": roots}

    @server.tool(
        name="list_directory",
        description=(
            "List one directory's immediate contents (subdirectories and files) under a root. "
            "Use this instead of list_gcode_files when you care about folder structure."
        ),
        annotations=READ,
    )
    @guarded
    async def list_directory(path: str = "gcodes", extended: bool = False) -> dict[str, Any]:
        p = _clean_path(path, allow_empty=True) or "gcodes"
        result = await rt.client.directory_info(path=p, extended=extended) or {}
        return {
            "ok": True,
            "path": p,
            "directories": result.get("dirs", []),
            "files": result.get("files", []),
            "disk_usage": result.get("disk_usage"),
            "root_info": result.get("root_info"),
        }

    @server.tool(
        name="upload_gcode",
        description=(
            "Upload a G-code file to the printer. Provide the content either as `content` "
            "(plain text) or `content_base64` (for binary-safe transfer). Does NOT start a "
            "print — call start_print separately, deliberately. Requires confirm=true; set "
            "overwrite=true to replace an existing file."
        ),
        annotations=WRITE,
    )
    @guarded
    async def upload_gcode(
        filename: str,
        content: str | None = None,
        content_base64: str | None = None,
        subdirectory: str = "",
        overwrite: bool = False,
        confirm: bool = False,
        confirm_token: str | None = None,
    ) -> dict[str, Any]:
        name = _clean_path(filename)
        if "/" in name:
            raise InvalidRequest(
                "filename must be a bare file name; put folders in `subdirectory`."
            )
        # .gcode.3mf is what OrcaSlicer exports and what QIDI's own screen writes,
        # so it is the native format of the printer's library -- every file on a
        # stock Max4 has that suffix. Rejecting it made upload_gcode unable to
        # deliver the one format the machine actually uses.
        if not name.lower().endswith(_GCODE_SUFFIXES):
            raise InvalidRequest(
                f"{name!r} does not look like a G-code file.",
                hint="Expected one of: " + ", ".join(_GCODE_SUFFIXES),
            )
        if (content is None) == (content_base64 is None):
            raise InvalidRequest("Provide exactly one of `content` or `content_base64`.")

        if content_base64 is not None:
            try:
                blob = base64.b64decode(content_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise InvalidRequest(f"content_base64 is not valid base64: {exc}") from exc
        else:
            blob = (content or "").encode("utf-8")

        if not blob:
            raise InvalidRequest("Refusing to upload an empty file.")
        if len(blob) > rt.settings.max_upload_bytes:
            raise InvalidRequest(
                f"File is {len(blob) / 1e6:.1f} MB; the cap is "
                f"{rt.settings.max_upload_bytes / 1e6:.0f} MB.",
                hint="Raise QIDI_MAX_UPLOAD_BYTES if this is legitimate.",
            )

        subdir = _clean_path(subdirectory, allow_empty=True)
        target = f"{subdir}/{name}" if subdir else name

        existing = await _file_exists("gcodes", target)
        if existing and not overwrite:
            raise InvalidRequest(
                f"{target!r} already exists on the printer.",
                hint="Pass overwrite=true to replace it, or choose another name.",
            )

        args = {
            "filename": target,
            "bytes": len(blob),
            "overwrite": overwrite,
        }
        # A fresh upload is reversible — delete it and nothing was lost. Replacing
        # an existing file is not: the previous G-code is gone for good, and any
        # already-issued start_print ticket was reviewed against the OLD contents.
        # That is destructive, so it takes the two-step token rather than a flag,
        # which also makes silent content-swapping impossible.
        risk = Risk.DESTRUCTIVE if existing else Risk.LOW
        ticket = await rt.authorize(
            "upload_gcode",
            risk,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(),
        )
        if ticket is not None:
            if existing:
                ticket["replacing"] = target
                ticket["why"] = (
                    f"{target!r} already exists and will be overwritten. Any pending "
                    "start_print confirmation for it becomes invalid."
                )
            return ticket

        result = await rt.client.file_upload(
            filename=name, content=blob, root="gcodes", path=subdir, start_print=False
        )
        return rt.ok(
            "upload_gcode",
            risk,
            args,
            {
                "uploaded": target,
                "size_bytes": len(blob),
                "replaced_existing": existing,
                "result": result,
                "note": "Nothing is printing yet — call start_print to run it.",
            },
        )

    @server.tool(
        name="download_file",
        description=(
            "Read a file off the printer as text (truncated to a byte budget so it cannot "
            "flood context). Works on any root: 'gcodes' for prints, 'config' for "
            "printer.cfg and friends, 'logs' for klippy.log. Use tail=true to read the end."
        ),
        annotations=READ,
    )
    @guarded
    async def download_file(
        path: str, root: str = "config", max_bytes: int | None = None, tail: bool = False
    ) -> dict[str, Any]:
        r = _check_root(root)
        p = _clean_path(path)
        budget = max(1_000, min(max_bytes or rt.settings.max_read_bytes, 2_000_000))
        # Ask for a bit more than the budget so we can tell whether it was truncated.
        raw = await rt.client.file_download(r, p, max_bytes=budget + 1)
        truncated = len(raw) > budget
        blob = raw[-budget:] if (tail and truncated) else raw[:budget]
        text = blob.decode("utf-8", errors="replace")
        return {
            "ok": True,
            "root": r,
            "path": p,
            "bytes_returned": len(blob),
            "truncated": truncated,
            "read_from": "end" if (tail and truncated) else "start",
            "content": text,
        }

    @server.tool(
        name="delete_file",
        description=(
            "Delete a file from the printer. Two-step confirmation. Refuses to delete the "
            "file that is currently printing."
        ),
        annotations=DESTRUCTIVE,
    )
    @guarded
    async def delete_file(
        path: str, root: str = "gcodes", confirm: bool = False, confirm_token: str | None = None
    ) -> dict[str, Any]:
        r = _check_root(root)
        p = _clean_path(path)
        state = await rt.state(fresh=True)
        if state.job_active and state.filename and p in state.filename:
            raise InvalidRequest(
                f"{p!r} is the file currently being printed.",
                hint="Cancel the print before deleting the file.",
            )
        args = {"path": p, "root": r}
        ticket = await rt.authorize(
            "delete_file",
            Risk.DESTRUCTIVE,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(require_klippy_ready=False),
        )
        if ticket is not None:
            ticket["why"] = "File deletion cannot be undone."
            return ticket
        result = await rt.client.file_delete(r, p)
        return rt.ok("delete_file", Risk.DESTRUCTIVE, args, {"deleted": p, "result": result})


    async def _destination_exists(path: str) -> bool:
        """True if a file already sits at ``path`` (so writing there destroys it).

        ``path`` is in move/copy form, i.e. it usually starts with its root.
        """
        return await _file_exists(*_split_root(path))

    @server.tool(
        name="move_file",
        description=(
            "Move or rename a file/directory on the printer. Both paths include the root, "
            "e.g. source='gcodes/old.gcode', dest='gcodes/archive/new.gcode'. Requires "
            "confirm=true."
        ),
        annotations=WRITE,
    )
    @guarded
    async def move_file(
        source: str, dest: str, confirm: bool = False, confirm_token: str | None = None
    ) -> dict[str, Any]:
        src, dst = _clean_path(source), _clean_path(dest)
        args = {"source": src, "dest": dst}
        # Overwriting an existing destination destroys it and invalidates any
        # start_print confirmation already issued for that path, so it takes the
        # two-step token rather than a bare flag.
        clobbers = await _destination_exists(dst)
        risk = Risk.DESTRUCTIVE if clobbers else Risk.LOW
        ticket = await rt.authorize(
            "move_file",
            risk,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(require_klippy_ready=False),
        )
        if ticket is not None:
            if clobbers:
                ticket["replacing"] = dst
                ticket["why"] = f"{dst!r} already exists and will be replaced."
            return ticket
        result = await rt.client.file_move(src, dst)
        return rt.ok("move_file", risk, args, {"result": result, "replaced_existing": clobbers})

    @server.tool(
        name="copy_file",
        description=(
            "Copy a file/directory on the printer. Both paths include the root, e.g. "
            "source='gcodes/a.gcode', dest='gcodes/backup/a.gcode'. Requires confirm=true."
        ),
        annotations=WRITE,
    )
    @guarded
    async def copy_file(
        source: str, dest: str, confirm: bool = False, confirm_token: str | None = None
    ) -> dict[str, Any]:
        src, dst = _clean_path(source), _clean_path(dest)
        args = {"source": src, "dest": dst}
        # Overwriting an existing destination destroys it and invalidates any
        # start_print confirmation already issued for that path, so it takes the
        # two-step token rather than a bare flag.
        clobbers = await _destination_exists(dst)
        risk = Risk.DESTRUCTIVE if clobbers else Risk.LOW
        ticket = await rt.authorize(
            "copy_file",
            risk,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(require_klippy_ready=False),
        )
        if ticket is not None:
            if clobbers:
                ticket["replacing"] = dst
                ticket["why"] = f"{dst!r} already exists and will be replaced."
            return ticket
        result = await rt.client.file_copy(src, dst)
        return rt.ok("copy_file", risk, args, {"result": result, "replaced_existing": clobbers})

    @server.tool(
        name="create_directory",
        description=(
            "Create a directory on the printer, path including the root, e.g. "
            "'gcodes/projects'. Requires confirm=true."
        ),
        annotations=WRITE,
    )
    @guarded
    async def create_directory(
        path: str, confirm: bool = False, confirm_token: str | None = None
    ) -> dict[str, Any]:
        p = _clean_path(path)
        args = {"path": p}
        ticket = await rt.authorize(
            "create_directory",
            Risk.LOW,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(require_klippy_ready=False),
        )
        if ticket is not None:
            return ticket
        result = await rt.client.directory_create(p)
        return rt.ok("create_directory", Risk.LOW, args, {"created": p, "result": result})

    @server.tool(
        name="delete_directory",
        description=(
            "Delete a directory on the printer. Two-step confirmation, and the ticket lists "
            "what is inside first. force=true is required to delete a non-empty directory."
        ),
        annotations=DESTRUCTIVE,
    )
    @guarded
    async def delete_directory(
        path: str, force: bool = False, confirm: bool = False, confirm_token: str | None = None
    ) -> dict[str, Any]:
        p = _clean_path(path)
        args = {"path": p, "force": force}
        ticket = await rt.authorize(
            "delete_directory",
            Risk.DESTRUCTIVE,
            args,
            confirm=confirm,
            confirm_token=confirm_token,
            interlocks=Interlocks(require_klippy_ready=False),
        )
        if ticket is not None:
            try:
                info = await rt.client.directory_info(path=p) or {}
                ticket["directory_contains"] = {
                    "files": [f.get("filename") for f in info.get("files", [])][:50],
                    "subdirectories": [d.get("dirname") for d in info.get("dirs", [])][:50],
                    "file_count": len(info.get("files", [])),
                }
            except QidiError:
                ticket["directory_contains"] = "could not read directory"
            ticket["why"] = "Deleting a directory removes everything in it, irreversibly."
            return ticket
        result = await rt.client.directory_delete(p, force=force)
        return rt.ok("delete_directory", Risk.DESTRUCTIVE, args, {"deleted": p, "result": result})

    _ = (
        list_gcode_files,
        get_file_metadata,
        list_file_roots,
        list_directory,
        upload_gcode,
        download_file,
        delete_file,
        move_file,
        copy_file,
        create_directory,
        delete_directory,
    )
