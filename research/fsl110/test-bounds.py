#!/usr/bin/env python3
"""Fail closed if the research requests leave the reviewed band."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def request(level: int) -> int:
    return 975 + (3375 * level) // 100


assert request(100) == 4350
assert request(110) == 4687
assert request(120) == 5025
assert request(130) == 5362
assert request(140) == 5700
assert request(140) < 7000
assert request(140) < 9000
assert request(1912) == 65505
assert request(1913) == 65538

ramp = (ROOT / "fsl_ramp_kensaki.c").read_text()
probe = (ROOT / "fsl_probe_atarime.c").read_text()
assert "120ULL, 130ULL, 140ULL" in ramp
assert "1912" not in ramp and "1913" not in ramp
assert "65535" not in ramp
assert probe.count("120ULL") == 1
assert "130ULL" not in probe and "140ULL" not in probe

for name in ("ramp-kensaki.sh", "probe-atarime.sh"):
    text = (ROOT / name).read_text()
    assert 'printf \'%s\\n\' step' in text or 'printf \'%s\\n\' run' in text
    assert "1913" not in text
    assert "timeout " not in text

assert "7000" in (ROOT / "ramp-kensaki.sh").read_text()
print("bounds_ok", request(120), request(130), request(140))
