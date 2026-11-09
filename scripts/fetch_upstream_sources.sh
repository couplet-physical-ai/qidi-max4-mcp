#!/usr/bin/env bash
# Optional local research material. None of these third-party files ships in the export.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out="$root/research/upstream"
klipper=61c0c8d2ef40340781835dd53fb04cc7a454e37a # Klipper v0.13.0
moonraker=b559b9a81161f958a43c2f527b6ad96f73310d5d # pinned master snapshot
qidi=47fc14cefe064dbd30b90582429fe650883ae900 # matching archived config blobs

fetch() {
    local repo="$1" ref="$2" source="$3" target="$4"
    mkdir -p "$(dirname "$out/$target")"
    curl --fail --location --retry 2 --silent --show-error \
        "https://raw.githubusercontent.com/$repo/$ref/$source" -o "$out/$target"
}

fetch Klipper3d/klipper "$klipper" klippy/gcode.py klipper_gcode_parser.py
for file in G-Codes.md Status_Reference.md Config_Reference.md Command_Templates.md; do
    fetch Klipper3d/klipper "$klipper" "docs/$file" "$file"
done
fetch Klipper3d/klipper "$klipper" COPYING klipper-COPYING

# Moonraker's current documentation is split into individual API chapters;
# the historical composite research/upstream/moonraker_api.md is not reproduced.
for file in introduction printer server authorization file_manager jsonrpc_notifications; do
    fetch Arksine/moonraker "$moonraker" "docs/external_api/$file.md" "moonraker/$file.md"
done
fetch Arksine/moonraker "$moonraker" LICENSE moonraker/LICENSE

for file in README.md LICENSE; do
    fetch QIDITECH/QIDI_MAX4 "$qidi" "$file" "qidi-max4/$file"
done
for file in printer.cfg moonraker.conf crowsnest.conf box.cfg drying.conf officiall_filas_list.cfg saved_variables.cfg.bak; do
    fetch QIDITECH/QIDI_MAX4 "$qidi" "config/$file" "qidi-max4/config/$file"
done
for file in bed_mesh.cfg beep.cfg fans.cfg filament.cfg globals.cfg heaters.cfg idle.cfg kinematics.cfg park.cfg pause_resume_cancel.cfg qd_macro.cfg start_end.cfg state.cfg status_events.cfg velocity.cfg; do
    fetch QIDITECH/QIDI_MAX4 "$qidi" "config/klipper-macros-qd/$file" "qidi-max4/config/klipper-macros-qd/$file"
done
printf 'Fetched optional GPLv3 reference sources at %s (ignored by Git).\n' "$out"
