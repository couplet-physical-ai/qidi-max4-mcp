# Optional upstream sources (not distributed)

No third-party source is shipped here. From the repository root run
`bash scripts/fetch_upstream_sources.sh` to download pinned GPLv3 reference material
into this Git-ignored directory. You need network access to GitHub. The proof
scripts that use it give a clear error if it is absent; regular package use and
CI tests do not require the download.

| Source | Pinned revision and license | Fetched paths |
| --- | --- | --- |
| [Klipper](https://github.com/Klipper3d/klipper) | [v0.13.0](https://github.com/Klipper3d/klipper/tree/61c0c8d2ef40340781835dd53fb04cc7a454e37a) (`61c0c8d2ef40340781835dd53fb04cc7a454e37a`), GPL-3.0; historical snapshot revision was not recorded, so this is a documented tag substitution | `klipper_gcode_parser.py` (from `klippy/gcode.py`), four `docs/*.md` references, `klipper-COPYING` |
| [Moonraker](https://github.com/Arksine/moonraker) | [master snapshot](https://github.com/Arksine/moonraker/tree/b559b9a81161f958a43c2f527b6ad96f73310d5d) (`b559b9a81161f958a43c2f527b6ad96f73310d5d`), GPL-3.0; historical composite revision was not recorded | `moonraker/*.md` API chapters and `moonraker/LICENSE` (the source repo's huge composite `moonraker_api.md` is intentionally not copied) |
| [QIDITECH/QIDI_MAX4](https://github.com/QIDITECH/QIDI_MAX4) | [matching config snapshot](https://github.com/QIDITECH/QIDI_MAX4/tree/47fc14cefe064dbd30b90582429fe650883ae900) (`47fc14cefe064dbd30b90582429fe650883ae900`), GPL-3.0 per upstream LICENSE | `qidi-max4/config/*.cfg`, `*.conf`, `klipper-macros-qd/*.cfg`, README and LICENSE |

QIDI's published config is a *vendor example*, not a recommended working config:
`crowsnest.conf` contains a vendor USB camera by-id identifier; the published
Moonraker config binds all interfaces and trusts private LAN ranges. **Never
copy these configs onto your machine unchanged.** The prior
`qidi_oss_firmware.md` was an original survey in the internal source tree, not a
third-party source; it was omitted with the directory. See the public firmware
repos linked in the root README instead. The previous parser's exact revision
was not recorded, so historical proof numbers should not be assumed to be
reproduced by the replacement v0.13.0 tag without re-running the scripts.
