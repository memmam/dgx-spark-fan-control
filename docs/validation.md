# Validation and provenance

## Rollback point for this branch

`e57001b9adaf1ca5695da447939c8def68eb51f5`, author date 2026-11-06 11:26:42
−0800, is the git tip before `3fdcc4d` (13:46 −0800 the same day), the first
ACPI thermal-escalation commit. History from there back is linear. Nothing
was committed during unit-a's step-down searches (about 11:31–13:03
−0800), so this commit is the tree those searches ran against. It is not a
hardware-proven last-working module. The later loaded module was 0.1.7.
This commit is module 0.1.6. Its parent `2955201` differs only in install
documentation.

The daemon curves in this commit are the accepted ones: default 50→3, 55→5,
60→8, 65→10, 70→12 with the state-1 pin, and the opt-in quiet table from
the tenhkspark patch. ACPI level samples from that afternoon stay research
evidence. This tree does not write ACPI `cur_state`.

With the module unloaded, a true-zero stall used to write ACPI level 100.
That write is removed and nothing is substituted for it. The alert, the one
reboot when no work is running, and the true-zero poweroff at 85 °C remain.
Those actions were not re-measured on hardware for this commit.

Carry-forward, decided on this branch:

- EC 3.5.8 keeps the one idle-mailbox floor read. Status `0x08` with the
  packet poll still 2 was measured, and one `GET_LOWER_LIMIT` recovered
  that case. The floor write is not replayed.
- The 6 Nov timeout happened while that read was already disabled. Its OEM
  status was not captured. That log does not make the read unsafe, and it
  does not show the read would have cleared that wedge.
- EC 3.13.7 still gets no floor command, including after a telemetry
  timeout. There is no module parameter for this. `allow_unverified_writes`
  stays off.
- No ACPI `cur_state` writes. No fault latch. Curves and the 85 °C
  true-zero cutoff are unchanged.
- The module version is 0.1.8 so it is not labeled as stock 0.1.6 or as
  the loaded 0.1.7.
- `scripts/ec358-regression` is the EC 3.5.8 check for this delta. With
  no `DGX_FAN_SUPERVISED=1` it does not touch the host. It stops unless
  fwupd or a current kernel pin shows EC 3.5.8 and the other source does
  not show a different version. `floor` is set-state 6 then automatic, and
  it checks that ACPI Fan `cur_state` does not change. No GPU load.

November rows below use unit-a and unit-b for the two machines. Those ids
are not firmware versions; each row states the firmware that unit was
running. Host names and home-directory log paths are omitted. Script and
log names here are sanitized equivalents of the names used on the machines.

## unit-a script, 6 Nov 2026 23:22 PST

Checkout `0d9b49e` on unit-a, EC 3.5.8. The run was
`scripts/ec358-regression` with `DGX_FAN_SUPERVISED=1`. The script
printed `PASS preflight`, `PASS install`, and `PASS floor`. No status
text, RPM, temperature, or kernel log was supplied with those lines, so
this row is the script result only. unit-b was not run. No GPU load was
run. Do not repeat these three cases unless this command or the installed
module changes, or a regression is being checked.

Older sections below are the record as it stood at `e57001b`, including
commands this tree no longer provides.

## unit-a direct `_FSL` 110, 7 Nov 2026 about 14:11–14:12 PST

Times in this row are the machine clock stated as PST. unit-a, EC 3.5.8,
kernel `7.0.0-1024-nvidia`. The controller was fully uninstalled. No GPU
load. unit-b was not run. No further write follows from this row.

Checkout `ac496545149aa87c413f0eea6224d0480f8918aa`. The temporary module
is `research/fsl110`. It is not DKMS and it is not the controller. About
14:11 PST, `build-sign-ec358.sh` printed `BUILD_SIGNED_NOT_LOADED`. gcc was
13.3.0. The signature hash was sha512. `modinfo` signer was `DGX Spark
local module signing`. The build had no failure. It warned about the
compiler name only, and it skipped BTF because `vmlinux` was absent.

