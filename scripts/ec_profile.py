# SPDX-License-Identifier: GPL-2.0-only
"""Read-only EC profile check. Does not change fwupd or load a module.

A visible fwupd Embedded Controller version and a current-boot kernel pin
are evidence. A hidden device is unverified, not a guessed profile. Two
readings that name different versions are a mismatch. Neither reading
stops the caller.
"""

from __future__ import annotations

import json
import re
import sys

EC_FWUPD_GUID = "3d13c989-e6a8-4ead-95ee-921f09868f65"
PIN_RE = re.compile(r"pinned (EC \d+\.\d+\.\d+) capabilities")


def ec_release_name(version: str) -> str | None:
    """Match userspace.ec_release_name. The second byte is skipped."""
    text = version.strip()
    if re.fullmatch(r"\d+\.\d+\.\d+", text):
        return text
    hex_text = text.lower().removeprefix("0x")
    if not re.fullmatch(r"[0-9a-f]{8}", hex_text):
        return None
    major = int(hex_text[0:2], 16)
    minor = int(hex_text[4:6], 16)
    patch = int(hex_text[6:8], 16)
    return f"{major}.{minor}.{patch}"


def _guids(device: dict) -> list[str]:
    guids = device.get("Guid", device.get("Guids", device.get("guid", [])))
    if isinstance(guids, str):
        return [guids.strip().lower()]
    if isinstance(guids, list):
        return [str(item).strip().lower() for item in guids]
    return []


def find_ec_device(payload: object) -> dict | None:
    if isinstance(payload, dict):
        if EC_FWUPD_GUID in _guids(payload):
            return payload
        for value in payload.values():
            found = find_ec_device(value)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = find_ec_device(item)
            if found is not None:
                return found
    return None


def fwupd_release(text: str) -> str | None:
    """Decoded release, or None when the EC device is absent or unreadable."""
    if not text.strip():
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    device = find_ec_device(payload)
    if device is None:
        return None
    version = device.get("Version", device.get("version", ""))
    return ec_release_name(str(version))


def kernel_pins(text: str) -> set[str]:
    found = set()
    for match in PIN_RE.findall(text or ""):
        name = ec_release_name(match.removeprefix("EC "))
        if name:
            found.add(name)
    return found


def classify(fwupd_text: str, kernel_text: str, expect: str) -> str:
    """OK, STOP_EC_MISMATCH, or STOP_EC_UNVERIFIED.

    The stop lines do not include device records.
    """
    if expect not in ("3.5.8", "3.13.7"):
        return "STOP_EC_UNVERIFIED"
    readings: list[str] = []
    fw_ver = fwupd_release(fwupd_text)
    if fw_ver:
        readings.append(fw_ver)
    readings.extend(sorted(kernel_pins(kernel_text)))
    if any(item != expect for item in readings):
        return "STOP_EC_MISMATCH"
    if expect in readings:
        return "OK"
    return "STOP_EC_UNVERIFIED"


def _read_frame(stream: sys.stdin) -> str:
    header = stream.readline()
    if not header:
        return ""
    parts = header.split()
    if len(parts) != 2:
        return ""
    try:
        size = int(parts[1])
    except ValueError:
        return ""
    if size < 0:
        return ""
    blob = stream.read(size)
    stream.read(1)
    return blob


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3 or args[0] != "decide" or args[1] != "--expect":
        print("STOP_EC_UNVERIFIED")
        return 1
    fwupd = _read_frame(sys.stdin)
    kernel = _read_frame(sys.stdin)
    print(classify(fwupd, kernel, args[2]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
