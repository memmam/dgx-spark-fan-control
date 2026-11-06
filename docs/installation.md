# Install on a DGX Spark

[← README](../README.md) · [Usage](usage.md) · [Updates and removal](maintenance.md)

Run commands on the Spark itself. Plan console access for the one-time signing
key enrollment: it includes a firmware-screen step during reboot.

The order is: **get the source → enroll a signing key → choose DKMS or manual
build → load and verify → choose startup behavior**.

## 1. Get the source and tools

```sh
sudo apt-get update
sudo apt-get install git build-essential linux-headers-"$(uname -r)" python3 openssl mokutil
git clone https://github.com/christopherowen/dgx-spark-fan-control.git
cd dgx-spark-fan-control
./scripts/check
uname -r
mokutil --sb-state
```

Keep this checkout for builds and updates. Commands below assume you are in its
root. If matching headers are unavailable, resolve that first through your
DGX OS package repositories. Another kernel's headers are not a substitute.

## 2. Generate and enroll your signing key

### Create a key, once

```sh
./scripts/generate-signing-key
```

The script creates these files outside the checkout:

| File | Purpose |
| --- | --- |
| `$HOME/.local/share/dgx-spark-fan-control/keys/MOK.priv` | Private signing key, mode `0600`; keep private |
| `$HOME/.local/share/dgx-spark-fan-control/keys/MOK.der` | Public certificate to enroll on the Spark |

It refuses to overwrite an existing pair. Reuse the enrolled pair for future
builds; do not generate a new key for every kernel update.

If you already maintain an enrolled pair named `MOK.priv` and `MOK.der`, skip
generation and select its directory:

```sh
export DGX_MOK_DIR="/absolute/path/to/your/keys"
```

The remaining commands use that override when set, otherwise the default path.

### Request enrollment

```sh
sudo mokutil --import "${DGX_MOK_DIR:-$HOME/.local/share/dgx-spark-fan-control/keys}/MOK.der"
```

Choose a temporary enrollment password. When ready to interrupt work:

```sh
sudo systemctl reboot
```

At the firmware MOK manager, select **Enroll MOK → Continue → Yes**, enter the
password, and complete the reboot. This needs the preboot console; SSH alone
does not complete enrollment.

### Verify after reboot

Return to the checkout. If using a custom `DGX_MOK_DIR`, export it again in the
new shell. Verify enrollment before building:

```sh
mokutil --test-key "${DGX_MOK_DIR:-$HOME/.local/share/dgx-spark-fan-control/keys}/MOK.der"
```

