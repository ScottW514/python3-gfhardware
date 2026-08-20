"""
(C) Copyright 2026
Scott Wiederhold, s.e.wiederhold@gmail.com
https://community.openglow.org

SPDX-License-Identifier:    MIT

Host tests for the cloud-mode job supervision in gfhardware.machine: how
a running or waiting job reacts to the lid, the interlock loop, the
button, and a service cancel. The real Machine code runs against a fake
kernel wrapper (records every cnc write, plays a simple state machine)
and a fake switch monitor (settable switch word, edge delivery on the
switch thread), with the wire events captured.

Run:  PYTHONPATH=.:../Glowforge-Utilities python3 -m unittest tests.test_machine_lid_button
"""
import os
import sys
import tempfile
import threading
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(os.path.dirname(ROOT), 'Glowforge-Utilities'))
os.environ['GFHOME_CONF'] = os.path.join(tempfile.gettempdir(), 'no-such-forgefirm.conf')

# The package __init__ imports the machine, which imports the hardware
# modules (one of them a compiled evdev extension): register a bare
# package first so gfhardware._common loads without it, then the fakes,
# then the machine.
_pkg = types.ModuleType('gfhardware')
_pkg.__path__ = [os.path.join(ROOT, 'gfhardware')]
sys.modules['gfhardware'] = _pkg

try:
    import fcntl                                                   # noqa: F401
except ImportError:
    # The job holds the pulse device flock'd for the kernel dead man's
    # switch. The lock is not what these tests exercise, so a stub keeps
    # them runnable on a host without it.
    _fcntl = types.ModuleType('fcntl')
    _fcntl.LOCK_EX = 2
    _fcntl.flock = lambda *a, **k: None
    sys.modules['fcntl'] = _fcntl

from gfhardware._common import (InputSwitch, MachineState, ButtonColor,   # noqa: E402
                                SwitchEvent, Position, AxisPosition, PulsPosition,
                                HeadInfo)


# ---------------------------------------------------------------- fakes

class FakeCNC:
    """Kernel wrapper stand-in. state plays: run() -> RUNNING for
    `run_reads` state reads, then IDLE (the program's end); stop() ->
    IDLE at once; resume(-n) -> RUNNING for `backtrack_reads` reads then
    IDLE; resume(+n) -> RUNNING again for the remainder."""

    def __init__(self):
        self.writes = []
        self._state = MachineState.IDLE
        self._reads_left = 0
        self.run_reads = 10 ** 9
        self.backtrack_reads = 3
        self._backward = False
        self.processed = 100
        self.total = 1000
        self.latch = 1
        # Ring room for the feed watchdog: plenty, unless a test says so.
        self.free = 10 ** 7
        # Steps of played, still-resident program a backward run can walk.
        # Plenty, unless a test says otherwise.
        self.backtrack_budget = 10 ** 6
        self.max_backtrack_error = None
        self.resume_error = None
        self.clear_error = None
        self.xy_steps = (10, 10)
        self.streaming_writes = []
        # After this many state reads a live-fed run reports a dry ring.
        self.underrun_after = None

    # -- attributes the Machine constructor maps pulse-header keys onto
    def set_step_freq(self, v): self.writes.append(('step_freq', v))
    def set_x_decay(self, v): pass
    def set_x_current(self, v): pass
    def set_x_mode(self, v): pass
    def set_y_decay(self, v): pass
    def set_y_current(self, v): pass
    def set_y_mode(self, v): pass

    # -- job path
    def run(self):
        self.writes.append(('run', 1))
        self._state = MachineState.RUNNING
        self._backward = False
        self._reads_left = self.run_reads

    def stop(self):
        self.writes.append(('stop', 1))
        self._state = MachineState.IDLE

    def resume(self, steps):
        if self.resume_error is not None:
            raise self.resume_error
        self.writes.append(('resume', int(steps)))
        self._state = MachineState.RUNNING
        self._backward = steps < 0
        self._reads_left = self.backtrack_reads if steps < 0 else self.run_reads

    def laser_latch(self, v):
        self.writes.append(('laser_latch', int(v)))
        self.latch = int(v)

    def clear_all(self): self.writes.append(('clear_all', 1))

    def clear_pulse_and_byte(self):
        if self.clear_error is not None:
            raise self.clear_error
        self.writes.append(('clear_pulse', 1))

    def set_streaming(self, val):
        self.streaming_writes.append(int(val))

    def set_pulse_dev(self, dev): pass
    def enable(self): pass
    def disable(self): pass
    def reset(self): pass

    @property
    def state(self):
        if self._state is MachineState.RUNNING:
            if self.underrun_after is not None:
                self.underrun_after -= 1
                if self.underrun_after <= 0:
                    self._state = MachineState.UNDERRUN
                    return self._state
            self._reads_left -= 1
            if self._reads_left <= 0:
                self._state = MachineState.IDLE
                # A backward run ends where it was asked to stop; only a
                # forward one can have played the program out.
                if not self._backward:
                    self.processed = self.total
        return self._state

    @property
    def max_backtrack(self):
        if self.max_backtrack_error is not None:
            raise self.max_backtrack_error
        return self.backtrack_budget

    @property
    def position(self):
        x = AxisPosition(self.xy_steps[0], self.xy_steps[0] / 53.333, 0.0)
        y = AxisPosition(self.xy_steps[1], self.xy_steps[1] / 53.333, 0.0)
        z = AxisPosition(0, 0.0, 0.0)
        return Position(x, y, z, PulsPosition(self.total, self.processed))


