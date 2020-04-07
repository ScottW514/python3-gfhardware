"""
(C) Copyright 2020
Scott Wiederhold, s.e.wiederhold@gmail.com
https://community.openglow.org
SPDX-License-Identifier:    MIT
"""
import logging
import os
from time import sleep

from gfutilities import BaseMachine
from gfutilities.configuration import get_cfg, set_cfg
from gfutilities.puls import generate_linear_puls
from gfutilities.service.websocket import load_motion, img_upload, send_wss_event
from gfutilities.device.settings import MACHINE_SETTINGS, update_settings

from gfhardware import id
from gfhardware._common import *
from gfhardware.cnc import *
from gfhardware.cooling import *
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


class Machine(BaseMachine):
    """
    Operates the GF Hardware
    See parent class for method documentation
    """

    def __init__(self):
        update_settings({
            'AAid': {'idle': fans.air_assist.set_pwm},
            'AArd': {'run': fans.air_assist.set_pwm},
            'AAwd': {'cool_down': fans.air_assist.set_pwm},
            'EFid': {'idle': fans.exhaust.set_pwm},
            'EFrd': {'run': fans.exhaust.set_pwm},
            'EFwd': {'cool_down': fans.exhaust.set_pwm},
            'IFid': {'idle': fans.intake_1.set_pwm},
            'IFrd': {'run': fans.intake_1.set_pwm},
            'IFwd': {'cool_down': fans.intake_1.set_pwm},
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

        set_cfg('MACHINE.HEAD_FIRMWARE', self.head_info().version, True)
        set_cfg('MACHINE.HEAD_ID', self.head_info().hardware_id, True)
        set_cfg('MACHINE.HEAD_SERIAL', self.head_info().hardware_id, True)

        set_cfg('MACHINE.SERIAL', id.serial(), True)
        set_cfg('MACHINE.HOSTNAME', id.hostname(), True)
        set_cfg('MACHINE.PASSWORD', id.password(), True)

        BaseMachine.__init__(self)

    def _button_wait(self, msg: dict) -> None:
        self._button_pressed = False
        set_button_color(ButtonColor.WHITE)
        logger.info('waiting for button')
        while not self._button_pressed:
            if self._running_action_cancelled:
                return
            sleep(.1)

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
        set_head_led_from_pulse(settings['HCil'])
        img = cam.capture(cam.GFCAM_HEAD, int(settings['HCex']), int(settings['HCga']))
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
        home_offset = int(get_cfg('MOTION.Z_HOME_OFFSET'))
        if home_offset is not None:
            logger.debug('moving z to home offset %s half steps' % home_offset)
            offset_dir = Dir.Pos if home_offset > 0 else Dir.Neg
            while home_offset != 0:
                ZAxis.step(offset_dir)

    def _initialize(self) -> None:
        logger.debug('initializing machine')
        self._sw_thread.start()
        # Setup machine
        set_lid_led(MACHINE_SETTINGS['LLvl'].default)
        TEC.off()
        WaterPump.on()
        WaterPump.set_heater(int(get_cfg('THERMAL.WATER_HEATER_PERCENT')))
        fans.reset()
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
            cnc.clear_all()
            # Download puls file from service
            logger.info('loading motion file from %s' % msg['motion_url'])
            self._motion_stats = load_motion(self._session, msg['motion_url'], PULS_DEVICE)
            logger.info('motion stats: %s' % self._motion_stats)
            if msg['action_type'] == 'print':
                send_wss_event(self._q_msg_tx, msg['id'], 'print:download:completed')
                cnc.laser_latch(0)
                self._button_wait(msg)
                if not self._running_action_cancelled:
                    send_wss_event(self._q_msg_tx, msg['id'], 'print:warmup:starting')

            # Configure for print, and wait for warm up
            if not self._running_action_cancelled:
                self._config_from_pulse('run', self._motion_stats['header_data'])
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
                self._return_home()
                logger.info('start cool down')
                self._config_from_pulse('cool_down', self._motion_stats['header_data'])
                if get_cfg('MOTION.COOL_DOWN_DELAY'):
                    sleep(int(get_cfg('MOTION.COOL_DOWN_DELAY')))
                logger.info('end cool-down temps: %s' % str(temp_sensor.all))

            # Config for idle
            logger.info('start idle')
            self._config_from_pulse('idle', self._motion_stats['header_data'])
            pos = cnc.position
            logger.info('end positions (%s, %s, %s)' % (pos.x.steps, pos.y.steps, pos.z.steps))
        logger.info('end motion')

    def _return_home(self) -> None:
        logger.info('start return home')
        pos = cnc.position
        generate_linear_puls(pos.x.steps * -1, pos.y.steps * -1, PULS_DEVICE)
        self._run_loop()
        send_wss_event(self._q_msg_tx, self.running_action_id, 'print:return_to_home:succeeded')

    def _run_loop(self):
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
        while cnc.state is MachineState.RUNNING:
            # TODO: Check for conditions that would make us want to stop what we are doing
            #       Like OVER-TEMP, LID OPEN, ACCELEROMETER TRIP, ETC.
            pass
            sleep(.1)
        logger.info('current state: %s' % cnc.state)
        set_button_color(ButtonColor.OFF)
        logger.info('finished run')

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
        self._sw_thread.stop = True
        logger.info('joining switch thread')
        self._sw_thread.join()
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
