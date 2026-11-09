# Max 4 source configuration: fetch locally

The vendor's configuration files (including `printer.cfg`, `moonraker.conf`,
`crowsnest.conf`, `officiall_filas_list.cfg`, and all macros) were used to
derive the safety bounds, but are not distributed under this repository's
Apache-2.0 license. Their identical upstream Git blobs are in
[QIDITECH/QIDI_MAX4 at 47fc14cefe064dbd30b90582429fe650883ae900](https://github.com/QIDITECH/QIDI_MAX4/tree/47fc14cefe064dbd30b90582429fe650883ae900/config)
(GPL-3.0). To fetch the same sources into the Git-ignored
`research/upstream/qidi-max4/config/`, run
`bash scripts/fetch_upstream_sources.sh` at the repository root. Do not use those
files directly as your live printer configuration: upstream crowsnest includes
a USB by-id device identifier, and Moonraker binds all interfaces with LAN
trust ranges.

| Vendor file | What the project derived |
| --- | --- |
| `printer.cfg` | Axis ranges, heater shutdown thresholds, probe and kinematics |
| `klipper-macros-qd/*.cfg` | Heater operational clamps, macro names, filament and Box operations |
| `moonraker.conf` | Available Moonraker components and trust model |
| `saved_variables.cfg.bak` | Box-state and power-recovery keys |
| `crowsnest.conf` | Webcam service/port conventions |
| `officiall_filas_list.cfg` | Real QIDI filament IDs; optional tests skip without a fetched copy |

**Disposition by file:** `UPSTREAM-QIDI-README.md` was an exact copy of
QIDI's root README and is excluded; the project's `README.md` was retained in
rewritten form (this page). All 22 matching QIDI config blobs are excluded:
`printer.cfg`, `moonraker.conf`, `crowsnest.conf`, `box.cfg`, `drying.conf`,
`officiall_filas_list.cfg`, `saved_variables.cfg.bak` and every file in
`macros/`: `bed_mesh.cfg`, `beep.cfg`, `fans.cfg`, `filament.cfg`, `globals.cfg`,
`heaters.cfg`, `idle.cfg`, `kinematics.cfg`, `park.cfg`, `pause_resume_cancel.cfg`,
`qd_macro.cfg`, `start_end.cfg`, `state.cfg`, `status_events.cfg`, `velocity.cfg`.
Each config file's local Git blob hash was compared to the upstream tree at the
pinned commit and matched; no hardware host, camera device ID, LAN trust
setting, or vendor macro body is distributed in this directory.
