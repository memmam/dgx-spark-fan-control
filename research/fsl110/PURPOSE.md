# fsl110

Research-only one-shot for Kensaki. It is not the fan controller, it is not
DKMS, and nothing in the controller install path loads it.

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
EC kept 4687. Unload does not undo a hold. The release that has restored
2700/4050 is a reboot, seen after level 100 through this same method.

`run-once.sh` is Kensaki only and still requires the 2700/4050 pair. Atarime
uses `build-sign-atarime.sh` and `run-once-atarime.sh`. Those refuse any other
hostname. The 110 module is unchanged: one `run` still calls `\_SB.FAN0._FSL`
with 110 and a second `run` is refused before ACPI.

`fsl_ramp_kensaki` accepts only `step`, and only for arguments 120, then 130,
then 140. `fsl_probe_atarime` accepts one `run` and only argument 120. A
failed ramp step latches and refuses the rest. Neither module is the 110 helper.

## Cleanup

`rmmod fsl110` removes `/proc/fsl110` and does not change the fan. Delete this
directory when the module is unloaded. Do not add it to DKMS or
`modules-load.d`.
