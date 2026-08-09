Python modules for accessing and controlling Glowforge brand CNC laser hardware.

`forgefirm-app/` contains the ForgeFIRM Glowforge web-service applications
built on these modules: `gfhome.py` (one-shot service-driven homing),
`gfcloud.py` (full cloud-mode controller daemon, with init script), and
`ffmachine.py` (the shared hardware-machine glue both use). They are not part
of the `gfhardware` Python package; the ForgeFIRM image recipes install them
directly from this directory.
