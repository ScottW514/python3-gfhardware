# ForgeFIRM Cloud Mode

ForgeFIRM's optional **factory cloud mode** runs the machine under the Glowforge
web service: the machine presents itself as a stock Glowforge, and the
Glowforge phone/web app drives it end to end — connect homing, Set Focus,
material imaging, and full prints (button press, cut, return-home). It is
distinct from the `gfhome.py` one-shot, which borrows the service only for a
camera-referenced homing cycle.

The Glowforge protocol is undocumented and can change without notice. Each
ForgeFIRM release validates cloud mode against one specific factory
service/firmware version — the version it advertises as `MCov` (currently
`2.6.0-2228`). forgectrl surfaces a compatibility warning when the live service
moves past that baseline (see "Firmware-update policy").

## Components

| Piece | Role |
|---|---|
| `gfcloud.py` (`/usr/sbin`) | Full cloud-mode controller daemon. Spawned and supervised by forgectrl when `controller_mode = cloud` (the init script defers to the supervisor and remains a manual stop only); the pulse device arrives as a broker-inherited fd (`GF_PULSE_FD`) that is never closed, so job boundaries and mode switches do not cycle the 40 V rail. SIGTERM stops the service loop, safes the hardware, and exits. |
| `gfhome.py` (`/usr/sbin`) | One-shot service-driven homing. Invoked for `$H` when `homing_mode = gfcloud`; dispatches with `allow_print=False` so a print can never run inside a homing session. Completion is guarded: a run of near-identical service corrections aborts (the machine is not physically moving), and quiet only counts as homed when the head accelerometer witnessed real motion during the session. |
| `ffmachine.py` (site-packages) | Shared hardware-machine glue: identity overrides from the shared config, and the forgectrl-routed capture machine both clients use. |
| `gfutilities` | Protocol/service layer: auth, WebSocket client, action dispatch, settings report, pulse-file handling. |
| `gfhardware` | The hardware `Machine`: motion, laser latch, switches, cameras. Thermal hardware belongs to the forgectrl cooling engine: the cloud client reports job state (`POST /cool/state`, with the pulse header's run fan duties as the per-job profile) and enforces the published verdict on its fire path — gaining the flow verification and over-temp protection the engine provides. |

Camera captures route through forgectrl's snapshot endpoint (it owns the
imx-media pipeline whenever a stream is open; the snapshot works during an
active stream and takes a per-shot lamp override), with direct V4L2 capture as
the fallback when the daemon is unreachable.

## Scope: telemetry is excluded

The factory streams continuous telemetry; ForgeFIRM does not and will not.
Excluded channels:

- `POST /api/sensor` — the binary sensor firehose.
- WSS `type:"log"` messages — in-band advisory logs.
- WSS `type:"progress"` messages — progress-bar feed.
- The `fault:*` / `estop:*` / `interlock:*` **cloud reporting** namespace.
  (Local fault-to-safe handling is independent of reporting and fully active.)

On-demand requests are still answered: the `settings` report, image captures,
and the functional action handshake.

## Connection and authentication

- `sign_in` returns two JWTs: `auth_token` (Bearer, ~6 h) and `ws_token` (a
  path component of the WS URL, ~30 s expiry, single-use).
- The WS client reconnects through a loop that re-runs `sign_in` for a fresh
  `ws_token` and rebuilds the URL on every reconnect.
- The service drops the socket on its own schedule — roughly hourly in a long
  idle session. That is routine: the reconnect loop re-authenticates with a
  fresh `auth_token` and the machine stays signed in and serving actions
  without operator involvement.
- Any HTTP request that gets a 401 re-signs-in and replays once (the sign-in
  request itself never retries, so there is no recursion).
- `ws_connect()` returns the running client; `GFUIService` stops it (flush
  final events, close socket, join) when the session ends. The WS client and
  action threads are daemon threads — nothing about a session can keep the
  process alive after the service loop exits.
- TLS: standard certificate validation. (The factory additionally pins the
  server SPKI and refuses unpinned connections; matching that is optional
  hardening, not required for interoperability.)

## Wire format