About 14:12 PST the operator ran `sudo DGX_FSL110=yes bash ./run-once.sh`.
The script printed `before 2700 4050`, then `loaded`, then status
`interpreter_ok`. Sample 1 was hwmon0 2700 and hwmon1 4050. Sample 2 was
hwmon0 4500 and hwmon1 4590. Sample 3 was the same 4500/4590. The module
was then unloaded.

One direct `\_SB.FAN0._FSL` with argument 110 completed. That argument
requests 4687 on both channels. The published `acpi_fan` speeds left
2700/4050 and landed above the earlier level-100 observation of about
4320/4320, so the downstream read was not hard-clamped at that earlier
level-100 result. These numbers are not an independently verified physical
shaft speed on EC 3.5.8. The calculated producer and the readback path are
still only bounded, not traced into the tachometer.

Statuses:

- BUILD/SIGN: PASS
- single request, evaluation, and readback response: PASS
- physical RPM: UNVERIFIED
- sustained behavior: UNRUN
- reboot cleanup: PASS
- restored idle baseline: PASS

Unload did not clear the hold. About 14:14 PST the operator rebooted and
answered yes when asked whether fanwatch showed 2700/4050 again. That
confirms the idle pair returned. It is not a load sweep and it does not
make the 4500/4590 reading a verified shaft speed. No further unit-a fan
write follows from this row.

## unit-b direct `_FSL` 110, 7 Nov 2026 about 14:17 PST

The sample clock is stated as PST. Host-log timestamps that were UTC stay
UTC. unit-b, EC 3.13.7. The controller was fully uninstalled. No GPU load.
This does not repeat on unit-a. `build-sign-ec3137.sh` printed `SIGNED` and
`BUILD_SIGNED_NOT_LOADED`.

`run-once-ec3137.sh` zones before, millidegrees C: 43500, 40800, 41700,
40800, 41700, 43500, 40800. GPU utilization 0 and 41 °C. Fans 2066 and
2156. Status `interpreter_ok`. Samples 2066/2156, 2071/2155, 2066/2156.
Zones after: 43500, 40800, 41600, 40800, 41800, 43500, 40800. The module
was unloaded.

Before the reboot, about a minute of fanwatch at 42–43 °C stayed near
2061–2077 and 2148–2162. One sample showed fan 2 at 0 between normal
samples, and the next second was normal again. That zero is not a verified
physical stop, and it is not attributed. A kernel log excerpt was saved.
The full file was not supplied. The posted warnings match startup lines
also seen on unit-a, plus an unclean FAT mount of `sda1`. `lsblk` showed
that device as the recovery USB `BOOTME`, not an internal disk. No
fan-specific error was reported. unit-a's current `-b` log is from after
its reboot, so it cannot be compared with the trial.

The operator then rebooted. About two minutes of fanwatch at 43 °C stayed
near 2070/2155 with no zeros, and fanwatch was then stopped. The idle tach
looks intact. That is not proof that every hold bit was released.

`interpreter_ok` means the interpreter returned. It does not mean the EC
accepted or rejected argument 110. There was no observed sustained speed
change. That is not a proven protocol rejection. unit-a's 4500/4590
reading and unit-b's tach are different readouts. A change in one is not
physical evidence about the other. Argument 110 is not repeated from this
record.

Statuses:

- BUILD/SIGN: PASS
- evaluation: PASS
- sustained speed response: no observed effect
- protocol rejection: NOT PROVEN
- physical RPM and the transient zero: UNVERIFIED
- post-reboot idle tach: recorded, hold release not fully proven

## unit-a ramp through 150, 7 Nov 2026 14:43:51 PST

