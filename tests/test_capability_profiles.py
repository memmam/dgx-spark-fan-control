# SPDX-License-Identifier: GPL-2.0-only
"""Compile the capability-profile matcher and check both observed contracts."""

import os
import re
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

SOURCE = (Path(__file__).resolve().parents[1] / "kernel" / "dgx_ec_fan_control.c").read_text()

HISTORICAL_358 = (
    0xFFFF, 2700, 3600, 4500, 5400, 6300, 7200, 8100, 9000,
    10125, 11250, 12375, 13500,
)


def brace_block(source, token):
    start = source.index(token)
    open_at = source.index("{", start)
    depth = 0
    for index in range(open_at, len(source)):
        depth += (source[index] == "{") - (source[index] == "}")
        if depth == 0:
            end = index + 1
            line_end = source.find("\n", end)
            semicolon = source.find(";", end)
            if semicolon != -1 and (line_end == -1 or semicolon < line_end):
                end = semicolon + 1
            return source[start:end]
    raise AssertionError(token)


def extract_function(source, name):
    call = source.index(f"{name}(")
    start = source.rfind("\nstatic ", 0, call) + 1
    open_at = source.index("{", call)
    depth = 0
    for index in range(open_at, len(source)):
        depth += (source[index] == "{") - (source[index] == "}")
        if depth == 0:
            return source[start:index + 1]
    raise AssertionError(name)


SHIM = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
#define U16_MAX UINT16_MAX
#define __packed __attribute__((packed))
#define __le16 uint16_t
#define ARRAY_SIZE(x) (sizeof(x) / sizeof((x)[0]))
static u16 le16_to_cpu(__le16 value) {
 u8 bytes[2];
 memcpy(bytes, &value, sizeof(bytes));
 return (u16)bytes[0] | ((u16)bytes[1] << 8);
}
'''

MAIN = r'''
static struct dgx_ec_fan_capabilities caps_from(const u8 *raw) {
 struct dgx_ec_fan_capabilities capabilities;
 memcpy(&capabilities, raw, sizeof(capabilities));
 return capabilities;
}
int main(void) {
 const u8 raw_358[] = {0x01, 0x00, 0xec, 0x04, 0x28, 0x23, 0x62, 0x07, 0xbc, 0x34};
 const u8 raw_3137[] = {0x01, 0x00, 0xcf, 0x03, 0xfe, 0x10, 0xcf, 0x03, 0xfe, 0x10};
 const struct dgx_ec_fan_profile *older, *newer;
 struct dgx_ec_fan_capabilities caps;
 u8 mutated[10];
 unsigned int i, state;
 static const u16 historical_358[] = {
  0xffff, 2700, 3600, 4500, 5400, 6300, 7200, 8100, 9000,
  10125, 11250, 12375, 13500
 };
 assert(dgx_ec_profiles_valid());
 caps = caps_from(raw_358);
 older = dgx_ec_match_profile(&caps);
 caps = caps_from(raw_3137);
 newer = dgx_ec_match_profile(&caps);
 assert(older && newer && older != newer);
 assert(!strcmp(older->name, "EC 3.5.8"));
 assert(!strcmp(newer->name, "EC 3.13.7"));
 assert(!older->telemetry_if_floor_unsupported);
 assert(newer->telemetry_if_floor_unsupported);
 assert(older->fan0_min == 1260 && older->fan0_max == 9000);
 assert(older->fan1_min == 1890 && older->fan1_max == 13500);
 assert(newer->fan0_min == 975 && newer->fan0_max == 4350);
 assert(newer->fan1_min == 975 && newer->fan1_max == 4350);
 for (state = 0; state < 13; state++)
  assert(older->floors[state] == historical_358[state]);
 for (state = 1; state < 13; state++) {
  u16 floor = 2000 + (4350 - 2000) * (state - 1) / 11;
  assert(newer->floors[state] == floor);
  assert(floor >= 2000 && floor <= 4350);
  assert(floor > newer->floors[state - 1] || state == 1);
 }
 assert(newer->floors[0] == 0xffff);
 assert(newer->floors[1] == 2000);
 assert(newer->floors[12] == 4350);
 assert(newer->floors[1] > 1620);
 memcpy(mutated, raw_3137, sizeof(mutated));
 for (i = 0; i < sizeof(mutated); i++) {
  mutated[i] ^= 1;
  caps = caps_from(mutated);
  assert(dgx_ec_match_profile(&caps) == NULL);
  mutated[i] ^= 1;
 }
 caps = caps_from(raw_358);
 caps.revision = 2;
 assert(dgx_ec_match_profile(&caps) == NULL);
 caps = caps_from(raw_358);
 caps.unit_mode = 1;
 assert(dgx_ec_match_profile(&caps) == NULL);
 return 0;
}
'''


class CapabilityProfileTests(unittest.TestCase):
    def test_exact_profiles_and_floor_invariants(self):
        defines = "\n".join(re.findall(r"^#define DGX_EC_.*$", SOURCE, re.M))
        pieces = [
            SHIM,
            defines,
            brace_block(SOURCE, "struct dgx_ec_fan_capabilities {"),
            brace_block(SOURCE, "struct dgx_ec_fan_profile {"),
            brace_block(SOURCE, "static const struct dgx_ec_fan_profile dgx_ec_profiles[]"),
            extract_function(SOURCE, "dgx_ec_match_profile"),
            extract_function(SOURCE, "dgx_ec_profiles_valid"),
            MAIN,
        ]
        with tempfile.TemporaryDirectory(prefix="dgx-profiles-") as directory:
            path = Path(directory) / "profiles.c"
            path.write_text("\n".join(pieces))
            binary = path.with_suffix("")
            result = subprocess.run(shlex.split(os.environ.get("CC", "cc")) + [
                "-std=c11", "-Wall", "-Wextra", "-Werror", str(path), "-o", str(binary),
            ], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_historical_358_table_is_not_derived_from_3137_span(self):
        self.assertEqual(HISTORICAL_358[1], 2700)
        self.assertEqual(HISTORICAL_358[12], 13500)
        self.assertLess(2700 * 4350 // 13500, 975)


if __name__ == "__main__":
    unittest.main()
