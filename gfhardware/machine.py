"""
(C) Copyright 2020
Scott Wiederhold, s.e.wiederhold@gmail.com
https://community.openglow.org
SPDX-License-Identifier:    MIT
"""
import fcntl
import logging
import os
from time import monotonic, sleep

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

if os.getenv('REMOTE_DEBUG'):
    import importlib.util
    cam_spec = importlib.util.spec_from_file_location(
        "cam", "/usr/lib/python3.7/site-packages/gfhardware/cam.cpython-37m-arm-linux-gnueabi.so")
    cam = importlib.util.module_from_spec(cam_spec)
    cam_spec.loader.exec_module(cam)
else:
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
        # The factory board's estop sense line reads LOW whenever the
        # steppers run (measured on live hardware: it drops for the whole
        # duration of any X or Z move and recovers at idle), so it cannot
        # gate motion there. Machines with a real e-stop circuit opt in
        # via MOTION.ESTOP_HALTS_MOTION.
        self._estop_halts_motion: bool = bool(get_cfg('MOTION.ESTOP_HALTS_MOTION'))

        set_cfg('MACHINE.HEAD_FIRMWARE', self.head_info().version, True)
        set_cfg('MACHINE.HEAD_ID', self.head_info().hardware_id, True)
        set_cfg('MACHINE.HEAD_SERIAL', self.head_info().hardware_id, True)

        set_cfg('MACHINE.SERIAL', id.serial(), True)
        set_cfg('MACHINE.HOSTNAME', id.hostname(), True)
        set_cfg('MACHINE.PASSWORD', id.password(), True)

        BaseMachine.__init__(self)

    def _button_wait(self, msg: dict) -> None:
        # The wait runs with the latch already unlocked, so it is
        # bounded and supervised the same way GRBL mode's arm window is:
        # the shared laser_button_timeout_s (default 300 s, clamped to
        # 1-3600 - out-of-range values fall back, never wait-forever)
        # bounds it, and an opened lid ends it. Timeout, lid, and cloud
        # cancel all relock the latch and disarm before returning.
        timeout_s = _conf_float('laser_button_timeout_s', 300.0)
        if not 1.0 <= timeout_s <= 3600.0:
            timeout_s = 300.0
        deadline = monotonic() + timeout_s
        self._button_pressed = False
        set_button_color(ButtonColor.WHITE)
        logger.info('waiting for button')
        abort = None
        while not self._button_pressed:
            if self._running_action_cancelled:
                abort = 'cancelled'
                break
            if not self._sw_thread.all_switches()[InputSwitch.SW_DOORS]:
                abort = 'lid opened'
                break
            if monotonic() > deadline:
                abort = 'timed out'
                break
            sleep(.1)
        if abort is not None:
            logger.warning('button wait %s - relocking the laser', abort)
            cnc.laser_latch(1)
            cooling_svc.set_armed(False)
            self._running_action_cancelled = True
            set_button_color(ButtonColor.OFF)

    @staticmethod
    def _config_from_pulse(state: str, header: dict):
        for key, setting in MACHINE_SETTINGS.items():
            val = header.get(key, None)
            if val is not None:
                func = getattr(setting, state)
                if func is not None:
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
            open('%s/%s.jpeg' % (get_cfg('LOGGING.DIR'), msg['id']), 'wb').write(img)

    def head_info(self) -> HeadInfo:
        (hw_id, serial, version, r5, r6) = read_file(SYSFS_GF_BASE + 'head/info').splitlines()
        return HeadInfo(
            int(hw_id.split('=')[1], 16),
            int(serial.split('=')[1]),
            int(version.split('=')[1], 16),
        )

    def _hunt(self, msg: dict) -> None:
        ZAxis.home()
        self._motion(msg)
        home_offset = int(get_cfg('MOTION.Z_HOME_OFFSET') or 0)
        if home_offset != 0:
            logger.debug('moving z to home offset %s half steps' % home_offset)
            offset_dir = Dir.Pos if home_offset > 0 else Dir.Neg
            for _ in range(abs(home_offset)):
                ZAxis.step(offset_dir)

    def _action_cleanup(self) -> None:
        """Post-action failsafe hook (BaseMachine runs it even when an action
        crashes): lock the laser latch, extinguish the head emitters, and drop
        the pulse-device registration. The deadman fd itself is closed by
        _motion's with-block - when a crash happens mid-run, that close is
        what fires the kernel dead man's switch."""
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
            open('%s/%s.jpeg' % (get_cfg('LOGGING.DIR'), msg['id']), 'wb').write(img)

    def _motion(self, msg: dict) -> None:
        logger.info('start motion')
        if self._safe_to_move:
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
                    self._motion_locked(msg, inherited)
                finally:
                    cnc.set_pulse_dev(None)
            else:
                with open(PULS_DEVICE, 'wb', buffering=0) as pulse_dev:
                    fcntl.flock(pulse_dev, fcntl.LOCK_EX)
                    cnc.set_pulse_dev(pulse_dev)
                    try:
                        self._motion_locked(msg, pulse_dev)
                    finally:
                        cnc.set_pulse_dev(None)
        logger.info('end motion')

    def _motion_locked(self, msg: dict, pulse_dev) -> None:
        """Body of a motion/print job; runs with the deadman fd held."""
        cnc.clear_all()
        # Download puls file from service
        logger.info('loading motion file from %s' % msg['motion_url'])
        self._motion_stats = load_motion(self._session, msg['motion_url'], pulse_dev)
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

        # Run motion job
        if not self._running_action_cancelled:
            if msg['action_type'] == 'print':
                logger.info('start temps: %s' % str(temp_sensor.all))
                send_wss_event(self._q_msg_tx, msg['id'], 'print:running')
            self._run_loop()
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
        # The park move is the response to an abort, so it must run even when
        # the action is cancelled (park=True); the service dead-reckons from
        # this move, so success is only reported when the park completed.
        logger.info('start return home')
        pos = cnc.position
        generate_linear_puls(pos.x.steps * -1, pos.y.steps * -1, pulse_dev)
        if self._run_loop(park=True):
            logger.warning('return home interrupted; not reporting success')
            return
        send_wss_event(self._q_msg_tx, self.running_action_id, 'print:return_to_home:succeeded')

    def _run_loop(self, park: bool = False) -> bool:
        logger.info('starting run')
        logger.info('current state: %s' % cnc.state)
        set_button_color(ButtonColor.WHITE)
        cnc.run()
        # Wait for state transition
        wait_time = 20
        while cnc.state is not MachineState.RUNNING and wait_time != 0:
            wait_time = wait_time - 1
            sleep(.1)
        logger.info('current state: %s' % cnc.state)
        # Live safety poll: the hardware chain kills the BEAM on
        # lid/interlock/estop by itself, but MOTION continued at full speed
        # until now. Switch polarity follows _safe_to_move / _switch_event:
        # truthy = circuit closed / OK. SW_INTERLOCK deliberately does NOT
        # gate motion: its sense is inverted - the remote-interlock loop
        # reads active only when OPEN (Basic/Plus ship the 2-pin connector
        # factory-jumpered, so it reads inactive = satisfied there) - and
        # the hardware chain already disables the beam whenever the loop
        # is open. SW_ESTOP gates motion only when
        # MOTION.ESTOP_HALTS_MOTION is set - the factory board's estop
        # sense reads False during any motion (measured), so it is
        # idle-telemetry only there.
        stop_sent = False
        while cnc.state is MachineState.RUNNING:
            if not stop_sent:
                switches = self._sw_thread.all_switches()
                # A locally-aborted run must not report ':completed' to the
                # service: marking the action cancelled routes the finish
                # through the ':cancelled' event.
                if self._estop_halts_motion and not switches[InputSwitch.SW_ESTOP]:
                    logger.error('estop tripped mid-run; emergency halt')
                    self._running_action_cancelled = True
                    cnc.halt()
                    cnc.disable()
                    stop_sent = True
                elif self._running_action_cancelled and not park:
                    logger.warning('action cancelled mid-run; stopping motion')
                    cnc.stop()
                    stop_sent = True
                elif not switches[InputSwitch.SW_DOORS]:
                    logger.warning('lid opened mid-run; stopping motion')
                    self._running_action_cancelled = True
                    cnc.stop()
                    stop_sent = True
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
                    cnc.stop()
                    stop_sent = True
            sleep(.1)
        logger.info('current state: %s' % cnc.state)
        set_button_color(ButtonColor.OFF)
        logger.info('finished run')
        return stop_sent

    @property
    def _safe_to_move(self) -> bool:
        switches = self._sw_thread.all_switches()
        if not switches[InputSwitch.SW_DOORS]:
            logger.info('door open, unsafe to move')
            return False
        if cnc.state is not MachineState.IDLE:
            logger.info('machine is not idle, state: %s' % cnc.state.value)
            return False
        if temp_sensor.water_2.C > int(get_cfg('THERMAL.MAX_START_TEMP')):
            logger.info('machine temp is too high, temp: %s' % temp_sensor.water_2.C)
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
        logger.debug('received switch event %s' % str(event))
        if event.code == InputSwitch.SW_BUTTON:
            if event.val:
                logger.info('button pushed')
                send_wss_event(self._q_msg_tx, None, 'button:pressed')
                self._button_pressed = True
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
