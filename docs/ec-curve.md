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
change. The bytes below are from the LVFS cabinets. The cabinet and
`ec_fused.cap` SHA-256 values were checked against those files; for 3.13.7
they also match the checksums in the LVFS `firmware.xml` / cabinet
metainfo:

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
and a step-down byte. In this disassembly the thermal loop (`0xC2A88` in
3.13.7) picks the highest entry at or below the current temperature and
holds that step until the temperature falls below the threshold minus the
last byte. The °C and percent units are inferred; the measured steps below
match them.

| Table | Temperature → duty (step-down margin) |
| --- | --- |
| Slow | 30 °C → 30% (10), 80 → 40% (30), 90 → 54% (20), 95 → 75% (10), 101 → 100% (15) |
| Fast | 20 °C → 30% (10), 40 → 45% (30), 70 → 70% (20), 85 → 100% (10), 101 → 100% (15) |

The slow table is maxeffort's curve 2, plus a 75% step at 95 °C. The fast
table is their curve 1. A scan of both images for five-entry tables of this
shape finds only these two.

In this disassembly a function returns one table pointer or the other from
a flag (`0xC4CF8` in 3.13.7, `0xC5D4C` in 3.5.8): the slow table when the
flag is set, the fast one otherwise. Board init sets the flag once at boot.
It clears it, calls the boot ROM at `0xF009` with index `0x3F9`, and sets
it only when that call returns 0. The byte it reads is discarded, so only
the call's status matters. Zephyr's Microchip HAL defines
`MEC5_ROM_OTP_RD8_ADDR` as `0xf009`, so this is most likely a read of
one-time-programmable byte `0x3F9`. Which status retail units get is not
confirmed. Five other call sites read the same flag in each image, so it is
most likely a part or board variant check, not a fan setting. Those sites
were not analyzed.

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
54% at 90, 75% at 95, 100% at 101), with 74% measured against 75% in the
table byte at about 95 °C. Neither unit followed the fast table, which
steps up 40 °C sooner (its first step at 40 °C versus 80 °C). The fans did
not move under a light load that held the chip near 50 °C.

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
sudo systemctl start dgx-fan-watchdog.service
sudo scripts/acpi-floor-sweep 40 35
```

It holds each level for 180 seconds and logs both fans and the hottest zone
every second to a CSV. It writes level 100 at once if a fan reads 0 or is
missing, a fan drops below 1000 RPM, the hottest zone reaches 60 C, or the run
is interrupted, and it always writes 100 when it finishes. It never writes a
level below 35, the lowest level to use. EC 3.13.7 ignores ACPI fan levels, so the script refuses
before any write when `dgx_ec_fan` is present without `dgx_ec_fan_floor`, or
when this boot's kernel log shows the module pinned EC 3.13.7. The shared
writer also requires positive 3.5.8 identification from the kernel log or
ESRT and an active independent watchdog; unknown firmware is refused.

## EC 3.5.8: ACPI holds replace the stock curve

**Do not hold an ACPI level under sustained full load. Level 100 is not
full fan speed, and stopping writes does not restore the EC curve.** Treat
any ACPI write on 3.5.8 as a persistent replacement of the EC curve until
reboot. There is no verified live ACPI-to-EC handback; see
[recorded restoration failures](validation.md).

On kensaki on November 6, 2026 (1:18–1:27 PM PST), with EC 3.5.8, the rig
off, and `gpu_burn` running, ACPI 100 held both fans at **4320/4320 RPM**
while the hottest zone reached **95–97.7 C**. Fanwatch read **96–97 C SoC**
for about 15 seconds without an EC override. At 9000/13500 RPM full scale,
4320/4320 is about **48%/32% duty**, not the stock curve's top step.
Without ACPI writes, that Spark reached **4860/7290 RPM by about 94.5 C**,
then **6660/9990 at about 95 C**, and settled at **88.8–89.7 C**.
These are measured runs, not a promise that every machine has the same
thresholds or sensor offsets. The burn view's GPU temperature can read
about **10 C below the hottest thermal zone**.

The independent `dgx-fan-watchdog.service` now monitors this project's ACPI
holds even after the writing command exits. Both `acpi-max` (including
stall repair) and `scripts/acpi-floor-sweep` arm a boot-scoped hold record
**before** writing. They refuse without an active watchdog, with invalid
thermal configuration, or without positive EC 3.5.8 identification from the
kernel's pinned version or the EC's ESRT GUID/version. EC 3.13.7 remains
telemetry-only: no ACPI write. The minimum is **35**, including the sweep;
34 is refused. ACPI 0 is never a release value and must never be written.
Install the updated userland and restart the watchdog before using these
commands; merely running an older watchdog is not protection. Direct sysfs
writes and other writers outside this project are not tracked or interlocked.

### Escalation and recovery

All thresholds refer to the **hottest thermal zone**, never the burn view:

1. **80 C:** force level 100 on every ACPI Fan cooler that is not already
   at 100, log at critical severity, and latch 100 until reboot. Lower-level
   writes from this project are then refused, including another sweep.
2. **85 C:** persist an incident latch and start the optional stop hook
   asynchronously. With no hook, a start failure, or a failed hook, send
   SIGTERM to eligible workload processes immediately. Otherwise allow
   **10 seconds** for the hook to cool the hottest zone below **83 C**;
   if it does not, send SIGTERM. A timed-out hook is killed without waiting
   for it. There is no workload restart or resume action.
3. **90 C:** preempt the hook and all SIGTERM grace, and send SIGKILL.
   Otherwise, after **10 seconds** following SIGTERM, send SIGKILL if a
   selected process is still alive or the hottest zone remains at least
   **83 C**. Newly discovered compute processes are included.
4. **95 C**, or still at least **83 C** **5 seconds** after SIGKILL:
   request clean shutdown with `systemctl poweroff`. At 95 C the stop/kill
   stages and shutdown execute in the same poll without waiting for graces.
   Failed shutdown requests are logged and retried. Shutdown is enabled
   unless explicitly disabled in configuration. An unreadable hottest zone
   while an ACPI hold is armed and the fans are spinning or unknown is
   treated conservatively as the final threshold; this can stop work and
   shut down the machine. A verified true zero (both tachometers at 0 RPM)
   does not: an unreadable zone stays on the existing true-zero rule, which
   does not power off without a readable zone at or above 85 C.
5. **Successful recovery:** once the hottest zone stays below **78 C**
   continuously for **30 seconds**, verify there are no remaining selected,
   GPU-compute, or configured target processes, no protected live targets,
   and no failed/ambiguous compute query. Then request `systemctl reboot`
   to restore EC control. Failed recovery reboots are retried while cool;
   continued heat still takes the shutdown path. After that reboot command
   has been accepted, an unreadable sample does not replace it with
   poweroff; a later finite reading at or above STOP does. This is not a
   re-arm rule. During a latched incident, legacy cold-stall/foreign-floor
   reboot paths cannot bypass this recovery verification; the hot true-zero
   shutdown still takes precedence.

The incident latch survives reboot. It inhibits `acpi-max`, the sweep,
stall-repair ACPI writes, daemon curve startup, boot restoration of a manual
floor, and `set-state`. After the reboot, inspect cooling and explicitly run:

```sh
sudo dgx-fan-control acpi-incident-reset
```

Reset refuses in the incident/ACPI-hold boot or unless the hottest zone is
below MAX minus HYSTERESIS (78 C by default). It writes no fan level and
starts no service or job. `automatic` does not clear this latch. Existing
external workload/service restart policies are outside this tool: configure
those policies or the stop hook to respect `/var/lib/dgx-fan-control/acpi-incident`
if they must remain stopped across reboot. The fan tool itself never starts
or resumes a workload.

Defaults are deliberately early: on kensaki the hottest zone rose about
**0.07 C/s between 88 and 95 C** under `gpu_burn`, while others reported
faster **90 → 98 → power-cut** ramps. One burn does not establish safe
timing. Userspace polling, process discovery, systemd, and shutdown latency
cannot guarantee protection against a rapid hardware trip.

The existing **85 C true-zero fan poweroff is separate and unchanged**.
This new policy applies only after a project ACPI write; ordinary spinning
fans on the stock EC curve do not arm it. The old true-zero hot shutdown
retains precedence over hook/process probing. Invalid new thermal settings
refuse new ACPI holds; the watchdog logs the error and uses safe thermal
defaults rather than abandoning its true-zero monitoring.

### Configuration and process safeguards

Set these in `/etc/default/dgx-fan-control`, then restart
`dgx-fan-watchdog.service`. They are root configuration, not environment
variables. Existing installations keep their defaults file; add new keys
explicitly when customizing. Missing keys use these defaults:

| Key | Default | Meaning |
| --- | --- | --- |
| `ACPI_THERMAL_MAX_C` | `80` | Force and latch level 100 |
| `ACPI_THERMAL_STOP_C` | `85` | Hook/SIGTERM threshold |
| `ACPI_THERMAL_KILL_C` | `90` | Immediate SIGKILL threshold |
| `ACPI_THERMAL_SHUTDOWN_C` | `95` | Immediate post-kill shutdown threshold |
| `ACPI_THERMAL_HOOK_SECONDS` | `10` | Hook cooling grace |
| `ACPI_THERMAL_TERM_SECONDS` | `10` | SIGTERM grace |
| `ACPI_THERMAL_KILL_SECONDS` | `5` | Post-kill cooling grace |
| `ACPI_THERMAL_COOL_SECONDS` | `30` | Continuous cool verification before recovery reboot |
| `ACPI_THERMAL_HYSTERESIS_C` | `2` | STOP minus this is adequate emergency cooling; MAX minus this is recovery/reset temperature |
| `ACPI_THERMAL_SHUTDOWN` | `yes` | Only explicit `no` disables this new shutdown stage, not true-zero poweroff or recovery reboot |
| `ACPI_THERMAL_STOP_HOOK` | empty | Trusted root executable/script plus arguments, parsed with `shlex`, **not a shell expression** |
| `ACPI_THERMAL_PIDS` | empty | Additional comma/space-separated PIDs |
| `ACPI_THERMAL_PROCESS_NAMES` | empty | Additional comma/space-separated exact Linux `comm` names (normally at most 15 characters) |

All numeric settings must be finite and positive. Thresholds must satisfy
`35 <= MAX < STOP < KILL < SHUTDOWN <= 100`; hysteresis must be less than
STOP minus MAX. Temperature stages preempt grace periods.

Default targets come from
`nvidia-smi --query-compute-apps=pid --format=csv,noheader`, the same query
already used by the repository's GPU-job check, with a two-second timeout.
Missing/failing `nvidia-smi` is logged; configured targets still apply, but
recovery reboot is inhibited until the workload-stop query can be verified.
No hardware query was performed as part of the mocked tests.

Signals are sent only through Linux pidfds after rechecking process start
identity and safeguards. There is no unsafe numeric-PID signaling fallback.
PID 1, this process and its ancestors, root-credential processes, kernel
threads, zombies, unreadable processes, recognized critical process names,
and `/system.slice/` or `/init.scope` processes are excluded. Explicit PID
or name configuration cannot override these exclusions. This deliberately
also excludes root-run/container-root GPU jobs and system services: use a
trusted stop hook for them; otherwise the final shutdown is the fallback.
A hook runs asynchronously as a direct child executable with arguments, not
through a shell. It is stopped only through a pidfd taken at spawn, never
by numeric PID, and only that process: it must return promptly and clean up
its own descendants. A hook that needs shell syntax should be an
administrator-maintained script. The watchdog sandbox applies to the hook.
Home directories are hidden, and `RestrictAddressFamilies=AF_UNIX` blocks
TCP and UDP, so the hook cannot call a Kubernetes, Slurm, or HTTP API
itself. `systemctl` over the system bus is allowed. Docker, Slurm,
Kubernetes, and systemd `Restart=` policies are not stopped by the fan
tool. Root and `/system.slice` jobs are not signaled either. Those have to
be stopped by the hook, or the final shutdown is the fallback. None of them
may start the workload again while
`/var/lib/dgx-fan-control/acpi-incident` exists. The incident file is
retried until this boot's copy has been synced to disk.

`acpi-floor-sweep --max-temp` defaults to **60 C**. A supervised burn test
must explicitly raise it, for example `--max-temp 95`; that changes only
the sweep abort, **not** the thermal escalation thresholds. The sweep's
exit/abort writes 100; it neither stops the burn nor restores automatic
control. Raising `--max-temp` is not permission to hold ACPI under full load.
The 85/90 C load-stop defaults intentionally interrupt such a burn.