class FakeFeeder:
    """Feeder stand-in. ``written`` stands still until a test says the feed
    is moving, which is the whole difference between a wedged feeder and a
    working one."""

    def __init__(self, written=0, finished=False, moving=False):
        self._written = written
        self.moving = moving
        self.finished = finished
        self.error = None
        self.stopped = False

    @property
    def written(self):
        if self.moving:
            self._written += 1
        return self._written

    def stop(self, timeout=5.0):
        self.stopped = True


class FakeSwitches:
    """Switch monitor stand-in: a settable switch word plus edge delivery
    into the machine's handler on a helper thread (as the real monitor
    thread would)."""

    def __init__(self):
        # Pending edge timers from the previous test must never land in
        # this one's run loop.
        for t in getattr(self, 'timers', []):
            t.cancel()
        for t in getattr(self, 'timers', []):
            t.join(1.0)
        self.timers = []
        self.word = {s: False for s in InputSwitch}
        self.word[InputSwitch.SW_DOOR1] = True
        self.word[InputSwitch.SW_DOOR2] = True
        self.word[InputSwitch.SW_DOORS] = True
        self.handler = None
        self.started = False

    def start(self): self.started = True
    def join(self): pass
    stop = False

    def all_switches(self):
        return dict(self.word)

    def _edge(self, code, val):
        self.word[code] = val
        if self.handler:
            self.handler(SwitchEvent(0, 0, 5, code, val))

    def _later(self, delay, fn):
        t = threading.Timer(delay, fn)
        self.timers.append(t)
        t.start()

    def press(self, delay=0.0):
        self._later(delay, lambda: (self._edge(InputSwitch.SW_BUTTON, True),
                                    self._edge(InputSwitch.SW_BUTTON, False)))

    def open_lid(self, delay=0.0):
        self._later(delay, lambda: (self._edge(InputSwitch.SW_DOORS, False),
                                    self._edge(InputSwitch.SW_DOOR1, False)))

    def close_lid(self, delay=0.0):
        self._later(delay, lambda: (self._edge(InputSwitch.SW_DOOR1, True),
                                    self._edge(InputSwitch.SW_DOORS, True)))

    def open_interlock(self, delay=0.0):
        self._later(delay, lambda: self._edge(InputSwitch.SW_INTERLOCK, True))