Expect a message saying the certificate is already enrolled. A generated key
or pending import alone is insufficient. See
[Ubuntu's Secure Boot documentation](https://documentation.ubuntu.com/security/security-features/platform-protections/secure-boot/)
for background.

If your machine intentionally runs without signature enforcement, enrollment
can be skipped. The manual unsigned build is below; changing Secure Boot
settings is not required by this project.

## 3A. DKMS: builds for kernel updates

Choose **this route or the manual route**, not both. DKMS registers module
source and uses the distribution's kernel-install hooks to rebuild it for new
kernels. Matching headers and successful compilation are still required.

```sh
sudo apt-get install dkms
dkms --version
```

These instructions target DKMS 3.x on Ubuntu-derived DGX OS. The included
[`dkms.conf`](../dkms.conf) builds only the control module and permits aarch64
targets. DKMS handles signing itself; it does not run `scripts/build-sign`.

### Set DKMS's persistent signing identity

First inspect existing DKMS signing settings:

```sh
sudo grep -H -E '^[[:space:]]*(mok_signing_key|mok_certificate)=' \
  /etc/dkms/framework.conf /etc/dkms/framework.conf.d/*.conf
```

No matches or an absent drop-in directory are normal on a fresh installation.
If DKMS already uses an enrolled key, retain it and verify that certificate
instead of changing the signing identity for your other modules.

For a new setup, copy the pair from step 2 to a persistent root-owned directory
so unattended builds do not depend on your home being mounted:

```sh
sudo install -d -m 0700 /root/.local/share/dgx-spark-fan-control/keys
sudo install -m 0600 \
  "${DGX_MOK_DIR:-$HOME/.local/share/dgx-spark-fan-control/keys}/MOK.priv" \
  /root/.local/share/dgx-spark-fan-control/keys/MOK.priv
sudo install -m 0644 \
  "${DGX_MOK_DIR:-$HOME/.local/share/dgx-spark-fan-control/keys}/MOK.der" \
  /root/.local/share/dgx-spark-fan-control/keys/MOK.der
sudo mokutil --test-key /root/.local/share/dgx-spark-fan-control/keys/MOK.der
sudoedit /etc/dkms/framework.conf
```

Set these entries, preserving unrelated settings:

```sh
mok_signing_key="/root/.local/share/dgx-spark-fan-control/keys/MOK.priv"
mok_certificate="/root/.local/share/dgx-spark-fan-control/keys/MOK.der"
```

These are **system-wide DKMS settings**. Check that no existing drop-in overrides
them. Verify the key and certificate paths printed during the build are the
intended, enrolled pair. See the
[DKMS signing documentation](https://github.com/dell/dkms/tree/v3.0.11#module-signing).

### Register, build, and install

From the checkout root, register only the files needed to build the module:

```sh
sudo install -d /usr/src/dgx-spark-fan-control-0.1.5/kernel
sudo install -m 0644 dkms.conf /usr/src/dgx-spark-fan-control-0.1.5/dkms.conf
sudo install -m 0644 kernel/Makefile kernel/dgx_ec_fan_control.c \
  /usr/src/dgx-spark-fan-control-0.1.5/kernel/
sudo dkms add -m dgx-spark-fan-control -v 0.1.5
sudo dkms build -m dgx-spark-fan-control -v 0.1.5 -k "$(uname -r)"
sudo dkms install -m dgx-spark-fan-control -v 0.1.5 -k "$(uname -r)"
dkms status -m dgx-spark-fan-control
modinfo -F signer dgx_ec_fan_control
```

Expect `installed` for the running kernel and the intended certificate subject
in `signer`. **DKMS installs only the kernel module.** It does not replace
`/usr/local/sbin/dgx-fan-control`. Continue with step 4 and install that
command before loading the module, or a previous 0.1.4 CLI stays in place.
If previously installed manually, use the
[migration instructions](maintenance.md#switch-from-manual-installation-to-dkms)
first so an old module does not mask the DKMS version.

## 3B. Manual build and sign

This route requires repeating the build/install steps for each new kernel:

```sh
./scripts/build-sign
sudo install -D -m 0644 kernel/dgx_ec_fan_control.ko \
  "/lib/modules/$(uname -r)/updates/dgx_ec_fan_control.ko"
sudo depmod -a
modinfo -F signer dgx_ec_fan_control
```

The script uses the enrolled pair from step 2 and builds for the running
kernel. Run it as the user who owns the key; use `sudo` for installation.
The script itself never installs or loads a module.

For a system without signature enforcement, replace `./scripts/build-sign`
with `make -C kernel`, then use the same installation commands. An unsigned
module will not load under enforced Secure Boot.

## 4. Install the command, then load the driver

**Do this after every DKMS or manual module build.** DKMS does not install
the Python command. If `/usr/local/sbin/dgx-fan-control` is left over from
0.1.4, the new module will be controlled by the old CLI until this copy
replaces it.

```sh
sudo install -D -m 0755 userspace/dgx_fan_control.py /usr/local/sbin/dgx-fan-control
sudo modprobe dgx_ec_fan_control
dgx-fan-control status
```

Status must start with **`state=0/12 (no floor; EC curve)`**: the device was
found and its added floor is unset. That is NVIDIA's curve, not fans stopped.
On EC 3.13.7, status may instead print
`control unsupported on this firmware`: telemetry is up and floor writes were
rejected. A mailbox timeout prints an error and does not create the hwmon
device. EC 3.5.8 with October SoC 2.158.15 has both completed this probe and
timed out on other boots, so a timeout is a fallback case, not the only
result for that pair. If status says the probe refused a foreign floor, do
not write ACPI level 0. Successful `modprobe` alone is insufficient; device
probing can still reject the firmware contract. See
[troubleshooting](troubleshooting.md) if needed.

## 5. Choose your startup mode

Install the boot-load entry, the unit files, the health check, and the
command. This enables and starts `dgx-fan-watchdog.service`, and enables
`dgx-fan-health.service` for the next boot. It does **not** enable
`dgx-fan-control.service`.

```sh
sudo install -D -m 0644 systemd/dgx_ec_fan_control.conf \
  /etc/modules-load.d/dgx_ec_fan_control.conf
sudo ./scripts/install-userland
```

The same commands by hand, without the script. Run them from the checkout.
This installs the command, the three unit files, the motd snippet, the
defaults file when it is absent, and the modules-load entry, then reloads
systemd, enables the health check, and enables the watchdog. It does **not**
enable `dgx-fan-control.service`. The defaults file is not overwritten when
it already exists.

```sh
sudo install -D -m 0755 userspace/dgx_fan_control.py /usr/local/sbin/dgx-fan-control
sudo install -D -m 0644 systemd/dgx_ec_fan_control.conf \
  /etc/modules-load.d/dgx_ec_fan_control.conf
sudo install -D -m 0644 systemd/dgx-fan-control.service \
  /etc/systemd/system/dgx-fan-control.service
sudo install -D -m 0644 systemd/dgx-fan-watchdog.service \
  /etc/systemd/system/dgx-fan-watchdog.service
sudo install -D -m 0644 systemd/dgx-fan-health.service \
  /etc/systemd/system/dgx-fan-health.service
sudo install -D -m 0644 systemd/dgx-fan-manual.service \
  /etc/systemd/system/dgx-fan-manual.service
sudo install -D -m 0755 systemd/95-dgx-fan-control \
  /etc/update-motd.d/95-dgx-fan-control
if [ ! -f /etc/default/dgx-fan-control ]; then
  sudo install -D -m 0644 systemd/dgx-fan-control.default \
    /etc/default/dgx-fan-control
fi
sudo systemctl daemon-reload
sudo systemctl enable dgx-fan-health.service
sudo systemctl enable dgx-fan-manual.service
sudo systemctl enable --now dgx-fan-watchdog.service
```

To install those files and leave the watchdog disabled, stop before the
`enable --now` line, or run `sudo ./scripts/install-userland --no-watchdog`.
The health check still expects the watchdog to be active, so that opt-out
makes the next health check record needs-intervention.

Manual removal of the command, the units, the motd snippet, and the defaults
file, the same steps as `scripts/uninstall-userland`. This does not unload
the kernel module. Restore the factory curve first, as in
[maintenance](maintenance.md#uninstall).

```sh
sudo systemctl disable --now dgx-fan-watchdog.service
sudo systemctl disable --now dgx-fan-health.service
sudo systemctl disable --now dgx-fan-manual.service
sudo systemctl disable --now dgx-fan-control.service
sudo rm -f /etc/systemd/system/dgx-fan-watchdog.service
sudo rm -f /etc/systemd/system/dgx-fan-health.service
sudo rm -f /etc/systemd/system/dgx-fan-manual.service
sudo rm -f /etc/modprobe.d/dgx-fan-control-owned.conf
sudo rm -f /etc/systemd/system/dgx-fan-control.service
sudo rm -f /etc/update-motd.d/95-dgx-fan-control
sudo rm -f /etc/default/dgx-fan-control
sudo rm -f /usr/local/sbin/dgx-fan-control
sudo systemctl daemon-reload
```

Then choose one mode:

| Startup mode | Best for | Command |
| --- | --- | --- |
| NVIDIA factory curve; manual boosts when needed | Pre-cooling individual jobs | `sudo systemctl disable --now dgx-fan-control.service` |
| This project's performance curve | Continual temperature-based extra cooling | `sudo systemctl enable --now dgx-fan-control.service` |

For factory-curve mode, also clear any manual floor:

```sh
sudo dgx-fan-control automatic
dgx-fan-control status
```

The module loads at boot on NVIDIA's curve (state 0) whether or not the
service is enabled. State 0 is that curve, not fans stopped. A latched lower
clamp of exactly 0 is cleared during load. A non-zero floor this driver does
not own is not cleared. Enabling the service adds our performance curve.
`module enable` loads the module and does not enable the service.
`sudo dgx-fan-control module enable --start-service` is the explicit choice,
same as the table above. `start`/`stop` change the current session;
`enable`/`disable` change future boots. Disabling the service alone does not
clear a manually set non-zero floor, which is why `automatic` is explicit
above. Do not substitute an ACPI level of 0 for `automatic`.

The watchdog is a separate unit. It reads the ACPI fans and `thermal_zone0`
directly. `systemctl disable --now dgx-fan-control.service` and
`module disable` do not stop it.

After a reboot, check the watchdog and the health oneshot. Do not write an
ACPI fan level for this check.

```sh
systemctl is-active dgx-fan-watchdog.service
systemctl is-active dgx-fan-health.service
sudo journalctl -u dgx-fan-watchdog.service -b --no-pager | head
sudo journalctl -u dgx-fan-health.service -b --no-pager
```

Both `is-active` commands should print `active`. The watchdog journal should
contain `fan-stall watchdog watching`. The health journal should contain
`health: healthy`, or a needs-intervention line when a check failed.
`dgx-fan-control health` as a normal user prints the same checks in one pass
and does not write the flag.
`dgx-fan-control status` prints `NEEDS INTERVENTION:` while that flag is set.
If you have explicitly enabled the performance
service, stop it without disabling it, confirm the watchdog journal then
shows the daemon-not-active alert, and start it again:

```sh
sudo systemctl stop dgx-fan-control.service
sudo journalctl -u dgx-fan-watchdog.service -n 20 --no-pager
sudo systemctl start dgx-fan-control.service
```

Do not write ACPI `cur_state` 0, or any level below 40, to provoke a stall.

For the performance service, verify startup:

```sh
systemctl is-enabled dgx-fan-control.service
systemctl is-active dgx-fan-control.service
dgx-fan-control status
sudo journalctl -u dgx-fan-control.service -n 30
```

The service runs as root to write thermal sysfs, with an empty capability set,
no network access, and a read-only system filesystem. Module loading is separate.
Continue to [pre-cooling and everyday usage](usage.md).

## 6. Updating an existing install

Do the userspace steps on every update. Update the kernel module only when
this checkout changes files under `kernel/`, which this update does. Check
before rebuilding:

```sh
modinfo dgx_ec_fan_control | grep ^version:
```

Compare that with `DGX_EC_DRIVER_VERSION` in `kernel/dgx_ec_fan_control.c`
(currently `0.1.5`). If they already match and `kernel/` did not change,
stop after the userspace steps. Otherwise use the same module route you
installed with: [6A](#6a-dkms-module-update) or
[6B](#6b-manual-module-update).

### Userspace steps

After `git pull`, reinstall the command, every unit file, and the motd
snippet. Copying only `dgx-fan-control.service` leaves the watchdog unit
missing, and `status` then shows `watchdog-enabled: not-found`. Leave an
existing `/etc/default/dgx-fan-control` in place so a `BUSY_CHECK`,
`READY_CHECK`, or `MANUAL_STATE` you already set is kept. Install the
template only when the file is absent. Do not replace
`/etc/modprobe.d/dgx-fan-control-owned.conf` if `set-state` wrote it; that
file is how the next load receives `owned_state` and `owned_floor`.

```sh
sudo install -D -m 0755 userspace/dgx_fan_control.py /usr/local/sbin/dgx-fan-control
sudo install -D -m 0644 systemd/dgx-fan-control.service \
  /etc/systemd/system/dgx-fan-control.service
sudo install -D -m 0644 systemd/dgx-fan-watchdog.service \
  /etc/systemd/system/dgx-fan-watchdog.service
sudo install -D -m 0644 systemd/dgx-fan-health.service \
  /etc/systemd/system/dgx-fan-health.service
sudo install -D -m 0644 systemd/dgx-fan-manual.service \
  /etc/systemd/system/dgx-fan-manual.service
sudo install -D -m 0755 systemd/95-dgx-fan-control \
  /etc/update-motd.d/95-dgx-fan-control
if [ ! -f /etc/default/dgx-fan-control ]; then
  sudo install -D -m 0644 systemd/dgx-fan-control.default \
    /etc/default/dgx-fan-control
fi
sudo systemctl daemon-reload
sudo systemctl enable dgx-fan-health.service
sudo systemctl enable dgx-fan-manual.service
if systemctl is-active --quiet dgx-fan-watchdog.service; then
  sudo systemctl restart dgx-fan-watchdog.service
fi
if systemctl is-active --quiet dgx-fan-control.service; then
  sudo systemctl restart dgx-fan-control.service
fi
```

If the watchdog unit was not installed yet, `is-active` does not enable it.
Use the manual install in [section 5](#5-choose-your-startup-mode), including
`systemctl enable --now dgx-fan-watchdog.service`. Restart the performance
daemon only when it is already active. Do not enable it from this update.

## 6A. DKMS module update

Use this when the original install was [3A](#3a-dkms-builds-for-kernel-updates).
DKMS signs with the enrolled MOK already configured in
`/etc/dkms/framework.conf`. It does not run `scripts/build-sign`. From the
checkout, refresh the registered source and rebuild for the running kernel:

```sh
sudo install -d /usr/src/dgx-spark-fan-control-0.1.5/kernel
sudo install -m 0644 dkms.conf /usr/src/dgx-spark-fan-control-0.1.5/dkms.conf
sudo install -m 0644 kernel/Makefile kernel/dgx_ec_fan_control.c \
  /usr/src/dgx-spark-fan-control-0.1.5/kernel/
sudo dkms install -m dgx-spark-fan-control -v 0.1.5 -k "$(uname -r)" --force
dkms status -m dgx-spark-fan-control
modinfo -F signer dgx_ec_fan_control
```

Expect `installed` for `$(uname -r)` and the enrolled certificate subject
in `signer`. Then unload and reload, and check health without sudo. The
reload reads `owned_state` and `owned_floor` from
`/etc/modprobe.d/dgx-fan-control-owned.conf` when that file exists.

```sh
if systemctl is-active --quiet dgx-fan-control.service; then
  sudo systemctl stop dgx-fan-control.service
  restart_performance=1
else
  restart_performance=0
fi
if lsmod | grep -q '^dgx_ec_fan_control '; then
  sudo modprobe -r dgx_ec_fan_control
fi
sudo modprobe dgx_ec_fan_control
if [ "$restart_performance" -eq 1 ]; then
  sudo systemctl start dgx-fan-control.service
fi
dgx-fan-control health
modinfo dgx_ec_fan_control | grep ^version:
```

`health` is not run with sudo. Do not write an ACPI fan level for this check.

## 6B. Manual module update

Use this when the original install was
[3B](#3b-manual-build-and-sign). Build and sign with the enrolled MOK, then
install for the running kernel:

```sh
./scripts/build-sign
sudo install -D -m 0644 kernel/dgx_ec_fan_control.ko \
  "/lib/modules/$(uname -r)/updates/dgx_ec_fan_control.ko"
sudo depmod -a
modinfo -F signer dgx_ec_fan_control
```

Run `./scripts/build-sign` as the user who owns the key. The script does not
install or load the module. An unsigned module will not load while Secure
Boot is enforced. Then the same unload, reload, and health check as [6A](#6a-dkms-module-update).
`modprobe` applies `owned_state` and `owned_floor` from
`/etc/modprobe.d/dgx-fan-control-owned.conf`.

```sh
if systemctl is-active --quiet dgx-fan-control.service; then
  sudo systemctl stop dgx-fan-control.service
  restart_performance=1
else
  restart_performance=0
fi
if lsmod | grep -q '^dgx_ec_fan_control '; then
  sudo modprobe -r dgx_ec_fan_control
fi
sudo modprobe dgx_ec_fan_control
if [ "$restart_performance" -eq 1 ]; then
  sudo systemctl start dgx-fan-control.service
fi
dgx-fan-control health
modinfo dgx_ec_fan_control | grep ^version:
```

`health` is not run with sudo. Do not write an ACPI fan level for this check.
