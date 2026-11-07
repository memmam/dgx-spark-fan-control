# ACPI floor sweep

Checkout diagnostic for measuring ACPI Fan levels. It is not installed by
`scripts/install-userland`, and the watchdog, health check, and kernel
module do not run it.

## What was measured

On EC 3.5.8 at idle, with the module unloaded, a 30-minute hold at level 35
kept the fans at 2070/2025 RPM. The hottest zone peaked at 53.7 C and
settled at 52.8 C. An earlier sample at 35 read 2160 RPM on fan1, one EC
step higher. That is one machine's idle hold, not a floor for every board.

Level 30 (about 1940 RPM) and level 20 (about 1620 RPM, one fan stalling)
are forum tach readings, not levels this script writes. Level 0, on the
hardware where it was tried, stopped both fans and left them stopped. The
script and the shared writer both refuse 0 and any level below 35.

The stock EC curve logs under `gpu_burn` are a separate measurement. They
do not come from this script.

## Usage

From the checkout root, on EC 3.5.8, at idle, with
`dgx_ec_fan_control` unloaded, `dgx-fan-control.service` stopped, and
`dgx-fan-watchdog.service` active:

```sh
sudo DGX_FAN_HARDWARE_TEST=1 research/acpi-floor-sweep/acpi-floor-sweep 40 35
```

Without that variable the script exits before preflight. `./scripts/check`
sets `DGX_FAN_CONTROL_OFFLINE=1`, which also refuses this script.

Defaults are levels 40 then 35, 180 seconds each, one sample per second.
`--min-rpm` defaults to 1000. `--max-temp` defaults to 60 C. A CSV is
written in the working directory.

It refuses to start if it is not root, the module is loaded, the
performance service is active, the bind is telemetry-only (`dgx_ec_fan`
without `dgx_ec_fan_floor`), or this boot's kernel log shows EC 3.13.7.
Before the first level it calls `arm_acpi_monitor` in
`userspace/dgx_fan_control.py`, which requires a positive EC 3.5.8
identification and refuses a latched ACPI incident.

## Limits that are not unique to this script

Every level goes through `write_acpi_fan_level`. That function still
refuses levels outside 35–100. Changing only this script cannot open
those levels.

Abort, interrupt, and a normal finish all write level 100. Level 100 is
an ACPI hold. It is the interim ceiling, not a return to the stock curve.
Reboot is what restores EC control, for this script and for every other
ACPI write from this project. A clean exit does not do that.

`--max-temp` only changes when the sweep aborts. It does not change the
watchdog's 70/80/85/90 ladder.