class FakeCooling:
    def __init__(self):
        self.armed = False
        self.mode = 'idle'
        self._fire_ok = True

    def set_armed(self, a): self.armed = bool(a)
    def set_mode(self, m): self.mode = m
    def fire_ok(self): return self._fire_ok
    def verdict(self): return {'fire_ok': self._fire_ok}
    def clear_profile(self): pass
    def start(self): pass
    def profile_air_assist(self, v): pass
    def profile_exhaust(self, v): pass
    def profile_intake(self, v): pass


def _install_fakes():
    """Register stand-ins for the hardware modules before gfhardware.machine
    is imported (it star-imports them at module load)."""
    cnc_mod = types.ModuleType('gfhardware.cnc')
    cnc_mod.cnc = FakeCNC()
    cnc_mod.__all__ = ['cnc']

    sw_mod = types.ModuleType('gfhardware.switches')
    fake_sw = FakeSwitches()

    class SwitchMonitor:
        def __new__(cls, dev, handler):
            fake_sw.handler = handler
            return fake_sw
    sw_mod.SwitchMonitor = SwitchMonitor
    sw_mod.__all__ = ['SwitchMonitor']

    leds_mod = types.ModuleType('gfhardware.leds')
    leds_mod.button_colors = []
    leds_mod.set_button_color = lambda c: leds_mod.button_colors.append(c)
    leds_mod.head_all_led_off = lambda: None
    leds_mod.set_lid_led = lambda v: None
    leds_mod.set_head_led_from_pulse = lambda v: None
    leds_mod.__all__ = ['set_button_color', 'head_all_led_off', 'set_lid_led',
                        'set_head_led_from_pulse']

    cooling_mod = types.ModuleType('gfhardware.cooling')

    class _Temp:
        C = 20.0

    class _Sensors:
        water_2 = _Temp()
        all = {}
    cooling_mod.temp_sensor = _Sensors()

    class WaterPump:
        @staticmethod
        def heater_off(): pass
    cooling_mod.WaterPump = WaterPump
    cooling_mod.__all__ = ['temp_sensor', 'WaterPump']

    coolsvc_mod = types.ModuleType('gfhardware.coolsvc')
    coolsvc_mod.cooling_svc = FakeCooling()

    z_mod = types.ModuleType('gfhardware.z_axis')

    class ZAxis:
        homed = 0

        @staticmethod
        def home(): ZAxis.homed += 1

        @staticmethod
        def step(d): pass

        @staticmethod
        def set_mode_from_puls(v): pass

        @staticmethod
        def reset(): pass
    z_mod.ZAxis = ZAxis

    cam_mod = types.ModuleType('gfhardware.cam')
    cam_mod.GFCAM_HEAD = 0
    cam_mod.GFCAM_LID = 1
    cam_mod.capture = lambda *a, **k: b''

    id_mod = types.ModuleType('gfhardware.id')
    id_mod.serial = lambda: 'XXX-000'
    id_mod.hostname = lambda: 'host'
    id_mod.password = lambda: 'pw'

    for name, mod in (('gfhardware.cnc', cnc_mod), ('gfhardware.switches', sw_mod),
                      ('gfhardware.leds', leds_mod), ('gfhardware.cooling', cooling_mod),
                      ('gfhardware.coolsvc', coolsvc_mod), ('gfhardware.z_axis', z_mod),
                      ('gfhardware.cam', cam_mod), ('gfhardware.id', id_mod)):
        sys.modules[name] = mod
    return cnc_mod.cnc, fake_sw, coolsvc_mod.cooling_svc, leds_mod


CNC, SW, COOL, LEDS = _install_fakes()

import gfhardware.machine as machine_mod          # noqa: E402
from gfhardware.machine import Machine             # noqa: E402
from gfutilities.configuration import set_cfg     # noqa: E402

