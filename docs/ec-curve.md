# NVIDIA's EC fan curve, measured

This is what the firmware curve (state 0, service stopped) did on two DGX
Sparks under `gpu_burn 600` (FP32, about 96% GPU use), logged once a second.
The two units are different hardware revisions (flat foot and ridged foot).
Both behaved the same way.

## Two fan tables in the firmware

In [forum post 16](https://forums.developer.nvidia.com/t/new-dgx-spark-fans-barely-speed-up-at-high-temps-and-cant-be-adjusted-critical-please-open-fan-control/386805/16),
maxeffort reported that the EC image on LVFS (`0x03000d07`, EC 3.13.7)
contains two fan curves, and that a unit's curve is fixed once, at boot.
NVIDIA replied in post 25: "There should only be one fan curve." The
thread did not check the earlier image.

Both public images contain the same two tables, so this is not a 3.13.7
change. The bytes below are from the LVFS cabinets:

| EC | Cabinet SHA-256 | `ec_fused.cap` SHA-256 | Tables at file offset |
| --- | --- | --- | --- |
| 3.13.7 (`0x03000d07`) | `c3fab69bb1ef6d22e1a1d28a0bbc05319fb321bf81af4fd574349d2b0b23fa30` | `d3c536ec33121f7e0c64994daf3152f86e12fa2018b3d8dd99155c002adf632b` | `0x10d4b` |
| 3.5.8 (`0x03000508`) | `3f220245f0803c4927490d01a8403560687fe6a631ec17e2c2d7359f76c3bef1` | `ab21ddb044108443f741edbe1a0567b04f3c621a1db862a2e870276a6cb82d82` | `0x11d73` |

The 40 bytes at those offsets are identical in both images:

```
1e 00 1e 0a 50 00 28 1e 5a 00 36 14 5f 00 4b 0a 65 00 64 0f
14 00 1e 0a 28 00 2d 1e 46 00 46 14 55 00 64 0a 65 00 64 0f
```

Each entry is four bytes: a little-endian 16-bit temperature, a duty byte,
and a step-down byte. The thermal loop (`0xC2A88` in 3.13.7) picks the highest entry at or below
the current temperature and holds that step until the temperature falls
below the threshold minus the last byte. The °C and percent units are
inferred; the measured steps below match them.

| Table | Temperature → duty (step-down margin) |
| --- | --- |
| Slow | 30 °C → 30% (10), 80 → 40% (30), 90 → 54% (20), 95 → 75% (10), 101 → 100% (15) |
| Fast | 20 °C → 30% (10), 40 → 45% (30), 70 → 70% (20), 85 → 100% (10), 101 → 100% (15) |

The slow table is maxeffort's curve 2, plus a 75% step at 95 °C. The fast
table is their curve 1. A scan of both images for five-entry tables of this
shape finds only these two.

A function returns one table pointer or the other from a flag
(`0xC4CF8` in 3.13.7, `0xC5D4C` in 3.5.8): the slow table when the flag is
set, the fast one otherwise. Board init sets the flag once at boot. It
clears it, calls the boot ROM at `0xF009` with index `0x3F9`, and sets it
only when that call returns 0. The byte it reads is discarded, so only the
call's status matters. Zephyr's Microchip HAL defines
`MEC5_ROM_OTP_RD8_ADDR` as `0xf009`, so this is most likely a read of
one-time-programmable byte `0x3F9`.
Which status retail units get is not confirmed. Five other call sites read
the same flag in each image, so it is most likely a part or board variant
check, not a fan setting. Those sites were not analyzed.

Every unit with posted data runs the slow table: maxeffort's, osos1's
("After multiple reboot mine seems to always be on curve 2"), and the two Sparks measured
below, on both firmwares. No retail unit has been shown on the fast table.

What changed in 3.13.7 is that the EC no longer takes fan requests from the
host, so nothing can lift a unit off the slow table there. This project does
not depend on which table a unit runs. On 3.5.8 a held floor of 12 runs
above every slow-table step below 101 °C. On 3.13.7 the GPU clock cap below
is the remaining lever.

## Steps

The curve holds its base speed until about 80 °C, then steps up:

| Hottest zone | EC 3.5.8 RPM (fan 1 / fan 2) | Share of 9000 / 13500 | EC 3.13.7 RPM (fan 1 / fan 2) |
| --- | --- | --- | --- |
| below about 80 °C | 2700 / 4050 | 30% | about 2085 / 2175 |
| about 80–82 °C | 3600 / 5400 | 40% | about 2595 / 2710 |
| about 90.5–91 °C | 4860 / 7290 | 54% | about 3190 / 3345 |
| about 95 °C | 6660 / 9990 | 74% | about 3910 / 4105 |

These match the slow fan table in the EC image (30% until 80 °C, 40% at 80,
54% at 90, 75% at 95, 100% at 101). Neither unit followed the fast table,
which would have raised the fans by 40 °C. The fans did not move under a
light load that held the chip near 50 °C.

At full load the hottest zone settled at about 93–94 °C on step 3, with the
GPU at 80–81 °C. Step 3 holds the chip there but does not pull it back
below 90 °C.

State 12 (9000 / 13500 RPM on 3.5.8) is 100% on the same scale, so a held
floor of 12 runs the fans faster than any step the firmware curve reaches
below 101 °C.

## EC 3.13.7 versus 3.5.8

On the same unit, the 3.13.7 curve stepped at the same temperatures. It
settled at the same temperatures (about 93.6 °C, GPU 81 °C) and gave the
same `gpu_burn` throughput (18693 versus 18746 Gflop/s, 0 errors on both).
The RPM readings are lower and the two fans read nearly alike, in line with
the 975–4350 RPM range 3.13.7 reports for both fans. Equal temperatures and
throughput suggest the reported scale changed rather than the fan speed.
That is an inference: fan speed was not measured independently.

On 3.13.7 the host cannot set a floor, so the firmware curve is the only
fan control. `status` and `health` print a `clock-cap:` line there. Capping
the GPU clock is the remaining way to run cooler under load:

```sh
sudo nvidia-smi -lgc 0,2200
```

The 2200 MHz figure is the default in MiaAI-Lab's
[sparkLimiter](https://github.com/MiaAI-Lab/sparkLimiter). `sudo nvidia-smi -rgc`
removes the cap.

## After the load stops

- **3.5.8:** the fans step down about one notch every 3–4 seconds and stop
  at 2790 / 4185, slightly above the 2700 / 4050 base. They stayed there at
  least until the chip was back to about 47–49 °C.
- **3.13.7:** the fans glide down smoothly, from step 3 to base in about
  50 seconds, and stop at about 2117 / 2203, slightly above the
  2085 / 2175 seen before the load.

On both, one unit started stepping down at about 88 °C while still under
load, so the steps have some hysteresis.

## Reproducing

Stop the service and run `sudo dgx-fan-control automatic`, then log the
`acpi_fan` hwmon `fan1_input` values, the hottest thermal zone and
`nvidia-smi --query-gpu=temperature.gpu,utilization.gpu` once a second while
a sustained GPU load runs. `gpu-burn` builds with `make` on the Spark without
Docker. Restore any held floor afterward.

## Measuring a lower ACPI floor

The ACPI command refuses levels below 35. Level 30 comes from forum
measurements (about 1940 RPM, with level 20 stalling one fan). Level 35 is
the lowest level measured to hold here: on EC 3.5.8 at idle, with the module
unloaded, a 30-minute hold at 35 kept the fans at 2070/2025 RPM, and the
hottest zone peaked at 53.7 C and settled at 52.8 C. In an earlier run at 35,
fan1 read 2160, one EC step higher. To measure it on your own machine, run
`scripts/acpi-floor-sweep` as root on EC 3.5.8, at idle, with the module
unloaded and `dgx-fan-control.service` stopped:

```
sudo systemctl stop dgx-fan-control.service
sudo modprobe -r dgx_ec_fan_control
sudo scripts/acpi-floor-sweep 40 35 30
```

It holds each level for 180 seconds and logs both fans and the hottest zone
every second to a CSV. It writes level 100 at once if a fan reads 0 or is
missing, a fan drops below 1000 RPM, the hottest zone reaches 60 C, or the run
is interrupted, and it always writes 100 when it finishes. It never writes a
level below 30, its own limit for a supervised run; outside it, 35 is the
lowest level to use. EC 3.13.7 ignores these writes, so a run there measures
nothing.
