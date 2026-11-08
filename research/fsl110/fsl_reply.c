// SPDX-License-Identifier: GPL-2.0-only
/*
 * Observe the buffer the existing FFH handler already uses.
 * Does not replace that handler and does not evaluate an ACPI method.
 *
 * On entry, qword 0 of the handler's value pointer is compared with
 * 0xC400008D. Other calls are ignored. On return, qwords at offsets 0 and
 * 0x20 are copied out. A low byte of 0x09 in the second qword is stored in
 * the write list. Anything else matching the SMC id is stored in the read
 * list and cannot evict a write. Building against a kernel's headers does
 * not prove that kernel uses the mainline FFH layout. This module does not
 * decide whether the EC kept a fan setpoint.
 */
#include <linux/kernel.h>
#include <linux/kprobes.h>
#include <linux/module.h>
#include <linux/proc_fs.h>
#include <linux/ptrace.h>
#include <linux/slab.h>
#include <linux/spinlock.h>
#include <linux/string.h>
#include <linux/uaccess.h>

#define FSL_REPLY_ID		0xC400008DULL
#define FSL_REPLY_WRITE_OP	0x09ULL
#define FSL_REPLY_WRITES	8
#define FSL_REPLY_READS		32
/* Overridable so a userspace build can force the short-buffer path. */
#ifndef FSL_REPLY_TEXT
#define FSL_REPLY_TEXT		16384
#endif

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
static struct fsl_reply_slot fsl_reply_writes[FSL_REPLY_WRITES];
static struct fsl_reply_slot fsl_reply_reads[FSL_REPLY_READS];
static int fsl_reply_nwrites;
static int fsl_reply_nreads;
static int fsl_reply_drop_w;
static int fsl_reply_drop_r;
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

static void fsl_reply_store(struct fsl_reply_slot *slot)
{
	unsigned long flags;
	bool is_write = (slot->pre_a4 & 0xffULL) == FSL_REPLY_WRITE_OP;

	spin_lock_irqsave(&fsl_reply_lock, flags);
	if (is_write) {
		if (fsl_reply_nwrites < FSL_REPLY_WRITES)
			fsl_reply_writes[fsl_reply_nwrites++] = *slot;
		else
			fsl_reply_drop_w++;
	} else if (fsl_reply_nreads < FSL_REPLY_READS) {
		fsl_reply_reads[fsl_reply_nreads++] = *slot;
	} else {
		fsl_reply_drop_r++;
	}
	spin_unlock_irqrestore(&fsl_reply_lock, flags);
}

static int fsl_reply_ret(struct kretprobe_instance *ri, struct pt_regs *regs)
{
	struct fsl_reply_ctx *ctx = (void *)ri->data;
	struct fsl_reply_slot slot;

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
	fsl_reply_store(&slot);
	return 0;
}

static struct kretprobe fsl_reply_kp = {
	.kp.symbol_name = "acpi_ffh_address_space_arch_handler",
	.entry_handler = fsl_reply_entry,
	.handler = fsl_reply_ret,
	.data_size = sizeof(struct fsl_reply_ctx),
	.maxactive = 16,
};

static int fsl_reply_add(char *tmp, int *n, int *truncated,
			 unsigned long long a, unsigned long long b,
			 unsigned long long c, unsigned long long d,
			 long handler, unsigned int ok, int index, char kind)
{
	int room, wrote;

	if (*truncated)
		return 0;
	room = FSL_REPLY_TEXT - *n;
	if (room < 64) {
		*truncated = 1;
		return 0;
	}
	/* snprintf, not scnprintf: the return is the untruncated length, so
	 * wrote >= room means the line did not fit. A clipped line is not stored.
	 */
	wrote = snprintf(tmp + *n, room,
			 "%c %d pre_a0=%llx pre_a4=%llx post_a0=%llx post_a4=%llx handler=%ld post_ok=%u\n",
			 kind, index, a, b, c, d, handler, ok);
	if (wrote < 0 || wrote >= room) {
		*truncated = 1;
		return 0;
	}
	*n += wrote;
	return 0;
}

static ssize_t fsl_reply_read(struct file *file, char __user *buf, size_t len,
			      loff_t *off)
{
	struct fsl_reply_slot *writes, *reads;
	char *tmp;
	int n = 0;
	int i;
	int truncated = 0;
	unsigned long flags;
	int nwrites, nreads, drop_w, drop_r;
	unsigned long missed;
	ssize_t copied;
	int wrote;

	writes = kmalloc_array(FSL_REPLY_WRITES, sizeof(*writes), GFP_KERNEL);
	reads = kmalloc_array(FSL_REPLY_READS, sizeof(*reads), GFP_KERNEL);
	tmp = kmalloc(FSL_REPLY_TEXT, GFP_KERNEL);
	if (!writes || !reads || !tmp) {
		kfree(writes);
		kfree(reads);
		kfree(tmp);
		return -ENOMEM;
	}
	spin_lock_irqsave(&fsl_reply_lock, flags);
	nwrites = fsl_reply_nwrites;
	nreads = fsl_reply_nreads;
	drop_w = fsl_reply_drop_w;
	drop_r = fsl_reply_drop_r;
	memcpy(writes, fsl_reply_writes, sizeof(*writes) * FSL_REPLY_WRITES);
	memcpy(reads, fsl_reply_reads, sizeof(*reads) * FSL_REPLY_READS);
	spin_unlock_irqrestore(&fsl_reply_lock, flags);
	missed = fsl_reply_kp.nmissed;

	wrote = snprintf(tmp, FSL_REPLY_TEXT,
			 "state %s\nwrites %d\nreads %d\ndropped_writes %d\ndropped_reads %d\nnmissed %lu\n",
			  fsl_reply_armed ? "armed" : "idle",
			  nwrites, nreads, drop_w, drop_r, missed);
	if (wrote < 0 || wrote >= FSL_REPLY_TEXT)
		truncated = 1;
	else
		n = wrote;
	for (i = 0; i < nwrites; i++)
		fsl_reply_add(tmp, &n, &truncated,
			      writes[i].pre_a0, writes[i].pre_a4,
			      writes[i].post_a0, writes[i].post_a4,
			      writes[i].handler, writes[i].post_ok ? 1 : 0,
			      i, 'W');
	for (i = 0; i < nreads; i++)
		fsl_reply_add(tmp, &n, &truncated,
			      reads[i].pre_a0, reads[i].pre_a4,
			      reads[i].post_a0, reads[i].post_a4,
			      reads[i].handler, reads[i].post_ok ? 1 : 0,
			      i, 'R');
	/* Fewer than 32 bytes left: the footer is omitted. A reader that
	 * requires "truncated N" then fails closed. This does not prove
	 * that every buffer size emits a complete record.
	 */
	if (n < FSL_REPLY_TEXT - 32)
		n += snprintf(tmp + n, FSL_REPLY_TEXT - n,
			      "truncated %d\n", truncated);
	copied = simple_read_from_buffer(buf, len, off, tmp, n);
	kfree(writes);
	kfree(reads);
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
