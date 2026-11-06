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

Do this between jobs. First restore automatic control and unload the existing
module; do not proceed if restoration or readback fails. A persistently pending
relay needs [maintenance recovery](troubleshooting.md#persistent-pending-and-service-exit-69)
before the new module can be loaded safely:

```sh
sudo systemctl stop dgx-fan-control.service
sudo dgx-fan-control automatic
dgx-fan-control status                    # require state=0/12 (daemon not setting a floor; EC's own curve)
sudo modprobe -r dgx_ec_fan_control
git pull --ff-only
./scripts/check
```

Then follow the route you originally installed:

- **Manual:** repeat the manual build/sign/install commands for the running
  kernel. Also rebuild for other installed kernels you intend to boot.
- **DKMS:** remove the old registered version. Upgrading from 0.1.4:
  `sudo dkms remove dgx-spark-fan-control/0.1.4 --all`
  (from 0.1.0: `sudo dkms remove -m dgx-spark-fan-control -v 0.1.0 --all`).
  Check `dkms status` for the installed version, then repeat the source-copy,
  add, build, and install steps in the installation guide. Use the version in
  the new checkout's `dkms.conf` (currently `0.1.6`) wherever the commands
  name a version. This also refreshes a changed checkout that retains the
  same version; `git pull` alone does not refresh DKMS's stored source.
  Build for every additional installed kernel you intend to boot.
  DKMS does not update `/usr/local/sbin/dgx-fan-control`. Install that command
  in the next step or the previous CLI remains.

Refresh the command, the unit files, and the motd snippet, then load and
check the new module. `install-userland` keeps an existing
`/etc/default/dgx-fan-control` and boot-load entry, restarts the watchdog and
`dgx-fan-control.service` only if they are already running, and does not
enable the performance daemon. If you installed with `--no-watchdog`, pass it
again. The full update steps are in
[Update the userspace](installation.md#6-update-the-userspace). Rebuild the
module only when that section's version check says to, using
[7A](installation.md#7a-update-the-module-with-dkms) or
[7B](installation.md#7b-update-the-module-by-hand).

```sh
sudo ./scripts/install-userland
sudo modprobe dgx_ec_fan_control
dgx-fan-control status
```

## Switch from manual installation to DKMS

Restore automatic mode and unload the module as above. Remove the manually
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