set_cfg('THERMAL.MAX_START_TEMP', 50)
# Point the reads the constructor makes at fakes.
machine_mod.read_file = lambda attr, binary=False: 'hw_id=0x4c\nserial=1\nversion=0x2\nr5=0\nr6=0'
EVENTS = []
machine_mod.send_wss_event = lambda q, action_id, event: EVENTS.append(event)
machine_mod.generate_linear_puls = lambda x, y, dev: None


def job_events():
    """The action's own wire events (the unsolicited button:*/lid:* edge
    reports are expected alongside and not part of these checks)."""
    return [e for e in EVENTS if e.startswith('print:')]


def make_machine():
    m = Machine()
    m.running_action_id = 42
    m._running_action_cancelled = False
    return m


class RunLoopTests(unittest.TestCase):

    def setUp(self):
        CNC.__init__()
        SW.__init__()
        COOL.__init__()
        del EVENTS[:]
        del LEDS.button_colors[:]
        self.m = make_machine()
        SW.handler = self.m._switch_event

    # -- lid / interlock mid-run --------------------------------------

    def test_lid_open_mid_run_stops_and_cancels(self):
        SW.open_lid(delay=0.05)
        t0 = time.monotonic()
        aborted = self.m._run_loop()
        dt = time.monotonic() - t0
        self.assertTrue(aborted)
        self.assertTrue(self.m._running_action_cancelled)
        self.assertIn(('stop', 1), CNC.writes)
        # Edge-driven: the stop lands well inside one 100 ms poll tick.
        self.assertLess(dt, 0.15, 'stop was not edge-driven (%.3f s)' % dt)

    def test_interlock_open_mid_run_stops_and_cancels(self):
        SW.open_interlock(delay=0.05)
        aborted = self.m._run_loop()
        self.assertTrue(aborted)
        self.assertTrue(self.m._running_action_cancelled)
        self.assertIn(('stop', 1), CNC.writes)

    def test_lid_open_before_run_stops_on_first_pass(self):
        SW.word[InputSwitch.SW_DOORS] = False
        aborted = self.m._run_loop()
        self.assertTrue(aborted)
        self.assertIn(('stop', 1), CNC.writes)

    def test_transient_lid_open_still_cancels(self):
        # Open and closed again before the loop's level read: the edge
        # is what counts (the hardware button latch set on it).
        SW.open_lid(delay=0.02)
        SW.close_lid(delay=0.03)
        aborted = self.m._run_loop()
        self.assertTrue(aborted)
        self.assertTrue(self.m._running_action_cancelled)

    # -- the park and the hunt ignore the lid ---------------------------

    def test_park_ignores_lid(self):
        CNC.run_reads = 5
        SW.open_lid(delay=0.02)
        aborted = self.m._run_loop(park=True)
        self.assertFalse(aborted)
        self.assertNotIn(('stop', 1), CNC.writes)

    def test_park_ignores_service_cancel(self):
        CNC.run_reads = 5
        self.m._running_action_cancelled = True
        aborted = self.m._run_loop(park=True)
        self.assertFalse(aborted)
        self.assertNotIn(('stop', 1), CNC.writes)

    def test_hunt_ignores_lid(self):
        CNC.run_reads = 5
        SW.word[InputSwitch.SW_DOORS] = False
        aborted = self.m._run_loop(lid_gated=False)
        self.assertFalse(aborted)
        self.assertNotIn(('stop', 1), CNC.writes)

    def test_service_cancel_mid_run_stops(self):
        threading.Timer(0.05, lambda: setattr(self.m, '_running_action_cancelled', True)).start()
        aborted = self.m._run_loop()
        self.assertTrue(aborted)
        self.assertIn(('stop', 1), CNC.writes)

    # -- a live feed that falls behind ---------------------------------

    def test_underrun_mid_run_stops_and_cancels(self):
        # A job longer than the ring is fed while it plays. If the ring ever
        # goes dry the kernel stops dead, so the job did not finish and the
        # position is not to be trusted: it has to end ':cancelled', never
        # ':completed'.
        CNC.underrun_after = 3
        aborted = self.m._run_loop()
        self.assertTrue(aborted)
        self.assertTrue(self.m._running_action_cancelled)
        # The stop is also the acknowledgement the kernel requires before it
        # will accept another run.
        self.assertIn(('stop', 1), CNC.writes)

    def test_underrun_while_paused_is_not_missed(self):
        CNC.underrun_after = 3
        aborted = self.m._run_loop(pausable=True)
        self.assertTrue(aborted)
        self.assertTrue(self.m._running_action_cancelled)

    def test_feed_failure_mid_run_stops_the_job(self):
        # The feeder cannot get the rest of the job into the ring: what is
        # already in there would keep playing, so the run has to be stopped.
        class DeadFeeder:
            error = OSError(5, 'I/O error')
            finished = False
            written = 0

            def stop(self, timeout=5.0):
                pass

        self.m._feeder = DeadFeeder()
        try:
            aborted = self.m._run_loop()
        finally:
            self.m._feeder = None
        self.assertTrue(aborted)
        self.assertTrue(self.m._running_action_cancelled)
        self.assertIn(('stop', 1), CNC.writes)

    def test_cooling_verdict_pulled_relocks_and_stops(self):
        threading.Timer(0.05, lambda: setattr(COOL, '_fire_ok', False)).start()
        COOL.armed = True
        aborted = self.m._run_loop()
        self.assertTrue(aborted)
        self.assertIn(('laser_latch', 1), CNC.writes)
        self.assertIn(('stop', 1), CNC.writes)

    # -- button pause / resume (prints) --------------------------------

    def test_button_pauses_and_resumes_a_print(self):
        SW.press(delay=0.05)                       # pause
        SW.press(delay=0.40)                       # resume
        threading.Timer(0.6, lambda: setattr(CNC, '_reads_left', 1)).start()   # end the program
        aborted = self.m._run_loop(pausable=True)
        self.assertFalse(aborted)
        w = CNC.writes
        self.assertIn(('stop', 1), w)
        self.assertIn(('resume', -2000), w)
        self.assertIn(('resume', 1950), w)
        self.assertLess(w.index(('stop', 1)), w.index(('resume', -2000)))
        self.assertLess(w.index(('resume', -2000)), w.index(('resume', 1950)))
        self.assertEqual(job_events(), ['print:paused', 'print:resumed'])
        self.assertFalse(self.m._running_action_cancelled)
        # The latch was never touched by the pause.
        self.assertNotIn(('laser_latch', 1), w)

    def test_button_does_not_pause_a_motion(self):
        CNC.run_reads = 5
        SW.press(delay=0.02)
        aborted = self.m._run_loop(pausable=False)
        self.assertFalse(aborted)
        self.assertNotIn(('stop', 1), CNC.writes)
        self.assertEqual(job_events(), [])

    def test_lid_open_while_paused_cancels_without_second_stop(self):
        SW.press(delay=0.05)                       # pause
        SW.open_lid(delay=0.40)
        aborted = self.m._run_loop(pausable=True)
        self.assertTrue(aborted)
        self.assertTrue(self.m._running_action_cancelled)
        self.assertEqual(CNC.writes.count(('stop', 1)), 1)   # only the pause's stop
        self.assertEqual(job_events(), ['print:paused'])

    def test_service_cancel_while_paused(self):
        SW.press(delay=0.05)
        threading.Timer(0.4, lambda: setattr(self.m, '_running_action_cancelled', True)).start()
        aborted = self.m._run_loop(pausable=True)
        self.assertTrue(aborted)
        self.assertEqual(job_events(), ['print:paused'])

    def test_pause_at_program_end_finishes(self):
        # The decel lands on the program's last byte: no backtrack, done.
        SW.press(delay=0.05)

        def end_it():
            CNC.processed = CNC.total
        threading.Timer(0.049, end_it).start()
        aborted = self.m._run_loop(pausable=True)
        self.assertFalse(aborted)
        self.assertNotIn(('resume', -2000), CNC.writes)
        self.assertEqual(job_events(), [])

    def test_backtrack_refused_pauses_in_place(self):
        # The kernel refuses the backtrack: the job holds where the decel
        # stopped, still paused, still resumable - and the resume asks for
        # the shortest lead there is, because nothing was retraced. A lead
        # over ground the job has not cut yet would leave that length
        # unburned, and a lead of zero is the kernel's "no laser at all".
        SW.press(delay=0.05)
        SW.press(delay=0.40)
        threading.Timer(0.6, lambda: setattr(CNC, '_reads_left', 1)).start()
        orig = CNC.resume

        def resume(steps):
            if steps < 0:
                raise OSError(1, 'EPERM')
            orig(steps)
        CNC.resume = resume
        try:
            aborted = self.m._run_loop(pausable=True)
        finally:
            del CNC.resume
        self.assertFalse(aborted)
        self.assertEqual(job_events(), ['print:paused', 'print:resumed'])
        self.assertIn(('resume', 1), CNC.writes)
        self.assertNotIn(('resume', 1950), CNC.writes)
        self.assertNotIn(('resume', 0), CNC.writes)

    def test_short_history_shortens_the_retrace_and_the_lead(self):
        # A pause early in a program, or on a ring a live feed has just
        # topped up, has less than the factory distance to walk back over.
        # The retrace shortens to what is there and the lead follows it,
        # keeping the 50-tick overlap the factory constants describe.
        CNC.backtrack_budget = 500
        SW.press(delay=0.05)
        SW.press(delay=0.40)
        threading.Timer(0.6, lambda: setattr(CNC, '_reads_left', 1)).start()
        aborted = self.m._run_loop(pausable=True)
        self.assertFalse(aborted)
        self.assertIn(('resume', -500), CNC.writes)
        self.assertIn(('resume', 450), CNC.writes)
        self.assertEqual(job_events(), ['print:paused', 'print:resumed'])

    def test_no_history_holds_and_resumes_lit(self):
        # Nothing to walk back over: no backward run is asked for at all,
        # and the resume puts the beam back on where it stopped.
        CNC.backtrack_budget = 0
        SW.press(delay=0.05)
        SW.press(delay=0.40)
        threading.Timer(0.6, lambda: setattr(CNC, '_reads_left', 1)).start()
        aborted = self.m._run_loop(pausable=True)
        self.assertFalse(aborted)
        self.assertFalse([w for w in CNC.writes if w[0] == 'resume' and w[1] < 0])
        self.assertIn(('resume', 1), CNC.writes)
        self.assertNotIn(('resume', 0), CNC.writes)
        self.assertEqual(job_events(), ['print:paused', 'print:resumed'])

    def test_unreadable_budget_asks_for_the_configured_distance(self):
        # No readback (an older module): ask for the factory distance and
        # let the kernel be the one to refuse it.
        CNC.max_backtrack_error = OSError(2, 'ENOENT')
        SW.press(delay=0.05)
        SW.press(delay=0.40)
        threading.Timer(0.6, lambda: setattr(CNC, '_reads_left', 1)).start()
        aborted = self.m._run_loop(pausable=True)
        self.assertFalse(aborted)
        self.assertIn(('resume', -2000), CNC.writes)
        self.assertIn(('resume', 1950), CNC.writes)


