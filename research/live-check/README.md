# Live hardware checks

This is a checkout diagnostic. `scripts/install-userland` does not install it.
`./scripts/check` does not discover or run it. The watchdog, the health
service, and the kernel module do not call it.

Set `DGX_FAN_LIVE=1` or the script exits before it reads sysfs or talks to
systemd. `DGX_FAN_CONTROL_OFFLINE=1` (the variable `./scripts/check` exports)
refuses the run even when the live flag is set. A case that changes fan
state also needs the same name passed to `--case` and `--consent`.

The 15-second command timeout stops the userspace program. It does not
interrupt `sync_send_receive2`. A secure-firmware call that never returns
still holds the driver mutex.

Nothing here enables or installs a service, writes ACPI, writes a literal
fan-clamp of 0, opts into `automatic_floor_recovery` or
`allow_unverified_writes`, injects a timeout, suspends, or reboots. Cooling
state 0 is the unset clamp `0xffff`, NVIDIA's curve, and this script does
not write that either. EC 3.13.7 never gets a floor write or a controller
start. `floor-automatic` runs only when the manual floor is `none`, the
cooling state is 0, and `dgx-fan-manual.service` is disabled or not
installed. If that unit is already enabled, the case is `SKIP`: `automatic`
would disable it, and this script will not change that saved setting to
make the case runnable. If `automatic` fails, the script stops and does not
try another cleanup.

EC 3.13.7 `status` does not print a zone temperature. The temperature used
here is the health line `temperature: pass (N.NC)` or `warn (...)`, then
the `hottest=` field on EC 3.5.8 status, then the kernel
`thermal_zone*/temp` files health already reads. A missing or malformed
value is reported as missing. It is not a cool reading, the read-only pass
fails, and no idle mutation starts.

The cool bound, below 60°C, is only a precondition for these cases. It is
not a thermal-policy threshold.

## Read-only, either machine

This prints the machine, profile, checkout version, loaded version, module
parameters, service and watchdog state, the manual unit's enablement, fan
RPM, and the temperature with its source. It does not change them. `PASS
read-only` requires a temperature from health, status, or a kernel thermal
zone. `hottest: None` is not that.

```sh
cd ~/dsfc
DGX_FAN_LIVE=1 ./research/live-check/live-check
```

## Next cases, one at a time

Run the read-only command again first. Use a mutating command only when
that pass prints a temperature, `source=health`, `status`, or
`thermal-zone`, and the value is below 60°C. A `SKIP` is the result; do
not disable `dgx-fan-manual.service` by hand to clear it.

On EC 3.5.8, `service-start-stop` is the first mutation. It starts and
stops the performance service and does not enable or disable the manual
unit. Then `unload-reload`, still from state 0 and manual floor `none`.
`floor-automatic` is last, and only when the read-only line shows
`manual-unit: disabled` or `manual-unit: not-found`.

On EC 3.13.7, do not run `floor-automatic` or `service-start-stop`. The
only mutation is `unload-reload`, and only after the new read-only pass
has a real temperature. It must not be run while the temperature line says
missing.

## Mutating cases

Run one at a time, as root, only after that read-only pass. Each refuses
with `SKIP` and the unmet precondition when the machine is not idle, cool,
watched, and clean. EC 3.13.7 prints `MANUAL` for the floor and service
cases and does not run them.

`floor-automatic` and `service-start-stop` are EC 3.5.8 only. They change
fan speed. `service-start-stop` uses `systemctl start` and later
`systemctl stop`. It does not enable or disable either unit.
`systemctl start` returns when the process has started. The daemon writes
its first non-zero floor in that first loop, before its 2-second wait, so
one immediate status can still show state 0. The harness then checks up to
4 samples, 2 seconds apart. That does not wait for the unit's 30-second
restart delay. The status words "daemon not setting a floor" mean the
cooling state is 0. If the floor never leaves 0, or start fails, the
harness still stops the service and reports that stop separately when it
fails. `unload-reload` unloads and reloads the module on either profile;
on EC 3.5.8 only from state 0 with no manual floor.

```sh
sudo DGX_FAN_LIVE=1 ./research/live-check/live-check \
  --case floor-automatic --consent floor-automatic

sudo DGX_FAN_LIVE=1 ./research/live-check/live-check \
  --case service-start-stop --consent service-start-stop

sudo DGX_FAN_LIVE=1 ./research/live-check/live-check \
  --case unload-reload --consent unload-reload
```

## Still manual

The script lists these as `MANUAL` and does not run them: reboot notifier,
reboot with a saved manual floor, sustained load, a natural mailbox
timeout, suspend, any ACPI write, clearing a foreign floor, injecting a
wedge, and writing a literal lower clamp of 0. Cooling state 0 is the
unset clamp `0xffff`, not that stop.
