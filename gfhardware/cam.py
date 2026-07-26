"""
(C) Copyright 2026
Scott Wiederhold, s.e.wiederhold@gmail.com
https://community.openglow.org
SPDX-License-Identifier:    MIT

Glowforge camera capture for the mainline imx-media stack.

The factory firmware drove a single NXP mxc_v4l2_capture node (/dev/video0)
that exposed the sensor controls directly and selected the lid/head camera via
a private V4L2_CID_GLOWFORGE_SEL_CAM control. ForgeFIRM runs mainline
imx-media, which is a media-controller graph: links and per-pad formats must be
configured before the capture node will stream, the sensor controls live on the
sensor subdev (not the capture node), camera selection is the video-mux input,
and there is no flash-LED control -- the bed/head is lit with the lid_led / head
white_led instead.

This module configures that pipeline (via media-ctl / v4l2-ctl) and the lighting
(via gfhardware.leds), then hands the streaming capture node to the _cam C
extension which grabs one frame, debayers it, and JPEG-encodes it. Both cameras
share one video-mux -> MIPI CSI-2 -> IPU CSI path to the 'ipu1_csi0 capture'
video node; only the mux input and the illumination LED differ.
"""
import logging
import re
import subprocess
import time

from gfhardware._cam import grab
from gfhardware._common import LOGGER_NAME, SYSFS_GF_BASE, read_file, write_attr

logger = logging.getLogger(LOGGER_NAME)

# Camera selection (identical to the factory cam module's constants).
GFCAM_LID = 0
GFCAM_HEAD = 1

# OV5648/OV8856 full-resolution raw-Bayer frame.
GFCAM_WIDTH = 2592
GFCAM_HEIGHT = 1944

# imx-media capture entity (ipu1_csi0) and the media-bus format carried on
# every pad of the active path. 'field:none' is mandatory: without it link
# validation rejects STREAMON with -EPIPE. The /dev/videoN node is resolved
# from the entity name at capture time: imx-media registers several video
# nodes from modules, so the number depends on probe order (a hardcoded
# /dev/video4 broke whenever it shifted - audit N21).
_CAPTURE_ENTITY = 'ipu1_csi0 capture'
_MBUS_FMT = 'SBGGR8_1X8/%dx%d field:none' % (GFCAM_WIDTH, GFCAM_HEIGHT)

# Brief settle after switching the illumination LED on. The lamp is driven via
# the raw PIC register (instant, no fade), so this is just to ensure it is on
# before the grab starts streaming.
_SETTLE_S = 0.2

# The factory mirrored both camera images (HFLIP=1). The ov5648 HFLIP *register*
# breaks imx-media CSI capture (frames never complete -> ipu1_csi0 EOF timeout),
# so the sensor is left un-flipped and the mirror is applied in software by the
# _cam grabber instead. VFLIP was 0 at the factory, so no software vflip needed.
_HFLIP = 1

# Exposure is in 1/16-line units and cannot exceed the frame length: the
# 2592x1944 mode is 1984 lines (vts), so the usable ceiling is ~(1984-margin)*16.
# Beyond it the sensor can't integrate within the frame and returns a blank frame.
_EXPOSURE_MAX = 31600

# Per-camera wiring + manual defaults. Both sensors feed the same video-mux;
# selection is which mux sink link is enabled, and illumination is that camera's
# LED.
#   bus             -- I2C bus the sensor sits on (resolves its media entity by
#                      address, so this works for OV5648 or OV8856 (HD))
#   muxpad          -- video-mux sink pad the sensor is wired to
#   lamp            -- sysfs attribute for that camera's illumination LED (raw
#                      PIC / head register, 10-bit PWM, read+write, no fade)
#   exposure / gain -- per-camera manual defaults (determined empirically)
_CAMERAS = {
    GFCAM_LID:  {'bus': 0, 'muxpad': 0, 'lamp': SYSFS_GF_BASE + 'pic/lid_led',
                 'exposure': 24000, 'gain': 50},
    GFCAM_HEAD: {'bus': 3, 'muxpad': 1, 'lamp': SYSFS_GF_BASE + 'head/white_led',
                 'exposure': 24000, 'gain': 200},
}


