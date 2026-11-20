"""Flooding series F: a separately frozen extension of protocol v3.0.1 (Amendment F).

Every run applies the frozen Low load before the action; after TERMINATION_CONFIRMED a flood controller
raises the load-generator rate to a frozen calibration setting for a fixed window and restores Low before
the delayed read. The series tests whether the retention-headroom rule still classifies record loss when
the bytes written after an action arrive as a burst, whether continuous acquisition still recovers every
primary fact under the burst, and whether the burst alone produces false attribution.

Nothing here changes the frozen v3.0.1 or series-R code, protocol files, evidence or output sets: the
series uses the frozen run, block, scoring and mechanism functions unchanged, with its own parameters
(protocol/flooding/plan.json), its own freeze record (protocol/flooding/FREEZE_F.json, public through
the pushed tag series-F-frozen), its own evidence root and its own outputs.

    python -m frx.flooding --help
"""
