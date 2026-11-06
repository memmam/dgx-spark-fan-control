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

Version 0.1.5 accepts exactly two capability replies: EC 3.5.8
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

`fan1_input` and `fan2_input` under the `dgx_ec_fan` hwmon device should still
be readable. Do not register a second fan driver or send raw mailbox commands
to work around the rejection. The ACPI `Fan` cooling device does not change
fan speed on this EC either.

## Mailbox timeout and the ACPI fan devices

A failed **first** query is a different failure. EC 3.5.8 paired with October
SoC firmware **2.158.15** (`0x02009e0f`) has failed the FF-A/eSPI mailbox on
the first read-only query (eSPI status `0x05`, about 54 ms). This driver
returns `-EIO` for submit status `0x05` and `-ETIMEDOUT` when a poll stays
pending. Either one, before a capability profile matches, refuses the device.
This driver's 3.5.8 fan-floor support assumes the pre-October SoC firmware.
The kernel log says that. October SoC **2.158.15** (`0x02009e0f`) is the
pairing that failed the first read-only query.
The module does not bind, including no hwmon device, because the mailbox did
not answer.

On **EC 3.5.8 with that newer SoC**, the path that has been observed to work
is the ACPI fan device, not this driver:

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
and both fans stopped at level 0. Do not write a level below 30; those
observers use 40. Writing either device on that hardware moved both fans.
This project does not implement that path. On EC 3.13.7 the same write is
accepted and ignored.

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
will not overwrite an unknown existing floor. Avoid concurrent writers. Version
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
three consecutive failures it attempts automatic restoration and exits 69.
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
for the daemon's startup check that fan control is unsupported, and
`RestartPreventExitStatus=69` stops only that exit from looping. A transport
failure or crash does not use exit 69, so it still restarts. Exit 69 used to
cover those runtime failures too, which left a stopped daemon holding a low
floor while temperatures stayed under the alarms (observed for 1 day 19 hours).
`dgx-fan-control watchdog` does not alarm when the last exit is 69. It still
logs and broadcasts when the unit is `failed`, or enabled but inactive with
another exit code, before it applies the temperature rule. SIGTERM lets the
daemon perform its own cleanup, without a competing `ExecStop` writer.

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
dgx-fan-control status                    # require state=0/12
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
# then the hwmon fan1_input / fan2_input commands in the usage guide
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