Host clock `2026-11-07T22:43:51+00:00`, which is 14:43:51 PST. Checkout
`7fdc6ca`. Kernel `7.0.0-1024-nvidia`. Build and sign passed on the
enrolled certificate. The ramp module was signed. No zeros and no drops
were reported. Published numbers are not shaft speeds.

Fifteen baseline samples were all 2700/4050. Each later dwell's first
sample was still the previous pair. From sample 2 onward the pair was
stable. Medians of the last five samples:

| Argument | Request | Settled published pair |
| ---: | ---: | --- |
| 120 | 5025 | 4950/4994 |
| 130 | 5362 | 5310/5265 |
| 140 | 5700 | 5670/5670 |
| 150 | 6037 | 6030/5670 |

At 150 the second of those channels stayed 5670. The script printed
`STOP_PLATEAU` and unloaded. That was the single-fan rule, not a measured
ceiling. Levels 160 and above were not issued. No release write. At about
14:48 PST a reboot of both units was reported. No unit-a post-reboot
2700/4050 pair has been supplied, so this sweep's restore is not confirmed.

Statuses:

- BUILD/SIGN: PASS
- arguments 120–150, evaluation and readback: PASS
- stop at 150: policy abort, not an established maximum
- physical RPM: UNVERIFIED
- arguments 160–237: UNRUN
- reboot after this sweep: PENDING

## unit-a continuation from 160, blocked before any write, 7 Nov 2026 14:55:24 PST

Host clock `2026-11-07T22:55:24+00:00`. The script printed fifteen baseline
samples, all 2610/3915, then `STOP_NOT_BASELINE raw=2610 3915`. It did not
print `loaded` or a step write. Zone files were checked and not printed.
No temperature was supplied for this capture, and none is filled in here. In `research/fsl110/ramp-ec358.sh` that
exit is in the baseline loop. `insmod` is the next statement after the loop,
and `stepped=1` is only set inside the step loop. No module was loaded and
no `_FSL` was issued. The guard is unchanged.

2610/3915 is 29 percent of 9000/13500, and it is also the EC 3.5.8 duty
formula at percent 29: `1260 + 15/86 * 7740 = 2610` and
`1890 + 15/86 * 11610 = 3915`. The idle pair 2700/4050 is that same formula
at percent 30, which is the duty byte on the first step of both stock
curves. The thermal walk applies that duty byte with no add or subtract.
The image does not show a step that turns 30 into 29. An older one-percent
gap, 75 versus measured 74, is the same kind of discrepancy and is still
unresolved. This pair is also exactly two-thirds, which is the telemetry
shape. The post-`_FSL` readings in the earlier ramp were near each other
(4950/4994, then 6030/5670), not two-thirds. That difference is consistent
with the EC curve publishing, and it is not proof the floor register is
clear, that the machine was damaged, or that 29 percent is the normal idle
step.

The 14:48 PST reboot was stated. This row does not mark that restore PASS.
Arguments 160–237 stay **UNRUN**. Do not loosen the baseline check and do
not retry from the 2610/3915 sample alone.

A fanwatch log from unit-a then shows fourteen 1-second samples
from 22:58:02 through 22:58:15 UTC, which is 14:58:02 through 14:58:15 PST.
Every sample is the reported SoC at 42 °C and fans 2610/3915, with no zeros.
That label is the fanwatch SoC reading, not a hottest-zone selection. The
pair is steady. It does not identify a cause, it does not establish 29
percent as the normal idle step, and it does not close the reboot. The
sweep stays blocked.

Statuses:

- this attempt: BLOCKED before write
- BUILD/SIGN for the continuation binary: not a new result
- arguments 160–237: UNRUN
- reboot restore after the 150 sweep: still PENDING in this capture

## unit-a idle pair after the 2610 hold, reported 7 Nov 2026 about 18:31 PST