A WS text frame may pack **several newline-delimited JSON objects**; each is
parsed separately, and unparseable objects are logged and skipped.

**Incoming action envelope** (server→machine): `id` (int64, becomes
`action_id`), `action_type`, `machine_serial` (ignored), `status`,
`motion_url` (hunt/motion/print), `settings` (sparse per-action dict), and on
image actions `endpoint` (presigned upload URL).

Only `status:"ready"` starts an action; the other statuses in the protocol
(`new`/`started`/`success`/`failure`) never launch work and are ignored.
**Cancellation is the same action `id` re-sent with `status:"cancelled"`.**

**Outgoing event envelope** (machine→server): `id` (monotonic per-connection
counter), `timestamp` (ms since daemon start, not wall clock), `type:"event"`,
`version:1`, `level`, `action_id` (absent on unsolicited events), `event`.
Parser note: an event's `<action>` prefix segment can be empty — never assume
`split(':')[0]` is non-empty.

## Actions

All eleven `action_type` values in the 2.6.0 vocabulary are handled through a
single dispatch table shared by `gfcloud` and `gfhome`:

| action_type | Behavior |
|---|---|
| `settings` | Sends the on-demand ~600-key settings report. Machine IP, firmware/app version, and serial reach the service through this report (`MCip`, `MCov`, `MCdv`, `MCsn`) — there is no separate status endpoint. |
| `hunt` | Focus-lens homing (Z home + the service's hunt pattern + home offset). |
| `motion` | Downloads and runs the pulse file at `motion_url`. |
| `print` | Full print lifecycle (gfcloud only; gfhome refuses prints). |
| `lid_image` | Lid-camera capture + upload. |
| `head_image` | Head-camera capture + upload. |
| `lidar_image` | Head captures with the distance-measuring laser (per-shot settings arrive as a list). |
| `user_image` | User-requested snapshot; defaults to the lid/bed view. |
| `factory_reset` | Acknowledged as `factory_reset:cancelled` and **never acted on** — a cloud command must not wipe a ForgeFIRM machine. |
| `update_check` | Acknowledged (`firmware_update:check:starting`/`:completed` + `:skipping`); see the firmware policy below. |
| unknown | Ignored. |

### Per-action settings

Actions carry a sparse `settings` dict (for lidar, a list of dicts), normalized
and made available to every image handler. Policy per key:

- **Honored:** `HCil` (head illumination) and `LCfl` (lid flash) are lighting
  the capture path can apply directly.
- **Deliberately not applied:** `HCex`/`HCga`/`HCae`/`HCag` — the factory-scale
  exposure/gain values use different units than the mainline camera controls
  and would mis-expose; per-camera defaults are used instead.
- The opening `settings` action can carry service-pushed values (e.g. the
  `NRic` network-retry family); ForgeFIRM reports its own values and the
  service tolerates that.

Head images are captured with the white torch off — added white light washes
out the measure-laser dot the cloud's focus analysis needs.

### Events emitted

The service drives the entire lifecycle on this reduced event set (the large
factory event/progress state machine is advisory):

- Per action: `<action>:starting`, `<action>:completed`, `<action>:cancelled`.
- Print lifecycle: `print:download:completed`, `print:running`,
  `print:cancelled`, `print:return_to_home:succeeded`, `print:completed`.
- Button: `button:pressed` / `button:released` (the app's "push the button"
  screen needs nothing else).
- Unsolicited `lid:opened` / `lid:closed` — these drive the app's header state
  and trigger an immediate service `lid_image` refresh.

Service behavior worth knowing:

- After any mid-job abort the service re-hunts; after a completed print it
  issues a `lid_image` and a Z re-hunt.
- **The service dead-reckons machine position.** The return-to-home park runs
  immune to the cancel flag (it still stops for lid/e-stop), and
  `print:return_to_home:succeeded` is sent only when the park actually
  completed — reporting success without parking would offset every subsequent
  motion until the next camera re-home.
- Server-side session state can be sticky: after abnormal session deaths the
  service may stall silently mid-sequence in the next session. A fresh WS
  session recovers it.

## Image upload

Image actions carry a presigned `endpoint` URL; the image is `PUT` there as a
plain request (the presigned URL carries its own auth — a Bearer header makes
the storage backend reject it). Without `endpoint`, the legacy
`POST /api/machines/<action_type>/<id>` fallback is used.

## Jobs (motion / print)

- The pulse file at `motion_url` is downloaded and written into the kernel
  pulse-device ring, then run. The deadman flock is held on one fd for the
  whole job; process death fires the kernel dead man's switch.
- An in-run safety poll stops motion on cancellation, lid open, or e-stop, and
  post-action cleanup always locks the laser latch and drops the pulse-device
  registration — including when an action crashes.
- A job larger than the ring is rejected cleanly ("job exceeds the device
  ring") and the action safe-aborts. The whole job is buffered before running,
  so the ring size caps job length.

## Firmware-update policy

ForgeFIRM **never downloads or installs factory firmware.**

- The `update_check` action is acknowledged with
  `firmware_update:check:starting` / `:completed` / `:skipping` — never the
  `:download`/`:apply`/`:commit`/`:reboot` events.
- On connect (when `FACTORY_FIRMWARE.CHECK` is set) a read-only
  `GET /update/current` probe records
  `{latest_gf_version, tested_against_gf, checked_at}` to
  `FACTORY_FIRMWARE.STATUS_FILE` (`/data/forgefirm/gf-latest.json`).
- forgectrl reads that file and shows a **compatibility warning** whenever the
  live service has moved past the tested baseline — regardless of whether a
  newer ForgeFIRM exists — plus an **upgrade recommendation** only when one
  does. The factory `.fw` is never offered.
- `tested_against_gf` is intended to become dedicated release metadata pinned
  by the release pipeline; until that field ships it reads the same value as
  the advertised `MCov`.

## Configuration

| Where | Keys |
|---|---|
| `/data/etc/gfhome.conf` (seeded from `/etc/gfhome.conf.sample`) | `SERVICE.*` (server/status URLs), `FACTORY_FIRMWARE.CHECK` / `STATUS_FILE`, `FORGECTRL.URL`, `LOGGING.FILE` / `LEVEL` / `SAVE_PULS` / `SAVE_SENT_IMAGES` (both save flags default off), `MOTION.*`, `THERMAL.*`. |
| `/data/forgefirm.conf` (managed from the forgectrl UI) | `controller_mode` (`grbl` / `cloud` — read by the forgectrl supervisor, which spawns exactly one controller at boot and on every mode switch; the init scripts defer to it), `homing_mode`, identity overrides `gf_serial` / `gf_password` (a serial override re-derives the hostname). |

## Outstanding items

- **Unobserved actions:** the live service has not been seen issuing
  `update_check`, `user_image`, or `factory_reset`; their exact expected
  payloads/acks are unconfirmed (current handlers are deliberate defaults —
  capture and adjust when first observed).
- **Oversize-job rejection:** not yet exercised against a live job larger than
  the kernel ring.
- **Packaged-path boot:** validate `gfcloud.init` autostart on a flashed image
  with `controller_mode = cloud`.
- **Streaming-during-run:** the kernel ring supports live append while running;
  interleaving download → ring-write → run (with backpressure) would lift the
  ring-size cap on job length.
- **Lid-flash hardware application:** drive the lid flash LED from `LCfl` (and
  any future exposure mapping) in gfhardware.
- **`SAVE_SENT_IMAGES` path:** derives only from `LOGGING.DIR`; should derive
  from `LOGGING.FILE` like `SAVE_PULS` so an unset dir cannot crash the image
  action thread.
- **Park-on-lid-open refinement:** on a lid-open abort, defer the return-home
  park until the lid closes (factory behavior) instead of skipping it.
- **Coolant control per job:** the forgectrl cooling engine holds the pump
  on as part of its idle posture; the `WPon` pulse-header key has no
  applier — if per-job pump control is ever wanted, it belongs in the
  engine's per-job profile (the `/cool/state` report), not here.
- **Optional:** SPKI pinning to match the factory client; emulator
  (`gf-machine-emulator`) full-session parity.
