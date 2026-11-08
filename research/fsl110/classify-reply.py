#!/usr/bin/env python3
"""Classify an fsl_reply proc snapshot. Not an EC acceptance."""

import re
import sys


def field(text: str, name: str):
    match = re.search(rf"^{name} (\d+)$", text, re.M)
    if match is None:
        return None
    return int(match.group(1))


W_LINE = re.compile(
    r"^W (\d+) pre_a0=([0-9a-fA-F]+) pre_a4=([0-9a-fA-F]+) "
    r"post_a0=([0-9a-fA-F]+) post_a4=([0-9a-fA-F]+) "
    r"handler=(-?\d+) post_ok=([01])$"
)


def classify(text: str) -> str:
    writes = field(text, "writes")
    dropped_w = field(text, "dropped_writes")
    dropped_r = field(text, "dropped_reads")
    missed = field(text, "nmissed")
    truncated = field(text, "truncated")
    listed = [line for line in text.splitlines() if line.startswith("W ")]
    reasons = []
    if None in (writes, dropped_w, dropped_r, missed, truncated):
        reasons.append("malformed")
    else:
        if writes != len(listed):
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
        bad_format = 0
        bad_copy = 0
        bad_opcode = 0
        for line in listed:
            match = W_LINE.match(line)
            if match is None:
                bad_format += 1
                continue
            if match.group(7) != "1":
                bad_copy += 1
            if int(match.group(3), 16) & 0xFF != 0x09:
                bad_opcode += 1
        if bad_format:
            reasons.append("record_format")
        if bad_copy:
            reasons.append("post_ok")
        if bad_opcode:
            reasons.append("opcode")
    if reasons:
        return "INCONCLUSIVE " + ",".join(reasons)
    return "CAPTURE_COMPLETE"


def main() -> int:
    print(classify(sys.stdin.read()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
