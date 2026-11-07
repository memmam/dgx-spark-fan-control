// SPDX-License-Identifier: GPL-2.0-only
/*
 * EC 3.5.8 continuation. Hardcoded \_SB.FAN0._FSL arguments from 160
 * through 237. Levels 120-150 already ran and are not in this list.
 * Level 237 requests 8973, the greatest request still at or below 9000.
 * Level 238 would request 9007 and is not present. Nothing near the
 * 16-bit wrap is accepted.
 *
 * Write the exact text "step". Any other text is refused before ACPI.
 * A failed evaluation latches; later steps are refused. Load and unload
 * do not evaluate AML. The return buffer is NULL.
 */
#include <linux/acpi.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/proc_fs.h>
#include <linux/uaccess.h>

#define FSL_RAMP_PATH		"\\_SB.FAN0._FSL"

static const u64 fsl_ramp_arg[] = {
	160ULL, 170ULL, 180ULL, 190ULL, 200ULL, 210ULL, 220ULL, 225ULL,
	230ULL, 232ULL, 234ULL, 236ULL, 237ULL
};
#define FSL_RAMP_N ((int)(sizeof(fsl_ramp_arg) / sizeof(fsl_ramp_arg[0])))
static struct proc_dir_entry *fsl_ramp_ent;
static DEFINE_MUTEX(fsl_ramp_lock);
static int fsl_ramp_next;
static bool fsl_ramp_latched;
static bool fsl_ramp_busy;
static char fsl_ramp_status[48] = "idle\n";

static ssize_t fsl_ramp_read(struct file *file, char __user *buf,
			     size_t len, loff_t *off)
{
	char tmp[48];

	mutex_lock(&fsl_ramp_lock);
	strscpy(tmp, fsl_ramp_status, sizeof(tmp));
	mutex_unlock(&fsl_ramp_lock);
	return simple_read_from_buffer(buf, len, off, tmp, strlen(tmp));
}

static ssize_t fsl_ramp_write(struct file *file, const char __user *buf,
			      size_t len, loff_t *off)
{
	char raw[8];
	size_t count = len;
	acpi_handle handle;
	acpi_status status;
	union acpi_object arg;
	struct acpi_object_list args;
	u64 level;
	char msg[48];

	if (count == 0 || count >= sizeof(raw))
		return -EINVAL;
	if (copy_from_user(raw, buf, len))
		return -EFAULT;
	if (raw[len - 1] == '\n')
		len--;
	raw[len] = '\0';
	if (strcmp(raw, "step") != 0)
		return -EINVAL;

	mutex_lock(&fsl_ramp_lock);
	if (fsl_ramp_busy || fsl_ramp_latched || fsl_ramp_next >= FSL_RAMP_N) {
		mutex_unlock(&fsl_ramp_lock);
		return -EBUSY;
	}
	fsl_ramp_busy = true;
	level = fsl_ramp_arg[fsl_ramp_next];
	strscpy(fsl_ramp_status, "entered\n", sizeof(fsl_ramp_status));
	mutex_unlock(&fsl_ramp_lock);

	status = acpi_get_handle(NULL, FSL_RAMP_PATH, &handle);
	if (ACPI_SUCCESS(status)) {
		arg.type = ACPI_TYPE_INTEGER;
		arg.integer.value = level;
		args.count = 1;
		args.pointer = &arg;
		status = acpi_evaluate_object(handle, NULL, &args, NULL);
	}
	mutex_lock(&fsl_ramp_lock);
	fsl_ramp_busy = false;
	if (ACPI_FAILURE(status)) {
		fsl_ramp_latched = true;
		scnprintf(msg, sizeof(msg), "error 0x%x\n", status);
	} else {
		fsl_ramp_next++;
		scnprintf(msg, sizeof(msg), "ok %llu\n", level);
	}
	strscpy(fsl_ramp_status, msg, sizeof(fsl_ramp_status));
	mutex_unlock(&fsl_ramp_lock);
	return count;
}

static const struct proc_ops fsl_ramp_ops = {
	.proc_read	= fsl_ramp_read,
	.proc_write	= fsl_ramp_write,
	.proc_lseek	= default_llseek,
};

static int __init fsl_ramp_init(void)
{
	fsl_ramp_ent = proc_create("fsl_ramp", 0600, NULL, &fsl_ramp_ops);
	if (!fsl_ramp_ent)
		return -ENOMEM;
	return 0;
}

static void __exit fsl_ramp_exit(void)
{
	proc_remove(fsl_ramp_ent);
}

module_init(fsl_ramp_init);
module_exit(fsl_ramp_exit);

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Research ramp for \\_SB.FAN0._FSL up through argument 237");
MODULE_AUTHOR("research");
