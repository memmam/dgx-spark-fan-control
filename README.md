# DGX Spark Fan Control

**Pre-cool your Spark, run your job, return to NVIDIA's fan curve.**

Control a shared minimum fan RPM from Linux userland, or run a temperature-based
performance curve. A small kernel driver talks to the existing firmware; a
Python command controls it. NVIDIA's embedded controller (EC) continues to
apply its own cooling policy throughout.

## Start here

| I want to… | Guide |
| --- | --- |
| Install with Secure Boot | [Installation, signing, and enrollment](docs/installation.md) |
| Rebuild automatically for kernel updates | [DKMS installation](docs/installation.md#3a-dkms-builds-for-kernel-updates) |
| Build and sign a module myself | [Manual installation](docs/installation.md#3b-manual-build-and-sign) |
| Choose what runs at boot | [Startup modes](docs/installation.md#5-choose-your-startup-mode) |
| Turn the fans up before a big job | [Pre-cooling](docs/usage.md#pre-cool-before-a-big-job) |
| Restore the factory fan curve afterward | [NVIDIA automatic control](docs/usage.md#return-to-nvidia-automatic-control) |
| Upgrade or remove the software | [Updates and removal](docs/maintenance.md) |

## A typical session

After [installation](docs/installation.md), stop the optional performance
service before selecting a manual floor:

```sh
sudo systemctl stop dgx-fan-control.service
sudo dgx-fan-control set-state 12   # profile maximum (9000/13500 RPM on EC 3.5.8)
dgx-fan-control status
```

Allow time for the fans to accelerate and temperatures to fall, then run your
workload normally. When finished, remove the added floor:

```sh
sudo dgx-fan-control automatic
dgx-fan-control status             # state=0/12 means NVIDIA automatic control
```

Leave the service stopped to keep NVIDIA's curve active. Starting the service
selects this project's more aggressive curve. The [usage guide](docs/usage.md)
includes a Bash job example that restores automatic control on ordinary exit
or interruption.

## What you control

The driver exposes two read-only RPM sensors through Linux `hwmon`, and a
thermal cooling device with state 0 (automatic) plus 12 common RPM floors.
These are **minimums, not exact per-fan targets**. Each fan saturates at its own
maximum, and firmware can ask for more cooling than your floor.

There is no firmware flashing, upper-clamp control, arbitrary EC memory access,
or raw PWM interface. This cannot make the fans quieter than NVIDIA's own
policy. See the [state table and performance curve](docs/usage.md#choose-a-fan-floor).

## Compatibility

Fan-floor **control** was tested on **NVIDIA DGX Spark, board P4242**, with
**EC 3.5.8** and the SoC firmware it was qualified against, **2.155.11**
(`0x02009b0b`), DGX OS 7.5.0, and kernel `6.17.0-1029-nvidia`. Later checks
used kernel `7.0.0-1019-nvidia`. That SoC pairing is part of the contract.
**EC 3.5.8 with October SoC firmware 2.158.15** (`0x02009e0f`) times out the
FF-A/eSPI mailbox on the first read-only query, so this driver does not bind.
Probe says so and points at the ACPI fan devices below.

**EC 3.13.7** (`fwupdmgr` `0x03000d07`) has been observed on the same board,
including kernel `7.0.0-1024-nvidia`. The capability reply is still revision 1,
unit mode 0, but both fans report **975–4350** instead of 1260–9000 and
1890–13500. Those figures match the ACPI tach target `975 + 33.75 × level`
(level 100 is 4350). They are not a rescaling of the older RPM-mode control
ranges, which the EC converts to PWM. A public diff of the EC images found the
override handlers and their dispatch table byte-identical between 3.5.8 and
3.13.7. They return **-5 (unsupported)** or **-116 (timeout)** when an enable
bit is clear. What changed is host command **0xEB**: it now honors a permission
table that marks the fan/thermal register block read-only, so the host can no
longer set that enable bit. This is deliberate access control. Floor writes on
3.13.7 are therefore expected to fail. Version 0.1.5 still accepts the exact
capability reply. If the floor command is rejected, the module registers hwmon
telemetry only and does not create a cooling device. `dgx-fan-control status`
then prints `control unsupported on this firmware`. The 3.13.7 state table,
used only if a write is actually accepted, is **not qualified**.

On **EC 3.5.8 plus the October SoC**, the alternative that has been observed to
work is the ACPI fan device: `/sys/class/thermal/cooling_device*` with type
`Fan` and `max_state` 100. The BIOS maps a level to `975 + 33.75 × level` RPM.
Reported measurements: level 100 about 4320 RPM, level 30 about 1940, level 20
about 1620 with the second fan stalling, and level 0 stops both fans. Do not
write a level below 30; the observers use 40. This project does not drive those
ACPI devices. On EC 3.13.7 the same ACPI write is accepted and does not change
fan speed.

Other GB10 boards, generic kernels, and any capability reply other than the
two profiles above are rejected. The driver does not relax validation to make
an unknown reply succeed.

You need Python 3.10+, matching NVIDIA kernel headers and build tools, and an
enrolled local signing certificate when Secure Boot is enabled. DKMS is optional;
systemd provides boot loading and the optional performance service.

This is an independent, experimental project, unaffiliated with NVIDIA.
Version **0.1.5** recognizes the EC 3.13.7 capability reply, binds telemetry
only when floor control is rejected, and tells a probe timeout apart from that
case. The performance curve never requests state 0 while the daemon is running.
A reply-guard recovery refusal is retried after the 30-second cooldown; a
foreign floor remains fatal. Version **0.1.4** keeps a loaded module on NVIDIA's
curve when the performance service is not running. State 0 is that curve. A
latched lower clamp of 0, which stops both fans and survives a warm reboot, is
cleared on load and on every automatic restore when floor writes still work.
Version **0.1.3** includes bounded in-driver recovery for the observed stale-pending,
idle-mailbox failure. It validates physical mailbox observations, submits one
read-only recovery request, checks the reply, and reconciles floor ownership
before continuing. Timed-out setters are never blindly replayed. Recovery
attempts have a shared 30-second cooldown; busy, unreadable, or inconsistent
mailboxes remain errors. The underlying firmware defect remains, and long-term
unattended reliability is not yet qualified. See
the [incident logs](docs/incident-logging.md), which retain recent transaction
timing and recovery observations without extra EC traffic, and the
[recovery instructions](docs/troubleshooting.md#persistent-pending-and-service-exit-69).
Orderly stop, suspend, reboot, and module removal request and verify automatic
control. A hard crash or transport failure can prevent restoration; manual
floors have no expiry timer. Read the [validation and limitations](docs/validation.md).

## Development

Run `./scripts/check` (Python 3.10+ and a C compiler) for hardware-free policy,
failure-path, compiled C transaction, signing-wrapper, and source-contract tests
plus shell syntax checks. GitHub Actions runs the same checks. Kernel
compilation and real EC behavior need a compatible Spark.

- [Firmware protocol](docs/protocol.md)
- [Firmware pending-state investigation](docs/firmware-pending-analysis.md)
- [Validation record](docs/validation.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Contributing](CONTRIBUTING.md)

## License

[GPL-2.0-only](LICENSE), matching the original kernel driver's SPDX identifier.
Copyright 2026 Christopher Owen.
