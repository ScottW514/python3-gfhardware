# ForgeFIRM Cloud Mode

ForgeFIRM's optional **factory cloud mode** runs the machine under the Glowforge
web service: the machine presents itself as a stock Glowforge, and the
Glowforge phone/web app drives it end to end: connect homing, Set Focus,
material imaging, and full prints (button press, cut, return-home). It is
distinct from the `gfhome.py` one-shot, which borrows the service only for a
camera-referenced homing cycle.

The Glowforge protocol is undocumented and can change without notice. Each
ForgeFIRM release validates cloud mode against one specific factory
service/firmware version, the version it advertises as `MCov` (currently
`2.6.0-2228`). forgectrl surfaces a compatibility warning when the live service
moves past that baseline (see "Firmware-update policy").

## Components

| Piece | Role |
|---|---|
| `gfcloud.py` (`/usr/sbin`) | Full cloud-mode controller daemon. Spawned and supervised by forgectrl when `controller_mode = cloud` (the init script defers to the supervisor and remains a manual stop only); the pulse device arrives as a broker-inherited fd (`GF_PULSE_FD`) that is never closed, so job boundaries and mode switches do not cycle the 40 V rail. SIGTERM stops the service loop, safes the hardware, and exits. |
| `gfhome.py` (`/usr/sbin`) | One-shot service-driven homing. Invoked for `$H` when `homing_mode = gfcloud`; dispatches with `allow_print=False` so a print can never run inside a homing session. Completion is guarded: a run of near-identical service corrections aborts (the machine is not physically moving), and quiet only counts as homed when the head accelerometer witnessed real motion during the session. |
| `ffmachine.py` (site-packages) | Shared hardware-machine glue: identity overrides from the shared config, and the forgectrl-routed capture machine both clients use. |
| `gfutilities` | Protocol/service layer: auth, WebSocket client, action dispatch, settings report, pulse-file handling. |
| `gfhardware` | The hardware `Machine`: motion, laser latch, switches, cameras. Thermal hardware belongs to the forgectrl cooling engine: the cloud client reports job state (`POST /cool/state`, with the pulse header's run fan duties as the per-job profile) and enforces the published verdict on its fire path, gaining the flow verification and over-temp protection the engine provides. |

Camera captures route through forgectrl's snapshot endpoint (it owns the
imx-media pipeline whenever a stream is open; the snapshot works during an
active stream and takes a per-shot lamp override), with direct V4L2 capture as
the fallback when the daemon is unreachable.

**The cameras only capture with the lid closed.** This is a privacy rule, not a
factory behavior: the lid camera faces the room once the lid is raised, and the
service asks for images on its own schedule. Both capture paths enforce it
(forgectrl answers `409` and the direct fallback raises `gfhardware.cam.LidOpen`)
and the check fails closed, so an unreadable lid also refuses. There is no
setting to disable it. A refused image action is reported to the service as
`<action>:failed` so it resolves rather than hanging, and the client does not
fall back to a direct grab that would refuse identically. **Consequence:** the
factory ran focus hunts with the lid open and a hunt includes a head capture,
so a hunt attempted with the lid open now fails; the lid must be shut before
the app focuses or prints.

## Scope: telemetry is excluded

The factory streams continuous telemetry; ForgeFIRM does not and will not.
Excluded channels:

- `POST /api/sensor`, the binary sensor firehose.
- WSS `type:"log"` messages, the in-band advisory logs.
- The `fault:*` / `estop:*` / `interlock:*` **cloud reporting** namespace.
  (Local fault-to-safe handling is independent of reporting and fully active.)

On-demand requests are still answered: the `settings` report, image captures,
and the functional action handshake.

**One exception, deliberately kept: the progress frame.** A capture of the
factory's own cloud session showed that the app's progress bar rides a WSS
`type:"progress"` frame the machine sends every 30 s during a job (below,
"Progress reporting"). That is a UI status update, not the sensor telemetry
this section excludes, so ForgeFIRM carries it. The bar is the operator's only
sign a multi-hour print is advancing; going dark for hours is not a scope we
want. Everything else above stays excluded, including inside that frame: of
the fifteen periodic tags the factory packs into it, ForgeFIRM fills the five
that describe the job (`CAid`, `CCbp`, `CCst`, `CCxp`, `CCyp`) and leaves the
temperatures and the IR readings out. The service already has this machine's
settings report; it does not get a sensor feed by the side door.

