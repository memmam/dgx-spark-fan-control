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

## Hold a manual floor

Stopping the performance service and setting a floor is a lasting mode, not
only a pre-cool. With `dgx-fan-control.service` stopped or disabled,
`set-state N` records N. `N` is a kernel cooling state from 1 through 12.
State 0 is the NVIDIA curve and is not recorded. This does not write an ACPI
fan level.

```sh
sudo systemctl stop dgx-fan-control.service
sudo dgx-fan-control set-state 6
dgx-fan-control status
```

`status` prints `manual-floor: 6 (last-set)`. The record is
`/var/lib/dgx-fan-control/manual-state`. The same command writes
`/etc/modprobe.d/dgx-fan-control-owned.conf` so the next module load receives
`owned_state` and `owned_floor`. The kernel probe compares the EC floor with
that pair. A match is adopted and is not a foreign floor. A mismatch, or no
record, stays on the foreign-floor path. Install enables
`dgx-fan-manual.service`. That unit reapplies the state at boot and does not
start the performance curve. While that record, or a valid `MANUAL_STATE`,
is set, the performance daemon refuses to start even if the service is
enabled. It exits 79 and does not change the cooling state, so the floor
survives a reboot. systemd does not restart that exit.

`MANUAL_STATE=` in `/etc/default/dgx-fan-control` overrides the record when
you set it. Leave it empty to use the last `set-state`.

See the held value with `dgx-fan-control status` or `dgx-fan-control health`
(`mode: manual floor state N (held)` and `floor: ours (matches last-set N)`).
Clear the record with `sudo dgx-fan-control automatic`. If you also set
`MANUAL_STATE`, empty that line or the override remains.

A manual stop does not make the watchdog broadcast. A true zero is still both
ACPI fan tachometers at 0 RPM. Cooling state 0, with the fans spinning, is
not a stall.

## Pre-cool before a big job

### 1. Request maximum cooling

```sh
sudo systemctl stop dgx-fan-control.service
sudo dgx-fan-control set-state 12
dgx-fan-control status
```

