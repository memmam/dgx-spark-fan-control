// SPDX-License-Identifier: GPL-2.0-only
/*
 * Read \_SB.FAN0._FST and \_SB.FAN1._FST once and print the package.
 * Does not call the fan set-level method. Does not issue an SMC of its own.
 * The _FST package
 * is revision, cached control, and speed. It is not the WRB reply from a
 * previous write: that buffer is local to WRB and is not returned.
 */
#include <linux/acpi.h>
#include <linux/atomic.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/proc_fs.h>
#include <linux/slab.h>
#include <linux/uaccess.h>

static struct proc_dir_entry *fst_ent;
static DEFINE_MUTEX(fst_lock);
static atomic_t fst_armed = ATOMIC_INIT(1);
static char fst_status[192] = "idle\n";

static void fst_set(const char *text)
{
	mutex_lock(&fst_lock);
	strscpy(fst_status, text, sizeof(fst_status));
	mutex_unlock(&fst_lock);
}

static int fst_format_one(const char *path, char *out, size_t outlen)
{
	acpi_handle handle;
	acpi_status status;
	struct acpi_buffer buf = { ACPI_ALLOCATE_BUFFER, NULL };
	union acpi_object *obj;
	int n;

	status = acpi_get_handle(NULL, (acpi_string)path, &handle);
	if (ACPI_FAILURE(status))
		return scnprintf(out, outlen, "%s error_handle 0x%x\n", path, status);
	status = acpi_evaluate_object(handle, NULL, NULL, &buf);
	if (ACPI_FAILURE(status)) {
		kfree(buf.pointer);
		return scnprintf(out, outlen, "%s error_eval 0x%x\n", path, status);
	}
	obj = buf.pointer;
	if (!obj || obj->type != ACPI_TYPE_PACKAGE || obj->package.count != 3) {
		n = scnprintf(out, outlen, "%s unexpected_type\n", path);
		kfree(buf.pointer);
		return n;
	}
	n = scnprintf(out, outlen, "%s", path);
	{
		int i;
		for (i = 0; i < 3; i++) {
			union acpi_object *el = &obj->package.elements[i];
			if (el->type != ACPI_TYPE_INTEGER)
				n += scnprintf(out + n, outlen - n, " bad");
			else
				n += scnprintf(out + n, outlen - n, " %llu",
					       el->integer.value);
		}
	}
	n += scnprintf(out + n, outlen - n, "\n");
	kfree(buf.pointer);
	return n;
}

static ssize_t fst_read(struct file *file, char __user *buf, size_t len, loff_t *off)
{
	char tmp[192];

	mutex_lock(&fst_lock);
	strscpy(tmp, fst_status, sizeof(tmp));
	mutex_unlock(&fst_lock);
	return simple_read_from_buffer(buf, len, off, tmp, strlen(tmp));
}

static ssize_t fst_write(struct file *file, const char __user *buf, size_t len, loff_t *off)
{
	char raw[8];
	size_t count = len;
	char msg[192];
	int n;

	if (count == 0 || count >= sizeof(raw))
		return -EINVAL;
	if (copy_from_user(raw, buf, len))
		return -EFAULT;
	if (raw[len - 1] == '\n')
		len--;
	raw[len] = '\0';
	if (strcmp(raw, "read") != 0)
		return -EINVAL;
	if (atomic_cmpxchg(&fst_armed, 1, 0) != 1)
		return -EBUSY;

	n = fst_format_one("\\_SB.FAN0._FST", msg, sizeof(msg));
	if (n > 0 && (size_t)n < sizeof(msg))
		fst_format_one("\\_SB.FAN1._FST", msg + n, sizeof(msg) - n);
	fst_set(msg);
	return count;
}

static const struct proc_ops fst_ops = {
	.proc_read = fst_read,
	.proc_write = fst_write,
	.proc_lseek = default_llseek,
};

static int __init fst_init(void)
{
	fst_ent = proc_create("fst_status_atarime", 0600, NULL, &fst_ops);
	if (!fst_ent)
		return -ENOMEM;
	return 0;
}

static void __exit fst_exit(void)
{
	proc_remove(fst_ent);
}

module_init(fst_init);
module_exit(fst_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Read-only _FST package capture for Atarime research");
MODULE_AUTHOR("research");
