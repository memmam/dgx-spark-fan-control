# Troubleshooting

Start with read-only observations:

```sh
uname -r
modinfo dgx_ec_fan_control
dgx-fan-control status
systemctl status dgx-fan-control.service
sudo journalctl -k -b | grep -E 'dgx|fan.floor|arm.ffa'
sudo journalctl -u dgx-fan-control.service -b -n 60
```

## Module rejected or cooling device absent

Check that the module was compiled for the running kernel (`modinfo` reports
`vermagic`), and that its signing certificate is enrolled. `Key was rejected
by service` usually requires checking signing/enrollment. `Invalid module
format` requires inspecting the kernel log and rebuilding for the exact kernel.

The module can load while its device probe rejects an unsupported transport or
capability response. Read the kernel log; do not remove validation checks.
This implementation matches the NVIDIA P4242 platform only. Other GB10 boards
have not been qualified.

Version 0.1.6 accepts exactly two capability replies: EC 3.5.8
(1260–9000 / 1890–13500) and EC 3.13.7 (975–4350 / 975–4350), both revision 1
and unit mode 0. Anything else is still `-EBADMSG` (`-74`), including a
one-count difference. The kernel log prints the unrecognized parsed ranges.
The raw bytes are available with dynamic debug:

```sh
echo 'file dgx_ec_fan_control.c +p' | sudo tee /sys/kernel/debug/dynamic_debug/control
```

## EC 3.13.7: control unsupported

On EC 3.13.7 the override handlers are still present, but they return
unsupported (`-5`) or timeout (`-116`) while their enable bit is clear. Host
command `0xEB` now refuses writes to the fan/thermal block, so that bit cannot
be set from the host. Expect floor writes to fail. Probe does not send one. After the capability
reply matches, it reads telemetry and binds **hwmon only**. There is no
`dgx_ec_fan_floor` cooling device, and the daemon must not keep retrying
writes. `allow_unverified_writes=1` at `insmod` or `modprobe` opts into floor
commands and logs a warning. The parameter defaults off and cannot be turned
on after the module is loaded.

```sh
dgx-fan-control status
# control unsupported on this firmware
```

That line is printed once. It is not also written to the log.
`dgx-fan-control health` does not fail this state and does not set
needs-intervention. The cooling device, the daemon, and the mode are
`n/a`: telemetry is up and floor control is not. A service left enabled
from an earlier firmware reports
`n/a (unsupported firmware; service enabled but idle)`, not failed, and
the watchdog does not broadcast. `module enable` still leaves the service
disabled unless `--force-service` or `--start-service` is set. A last-set manual floor
is kept for a later downgrade to EC 3.5.8. Fans, temperature, and the
watchdog are still checked. Health fails only when a firmware that should
accept floor control has no cooling device, or when telemetry is missing.

`fan1_input` and `fan2_input` on the single `dgx_ec_fan` hwmon device should
still be readable. ACPI tachometers are separate `acpi_fan` devices, one
`fan1_input` each. Do not register a second fan driver or send raw mailbox
commands to work around the rejection. The ACPI `Fan` cooling device does not change
fan speed on this EC either.

## Stored health still says needs-intervention

`health: healthy` and a stored `needs-intervention` can both be true. The
checks are this run. `stored health:` is the last file under
`/var/lib/dgx-fan-control/health`, and a plain `health` does not rewrite it.
`recorded: no` means it did not. EC 3.13.7 telemetry-only stays a pass. That
state is not written as needs-intervention. A record that already says
`reason=unsupported firmware; telemetry only` is removed on read when this
user can unlink it. Otherwise the line is
`stored health: none (dismissed: ...)`, not a live needs-intervention, and
the file is left for `sudo dgx-fan-control health --record` to replace.
The stored `ready=` matches the live `ready:` line, so telemetry-only
records `ready=n/a`.

A single `acpi_fan` reading of 0 RPM, or a missing `fan1_input`, is reread
by `health` up to three times, one second apart. The check fails only when
that 0 or miss is still there. A recovery is a pass and names the transient,
for example `hwmon1:fan1 read 0 RPM once, recovered to 2148 RPM on recheck (transient)`.
The watchdog does not recheck; a true zero there is still immediate.

