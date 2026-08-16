"""
(C) Copyright 2020
Scott Wiederhold, s.e.wiederhold@gmail.com
https://community.openglow.org
SPDX-License-Identifier:    MIT
"""
import fcntl
import logging
import os
from threading import Event
from time import monotonic, sleep
from typing import Union

from gfutilities import BaseMachine
from gfutilities.configuration import get_cfg, set_cfg
from gfutilities.puls import generate_linear_puls
from gfutilities.service.websocket import load_motion, img_upload, send_wss_event
from gfutilities.device.settings import MACHINE_SETTINGS, update_settings

from gfhardware import id
from gfhardware._common import *
from gfhardware.cnc import *
from gfhardware.cooling import *
from gfhardware.coolsvc import cooling_svc
from gfhardware.leds import *
from gfhardware.switches import *
from gfhardware.z_axis import ZAxis

from gfhardware import cam

logger = logging.getLogger(LOGGER_NAME)

# Under the forgectrl device broker the pulse device arrives as an
# inherited fd (GF_PULSE_FD): the broker holds /dev/glowforge open for
# its lifetime, so this process must never close it - handovers and
# job boundaries no longer cycle the 40 V rail, and the broker (not
# the kernel close) is the dead-man for a writer crash. Standalone,
# the per-job open/flock/close below keeps the original semantics.
_pulse_stream = None


def _inherited_pulse_dev():
    global _pulse_stream
    fd = os.getenv('GF_PULSE_FD')
    if fd is None:
        return None
    if _pulse_stream is None:
        _pulse_stream = os.fdopen(int(fd), 'wb', buffering=0)
    return _pulse_stream


# The shared machine config: the same trivial "key = value" file the
# GRBL controller and forgectrl read, so both controller modes honor
# the same operator-facing tunables.
MACHINE_CONF = os.environ.get('GFHOME_CONF', '/data/forgefirm.conf')


def _conf_float(key: str, default: float) -> float:
    try:
        with open(MACHINE_CONF) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                if k.strip() == key:
                    try:
                        default = float(v.strip())  # last occurrence wins
                    except ValueError:
                        pass
    except OSError:
        pass
    return default


