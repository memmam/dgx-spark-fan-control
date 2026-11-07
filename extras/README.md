# Extras

Checkout diagnostics. `scripts/install-userland` does not install this
directory, and the watchdog does not run it.

`acpi-floor-sweep` measures ACPI Fan levels on EC 3.5.8 while logging
tachometers and the hottest zone. It uses the same writer as `acpi-max`:
levels below 35 and level 0 are refused, and the run arms the ACPI monitor
before it writes. Abort and a normal finish both write level 100. That
level is still an ACPI hold. Reboot is what restores the EC curve.

Run it from the checkout root:

```sh
sudo extras/acpi-floor-sweep 40 35
```
