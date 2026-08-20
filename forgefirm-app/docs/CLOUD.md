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

**The cameras only capture with the lid closed.** This is a privacy rule, not a
factory behavior: the lid camera faces the room once the lid is raised, and the
service asks for images on its own schedule. Both capture paths enforce it —
forgectrl answers `409` and the direct fallback raises `gfhardware.cam.LidOpen`
— and the check fails closed, so an unreadable lid also refuses. There is no
setting to disable it. A refused image action is reported to the service as
`<action>:failed` so it resolves rather than hanging, and the client does not
fall back to a direct grab that would refuse identically. **Consequence:** the
factory ran focus hunts with the lid open and a hunt includes a head capture,
so a hunt attempted with the lid open now fails; the lid must be shut before
the app focuses or prints.

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

The service pushes very little, and it is worth knowing exactly how little
before going looking for a setting that is not in a pulse header. Across every
captured session, counting every action type and not just the opening one, the
service has ever pushed seven keys: `IMct`, `NRic`, `HCil`, `HCae`, `HCex`,
`HCag` and `HCga`. The opening `settings` action on its own carries one,
`NRic`. Nothing thermal, nothing about fans, nothing that bounds the machine
arrives this way. The operating envelope reaches the machine only in the pulse
header, per job.

The other half of that: for the header fields the service does not override
with its own policy, what comes back is what this machine last reported. The
`MACHINE_SETTINGS` defaults are largely placeholders, so those fields
round-trip as placeholders. A field arriving as zero usually means ForgeFIRM
sent zero, not that the service has nothing to say. Nothing downstream consumes
them today, but it does mean the service is not a source of truth for any limit
the machine itself declares.

Head images are captured with the white torch off — added white light washes
out the measure-laser dot the cloud's focus analysis needs.

### Events emitted

The service drives the entire lifecycle on this reduced event set (the large
factory event/progress state machine is advisory):

- Per action: `<action>:starting`, `<action>:completed`, `<action>:cancelled`.
- Print lifecycle: `print:download:completed`, `print:running`,
  `print:paused` / `print:resumed`, `print:cancelled`,
  `print:return_to_home:succeeded`, `print:completed`.
- Button: `button:pressed` / `button:released` (the app's "push the button"
  screen needs nothing else).
- Unsolicited `lid:opened` / `lid:closed` — these drive the app's header state
  and trigger an immediate service `lid_image` refresh.

Service behavior worth knowing:

- After any mid-job abort the service re-hunts; after a completed print it
  issues a `lid_image` and a Z re-hunt.