class ButtonWaitTests(unittest.TestCase):

    def setUp(self):
        CNC.__init__()
        SW.__init__()
        COOL.__init__()
        del EVENTS[:]
        self.m = make_machine()
        SW.handler = self.m._switch_event
        COOL.armed = True

    def test_press_ends_the_wait(self):
        SW.press(delay=0.05)
        self.m._button_wait({'id': 42})
        self.assertFalse(self.m._running_action_cancelled)
        self.assertNotIn(('laser_latch', 1), CNC.writes)

    def test_lid_open_cancels_the_wait(self):
        SW.open_lid(delay=0.05)
        self.m._button_wait({'id': 42})
        self.assertTrue(self.m._running_action_cancelled)
        self.assertIn(('laser_latch', 1), CNC.writes)
        self.assertFalse(COOL.armed)

    def test_interlock_open_cancels_the_wait(self):
        SW.open_interlock(delay=0.05)
        self.m._button_wait({'id': 42})
        self.assertTrue(self.m._running_action_cancelled)
        self.assertIn(('laser_latch', 1), CNC.writes)

    def test_press_with_lid_open_does_not_arm(self):
        SW.word[InputSwitch.SW_DOORS] = False
        SW.press(delay=0.02)
        self.m._button_wait({'id': 42})
        self.assertTrue(self.m._running_action_cancelled)
        self.assertIn(('laser_latch', 1), CNC.writes)