`progress:` repeats the watchdog sample and names the reason, for example
`off (no job running; recorded 2026-11-06 09:41:12 UTC)` or
`built-in (GPU utilization; recorded 2026-11-06 09:41:12 UTC)`. The clock
is absolute UTC. Root and a normal user read that same file. `unknown (could not sample GPU: nvidia-smi exited N; transient after a restart is normal)`
means a short retry still could not run nvidia-smi. It is not a fault:
health stays healthy, and the next good sample replaces it. A file left by
an older watchdog that says `nvidia-smi failed` is printed with that same
sentence. If the file is older than the current watchdog start, the line is
`waiting for first watchdog sample` and names that previous sample with the
UTC time stored in the file (`recorded=`), not the file's modification time.
A file with no stored stamp says `time unknown`. The watchdog waits 5 seconds before the first GPU sample so the
restart itself is not recorded as a miss. The watchdog
still defers a reboot on a real sampling gap. A true-zero poweroff at 85°C logs
the hottest zone and the last job-watch sample with a UTC timestamp, or
`(not sampled now; no earlier work-state record)` when that file cannot
be read. It does not sample the GPU before powering off. The stuck-job
line is still only a warning.

The time is this Spark's local time, with the zone abbreviation. These
machines are set to UTC, so expect `time=2026-11-06 07:25:45 UTC`.
`sudo timedatectl set-timezone America/Los_Angeles` is optional. It changes
only how the stamp is shown. A value that is not a number is printed as
stored.

`stale (...)` names every reason that applies:

- `previous boot`
- `firmware changed`, when both firmwares are known and they differ
- `firmware not recorded`, which is a file from before that field existed
- `older record format`, when `version=` is missing or is not `2`
- `written by a different install`, when the format is current but `code=`
  does not match this program. `code=` is 12 hex characters of the script's
  SHA-256. Installing a different build does this without a version edit.
  Installing the same bytes does not.

A separate `stored health note:` follows when this run is healthy and the
stored result is needs-intervention. A stale record says it was superseded
by the current healthy run. A record from this same boot and this same
install is not stale: `stored health:` keeps `reason=`, and the note says
an earlier run this boot failed. The note tells you that
`sudo dgx-fan-control health --record` saves this result and, on a healthy
run, clears the needs-intervention flag, the stall marker, and the
watchdog-reboot marker. If this boot follows a watchdog reboot that has not
been announced, that command also logs and broadcasts
`recovered after watchdog reboot` once. A missing file prints
`stored health: none`. A file you cannot read prints
`stored health: unknown (needs root)`.

The motd snippet and `status` read the needs-intervention flag, not this
file. The watchdog does not read it either.

## Block or allow EC firmware updates