Reported, not sampled by `ramp-ec358.sh`: the 2610/3915 pair persisted until
a reboot, and after that reboot the idle pair was 2700/4050. The 14:55
attempt remains a blocked run. Arguments 160–237 remain **UNRUN**. The
continuation prepared in `e19bef7`, now `research/fsl110/ramp-ec358.sh`,
still requires fifteen of its own 2700/4050 baseline samples before it
loads a module. A reported idle pair does not skip that gate.

## unit-a ramp build passed, run blocked, 8 Nov 2026 03:28:56 +00:00

`date -Is` printed `2026-11-08T03:28:56+00:00`. The offset is the machine
zone. That instant is 19:28:56 on 7 Nov in Pacific Standard Time. The
launcher's old sentence said the clock was not labeled PST. The offset on
`HOST_DATE` is the label.

Kernel `7.0.0-1024-nvidia`, checkout `cursor/acpi-reply-ramp-e2ec`.
`build-sign-ramp.sh` printed `BUILD_SIGNED_NOT_LOADED`. gcc 13 matched the
kernel. The build warned about the compiler name and skipped BTF because
`vmlinux` was absent. The same build compiled the other research modules.
`fsl_reply_read` warned that its stack frame was 1520 bytes, above 1024.

`DGX_FSL_RAMP_EC358=yes bash ramp-ec358.sh` then printed the date, the clock
sentence, and the zone legend, and stopped with `STOP_EC_UNVERIFIED`. No
module was loaded and no `_FSL` was issued.

That stop is what the checker had to do. fwupd `get-devices` omits an
Embedded Controller whose updates are blocked, and this unit's EC 3.5.8
updates were blocked. The kernel pin is logged only after
`dgx_ec_fan_control` probes, and this launcher stops while that controller
is installed. Neither source could show a version. This is not a ramp pass.
Arguments 160–237 stay **UNRUN**.

The checker now also reads the EFI system resource table `fw_version` for
the same EC class. A missing table stays unverified. A version that
disagrees with fwupd or the kernel pin still stops. The reply reader's
1024-byte buffer is no longer on the stack.

Statuses:

- BUILD/SIGN: PASS
- this run: BLOCKED before write
- arguments 160–237: UNRUN

## unit-a argument 160, partial dwell, 8 Nov 2026 03:35:42 +00:00

`date -Is` printed `2026-11-08T03:35:42+00:00`. That instant is 19:35:42 on
7 Nov in Pacific Standard Time. Kernel `7.0.0-1024-nvidia`. The profile
line was `fwupd=absent pin=absent esrt=3.5.8 verdict=OK`. The earlier
`fsl_reply_read` frame-size warning was absent from this build.
`BUILD_SIGNED_NOT_LOADED` had already passed.

Fifteen baseline samples were 2700/4050. Hottest zone 41.7 °C. GPU
temperature 39 °C. The module loaded and the status was `ok 160`. Argument
160 requests 6375. The reported tach after the call:

| Sample | Reported pair |
| ---: | --- |
| 1 | 2700/4050 |
| 2 | 4500/6345 |
| 3–9 | 6300/6345 |

The hottest zone moved from 41.7 °C to 40.8 °C. GPU temperature moved from
39 °C to 38 °C. Sample 10 was not a fan pair. The script stopped with
`STOP_GPU util=1 nvidia_gpu_temp=38C raw=1, 38`, unloaded, and printed
`REBOOT_NOW`. The operator was told to reboot immediately.

Nine samples are not the dwell. The plateau rule uses the median of samples
11–15. Those samples were not taken, so argument 160 has no settled median
and is not a dwell pass. The reported pairs are not a measured shaft speed.
Arguments 170–237 were not issued. The operator reported that after the
reboot the idle pair was 2700/4050, at about 19:42 PST on 7 Nov. That is a
firsthand report, not a timestamp from the host log and not a sample from
the launcher. It does not finish the dwell.

