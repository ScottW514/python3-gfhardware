"""
(C) Copyright 2020
Scott Wiederhold, s.e.wiederhold@gmail.com
https://community.openglow.org
SPDX-License-Identifier:    MIT
"""

if __name__ == '__main__':
    import argparse
    from PIL import Image
    from gfhardware.cam import GFCam, GFCAM_WIDTH, GFCAM_HEIGHT, GFCAM_LID, GFCAM_HEAD
    parser = argparse.ArgumentParser(description='Capture jpeg image from Glowforge camera.')
    parser.add_argument('--head', action='store_true',
                       help='Capture from head camera [default: lid camera]')
    parser.add_argument('filename', action='store',
                        default="capture.jpeg", type=str,
                        nargs='?',
                        help='Specify output filename [default: capture.jpeg]')
    args = parser.parse_args()

    camera = GFCAM_LID
    if args.head:
        camera = GFCAM_HEAD

    Image.frombytes("RGB", (GFCAM_WIDTH, GFCAM_HEIGHT), GFCam(camera).capture()).save(args.filename)
