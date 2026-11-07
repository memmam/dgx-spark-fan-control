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

All thresholds refer to the **hottest thermal zone**, never the burn view.
The same ladder applies in both phases. The ACPI phase is the only one
that writes a fan level. The gaps between 70, 80, 85, and 90 C are
temperature headroom, not a promise of time. A later poll can already be
at 90 C, and that poll does not wait out the lower stages.

1. **70 C, advisory only.** Log one warning. The human text asks for a
   checkpoint or a lighter load and says this is not a failure. The same
   line is machine-readable: `acpi_thermal_advisory` with `phase=acpi` or
   `phase=ec`, `zone_c`, `advisory_c`, `hook_c`, `term_c`, `kill_c`,
   `boot_id`, `incident_boot` (`-` when there is no incident file),
   `sequence`, `action=none`, and `timer=no`. The incident identifier is
   `incident_boot`. Holding **70 C through 79.9 C** does this and nothing else,
   for as long as the zone stays there. It does not start the stop hook,
   send a signal, force a fan level, write the incident latch, write the
   phase-2 stop marker, or start a timer. The line is not repeated until
   the zone falls below **68 C** (advisory minus hysteresis) and then
   reaches 70 C again. A jump straight to 80 C or above does not emit the
   advisory on that poll; the stop ladder is the record of that poll.
   The same episode is written for a program to read. See
   [Listening for the advisory](#listening-for-the-advisory).
2. **80 C.** In the ACPI phase, force level 100 on every ACPI Fan cooler
   that is not already at 100, and latch 100 until reboot. In the EC-only
   phase, write no `cur_state`. If `ACPI_THERMAL_STOP_HOOK` is set and the
   zone is still under 85 C, start that hook. Phase 1 records the incident
   latch here, because level 100 has replaced the EC curve. Phase 2 records
   the EC-stop marker here, so a resume hook cannot start a job this boot
   has already begun to stop. **No hook configured:** do not send SIGTERM.
   There is no timer. The zone may sit at 80–84.9 C indefinitely and the
   only signal is still the one at 85 C. That is the no-hook reaction
   window, from the 70 C advisory through 85 C.
3. **Hook running, or a hook that exited 0.** It has **10 seconds**, unless
   the zone reaches 85 C or 90 C first. If those 10 seconds end and the
   zone is still at least **83 C**, send SIGTERM even though the zone is
   under 85 C. That timeout exists only because a hook was started. If the
   grace ends below 83 C, wait; SIGTERM is sent if the zone returns to
   85 C. A hook that **fails to start or exits non-zero** is not the
   no-hook window: SIGTERM is sent on that same poll, including under 85 C.
4. **85 C:** if the workload is not already confirmed stopped, send
   SIGTERM. This preempts a hook that is still inside its 10 seconds.
   With no hook, this is the first signal, however long the zone has
   already been at 70–84.9 C.
5. **90 C:** preempt the hook and the SIGTERM grace, and send SIGKILL.
   Otherwise, after **10 seconds** following SIGTERM, send SIGKILL if a
   selected process is still alive or the hottest zone remains at least
   **83 C**. The poll that sends SIGKILL does not also power off.
6. **Phase 1, confirmed stop:** as soon as the workload is verified stopped,
   request `systemctl reboot` to restore stock EC control. There is no
   cool-down wait. Verification means no remaining selected, GPU-compute,
   or configured target processes, no protected live targets, and no failed
   or ambiguous compute query. The reboot is requested on that verifying
   poll, including when the zone is still hot. It does not relaunch the
   job. A failed reboot is retried. Poweroff is not the normal result of a
   successful stop. It is used only if reboot keeps failing and the zone
   then stays at or above **90 C for 15 seconds**. One sample at or above
   90 C does not power off. A failed reboot while the zone stays below
   90 C does not power off.
7. **Phase 2, the next boot:** the incident file still names the previous
   boot, so monitoring continues with **no ACPI writes**. `acpi-phase`
   records that phase 1 requested this reboot. The incident file selects
   the phase; the phase file does not override it, and the phase file alone
   does not enable monitoring. If the boot id cannot be read, a surviving
   incident latch stays on this no-write path. The optional resume hook may
   run once, and only below 80 C. If the zone again reaches the ladder
   above, stop the workload the same way: hook at 80 C if configured,
   SIGTERM at 85 C, SIGKILL at 90 C. There is no second reboot and no
   second resume. After the stop is confirmed, watch the zone. Power off
   only when the zone stays at or above **90 C for 15 seconds**. One sample
   is not enough, and a drop below 90 C starts that clock over. A zone that
   stays at 84 C, or at 89 C, does not power off on this path however long
   it stays there.

   SIGKILL and the 10-second SIGTERM grace are workload escalation. They
   are not poweroff. If the stop cannot be verified, poweroff still waits
   for the same **15 seconds at or above 90 C**, counted from SIGKILL, not
   from an earlier watch. Five seconds after SIGKILL at 86 C does not power
   off. A failed recovery reboot drops that SIGKILL clock and starts a new
   15 seconds. The failure poll itself does not power off.

   Two clocks are separate. `since` is how long the current workload stage
   has run: the hook grace, the SIGTERM grace, or the time since SIGKILL.
   `ceiling_since` is only the poweroff observation. It is kept for an
   EC-only watch after a confirmed stop, for an unverified SIGKILL, and for
   the wait after a failed reboot. Entering any of those starts it over. A
   measured zone below 90 C clears it, including a zone below 80 C while a
   new process is present or `nvidia-smi` did not answer. An EC-only sample
   with no readable zone clears it and does not count as heat. A poll that
   this policy skips, including the true-zero handoff, clears it so the gap
   is not added to the 15 seconds. Phase 1 is different on purpose: an
   unreadable zone there is treated as 90 C and can continue the clock.
   That fail-closed path is not used in the EC-only phase, which does not
   write ACPI and does not power off on the unreadable sample. An exception
   from the thermal poll is a third case: the watchdog logs it, clears only
   the poweroff clock, and keeps polling. The incident latch and the
   workload stage are left as they were. An unreadable phase-1 zone is not
   this exception path and is still treated as 90 C.

   These intervals were not timed on hardware and they are not a claim
   about when a machine will cut power. An unreadable zone in this phase
   does not write ACPI and does not power off on that sample. A verified
   true zero (both tachometers at 0 RPM) stays on the existing true-zero
   rule in both phases, including its separate 85 C cutoff. During a
   latched incident, legacy cold-stall and foreign-floor reboot paths
   cannot bypass phase 1's stop verification.

The reboot restores stock EC control. It is not an in-place ACPI recovery,
and it does not relaunch the process that was stopped. The incident latch
survives that reboot and inhibits `acpi-max`, the sweep, stall-repair ACPI
writes, daemon curve startup, boot restoration of a manual floor, and
`set-state`. It does not forbid a workload from running again under the
restored EC curve. After the reboot, inspect cooling and explicitly run:

```sh
sudo dgx-fan-control acpi-incident-reset
```

Reset refuses in the incident/ACPI-hold boot or unless the hottest zone is
below MAX minus HYSTERESIS (78 C by default). It writes no fan level and
starts no service or job. `automatic` does not clear this latch.

### Optional resume after the next boot

`ACPI_THERMAL_RESUME_HOOK` is empty by default. Nothing is resumed until an
administrator sets it. The tool does not guess the killed command line and
does not assume the job can be checkpointed. A non-checkpointable job does
not continue; only this hook, or the workload manager the hook calls, can
start work again.

That boot is phase 2. `dgx-fan-health.service` (`health --record`) may run
the hook once, only after that health run has passed and the hottest zone
is below 80 C. A 70 C advisory does not block it. If the watchdog has
already entered the phase-2 stop ladder on this boot, the hook does not
run, so it cannot start a job that was
stopped for heat. The watchdog starts at sysinit; health starts later, and
the stop is recorded before any signal. A later thermal stop does not
reboot and does not run the hook again. A plain `health` run, or a failed
one, does not start it. The hook is skipped unless every one of these is true:

- the command is set, and this process is root
- this `health --record` run passed, and the hottest zone is below 80 C.
  The advisory band from 70 C does not block resume
- `/var/lib/dgx-fan-control/acpi-incident` exists and names an **earlier**
  boot, so this is a new boot and ACPI writes are still inhibited
- this boot has not armed an ACPI hold
- this boot has not already entered the phase-2 stop ladder
- this boot has not already attempted the hook

The attempt is recorded **before** the command starts. A failure, a timeout
(30 seconds), or a second `health --record` does not run it again until the
next boot. The hook inherits the health unit's sandbox: home directories are
hidden, so use an absolute program outside `$HOME`, usually `systemctl start`
of a unit that knows how to resume. The command is split with `shlex` and is
not a shell. It receives `DGX_ACPI_INCIDENT=1`,
`DGX_ACPI_INCIDENT_BOOT` (the incident boot id), `DGX_ACPI_INCIDENT_PATH`,
and `DGX_ACPI_RESUME_BOOT` (this boot). It must exit promptly. What to start,
and whether that job can resume at all, is not something this repository can
know.

The degrees between the advisory and SIGTERM are headroom, not seconds of
warning. On kensaki the hottest zone rose about **0.07 C/s between 88 and
95 C** under `gpu_burn`, while others reported faster **90 → 98 →
power-cut** ramps. That burn is not a calibration of these thresholds, and
nothing here was measured against a hardware power cut. A fast rise can
cross 70, 80, 85, and 90 C between polls; the poll that sees 90 C sends
SIGKILL on that sample. Userspace polling, process discovery, systemd, and
shutdown latency cannot guarantee protection against a rapid hardware trip.

The existing **85 C true-zero fan poweroff is separate and unchanged**.
This new policy applies only after a project ACPI write; ordinary spinning
fans on the stock EC curve do not arm it. The old true-zero hot shutdown
retains precedence over hook/process probing. Invalid new thermal settings
refuse new ACPI holds; the watchdog logs the error and uses safe thermal
defaults rather than abandoning its true-zero monitoring.

### Listening for the advisory

The advisory is the window before the 80 C hook. Default threshold is **70 C**
(`ACPI_THERMAL_ADVISORY_C`), not a separate 75 C stage. A program can
checkpoint, shed load, or stop itself. This repository does not do that for
it, and the advisory hook is not allowed to delay the 80/85/90 ladder.
Nothing here was run against a live workload or on DGX hardware.

Each new episode publishes three views of one record. Repeated polls while
the zone stays at 70–79.9 C do not publish again. The episode ends only when
the zone falls below **68 C**. The next crossing of 70 C is a new episode.
A zone that bounces between 69 C and 71 C stays in the same episode.

The record is schema 1. The keys, in this order, are `event`
(`acpi_thermal_advisory`), `schema`, `phase` (`acpi` or `ec`), `zone_c`,
`advisory_c`, `hook_c` (80), `term_c` (85), `kill_c` (90), `boot_id`,
`incident_boot` (`-` when no incident file exists), `sequence`, `action`
(`none`), and `timer` (`no`). `sequence` counts episodes in this watchdog
process. A restart can publish again with a new sequence while the zone is
still hot; a listener should treat the work as idempotent.

1. **Journal.** The watchdog logs one warning line that starts with those
   fields and then says to checkpoint or reduce load, and that this is not
   a failure. `journalctl` keeps it with the service:

   ```sh
   journalctl -u dgx-fan-watchdog -f -g acpi_thermal_advisory
   ```

   Reading the system journal requires membership in the systemd journal
   group, or root. The watchdog's own sandbox does not grant that to the
   workload.

2. **State file.** The same fields, one `key=value` per line, are replaced
   atomically at `/var/lib/dgx-fan-control/acpi-advisory`. The directory is
   mode `0755` and this file is mode `0644`, so a normal login can read it.
   The watchdog's `UMask=0077` does not apply to this file. The file stays
   until the zone falls below 68 C, including after the ladder has moved
   on to 80 C or above. Removing the file is not a request to stop.

   ```sh
   # Example only. Not run on hardware. Poll; do not block the watchdog.
   record=/var/lib/dgx-fan-control/acpi-advisory
   if [ -f "$record" ] && grep -q '^event=acpi_thermal_advisory$' "$record"; then
       phase=$(sed -n 's/^phase=//p' "$record")
       zone=$(sed -n 's/^zone_c=//p' "$record")
       echo "advisory phase=$phase zone=$zone; checkpoint or reduce load"
   fi
   ```

3. **Optional hook.** `ACPI_THERMAL_ADVISORY_HOOK` is empty by default.
   When set, it is an executable plus arguments, split with `shlex`, not a
   shell. The watchdog starts it and does not wait for it to exit. A hook
   that is still running is not started again and is not killed. A missing
   executable, a bad command, or an exception is logged once for that
   episode. The next temperature poll still runs.    The hook is a child of `dgx-fan-watchdog.service`. That unit sets
   `RestrictAddressFamilies=AF_UNIX`, `ProtectSystem=strict`,
   `ProtectHome=yes`, and `NoNewPrivileges=yes`. It does **not** set
   `PrivateNetwork`. `start_new_session` only starts a new session; it
   does not add or remove those unit settings. This tree was not loaded
   under systemd and no socket was opened from a child, so inheritance of
   the address-family filter is what the unit file says, not a measured
   result. The resume hook is different: it is a child of
   `dgx-fan-health.service`, which leaves `PrivateNetwork` and
   `RestrictAddressFamilies` unset so an optional URL can run.
   `systemctl start` of a unit
   that knows how to notify the job is the usual shape. The hook receives
   `DGX_ACPI_ADVISORY=1` and `DGX_ACPI_ADVISORY_PHASE`, `_ZONE_C`, `_C`,
   `_HOOK_C`, `_TERM_C`, `_KILL_C`, `_BOOT`, `_INCIDENT_BOOT`, `_SEQUENCE`,
   and `_PATH`. It must not assume it is the only listener.

The workload is not signaled, checkpointed, or stopped by this event.
SIGTERM is still 85 C, SIGKILL is still 90 C, and a missing stop hook
still does not send SIGTERM at 80 C.

### Configuration and process safeguards

Set these in `/etc/default/dgx-fan-control`, then restart
`dgx-fan-watchdog.service`. They are root configuration, not environment
variables. Existing installations keep their defaults file; add new keys
explicitly when customizing. Missing keys use these defaults:

| Key | Default | Meaning |
| --- | --- | --- |
| `ACPI_THERMAL_ADVISORY_C` | `70` | Advisory only. Stable 70–79 C does not stop, latch, or start a timer. This is the listen-able warning |
| `ACPI_THERMAL_ADVISORY_HOOK` | empty | Optional program started when the advisory is published. Not waited on. Empty disables it. Not a workload signal |
| `ACPI_THERMAL_MAX_C` | `80` | ACPI phase: force and latch level 100. Both phases: start a configured stop hook. Not a SIGTERM |
| `ACPI_THERMAL_STOP_C` | `85` | SIGTERM if the workload is not already confirmed stopped |
| `ACPI_THERMAL_KILL_C` | `90` | SIGKILL. Preempts hook and SIGTERM grace |
| `ACPI_THERMAL_SHUTDOWN_C` | `90` | Thermal poweroff threshold. The zone must stay at or above this. Below it, this path does not power off |
| `ACPI_THERMAL_HOOK_SECONDS` | `10` | Grace for a hook that actually started, then SIGTERM. Not a poweroff timer |
| `ACPI_THERMAL_TERM_SECONDS` | `10` | SIGTERM grace before SIGKILL. Not a poweroff timer |
| `ACPI_THERMAL_KILL_SECONDS` | deprecated | If an old file sets it, the value is checked, a warning is logged, and the value is ignored. Absent from the shipped defaults. It does not delay SIGKILL or poweroff |
| `ACPI_THERMAL_SHUTDOWN_SECONDS` | `15` | How long the zone must stay at or above `SHUTDOWN_C` before poweroff. One sample is not enough |
| `ACPI_THERMAL_HYSTERESIS_C` | `2` | Clears the advisory below 68 C. Hook cooling is adequate below 83 C. Manual reset requires a zone below 78 C. Not a reboot delay |
| `ACPI_THERMAL_SHUTDOWN` | `yes` | Only explicit `no` disables this shutdown stage, not true-zero poweroff or the recovery reboot |
| `ACPI_THERMAL_STOP_HOOK` | empty | Trusted root executable plus arguments, parsed with `shlex`, **not a shell expression**. Empty keeps the no-hook window through 85 C |
| `ACPI_THERMAL_RESUME_HOOK` | empty | Optional post-boot resume. Empty disables it. Not a relaunch of the killed command |
| `ACPI_THERMAL_PIDS` | empty | Additional comma/space-separated PIDs |
| `ACPI_THERMAL_PROCESS_NAMES` | empty | Additional comma/space-separated exact Linux `comm` names (normally at most 15 characters) |

All numeric settings must be finite and positive. Thresholds must satisfy
`35 <= ADVISORY < MAX < STOP < KILL <= SHUTDOWN <= 100`. Hysteresis must be
less than STOP minus MAX and less than MAX minus ADVISORY. A temperature
crossing preempts a grace period. The no-hook path has no grace period
below STOP.

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
administrator-maintained script. The stop hook and the advisory hook are
children of the watchdog unit. That unit sets
`RestrictAddressFamilies=AF_UNIX`, hides home directories, and does not set
`PrivateNetwork`. `start_new_session` does not create that address-family
line and was not used here as proof of isolation. `systemctl` over the
system bus uses a Unix socket, which that line allows. The resume hook is
a child of the health unit, which does not set the address-family line. Docker, Slurm,
Kubernetes, and systemd `Restart=` policies are not stopped by the fan
tool. Root and `/system.slice` jobs are not signaled either. Those have to
be stopped by the hook, or the final shutdown is the fallback. The incident
file blocks later ACPI writes from this project. It does not block a
workload manager from starting work again under the restored EC curve. The
file is retried until this boot's copy has been synced to disk.

`acpi-floor-sweep --max-temp` defaults to **60 C**. A supervised burn test
must explicitly raise it, for example `--max-temp 95`; that changes only
the sweep abort, **not** the thermal escalation thresholds. The sweep's
exit/abort writes 100; it neither stops the burn nor restores automatic
control. Raising `--max-temp` is not permission to hold ACPI under full load.
The 80/85/90 C load-stop defaults intentionally interrupt such a burn.
The 70 C line does not.