The exact-zero GPU check that stopped this run is a policy rule. One percent
on `utilization.gpu` is one percent of the driver's sample window. Zone and
GPU temperatures were falling, so this sample is not evidence of a heating
workload. It is also not evidence that every non-zero reading is harmless.
A later code change records 1–4 and stops at 5 or more. Five is a policy
cutoff, not a measured fan-demand limit. This run remains a stop under the
rule that was loaded.

A later sweep that still compares settled medians has to issue 160 again
after a confirmed idle baseline of 2700/4050. Skipping to 170 would compare
the next dwell with the baseline, not with a 160 median this run never
finished. That repeat is not authorized in this record.

Statuses:

- BUILD/SIGN: PASS
- profile line: PASS as `esrt=3.5.8` with the other two sources absent
- argument 160 evaluation: PASS
- argument 160 dwell: INCOMPLETE, 9 samples, no median
- physical RPM: UNVERIFIED
- arguments 170–237: UNRUN
- reboot after this write: reported, idle pair 2700/4050 about 19:42 PST on 7 Nov. Not a script sample. The dwell stays incomplete.

## unit-b argument 120, two runs, 7 Nov 2026

Same checkout and kernel. The first build attempt used the unit-a script
and correctly printed `STOP_HOST`. The unit-b probe then signed and
printed `BUILD_SIGNED_NOT_LOADED` on the enrolled certificate. Both runs
were appended to the probe host log. No further unit-b write follows from
this row. `interpreter_ok` is not an EC acceptance or rejection.

First start 14:44:05 PST, while the unit-a ramp was also running. The two
machines shared a thermal pad and fan rig, so this run is confounded.
Drift was already present before the write. The last five
baseline pairs were 2079/2173, 2076/2177, 2085/2173, 2081/2170, 2081/2171.
After the call, fifteen samples stayed about 2071–2084 and 2169–2182. No
zero.

The machine was rebooted and the probe was rerun with unit-a not driving
the rig. Second start 14:48:30 PST. Fifteen baseline samples stayed about 2068–2079 and
2149–2165. The last five were 2069/2153, 2070/2159, 2069/2149, 2075/2157,
2071/2159. After the call, fifteen samples stayed about 2063–2076 and
2151–2161. The last five were 2076/2155, 2073/2159, 2074/2159, 2072/2161,
2070/2159. `PROBE_DONE`, then unload. No zeros. No observed sustained
change. That is not a proven protocol rejection. A reboot after this second
call was requested and is not yet confirmed. The second run does show that
unit-b had booted far enough to execute the probe. It is not a unit-a
post-reboot fan pair.

A later fanwatch, about 17 seconds, SoC 45–47 °C, showed fan 1 at 2064–2079
and fan 2 at 2156–2168, with no zeros. The operator did not say this sample
was after the post-probe reboot, so it does not close that reboot. It is the
same idle tach band as the isolated probe. No further unit-b write follows
from this row. `interpreter_ok` is still only the AML interpreter. The captured
`WRB` method does not test the SMC response, and the research caller discards
it. These tach readings do not separate an EC rejection from a write the EC
accepted and did not apply.

## unit-b `_FST` package read, 7 Nov 2026 15:11:56 PST

Host clock `2026-11-07T23:11:56+00:00`. Checkout `e989d4f`, then the
temperature-path fix the machine already had from `5cdf3b9`. Kernel
`7.0.0-1024-nvidia`. `test-fst-read.sh` printed
`override_rejected_before_hardware` for all three overrides and
`fst_read_offline_ok`. Build and sign passed: Secure Boot enabled, enrolled
MOK, sha512, `BUILD_SIGNED_NOT_LOADED`.

Zones, millidegrees C, type `acpitz`: 42800, 40800, 40800, 40900, 41700,
42800, 40700. Hottest of those zones 42.800 °C. `temperature.gpu` 41 °C.
`hwmon0` `fan1_input` 2068, `hwmon1` `fan1_input` 2156. The module printed:

```
\_SB.FAN0._FST 0 0 2068
\_SB.FAN1._FST 0 0 2156
```

