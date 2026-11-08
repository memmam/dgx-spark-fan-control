#!/usr/bin/env python3
"""Classify an fsl_reply proc snapshot. Not an EC acceptance."""

import re
import sys


def field(text: str, name: str):
    match = re.search(rf"^{name} (\d+)$", text, re.M)
    if match is None:
        return None
    return int(match.group(1))


def classify(text: str) -> str:
    writes = field(text, "writes")
    dropped_w = field(text, "dropped_writes")
    dropped_r = field(text, "dropped_reads")
    missed = field(text, "nmissed")
    truncated = field(text, "truncated")
    listed = len(re.findall(r"^W ", text, re.M))
    reasons = []
    if None in (writes, dropped_w, dropped_r, missed, truncated):
        reasons.append("malformed")
    else:
        if writes != listed:
            reasons.append("count_mismatch")
        if writes != 4:
            reasons.append(f"writes={writes}")
        if dropped_w:
            reasons.append(f"dropped_writes={dropped_w}")
        if dropped_r:
            reasons.append(f"dropped_reads={dropped_r}")
        if missed:
            reasons.append(f"nmissed={missed}")
        if truncated:
            reasons.append("truncated")
    if reasons:
        return "INCONCLUSIVE " + ",".join(reasons)
    return "CAPTURE_COMPLETE"


def main() -> int:
    print(classify(sys.stdin.read()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
