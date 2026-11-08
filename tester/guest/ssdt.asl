/* Labeled synthetic SSDT. This is not the captured DGX Spark DSDT.
 * \_SB.FAN0._FSL stores Arg0 and returns. ACPICA can evaluate that.
 * It does not call the Spark SMC handler. A Fixed Hardware region is
 * omitted because executing one hangs this virt boot.
 */
DefinitionBlock ("ssdt.aml", "SSDT", 2, "DGXTST", "LABELED", 1)
{
    Scope (\_SB)
    {
        Device (FAN0)
        {
            Name (_HID, EisaId ("PNP0C0B"))
            Name (FLVL, Zero)
            Method (_FSL, 1, Serialized)
            {
                FLVL = Arg0
            }
        }
    }
}
