/* Drive the real fsl_reply.c functions. The .c file is pulled in with -include. */
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct job {
	u64 pre_a4;
};

static void one_event(u64 pre_a4, u64 post_a0, u64 post_a4, int fail_on, long handler)
{
	u64 buf[8];
	struct pt_regs regs;
	struct kretprobe_instance ri;

	memset(buf, 0, sizeof(buf));
	memset(&regs, 0, sizeof(regs));
	memset(&ri, 0, sizeof(ri));
	buf[0] = 0xC400008DULL;
	buf[4] = pre_a4;
	dgx_tls_buf = buf;
	dgx_tls_handler = handler;
	dgx_tls_copies = 0;
	dgx_tls_fail_on = 0;
	if (fsl_reply_entry(&ri, &regs) != 0)
		return;
	buf[0] = post_a0;
	buf[4] = post_a4;
	/* fail_on is counted only on the post copies. 1 fails post_a0. */
	dgx_tls_copies = 0;
	dgx_tls_fail_on = fail_on;
	fsl_reply_ret(&ri, &regs);
}

static void *worker(void *arg)
{
	struct job *job = arg;

	one_event(job->pre_a4, 0xC400008EULL, 0, 0, 0);
	return NULL;
}

static int dump(void)
{
	char out[65536];
	loff_t off = 0;
	ssize_t n = fsl_reply_read(NULL, out, sizeof(out) - 1, &off);

	if (n < 0) {
		fprintf(stderr, "proc read failed\n");
		return 1;
	}
	fwrite(out, 1, (size_t)n, stdout);
	return 0;
}

int main(void)
{
	char cmd[64];

	if (fsl_reply_init() != 0) {
		fprintf(stderr, "init failed\n");
		return 1;
	}
	while (scanf("%63s", cmd) == 1) {
		if (strcmp(cmd, "event") == 0) {
			unsigned long long a4, post0, post4;
			int fail_on, handler;

			if (scanf("%llx %llx %llx %d %d", &a4, &post0, &post4, &fail_on, &handler) != 5)
				return 2;
			one_event(a4, post0, post4, fail_on, handler);
		} else if (strcmp(cmd, "missed") == 0) {
			unsigned long missed;

			if (scanf("%lu", &missed) != 1)
				return 2;
			fsl_reply_kp.nmissed = missed;
		} else if (strcmp(cmd, "concurrent") == 0) {
			int count, i;
			pthread_t *threads;
			struct job *jobs;

			if (scanf("%d", &count) != 1 || count < 1 || count > 64)
				return 2;
			threads = calloc((size_t)count, sizeof(*threads));
			jobs = calloc((size_t)count, sizeof(*jobs));
			if (!threads || !jobs)
				return 1;
			for (i = 0; i < count; i++) {
				jobs[i].pre_a4 = ((u64)(i + 1) << 8) | 0x09ULL;
				if (pthread_create(&threads[i], NULL, worker, &jobs[i]) != 0)
					return 1;
			}
			for (i = 0; i < count; i++)
				pthread_join(threads[i], NULL);
			free(threads);
			free(jobs);
		} else if (strcmp(cmd, "dump") == 0) {
			if (dump() != 0)
				return 1;
		} else {
			fprintf(stderr, "unknown %s\n", cmd);
			return 2;
		}
	}
	return 0;
}