class StartGateTests(unittest.TestCase):

    def setUp(self):
        CNC.__init__()
        SW.__init__()
        COOL.__init__()
        del EVENTS[:]
        self.m = make_machine()
        SW.handler = self.m._switch_event

    def test_lid_open_at_start_cancels_a_gated_job(self):
        SW.word[InputSwitch.SW_DOORS] = False
        self.m._motion({'id': 42, 'action_type': 'motion', 'motion_url': 'x'})
        self.assertTrue(self.m._running_action_cancelled)
        self.assertNotIn(('run', 1), CNC.writes)

    def test_interlock_open_at_start_cancels_a_gated_job(self):
        SW.word[InputSwitch.SW_INTERLOCK] = True
        self.m._motion({'id': 42, 'action_type': 'print', 'motion_url': 'x'})
        self.assertTrue(self.m._running_action_cancelled)

    def test_hunt_start_gate_ignores_the_lid(self):
        SW.word[InputSwitch.SW_DOORS] = False
        self.assertTrue(self.m._safe_to_move(lid_gated=False))
        self.assertFalse(self.m._safe_to_move(lid_gated=True))


if __name__ == '__main__':
    unittest.main()


class ReturnHomeTests(unittest.TestCase):

    def setUp(self):
        CNC.__init__()
        SW.__init__()
        COOL.__init__()
        del EVENTS[:]
        self.m = make_machine()
        SW.handler = self.m._switch_event
        self.parks = []
        machine_mod.generate_linear_puls = lambda x, y, dev: self.parks.append((x, y))

    def tearDown(self):
        machine_mod.generate_linear_puls = lambda x, y, dev: None

    def test_park_clears_the_ring_before_it_runs(self):
        # The job did not play out (a print aborted mid-run, or one cancelled
        # at the button wait): what it left in the ring must not play ahead
        # of the park.
        CNC.xy_steps = (500, 300)
        CNC.run_reads = 3
        self.m._return_home(None)
        w = CNC.writes
        self.assertIn(('clear_pulse', 1), w)
        self.assertIn(('run', 1), w)
        self.assertLess(w.index(('clear_pulse', 1)), w.index(('run', 1)))
        self.assertEqual(self.parks, [(-500, -300)])
        self.assertEqual(job_events(), ['print:return_to_home:succeeded'])

    def test_park_at_the_start_runs_nothing(self):
        # Cancelled before it moved (the button wait): the ring holds the
        # whole print; nothing may run, the head is already at the start.
        CNC.xy_steps = (0, 0)
        self.m._return_home(None)
        self.assertIn(('clear_pulse', 1), CNC.writes)
        self.assertNotIn(('run', 1), CNC.writes)
        self.assertEqual(self.parks, [])
        self.assertEqual(job_events(), ['print:return_to_home:succeeded'])

    def test_refused_clear_parks_nothing(self):
        # The kernel refuses the clear (still running): never park on top of
        # an uncleared ring, and never claim success.
        CNC.xy_steps = (500, 300)
        CNC.clear_error = OSError(1, 'EPERM')
        self.m._return_home(None)
        self.assertNotIn(('run', 1), CNC.writes)
        self.assertEqual(self.parks, [])
        self.assertEqual(job_events(), [])