class Machine(BaseMachine):
    """
    Operates the GF Hardware
    See parent class for method documentation
    """

    def __init__(self):
        # The thermal hardware (fans, pump, TEC, heater) is owned by the
        # forgectrl cooling engine; the pulse header's RUN fan duties go
        # to it as the per-job profile in the /cool/state reports. The
        # idle/cool-down duty keys are not mapped - those profiles are
        # the engine's.
        update_settings({
            'AArd': {'run': cooling_svc.profile_air_assist},
            'EFrd': {'run': cooling_svc.profile_exhaust},
            'IFrd': {'run': cooling_svc.profile_intake},
            'STfr': {'run': cnc.set_step_freq},
            'XSdm': {'run': cnc.set_x_decay},
            "XShc": {'idle': cnc.set_x_current},
            'XSmm': {'run': cnc.set_x_mode},
            'XSrc': {'run': cnc.set_x_current},
            'YSdm': {'run': cnc.set_y_decay},
            "YShc": {'idle': cnc.set_y_current},
            'YSmm': {'run': cnc.set_y_mode},
            'YSrc': {'run': cnc.set_y_current},
            'ZSmd': {'run': ZAxis.set_mode_from_puls},
        })

        self._button_pressed: bool = False
        self._motion_stats: dict = {}
        self._sw_thread: SwitchMonitor = SwitchMonitor(SWITCH_DEVICE, self._switch_event)
        # Edge-to-run-loop signaling. The switch thread flags edges and
        # wakes the run loop; the run loop (the one owner of every cnc
        # write during a job) reacts on the wake instead of at its next
        # 100 ms tick, so a lid open reaches cnc/stop within a few ms.
        # _button_edges counts presses seen while a job runs (the pause /
        # resume toggle); _enclosure_edge latches a lid or interlock open
        # seen by the edge thread until the run loop consumes it.
        self._run_wake: Event = Event()
        self._button_edges: int = 0
        self._enclosure_edge: bool = False

        set_cfg('MACHINE.HEAD_FIRMWARE', self.head_info().version, True)
        set_cfg('MACHINE.HEAD_ID', self.head_info().hardware_id, True)
        set_cfg('MACHINE.HEAD_SERIAL', self.head_info().hardware_id, True)

        set_cfg('MACHINE.SERIAL', id.serial(), True)
        set_cfg('MACHINE.HOSTNAME', id.hostname(), True)
        set_cfg('MACHINE.PASSWORD', id.password(), True)

        BaseMachine.__init__(self)

    # Switch polarity, everywhere below: truthy = circuit closed / OK for
    # the lid (SW_DOORS, the series chain the hardware safety chain itself
    # uses); the remote-interlock loop (SW_INTERLOCK) has the INVERTED
    # sense - it reads active only when the loop is OPEN (Basic/Plus ship
    # the 2-pin connector factory-jumpered, so it reads inactive =
    # satisfied there). SW_HV_ENABLE is the readback of the chain's
    # HV_ENABLE output and gates nothing. The hardware chain kills the
    # BEAM on lid or interlock by itself; what the checks below decide is
    # what MOTION and the job do.
    @staticmethod
    def _enclosure_open(switches: dict) -> Union[str, None]:
        """'lid opened' / 'interlock opened' when the enclosure is not
        safe, else None."""
        if not switches[InputSwitch.SW_DOORS]:
            return 'lid opened'
        if switches[InputSwitch.SW_INTERLOCK]:
            return 'interlock opened'
        return None

    def _button_wait(self, msg: dict) -> None:
        # The wait runs with the latch already unlocked, so it is
        # bounded and supervised the same way GRBL mode's arm window is:
        # the shared laser_button_timeout_s (default 300 s, clamped to
        # 1-3600 - out-of-range values fall back, never wait-forever)
        # bounds it, and an opened lid or interlock loop ends it (the
        # hardware button latch would ignore the press anyway). Timeout,
        # lid, interlock, and cloud cancel all relock the latch and disarm
        # before returning. Wakes on switch edges, so a press or a lid
        # open is seen within milliseconds.
        timeout_s = _conf_float('laser_button_timeout_s', 300.0)
        if not 1.0 <= timeout_s <= 3600.0:
            timeout_s = 300.0
        deadline = monotonic() + timeout_s
        self._button_pressed = False
        self._run_wake.clear()
        set_button_color(ButtonColor.WHITE)
        logger.info('waiting for button')
        abort = None
        while True:
            reason = self._enclosure_open(self._sw_thread.all_switches())
            if self._running_action_cancelled:
                abort = 'cancelled'
                break
            if reason is not None:
                abort = reason
                break
            if self._button_pressed:
                break
            if monotonic() > deadline:
                abort = 'timed out'
                break
            self._run_wake.wait(.1)
            self._run_wake.clear()
        if abort is not None:
            logger.warning('button wait %s - relocking the laser', abort)
            cnc.laser_latch(1)
            cooling_svc.set_armed(False)
            self._running_action_cancelled = True
            set_button_color(ButtonColor.OFF)

    @staticmethod
    def _config_from_pulse(state: str, header: dict):
        # Header values come from the service and go straight to motion
        # hardware (step frequency, stepper currents, microstep/decay
        # modes, fan duties): clamp each to its declared bounds before
        # applying.
        for key, setting in MACHINE_SETTINGS.items():
            val = header.get(key, None)
            if val is not None:
                func = getattr(setting, state)
                if func is not None:
                    if setting.min_value is not None and val < setting.min_value:
                        logger.warning('pulse header %s=%r below %s; clamped',
                                       key, val, setting.min_value)
                        val = setting.min_value
                    if setting.max_value is not None and val > setting.max_value:
                        logger.warning('pulse header %s=%r above %s; clamped',
                                       key, val, setting.max_value)
                        val = setting.max_value
                    func(val)

    def _head_image(self, msg: dict, settings: dict = None) -> None:
        logger.info('capturing Head Image')
        # settings is None for plain head-image requests (only lidar/hunt
        # requests carry HCil); unconditional indexing crashed those requests.
        if settings and settings.get('HCil') is not None:
            set_head_led_from_pulse(settings['HCil'])
        # exposure/gain come from the per-camera defaults in gfhardware.cam; the
        # cloud's HCex/HCga are factory-scale (1/16-line units differ) and would
        # under-expose on mainline.
        # illumination=0: the factory captured ALL head images with the white
        # torch off - added white light washes out the measure-laser dot and
        # can break the cloud's focus/hunt analysis.
        # try/finally: a failed capture must never leave the measure laser
        # lit with no owner (it was just armed from HCil above).
        try:
            img = cam.capture(cam.GFCAM_HEAD, illumination=0)
        finally:
            head_all_led_off()
        logger.info('uploading Head Image')
        img_upload(self._session, img, msg)
        if get_cfg('LOGGING.SAVE_SENT_IMAGES'):
            logger.info('saving Head Image')
            with open('%s/%s.jpeg' % (get_cfg('LOGGING.DIR'), msg['id']), 'wb') as f:
                f.write(img)

    def head_info(self) -> HeadInfo:
        (hw_id, serial, version, r5, r6) = read_file(SYSFS_GF_BASE + 'head/info').splitlines()
        return HeadInfo(
            int(hw_id.split('=')[1], 16),
            int(serial.split('=')[1]),
            int(version.split('=')[1], 16),
        )

    def _hunt(self, msg: dict) -> None:
        # A hunt is lens travel plus the service's XY hunt pattern; the
        # lid does not gate it (the factory runs a hunt with the lid open,
        # and the beam is blocked in hardware regardless).
        ZAxis.home()
        self._motion(msg, lid_gated=False)
        home_offset = int(get_cfg('MOTION.Z_HOME_OFFSET') or 0)
        if home_offset != 0:
            logger.debug('moving z to home offset %s half steps' % home_offset)
            offset_dir = Dir.Pos if home_offset > 0 else Dir.Neg
            for _ in range(abs(home_offset)):
                ZAxis.step(offset_dir)

    def _action_cleanup(self) -> None:
        """Post-action failsafe hook (BaseMachine runs it even when an action
        crashes): stop motion, lock the laser latch, extinguish the head
        emitters, and drop the pulse-device registration. The deadman fd
        itself is closed by _motion's with-block - when a crash happens
        mid-run, that close is what fires the kernel dead man's switch."""
        # cnc.stop() first: a crashed action must not leave the gantry
        # running the rest of the program unsupervised with only the beam
        # latched off. A controlled stop is a no-op when already idle.
        cnc.stop()
        cnc.laser_latch(1)
        head_all_led_off()
        cnc.set_pulse_dev(None)
        cooling_svc.set_armed(False)
        cooling_svc.set_mode('idle')

    def _initialize(self) -> None:
        logger.debug('initializing machine')
        self._sw_thread.start()
        # Setup machine. The thermal posture (pump, TEC, heater, fans)
        # belongs to the forgectrl cooling engine; this client only
        # starts reporting job state to it.
        cooling_svc.start()
        set_lid_led(MACHINE_SETTINGS['LLvl'].default)
        cnc.reset()
        ZAxis.reset()
        set_button_color(ButtonColor.OFF)
        cnc.enable()

    def _lid_image(self, msg: dict) -> None:
        logger.info('capturing Lid Image')
        img = cam.capture(cam.GFCAM_LID)
        logger.info('uploading Lid Image')
        img_upload(self._session, img, msg)
        if get_cfg('LOGGING.SAVE_SENT_IMAGES'):
            logger.info('saving Lid Image')
            with open('%s/%s.jpeg' % (get_cfg('LOGGING.DIR'), msg['id']), 'wb') as f:
                f.write(img)

    def _motion(self, msg: dict, lid_gated: bool = True) -> None:
        logger.info('start motion')
        if not self._safe_to_move(lid_gated):
            # Refused before anything moved. The service dead-reckons from
            # the events it gets back, so a job that never ran must end
            # ':cancelled', never ':completed'. (The service itself will
            # not print with the lid open - the app requires the lid
            # closed and imaged first - so this is a backstop.)
            self._running_action_cancelled = True
        else:
            # The job runs against a flock(LOCK_EX)'d pulse device fd:
            # the lock arms the kernel dead man's switch on the open
            # file description, and every pulse write and seek routes
            # through the one fd (cnc.set_pulse_dev() points the seek
            # helpers at it; load_motion/generate_linear_puls accept the
            # open file). Broker mode reuses the inherited, never-closed
            # fd; standalone opens and closes per job, the close being
            # what fires the dead-man if this process dies mid-print.
            inherited = _inherited_pulse_dev()
            if inherited is not None:
                fcntl.flock(inherited, fcntl.LOCK_EX)
                cnc.set_pulse_dev(inherited)
                try:
                    self._motion_locked(msg, inherited, lid_gated)
                finally:
                    cnc.set_pulse_dev(None)
            else:
                with open(PULS_DEVICE, 'wb', buffering=0) as pulse_dev:
                    fcntl.flock(pulse_dev, fcntl.LOCK_EX)
                    cnc.set_pulse_dev(pulse_dev)
                    try:
                        self._motion_locked(msg, pulse_dev, lid_gated)
                    finally:
                        cnc.set_pulse_dev(None)
        logger.info('end motion')

    def _motion_locked(self, msg: dict, pulse_dev, lid_gated: bool = True) -> None:
        """Body of a motion/print job; runs with the deadman fd held."""
        cnc.clear_all()
        # Download puls file from service
        logger.info('loading motion file from %s' % msg['motion_url'])
        stats = load_motion(self._session, msg['motion_url'], pulse_dev)
        if not stats:
            # Rejected before anything reached the ring (bad magic, short
            # or unusable header): cancel cleanly instead of subscripting
            # False.
            logger.error('motion file rejected; cancelling the action')
            self._running_action_cancelled = True
            return
        self._motion_stats = stats
        logger.info('motion stats: %s' % self._motion_stats)
        if msg['action_type'] == 'print':
            send_wss_event(self._q_msg_tx, msg['id'], 'print:download:completed')
            # The cooling engine's verdict gates the armed window: a
            # flow fault, over-temp, or an absent engine blocks firing.
            if not cooling_svc.fire_ok():
                logger.error('cooling verdict blocks firing: %s',
                             cooling_svc.verdict())
                self._running_action_cancelled = True
            else:
                cnc.laser_latch(0)
                cooling_svc.set_armed(True)
                self._button_wait(msg)
            if not self._running_action_cancelled:
                send_wss_event(self._q_msg_tx, msg['id'], 'print:warmup:starting')

        # Configure for print, and wait for warm up
        if not self._running_action_cancelled:
            self._config_from_pulse('run', self._motion_stats['header_data'])
            cooling_svc.set_mode('run')
            if msg['action_type'] == 'print':
                if get_cfg('MOTION.WARM_UP_DELAY'):
                    sleep(int(get_cfg('MOTION.WARM_UP_DELAY')))

        # Run motion job. Only a print pauses on the button (the factory's
        # print handler is the one that acts on the press); a motion or a
        # hunt runs straight through.
        if not self._running_action_cancelled:
            if msg['action_type'] == 'print':
                logger.info('start temps: %s' % str(temp_sensor.all))
                send_wss_event(self._q_msg_tx, msg['id'], 'print:running')
            self._run_loop(lid_gated=lid_gated,
                           pausable=msg['action_type'] == 'print')
            cnc.laser_latch(1)
            cooling_svc.set_armed(False)
            pos = cnc.position
            logger.info('end positions (actual/expected): X (%s/%s), Y (%s/%s), Z (%s/%s)' % (
                pos.x.steps, self._motion_stats['stats']['XEND'],
                pos.y.steps, self._motion_stats['stats']['YEND'],
                pos.z.steps, self._motion_stats['stats']['ZEND'],
            ))
            logger.info('motion bytes actual:%s, expected: %s' %
                        (pos.bytes.processed, self._motion_stats['size']))
            if msg['action_type'] == 'print':
                logger.info('end print temps: %s' % str(temp_sensor.all))

        # Cool down for prints
        if msg['action_type'] == 'print':
            self._return_home(pulse_dev)
            logger.info('start cool down')
            self._config_from_pulse('cool_down', self._motion_stats['header_data'])
            cooling_svc.set_mode('cooldown')
            if get_cfg('MOTION.COOL_DOWN_DELAY'):
                sleep(int(get_cfg('MOTION.COOL_DOWN_DELAY')))
            logger.info('end cool-down temps: %s' % str(temp_sensor.all))

        # Config for idle
        logger.info('start idle')
        self._config_from_pulse('idle', self._motion_stats['header_data'])
        cooling_svc.set_mode('idle')
        cooling_svc.clear_profile()
        pos = cnc.position
        logger.info('end positions (%s, %s, %s)' % (pos.x.steps, pos.y.steps, pos.z.steps))

    def _return_home(self, pulse_dev) -> None:
        # The park is the response to an abort as much as to a finished
        # print, so it runs regardless of the cancel flag and regardless
        # of the lid: the factory parks with the lid open, and the service
        # dead-reckons from this move. Success is reported only if the
        # park ran to completion (a kernel fault is the one thing that
        # ends it early).
        logger.info('start return home')
        pos = cnc.position
        generate_linear_puls(pos.x.steps * -1, pos.y.steps * -1, pulse_dev)
        if self._run_loop(park=True):
            logger.warning('return home interrupted; not reporting success')
            return
        logger.info('return home complete')
        send_wss_event(self._q_msg_tx, self.running_action_id, 'print:return_to_home:succeeded')

    def _wait_kernel_idle(self, timeout_s: float = 10.0) -> bool:
        """After a stop or a backtrack: True once the kernel reports idle
        (the controlled decel has played out), False on timeout/fault."""
        deadline = monotonic() + timeout_s
        while monotonic() < deadline:
            state = cnc.state
            if state is MachineState.IDLE:
                return True
            if state is not MachineState.RUNNING:
                return False
            sleep(.01)
        return False

    def _run_loop(self, park: bool = False, lid_gated: bool = True,
                  pausable: bool = False) -> bool:
        """Play the loaded program. Returns True if the run was aborted
        (stopped before the program's end), False if it ran to completion.

        Reactions during the run - the factory's, both modes alike:
          - lid or interlock loop opens: controlled stop, job cancelled
            (a print then parks; the park itself ignores the lid);
          - service cancel: the same stop;
          - cooling verdict pulled: latch relocked, the same stop;
          - button press (prints only): pause - stop, backtrack
            cloud_pause_backtrack_ticks with the laser off, hold; the
            next press resumes - forward with the laser re-enabled after
            cloud_resume_lead_ticks. Lid/interlock/cancel while paused
            cancel the job from where it stands.
        The switch thread wakes this loop on every edge, so a reaction
        lands within milliseconds; the level read each pass is the
        backstop for an edge the thread missed.
        """
        logger.info('starting run')
        logger.info('current state: %s' % cnc.state)
        set_button_color(ButtonColor.WHITE)
        self._button_edges = 0
        self._enclosure_edge = False
        self._run_wake.clear()
        cnc.run()
        # Wait for state transition
        wait_time = 20
        while cnc.state is not MachineState.RUNNING and wait_time != 0:
            wait_time = wait_time - 1
            sleep(.1)
        logger.info('current state: %s' % cnc.state)
        backtrack = int(_conf_float('cloud_pause_backtrack_ticks', 2000))
        lead = int(_conf_float('cloud_resume_lead_ticks', 1950))
        aborted = False
        paused = False
        while True:
            state = cnc.state
            if state is not MachineState.RUNNING and not paused:
                break                       # program ended, or the kernel faulted
            switches = self._sw_thread.all_switches()
            enclosure = self._enclosure_open(switches)
            if enclosure is None and self._enclosure_edge:
                enclosure = 'lid opened'    # an edge the level read already missed
            self._enclosure_edge = False
            # A locally-aborted run must not report ':completed' to the
            # service: marking the action cancelled routes the finish
            # through the ':cancelled' event.
            if self._running_action_cancelled and not park:
                logger.warning('action cancelled mid-run; stopping motion')
                aborted = True
            elif enclosure is not None and lid_gated and not park:
                logger.warning('%s mid-run; stopping motion', enclosure)
                self._running_action_cancelled = True
                aborted = True
            elif cooling_svc.armed and not cooling_svc.fire_ok():
                # The cooling engine's verdict (flow fault, over-temp,
                # or an absent engine) pulls the job: latch the laser
                # and stop.
                verdict = cooling_svc.verdict()
                logger.error('cooling verdict pulled fire mid-run: %s',
                             verdict)
                cnc.laser_latch(1)
                if verdict is None:
                    # Engine absent: if it died mid flow-check the
                    # heater is still on - a write nobody else will
                    # make now.
                    WaterPump.heater_off()
                self._running_action_cancelled = True
                aborted = True
            if aborted:
                if not paused:
                    cnc.stop()
                    self._wait_kernel_idle()
                break
            if pausable and self._button_edges:
                self._button_edges = 0
                if paused:
                    logger.info('button pressed while paused; resuming '
                                '(laser lead %d ticks)', lead)
                    try:
                        cnc.resume(lead)
                    except OSError as e:
                        logger.error('resume refused (%s); cancelling', e)
                        self._running_action_cancelled = True
                        aborted = True
                        break
                    paused = False
                    send_wss_event(self._q_msg_tx, self.running_action_id,
                                   'print:resumed')
                    # Give the kernel a moment to leave idle before the
                    # next pass reads the state.
                    sleep(.05)
                    continue
                logger.info('button pressed mid-run; pausing')
                cnc.stop()
                if not self._wait_kernel_idle():
                    break                   # fault: the state read above ends the loop
                pos = cnc.position
                if pos.bytes.processed >= pos.bytes.total:
                    break                   # the decel ended the program: done
                if backtrack > 0:
                    try:
                        cnc.resume(-backtrack)
                    except OSError as e:
                        # Not fatal: hold where the decel stopped.
                        logger.warning('backtrack refused (%s); pausing in place', e)
                    else:
                        if not self._wait_kernel_idle():
                            break
                paused = True
                send_wss_event(self._q_msg_tx, self.running_action_id,
                               'print:paused')
                logger.info('paused at %s', cnc.position)
                continue
            if paused and state not in (MachineState.IDLE, MachineState.RUNNING):
                break                       # the kernel faulted while paused
            self._run_wake.wait(.1)
            self._run_wake.clear()
        logger.info('current state: %s' % cnc.state)
        set_button_color(ButtonColor.OFF)
        logger.info('finished run')
        return aborted

    def _safe_to_move(self, lid_gated: bool = True) -> bool:
        switches = self._sw_thread.all_switches()
        reason = self._enclosure_open(switches)
        if reason is not None and lid_gated:
            logger.info('%s, unsafe to move', reason)
            return False
        if cnc.state is not MachineState.IDLE:
            logger.info('machine is not idle, state: %s' % cnc.state.value)
            return False
        temp = temp_sensor.water_2.C
        if temp > int(get_cfg('THERMAL.MAX_START_TEMP')):
            logger.info('machine temp is too high, temp: %s' % temp)
            return False
        if temp <= -100:
            # A dead or disconnected coolant sensor reads the -273.15
            # sentinel, which must not pass the gate as "cold enough".
            logger.info('coolant sensor reads invalid (%s); unsafe to move' % temp)
            return False
        return True

    def _shutdown(self) -> None:
        logger.info('shutting down')
        # Safe posture in EVERY mode: under the broker this process's
        # exit is not a final close of the pulse device, so neither the
        # kernel dead-man nor the close-relock fires on it. Stop motion
        # and lock the latch explicitly, then hand the cooling engine a
        # final disarmed/idle report so it stands down through cooldown.
        cnc.stop()
        cnc.laser_latch(1)
        cooling_svc.set_armed(False)
        cooling_svc.set_mode('idle')
        cooling_svc.stop = True
        self._sw_thread.stop = True
        logger.info('joining switch thread')
        self._sw_thread.join()
        # Rail policy belongs to the forgectrl broker when it owns the
        # device: disabling on handback would drop the rail out from
        # under the next writer. Standalone stands the rail down.
        if _inherited_pulse_dev() is None:
            cnc.disable()
        logger.info('shut down complete')

    def _switch_event(self, event: SwitchEvent) -> None:
        # Runs on the switch thread: report the edge to the service, flag
        # it for the button wait / run loop, and wake them. Nothing here
        # touches the cnc - the run loop owns those writes.
        logger.debug('received switch event %s' % str(event))
        if event.code == InputSwitch.SW_BUTTON:
            if event.val:
                logger.info('button pushed')
                send_wss_event(self._q_msg_tx, None, 'button:pressed')
                self._button_pressed = True
                self._button_edges += 1
                self._run_wake.set()
            else:
                logger.info('button released')
                send_wss_event(self._q_msg_tx, None, 'button:released')
        elif event.code == InputSwitch.SW_DOORS:
            if event.val:
                logger.info('lid closed')
                send_wss_event(self._q_msg_tx, None, 'lid:closed')
            else:
                logger.info('lid opened')
                send_wss_event(self._q_msg_tx, None, 'lid:opened')
                self._enclosure_edge = True
                self._run_wake.set()
        elif event.code == InputSwitch.SW_INTERLOCK:
            # Active = the remote-interlock loop OPENED. Not reported to
            # the service (see docs/CLOUD.md); gates the job like the lid.
            if event.val:
                logger.info('interlock loop opened')
                self._enclosure_edge = True
                self._run_wake.set()
            else:
                logger.info('interlock loop closed')