`set-state 12` records that floor until `automatic` clears it. See [Hold a
manual floor](#hold-a-manual-floor). While it is held, the performance daemon
refuses to start, even if it is enabled, so the floor survives a reboot.
The watchdog does not broadcast for this stop.

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

Expect **`state=0/12 (daemon not setting a floor; EC's own curve)`**. That is NVIDIA's curve, not fans stopped. `automatic` writes state 0 and clears the persisted manual floor. It does not start the daemon. If `dgx-fan-control.service` is enabled, it starts again at the next boot. Start it now with `sudo systemctl start dgx-fan-control.service`. `module enable` does not start it.
On EC 3.13.7 telemetry-only there is no floor to clear in sysfs, so `automatic` is a no-op that still clears a stale last-set record and exits 0.
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
service stopped, state 0 is that curve. It does not stop the fans. [What that curve does under load](ec-curve.md).

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
boot. While a floor is held, `sudo systemctl start dgx-fan-control.service`
returns, then the daemon exits 79 and leaves the cooling state unchanged. Clear
the record with `sudo dgx-fan-control automatic` first. `automatic` does not
start the daemon. If the service is enabled, the next boot starts it; start
it now with `sudo systemctl start dgx-fan-control.service` after the record
is cleared. This is **our performance curve**, not NVIDIA's factory curve. While the
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
the startup check that fan control is unsupported. Exit 79 means a held floor
refused the start. Systemd restarts exit 1 every 30 seconds and does not
restart exit 69 or exit 79. See [troubleshooting](troubleshooting.md).

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
about 2700/4050 RPM, which is what `state=0/12 (daemon not setting a floor; EC's own curve)` means.
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
  [ "$(cat "$device/name" 2>/dev/null)" = dgx_ec_fan ] || continue
  for input in "$device"/fan*_input; do
    [ -e "$input" ] || continue
    echo "$device $(basename "$input" _input)=$(cat "$input")"
  done
done
```

That name is one device with `fan1_input` and `fan2_input`. It is not the
ACPI tachometers. Those are separate `acpi_fan` devices, one `fan1_input`
each on these Sparks. `status` shows the requested state and hottest thermal
zone; the hwmon files show measured RPM. If a command fails, see
[troubleshooting](troubleshooting.md).

## Watch for stopped fans

It watches every `fan*_input` under hwmon devices named `acpi_fan`, and
`thermal_zone0`. If fewer than two fan inputs are visible it does not trip.
On EC 3.5.8 with the October SoC, idle fans run near 2700/4050 RPM, so 0/0
is not a normal reading.

If every fan stays at 0 RPM for more than 15 seconds, at any temperature, it
logs a critical line, broadcasts it with `wall`, and tries one repair.
`--stall-seconds` changes that hold. Fifteen seconds is several 2-second
polls, so one missed tachometer sample does not alert.

The repair never writes ACPI level 0 or any level below 35. With
`dgx_ec_fan_control` unloaded, it writes ACPI Fan level 100. With the module
loaded, it requests that cooling device's maximum state and does not write
ACPI, because both paths use the same EC register.

About 60 seconds after the zeros began (`--reboot-seconds`), if no work is
in progress, it runs `systemctl reboot` (`--reboot-action`). At most one
reboot is requested per stall. A file under `/var/lib/dgx-fan-control`
records the boot id and the reason (`true-zero` or `foreign-floor`). If the
next boot is still at 0 RPM on every fan, it runs `systemctl poweroff`
(`--action`) instead of rebooting again. The marker is removed after the
fans have read nonzero for 30 seconds. A passing health check removes it too.

Work in progress defers that reboot. When `BUSY_CHECK` in
`/etc/default/dgx-fan-control` is unset, the signal is
`nvidia-smi --query-compute-apps=pid --format=csv,noheader`. A failed or
missing `nvidia-smi` counts as a job, so the machine is not rebooted on a
guess. A persistent model server always shows a compute process, so with
that default the reboot stays deferred for as long as that process is
present, and only the 85°C poweroff protects the machine. Set `BUSY_CHECK`
to a command whose exit 0 means a request is still in flight. A failure or
a timeout of that command counts as busy. Prefer a check of in-flight
requests over the compute-process list.

Deferral has a limit. `MAX_DEFER_SECONDS` defaults to 1800 (30 minutes).
The same number is `--max-defer-seconds`; a value in the defaults file
overrides the flag. When the limit runs out and the fans are still at 0
RPM, or the floor is still foreign, the watchdog broadcasts a critical
alert and keeps deferring. It does not reboot through work.

If the hottest thermal zone reaches `--zero-fan-celsius` while **both**
fans read 0 RPM, it powers off immediately. That does not wait for
`--reboot-seconds`, and it does not query the GPU first. The journal line
is `fan stall: hottest zone is 85.3C; running systemctl poweroff (not sampled now; last recorded work state at 2026-11-06 09:41:12 UTC: GPU job running, progress moving via PROGRESS_CHECK)`,
using the previous job-watch sample and its UTC time. If that sample is
missing or unreadable, the parenthesis is
`(not sampled now; no earlier work-state record)`.
The default is **85°C**. `--soc-celsius` is a deprecated alias for that
true-zero cutoff. A stall itself does not require a high temperature:
both tachometers at 0 RPM are enough. Spinning fans are not powered off
for temperature.

85°C is not a general limit. A published 4-node DGX Spark setup
([tenhkspark/dgx-spark-tuning](https://github.com/tenhkspark/dgx-spark-tuning))
reports GPU temperatures of about 72–77°C and a **system** temperature of
about **84°C** during a 1M-token prefill, with the fans healthy and 0 ms
of throttling. That figure is user-reported, not a specification. A
healthy machine under heavy load can sit next to 85°C while the fans are
spinning, so that cutoff must not power the machine off in that state.

The kernel's critical trip is about 104°C (104.8 in
[tonyd2wild/DGX-Spark-Hard-Poweroff-Fix](https://github.com/tonyd2wild/DGX-Spark-Hard-Poweroff-Fix)).
That repository reports a hotspot spiking from about 90°C to about 98°C in
about 2 seconds, roughly 4°C/s, before a hard power cut. At that rate the
hotspot passes the 104.8°C critical trip before the next reading can be
logged, so the cut the repository calls faster than Linux can log is
consistent with the ordinary critical trip. The repository's other
suggestion, a thermal/over-current cut, is the alternative. No sourced EC
temperature cutoff exists. 98°C there is the last reading that was logged,
not a limit. Userspace polling cannot beat a rise of about 4°C/s to that
trip: a fixed threshold near the spike would miss it and only look like
protection. The defenses that work are keeping the fans running, which is
what the true-zero path is for, and avoiding the spike in the first place
with a fan floor or pre-cool. If the hottest zone rises about 4°C/s between
polls, the watchdog writes one observational journal line and takes no
action.

## Hot under load

A hot episode starts when the fans are spinning and the hottest zone is at
or above the true-zero cutoff (85°C by default). The watchdog keeps polling.
The episode ends when the zone falls more than 2°C below that cutoff, or
when the job ends. This path never kills a job, reboots, powers off, or
writes `needs-intervention`. A true zero, both fans at 0 RPM, is not a hot
episode; that path is the stall handling above.

Progress is sampled whenever work is busy, including before the machine is
hot, so a stall that started earlier still counts when the zone crosses the
cutoff. The stuck warning itself is raised only during a hot episode.

`PROGRESS_CHECK` in `/etc/default/dgx-fan-control` is the primary signal. It
is a command with the same 5 second timeout as `BUSY_CHECK`. It must print
one monotonic number. A total of 0 is still that number; only a larger
number than the last sample is progress. A failure, a timeout, or output
that is not a number is logged once, and the
watchdog falls back to the built-in heuristic until the hook works again.
Empty means the built-in heuristic. This example reads vLLM's Prometheus
counters and is not enabled by default:

```
PROGRESS_CHECK=curl -fsS http://127.0.0.1:8000/metrics | awk '/^vllm:(generation|prompt)_tokens_total[{ ]/{s+=$NF} END{print s+0}'
```

The built-in heuristic is weaker. With a compute process still present, the
job looks stuck when GPU utilization stays at or below 5% for
`STUCK_SECONDS`, or when those processes' cumulative CPU time in
`/proc/<pid>/stat` does not advance for that long. A long prefill can
report a low utilization and look idle. A hang that still burns CPU or GPU
time can look alive.

`STUCK_SECONDS` defaults to 600. `--stuck-seconds` is the same number; a
value in the defaults file overrides the flag. When that interval passes
with no progress during a hot episode, the watchdog writes one critical
journal line and broadcasts a wall alert, then repeats the wall at the
usual `--alert-repeat-seconds` interval. The warning clears when progress
resumes or the job ends. When a job that was hot finishes, the journal
records one line: `job finished`, how long it was hot, and the peak
temperature.

`dgx-fan-control health` reads `/var/lib/dgx-fan-control/job-watch` and
prints the progress source in use: `progress: hook`, `progress: built-in`,
or `progress: off`. A stuck job is
`job: warn (job may be stuck: no progress Ns, via hook)` (or `built-in`).
That line does not fail health and does not set needs-intervention.

A `wall` broadcast for a daemon that is enabled but not running is sent when
that state changes, and again at most every 30 minutes
(`--alert-repeat-seconds`) while it lasts. The journal records the condition
on every poll.

Exit 78, a foreign floor, uses that same reboot path without the repair.
The fans are already held high. One broadcast says a reboot clears the ACPI
floor, then the journal repeats it. After `--reboot-seconds`, with no work
in progress, it reboots once. If the next boot still has that floor, it
writes `/var/lib/dgx-fan-control/needs-intervention` and does not reboot
again and does not power off. A foreign floor holds the fans high, so
temperature does not power the machine off on that path. The true-zero
poweroff stays armed whether or not the health check has failed.

Install starts this as `dgx-fan-watchdog.service`, with
`scripts/install-userland` in
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
daemon-not-active alert, then start it again. While a floor is held,
the daemon exits 79 right after `start`, the cooling state stays, and the
watchdog does not alert for that exit.

```sh
sudo systemctl stop dgx-fan-control.service
sudo journalctl -u dgx-fan-watchdog.service -n 20 --no-pager
sudo systemctl start dgx-fan-control.service
```

Do not write ACPI `cur_state` 0, or any level below 35, to test the stall
path. `module status` prints `watchdog-active` and `watchdog-enabled`.

`--stall-seconds`, `--reboot-seconds`, `--zero-fan-celsius`, `--action`,
`--reboot-action`, `--alert-repeat-seconds`, `--max-defer-seconds`, and
`--stuck-seconds` change the alert hold, the reboot hold, the hottest-zone
true-zero poweroff temperature, the poweroff command, the reboot command,
how often a standing alert is broadcast again, how long a reboot may be
deferred, and how long a hot job may show no progress before a warning.
`--stuck-seconds` does not stop the job. Each watchdog command is one
program plus arguments, not a shell pipeline.
`BUSY_CHECK`, `READY_CHECK`, `PROGRESS_CHECK`, and `DGX_FAN_NOTIFY` are
shell commands from
the root-owned defaults file; an `http://` or `https://` value for
`READY_CHECK` is a GET instead. See the [known
issues](../README.md#known-issues) before enabling this on a machine that
uses external fans. The cold alert and the level-100 write have not been
run on hardware yet.

## Check health after boot

`dgx-fan-health.service` is a oneshot that starts after `multi-user.target`.
It does not depend on the watchdog. The watchdog keeps its own safety loop
from early boot. The health check is the "login and SSH are up" result.

Run `sudo dgx-fan-control health`. It also runs without root, but then some
checks read `unknown (needs root)`. It does one pass and prints
each check as it finishes, with `pass`, `fail (reason)`, or
`unknown (needs root)`. A check that cannot be read is `unknown (needs root)`,
not an error. The command exits nonzero when any check fails or is
`unknown (needs root)`. An `ec-updates: unknown` line does not.
It also prints a needs-intervention flag or health file it can read.
`systemctl`, `READY_CHECK`, `PROGRESS_CHECK`, `nvidia-smi`, `fwupdmgr`,
`fwupdtool`, and the kernel log read each time out after 5 seconds.

The core checks are fan and thermal only:

- `dgx_ec_fan_control` is loaded
- `dgx_ec_fan_floor` is present and the probe did not refuse a foreign floor
- `dgx-fan-control.service` is active when it is enabled, and it did not
  exit 78. Exit 79 is a note, `stopped (held floor N; automatic clears it)`,
  not a failure and not needs-intervention. every start exits 79 while that
  floor is held
- both ACPI fans read nonzero RPM. The line lists every `fan*_input`
  under every hwmon device named `acpi_fan`, plus each ACPI `Fan`
  `cur_state`. One device with two inputs prints `fan1=… fan2=… RPM`.
  These Sparks have one `fan1_input` on each of two `acpi_fan` devices, so
  the line is `hwmon0:fan1=… hwmon1:fan1=… RPM`. Fewer than two readings,
  or any 0, fails. Health alone rereads a 0 or a missing input up to three
  times, one second apart, before that failure. A value that returns is a
  pass and the line names it, for example
  `hwmon1:fan1 read 0 RPM once, recovered to 2148 RPM on recheck (transient)`.
  A miss that is still missing is not confirmed, and still fails. The
  watchdog does not do this recheck. That tachometer is not the
  `dgx_ec_fan` mailbox telemetry. On EC 3.13.7 a `cur_state` write does not
  change RPM, so a nonzero level next to a 0 RPM reading is still a
  tachometer failure
- the hottest zone is readable. At or above 85°C with the fans spinning
  the line is `warn (hot under load)`, which does not fail the check and
  does not set needs-intervention
- `progress:` is the watchdog's last sample, with the reason and the
  sample's absolute UTC time when one was stored
  (`; recorded 2026-11-06 09:41:12 UTC`):
  `off (no job running)`, `off (BUSY_CHECK failed)`,
  `built-in (GPU utilization)`, `built-in (BUSY_CHECK)`,
  `built-in (PROGRESS_CHECK failed)`, `hook (PROGRESS_CHECK)`, or
  `off (no watchdog sample)`.
  `unknown (could not sample GPU: nvidia-smi exited N; transient after a restart is normal)`
  means nvidia-smi still failed after a short retry. It does not fail health
  and does not set needs-intervention. An older file that still says
  `nvidia-smi failed` is shown with that same sentence, not the old words.
  The watchdog still treats that sample as a running job and defers a reboot.
  The next successful sample replaces the line. A file written before the
  current watchdog start is
  `waiting for first watchdog sample (previous sample at YYYY-MM-DD HH:MM:SS UTC: ...)`.
  That clock is the sample's stored `recorded=` time, the same absolute UTC
  form as `; recorded`. It is not the file's modification time. A file with
  no stored stamp says `time unknown`.
  The first GPU sample waits 5 seconds after the watchdog starts, so a
  restart does not record a miss while NVML is still settling. Health does not
  run `nvidia-smi` or `PROGRESS_CHECK` itself, so a normal user and root
  print the same line from that file. A failed probe is not called GPU
  utilization. No sample yet is `off (no watchdog sample)`.
  An unreadable sample is `unknown (needs root)`. A stuck job adds
  `job: warn (job may be stuck: ...)`, which does not fail the check and
  does not set needs-intervention
- `dgx-fan-watchdog.service` is active
- `ec-updates:` is `pass (blocked)`, `warn (not blocked; EC 3.5.8 can be
  replaced by 3.13.7)`, or `unknown` when fwupd is missing or slow. Without
  root it is `unknown (needs root to confirm the block)` when the EC is
  hidden; run it with `sudo` to see `pass (blocked)`. The warn and the
  unknown do not fail the check and do not set needs-intervention.
  `status` does not query fwupd
- EC 3.13.7 telemetry-only is not a failure. `firmware:` says control is
  unsupported, and the cooling device, mode, and ready lines are `n/a`.
  A `clock-cap:` note follows, in `health` and `status`, suggesting
  `sudo nvidia-smi -lgc 0,2200` (the default in MiaAI-Lab's sparkLimiter).
  It is advice only and nothing applies it; see [the EC curve](ec-curve.md).
  A service left enabled reports
  `daemon: n/a (unsupported firmware; service enabled but idle)`. The
  watchdog does not broadcast for exit 69 or for this firmware. A last-set
  floor is kept. `automatic` still clears that record without writing
  sysfs; `set-state` and other control writes still error. This does not
  set needs-intervention

`READY_CHECK` runs in the same pass. Empty means not configured, and the
fan checks alone decide. A command succeeds on exit 0. A URL succeeds on
an HTTP status from 200 through 299. Failure is the reason `workload not
ready`.

The boot unit is different. `dgx-fan-health.service` runs
`dgx-fan-control health --record --settle 45` as root. `--settle 45` retries
for 45 seconds so a slow module load can still pass, and each failed attempt
is one journal line naming the failing checks. `--record` writes
`/var/lib/dgx-fan-control/health`. On a pass it clears the
needs-intervention flag, the stall marker, and the watchdog-reboot marker.
If this boot followed a watchdog reboot that has not been announced yet, it
also logs and broadcasts `recovered after watchdog reboot` once. On failure
it writes the needs-intervention flag. It does not reboot. Only `--record`
writes; `sudo dgx-fan-control health` without it prints `recorded: no`.
`--record` without root prints that it requires root and still prints the
checks. It does not write.

The checks above are this run. The stored lines underneath are the last
recorded file, which a plain `health` does not rewrite. `recorded: no` means
it did not. `stored health:` is one line: `result=`, `ready=`, `time=` in
this machine's local zone, and `reason=` when the stored result is
needs-intervention. `ready=` is the same token the live `ready:` line shows,
including `n/a` on EC 3.13.7 telemetry-only. These Sparks are set to UTC, so
a stamp looks like `time=2026-11-06 07:25:45 UTC`.
`sudo timedatectl set-timezone America/Los_Angeles` is optional and only
changes the display. A time that is not a number is printed unchanged.

A stored needs-intervention whose only reason is unsupported firmware is
removed on read when the process can unlink it. `stored health:` is then
`none`, and so is `stored needs-intervention:`. If the file is root-owned
and this user cannot unlink it, the bytes stay and the line is
`none (dismissed: unsupported firmware is a deliberate state; run sudo
dgx-fan-control health --record to remove the file)`. That is not a live
fault. `sudo dgx-fan-control health --record` on a healthy run replaces the
health file and removes the flag.

The record is marked `stale (...)` for any of these that apply:

- `previous boot`, when `boot_id` differs
- `firmware changed`, when the stored firmware and the current one are both known and differ
- `firmware not recorded`, when the file has no `firmware=` field
- `older record format`, when `version=` is missing or is not `2`
- `written by a different install`, when `version=` is current but `code=`
  is missing or is not this script. `code=` is the first 12 hex characters
  of the SHA-256 of the installed program. A new build marks older records
  stale without a format bump. Reinstalling the same bytes does not.

When this run is healthy and the stored result is still needs-intervention,
the next line is `stored health note:`. A stale record says it was
superseded by the current healthy run. A record from this same boot and
this same install is not stale: the `reason=` stays on the health line, and
the note says an earlier run this boot failed while this run is healthy.
The note names `sudo dgx-fan-control health --record` and what that command
clears, including the one recovery broadcast above. A missing file prints
`stored health: none`. A file this user cannot read prints
`stored health: unknown (needs root)`.

`status` prints `NEEDS INTERVENTION:` while the flag is readable, and
`unknown (needs root)` for a sysfs value it cannot read.
`dgx-fan-control health --clear` removes the flag and requires root.
A later recorded pass removes it too.
`/etc/update-motd.d/95-dgx-fan-control` prints that flag's reason on a later
SSH login. It reads `/var/lib/dgx-fan-control/needs-intervention` only, not
the health file, so `code=` and the stored-health note do not appear there.

`DGX_FAN_NOTIFY` is empty by default. When a recorded check has it set, the
summary is written to that command's standard input on failure and on recovery.

## After a watchdog reboot

The watchdog reboots only the local node. A few things that were running
before that reboot do not come back by themselves:

- A user-level systemd service does not start at boot unless that user has
  a lingering session: `loginctl enable-linger <user>`.
- An `nvidia-smi -lgc` clock lock does not persist across a reboot. A
  oneshot unit can apply it again if you need the lock.
- A multi-node workload can lose the pair when one node reboots. The fan
  watchdog still reboots only the machine whose fans stopped or whose floor
  is foreign.

## Set the ACPI fan level

This does not use the kernel module. It writes every cooling device whose
type is `Fan`:

```sh
sudo dgx-fan-control acpi-max
sudo dgx-fan-control acpi-max --level 40
```

On success it prints one line per Fan device with the read-back level, for
example `cooling_device0 (Fan): 100/100`. On EC 3.5.8 the trailing line is
`check fan RPM`. On EC 3.13.7 telemetry-only, `acpi-max` writes nothing,
logs that the EC runs its own curve, and exits 0. The command exits nonzero
when it finds no Fan cooling device, a write fails, the kernel module is
loaded, or the fan service is active.

The module and ACPI `cur_state` write the same EC lower floor. `acpi-max`
refuses in that case. `--force` overrides the refusal and logs a warning.
`--level` below 35 is refused and does not write. **Never write a level
below 35. Level 0 stops both fans and is not a recovery step.** Level 20 has
stalled one fan. Level 100 is about 4320 RPM, quieter than module state 12
on EC 3.5.8. The level resets on reboot. If a level below 100 fails partway,
or the command is interrupted after it has found the devices, it tries to
write 100 to each of them. It never writes 0 from that path.