class FeedWatchdogTests(unittest.TestCase):
    """A live feed that wedges must not be left to play the ring dry: the
    job is stopped and retraced while there is still history to retrace
    over, and picked back up if the feed catches up."""

    def setUp(self):
        CNC.__init__()
        SW.__init__()
        COOL.__init__()
        del EVENTS[:]
        del LEDS.button_colors[:]
        self.m = make_machine()
        SW.handler = self.m._switch_event
        self._stall, self._recover = machine_mod.FEED_STALL_S, machine_mod.FEED_RECOVER_S
        machine_mod.FEED_STALL_S = 0.2

    def tearDown(self):
        machine_mod.FEED_STALL_S = self._stall
        machine_mod.FEED_RECOVER_S = self._recover
        self.m._feeder = None

    def test_stalled_feed_holds_the_job_and_resumes_when_it_moves(self):
        machine_mod.FEED_RECOVER_S = 5.0
        feeder = FakeFeeder(written=1000)
        self.m._feeder = feeder
        SW._later(0.6, lambda: setattr(feeder, 'moving', True))
        SW._later(1.2, lambda: setattr(CNC, '_reads_left', 1))

        aborted = self.m._run_loop(pausable=True)

        self.assertFalse(aborted)
        self.assertFalse(self.m._running_action_cancelled)
        w = CNC.writes
        self.assertIn(('stop', 1), w)               # stopped before the ring ran dry
        self.assertIn(('resume', -2000), w)         # retraced like a pause
        self.assertIn(('resume', 1950), w)          # and led back on over cut ground
        self.assertEqual(job_events(), ['print:paused', 'print:resumed'])

    def test_feed_that_never_moves_cancels_the_job(self):
        machine_mod.FEED_RECOVER_S = 0.5
        self.m._feeder = FakeFeeder(written=1000)

        aborted = self.m._run_loop(pausable=True)

        self.assertTrue(aborted)
        self.assertTrue(self.m._running_action_cancelled)
        self.assertIn(('stop', 1), CNC.writes)
        # Held, then given up on: the job never claims to have finished.
        self.assertEqual(job_events(), ['print:paused'])

    def test_a_full_ring_is_not_a_stalled_feed(self):
        # The feeder has written nothing for the whole run because there is
        # no room to write into. That is a healthy feed with a full window,
        # and stopping the job for it would be the watchdog inventing a
        # fault.
        CNC.free = 0
        CNC.run_reads = 12                          # the program ends on its own
        self.m._feeder = FakeFeeder(written=1000)

        aborted = self.m._run_loop(pausable=True)

        self.assertFalse(aborted)
        self.assertNotIn(('stop', 1), CNC.writes)
        self.assertEqual(job_events(), [])

    def test_a_job_that_fits_the_ring_is_never_watched(self):
        # Nothing left to feed: end-of-data is the end of the job.
        CNC.run_reads = 12
        self.m._feeder = FakeFeeder(written=1000, finished=True)

        aborted = self.m._run_loop(pausable=True)

        self.assertFalse(aborted)
        self.assertNotIn(('stop', 1), CNC.writes)

    def test_a_press_while_held_for_the_feed_is_not_lost(self):
        # Pausing a job that is already stopped is not a thing the machine
        # can do, so the press waits for the resume and pauses then.
        machine_mod.FEED_RECOVER_S = 5.0
        feeder = FakeFeeder(written=1000)
        self.m._feeder = feeder
        SW.press(delay=0.6)                         # while the job is held
        SW._later(0.9, lambda: setattr(feeder, 'moving', True))
        SW.press(delay=1.8)                         # resume from that pause
        SW._later(2.4, lambda: setattr(CNC, '_reads_left', 1))

        aborted = self.m._run_loop(pausable=True)

        self.assertFalse(aborted)
        self.assertEqual(job_events(),
                         ['print:paused', 'print:resumed',    # the watchdog's hold
                          'print:paused', 'print:resumed'])   # the operator's press
