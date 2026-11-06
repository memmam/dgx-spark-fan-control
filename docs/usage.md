# Fan control for everyday work

[← README](../README.md) · [Installation](installation.md) · [Maintenance](maintenance.md)

There are three modes:

| Mode | What controls the added floor | How to select it |
| --- | --- | --- |
| NVIDIA automatic | No added floor | Stop the service, then run `automatic` |
| Manual boost | Your selected state, until you change it | Stop the service, then run `set-state` |
| Performance curve | This project's temperature-based daemon | Start the service |

NVIDIA's thermal policy remains active in all three modes. Use one policy
writer at a time; stop the service before a manual boost.

## Pre-cool before a big job

### 1. Request maximum cooling

```sh
sudo systemctl stop dgx-fan-control.service
sudo dgx-fan-control set-state 12
dgx-fan-control status
```

State 12 requests that profile's maximum common floor. On EC 3.5.8 the two fans
can reach approximately **9000 and 13500 RPM** in the firmware's RPM-mode
control encoding. They accelerate under firmware control; setting a state does
not instantly change their measured speed. EC 3.13.7 is expected to reject the
write. See [compatibility](../README.md#compatibility).

### 2. Let the machine cool, then run your workload

Watch the hottest reported temperature:

```sh
watch -n 2 dgx-fan-control status
```

Press Ctrl+C to stop watching. Allow tens of seconds for pre-cooling and check
actual temperatures and [RPM readings](#read-the-fan-rpms); the time needed
varies with ambient temperature and existing load. There is no universal
pre-cooling temperature or guaranteed performance improvement.

Run your training, inference, rendering, or other job as your normal user.
The manual floor remains active throughout, even if the command that set it
has already exited. A moderate boost is also available, for example
`sudo dgx-fan-control set-state 3` (4500 RPM on EC 3.5.8).

### 3. Return to NVIDIA automatic control

When the job finishes:

```sh
sudo dgx-fan-control automatic
dgx-fan-control status
```

Expect **`state=0/12 (no floor; EC curve)`**. That is NVIDIA's curve, not fans stopped. Leave the service stopped to retain it. Enabling the service is a separate command; `module enable` does not do it.
Firmware hysteresis can keep fans fast for a while after the floor is removed.

## Return to NVIDIA automatic control

From either manual or performance mode:

```sh
sudo systemctl stop dgx-fan-control.service
sudo dgx-fan-control automatic
dgx-fan-control status
```

To make that the default on future boots too:

```sh
sudo systemctl disable dgx-fan-control.service
```

The module may stay loaded for RPM monitoring and later manual boosts. There
is no need to unload it or reboot to return to the factory curve. With the
service stopped, state 0 is that curve. It does not stop the fans.

## Wrap a job with ordinary-exit cleanup

For terminal-driven jobs, this **Bash subshell** sets the floor, allows 30 seconds
of pre-cooling, runs a job, and attempts to restore the factory curve on exit,
including job failure and handled Ctrl+C/SIGTERM. Replace the marked job command:

```bash
(
  set -e
  sudo -v

  restore_automatic() {
    job_status=$?
    trap - EXIT
    if ! sudo dgx-fan-control automatic; then
      echo "Automatic restoration failed; check dgx-fan-control status and the kernel log." >&2
      exit 1
    fi
    exit "$job_status"
  }
  trap restore_automatic EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM

  sudo systemctl stop dgx-fan-control.service
  sudo dgx-fan-control set-state 12
  sleep 30
  ./your-job --your-options  # replace this line; run the workload without sudo
)
```

Keep the terminal available: `sudo` may request your password again after a
long job. This is not an unattended scheduler or a crash watchdog. Bash can
defer a signal trap while waiting for a foreground child; SIGKILL, loss of the
shell, or transport failure may prevent cleanup. A manual floor has no timer.
Always verify `dgx-fan-control status` after interruption. This example leaves
the performance service stopped, even if it was running before the job.

## Use the performance curve continuously

For automatic extra cooling as temperature rises:

```sh
sudo systemctl start dgx-fan-control.service
dgx-fan-control status
sudo journalctl -u dgx-fan-control.service -n 30
```

Use `sudo systemctl enable dgx-fan-control.service` if you also want this at
boot. This is **our performance curve**, not NVIDIA's factory curve. While the
daemon is running it never selects state 0. Below 50°C it holds state 1, the
first additive floor, because a clamp of 0 has been observed to latch with the
fans stopped. Stopping the service still restores state 0 (NVIDIA's curve).
Temperatures below are the default `performance` curve. `--curve quiet` or
`DGX_FAN_CURVE=quiet` selects the tenhkspark opt-in curve (state 1 below
52°C, state 12 at 78°C) for EC 3.5.8 with the pre-October SoC when less noise
is preferred. Both curves keep state at 1 or higher while the daemon runs.
The state numbers are the same on every profile; the kernel maps them through
the table below.

| Filtered hottest temperature | Target state | EC 3.5.8 floor | EC 3.13.7 reported floor |
| ---: | ---: | ---: | ---: |
| Below 50°C, daemon running | 1 | 2,700 | 2,000 |
| 50°C | 3 | 4,500 | 2,427 |
| 55°C | 5 | 6,300 | 2,854 |
| 60°C | 8 | 9,000 | 3,495 |
| 65°C | 10 | 11,250 | 3,922 |
| 70°C or above | 12 | 13,500 | 4,350 |

The 3.13.7 column is the reported capability span, not a measured tachometer
table. Floor writes on that firmware are expected to be rejected. If they are,
there is no cooling device and this curve does not run.

It samples every two seconds, smooths temperature with an exponential filter
(`alpha=0.35`), rises by at most two states per sample, and falls by at most
one with 4°C hysteresis. If no valid kernel thermal-zone readings remain, it
requests state 12 immediately. Individual invalid sensors are skipped.

Constants live in [`userspace/dgx_fan_control.py`](../userspace/dgx_fan_control.py).
The daemon rereads the kernel state each sample, including after suspend/resume.
Brief transport failures receive two retries, after two and four seconds. Three
consecutive failures attempt automatic restoration and exit 1. Exit 69 is only
the startup check that fan control is unsupported. Systemd restarts exit 1
every 30 seconds and does not restart exit 69. See [troubleshooting](troubleshooting.md).

## Choose a fan floor

State 0 is automatic on every profile. State 12 is that profile's reported
maximum. Values in between stay inside the reported range and never use a
non-unset floor below the greater reported minimum.

### EC 3.5.8

These are the observed RPM-mode clamp values. The EC converts each one to PWM
against fan 0's 1260–9000 range and fan 1's 1890–13500 range. States 1–8 step
by 900; states 9–12 step by 1125. This table is unchanged.

| State | Common floor | State | Common floor |
| ---: | ---: | ---: | ---: |
| 0 | Automatic / unset | 7 | 8,100 |
| 1 | 2,700 | 8 | 9,000 |
| 2 | 3,600 | 9 | 10,125 |
| 3 | 4,500 | 10 | 11,250 |
| 4 | 5,400 | 11 | 12,375 |
| 5 | 6,300 | 12 | 13,500 |
| 6 | 7,200 | | |

On EC 3.5.8 with October SoC `0x02009e0f`, module floors go well above the
ACPI ceiling of about 4320 RPM (level 100). With the daemon stopped,
`set-state 6` measured about 7560/7155 RPM. State 12 writes floor `0x34bc`
(13500) and was louder than ACPI level 100. The idle curve on that pair was
about 2700/4050 RPM, which is what `state=0/12 (no floor; EC curve)` means.
State 0 is not fans off. ACPI level 0 is fans off. Do not write ACPI level 0.

### EC 3.13.7 (unverified, and likely unused)

Both fans report 975–4350. That span matches `975 + 33.75 × level` for an ACPI
level of 0–100. It is not the 3.5.8 control encoding, so those 900-RPM steps
are not scaled onto it. States 1–12 divide the reported span:

`floor(state) = 2000 + (4350 - 2000) * (state - 1) / 11`

State 1 is 2000, not 975. ACPI level 20 on EC 3.5.8 was measured near 1620 RPM
with the second fan stalled, and level 30 near 1940
([forum topic 386805, post 21](https://forums.developer.nvidia.com/t/new-dgx-spark-fans-barely-speed-up-at-high-temps-and-cant-be-adjusted-critical-please-open-fan-control/386805)).
Nothing in states 1–12 is below 2000. State 12 is the reported maximum, 4350.

| State | Reported floor | State | Reported floor |
| ---: | ---: | ---: | ---: |
| 0 | Automatic / unset | 7 | 3,281 |
| 1 | 2,000 | 8 | 3,495 |
| 2 | 2,213 | 9 | 3,709 |
| 3 | 2,427 | 10 | 3,922 |
| 4 | 2,640 | 11 | 4,136 |
| 5 | 2,854 | 12 | 4,350 |
| 6 | 3,068 | | |

Public EC image diffs say this write is rejected because the override enable
bit can no longer be set. Probe does not try it. There is no cooling device
unless the module was loaded with `allow_unverified_writes=1`, which logs a
warning and is not qualified.
Do not treat this column as a measured RPM result. It is also unknown whether
a future firmware that accepted the write would interpret the numbers as tach
RPM or as the older PWM control encoding. ACPI level 0 on the 975–4350 scale
has stopped fans, and level 20 has stalled one of them, when used as a target
rather than as a floor. This table's low states have not been tried.

The same minimum is requested for both channels; each fan saturates at its
own maximum. Firmware can keep a fan above your chosen floor. Setting a lower
state does not force RPM below firmware demand. `set-state 0` and `automatic`
both remove the added floor when the EC still accepts floor writes.

## Read the fan RPMs

Find the hwmon device by name because numeric indexes change across boots:

```sh
for device in /sys/class/hwmon/hwmon*; do
  if [ "$(cat "$device/name" 2>/dev/null)" = dgx_ec_fan ]; then
    printf 'Fan 1: %s RPM\n' "$(cat "$device/fan1_input")"
    printf 'Fan 2: %s RPM\n' "$(cat "$device/fan2_input")"
  fi
done
```

`status` shows the requested state and hottest thermal zone; the hwmon files
show measured RPM. If a command fails, see [troubleshooting](troubleshooting.md).

## Watch for stopped fans

It watches every `fan*_input` under hwmon devices named `acpi_fan`, and
`thermal_zone0`. If fewer than two fan inputs are visible it does not trip.
On EC 3.5.8 with the October SoC, idle fans run near 2700/4050 RPM, so 0/0
is not a normal reading.

If every fan stays at 0 RPM for more than 15 seconds, at any temperature, it
logs one critical line and broadcasts it once with `wall`. That repeats at
most once a minute, the same interval as the daemon-down alert. It does
**not** power off for this alone. Fifteen seconds is several 2-second polls,
so one missed tachometer sample does not alert, and a stop at about 39°C is
still announced well before the chip would have to reach 70°C.
`--stall-seconds` changes that hold.

When that cold stop is confirmed and `dgx_ec_fan_control` is not loaded, it
tries once to write ACPI Fan level 100. It never writes 0 or any level below
40. If the module is loaded, it skips the write and logs why: ACPI
`cur_state` and the module share the EC lower-floor register.

If every fan stays at 0 RPM for more than 10 seconds while `thermal_zone0`
is at least 70°C, it logs at critical level and runs `systemctl poweroff`.
That hot hold is separate from the 15-second alert.

Install starts this as `dgx-fan-watchdog.service`, either with
`scripts/install-userland` or the manual commands in
[startup modes](installation.md#5-choose-your-startup-mode). That unit does not
depend on `dgx-fan-control.service` or on the kernel module, so stopping or
disabling the performance daemon leaves the watchdog running. `module enable`
and `module disable` do not touch it. Opt out at install time with
`sudo ./scripts/install-userland --no-watchdog`.

```sh
sudo dgx-fan-control watchdog
```

After a reboot, `systemctl is-active dgx-fan-watchdog.service` should print
`active`, and `journalctl -u dgx-fan-watchdog.service` should contain
`fan-stall watchdog watching`. If the performance service is enabled, stop
it without disabling it and confirm the watchdog journal shows the
daemon-not-active alert, then start it again:

```sh
sudo systemctl stop dgx-fan-control.service
sudo journalctl -u dgx-fan-watchdog.service -n 20 --no-pager
sudo systemctl start dgx-fan-control.service
```

Do not write ACPI `cur_state` 0, or any level below 40, to test the stall
path. `module status` prints `watchdog-active` and `watchdog-enabled`.

`--zero-seconds`, `--stall-seconds`, `--soc-celsius`, and `--action` change
the hot hold, the any-temperature alert hold, the temperature, and the
command. The action is one program plus arguments, not a shell pipeline. See
the [known issues](../README.md#known-issues) before enabling this on a
machine that uses external fans. The cold alert and the level-100 write have
not been run on hardware yet.

## Set the ACPI fan level

This does not use the kernel module. It writes every cooling device whose
type is `Fan`:

```sh
sudo dgx-fan-control acpi-max
sudo dgx-fan-control acpi-max --level 40
```

On success it prints one line per Fan device with the read-back level, for
example `cooling_device0 (Fan): 100/100`. On EC 3.13.7, or when the kernel
log does not name an EC, it also says firmware may accept the write without
changing RPM. On EC 3.5.8 the trailing line is `check fan RPM`. The command
exits nonzero when it finds no Fan cooling device, a write fails, the kernel
module is loaded, or the fan service is active.

The module and ACPI `cur_state` write the same EC lower floor. `acpi-max`
refuses in that case. `--force` overrides the refusal and logs a warning.
`--level` below 40 is refused and does not write. **Never write a level
below 30. Level 0 stops both fans and is not a recovery step.** Level 20 has
stalled one fan. Level 100 is about 4320 RPM, quieter than module state 12
on EC 3.5.8. The level resets on reboot. If a level below 100 fails partway,
or the command is interrupted after it has found the devices, it tries to
write 100 to each of them. It never writes 0 from that path.