Then `FST_READ_DONE` and unload. The three integers are the `_FST` package:
revision, cached control, speed. Both controls are 0. Both speeds match the
`fan1_input` values from the same run. This is a successful read only. It
does not prove the earlier write was rejected, it does not prove an EC floor
is clear, and it does not confirm the reboot after the second argument-120
call. This read is not repeated from this record. The unit-a 160
continuation stays blocked on the 2610/3915 baseline.

Statuses:

- BUILD/SIGN: PASS
- `_FST` read, cached control 0, tach match: PASS
- write rejection, floor clear, reboot: not shown

fwupd on this machine, transcribed without serial numbers. Embedded
Controller current `0x03000d07`, minimum `0x02003400`, GUID
`3d13c989-e6a8-4ead-95ee-921f09868f65`, with a reported successful update
`0x03000508` to `0x03000d07`, capsule on disk. Two UEFI device-firmware
entries, both success and capsule on disk: `b488217b-3895-4fc0-b1bf-ab7005a2d45a`
current `0x02009e0f` minimum `0x02007300`, and
`dd1a238a-5f8e-46bd-9401-a88da99c5a96` current `0x00000519` minimum
`0x00000400`. `/boot/efi` is vfat on `/dev/nvme0n1p1`. That node is not to
be read. `/lib/firmware` contains `nvidia/`, `7.0.0-1019-nvidia/`, and
`7.0.0-1024-nvidia/` among ordinary Linux firmware. `nvidia_ffa_ec` is
builtin, `drivers/platform/arm64/nvidia-ffa-ec`, softdep `arm-ffa`.

The EFI listing is now in. `/boot/efi/EFI` has `BOOT`, `ubuntu`, and
`UpdateCapsule`. `UpdateCapsule` is empty. `ubuntu/fw` is empty. `ubuntu`
holds `grubaa64.efi`, `grub.cfg`, `shimaa64.efi`, `mmaa64.efi`,
`BOOTAA64.CSV`, and `fw/`. `BOOT` holds `BOOTAA64.EFI`, `fbaa64.efi`, and
`mmaa64.efi`. The name filter on the three firmware directories above
matched GPU microcode and unrelated device-tree board files. It did not
match a name containing `ffa` or `socfw`. `/usr/src` has no
`nvidia-ffa-ec*` file. These are filename results, not a content search, so
they do not prove the SMC handler is missing from the machine. Empty
capsule directories mean nothing is staged now. They do not identify the
monitor that answered the earlier SMC. No further listing is part of this
record. Acceptance of the setpoint remains unshown. The unit-a sweep stays
blocked.

## Same-boot `_FST` around one unit-b `_FSL`, not prepared

Recorded 7 Nov 2026. A same-boot `_FST` before and after one existing
request, 110 or 120, would show the cached control before a reboot. No
module was added and no command is given. In the captured DSDT, `WFCF`
assigns `FLVL = Arg0` before its four `WRB` calls. The assignment does not
read the SMC reply. Both fans' `_FST` methods publish that same name. After
`_FSL` returns success, the control field is the argument for the rest of
that namespace. Argument 120 leaves 120. Argument 110 leaves 110. The 15:11
control value 0 is the initializer `Name (FLVL, Zero)`. A reboot rebuilds
that name. This comparison restates the method. It does not show whether
the EC kept the setpoint. The unit-a continuation remains UNRUN until
`ramp-ec358.sh` sees 2700/4050 itself.

Statuses:

- this comparison: NOT PREPARED
- arguments 110 and 120: no new run
- EC acceptance: still unshown

## unit-a continuation procedure after the partial 160

