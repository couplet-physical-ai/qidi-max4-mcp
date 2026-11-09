# Research and provenance

The safety envelope in `qidi_mcp/limits.py` was derived from QIDI Max 4
firmware/configuration and Klipper/Moonraker APIs. This Apache-2.0 export
contains our original analysis and proof scripts, **not** verbatim GPLv3
firmware, macros, docs or parser. For full source links, exact revisions,
licenses and the optional retrieval command, see
[upstream sources](upstream/README.md) and the [vendor config index](max4/README.md).

| Source | Used for | Public location |
| --- | --- | --- |
| QIDI Max 4 firmware/config | Heater and axis bounds; macro behavior; filament table; API extensions | [QIDITECH/QIDI_MAX4 config at pinned commit](https://github.com/QIDITECH/QIDI_MAX4/tree/47fc14cefe064dbd30b90582429fe650883ae900/config), GPL-3.0 |
| Klipper | G-code command parsing/semantics and object configuration | [Klipper v0.13.0](https://github.com/Klipper3d/klipper/tree/61c0c8d2ef40340781835dd53fb04cc7a454e37a), GPL-3.0; historical local snapshot revision not recorded |
| Moonraker | HTTP and WebSocket API contracts | [Moonraker pinned API docs](https://github.com/Arksine/moonraker/tree/b559b9a81161f958a43c2f527b6ad96f73310d5d/docs/external_api), GPL-3.0; historical composite revision not recorded |

For local validation run `bash scripts/fetch_upstream_sources.sh`; it downloads the
three public upstreams into the Git-ignored `research/upstream/`. It is not
required for installation or the offline quickstart. Tests requiring QIDI's
complete filament table skip when sources have not been fetched. Differential
proofs exit with a clear instruction if required source is absent.

The vendor configuration is reference-only. In particular, QIDI's published
Moonraker configuration trusts LAN clients without forced login and binds all
interfaces; never copy that config unreviewed onto a real machine. Klipper's
`max_temp` is an MCU shutdown threshold, not a safe operating target. The
vendor heater macro caps below shutdown, and this project defaults lower yet.
