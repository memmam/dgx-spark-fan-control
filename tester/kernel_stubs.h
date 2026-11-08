/* Userspace stand-ins so research/fsl110/fsl_reply.c can be compiled and
 * called directly. This is not a kernel build and it does not implement
 * kretprobe misses; the guest exercises that path.
 */
#ifndef DGX_TESTER_KERNEL_STUBS_H
#define DGX_TESTER_KERNEL_STUBS_H

#include <errno.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>

typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef uint64_t u64;
typedef pthread_mutex_t spinlock_t;

#define __user
#define __init
#define __exit
#define GFP_KERNEL 0
#define MODULE_LICENSE(x)
#define MODULE_DESCRIPTION(x)
#define MODULE_AUTHOR(x)
#define module_init(fn)
#define module_exit(fn)

#define DEFINE_SPINLOCK(name) pthread_mutex_t name = PTHREAD_MUTEX_INITIALIZER

struct module;
struct proc_dir_entry { int unused; };
struct file;

#define spin_lock_irqsave(lock, flags) \
	do { (flags) = 0; pthread_mutex_lock(lock); } while (0)

static inline void spin_unlock_irqrestore(pthread_mutex_t *lock, unsigned long flags)
{
	(void)flags;
	pthread_mutex_unlock(lock);
}

struct pt_regs { unsigned long unused; };

static __thread u64 *dgx_tls_buf;
static __thread long dgx_tls_handler;
static __thread int dgx_tls_copies;
static __thread int dgx_tls_fail_on;

static inline u64 regs_get_kernel_argument(struct pt_regs *regs, unsigned int index)
{
	(void)regs;
	(void)index;
	return (u64)(uintptr_t)dgx_tls_buf;
}

static inline unsigned long regs_return_value(struct pt_regs *regs)
{
	(void)regs;
	return (unsigned long)dgx_tls_handler;
}

static inline int copy_from_kernel_nofault(void *dst, const void *src, size_t len)
{
	dgx_tls_copies++;
	if (dgx_tls_fail_on && dgx_tls_copies == dgx_tls_fail_on)
		return -EFAULT;
	memcpy(dst, src, len);
	return 0;
}

static inline int scnprintf(char *buf, size_t size, const char *fmt, ...)
{
	va_list ap;
	int wrote;

	if (size == 0)
		return 0;
	va_start(ap, fmt);
	wrote = vsnprintf(buf, size, fmt, ap);
	va_end(ap);
	if (wrote < 0)
		return wrote;
	if ((size_t)wrote >= size)
		return (int)size - 1;
	return wrote;
}

static inline void *kmalloc(size_t size, int flags)
{
	(void)flags;
	return calloc(1, size);
}

static inline void *kmalloc_array(size_t n, size_t size, int flags)
{
	(void)flags;
	return calloc(n, size);
}

static inline void kfree(void *ptr)
{
	free(ptr);
}

static inline ssize_t simple_read_from_buffer(void *to, size_t count, loff_t *ppos,
					     const void *from, size_t available)
{
	loff_t pos;

	if (!ppos || *ppos < 0)
		return -EINVAL;
	pos = *ppos;
	if ((size_t)pos >= available)
		return 0;
	if (count > available - (size_t)pos)
		count = available - (size_t)pos;
	memcpy(to, (const char *)from + pos, count);
	*ppos = pos + (loff_t)count;
	return (ssize_t)count;
}

struct kprobe { const char *symbol_name; };
struct kretprobe_instance { char data[128]; };

struct kretprobe {
	struct kprobe kp;
	int (*entry_handler)(struct kretprobe_instance *, struct pt_regs *);
	int (*handler)(struct kretprobe_instance *, struct pt_regs *);
	size_t data_size;
	int maxactive;
	unsigned long nmissed;
};

static inline int register_kretprobe(struct kretprobe *probe)
{
	(void)probe;
	return 0;
}

static inline void unregister_kretprobe(struct kretprobe *probe)
{
	(void)probe;
}

struct proc_ops {
	ssize_t (*proc_read)(struct file *, char *, size_t, loff_t *);
	ssize_t (*proc_write)(struct file *, const char *, size_t, loff_t *);
	loff_t (*proc_lseek)(struct file *, loff_t, int);
};

static inline loff_t default_llseek(struct file *file, loff_t off, int whence)
{
	(void)file;
	(void)off;
	(void)whence;
	return 0;
}

static inline struct proc_dir_entry *proc_create(const char *name, int mode,
						 void *parent, const struct proc_ops *ops)
{
	(void)name;
	(void)mode;
	(void)parent;
	(void)ops;
	return (struct proc_dir_entry *)1;
}

static inline void proc_remove(struct proc_dir_entry *entry)
{
	(void)entry;
}

#endif
