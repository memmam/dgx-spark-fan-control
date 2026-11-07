# EC 3.13.7 fan paths, after the ACPI trials

No new Atarime write. Levels 110 and 120 stayed in the idle tach band.
`interpreter_ok` is not an EC acknowledgement. This note does not infer
rejection from that tach.

## Verified

`\_SB.FAN0._FSL` calls `WFCF`, which calls `WRB` four times. `WRB` in the
captured DSDT (`8f6831` and `981cff` are the same DSDT, SHA-256
`1a160b3bfcbd0d61df99fd3aa468582b4142ed1e2615a2b50c00cd808d726908`) stores
the SMC buffer and does not compare `XX0` with `0xC400008E`. That compare
is only in `_FST`, and only to decide whether to publish a speed.

The kernel handler `drivers/acpi/arm64/ffh.c` copies the SMC result back
into that buffer and returns success without reading the result and without
a log line. The research caller passes a NULL return buffer, so the reply
registers were not saved. `~/atarime-fsl110-kernel.log` was not supplied in
full and the probe module does not print those registers.

Family 7 through SoC service `0x81` is a different path. On EC 3.13.7,
selectors 3 and 5 return status `0xff` and do not store a floor or ceiling
(`CONTROL-PATHS.md`). Selectors 2 and 4 return success and `0xffff`. That
status byte is not the ACPI SMC reply. The public EC capsules do not
contain `0x06000790` as an immediate, so the ACPI byte-write is not shown
to be that selector. The UEFI partition in the public SoC capsules is empty
padding. No second host duty setter was located in the fan-relay slice,
which is identical in SoC 2.155.11 and 2.158.15.

## Hidden

The four write replies: the first SMC register, and whether the EC applied
the bytes at `0x06000790`–`0x06000793`. Unchanged tach fits a rejection, an
accepted setpoint that does not move the fans, or a request below the speed
already running. None of those is identified. Another `_FSL` level is not
the missing evidence. Capturing the reply would be a new firmware call and
is not prepared here.

## Not run

A read-only sysfs snapshot cannot recover that reply. `cur_state` only
shows the cached AML level, and only if this boot has not reset it. It is
not requested.
