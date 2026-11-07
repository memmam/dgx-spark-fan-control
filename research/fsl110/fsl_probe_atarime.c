// SPDX-License-Identifier: GPL-2.0-only
/*
 * Atarime research probe. One hardcoded \_SB.FAN0._FSL argument: 120.
 * The request is 5025. Write the exact text "run" once. Load and unload
 * do not evaluate AML. The return buffer is NULL.
 */
#include <linux/acpi.h>
#include <linux/atomic.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/proc_fs.h>
#include <linux/uaccess.h>

#define FSL120_PATH		"\\_SB.FAN0._FSL"
#define FSL120_ARG		120ULL

static struct proc_dir_entry *fsl120_ent;
static DEFINE_MUTEX(fsl120_lock);
static atomic_t fsl120_armed = ATOMIC_INIT(1);
static char fsl120_status[48] = "idle\n";

static ssize_t fsl120_read(struct file *file, char __user *buf,
			   size_t len, loff_t *off)
{
	char tmp[48];

	mutex_lock(&fsl120_lock);
	strscpy(tmp, fsl120_status, sizeof(tmp));
	mutex_unlock(&fsl120_lock);
	return simple_read_from_buffer(buf, len, off, tmp, strlen(tmp));
}

static void fsl120_set_status(const char *text)
{
	mutex_lock(&fsl120_lock);
	strscpy(fsl120_status, text, sizeof(fsl120_status));
	mutex_unlock(&fsl120_lock);
}

static ssize_t fsl120_write(struct file *file, const char __user *buf,
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
	if (atomic_cmpxchg(&fsl120_armed, 1, 0) != 1)
		return -EBUSY;

	fsl120_set_status("entered\n");
	status = acpi_get_handle(NULL, FSL120_PATH, &handle);
	if (ACPI_FAILURE(status)) {
		scnprintf(msg, sizeof(msg), "error_handle 0x%x\n", status);
		fsl120_set_status(msg);
		return count;
	}
	arg.type = ACPI_TYPE_INTEGER;
	arg.integer.value = FSL120_ARG;
	args.count = 1;
	args.pointer = &arg;
	status = acpi_evaluate_object(handle, NULL, &args, NULL);
	if (ACPI_FAILURE(status))
		scnprintf(msg, sizeof(msg), "error_eval 0x%x\n", status);
	else
		scnprintf(msg, sizeof(msg), "interpreter_ok\n");
	fsl120_set_status(msg);
	return count;
}

static const struct proc_ops fsl120_ops = {
	.proc_read	= fsl120_read,
	.proc_write	= fsl120_write,
	.proc_lseek	= default_llseek,
};

static int __init fsl120_init(void)
{
	fsl120_ent = proc_create("fsl120_atarime", 0600, NULL, &fsl120_ops);
	if (!fsl120_ent)
		return -ENOMEM;
	return 0;
}

static void __exit fsl120_exit(void)
{
	proc_remove(fsl120_ent);
}

module_init(fsl120_init);
module_exit(fsl120_exit);

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Atarime research caller for \\_SB.FAN0._FSL argument 120");
MODULE_AUTHOR("research");