fwupd can install EC 3.13.7 over 3.5.8 and remove fan-floor control. The
steps below match
[EC firmware pairing and downgrade](installation.md#ec-firmware-pairing-and-downgrade).

The EC is a **UEFI Device Firmware** entry. It is not labeled Embedded
Controller in `get-devices`. The same name is used for the SoC
(`b488217b-3895-4fc0-b1bf-ab7005a2d45a`, version `0x02009e0f`) and another
device (`dd1a238a-5f8e-46bd-9401-a88da99c5a96`, version `0x00000519`). Do
not flash those when you mean the EC.

The fwupd ESRT device ID comes from the GUID and the physical path, so
identical boards share it. On DGX Spark the EC device ID is
`8c948e1db381648c8893897e4d09b7b153309991`. Before any flash, confirm GUID
`3d13c989-e6a8-4ead-95ee-921f09868f65` and a version of `0x03xxxxxx`
(`0x03000508` is 3.5.8, `0x03000d07` is 3.13.7).

A blocked EC is absent from `fwupdmgr get-devices`,
`fwupdmgr get-devices --show-all`, and `fwupdmgr get-updates`. On fwupd
2.0.20 the block is not in `/etc/fwupd/fwupd.conf`. That file can contain
only `[fwupd]` and `EspLocation=/boot/efi`. These searches can find nothing:

```sh
grep -ri DisabledDevices /etc/fwupd /var/lib/fwupd
sudo grep -rIl 3d13c989 /etc /var/lib/fwupd /usr/share/fwupd
```

The line that shows the block:

```sh
sudo fwupdtool get-devices --show-all --verbose 2>&1 | grep -i disabled
```

Expect `is disabled [3d13c989-e6a8-4ead-95ee-921f09868f65]`.
`dgx-fan-control health` prints `ec-updates: pass (blocked)`,
`ec-updates: warn (not blocked; EC 3.5.8 can be replaced by 3.13.7)`, or
`ec-updates: unknown (...)`. That line does not fail health and does not
set needs-intervention.

Block the EC. This replaces the whole `DisabledDevices` list. To keep
another device disabled, include every GUID in this one value, separated
by commas:

```sh
sudo fwupdmgr modify-config fwupd DisabledDevices 3d13c989-e6a8-4ead-95ee-921f09868f65
sudo systemctl restart fwupd
```

Unblock. An empty value clears every `DisabledDevices` entry:

```sh
sudo fwupdmgr modify-config fwupd DisabledDevices ""
sudo systemctl restart fwupd
```

Upgrade or downgrade only after the GUID and `0x03xxxxxx` version match.
Decline the report-upload prompt. Let the machine reboot. Do not cut power.

```sh
sudo fwupdmgr get-releases 8c948e1db381648c8893897e4d09b7b153309991
sudo fwupdmgr update 8c948e1db381648c8893897e4d09b7b153309991
sudo fwupdmgr downgrade 8c948e1db381648c8893897e4d09b7b153309991
```

`update` moves toward 3.13.7 and removes fan-floor control. `downgrade`
moves toward 3.5.8. After that downgrade, set `DisabledDevices` to the EC
GUID again. There is no helper command: this fwupd version does not show
the current list, so a helper would overwrite entries it cannot read.

## Mailbox timeout and the ACPI fan devices

A failed **first** query is a different failure from a later wedge. EC 3.5.8
paired with October SoC firmware **2.158.15** (`0x02009e0f`) has failed the
FF-A/eSPI mailbox on the first read-only query (eSPI status `0x05`, about 54
ms) on some boots. The same pair has also completed probe on both DGX Sparks
(kernel `7.0.0-1024-nvidia`, module 0.1.5): `pinned EC 3.5.8 capabilities
fan0=1260..9000 fan1=1890..13500`, baseline about 2700/4050 RPM, and a cooling
device. This driver returns `-EIO` for submit status `0x05` and `-ETIMEDOUT`
when a poll stays pending. Either one, before a capability profile matches,
refuses the device. The kernel log says the mailbox did not complete and
names the ACPI fallback. It does not claim that October SoC firmware can
never bind.

When the mailbox does not complete, the module does not bind, including no
hwmon device. The fallback is the ACPI fan device, with this module unloaded:

```sh
for device in /sys/class/thermal/cooling_device*; do
  printf '%s type=%s max=%s cur=%s\n' \
    "$device" "$(cat "$device/type")" \
    "$(cat "$device/max_state")" "$(cat "$device/cur_state")"
done
```

Look for type `Fan` and `max_state` 100. The BIOS maps a level to
`975 + 33.75 * level` RPM. Observers measured about 4320 RPM at level 100,
about 1940 at level 30, about 1620 at level 20 with the second fan stalling,
and both fans stopped at level 0. **Never write a level below 30. Level 0
stops both fans immediately and they stay at 0 RPM; the EC did not spin them
back up at 39°C, and the write did not clear a foreign floor.** `acpi-max`
refuses levels below 40. It also refuses while this module is loaded or the
fan service is active. On EC 3.13.7 the same write is accepted and ignored.
An ACPI level resets on reboot: both Sparks returned to about 2700/4050 RPM
without another `acpi-max`.

An older experimental driver may already own the relay. Do not load multiple
fan drivers or run a direct packet client alongside the controller. Restore
automatic control with the existing owner before replacing it.

## EIO, busy, or refused floor changes

The original research found boots where both the stock ACPI time-alarm read and
the fan packet service failed. Secure-partition status `0x05` indicated failure
of an eSPI mailbox-status read, while `0x0a` indicated mailbox busy. These are
different failures. A cold power cycle recovered the transport in the recorded
cases; an ordinary warm reboot was not a deterministic trigger or cure.

There is no demonstrated safe userland command to reinitialize that secure
transport. If an orderly automatic reset fails, retain the logs and arrange a
maintenance shutdown and cold power cycle. Do not repeatedly load competing
probes or access controller MMIO: even a normal-world read of the secure-owned
eSPI controller caused a watchdog reboot during the original investigation.

An ownership mismatch (`ESTALE`) causes failure: the driver
will not overwrite an unknown existing floor. ACPI `cur_state` writes that
same register, so `acpi-max` while the module is loaded can move the floor
(level 100 was observed as `0x10fe`, 4350 RPM, over a module state-12 floor
of `0x34bc`). `status` then says the floor was changed outside this driver,
with the observed and expected RPM when the kernel log still has them.
Probe logs `refusing to replace existing lower floor` with the RPM and
returns `-EBUSY`. It does not clear the floor. Do not write ACPI level 0 to
recover; that stops both fans and leaves the floor in place. Unload the
module before any ACPI write. A cold power cycle is the recovery that does
not write a fan level. Avoid concurrent writers. Version
0.1.5 uses `ESTALE` only for that mismatch. A reply-guard refusal, where
recovery will not trust the shared response, returns `EPROTO`. The daemon
retries `EPROTO` after 31 seconds, past the driver's 30-second cooldown, at
most three times. Idle-guard refusal stays `EAGAIN` and keeps the two-second
then four-second retry. `ESTALE` still stops immediately. Version
0.1.1 remembers its own attempted write and can reconcile it after transport
recovery, even when acknowledgement or readback failed. Version 0.1.0 could lose
ownership in this situation. Neither version can restore through a relay that
never completes its outstanding transaction.

## Persistent pending and service exit 69

Version 0.1.3 emits a numbered incident with recent transaction history,
submit/poll timing, mailbox/response evidence, and the recovery outcome.
See [incident logging](incident-logging.md) for fields, validity masks, and
journal collection commands. Preserve these logs before restarting or reloading.

A separate failure was observed on two systems running version 0.1.0: the stock
ACPI EC time read still worked, while successful FF-A poll calls continuously
returned packet state `2` (pending). No new fan request was being submitted.
This differs from a failed FF-A call or submission status `0x0a` (mailbox busy).
The live trigger remains unconfirmed. Further offline analysis reproduced a
firmware ordering defect that can leave the pending flag set **after the EC
has completed the request**. See the [firmware analysis](firmware-pending-analysis.md)
for that distinction, other failure paths, and the subsequent live recovery.

Versions 0.1.1 and later log preflight timeouts separately from submission failures. The
daemon retries transient transport errors after two and four seconds; after
three consecutive failures it attempts automatic restoration and exits 1.
Non-retryable errors, including an ownership mismatch, stop immediately. Both
the initiating error and any restoration failure remain in the journal. A
failed restoration means the current floor is **unverified**, not automatic.

Version **0.1.2** first attempts [bounded driver recovery](protocol.md#bounded-stale-pending-recovery-012)
when preflight or completion remains pending. A verified idle mailbox permits
one read-only resynchronization; successful recovery lets the current operation
continue without a daemon restart. The kernel logs `recovered idle-mailbox
pending` with the verified floor and count. It never repeats a timed-out setter.
Recovery attempts are limited to one per 30 seconds across all sysfs and
lifecycle callers. The daemon's retry/exit behavior above remains in effect
when recovery fails; a cooldown is not an automatic promise to retry forever.

Missing or bound OEM service, busy/changing mailbox status, invalid clock or
response bytes, another timeout, and inconsistent floor ownership all refuse
recovery. Retain the logs when this occurs. Do not repeatedly restart the
service to work around the cooldown or discard ownership by reloading.

The service uses `Restart=always` with a 30-second delay and
`StartLimitIntervalSec=0`, so a crash is retried forever. Exit 69 is reserved
for the daemon's startup check that fan control is unsupported. Exit 78 is
reserved for a foreign lower floor. `RestartPreventExitStatus=69 78` stops
only those two exits from looping. A transport failure or crash does not use
either code, so it still restarts. Exit 69 used to
cover those runtime failures too, which left a stopped daemon holding a low
floor while temperatures stayed under the alarms (observed for 1 day 19 hours).
One later wedge is still open. On EC 3.5.8 with the October SoC, the daemon
read the floor about 99 times, then near 188 seconds of uptime operation
`0x4` stuck pending (state `0x2`, 100 polls, `-110`). Every later preflight
timed out until reboot. It has not been reproduced on demand. It may have
coincided with NVIDIA Sync's Resource Monitor and the DGX Dashboard
telemetry freezing. Incident lines now include `realtime=` (`CLOCK_REALTIME`)
and `uptime=` (`CLOCK_BOOTTIME`) so the next one can be compared with those
tools. The driver still does not clear a foreign floor and does not treat
this wedge as solved.

## Daemon restarts forever after an ACPI floor

On one Spark the performance service restarted 196 times, every 30 seconds,
logging `no dgx_ec_fan_floor cooling device: probe did not register one`.
`status` on that machine said the probe had refused a foreign lower floor,
left by an earlier ACPI `cur_state` write. Starting the service again cannot
clear that floor. The daemon now exits 78 and systemd does not restart it.
The journal line says a reboot clears the ACPI floor. Do not toggle the
service, and do not write ACPI level 0. The fans are held high, so this is
not a true-zero stall and the watchdog does not run the repair. It does
reboot once, after the same hold and the same deferral as a true zero. If
the next boot still has the foreign floor, it writes
`/var/lib/dgx-fan-control/needs-intervention` and does not reboot again and
does not power off. `dgx-fan-control health` reports the same failure
without root, in one pass, and does not write anything. The boot unit
retries for 45 seconds and records the flag. The motd snippet shows it on
a later login. `sudo dgx-fan-control health --clear` removes the flag after
you have dealt with it. A later recorded pass removes it as well.

`dgx-fan-control watchdog` does not alarm when the last exit is 69. Exit 78
is one broadcast and then journal lines, and then that one reboot. It still
logs on every poll, and broadcasts when the state changes or every 30
minutes, when the unit is `failed` or enabled but inactive for another
reason, before it applies the temperature rule. SIGTERM lets the daemon
perform its own cleanup, without a competing `ExecStop` writer.

On 2026-09-07, both machines had an **idle physical mailbox with a stale pending
flag**. A guarded, one-shot read-lower-floor retry recovered communication on
each. Their old drivers also remembered a different floor from the EC's actual
floor, so recovery required separately identifying and removing that floor,
verifying automatic control, and upgrading the driver. Neither machine rebooted.

Retain the kernel and service logs. The [operator diagnostic](../research/mailbox/README.md)
documents the version-pinned procedure and refusal conditions. It is research
tooling for the pinned older modules; 0.1.2 incorporates the guarded read path
directly in its driver and does not use the helper's private-layout access.
A busy or unreadable mailbox
is outside the demonstrated recovery. Do not unload/reload to discard ownership
state, remove the driver's pending check, or submit raw reset packets. After
communication and ownership are reconciled and status verifies state 0, restart explicitly:

```sh
dgx-fan-control status                    # require state=0/12 (no floor; EC curve)
sudo systemctl reset-failed dgx-fan-control.service
sudo systemctl start dgx-fan-control.service
sudo journalctl -u dgx-fan-control.service -n 30
```

A successful restart must be followed by state, RPM, and temperature checks
under load, automatic restoration, and a longer soak before claiming stability.

## Fans stop with the module loaded and the service stopped

State 0 is NVIDIA's curve. A lower clamp of exactly 0 is not: both fans stay
stopped, `status` returns an ownership error instead of `state=0/12`, and a
warm reboot leaves the clamp in place because the EC stays powered. Version
0.1.4 writes the unset clamp over that 0 when the module loads and when
`automatic` or a lifecycle restore runs. A non-zero floor that this driver did
not set is still left untouched.

Confirm with RPM, not with the cooling state alone:

```sh
dgx-fan-control status
# then the dgx_ec_fan fan*_input reads in the usage guide
```

The kernel log line `clearing zero lower clamp` means that latch was present
and a restore was attempted. If both tachometers are still 0 after a
successful `state=0/12`, the clamp is no longer the cause; use the cold
power-cycle guidance above. Do not paper over it by pinning a permanent
minimum floor. That abandons NVIDIA's curve whenever the service is stopped.

## Fans stay fast, or curve stops following temperature

`automatic` removes the common lower clamp. Firmware hysteresis may keep the
fans fast until its own policy decides to reduce them. Check RPM and temperature
over time. The daemon's sensor-failure response requests maximum cooling; its
journal explains those transitions.

Stop the daemon before using `set-state`. Version 0.1.1 rereads the kernel state
on every sample, so normal suspend/resume needs no manual restart. Version
0.1.0 needs `sudo systemctl restart dgx-fan-control.service` after resume.

For an issue report, include board model, OS/kernel and firmware versions,
module version, the commands used, and relevant log excerpts. Remove hostnames,
serial numbers, network addresses, and unrelated application output.