The module list still starts at 160 and ends at 237 (request 8973). It does
not repeat 120–150. Argument 160 was issued at 03:35:42 +00:00 and its dwell
did not finish, so the list has not been retired. The next write, when one
is authorized, requires a fresh 2700/4050 baseline observed by the launcher
on that run. The report of about 19:42 PST on 7 Nov, idle pair 2700/4050
after the reboot, does not replace that baseline. A single fan whose settled
median does not rise
is logged as `PLATEAU_RECORDED` and the sweep continues. The sweep stops if
both medians fail to rise, if either median falls, or on zero, a bad
readout, a zone outside 25–60 °C, GPU utilization of 5 or more, or a reading
above 9500. Utilization of 1–4 is logged and does not stop. Settled samples
in the 120–150 dwells did not jitter, so equality is a plateau and a lower
median is a decrease. No deadband is added. No release write. Reboot after
any step. The reboot recovery is already reported and is not an open
question. `ramp-ec358-preflight.sh` only reads. It does not load a module.

## unit-b write-reply capture, not yet run

**UNRUN.** `research/fsl110/fsl_reply.c` registers a kretprobe on
`acpi_ffh_address_space_arch_handler`. It does not replace that function
and it does not evaluate ACPI. For a buffer whose first qword is
`0xC400008D`, it keeps that qword and the qword at offset `0x20` from
before the call, and the same two qwords after the call returns. On
mainline arm64 those qwords are SMCCC `a0` and `a4` for the 64-bit FFH
region. This repository does not contain the running kernel's `ffh.c`, so
the printed numbers are the evidence. No saved capture contains them.

`reply-ec3137.sh` samples fans first, then loads the observer, and refuses
the write unless the proc file says `state armed` and no captured `pre_a4`
has low byte `0x09`. A tach read uses low byte `0x0C` and does not by itself
block the write. It then issues one `_FSL` argument 120 and prints the proc
file before it reads the fans again. Four records whose low byte is `0x09`
would be the four `WRB` writes from one call. `post_a0` of `c400008e` is the
sentinel the read path already requires before it publishes a speed. That
sentinel would still not prove the fan loop stored the bytes. Zero write
records means the observer did not see the call. Confidentiality lockdown
stops the script before either module is loaded. A failed `insmod` of the
observer does not load the probe.

The module compiled on kernel `7.0.0-1024-nvidia` when the ramp was built,
including the kprobe helpers in that kernel's headers. Compilation does not
show that the handler's first argument is the FFH buffer, or that offsets 0
and `0x20` are SMCCC `a0` and `a4`, on that kernel. Those offsets are the
mainline arm64 FFH offset-1 layout. This tree does not contain that
kernel's `ffh.c`. `reply-ec3137-preflight.sh` only reads the profile, the
lockdown mode, and whether `/proc/kallsyms` lists the handler. A present
symbol does not verify the layout and does not authorize the write. The
return probe copies the buffer before the caller continues; if this kernel's
handler does not leave the result there, `post_ok=0` or a nonsense qword is
the result, not an acceptance.

Statuses:

- this capture: UNRUN
- argument 120 with a reply buffer: not a new result
- EC acceptance: still unshown

## Version 0.1.5 on EC 3.5.8 with October SoC, 6 Nov 2026

Both DGX Sparks, previously EC 3.13.7, downgraded to EC 3.5.8 with fwupd and
the EC blocked in `DisabledDevices`. SoC/UEFI stayed October `0x02009e0f`.
Kernel `7.0.0-1024-nvidia`. Module 0.1.5.

Probe bound. The kernel log showed `pinned EC 3.5.8 capabilities
fan0=1260..9000 fan1=1890..13500`, baseline 2700/4050 RPM, a cooling device,
and state 1 floor `0xa8c`. With the daemon stopped, `set-state 6` measured
about 7560/7155 RPM. State 12 wrote floor `0x34bc`. This is the opposite of
the earlier report that this pair always times out the mailbox. That timeout
has also been seen, on other boots, so it stays the fallback when probe
fails. It is not the only outcome.

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

The fan service was enabled and started at boot even though an earlier step
had left it disabled. `module enable` had been enabling it whenever fan
control was supported. It no longer does that.

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
