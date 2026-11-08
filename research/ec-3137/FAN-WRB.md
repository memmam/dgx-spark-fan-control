# Fan SMC bytes from capture 8f6831

The binary ACPI table `tables/DSDT` in capture `8f6831` has SHA-256
`1a160b3bfcbd0d61df99fd3aa468582b4142ed1e2615a2b50c00cd808d726908`.
The lines below are the iasl disassembly of that table, device `\_SB.FAN0`.
They are not a rewrite of the method.

`OperationRegion (SMCR, FFixedHW, One, 0x90)` is offset 1 and length
`0x90` (144) bytes. The field `SMCF` is 1152 bits, the same 144 bytes.
`WRB` builds a 0x90 buffer and names qword 0 `XX0` and the qword at
offset `0x20` `XX4`. It sets `XX0` to `0xC400008D` and

```
XX4 = ((0x09 | (Arg0 << 0x08)) | (Arg1 << 0x28))
```

The low byte of that qword is `0x09`. `WFCF` calls `WRB` with addresses
`0x06000790`, `0x06000791`, `0x06000792`, and `0x06000793`.

`_FST` uses the same buffer shape and sets `XX4` to `0x000002060007040C`
for FAN0 and `0x000002060007060C` for FAN1. The low byte of both constants
is `0x0C`. `_FST` publishes a speed only when `XX0` comes back as
`0xC400008E`.

The reply observer treats a low byte of `0x09` in the qword at offset
`0x20` as one of those writes, and any other `0xC400008D` call as a read.
That rule is this capture, not a guess from the kernel. The kernel side,
read from Ubuntu `linux-nvidia-7.0` branch `applied/ubuntu/noble-devel`
(the `import/7.0.0-1024.25` URL returned 404, so this is not a byte check
of the installed package), is separate: offset 1 passes the buffer to
`arm_smccc_1_2_smc` as both the argument and the result, and that assembly
stores `x0`–`x17` back to the result pointer. `a4` is the fifth
`unsigned long` of `struct arm_smccc_1_2_regs`, at byte `0x20`. Offsets
other than 0 and 1 go to a custom handler in that file, not to this SMC
path.
