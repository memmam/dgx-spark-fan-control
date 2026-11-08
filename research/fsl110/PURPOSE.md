# fsl110

Research-only callers. They are not the fan controller, not DKMS, and
nothing in the controller install path loads them.

Ubuntu `acpi-call` 1.2.2-2.1 is the nix-community 1.2.2 source with no patch
to `do_acpi_call`. After `acpi_evaluate_object` succeeds it always calls
`acpi_result_to_string(buffer.pointer)`. `\_SB.FAN0._FSL` has no `Return`.
On ARM64, `acpi_strict` is defined as 1, so Linux does not enable interpreter
slack and does not create an implicit return. A successful evaluation then
leaves the return pointer NULL. That package is the wrong tool on this machine.

`fsl110` calls `acpi_evaluate_object` with a NULL return buffer, which the
ACPICA evaluator accepts (`nsxfeval.c` skips the copy when the buffer pointer
is NULL). The argument is the integer 110. The path is `\_SB.FAN0._FSL`.
Init and exit do not evaluate AML. A write is evaluated only when the text is
exactly `run`, and a second `run` returns `-EBUSY` before ACPI.

`interpreter_ok` means the interpreter returned success. It does not mean the
EC kept the requested speed. Unload does not undo a hold.

`build-sign-ec358.sh` and `run-once.sh` are the EC 3.5.8 one-shot. The
launcher requires `DGX_FSL_EC358=yes` and the 2700/4050 pair.
`build-sign-ec3137.sh` and `run-once-ec3137.sh` are the same module for an
EC 3.13.7 machine, with `DGX_FSL_EC3137=yes`. Each launcher reads fwupd and
the current kernel pin and stops unless one of those reports the expected
version and neither reports a different one. A hidden Embedded Controller
with no pin stops the launcher. The scripts do not change fwupd's block and
do not infer a profile from the machine name.

`fsl_ramp` accepts only `step`, from argument 160 through 237 (request
8973). A finished dwell is followed by the next argument in that list.
Levels 120–150 are not in this binary. `fsl_probe` accepts one `run` and
only argument 120. A failed ramp step latches and refuses the rest.
`fst_status` only reads `_FST`. `fsl_reply` registers a kretprobe on
`acpi_ffh_address_space_arch_handler` and copies two qwords from that
handler's existing buffer when the request id is `0xC400008D`. A low byte
of `0x09` at offset `0x20` is kept in the write list. Other matches are
kept in the read list and cannot evict a write. It does not evaluate ACPI.
`reply-ec3137.sh` loads that observer, requires `state armed` and no write
record yet, and only then runs the argument-120 probe once. `CAPTURE_COMPLETE`
means four write records were kept. It does not mean the EC stored them.

Every `build-sign-*.sh` runs one `make` and then signs `fsl110.ko`,
`fsl_ramp.ko`, `fsl_probe.ko`, `fst_status.ko`, and `fsl_reply.ko`. Signing
uses `DGX_MOK_KEY` and `DGX_MOK_CERT` when both are set, otherwise the
single DKMS mok path. The scripts do not print those paths, create a key,
or enroll one. Launchers refuse a module older than its source, built for
another kernel, or with an empty signer.

## Cleanup

`rmmod` of the module this script loaded removes its proc entry and does
not change the fan. Do not add these modules to DKMS or `modules-load.d`.
Rebuild after this tree's module names change, and do not leave an older
research module loaded beside the new one.
