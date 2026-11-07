// SPDX-License-Identifier: GPL-2.0-only
/*
 * One-shot caller for the captured method \_SB.FAN0._FSL, argument 110.
 *
 * Research only. Not part of the fan controller. Load and unload do not
 * evaluate AML. The only evaluation is a write of the exact text "run"
 * to /proc/fsl110, and that write is refused after the first attempt.
 *
 * The return buffer is NULL. This method has no Return operator, and
 * ARM64 builds acpi_strict as 1, so the interpreter does not invent an
 * implicit return. A NULL buffer is the documented acpi_evaluate_object
 * path for "caller does not want the object."
 */
#include <linux/acpi.h>
#include <linux/atomic.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/proc_fs.h>
#include <linux/uaccess.h>

#define FSL110_PATH		"\\_SB.FAN0._FSL"
#define FSL110_ARG		110ULL

static struct proc_dir_entry *fsl110_ent;
static DEFINE_MUTEX(fsl110_lock);
static atomic_t fsl110_armed = ATOMIC_INIT(1);
static char fsl110_status[48] = "idle\n";

static ssize_t fsl110_read(struct file *file, char __user *buf,
			   size_t len, loff_t *off)
{
	char tmp[64];

	mutex_lock(&fsl110_lock);
	strscpy(tmp, fsl110_status, sizeof(tmp));
	mutex_unlock(&fsl110_lock);
	return simple_read_from_buffer(buf, len, off, tmp, strlen(tmp));
}

static void fsl110_set_status(const char *text)
{
	mutex_lock(&fsl110_lock);
	strscpy(fsl110_status, text, sizeof(fsl110_status));
	mutex_unlock(&fsl110_lock);
}

static ssize_t fsl110_write(struct file *file, const char __user *buf,
			    size_t len, loff_t *off)
{
	char raw[8];
	size_t count = len;
	acpi_handle handle;
	acpi_status status;
	union acpi_object arg;
	struct acpi_object_list args;
	char msg[48];

	if (count == 0 || count >= sizeof(raw))
		return -EINVAL;
	if (copy_from_user(raw, buf, len))
		return -EFAULT;
	if (raw[len - 1] == '\n')
		len--;
	raw[len] = '\0';
	if (strcmp(raw, "run") != 0)
		return -EINVAL;
	if (atomic_cmpxchg(&fsl110_armed, 1, 0) != 1)
		return -EBUSY;

	fsl110_set_status("entered\n");

	status = acpi_get_handle(NULL, FSL110_PATH, &handle);
	if (ACPI_FAILURE(status)) {
		scnprintf(msg, sizeof(msg), "error_handle 0x%x\n", status);
		fsl110_set_status(msg);
		return count;
	}

	arg.type = ACPI_TYPE_INTEGER;
	arg.integer.value = FSL110_ARG;
	args.count = 1;
	args.pointer = &arg;
	status = acpi_evaluate_object(handle, NULL, &args, NULL);
	if (ACPI_FAILURE(status))
		scnprintf(msg, sizeof(msg), "error_eval 0x%x\n", status);
	else
		scnprintf(msg, sizeof(msg), "interpreter_ok\n");
	fsl110_set_status(msg);
	return count;
}

static const struct proc_ops fsl110_ops = {
	.proc_read	= fsl110_read,
	.proc_write	= fsl110_write,
	.proc_lseek	= default_llseek,
};

static int __init fsl110_init(void)
{
	fsl110_ent = proc_create("fsl110", 0600, NULL, &fsl110_ops);
	if (!fsl110_ent)
		return -ENOMEM;
	return 0;
}

static void __exit fsl110_exit(void)
{
	proc_remove(fsl110_ent);
}

module_init(fsl110_init);
module_exit(fsl110_exit);

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("One-shot research caller for \\_SB.FAN0._FSL argument 110");
MODULE_AUTHOR("research");
