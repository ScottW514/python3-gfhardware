"""
(C) Copyright 2020
Scott Wiederhold, s.e.wiederhold@gmail.com
https://community.openglow.org
SPDX-License-Identifier:    MIT
"""

if __name__ == '__main__':
    import argparse
    from gfhardware.cam import capture, GFCAM_LID, GFCAM_HEAD
    parser = argparse.ArgumentParser(description='CaptureThread jpeg image from Glowforge camera.')
    parser.add_argument('--head', action='store_true',
                        help='CaptureThread from head camera [default: lid camera]')
    parser.add_argument('filename', action='store',
                        default="capture.jpeg", type=str,
                        nargs='?',
                        help='Specify output filename [default: capture.jpeg]')
    parser.add_argument('exposure', action='store',
                        default=3000, type=int,
                        nargs='?',
                        help='Specify exposure [range: 0-65535, default: 3000]')
    parser.add_argument('gain', action='store',
                        default=30, type=int,
                        nargs='?',
                        help='Specify gain [range: 0-1023, default: 30]')
    args = parser.parse_args()

    camera = GFCAM_LID
    if args.head:
        camera = GFCAM_HEAD

    with open(args.filename, 'wb') as f:
        f.write(capture(camera, args.exposure, args.gain))
