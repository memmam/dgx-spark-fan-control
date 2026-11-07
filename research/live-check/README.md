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

Nothing here enables or installs a service, writes ACPI, writes state 0 as a
stop, opts into `automatic_floor_recovery` or `allow_unverified_writes`,
injects a timeout, suspends, or reboots. EC 3.13.7 never gets a floor write
or a controller start. `floor-automatic` is refused unless the manual floor
is `none`, the cooling state is 0, and `dgx-fan-manual.service` is disabled
or not installed, so a successful run puts that saved state back. If
`automatic` fails, the script stops and does not try another cleanup.

The cool bound, below 60°C, is only a precondition for these cases. It is
not a thermal-policy threshold.

## Read-only, either machine

This prints the machine, profile, checkout version, loaded version, module
parameters, service and watchdog state, fan RPM, and health. It does not
change them.

```sh
cd ~/dsfc
DGX_FAN_LIVE=1 ./research/live-check/live-check
```

## Mutating cases

Run one at a time, as root, only after the read-only pass. Each refuses
with `SKIP` and the unmet precondition when the machine is not idle, cool,
watched, and clean. EC 3.13.7 prints `MANUAL` for the floor and service
cases and does not run them.

`floor-automatic` and `service-start-stop` are EC 3.5.8 only. They change
fan speed. `unload-reload` unloads and reloads the module on either
profile; on EC 3.5.8 only from state 0 with no manual floor.

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
wedge, and writing a zero floor.