def _media_ctl(*args: str) -> str:
    """Run media-ctl (default /dev/media0), returning stripped stdout."""
    return subprocess.run(
        ['media-ctl', *args], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.decode().strip()


def _v4l2_ctl(*args: str) -> None:
    subprocess.run(['v4l2-ctl', *args], check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _capture_device() -> str:
    """Resolve the capture entity's /dev/videoN node at runtime."""
    return _media_ctl('-e', _CAPTURE_ENTITY)


def _sensor_entity(bus: int, required: bool = True):
    """Resolve the camera sensor's media entity name on the given I2C bus.

    Returns e.g. 'ov5648 0-0036', found by I2C address so it works whether an
    OV5648 or an OV8856 (HD model) bound to the shared camera node. Returns None
    when the sensor is absent and not required.
    """
    match = re.search(r'entity \d+: (\S+ %d-0036)\b' % bus, _media_ctl('-p'))
    if match:
        return match.group(1)
    if required:
        raise IOError('no camera sensor found on i2c-%d' % bus)
    return None


def _configure_pipeline(cam_sel: int, sensor: str, other_sensor) -> None:
    """Route the selected sensor through the video-mux to the capture node and
    propagate the raw-Bayer format along every pad of the active path.

    Exactly one sensor link on the video-mux may be enabled -- if both are, the
    CSI cannot resolve a unique upstream sensor (get_mbus_config / link-freq
    lookup) -- so the other camera's link is disabled first.
    """
    cam = _CAMERAS[cam_sel]
    other = _CAMERAS[GFCAM_HEAD if cam_sel == GFCAM_LID else GFCAM_LID]

    links = []
    if other_sensor:
        links.append('"%s":0 -> "video-mux":%d [0]' % (other_sensor, other['muxpad']))
    links += [
        '"%s":0 -> "video-mux":%d [1]' % (sensor, cam['muxpad']),
        '"video-mux":2 -> "imx6-mipi-csi2":0 [1]',
        '"imx6-mipi-csi2":1 -> "ipu1_csi0_mux":0 [1]',
        '"ipu1_csi0_mux":5 -> "ipu1_csi0":0 [1]',
        '"ipu1_csi0":2 -> "ipu1_csi0 capture":0 [1]',
    ]
    for link in links:
        _media_ctl('-l', link)

    pads = [
        '"%s":0' % sensor,
        '"video-mux":%d' % cam['muxpad'], '"video-mux":2',
        '"imx6-mipi-csi2":0', '"imx6-mipi-csi2":1',
        '"ipu1_csi0_mux":0', '"ipu1_csi0_mux":5',
        '"ipu1_csi0":0', '"ipu1_csi0":2',
    ]
    for pad in pads:
        _media_ctl('-V', '%s [fmt:%s]' % (pad, _MBUS_FMT))


def _configure_sensor(sensor: str, exposure: int, gain: int) -> None:
    """Apply the factory manual exposure/gain/white-balance on the sensor subdev.
    The auto-clusters must be switched to manual before the manual values take
    effect. Values match the factory gfcam defaults. Note: the sensor flips are
    forced off here -- HFLIP would break CSI capture, so the factory's horizontal
    mirror is applied in software during the grab (see _HFLIP).
    """
    subdev = _media_ctl('-e', sensor)
    if not subdev:
        raise IOError('could not resolve subdev for %s' % sensor)
    _v4l2_ctl('-d', subdev,
              '-c', 'auto_exposure=1',
              '-c', 'gain_automatic=0',
              '-c', 'white_balance_automatic=0')
    _v4l2_ctl('-d', subdev,
              '-c', 'exposure=%d' % exposure,
              '-c', 'gain=%d' % gain,
              '-c', 'red_balance=1100',
              '-c', 'blue_balance=1400',
              '-c', 'horizontal_flip=0',
              '-c', 'vertical_flip=0')


def capture(cam_sel: int = GFCAM_LID, exposure: int = None, gain: int = None,
            illumination: int = 132) -> bytes:
    """Capture a JPEG image from the selected Glowforge camera.

    cam_sel      -- GFCAM_LID (0, bed) or GFCAM_HEAD (1)
    exposure     -- sensor exposure in 1/16-line units; None uses the camera's
                    default. Clamped to _EXPOSURE_MAX -- the frame-length ceiling
                    (a larger value returns a blank frame).
    gain         -- sensor gain, 16..1023; None uses the camera's default.
    illumination -- scene-lighting LED brightness during the grab (lid_led for
                    the lid camera, head white_led for the head)

    Returns the frame as JPEG bytes.
    """
    if cam_sel not in _CAMERAS:
        raise ValueError('cam_sel must be GFCAM_LID (0) or GFCAM_HEAD (1)')

    cam = _CAMERAS[cam_sel]
    if exposure is None:
        exposure = cam['exposure']
    if gain is None:
        gain = cam['gain']
    exposure = max(0, min(exposure, _EXPOSURE_MAX))
    if not 0 <= gain <= 1023:
        raise ValueError('gain must be between 0 and 1023')

    other = _CAMERAS[GFCAM_HEAD if cam_sel == GFCAM_LID else GFCAM_LID]
    sensor = _sensor_entity(cam['bus'])
    other_sensor = _sensor_entity(other['bus'], required=False)

    _configure_pipeline(cam_sel, sensor, other_sensor)
    _configure_sensor(sensor, exposure, gain)

    # Light the scene at the requested level for the grab, then restore the lamp
    # to whatever it was before (don't leave it forced off). If the current level
    # can't be read back, fall back to off.
    lamp = cam['lamp']
    try:
        previous = int(read_file(lamp))
    except (OSError, ValueError):
        previous = None
    write_attr(lamp, illumination)
    try:
        time.sleep(_SETTLE_S)
        return grab(_capture_device(), GFCAM_WIDTH, GFCAM_HEIGHT, _HFLIP)
    finally:
        write_attr(lamp, previous if previous is not None else 0)


__all__ = ['GFCAM_LID', 'GFCAM_HEAD', 'GFCAM_WIDTH', 'GFCAM_HEIGHT', 'capture']
