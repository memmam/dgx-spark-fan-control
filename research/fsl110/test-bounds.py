#!/usr/bin/env python3
"""Fail closed if the research requests leave the reviewed band."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def request(level: int) -> int:
    return 975 + (3375 * level) // 100


assert request(100) == 4350
assert request(110) == 4687
assert request(120) == 5025
assert request(237) == 8973
assert request(238) == 9007
assert request(1912) == 65505
assert request(1913) == 65538

ramp_c = (ROOT / "fsl_ramp_kensaki.c").read_text()
probe_c = (ROOT / "fsl_probe_atarime.c").read_text()
ramp_sh = (ROOT / "ramp-kensaki.sh").read_text()
probe_sh = (ROOT / "probe-atarime.sh").read_text()

c_levels = [int(x) for x in re.findall(r"(\d+)ULL", ramp_c)]
assert c_levels[0] == 160
assert c_levels[-1] == 237
assert 120 not in c_levels and 150 not in c_levels
assert 238 not in c_levels
assert 1912 not in c_levels and 1913 not in c_levels
assert all(request(level) <= 9000 for level in c_levels)
assert request(c_levels[-1]) == 8973

sh_match = re.search(r"steps=\(([^)]*)\)", ramp_sh)
assert sh_match
sh_levels = [int(x) for x in sh_match.group(1).split()]
assert sh_levels == c_levels

assert "120ULL" in probe_c and "130ULL" not in probe_c
assert "PLATEAU_RECORDED" in ramp_sh
assert "STOP_PLATEAU both" in ramp_sh
assert "9500" in ramp_sh
assert "16000" in probe_sh
assert "timeout " not in ramp_sh and "timeout " not in probe_sh
assert "loaded_by_us" in ramp_sh and "loaded_by_us" in probe_sh
for text in (ramp_sh, probe_sh):
    assert "zones_ok" in text and "gpu_idle" in text
    assert "STOP_READ" in text and "STOP_TEMP" in text

print("bounds_ok", [(level, request(level)) for level in c_levels])
