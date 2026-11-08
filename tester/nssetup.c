/* Mounts that exist only inside the tester's user namespace.
 *
 * /lib/modules is created on a private tmpfs that covers the parent of that
 * path. The host directory is not created and not written. /sys is two
 * tmpfs layers under the fixture, so one or two umounts still do not show
 * host sysfs. /tmp is a private tmpfs. /etc is a read-only bind.
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/utsname.h>
#include <unistd.h>

static void die(const char *what)
{
	fprintf(stderr, "nssetup: %s: %s\n", what, strerror(errno));
	_exit(125);
}

static void join2(char *dst, size_t dstsz, const char *a, const char *b)
{
	int n = snprintf(dst, dstsz, "%s/%s", a, b);

	if (n < 0 || (size_t)n >= dstsz)
		die("path");
}

static void xmount(const char *src, const char *tgt, const char *type,
		   unsigned long flags, const char *data)
{
	if (mount(src, tgt, type, flags, data) != 0)
		die(tgt);
}

static void place(const char *src, const char *dst)
{
	struct stat st;
	char link[PATH_MAX];
	ssize_t n;
	int fd;

	if (lstat(src, &st) != 0)
		die(src);
	if (S_ISLNK(st.st_mode)) {
		n = readlink(src, link, sizeof(link) - 1);
		if (n < 0)
			die(src);
		link[n] = 0;
		if (symlink(link, dst) != 0)
			die(dst);
		return;
	}
	if (S_ISDIR(st.st_mode)) {
		if (mkdir(dst, 0755) != 0)
			die(dst);
		xmount(src, dst, NULL, MS_BIND, NULL);
		return;
	}
	fd = open(dst, O_CREAT | O_WRONLY, 0644);
	if (fd < 0)
		die(dst);
	close(fd);
	xmount(src, dst, NULL, MS_BIND, NULL);
}

static void bind_children(const char *from, const char *to, int skip_modules)
{
	DIR *dir;
	struct dirent *ent;

	dir = opendir(from);
	if (!dir)
		die(from);
	while ((ent = readdir(dir)) != NULL) {
		char src[PATH_MAX];
		char dst[PATH_MAX];

		if (ent->d_name[0] == '.' &&
		    (ent->d_name[1] == 0 ||
		     (ent->d_name[1] == '.' && ent->d_name[2] == 0)))
			continue;
		if (skip_modules && strcmp(ent->d_name, "modules") == 0)
			continue;
		join2(src, sizeof(src), from, ent->d_name);
		join2(dst, sizeof(dst), to, ent->d_name);
		place(src, dst);
	}
	closedir(dir);
}

static void modules_parent(char *out, size_t outsz)
{
	struct stat st;
	char target[PATH_MAX];
	ssize_t n;

	if (lstat("/lib", &st) != 0)
		die("/lib");
	if (S_ISLNK(st.st_mode)) {
		n = readlink("/lib", target, sizeof(target) - 1);
		if (n < 0)
			die("readlink /lib");
		target[n] = 0;
		if (target[0] == '/') {
			if ((size_t)n >= outsz)
				die("lib link");
			memcpy(out, target, (size_t)n + 1);
		} else if (snprintf(out, outsz, "/%s", target) >= (int)outsz) {
			die("lib link");
		}
		return;
	}
	if (!S_ISDIR(st.st_mode))
		die("/lib type");
	if (snprintf(out, outsz, "/lib") >= (int)outsz)
		die("/lib");
}

static void setup_modules(const char *state)
{
	char kdir[PATH_MAX];
	char view[PATH_MAX];
	char parent[PATH_MAX];
	char modules[PATH_MAX];
	char release_dir[PATH_MAX];
	char build[PATH_MAX];
	struct stat st;
	struct utsname uts;

	join2(kdir, sizeof(kdir), state, "kdir");
	if (lstat(kdir, &st) != 0)
		return;
	if (!S_ISDIR(st.st_mode))
		return;
	modules_parent(parent, sizeof(parent));
	join2(view, sizeof(view), state, "lib-view");
	if (mkdir(view, 0755) != 0 && errno != EEXIST)
		die(view);
	xmount(parent, view, NULL, MS_BIND, NULL);
	xmount("tmpfs", parent, "tmpfs", 0, "mode=755");
	bind_children(view, parent, 1);
	join2(modules, sizeof(modules), parent, "modules");
	if (mkdir(modules, 0755) != 0)
		die(modules);
	if (uname(&uts) != 0)
		die("uname");
	join2(release_dir, sizeof(release_dir), modules, uts.release);
	if (mkdir(release_dir, 0755) != 0)
		die(release_dir);
	join2(build, sizeof(build), release_dir, "build");
	if (mkdir(build, 0755) != 0)
		die(build);
	xmount(kdir, build, NULL, MS_BIND, NULL);
}

static void setup_sys(const char *state)
{
	char fixture[PATH_MAX];

	xmount("tmpfs", "/sys", "tmpfs", 0, "mode=555");
	xmount("tmpfs", "/sys", "tmpfs", 0, "mode=555");
	join2(fixture, sizeof(fixture), state, "sys");
	xmount(fixture, "/sys", NULL, MS_BIND, NULL);
}

static void setup_proc(const char *state)
{
	char fixture[PATH_MAX];

	join2(fixture, sizeof(fixture), state, "proc_modules");
	xmount(fixture, "/proc/modules", NULL, MS_BIND, NULL);
}

static void setup_var(void)
{
	xmount("tmpfs", "/var/lib", "tmpfs", 0, "mode=755");
	if (mkdir("/var/lib/dgx-fan-control", 0755) != 0)
		die("/var/lib/dgx-fan-control");
}

static void setup_tmp(void)
{
	xmount("tmpfs", "/tmp", "tmpfs", 0, "mode=1777");
}

static void setup_etc(void)
{
	xmount("/etc", "/etc", NULL, MS_BIND, NULL);
	xmount(NULL, "/etc", NULL, MS_BIND | MS_REMOUNT | MS_RDONLY, NULL);
}

int main(int argc, char **argv)
{
	if (argc != 2) {
		fprintf(stderr, "nssetup: usage: nssetup STATE\n");
		return 125;
	}
	if (mount(NULL, "/", NULL, MS_REC | MS_PRIVATE, NULL) != 0)
		die("private /");
	setup_sys(argv[1]);
	setup_proc(argv[1]);
	setup_var();
	setup_tmp();
	setup_etc();
	setup_modules(argv[1]);
	return 0;
}