## Connection and authentication

- `sign_in` returns two JWTs: `auth_token` (Bearer, ~6 h) and `ws_token` (a
  path component of the WS URL, ~30 s expiry, single-use).
- The WS client reconnects through a loop that re-runs `sign_in` for a fresh
  `ws_token` and rebuilds the URL on every reconnect.
- The service drops the socket on its own schedule, roughly hourly in a long
  idle session. That is routine: the reconnect loop re-authenticates with a
  fresh `auth_token` and the machine stays signed in and serving actions
  without operator involvement.
- Any HTTP request that gets a 401 re-signs-in and replays once (the sign-in
  request itself never retries, so there is no recursion).
- `ws_connect()` returns the running client; `GFUIService` stops it (flush
  final events, close socket, join) when the session ends. The WS client and
  action threads are daemon threads: nothing about a session can keep the
  process alive after the service loop exits.
- TLS: standard certificate validation. The factory additionally pins the
  server SPKI and refuses unpinned connections; ForgeFIRM does not pin, by
  decision. A pin is a copy of a key the service can rotate whenever it
  likes, and chain validation already gives interoperability everything it
  needs.

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
Parser note: an event's `<action>` prefix segment can be empty; never assume
`split(':')[0]` is non-empty.

## Actions

The 2.6.0 vocabulary is twelve `action_type` values, and they are handled
through a single dispatch table shared by `gfcloud` and `gfhome`:

