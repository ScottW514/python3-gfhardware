"""
(C) Copyright 2020
Scott Wiederhold, s.e.wiederhold@gmail.com
https://community.openglow.org
SPDX-License-Identifier:    MIT
"""
from typing import Union


def read_file(filename, binary: bool = False) -> Union[str, bytes]:
    with open(filename, 'br' if binary else 'r') as f:
        return f.read() if binary else f.read().strip()


def write_file(filename: str, val: Union[str, bytes], binary: bool = False):
    with open(filename, 'bw' if binary else 'w') as file:
        file.write(val)

