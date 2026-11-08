# SPDX-License-Identifier: GPL-2.0-only
"""Read-only EC profile check. Does not change fwupd or load a module.

Evidence is a visible fwupd Embedded Controller version, the current boot's
kernel pin, or the EFI system resource table version for the same class.
A hidden fwupd device is not a version: updates blocked in fwupd omit it.
A missing pin is not a version: the pin is logged only after the fan module
probes. Disagreement among sources stops the caller. No source stops it too.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

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


def u32_release(version: str) -> str | None:
    """ESRT fw_version is a decimal u32 of the same encoding."""
    text = version.strip()
    if text.lower().startswith("0x"):
        return ec_release_name(text)
    if re.fullmatch(r"[0-9]+", text):
        value = int(text)
        if value > 0xFFFFFFFF:
            return None
        return ec_release_name(f"0x{value:08x}")
    return ec_release_name(text)


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


def esrt_versions(root: Path | None = None) -> str:
    """Raw fw_version lines for the EC class. Empty when the table is absent."""
    entries = Path("/sys/firmware/efi/esrt/entries") if root is None else root
    if not entries.is_dir():
        return ""
    found = []
    for entry in sorted(entries.glob("entry*")):
        try:
            fw_class = (entry / "fw_class").read_text(
                encoding="ascii", errors="replace").strip().lower()
            version = (entry / "fw_version").read_text(
                encoding="ascii", errors="replace").strip()
        except OSError:
            continue
        if fw_class == EC_FWUPD_GUID and version:
            found.append(version)
    return "\n".join(found)


def esrt_releases(text: str) -> set[str]:
    found = set()
    for line in (text or "").splitlines():
        name = u32_release(line.strip())
        if name:
            found.add(name)
    return found


def kernel_pins(text: str) -> set[str]:
    found = set()
    for match in PIN_RE.findall(text or ""):
        name = ec_release_name(match.removeprefix("EC "))
        if name:
            found.add(name)
    return found


def classify(fwupd_text: str, kernel_text: str, expect: str,
             esrt_text: str = "") -> str:
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
    readings.extend(sorted(esrt_releases(esrt_text)))
    if any(item != expect for item in readings):
        return "STOP_EC_MISMATCH"
    if expect in readings:
        return "OK"
    return "STOP_EC_UNVERIFIED"


def explain(fwupd_text: str, kernel_text: str, esrt_text: str, expect: str) -> str:
    """One line. Versions only, no device record and no path."""
    fw_ver = fwupd_release(fwupd_text) or "absent"
    pins = kernel_pins(kernel_text)
    tables = esrt_releases(esrt_text)
    pin = ",".join(sorted(pins)) if pins else "absent"
    esrt = ",".join(sorted(tables)) if tables else "absent"
    verdict = classify(fwupd_text, kernel_text, expect, esrt_text)
    return f"fwupd={fw_ver} pin={pin} esrt={esrt} verdict={verdict}"


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
    if args == ["esrt"]:
        text = esrt_versions()
        if text:
            print(text)
        return 0
    if len(args) != 3 or args[0] not in ("decide", "explain") or args[1] != "--expect":
        print("STOP_EC_UNVERIFIED")
        return 1
    fwupd = _read_frame(sys.stdin)
    kernel = _read_frame(sys.stdin)
    esrt = _read_frame(sys.stdin)
    if args[0] == "explain":
        print(explain(fwupd, kernel, esrt, args[2]))
    else:
        print(classify(fwupd, kernel, args[2], esrt))
    return 0


if __name__ == "__main__":
    sys.exit(main())
