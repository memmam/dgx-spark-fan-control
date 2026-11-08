/* PID 1 for the virt guest. Prints one line the host can grep. */
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

static void mount_or_die(const char *source, const char *target, const char *type)
{
	if (mount(source, target, type, 0, NULL) != 0) {
		dprintf(STDOUT_FILENO, "GUEST_MOUNT_FAIL %s %d\n", target, errno);
		_exit(1);
	}
}

static int load_module(const char *path)
{
	int fd = open(path, O_RDONLY);
	int rc;

	if (fd < 0)
		return -1;
	rc = syscall(SYS_finit_module, fd, "", 0);
	close(fd);
	return rc;
}

static int write_text(const char *path, const char *text)
{
	int fd = open(path, O_WRONLY);
	ssize_t n;

	if (fd < 0)
		return -1;
	n = write(fd, text, strlen(text));
	close(fd);
	return n == (ssize_t)strlen(text) ? 0 : -1;
}

static void read_into(const char *path, char *buf, size_t len)
{
	int fd = open(path, O_RDONLY);
	ssize_t n;

	buf[0] = '\0';
	if (fd < 0)
		return;
	n = read(fd, buf, len - 1);
	close(fd);
	if (n < 0)
		n = 0;
	buf[n] = '\0';
	for (ssize_t i = 0; i < n; i++) {
		if (buf[i] == '\n')
			buf[i] = '|';
	}
}

int main(void)
{
	char status[128];
	char reply[512];
	int probe;
	int reply_mod;

	mkdir("/proc", 0755);
	mkdir("/sys", 0755);
	mount_or_die("proc", "/proc", "proc");
	mount_or_die("sysfs", "/sys", "sysfs");
	reply_mod = load_module("/fsl_reply.ko");
	if (reply_mod != 0)
		dprintf(STDOUT_FILENO, "GUEST_REPLY_ERRNO %d\n", errno);
	probe = load_module("/fsl_probe.ko");
	if (probe != 0)
		dprintf(STDOUT_FILENO, "GUEST_PROBE_ERRNO %d\n", errno);
	if (write_text("/proc/fsl_probe", "run\n") != 0)
		dprintf(STDOUT_FILENO, "GUEST_WRITE_FAIL %d\n", errno);
	read_into("/proc/fsl_probe", status, sizeof(status));
	read_into("/proc/fsl_reply", reply, sizeof(reply));
	dprintf(STDOUT_FILENO,
		"GUEST_RESULT probe_load=%d reply_load=%d status=%s reply=%s\n",
		probe, reply_mod, status, reply);
	if (probe == 0 && reply_mod == 0 && strstr(status, "interpreter_ok") &&
	    strstr(reply, "state armed"))
		dprintf(STDOUT_FILENO, "GUEST_OK\n");
	else
		dprintf(STDOUT_FILENO, "GUEST_FAIL\n");
	dprintf(STDOUT_FILENO, "GUEST_HALT\n");
	/* LINUX_REBOOT_MAGIC1, MAGIC2, CMD_POWER_OFF */
	syscall(SYS_reboot, 0xfee1dead, 672274793, 0x4321fedc, NULL);
	for (;;)
		pause();
}
