"""
(C) Copyright 2020
Scott Wiederhold, s.e.wiederhold@gmail.com
https://community.openglow.org
SPDX-License-Identifier:    MIT
"""
from gfhardware._common import read_file


def serial() -> int:
    return int(read_file('/sys/fsl_otp/HW_OCOTP_MAC0'), 16)


def hostname() -> str:
    mid = serial()
    ser = ""
    while int(mid) > 0 and len(ser) < 6:
        ser = 'BCDFGHJKMQRTVWXY2346789'[int(mid) % 23] + ser
        mid = mid / 23

    return "{}-{}".format(ser[:3], ser[3:])


def password() -> str:
    pw = ""
    for word in range(8):
        pw += "%08x" % int(read_file('/sys/fsl_otp/HW_OCOTP_SRK%d' % word), 16)
    return pw
