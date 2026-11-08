// SPDX-License-Identifier: GPL-2.0-only
/*
 * Observe the buffer the existing FFH handler already uses.
 * Does not replace that handler and does not evaluate an ACPI method.
 *
 * On entry, qword 0 of the handler's value pointer is compared with
 * 0xC400008D. Other calls are ignored. On return, qwords at offsets 0 and
 * 0x20 are copied out. Mainline arm64 FFH offset 1 leaves the SMCCC result
 * in that buffer before it returns. This module does not decide whether
 * the EC kept a fan setpoint.
 */
#include <linux/kernel.h>
#include <linux/slab.h>
#include <linux/string.h>
#include <linux/kprobes.h>
#include <linux/module.h>
#include <linux/proc_fs.h>
#include <linux/ptrace.h>
#include <linux/spinlock.h>
#include <linux/uaccess.h>

#define FSL_REPLY_ID		0xC400008DULL
#define FSL_REPLY_SLOTS		8

struct fsl_reply_ctx {
	bool match;
	u64 *buf;
	u64 pre_a0;
	u64 pre_a4;
};

struct fsl_reply_slot {
	u64 pre_a0;
	u64 pre_a4;
	u64 post_a0;
	u64 post_a4;
	long handler;
	bool post_ok;
};

static struct proc_dir_entry *fsl_reply_ent;
static DEFINE_SPINLOCK(fsl_reply_lock);
static struct fsl_reply_slot fsl_reply_slots[FSL_REPLY_SLOTS];
static int fsl_reply_count;
static int fsl_reply_dropped;
static bool fsl_reply_armed;

static int fsl_reply_entry(struct kretprobe_instance *ri, struct pt_regs *regs)
{
	struct fsl_reply_ctx *ctx = (void *)ri->data;
	u64 *buf;
	u64 a0, a4;

	ctx->match = false;
	ctx->buf = NULL;
	buf = (u64 *)regs_get_kernel_argument(regs, 0);
	if (!buf)
		return 1;
	if (copy_from_kernel_nofault(&a0, buf, sizeof(a0)))
		return 1;
	if (a0 != FSL_REPLY_ID)
		return 1;
	if (copy_from_kernel_nofault(&a4, buf + 4, sizeof(a4)))
		return 1;
	ctx->match = true;
	ctx->buf = buf;
	ctx->pre_a0 = a0;
	ctx->pre_a4 = a4;
	return 0;
}

static int fsl_reply_ret(struct kretprobe_instance *ri, struct pt_regs *regs)
{
	struct fsl_reply_ctx *ctx = (void *)ri->data;
	struct fsl_reply_slot slot;
	unsigned long flags;

	if (!ctx->match || !ctx->buf)
		return 0;
	slot.pre_a0 = ctx->pre_a0;
	slot.pre_a4 = ctx->pre_a4;
	slot.handler = regs_return_value(regs);
	slot.post_ok = !copy_from_kernel_nofault(&slot.post_a0, ctx->buf,
						 sizeof(slot.post_a0)) &&
		       !copy_from_kernel_nofault(&slot.post_a4, ctx->buf + 4,
						 sizeof(slot.post_a4));
	if (!slot.post_ok) {
		slot.post_a0 = 0;
		slot.post_a4 = 0;
	}
	spin_lock_irqsave(&fsl_reply_lock, flags);
	if (fsl_reply_count < FSL_REPLY_SLOTS)
		fsl_reply_slots[fsl_reply_count++] = slot;
	else
		fsl_reply_dropped++;
	spin_unlock_irqrestore(&fsl_reply_lock, flags);
	return 0;
}

static struct kretprobe fsl_reply_kp = {
	.kp.symbol_name = "acpi_ffh_address_space_arch_handler",
	.entry_handler = fsl_reply_entry,
	.handler = fsl_reply_ret,
	.data_size = sizeof(struct fsl_reply_ctx),
	.maxactive = FSL_REPLY_SLOTS,
};

static ssize_t fsl_reply_read(struct file *file, char __user *buf, size_t len,
			      loff_t *off)
{
	struct fsl_reply_slot *slots;
	char *tmp;
	int n = 0;
	int i;
	unsigned long flags;
	int count, dropped;
	ssize_t copied;

	slots = kmalloc_array(FSL_REPLY_SLOTS, sizeof(*slots), GFP_KERNEL);
	tmp = kmalloc(1024, GFP_KERNEL);
	if (!slots || !tmp) {
		kfree(slots);
		kfree(tmp);
		return -ENOMEM;
	}
	spin_lock_irqsave(&fsl_reply_lock, flags);
	count = fsl_reply_count;
	dropped = fsl_reply_dropped;
	memcpy(slots, fsl_reply_slots, sizeof(*slots) * FSL_REPLY_SLOTS);
	spin_unlock_irqrestore(&fsl_reply_lock, flags);

	n = scnprintf(tmp, 1024, "state %s\ncount %d\ndropped %d\n",
		      fsl_reply_armed ? "armed" : "idle", count, dropped);
	for (i = 0; i < count && n < 1024; i++) {
		n += scnprintf(tmp + n, 1024 - n,
			       "%d pre_a0=%llx pre_a4=%llx post_a0=%llx post_a4=%llx handler=%ld post_ok=%u\n",
			       i,
			       (unsigned long long)slots[i].pre_a0,
			       (unsigned long long)slots[i].pre_a4,
			       (unsigned long long)slots[i].post_a0,
			       (unsigned long long)slots[i].post_a4,
			       slots[i].handler, slots[i].post_ok ? 1 : 0);
	}
	copied = simple_read_from_buffer(buf, len, off, tmp, n);
	kfree(slots);
	kfree(tmp);
	return copied;
}

static ssize_t fsl_reply_write(struct file *file, const char __user *buf,
			       size_t len, loff_t *off)
{
	return -EINVAL;
}

static const struct proc_ops fsl_reply_ops = {
	.proc_read = fsl_reply_read,
	.proc_write = fsl_reply_write,
	.proc_lseek = default_llseek,
};

static int __init fsl_reply_init(void)
{
	int ret;

	ret = register_kretprobe(&fsl_reply_kp);
	if (ret)
		return ret;
	fsl_reply_ent = proc_create("fsl_reply", 0400, NULL, &fsl_reply_ops);
	if (!fsl_reply_ent) {
		unregister_kretprobe(&fsl_reply_kp);
		return -ENOMEM;
	}
	fsl_reply_armed = true;
	return 0;
}

static void __exit fsl_reply_exit(void)
{
	fsl_reply_armed = false;
	proc_remove(fsl_reply_ent);
	unregister_kretprobe(&fsl_reply_kp);
}

module_init(fsl_reply_init);
module_exit(fsl_reply_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Observe FFH replies for one SMC id without issuing a fan write");
MODULE_AUTHOR("research");