| action_type | Behavior |
|---|---|
| `settings` | Sends the on-demand ~600-key settings report. Machine IP, firmware/app version, and serial reach the service through this report (`MCip`, `MCov`, `MCdv`, `MCsn`); there is no separate status endpoint. |
| `hunt` | Focus-lens homing (Z home + the service's hunt pattern + home offset). |
| `motion` | Downloads and runs the pulse file at `motion_url`. |
| `print` | Full print lifecycle (gfcloud only; gfhome refuses prints). |
| `lid_image` | Lid-camera capture + upload. |
| `head_image` | Head-camera capture + upload. |
| `lidar_image` | Head captures with the distance-measuring laser (per-shot settings arrive as a list). |
| `user_image` | User-requested snapshot: a lid-camera capture, lid closed. Same as `lid_image` in the factory too, name apart. |
| `factory_reset` | Refused with `factory_reset:failed` and **never acted on** - a cloud command must not wipe a ForgeFIRM machine. |
| `update_check` | Answered with `update_check:completed`; see the firmware policy below. |
| `head_firmware_update` | Refused with `head_firmware_update:failed` - a cloud command does not flash the laser head. |
| `focus` | Ignored, as the factory application ignores it: its own dispatch has no case for the name. |
| unknown | Ignored. |

Only a `ready` status is a request to do anything. The three answered on the
protocol thread (`settings`, `update_check`, `factory_reset`,
`head_firmware_update`) act on `ready` alone: the service also sends a
`cancelled` for actions a machine never received, and the factory ignores
those rather than answering them. The job actions are deliberately not gated
that way, because a `cancelled` is how a print in flight is stopped.

### What the three unprompted actions do in the factory

None of these has ever been seen on the wire here, so they were read out of
the 2.6.0 binary rather than observed. All three turn out to be hand-offs to
programs ForgeFIRM does not have:

- **`update_check` checks nothing.** The handler writes `'u'` to the runit
  control fifo `/var/run/svs/glowforge-updater/control` and reports
  `:completed`, or `:failed` if that write fails; on a service-sent failure or
  cancel it writes `'d'` to stop the service again. Everything an update means
  happens in that separate daemon. The application carries no update endpoint
  at all, and a cut refuses to start while the updater holds its lock.
- **`factory_reset` replaces the application with a script.** The action posts
  a command to the hardware task, which tells runit not to restart the
  application and then `execl`s `/usr/bin/factory_reset.sh`, passing `reboot`
  when the request's flag asks for one.
- **`head_firmware_update` flashes the laser head.** It takes
  `head_firmware_filename` from the request, reads it out of
  `/glowforge/fw/head/` and runs `/usr/bin/head-update.sh`.

The two refusals report `:failed` rather than `:cancelled` on purpose. In this
protocol a cancel is what the service says when it withdraws an action;
failure is what a machine says when the thing did not happen, and it is the
factory's own report when its reset script cannot be launched.

### Per-action settings

Actions carry a sparse `settings` dict (for lidar, a list of dicts), normalized
and made available to every image handler. Policy per key:

- **Honored:** `HCil` (head illumination) and `LCfl` (lid flash) are lighting
  the capture path can apply directly.
- **Deliberately not applied:** `HCex`/`HCga`/`HCae`/`HCag`: the factory-scale
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

Head images are captured with the white torch off, because added white light washes
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
- Unsolicited `lid:opened` / `lid:closed`, which drive the app's header state
  and trigger an immediate service `lid_image` refresh.
- Alongside the events, a running print sends the `type:"progress"` frame
  described below. It is a different message type, not an event.

Service behavior worth knowing:

- After any mid-job abort the service re-hunts; after a completed print it
  issues a `lid_image` and a Z re-hunt.
- **The service dead-reckons machine position.** The return-to-home park runs
  after every print, finished or aborted, and ignores the lid and the cancel
  flag while it runs (the factory parks with the lid open, and a park cut short
  would offset every subsequent motion until the next camera re-home);
  `print:return_to_home:succeeded` is sent only when the park actually
  completed. A job refused before it moved (lid or interlock open at start,
  a backstop, since the app itself will not print until the lid is closed and imaged)
  ends `:cancelled`, never `:completed`.
- Server-side session state can be sticky: after abnormal session deaths the
  service may stall silently mid-sequence in the next session. A fresh WS
  session recovers it.

### Progress reporting

The app's progress bar rides one carrier, settled by a capture of the
factory's own session:

- **The carrier is an outbound WSS `type:"progress"` frame**, machine to
  service, and nothing else. No `<action>:progress` event, and no
  `progress_bytes` query on the action endpoint, appeared in a full session.
- **The progress frame is the periodic settings report.** Its `settings.values`
  block is exactly `periodic_settings_tags` (`BTvl CAid CCbp CCst CCxp CCyp
  CMet FTvl HTvl IRva IRvb IRvc IRvd ITvl LTvl`): board / fused / head /
  interconnect / lid temperatures, four IR values, a camera id, a coolant
  value, the byte position `CCbp`, the state `CCst`, and X/Y position
  `CCxp`/`CCyp`. Progress and the periodic telemetry are one message, which is
  why carrying it is cheap: the client already builds the ~600-key settings
  report on demand.

  ```json
  {"id":359,"type":"progress","version":1,"action_id":1577564802,
   "progress":"print:progress","current":994,"units":"steps","total":33291208,
   "settings":{"values":{"CCbp":1009,"CCst":1,"CCxp":0,"CCyp":0, ...}}}
  ```

- **Cadence is 30 s** (`progress_update_interval_ms` = 30000), plus a burst at
  every phase transition. During a cut `current` advances at the step
  frequency; at the phase boundaries the frame is `<action>:download`,
  `<action>:upload`, etc., with `current` in bytes.
- **`total` is bytes enqueued, not the job.** In the captured print it grew
  33,291,208 → 33,553,352 → 33,815,496 in steps of 262,144 (256 KiB per
  interval): the factory live-appending to its ring, on the wire. A progress
  report of `current/total` therefore divides by a denominator that is itself
  growing.
- `CCbp` reads the byte position (1009 against `current` 994 in the frame
  above). It is telemetry, not a job parameter: the factory's own tag table
  marks `CCbp` and `CCbt` report-only, so neither can appear in a pulse
  header at all, and the pause constants stay in `forgefirm.conf`.

**What ForgeFIRM sends.** The same frame, at the same 30 s cadence, forced at
every phase change: the run's start, each pause and resume, each hold for a
stalled feed, and once more where the job ends. A print is the only action
that reports, which is what the factory does too, and its park reports under
the print as its last leg. `current` is the byte position the kernel has
played, so it steps back after a pause backtracks, exactly as the factory's
does. A pause is reported as the two events `print:paused` and
`print:resumed`; the factory's ten-event pause phase machine
(`print:pausing_decel`, `print:pausing_backtrack`, `print:paused`,
`print:resuming`, each with `:starting`/`:succeeded`) is advisory and is
not mirrored, and neither are its transfer-phase frames
(`<action>:download`, `<action>:upload`), since the `:starting` event
already says the same thing.

The denominator is the one place ForgeFIRM deliberately does better. The job's
length is known before a byte plays: a plain body carries it in its size and a
compressed one in the gzip ISIZE trailer, so the whole job never has to be
inflated to learn it. That figure is frozen when the run starts and every
frame divides by it, which is why the bar means what it says under a live feed
where the kernel's own byte total is still climbing. A job that plays past its
declared length reports complete rather than overshooting, and says so in the
log once. The length is named in the log at the start of every job, and a job
that does not end where it said it would is named there too.

## Image upload

Image actions carry a presigned `endpoint` URL; the image is `PUT` there as a
plain request (the presigned URL carries its own auth; a Bearer header makes
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
  - **a live feed that wedges holds the job the same way.** A feeder with room
    in the ring in front of it and no progress behind it is not feeding, and
    left alone it ends one way: the ring plays out what it holds, tens of
    minutes at the print tick, and then goes dry, which is an underrun, a
    position no longer trusted and a job that cannot be picked back up. Thirty
    seconds of that is enough to stop the machine cleanly and retrace, which
    is done while there is still history to retrace over. If the feed moves
    again within a minute the job resumes over ground it already cut, seam
    hidden, `print:paused` and `print:resumed` reporting it exactly as the
    button pause does; if it does not, the job is cancelled rather than left
    stopped in the material. A feed that stalls repeatedly (three holds) is
    cancelled rather than cut in pieces, and a full ring is never mistaken for
    a stall. A press during a hold is not lost: pausing a stopped job is not a
    thing the machine can do, so the press is read once the job is moving
    again.
- Post-action cleanup always locks the laser latch and drops the pulse-device
  registration, including when an action crashes.
- A job larger than the ring runs anyway: the client holds the compressed body
  in memory, fills the ring before the button is asked for, and tops it up as
  it drains, so the ring is a window onto the job rather than the place the job
  lives. The ring size caps how much of a job is buffered at once (~56 min at
  the print tick), not how long a job may be.
- What does have a ceiling is the memory that body sits in. The client refuses
  a job whose declared length is past `pulse_reject_threshold_bytes`, before it
  takes a byte, and abandons a download that runs past it whatever was
  declared, because a service that declares nothing (or declares wrongly) must
  not be handed all the memory there is. A refusal is logged as `refusing the
  job:` with the size and the limit, so it reads differently from a transport
  failure, and the print is reported `:cancelled` like any job that never
  moved. `pulse_warn_threshold_bytes` only logs. Both are memory guards and
  neither is a ring guard: they say nothing about how long a job may be.
  The defaults (32 MiB warn, 128 MiB refuse) are reasoned rather than
  measured, because at the compression the service actually uses 128 MiB
  of body is days of cutting and nothing has come near it; every job logs
  the body it arrived as and the program it played, so the ratio the
  guards are sized against is on record should a job ever get close.

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

Five more are passed to the engine as the job's limits, riding every
`POST /cool/state` while the job is loaded: `CMrx` and `CMrn` (the coolant
run window, millidegrees, sent as degrees) and `EFrx`, `IFrx`, `AArx` (the
tach maximum periods, sent as the minimum speed each means in the kernel's
units). A tag that is absent, a sentinel (0, 1023, the signed extremes, the
unsigned rail) or absurd is dropped and the engine's own limit stands. The
engine applies each only where it is stricter than its configured value,
never looser, and never to a gate the operator turned off; the coolant
ceiling and the fan floors (the airflow gates) are the consumers. The job logs what it derived (`job limits from the header:
...`) and the engine logs what it resolved (`effective limits: ...`). The
tach minimum periods (`AArn`, `EFrn`, `IFrn`) are maximum speeds, which
nothing gates on; they are read so a header carrying them is not counted
as a gap.

Two more are checked rather than applied, before a byte reaches the ring:
`MCsn`, the serial the job is locked to, and `PDfm`, the pulse-data format.
A job addressed to another machine, or carrying a format the step decoder
does not know, is refused with the reason in the log (`refusing the job:`)
and reported `:cancelled` like any job that never moved. Four lifecycle
keys (`CFrh`, `CCwp`, `CCrp`, `CCup`) are named in the log of every job and
drive nothing, which is what the factory does with them too (below, "A
print's warm-up and its rest").

Everything else is dropped, and not silently: every job logs how many of
its header keys have no applier here, and names them with their values at
debug level. A key the machine does not act on is a decision, and a
decision that leaves no trace is indistinguishable from an oversight, so
each job is its own record of what the service asked for and this machine
ignored.

Some of the dropping is deliberate. Thermal policy belongs to the cooling
engine, which runs its own coolant ceiling, flow verification, emission witness
and silence timeout, and a machine should not let a remote service raise its own
limits. The rest is not deliberate, and the specifics are in the outstanding
items below.

## A print's warm-up and its rest

The factory holds twice around a print, and so does ForgeFIRM. Measured on
a factory slot: **3.05 s** between configuring the run and starting it, and
about **10.35 s** of rest after the park before the machine goes idle. A
motion or a hunt gets neither.

Both are equipment protection rather than ceremony. The warm-up is what gets
air and coolant moving before the first fire; the rest is what purges the
enclosure and the tube after the last one. The service assumes both have
happened, so a machine that skips them is running hardware nobody looked
after.

`MOTION.WARM_UP_DELAY` and `MOTION.COOL_DOWN_DELAY` carry the seconds and
default to the factory's measurements. 0 skips either, deliberately, and a
skipped period says so in the log rather than passing in silence: a config
that carries explicit zeros keeps them until someone changes them.

The pulse header looks like the source of these periods and is not. Every
captured print header carries `CCwp` 5000 and `CCrp` 10000, with `CFrh` for
the park, and a motion or a hunt carries none of them; the correlation is
real and the causation is not. In the 2.6.0 application all four lifecycle
keys (`CCrp`, `CCup`, `CCwp`, `CFrh`) are parsed, stored, copied into the
settings batch and acted on by nothing: a tag reaches behavior either
through a peripheral that registers it against a source or through an
inlined lookup by index, and these four have neither. The factory's warm-up
and rest come from somewhere other than the job, so configured periods
defaulted to what the factory was measured doing are the right model rather
than a placeholder. The keys stay in the per-job log line as a record of
what the service sends.

## Firmware-update policy

ForgeFIRM **never downloads or installs factory firmware.**

- The `update_check` action is answered with `update_check:completed`, which
  is what a factory machine sends once it has started its updater, without
  the hand-off that would install a factory image over ForgeFIRM. A version
  probe that fails answers `update_check:failed`, the one honest failure
  here: the check itself did not happen.
- On connect (when `FACTORY_FIRMWARE.CHECK` is set) a read-only
  `GET /update/current` probe records
  `{latest_gf_version, tested_against_gf, checked_at}` to
  `FACTORY_FIRMWARE.STATUS_FILE` (`/data/forgefirm/gf-latest.json`).
- forgectrl reads that file and shows a **compatibility warning** whenever the
  live service has moved past the tested baseline, regardless of whether a
  newer ForgeFIRM exists, plus an **upgrade recommendation** only when one
  does. The factory `.fw` is never offered.
- `tested_against_gf` is `FACTORY_FIRMWARE.FW_VERSION` from the client's
  configuration: the factory firmware version this build advertises as
  `MCov` and was tested against. There is no separate release-side field:
  the value travels config → `gf-latest.json` → forgectrl's status (`gfsvc`)
  → the panel banner.
- One caveat on that probe: `/update/current` is inherited from the 1.x-era
  client and appears nowhere in the 2.6.0 application, which reaches its
  updater through the service action instead. The endpoint answering is an
  assumption, not something the current factory firmware demonstrates.

## Configuration

| Where | Keys |
|---|---|
| `/data/etc/gfhome.conf` (seeded from `/etc/gfhome.conf.sample`) | `SERVICE.*` (server/status URLs), `FACTORY_FIRMWARE.CHECK` / `STATUS_FILE`, `FORGECTRL.URL`, `LOGGING.SAVE_PULS` / `SAVE_SENT_IMAGES` (both default off) and `LOGGING.CAPTURE_DIR` (default `/data/forgefirm/captures/<app>`), `MOTION.*`, `THERMAL.*`. |
| `/data/forgefirm.conf` (managed from the forgectrl UI) | `controller_mode` (`grbl` / `cloud`, read by the forgectrl supervisor, which spawns exactly one controller at boot and on every mode switch; the init scripts defer to it), `homing_mode`, identity overrides `gf_serial` / `gf_password` (a serial override re-derives the hostname), the pause pair `cloud_pause_backtrack_ticks` / `cloud_resume_lead_ticks`, the download guards `pulse_warn_threshold_bytes` / `pulse_reject_threshold_bytes` (bytes of compressed body held in memory, unset = 32 MiB warn and 128 MiB refuse, 0 lifts either), and the log levels `log_gfcloud_disk` / `log_gfcloud_remote` and `log_gfhome_*` (each `off`..`debug`; read at process start, so applied at reboot). |

## Outstanding items

Everything the bench can exercise is exercised: the `cloud.*` acceptance
tests cover mode switching, service homing, the lid and interlock aborts,
the button-wait cancel, a hunt with the lid open, pause and resume, the
paused and running cancel paths, and a print longer than the ring fed from
the live service. What is left is below.

- **8 MP ("HD") machines:** an OV8856 machine captures 3264x2448, not the
  2592x1944 a 5 MP machine sends. Whether the service accepts a larger image
  for bed alignment and focus analysis is unknown; no 8 MP machine has been
  on the bench.
- **Pulse-header envelope not enforced:** seventeen of the twenty-nine header
  fields the factory requires are read and discarded. None of them can put
  energy anywhere (the hardware chain is the emission boundary and the header
  never touches it), but each one the factory arms per job is a failure mode it
  watches and ForgeFIRM does not.

  The whole header has now been swept rather than sampled, so the size of this
  is known. Of the 346 tags a pulse header may carry, ForgeFIRM applies 13,
  refuses the file on 2, and names 4 in the job log. The factory binds 283 of
  them to a source and applies them generically, and four have no consumer
  anywhere in the factory either. That leaves 270 tags bound there and not
  applied here, which sounds like a mountain and is not: 20 configure the
  client's own network backoff, where ForgeFIRM has its own policy; 39 belong
  to the three fans of an air filter most machines do not have; the camera
  families are the exposure and gain values deliberately not applied because
  the mainline driver's units differ; and 42 accelerometer and 54 temperature
  entries are mostly per-phase idle and warm-up variants of the same few
  limits. What actually matters is the list below, and it is the reason this
  item exists:
  - ~~`AArn`/`AArx`, `EFrn`/`EFrx`, `IFrn`/`IFrx`, the run-phase tach windows~~
    **closed by the airflow gates:** the engine holds every fan to a locally
    configured floor while the run profile is applied, and a header window
    can only raise a floor for its job. The captured headers carry zero for
    every window except `AArx`, so in practice the local floors are the
    gate. Stricter than the factory by decision: a stalled fan is a fault
    with no resume, where the factory pauses, and the factory's own intake
    and exhaust monitors cannot fire at all (a zero limit reads as "not
    configured" there).
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

  None of this is the cloud client's to fix. Its share is done: `MCsn` and
  `PDfm` are refused before a byte is loaded, and the coolant window and the
  tach windows reach the engine with every report as limits that can only
  tighten a locally configured one (above, "The pulse header"). Enforcing a
  tach floor or a temperature ceiling belongs to the machine-services
  daemon, which owns the thermal hardware and publishes the fire verdict,
  and it has to hold in GRBL mode too.

- **Coolant control per job:** the forgectrl cooling engine holds the pump
  on as part of its idle posture; the `WPon` pulse-header key has no
  applier. If per-job pump control is ever wanted, it belongs in the
  engine's per-job profile (the `/cool/state` report), not here.
