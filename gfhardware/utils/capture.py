"""
(C) Copyright 2020-2026
Scott Wiederhold, s.e.wiederhold@gmail.com
https://community.openglow.org
SPDX-License-Identifier:    MIT
"""

if __name__ == '__main__':
    import argparse
    from gfhardware.cam import capture, GFCAM_LID, GFCAM_HEAD
    parser = argparse.ArgumentParser(description='Capture jpeg image from Glowforge camera.')
    parser.add_argument('--head', action='store_true',
                        help='Capture from head camera [default: lid camera]')
    parser.add_argument('filename', action='store',
                        default="capture.jpeg", type=str,
                        nargs='?',
                        help='Specify output filename [default: capture.jpeg]')
    parser.add_argument('exposure', action='store',
                        default=None, type=int,
                        nargs='?',
                        help='Sensor exposure in 1/16-line units, capped at 31600 [default: per-camera]')
    parser.add_argument('gain', action='store',
                        default=None, type=int,
                        nargs='?',
                        help='Sensor gain, 16-1023 [default: per-camera]')
    parser.add_argument('illumination', action='store',
                        default=132, type=int,
                        nargs='?',
                        help='Scene LED brightness during capture [default: 132]')
    args = parser.parse_args()

    camera = GFCAM_LID
    if args.head:
        camera = GFCAM_HEAD

    with open(args.filename, 'wb') as f:
        f.write(capture(camera, args.exposure, args.gain, args.illumination))
