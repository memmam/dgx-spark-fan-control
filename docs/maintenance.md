# Updates and removal

[← README](../README.md) · [Installation](installation.md) · [Usage](usage.md)

A module file is built for one kernel. Installing a new file does not replace
a module already loaded in memory. Keep NVIDIA's normal OS/firmware update
process; check this driver's build and operation before relying on extra cooling.

## Kernel updates with DKMS

With the [DKMS route](installation.md#3a-dkms-builds-for-kernel-updates), package
hooks normally rebuild and sign the registered source for newly installed
kernels. They do not update this project's source or Python service.

Before rebooting into a new kernel, set its **exact installed release name**
(the directory name under `/lib/modules`) and verify the build:

```sh
# Replace the example with the kernel you are about to boot.
new_kernel="6.17.0-1029-nvidia"
sudo apt-get install "linux-headers-$new_kernel"
sudo dkms install -m dgx-spark-fan-control -v 0.1.6 -k "$new_kernel"
dkms status -m dgx-spark-fan-control
modinfo -k "$new_kernel" -F vermagic dgx_ec_fan_control
modinfo -k "$new_kernel" -F signer dgx_ec_fan_control
```

Expect `installed` for that kernel and the intended signing identity. DKMS
reuses the configured certificate; no repeat enrollment is needed unless the
key changes. A failed rebuild is not fixed by copying a `.ko` from an old kernel.
Resolve build failures before depending on this controller after reboot.

After booting the new kernel:

```sh
uname -r
dgx-fan-control status
systemctl is-active dgx-fan-control.service
sudo journalctl -k -b | grep -E 'dgx|fan.floor|arm.ffa'
```

`inactive` is expected if you chose factory-curve/manual-boost mode. In that
mode, status should initially show `state=0/12 (daemon not setting a floor; EC's own curve)`. With the performance service
enabled, the state follows temperature. Firmware updates also require this
check: a successful kernel build does not prove a changed EC contract works.

## Kernel updates with manual builds

From your checkout, after installing the new kernel's matching headers:

```sh
# Replace the example with the kernel you are about to boot.
new_kernel="6.17.0-1029-nvidia"
sudo apt-get install "linux-headers-$new_kernel"
KERNEL_RELEASE="$new_kernel" ./scripts/build-sign
sudo install -D -m 0644 kernel/dgx_ec_fan_control.ko \
  "/lib/modules/$new_kernel/updates/dgx_ec_fan_control.ko"
sudo depmod -a "$new_kernel"
modinfo -k "$new_kernel" -F vermagic dgx_ec_fan_control
modinfo -k "$new_kernel" -F signer dgx_ec_fan_control
```

Use the same `DGX_MOK_DIR` override as at installation, if any. On a system
without signature enforcement, the unsigned equivalent is
`make -C kernel KDIR="/lib/modules/$new_kernel/build" clean all`.
After reboot, perform the status checks above.

## Update this project's source

Do this between jobs. Update the userspace on every update. Rebuild the kernel
module only when the version check below says `kernel/` changed. A persistently
pending relay needs
[maintenance recovery](troubleshooting.md#persistent-pending-and-service-exit-69)
before the new module can be loaded safely.

### Update the userspace

Do this on every update. It does not rebuild the kernel module.

```sh
git pull --ff-only
./scripts/check
sudo ./scripts/install-userland
sudo dgx-fan-control health
sudo dgx-fan-control health --record
```

`--ff-only` refuses a diverged checkout instead of merging. `./scripts/check`
is the same test run as a fresh install.

The script is the same one as in
[Choose your startup mode](installation.md#5-choose-your-startup-mode) and is
safe to rerun. It reinstalls the command, every unit file, and the motd
snippet. It keeps an existing `/etc/default/dgx-fan-control`, so a
`BUSY_CHECK`, `READY_CHECK`, or `MANUAL_STATE` you already set is kept, and
it keeps an existing boot-load entry. It restarts the watchdog and
`dgx-fan-control.service` only if they are already running, and it does not
enable the performance service. If you installed with `--no-watchdog`, pass
it again; without it, the script enables and starts the watchdog.

New optional keys in `systemd/dgx-fan-control.default`, such as
`PROGRESS_CHECK` and `STUCK_SECONDS`, are not merged into an existing
`/etc/default` file. Copy the ones you want by hand. The script does not
touch `/etc/modprobe.d/dgx-fan-control-owned.conf`; if `set-state` wrote it,
that file is how the next load receives `owned_state` and `owned_floor`.

The first `health` run prints the old record as `stale (written by a
different install)`; that is expected, and the `--record` run replaces it.
Why that refresh is required, and what `--record` clears, is in
[Install the command, then load the driver](installation.md#4-install-the-command-then-load-the-driver).
Do not write an ACPI level to make this health check pass or to provoke a
result. The exception after an EC downgrade is in
[EC firmware pairing and downgrade](installation.md#ec-firmware-pairing-and-downgrade).

### Decide whether the kernel module changed

```sh
modinfo dgx_ec_fan_control | grep ^version:
```

Compare that with `DGX_EC_DRIVER_VERSION` in `kernel/dgx_ec_fan_control.c`
(currently `0.1.6`). If they already match and `kernel/` did not change,
stop here. Otherwise use the same module route you installed with:
[DKMS](#update-the-module-with-dkms) or
[by hand](#update-the-module-by-hand).

Run either module route only after the `git pull` above. Building before the
pull rebuilds the old source under the new version number.

First restore automatic control and unload the existing module. Do not proceed
if restoration or readback fails. This clears a held floor on purpose:

```sh
sudo systemctl stop dgx-fan-control.service
sudo dgx-fan-control automatic
dgx-fan-control status                    # require state=0/12 (daemon not setting a floor; EC's own curve)
sudo modprobe -r dgx_ec_fan_control
```

### Update the module with DKMS

Use this when the original install was
[DKMS](installation.md#3a-dkms-builds-for-kernel-updates).
DKMS signs with the enrolled MOK already configured in
`/etc/dkms/framework.conf`. It does not run `scripts/build-sign`.

`git pull` alone does not refresh DKMS's stored source. Use the version in
the new checkout's `dkms.conf` (currently `0.1.6`) wherever a command names a
version. When `dkms status` still shows an older registered version, remove
that version before registering the new one. Upgrading from 0.1.4:
`sudo dkms remove dgx-spark-fan-control/0.1.4 --all`
(from 0.1.0: `sudo dkms remove -m dgx-spark-fan-control -v 0.1.0 --all`).
Check `dkms status` and substitute the version it prints.

From the checkout, refresh the registered source and rebuild for the running
kernel. `dkms install --force` refreshes a checkout that kept the same
version. If this version is not registered yet, the add/build/install steps
in the DKMS installation route are the first-time form of the same copy:

```sh
sudo install -d /usr/src/dgx-spark-fan-control-0.1.6/kernel
sudo install -m 0644 dkms.conf /usr/src/dgx-spark-fan-control-0.1.6/dkms.conf
sudo install -m 0644 kernel/Makefile kernel/dgx_ec_fan_control.c \
  /usr/src/dgx-spark-fan-control-0.1.6/kernel/
sudo dkms install -m dgx-spark-fan-control -v 0.1.6 -k "$(uname -r)" --force
dkms status -m dgx-spark-fan-control
modinfo -F signer dgx_ec_fan_control
```

Expect `installed` for `$(uname -r)` and the enrolled certificate subject
in `signer`. Build for every additional installed kernel you intend to boot.
DKMS does not update `/usr/local/sbin/dgx-fan-control`; the userspace step
above does.

Then [reload and check](#reload-and-check). The reload reads `owned_state`
and `owned_floor` from `/etc/modprobe.d/dgx-fan-control-owned.conf` when that
file exists. Loading the module puts the fans back on the EC's own curve, so
a held manual floor is gone after the reload. Because automatic already
cleared that floor, `dgx-fan-manual.service` exits without writing. If the
performance daemon was not running and a floor is still recorded, the reload
block reruns that unit to reapply it.

### Update the module by hand

Use this when the original install was
[manual build and sign](installation.md#3b-manual-build-and-sign).
Build and sign with the enrolled MOK, then install for the running kernel.
Use the same `DGX_MOK_DIR` override as at installation, if any. For any other
installed kernel you intend to boot, repeat the build with that kernel's
headers as in [Kernel updates with manual builds](#kernel-updates-with-manual-builds).

```sh
./scripts/build-sign
sudo install -D -m 0644 kernel/dgx_ec_fan_control.ko \
  "/lib/modules/$(uname -r)/updates/dgx_ec_fan_control.ko"
sudo depmod -a
modinfo -F signer dgx_ec_fan_control
```

Run `./scripts/build-sign` as the user who owns the key. The script does not
install or load the module. An unsigned module will not load while Secure
Boot is enforced. Then the same [reload and check](#reload-and-check) as
DKMS. `modprobe` applies `owned_state` and `owned_floor` from
`/etc/modprobe.d/dgx-fan-control-owned.conf`. As in the DKMS route, the
reload drops a held manual floor, and the block reruns
`dgx-fan-manual.service` to reapply it when the performance daemon is not
restarted.

### Reload and check

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
else
  sudo systemctl restart dgx-fan-manual.service
fi
sudo dgx-fan-control health
sudo dgx-fan-control health --record
modinfo dgx_ec_fan_control | grep ^version:
```

Why that refresh is required, and what `--record` clears, is in
[Install the command, then load the driver](installation.md#4-install-the-command-then-load-the-driver).
Do not write an ACPI level to make this health check pass or to provoke a
result. The exception after an EC downgrade is in
[EC firmware pairing and downgrade](installation.md#ec-firmware-pairing-and-downgrade).

## Switch from manual installation to DKMS

Restore automatic mode and unload the module as in
[Decide whether the kernel module changed](#decide-whether-the-kernel-module-changed).
Remove the manually
installed file for the running kernel:

```sh
sudo rm -f "/lib/modules/$(uname -r)/updates/dgx_ec_fan_control.ko"
sudo depmod -a
```

Repeat for any other kernel where you manually installed this exact file. Then
follow the DKMS installation route and load/verify the result. Removing manual
copies first avoids DKMS backing one up and restoring it during later removal.

## Uninstall

### 1. Restore the factory curve before removing anything

Stop the watchdog first so it cannot write an ACPI level while the module
comes out. Then stop the performance daemon and restore the factory curve.

```sh
sudo systemctl disable --now dgx-fan-watchdog.service
sudo systemctl disable --now dgx-fan-health.service
sudo systemctl disable --now dgx-fan-manual.service
sudo systemctl disable --now dgx-fan-control.service
sudo dgx-fan-control automatic
dgx-fan-control status                    # require state=0/12 (daemon not setting a floor; EC's own curve)
sudo modprobe -r dgx_ec_fan_control
```

`sudo ./scripts/uninstall-userland` stops and disables the watchdog, the
health check, and the performance daemon, and removes their files, the motd
snippet, the defaults file, and the command. Run it only after the factory
curve is restored. It does not unload the kernel module.

If restoration, readback, or unload fails, stop here and consult
[troubleshooting](troubleshooting.md). Do not discard the tools needed to
inspect and restore a failed state.

### 2. Remove the module using your installation method

For DKMS (substitute your installed version if different):

```sh
sudo dkms remove -m dgx-spark-fan-control -v 0.1.6 --all
dkms status -m dgx-spark-fan-control
```

For a manual installation:

```sh
sudo rm -f "/lib/modules/$(uname -r)/updates/dgx_ec_fan_control.ko"
sudo depmod -a
```

Repeat manual removal for other kernels where you installed it. DKMS source
under `/usr/src/dgx-spark-fan-control-0.1.6` can be removed separately once no
registered version uses it.

### 3. Remove startup entries and the userland command

```sh
sudo rm -f /etc/modules-load.d/dgx_ec_fan_control.conf
sudo rm -f /etc/systemd/system/dgx-fan-control.service
sudo rm -f /etc/systemd/system/dgx-fan-watchdog.service
sudo rm -f /etc/systemd/system/dgx-fan-health.service
sudo rm -f /etc/systemd/system/dgx-fan-manual.service
sudo rm -f /etc/modprobe.d/dgx-fan-control-owned.conf
sudo rm -f /etc/update-motd.d/95-dgx-fan-control
sudo rm -f /etc/default/dgx-fan-control
sudo rm -f /usr/local/sbin/dgx-fan-control
sudo systemctl daemon-reload
```

Keep signing keys, enrolled certificates, and system-wide DKMS signing settings
if other modules depend on them. Uninstalling this controller does not require
changing Secure Boot enrollment.