- **The service dead-reckons machine position.** The return-to-home park runs
  after every print, finished or aborted, and ignores the lid and the cancel
  flag while it runs (the factory parks with the lid open, and a park cut short
  would offset every subsequent motion until the next camera re-home);
  `print:return_to_home:succeeded` is sent only when the park actually
  completed. A job refused before it moved (lid or interlock open at start —
  a backstop, the app itself will not print until the lid is closed and imaged)
  ends `:cancelled`, never `:completed`.
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
- The job is supervised the way the factory firmware supervises it. The
  hardware chain kills the beam on the lid and the interlock loop by itself;
  the client decides what motion and the job do, reacting on the switch edge
  (the switch thread wakes the run loop; the level read every 100 ms is the
  backstop):
  - lid or interlock loop opens during a print/motion, or the service cancels
    it, or the cooling verdict pulls fire: controlled stop (`cnc/stop`,
    position kept), job cancelled; a print then parks (with the lid open, if
    it is) and reports `:cancelled`. The park first drops what the job left
    in the ring (the rest of an aborted print, or the whole print after a
    cancel at the button wait) - the factory's "clearing pulse data" - so
    nothing plays ahead of it; a job that never moved parks nothing;
  - lid or interlock open during the pre-print button wait: latch relocked,
    job cancelled; a press with the lid open never arms;
  - a hunt ignores the lid (lens travel plus the service's XY hunt pattern);
  - **the button pauses and resumes a print**: press → controlled stop, then
    `cloud_pause_backtrack_ticks` (default 2000) ticks backward with the laser
    off, `print:paused`; press again → forward with the laser re-enabled after
    `cloud_resume_lead_ticks` (default 1950), `print:resumed`. What the two
    counts really say is that the beam comes back on 50 ticks before the point
    the pause stopped at, over ground the job already cut, and that is the
    invariant the client keeps: the retrace is sized to `cnc/max_backtrack`,
    the history the ring still holds, and the lead follows it down. A pause in
    a print's first moments therefore retraces a little and leads a little,
    rather than failing; a live-fed print pauses exactly like a preloaded one,
    because the ring's retained gap is history whether the job was preloaded
    or is being fed. The laser latch stays unlocked and the armed window open
    through the pause (HV_ENABLE drops by itself when the stream stops, and the
    resume lead covers its re-arm); lid, interlock or a service cancel while
    paused cancel the job from where it stands. Motions and hunts do not pause.
    Both tick counts are `forgefirm.conf` keys.
- Post-action cleanup always locks the laser latch and drops the pulse-device
  registration — including when an action crashes.
- A job larger than the ring runs anyway: the client holds the compressed body
  in memory, fills the ring before the button is asked for, and tops it up as
  it drains, so the ring is a window onto the job rather than the place the job
  lives. The ring size caps how much of a job is buffered at once (~56 min at
  the print tick), not how long a job may be.

### The pulse header

Every pulse file opens with a header: a length, then a flat list of 4-character
tags each carrying a 32-bit little-endian value. It is the job's operating
envelope, and the factory firmware treats a well-formed one as a precondition
for cutting at all. Of the tags it knows, 346 are accepted in a header and 29
are mandatory; a header missing any one of the 29 is refused outright, and a
known tag that is not header-legal is refused too. An unrecognized tag is only
logged and skipped, which is why a newer service can talk to an older machine.

The header is not a set of echoes. Roughly two thirds of the fields come back
holding whatever the machine last reported, but the service substitutes real
operating values for the ones that matter: fan duties, per-sensor temperature
ceilings, lid IR flame thresholds, head accelerometer limits and a high-voltage
current cap all arrive filled in per job.

ForgeFIRM applies thirteen of them, and only those:

| Tags | Applied to |
|---|---|
| `AArd`, `EFrd`, `IFrd` | run-phase fan duties, handed to the forgectrl cooling engine as the per-job profile |
| `STfr` | step frequency |
| `XSrc`, `YSrc` | stepper current while running |
| `XShc`, `YShc` | stepper current while idle |
| `XSdm`, `YSdm` / `XSmm`, `YSmm` | decay mode / microstep mode |
| `ZSmd` | Z mode |

Fan duties are honored on the scale the service uses: air assist 0 to 1023,
exhaust and intake 0 to 65535.

Everything else is dropped. Some of that is deliberate. Thermal policy belongs
to the cooling engine, which runs its own coolant ceiling, flow verification,
emission witness and silence timeout, and a machine should not let a remote
service raise its own limits. The rest is not deliberate, and the specifics are
in the outstanding items below.

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
- `tested_against_gf` is `FACTORY_FIRMWARE.FW_VERSION` from the client's
  configuration — the factory firmware version this build advertises as
  `MCov` and was tested against. There is no separate release-side field:
  the value travels config → `gf-latest.json` → forgectrl's status (`gfsvc`)
  → the panel banner.

## Configuration

| Where | Keys |
|---|---|
| `/data/etc/gfhome.conf` (seeded from `/etc/gfhome.conf.sample`) | `SERVICE.*` (server/status URLs), `FACTORY_FIRMWARE.CHECK` / `STATUS_FILE`, `FORGECTRL.URL`, `LOGGING.SAVE_PULS` / `SAVE_SENT_IMAGES` (both default off) and `LOGGING.CAPTURE_DIR` (default `/data/forgefirm/captures/<app>`), `MOTION.*`, `THERMAL.*`. |
| `/data/forgefirm.conf` (managed from the forgectrl UI) | `controller_mode` (`grbl` / `cloud` — read by the forgectrl supervisor, which spawns exactly one controller at boot and on every mode switch; the init scripts defer to it), `homing_mode`, identity overrides `gf_serial` / `gf_password` (a serial override re-derives the hostname), and the log levels `log_gfcloud_disk` / `log_gfcloud_remote` and `log_gfhome_*` (each `off`..`debug`; read at process start, so applied at reboot). |

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
- **8 MP ("HD") machines:** an OV8856 machine captures 3264x2448, not the
  2592x1944 a 5 MP machine sends. Whether the service accepts a larger image
  for bed alignment and focus analysis is unknown — no 8 MP machine has been
  on the bench.
- **Pulse-header envelope not enforced:** nineteen of the twenty-nine header
  fields the factory requires are read and discarded. None of them can put
  energy anywhere (the hardware chain is the emission boundary and the header
  never touches it), but each one the factory arms per job is a failure mode it
  watches and ForgeFIRM does not:
  - `AArn`/`AArx`, `EFrn`/`EFrx`, `IFrn`/`IFrx` are the run-phase tach windows
    for air assist, exhaust and intake. ForgeFIRM reads every tach for the
    dashboard and gates on none of them, so a fan that stalls mid-cut goes
    unnoticed. The exhaust one is the fume path and is the first worth closing.
    These particular fields are among the ones the service echoes rather than
    fills: every captured header carries zero for all of them except `AArx`,
    so a working gate needs locally configured thresholds and can only let a
    header value tighten them. The factory's own policy is known even though
    its numbers are not: a fan tach alert during a cut **pauses** the print,
    taking the same transition a user pause takes. Two of the factory's three
    tach monitors cannot fire at all, because they treat a zero limit as "not
    configured", so on a factory machine a stalled extraction fan is caught by
    the temperature it causes rather than by its tachometer.
  - `PTmn`/`PTmx` cap the power-supply temperature. Only the coolant loop gates
    today; board, head, interconnect, lid, fused and supply temperatures are
    displayed and gate nothing, though the service sends real ceilings for all
    of them. The factory runs two tiers on these: a plain temperature alert
    pauses the print, and a `*_temp_critical` fails the machine outright. Watch
    the units before adopting a ceiling, because they are per sensor rather
    than universal. The coolant family is the worked example, carried twice in
    parallel, once in raw ADC counts (`CT{i,w,r}{n,x}`, where "min" is the hot
    end because the thermistors are NTCs) and once in millidegrees
    (`CM{i,w,r}{n,x}`, with `CMhl`/`CMhu` hysteresis).
  - `MCsn` is the machine serial. The factory refuses a pulse file whose serial
    does not match; ForgeFIRM runs it.
  - `PDfm` declares the pulse-data format. The factory made it mandatory so it
    could refuse a stream it does not understand; ForgeFIRM assumes the format.
  - The nine warmup-phase fan fields (`AAw*`, `EFw*`, `IFw*`) are ignored, so a
    job's requested warmup airflow is not applied.

  Beyond the required set, the header also carries head accelerometer run
  thresholds (`HAxr`, `HAyr`, `HAar`), lid IR flame thresholds and baselines
  (`IRwx`/`IRwc`, `IRxx`/`IRxc`, `IR?b`) and a high-voltage current cap
  (`HIix`/`HIrx`). None has a ForgeFIRM counterpart: the head accelerometer is
  used only as a motion-liveness probe, the IR fire watch ships disabled, and
  beam detect is not read at all. In the factory these are graded rather than
  uniform: an accelerometer or beam-detect *alert* pauses the print, the
  matching *abort* aborts it, and the high-voltage current cap is in the
  machine-unusable set alongside the critical temperatures.

  One place the header is not the model to follow is the coolant loop. The
  header carries a whole `CF` family for the factory's calorimetric flow
  controller, with a setpoint, PI gains and a differential-temperature readout,
  and none of it is ever sent. A stock machine reports none of those settings,
  and the three faults the controller would raise have no raiser in the factory
  image, so the factory watches coolant temperature and does not verify flow.
  ForgeFIRM's flow verification is ahead of the factory here, not behind it.

  Most of this is not the cloud client's to fix. Reading a header field is one
  line here; enforcing a tach window or a temperature ceiling belongs to the
  machine-services daemon, which owns the thermal hardware and publishes the
  fire verdict, and it has to hold in GRBL mode too. The client's share is the
  two refusals that are purely about the file it was handed, `MCsn` and `PDfm`,
  and passing the rest of the envelope through to the engine rather than
  dropping it on the floor. A limit that arrives from a remote service is a
  limit that service can raise, so the shape to aim for is a header value that
  can only tighten a locally configured ceiling, never loosen it.

- **Pause constants from the job:** the pulse header's `CCbp` / `CCbt` keys
  are the likely factory source of the backtrack and resume-lead counts; once
  a captured header confirms it, prefer them over the `forgefirm.conf` values.
  The `CC` family is header-only (it carries the job rather than the machine's
  configuration) and its sources are unnamed in the factory firmware, so the
  meanings have to come from observation rather than from reading.
- **Coolant control per job:** the forgectrl cooling engine holds the pump
  on as part of its idle posture; the `WPon` pulse-header key has no
  applier — if per-job pump control is ever wanted, it belongs in the
  engine's per-job profile (the `/cool/state` report), not here.
- **Optional:** SPKI pinning to match the factory client; emulator
  (`gf-machine-emulator`) full-session parity.
