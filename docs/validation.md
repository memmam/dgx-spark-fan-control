# Validation and provenance

Current hardware results for module 0.1.7 are the
[hardware ledger](#module-017-hardware-ledger). A `./scripts/check` pass is
not hardware readiness. Do not repeat a row marked PASS or BLOCKED unless
the relevant source changed, a regression is being checked, or a defined
new observation is available. Older sections below are provenance for
earlier versions. They are not a second to-do list.

## Version 0.1.5 on EC 3.5.8 with October SoC, 6 Nov 2026

Both DGX Sparks, previously EC 3.13.7, downgraded to EC 3.5.8 with fwupd and
the EC blocked in `DisabledDevices`. SoC/UEFI stayed October `0x02009e0f`.
Kernel `7.0.0-1024-nvidia`. Module 0.1.5.

Probe bound. The kernel log showed `pinned EC 3.5.8 capabilities
fan0=1260..9000 fan1=1890..13500`, baseline 2700/4050 RPM, a cooling device,
and state 1 floor `0xa8c`. With the daemon stopped, `set-state 6` measured
about 7560/7155 RPM. State 12 wrote floor `0x34bc`. On these boots the
mailbox query completed and the driver bound. The same EC 3.5.8 and October
SoC pair has also timed out that first read-only query on other boots, so a
timeout remains the fallback when probe fails. It is not the only outcome.

One mailbox wedge is open. The daemon, enabled at boot, read the floor about
99 times at a 2-second interval, then near 188 seconds of uptime operation
`0x4` stuck pending (state `0x2`, 100 polls, `-110`). Later preflights timed
out until reboot. It was not reproduced on demand afterward. NVIDIA Sync
Resource Monitor and the DGX Dashboard telemetry froze around the same time;
that is a correlation to check, not a cause. Incident logs now include
`realtime=` and `uptime=` for the next occurrence.

ACPI `cur_state` and the module write the same EC lower floor. After state
12 (`0x34bc`), `acpi-max` (ACPI level 100) replaced it with `0x10fe` (4350
RPM). The module then logged `floor ownership mismatch: observed=0x10fe
confirmed=0x34bc`. Unload logged `failed to restore automatic fan policy for
module removal: -116`. The next probe logged `refusing to replace existing
lower floor 0x10fe` and did not register a cooling device. Writing ACPI
level 0 stopped both fans immediately; they stayed at 0/0 RPM at 39°C, and
the EC did not intervene. Level 0 also left the foreign floor in place.
ACPI level 100 measured about 4320 RPM and was quieter than module state 12.
After reboot, both machines were back near 2700/4050 RPM without another
`acpi-max`.

On that boot the performance service was enabled and started. `module enable`
leaves `dgx-fan-control.service` disabled unless `--start-service` or
`--force-service` is passed.

Not observed on these boots: the quiet curve, a watchdog trip, the
true-zero repair, the one-reboot-then-poweroff sequence, and a repeated
copy of the 188-second wedge. `allow_unverified_writes` stayed off.

On a later boot the performance service on one Spark restarted 196 times,
every 30 seconds, because probe had refused a foreign floor left by ACPI.
Exit 78 stops that loop. The recovery is a reboot, which clears the ACPI
floor. Do not write ACPI level 0, and do not toggle the service to clear it.
The watchdog's true-zero 85°C poweroff, the deferral while work is in
progress, the reboot marker, the one foreign-floor reboot, the
needs-intervention flag, and the post-boot health check have not been run
on hardware. An interactive `dgx-fan-control health` is one read-only pass.
The 45 second retry runs only from `dgx-fan-health.service`. 85°C applies
only when both tachometers read 0. It is not a limit while the fans spin.
A user-reported 1M-token prefill
([tenhkspark/dgx-spark-tuning](https://github.com/tenhkspark/dgx-spark-tuning))
reached about 84°C system temperature with healthy fans. The kernel
critical trip is near 104°C (104.8 in the tonyd2wild notes). Those notes
show a hotspot rising about 4°C/s, fast enough to pass that trip before
the next sample is logged. Userspace cannot beat that rise with another
threshold. The default busy signal is `nvidia-smi` compute apps, and only
when `BUSY_CHECK` is unset. A persistent server keeps that default busy,
so the reboot waits until the 30 minute deferral limit alerts, and still
does not reboot through work. Only a true zero at 85°C powers off a
machine that stays busy.
No step in the notes below writes ACPI level 0.

Recheck, on each Spark, after installing this command and module:

1. If `systemctl is-enabled dgx-fan-control.service` is already `enabled`
   from the previous `module enable`, run `sudo systemctl disable --now
   dgx-fan-control.service` once. After that, `module enable` must leave it
   `disabled`. `dgx-fan-control status` shows `state=0/12 (daemon not setting a floor; EC's own curve)`
   when the module is loaded and the service is stopped.
2. With the service stopped, `sudo dgx-fan-control set-state 6`, read both
   `fan*_input` values, then `sudo dgx-fan-control automatic`. Status returns
   to `state=0/12 (daemon not setting a floor; EC's own curve)`.
3. `sudo dgx-fan-control acpi-max` while the module is still loaded exits
   nonzero and does not change `cur_state`. Then `sudo dgx-fan-control module
   disable` and `sudo dgx-fan-control acpi-max` (level 100). Fans should move
   to about 4320 RPM. Do not pass `--level` below 35.
4. Reboot. Idle fans should be back near 2700/4050 RPM without another
   `acpi-max`. Only then `sudo dgx-fan-control module enable`, and only if
   status does not say the probe refused a foreign floor.

## Version 0.1.5 on EC 3.13.7

Hardware check on DGX Spark, kernel `7.0.0-1024-nvidia`, EC 3.13.7.
DKMS 0.1.5 built and loaded on aarch64. The kernel log showed partition
`0x8003`, properties `0x109`, FF-A 1.2, `pinned EC 3.13.7 capabilities
fan0=975..4350 fan1=975..4350`, floor writes disabled, baseline fans about
2101 and 2141 RPM, and hwmon telemetry with no cooling device. `module
disable` left the module unloaded with the managed blacklist and mode
ACPI-only. DKMS did not replace `/usr/local/sbin/dgx-fan-control`; the
previous 0.1.4 command stayed until it was installed by hand.

A first `module enable`, using the command from before the exit-69 change,
started the service. The daemon logged `control unsupported on this
firmware`, exited 0, and would have restarted every 30 seconds. The retest
on both Sparks, same kernel and EC, confirmed the correction: `module enable`
leaves the service disabled, and `module status` reports
`control: unsupported (EC 3.13.7, telemetry only)`. `acpi-max` wrote
`cooling_device0` and `cooling_device1` to 100/100. Fans stayed about 2,000
to 2,100 RPM, which is expected because EC 3.13.7 accepts the ACPI write and
does not change speed.

Not checked on those machines: the watchdog, the quiet curve, and the EC
3.5.8 floor-write path. The 3.13.7 floor table was not written.
`allow_unverified_writes` stayed off.

## Version 0.1.5 profiles and read-only EC 3.13.7

The hardware-free suite covers exact capability matches, rejection of a
one-count change, both floor tables, `-EPERM` without
`allow_unverified_writes`, poll state `0xFF` as `-EOPNOTSUPP`, reply-guard
`EPROTO` versus idle-guard `EAGAIN` and ownership `ESTALE`, both curves
staying off state 0, and the module toggle against a temporary directory.
EC 3.5.8 with SoC 2.158.15, the ACPI `Fan` levels, and the November 2026
stopped-fan reports are documented from the owner and the forum, not
reproduced as a controlled test here.

## Version 0.1.4 zero-clamp restore

On **2026-10-29** the signed module was built on **dgx4** with DKMS against
the running kernel **`7.0.0-1019-nvidia-64k`**. EC firmware was **3.5.8**
(`0x03000508`) and SoC firmware **2.155.11** (`0x02009b0b`). A vLLM worker was
serving. Hottest zone about **68°C**, fans **9000 / 11205 RPM** at state 10
under 0.1.3.

Stopping the service left **state 0** and the same RPM (firmware hysteresis,
not a stop). Unloading 0.1.3 and loading 0.1.4 with the service still stopped
logged `automatic fan policy active for module load`, read **state 0/12** with
lower floor `0xffff`, and both fans stayed at **9000 / 11205 RPM**. No zero
clamp was present, so the clear-zero path did not run on this host. Starting
the service resumed the curve (state 4, **8460 / 10530 RPM**). The host
transaction test covers a clamp of 0 being replaced by the unset value, and a
non-zero foreign floor still being refused.

## Version 0.1.3 incident evidence

On **2026-09-08**, all **23 project tests** passed, including **43 actual-C
transaction/logging scenarios**. The existing 40 failure/recovery cases retain
their request-count and ownership assertions. Three new logging scenarios
verify quiet healthy polling, bounded chronological history after wraparound,
trigger operation/phase, submit timing, pending-poll counts, physical bytes,
uncertain setter evidence, partial-read validity masks and failure stage,
cooldown suppression, and a new incident ID on the next admitted attempt.
Their FF-A counts show that recording evidence adds no hardware requests.

Both Sparks built and signed 0.1.3 against `6.17.0-1029-nvidia` with `W=1`.
The usual compiler command-name and unavailable-`vmlinux` notices remained;
there were no driver compilation errors. Logging adds fixed per-device memory
and host timing/printing work; it cannot expose internal secure-world ordering.
The [logging guide](incident-logging.md) explains fields and their limits.

The signed build was installed and loaded on Spark 1 at **08:46 UTC** and
Spark 2 at **08:47 UTC**, after each old controller verified automatic/unset.
Loaded and installed `srcversion` matched **`A1ED4009D06119FE4AC778A`** on both
hosts, and deployed C source hashes matched the checkout. Both services
returned to state 5 with zero automatic restarts; boot IDs were unchanged.
The existing passive recorders stayed active. No live fault was induced;
incident contents were checked against simulated failures, and a natural
0.1.3 incident had not yet occurred at closeout.

## Version 0.1.2 guarded recovery

On **2026-09-08**, all **22 project tests** passed. The actual C transaction
test now executes **40 scenarios**, including 30 new recovery cases: stale
pending before submission, isolated read timeout, an applied or unapplied
setter, automatic/unset recovery, late completion during observation, distinct
capabilities/telemetry payloads, a retry that wedges again, busy or changing
status, zero/invalid OEM data, stale/overwritten/error replies, foreign floors,
sender rejection, each of six OEM-read transport failures, fixed-address
refusals, and cooldown expiry.
Tests assert bounded request counts, no recovery writes, retained ownership on
failure, telemetry invalidation, and no repeated diagnosis during cooldown.
Every rejected-recovery case also attempts a setter after cached pending clears
and verifies that the unverified recovery still blocks all new submissions.

Both Sparks compiled 0.1.2 with `W=1` against `6.17.0-1029-nvidia` and signed
it with their existing enrolled certificates. There were no driver compilation
errors; Kbuild reported the known GCC command-name difference and missing
`vmlinux` for BTF. The C tests substitute Linux lifetime/locking and FF-A
boundaries, so they do not prove kernel concurrency or secure-firmware timing.

Both hosts passed automatic → state 12 → automatic during rollout, reaching
9,000/13,500 RPM after eight seconds at maximum. The final signed build was
installed for the running kernel and loaded at **08:30 UTC**; loaded and
installed `srcversion` matched **`591D047A4087B5F270FB2B6`** on both hosts.
Both final-build services started successfully, progressed 0 → 2 → 4 → 5,
and reported zero automatic restarts. At 08:31:45 UTC, both read state 5 and
6,300/6,210 RPM; the hottest sensors were 54.8°C and 52.8°C respectively.
Boot IDs were unchanged. Previous signed 0.1.1 modules were retained locally
for rollback after verified automatic restoration.

Spark 1's pre-existing wedge was diagnosed and cleared using the pinned
one-shot helper while its 0.1.1 owner remained loaded, followed by that owner's
verified automatic restoration. This was required for a safe ownership
handoff; it is **not** a live demonstration of the new driver's automatic
recovery. No live fault was induced. The new recovery integration is verified
against simulated failures; unattended recurrence recovery and long-term
stability remain to be established. Fresh bounded 24-hour passive captures
started on both hosts at 08:31 UTC under transient unit
`dgx-ec-ffa-capture-20260908.service`; kernel recovery logs supply the verified
outcome if the request recorder stops at timeout onset.

## Prevention investigation, 2026-09-07

The firmware replay passed **24 scenarios**, including sixteen paired cases
that isolate metadata ordering for a single read or write. All four
original-order early-completion cases remain pending with an idle mailbox;
the four matching metadata-first cases complete. Normal timing passes with
both orders. This is an offline causal experiment, not a hardware firmware fix.
See the [analysis](firmware-pending-analysis.md#isolating-the-trigger).

The passive FF-A recorder compiled and ran for approximately 60 seconds on
each Spark with both direct-message probes enabled. Each observed 30 floor
reads and 60 cached polls, all successful, and no other calls to partition
`0x8003` in those windows. Its failure-trigger branch has not been exercised
against a live wedge; no fault was induced to test it. No runtime controller,
kernel module, or firmware was changed for this investigation.

At **18:21:13 UTC**, both hosts attached the same seven probes for a bounded
24-hour capture under transient unit `dgx-ec-ffa-capture-20260907.service`.
Both recorders were verified running. They exit on persistent pending or
duration expiry and perform no recovery. The normal **22 project tests**,
shell syntax checks, and changed documentation link checks also passed.

## Original hardware work

The release extraction comes from the original `kernel-mod` project at commit
`87b88039a5786f6669c3deef81cda723720cea8c` (2026-08-15). The kernel control
driver, Python controller, systemd files, and eight original tests were copied
without behavior changes. The public build/signing scripts and documentation
were adapted for standalone use. Exploratory modules, private operational
notes, signing material, and firmware binaries were not part of the extraction.
Subsequent bounded research tooling is published under `research/`.

The original notes record these results on two NVIDIA DGX Sparks, board P4242,
DGX OS 7.5.0, kernel `6.17.0-1029-nvidia`, EC 3.5.8, SoC firmware 2.155.11,
with Secure Boot enabled:

| Exercise | Recorded observation |
| --- | --- |
| Read-only RPM observer | 120 samples over two minutes on one system; stable 2700/4050 RPM |
| Bounded 4500 RPM floor | Approximately 4500/4455 RPM; independent unset-clamp readback after rollback |
| Bounded 13500 RPM floor | Channel maxima 9000/13500 RPM reached within 6.3 seconds |
| Control driver state sequence | 0 → 3 → 8 → 12 → 0, with readback and automatic restoration |
| Python curve on hot sensors | 0 → 2 → 4 → 6 → 8 → 10 → 12 at two-second intervals |
| Python termination | SIGTERM restored state 0 |
| Persistent installation | Both systems built/signed locally and started the systemd service |
| Service lifecycle | Stop/start on one installed system restored automatic control, then resumed the curve |

Earlier firmware versions were examined offline only. The recorded thermal
experiments are observations, not a controlled performance benchmark. Long-term
reliability and compatibility with later kernels/firmware remain unqualified.

## Checks for the public extraction

`./scripts/check` exercises userspace behavior using temporary files and mock
boundaries, checks the narrow kernel source contract, and parses the shell
scripts. It does not issue FF-A messages or require root. The source assertions
are regression guardrails, not proof of kernel or firmware correctness.

Target compilation must use a Spark's exact NVIDIA kernel headers with `W=1`.
Building is separate from signing, installation, module loading, and live
thermal testing. GitHub Actions runs the hardware-free checks only.

On **2026-09-06**, the public extraction passed all **13 tests** and shell
syntax checks. Signing-key generation, file permissions, and refusal to
overwrite an existing key pair passed an isolated temporary-directory check.
The Python runtime source matched the original byte for byte; the kernel source differed only in author metadata.

The extracted module also compiled on a Spark against
`6.17.0-1029-nvidia` with `W=1` and the expected aarch64 vermagic. Kbuild noted
the compiler command-name difference (`gcc-13` versus
`aarch64-linux-gnu-gcc-13`, both Ubuntu GCC 13.3.0) and skipped BTF generation
because `vmlinux` was unavailable. There were no driver compilation errors.
This check used a temporary build directory and did not sign, install, or load
the module, or modify the running controller. The original extraction check did
not exercise the signing wrapper; the 0.1.1 check below does.

## Installation-guide and DKMS checks

On **2026-09-06**, the added `dkms.conf` passed an isolated **DKMS 3.0.11
add/build** on a Spark with `6.17.0-1029-nvidia`. DKMS reported the module as
`built`; `modinfo` confirmed version 0.1.0, the target aarch64 vermagic, and
the test certificate's signing identity. This used an upstream DKMS script,
temporary source/state directories, and a disposable signing key. The test
did not register with the host's system DKMS tree, enroll a key, install or
load a module, or change the running controller. Temporary files were removed.

All 13 existing tests and shell syntax checks passed. Documentation links and
anchors were checked, and all 38 documented shell blocks passed `bash -n`.
The Bash job example was exercised with mock commands for job success, job
failure, and restoration failure; it attempted automatic restoration and
returned the expected exit status in each case. This does not verify real
signal delivery, sudo renewal, or EC recovery on hardware.

System-wide DKMS installation, distribution kernel-update hooks, and preboot
enrollment remain operator steps described in the guide, not actions performed
by these checks.

## Version 0.1.1 recovery checks

On **2026-09-06**, all **21 tests** and shell syntax checks passed. The suite now
compiles the driver's actual C transaction functions against a simulated FF-A
peer and exercises ten scenarios: lost write completion, lost readback,
reconciliation after transport recovery, an unrelated floor, a foreign change
during a write, lost automatic-restoration completion, delayed preflight
completion, permanent pending, bounded restoration retries, and unexpected
protocol/transport responses. This exercises the C behavior, not Linux locking,
real timing, or secure firmware execution.

Python checks cover transient and persistent failures, preserving the original
error through failed cleanup, ownership rejection, resume resynchronization,
signal interruption of retry waits, and the service's exit-69 contract. The
signing wrapper accepts only mokutil's exact affirmative enrollment message,
including the exit-status-1 convention observed on DGX OS; rejection cases are
also tested with isolated mock commands.

The 0.1.1 module built with `W=1` and was signed using an already-enrolled local
certificate on **both Sparks** against `6.17.0-1029-nvidia`. Version, aarch64
vermagic, and signature identity were verified. The only build notices were the
same GCC command-name difference and missing `vmlinux` described above. These
signed modules were initially staged without installation. The live recovery
below subsequently made it possible to install and load them safely.

The updated Python command and systemd unit were installed on both systems,
with backups of the previous files. Against the existing stuck relay and loaded
0.1.0 driver, each service made three attempts, reported failed automatic
restoration, exited 69, and remained failed with zero restarts. This validates
failure containment on hardware; it does **not** establish recovered fan control.
The current floor could not be verified. No reboot or cold power cycle was
performed during that failure-containment check.

## Rebootless recovery and 0.1.1 live checks

On **2026-09-07**, both Sparks were recovered through the version-pinned
[mailbox diagnostic](../research/mailbox/README.md). Both physical mailboxes
were idle despite cached packet state 2. One read-only retry per machine
recovered communication. Explicit removal of each observed floor passed two
physical/cached comparisons before UNSET and two afterward. Old modules
confirmed automatic policy on orderly removal; both 0.1.1 modules then loaded
in state 0. See the [firmware analysis](firmware-pending-analysis.md#live-recovery-on-both-sparks)
for timestamps, floors, the initially refused restoration, and limits of inference.

Both systems passed manual **0 → 12 → 0**, measured **9000/13500 RPM** after
eight seconds at state 12, and restarted the performance service successfully.
Both retained their original boot IDs and had zero service restarts. No firmware
was flashed and unrelated workloads were not restarted. The subsequent soak
**failed on Spark 1 at 12:58 UTC**, about 3½ hours after startup: the relay wedged
again, the daemon exited 69, and automatic restoration failed. At 17:59 UTC one
guarded read retry recovered it; the normal 0.1.1 driver then restored state 0
and the service restarted without a reboot. Spark 2 remained running. See the
[recurrence evidence](firmware-pending-analysis.md#recurrence-on-version-011).
Version 0.1.1 therefore has demonstrated failure containment, not unattended
reliability.

All **22 tests** and shell syntax checks passed. The additional compiled-C test
executes the actual diagnostic functions against a strict fake FF-A peer,
including busy/unreadable mailboxes, invalid clocks and headers, bounded retry,
conflicting modes, physical/cache disagreement, ownership mismatch, and failed
UNSET readback. It checks that diagnostic recovery issues no setters and that
explicit restoration can only send one UNSET. The helper also built with `W=1`
against the exact target headers on both Sparks.

## Known limitations

The subsequent [firmware investigation](firmware-pending-analysis.md) reproduced
eight scenarios by executing original SoC 2.155.11 instructions against a
simulated EC boundary. It demonstrates an early-completion ordering defect and
a conditional idle-mailbox recovery path. The subsequent hardware observations
validate that path on both Sparks, but do not establish the historical trigger.

- This is an out-of-tree kernel module tied to the observed platform and APIs.
  DKMS can rebuild it for new kernels, but there is no promise of compatibility
  with a changed kernel or firmware contract.
- Manual floors do not expire. SIGKILL, a kernel crash, or a broken transport
  can prevent restoration. A retained lower floor can leave the fans running
  faster; it does not suppress firmware cooling demand.
- Restoration accepts only the last confirmed floor, the driver's own uncertain
  attempted floor, or an unset clamp. An unrelated value requires operator
  investigation. Use only one policy writer.
- Version 0.1.1's ownership fixes do not themselves clear permanently pending
  firmware state. The operator helper recovered the observed idle-mailbox case;
  busy/unreadable mailboxes and notification failures remain outside that
  demonstrated recovery. The firmware race is still present. Version 0.1.1
  contains recurring failures with bounded retries and an explicit failed service.
- The daemon ignores individual unreadable or implausible thermal sensors.
  Maximum cooling is requested only when no valid sensor readings remain.
- Fan RPM is not an exact setpoint. Firmware demand, fan saturation, and ramp
  time affect observed values. State 0 removes this driver's floor; it does not
  force an immediate return to idle RPM.
- Some investigated boots had an unavailable secure eSPI transport. See the
  [troubleshooting guide](troubleshooting.md) before attempting recovery.

## ACPI load hold, November 6, 2026

On kensaki with EC 3.5.8, rig off, and `gpu_burn` (1:18–1:27 PM PST), ACPI
100 held 4320/4320 RPM through hottest-zone readings of 95–97.7 C. Fanwatch
read SoC 96–97 C for about 15 seconds with no fan override. The stock EC
curve had reached 4860/7290 by about 94.5 C and 6660/9990 at about 95 C,
then settled at 88.8–89.7 C. Stopping the ACPI writes did not restore that
stock curve. On the 0.1.5 boots the same day, ACPI level 100 replaced a
module state-12 floor with `0x10fe`, unload logged `failed to restore
automatic fan policy for module removal: -116`, and the next probe refused
that floor. After reboot, both machines were back near 2700/4050 RPM
without another `acpi-max`. Level 100 is
about 48%/32% of 9000/13500 RPM. The burn view's GPU reading was about 10 C
below the hottest zone. These observations motivated the separate
[ACPI thermal escalation and recovery policy](ec-curve.md#ec-358-acpi-holds-replace-the-stock-curve).
The implementation is tested with mocked thermal, process, hook, and
power-action interfaces; it has not been hardware-validated by those tests.

## Module 0.1.7 hardware ledger

This is the record for loaded module **0.1.7**. The live rows below were run
from checkout `5e7cf1f` unless a row names an earlier checkout. Spark journal
clocks for the 6 Nov 2026 evening session are **7 Nov 2026 UTC**. The
operator's clock for that session is **America/Los_Angeles, PST**.

`./scripts/check` is an offline mock suite. A pass there is not hardware
readiness. `research/live-check` is opt-in and is not part of that suite.

Do not repeat a **PASS** or **BLOCKED** row unless the relevant source
changed, a regression is being checked, or a defined new observation is
available. The reboot-notifier gap was seen again on 6 Nov 2026. That repeat
is the same missing log, not a new kind of evidence. Do not rerun that
saved-floor reboot until shutdown kernel logs can be kept.

These are not pending tests: a deliberate mailbox wedge, clearing a foreign
floor, writing a literal lower clamp of 0, an ACPI fan hold, and
`allow_unverified_writes` or `automatic_floor_recovery`. Leave both
parameters at `N`.

### Matrix

| Test | Host | Kind | Outcome | Evidence | Do not repeat unless |
| --- | --- | --- | --- | --- | --- |
| `./scripts/check`, 212 tests, no sudo | both | offline | **PASS** at `5e7cf1f` | operator, 6 Nov 2026 PST | source or regression |
| read-only health, numeric zone | kensaki EC 3.5.8 | live read-only | **PASS** 43.9°C | `5e7cf1f`, params `N`/`N` | source or regression |
| read-only health, numeric zone | atarime EC 3.13.7 | live read-only | **PASS** 44.8°C | telemetry only; an earlier `c92aa96` pass had no zone number and was not this result | source or regression |
| `service-start-stop` | kensaki | supervised | **PASS** | state 0 on sample 1, state 1 on sample 2, then stop; final inactive, state 0, manual floor none, watchdog active, 41.8°C | source or regression |
| `service-start-stop` | atarime | supervised | **MANUAL** | refused: no controller start and no floor write. Not a lifecycle pass | firmware gains floor control |
| `unload-reload`, idle | kensaki | supervised | **PASS** | 41.8°C, final state 0, manual none, watchdog active, fans 2700/4050 | source or regression |
| `unload-reload`, idle | atarime | supervised | **PASS** | 42.4°C, stayed telemetry only, watchdog active, fans 2061/2148. Saved last-set 12 was not applied | source or regression |
| manual units | both | configuration | **left enabled** | both units were enabled; cleanup put kensaki's enablement back after `automatic` | do not disable to satisfy a harness |
| saved floor 6 reapplied after reboot | kensaki | boot | **PASS** | `applied manual floor state 6 from last-set` at 19:28:27 PST | already shown; not notifier proof |
| shutdown reboot notifier | kensaki | boot | **UNVERIFIED** | previous-boot grep had neither success nor failure line. Not a recorded failure | a log that survives shutdown |
| suspend | kensaki | blocked | **BLOCKED** | `systemctl suspend` refused; no sleep occurred | the NVIDIA drop-in is removed on purpose |
| performance curve under `gpu_burn` | kensaki | supervised | **OBSERVED** | 19:57–20:04 PST. Idle state 1 at 41.9°C, then state 12 at hottest 80.4°C with `dgx_ec_fan` 9000/13500. Cooldown to state 1 at 44.8°C, then stop to state 0 at 42.7°C. The 70°C hottest bound was exceeded. Not a bounded pass and not a module failure. A later kernel excerpt shows the ending write at 04:04:48 UTC, state 0 floor `0xffff`. Do not repeat this ramp | source change, or a new bound with a different goal |
| fixed state 12, idle, service stopped | kensaki | supervised | **OBSERVED** | 20:10–20:12 PST. Service inactive, `set-state 12` at 43.5°C, last-set 12. After 60s, state 12, hottest 38.8°C, both fan paths 9000/13500. Not a load | source or regression |
| fixed state 12 under `gpu_burn` | kensaki | supervised | **INTERRUPTED** | 20:17 PST start from 36.8°C. About 20:20–20:23, GPU 71–73°C and hottest 80–83°C. Mailbox timeout at 20:22 UTC-equivalent. Not an equilibrium and not a bounded pass. Burn exit and later RPM not confirmed | do not repeat while the mailbox is unrecovered |
| natural mailbox timeout on 0.1.7 | kensaki EC 3.5.8 | live | **OBSERVED** | 04:22:13–04:23:07 UTC. Last floor write 04:10:28 UTC state 12 `0x34bc`. Then op `0x7` completion `-110`, and later op `0x7`/`0x4` preflights refused with no floor read. Recovery not established. Not induced | do not induce another. A later boot that actually recovers is different evidence |
| foreign floor `0x10fe` | kensaki | earlier live | **PASS** refuse | module did not clear it; reboot cleared it. Date not recorded | source or regression |
| state 6 then `automatic` | kensaki | earlier live | **PASS** | returned to the EC curve. Date not recorded | source or regression |
| unload while owning state 6 | kensaki | earlier live | **PASS** | restore and the next probe were clean. Date not recorded | source or regression |
| performance service 0 to 1 to stop | kensaki | earlier live | **PASS** | idle service, then stop back to state 0. Date not recorded. Distinct from the later sampled start/stop | source or regression |
| earlier saved-6 reboot | kensaki | earlier boot | **PASS** reapply, notifier **UNVERIFIED** | same missing notifier line as 6 Nov. Date not recorded | do not repeat for the same gap |
| telemetry unload, last-set 12 kept | atarime | earlier live | **PASS** | last-set 12 not applied. Date not recorded | source or regression |

### 6 Nov 2026, about 19:17–19:34 PST

Checkout `5e7cf1f`. Loaded module 0.1.7.
`automatic_floor_recovery=N` and `allow_unverified_writes=N` on both.
No GPU job. Both `dgx-fan-manual.service` units stayed enabled.
Journals from the Sparks use 7 Nov 2026 UTC.

Both machines, without sudo: `./scripts/check` passed 212 tests. No polkit
prompt. That is the offline row above.

Read-only live checks then printed a numeric zone: kensaki 43.9°C, atarime
44.8°C. Health was healthy. This replaces the earlier `c92aa96` read-only
on atarime, which had no zone number because telemetry-only `status` does
not print one. That earlier pass did not validate temperature.

Kensaki `service-start-stop` passed. Sample 1 was still state 0. Sample 2
was state 1. Stop ran. The explicit finish was inactive, state 0, manual
floor none, watchdog active, 41.8°C. An earlier attempt at checkout
`391abc5` read state 0 once and returned before `systemctl stop`. That was
a harness stop, not this pass. Atarime correctly printed `MANUAL` for
`service-start-stop`: no controller start and no floor write.

Idle `unload-reload` passed on both. Kensaki finished at 41.8°C, state 0,
manual floor none, watchdog active, fans 2700/4050. Atarime finished at
42.4°C, telemetry only, watchdog active, fans 2061/2148. Atarime's saved
last-set 12 was not applied and was not edited.

Kensaki reboot, same evening. Preflight was state 0, manual floor none,
41.8°C, performance service inactive, manual unit enabled, watchdog active,
parameters `N`/`N`, no GPU job. `set-state 6` persisted at 41.5°C. The
operator rebooted at 19:28 PST. The previous boot's kernel journal had no
`automatic fan policy active for reboot` line and no
`failed to restore automatic fan policy for reboot` line. The new probe
logged `pinned EC 3.5.8 capabilities fan0=1260..9000 fan1=1890..13500` and
did not log ownership or refusal of an existing floor. Coming up unset does
not prove the notifier ran. At 19:28:27 PST the manual unit logged
`applied manual floor state 6 from last-set`. Final status was state 6,
manual floor 6, watchdog active, 43.1°C. Saved-floor reapplication passed.
Shutdown restore stays unverified.

Cleanup was `sudo dgx-fan-control automatic` and then
`sudo systemctl enable dgx-fan-manual.service`, because `automatic` disables
that unit. Afterward the unit was enabled, state was 0, manual floor was
none, the zone was 40.3°C, and the watchdog was active.

Suspend was then tried on Kensaki. `/sys/power/state` contained `freeze mem`
and `/sys/power/mem_sleep` showed `[s2idle]`. `systemctl suspend` refused
with `Sleep verb suspend is disabled by config`. No suspend occurred.
`/etc/systemd/sleep.conf.d/disable_suspend.conf` sets `AllowSuspend=no`.
`dpkg-query -S` names the file `nvidia-no-systemd-suspend`. `dpkg-query -s`
reports version `25.06-1`, maintainer `dgx-dev@nvidia.com`, description
`Disables suspend using systemd drop-in` and
`Disables suspend by setting AllowSuspend=no`. The package does not say why
suspend is disabled. Leave the drop-in in place. This is a host
configuration block, not a module failure.

### 6 Nov 2026, 19:49 PST preflight

Checkout `5e7cf1f`. Module 0.1.7. Kensaki, before any workload: state 0,
manual floor none, 41.8°C, watchdog active and enabled, performance service
inactive, `nvidia-smi --query-compute-apps` empty, parameters `N`/`N`.
No workload was started. This is not a load pass. Fan RPM was not in this
reading, so the external rig is not taken from the earlier 2700/4050 sample.

### 6 Nov 2026, 19:56–20:04 PST performance curve under load

Checkout `5e7cf1f`. Module 0.1.7. Kensaki. Parameters stayed `N`/`N`.
No ACPI write. At 19:56 both `/var/lib/dgx-fan-control/acpi-hold-boot` and
`acpi-incident` were absent. The installed `PERFORMANCE_CURVE` matched the
checkout: `(0, 1)`, `(50, 3)`, `(55, 5)`, `(60, 8)`, `(65, 10)`, `(70, 12)`.
The defaults grep printed only `BUSY_CHECK=` with an empty value. `gpu_burn`
was not on `PATH`. The executable was
`/home/tipsygnostalgic/gpu-burn/gpu_burn`, 81712 bytes.

At 19:57 the performance service was started and left running. Six seconds
later it was active, state 1, hottest 41.9°C, watchdog active, and manual
floor none. The operator then ran foreground `cd ~/gpu-burn && ./gpu_burn 600`,
with status, the service, and `sensors` watched about every 2 seconds.
The bound was to stop at 70°C on the hottest zone. At 20:00 the operator
reported 70 and that it was holding. At 20:02 `gpu_burn` still showed 70°C
while `status` was state 12, hottest 80.4°C, service active, watchdog
active, manual floor none. `sensors` read `dgx_ec_fan` 9000/13500 RPM,
`acpitz` 0 = 80.4°C, 6 = 63.7°C, 4 = 59.8°C, 2 = 60.2°C, and each mlx5 ASIC
at 60°C. The burn was interrupted. No exit text and no duration were
supplied, so this is not a completed 600 second run.

With the service still running, the next status in the same minute was
state 1, hottest 44.8°C, watchdog active, manual floor none. At 20:04
`nvidia-smi --query-compute-apps` was empty. `systemctl stop` returned
without an error. Status was state 0, manual floor none, 42.7°C, watchdog
active and enabled, and `dgx-fan-manual.service` was enabled. The
performance service's final `is-active` line was not supplied. A kernel
excerpt exported later shows curve writes from 03:59:07 through 04:04:48
UTC ending at state 0, floor `0xffff`. That excerpt does not show a
`-110` in that earlier window. The timeout further down is the next
section, not this ramp.

State 12 at 80.4°C and 9000/13500 RPM is the top of this curve, and the
fall to state 1 at 44.8°C is the idle point below 50°C. Stop returned
state 0. That behavior is supported. The run is not a bounded pass,
because the hottest zone went past the 70°C stop. It is not a module
failure. The older stock-curve and ACPI-hold `gpu_burn` notes are a
different record. Do not repeat this ramp.

### 6 Nov 2026, 20:10–20:25 PST fixed floor and natural timeout

Checkout `5e7cf1f`. Module 0.1.7. Kensaki, EC 3.5.8. Parameters stayed
`N`/`N`. No ACPI write and no `automatic_floor_recovery`. Atarime was
left unchanged. The two Sparks were joined top to top with a thermal pad.
NVIDIA Sync read Kensaki 38 and Atarime 42. That coupling is context for
the low idle zones below. It is not a second test.

Kernel lines in this section are UTC on 7 Nov 2026. PST is eight hours
earlier. The burn process and the fan speed after 20:23 PST were not
confirmed. Do not fill those in.

At 20:10 PST the performance service was inactive, `MANUAL_STATE` was
unset, and both ACPI hold and incident files were absent. `set-state 12`
succeeded: hottest 43.5°C, `manual-floor: 12 (last-set)`, watchdog
active. The kernel line at 04:10:28 UTC is `cooling state=12 lower floor=0x34bc`.
At 20:12 PST, after 60 seconds with no job, status was state 12, hottest
38.8°C. Both `dgx_ec_fan` and `acpi_fan` read 9000/13500 RPM. Other zones
were 36–38.8°C. That is the idle fixed-floor observation. It is not a load.

At 20:17 PST the zone was steady at 36.8°C and the operator started
foreground `~/gpu-burn/gpu_burn 600`. Around 20:20 PST the GPU reading was
71°C and the hottest zone was 80°C and rising. The 70°C hottest stop was
already past. Around 20:23 PST the operator reported the GPU holding about
71–73°C and the hottest zone about 81–83°C, and that the mailbox had
died. The burn was to be interrupted and the status/sensors watch stopped.
Neither the burn's exit nor a later fan reading was supplied. That hold
is not a sustained equilibrium. No thermal policy was changed. The
published 90°C to 98°C spike and the 104.8°C power-loss report stay
background. They were not a trip point edited in this session.

The supplied kernel excerpt, same export, also contains earlier lines that
are not this failure. From 03:59:07 through 04:04:48 UTC the performance
curve wrote floors, ending at state 0, floor `0xffff`. That is the
20:04 PST stop of the curve run above. At 04:06:09 UTC, operation `0x7`
preflight failed `-16` (`-EBUSY`) twice, and the original request was not
submitted. That was before `set-state 12`. The mailbox then accepted the
04:10:28 UTC floor write, so those two busy preflights were not this wedge.

From 04:22:13 UTC the wedge is operation `0x7`, which is
`DGX_EC_FAN_GET_TELEMETRY`. Transaction 3560 is phase `completion`, state
`0x2`, 100 polls, `-110`, with `timeout left as -110; not submitting a
floor read (profile=EC 3.5.8)`. Transaction 3561 at 04:22:14 UTC is the
same operation in phase `preflight`, again `-110`, and
`operation=0x7 preflight failed: -110; original request not submitted`.
Transaction 3562 at 04:22:18 UTC is operation `0x4`,
`DGX_EC_FAN_GET_LOWER_LIMIT`, phase `preflight` only, same `-110`, original
not submitted. Repeats of `0x7` and `0x4` continue through 04:23:07 UTC,
transaction 3587, all phase `preflight`, state `0x2`, no floor read. No
recovery line is in the excerpt. No floor write appears after 04:10:28 UTC.

Operation `0x7` is the hwmon telemetry read (`dgx_ec_fan` `fan*_input`).
Operation `0x4` is the cooling-device floor read (`cur_state`). Operation
`0x5` is the floor write and does not appear after the state 12 line.
With `automatic_floor_recovery` off, a failed `0x7` does not submit
`0x4`. The following `0x4` preflights are separate callers. The status
and `sensors` watch is one such pair: `status` reads `cur_state`, and
`sensors` reads `dgx_ec_fan`. The excerpt does not name the process, so a
thermal-core poll of the cooling device is not ruled out. Each caller
still runs the 100-poll preflight. The driver does not latch a dead
mailbox and skip that poll. It refuses the original command. That is why
the log keeps moving through new transaction numbers without a floor read
and without a recovery.

The last confirmed floor write remains `0x34bc` at 04:10:28 UTC. The
excerpt cannot show that the tachometers stayed at 9000/13500, or whether
the pending command was caused by temperature, the burn, or the telemetry
polling. `sync_send_receive2` has no host timeout. These lines show the
poll loop returning `-110`. They do not show a call that never returned.
Recovery was not demonstrated. Do not set `automatic_floor_recovery`. Do
not write ACPI. Do not run `automatic`, `status`, `sensors`, or an unload
to probe it. The watchdog reads `acpi_fan`, not this mailbox, and was
left running.

### Earlier 0.1.7 observations

The calendar date of these runs was not recorded. They are earlier than
19:17 PST on 6 Nov 2026. Hosts and firmware are the same pair. Module
0.1.7. Do not invent a tighter timestamp.

On kensaki the module refused foreign floor `0x10fe` (4350 RPM) and did not
clear it (`ESTALE` / `-EBUSY`). A reboot cleared it. State 6 then
`automatic` returned to the EC curve. Unload while the module owned state 6
restored automatic control, and the next probe was clean. With the
performance service idle, state went 0 to 1, and stopping the service
returned to 0. An earlier reboot with saved state 6 reapplied 6 and also
lacked the notifier line. That is the same unverified shutdown restore as
the 6 Nov reboot, not a second open test.

On atarime, telemetry-only unload and reload passed. Saved last-set 12 was
kept and was not applied.

### Remaining gaps

These are the open hardware rows. This list is not a request to run them
from this commit.

- Recovery from the 20:22 PST Kensaki mailbox timeout. The timeout itself
  is observed. Do not induce another. Do not claim the fans, the burn
  exit, or a restored mailbox from the excerpt alone.
- The fixed-12 burn was interrupted in the low 80s hottest and is not an
  equilibrium measurement. The idle state-12 soak is already recorded.
  Do not repeat either while this mailbox is unrecovered.
- The reboot notifier, only if a future shutdown can retain the kernel log.
  Repeating saved state 6 without that capture will not resolve it.
- Suspend, only if `AllowSuspend=no` is no longer required. Do not remove
  `nvidia-no-systemd-suspend` to force the test.

### Not a hardware pass

The host cannot stop ACPI or another FF-A client from owning the floor.
A reply that matches in both the packet cache and the OEM image can still
be stale. No captured incident shows mailbox status bits 0–1 clear while
the packet poll stays pending, so automatic recovery stays off. A
`sync_send_receive2` that never returns is not interruptible from this
driver, and the live harness timeout does not interrupt it either.
