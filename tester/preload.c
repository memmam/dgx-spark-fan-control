/* Redirect the research proc files to the machine socket.
 * Every other open goes to the real filesystem, which the sandbox has
 * already covered with a private /sys.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <signal.h>
#include <string.h>

__attribute__((constructor))
static void dgx_ignore_sigpipe(void)
{
	signal(SIGPIPE, SIG_IGN);
}
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

static int (*real_openat)(int, const char *, int, ...);
static ssize_t (*real_write)(int, const void *, size_t);
static ssize_t (*real_read)(int, void *, size_t);
static int (*real_close)(int);
static int (*real_dup)(int);
static int (*real_dup2)(int, int);
static size_t (*real_fwrite)(const void *, size_t, size_t, FILE *);

#define DGX_IO_ERR_SEEN 0x20

static int tracked[1024];
static char accum[1024][64];
static unsigned accum_n[1024];

static void init_syms(void)
{
	if (real_openat)
		return;
	real_openat = dlsym(RTLD_NEXT, "openat");
	real_write = dlsym(RTLD_NEXT, "write");
	real_read = dlsym(RTLD_NEXT, "read");
	real_close = dlsym(RTLD_NEXT, "close");
	real_dup = dlsym(RTLD_NEXT, "dup");
	real_dup2 = dlsym(RTLD_NEXT, "dup2");
	real_fwrite = dlsym(RTLD_NEXT, "fwrite");
}

static int virtual_proc(const char *path)
{
	if (!path)
		return 0;
	if (strncmp(path, "/proc/fsl", 9) == 0)
		return 1;
	if (strncmp(path, "/proc/fst_status", 16) == 0)
		return 1;
	return 0;
}

static int connect_sock(void)
{
	const char *path = getenv("DGX_TESTER_SOCK");
	struct sockaddr_un addr;
	int fd;

	if (!path) {
		errno = EIO;
		return -1;
	}
	fd = socket(AF_UNIX, SOCK_STREAM, 0);
	if (fd < 0)
		return -1;
	memset(&addr, 0, sizeof(addr));
	addr.sun_family = AF_UNIX;
	if (strlen(path) >= sizeof(addr.sun_path)) {
		real_close(fd);
		errno = ENAMETOOLONG;
		return -1;
	}
	memcpy(addr.sun_path, path, strlen(path) + 1);
	if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
		int saved = errno;

		real_close(fd);
		errno = saved;
		return -1;
	}
	return fd;
}

static int track(int fd, int kind)
{
	if (fd >= 0 && fd < 1024)
		tracked[fd] = kind;
	return fd;
}

static void copy_track(int from, int to)
{
	if (to < 0 || to >= 1024)
		return;
	tracked[to] = (from >= 0 && from < 1024) ? tracked[from] : 0;
}

int dup(int oldfd)
{
	int fd;

	init_syms();
	fd = real_dup(oldfd);
	if (fd >= 0)
		copy_track(oldfd, fd);
	return fd;
}

int dup2(int oldfd, int newfd)
{
	int fd;

	init_syms();
	fd = real_dup2(oldfd, newfd);
	if (fd >= 0)
		copy_track(oldfd, fd);
	return fd;
}

int openat(int dirfd, const char *path, int flags, ...)
{
	mode_t mode = 0;
	int fd, wr, n;
	char hdr[320];

	init_syms();
	if (flags & O_CREAT) {
		va_list ap;

		va_start(ap, flags);
		mode = (mode_t)va_arg(ap, int);
		va_end(ap);
	}
	if (!virtual_proc(path))
		return real_openat(dirfd, path, flags, mode);
	fd = connect_sock();
	if (fd < 0)
		return -1;
	wr = ((flags & O_ACCMODE) != O_RDONLY);
	n = snprintf(hdr, sizeof(hdr), "%c %s\n", wr ? 'W' : 'R', path);
	if (n < 0 || real_write(fd, hdr, (size_t)n) != n) {
		real_close(fd);
		errno = EIO;
		return -1;
	}
	return track(fd, wr ? 2 : 1);
}

int open(const char *path, int flags, ...)
{
	mode_t mode = 0;

	if (flags & O_CREAT) {
		va_list ap;

		va_start(ap, flags);
		mode = (mode_t)va_arg(ap, int);
		va_end(ap);
	}
	return openat(AT_FDCWD, path, flags, mode);
}

int open64(const char *path, int flags, ...)
{
	mode_t mode = 0;

	if (flags & O_CREAT) {
		va_list ap;

		va_start(ap, flags);
		mode = (mode_t)va_arg(ap, int);
		va_end(ap);
	}
	return open(path, flags, mode);
}

int openat64(int dirfd, const char *path, int flags, ...)
{
	mode_t mode = 0;

	if (flags & O_CREAT) {
		va_list ap;

		va_start(ap, flags);
		mode = (mode_t)va_arg(ap, int);
		va_end(ap);
	}
	return openat(dirfd, path, flags, mode);
}

static ssize_t protocol_write(int fd, const void *buf, size_t len)
{
	char hdr[64];
	char ack = 0;
	size_t off = 0;
	int n;

	n = snprintf(hdr, sizeof(hdr), "B %zu\n", len);
	if (n < 0 || real_write(fd, hdr, (size_t)n) != n) {
		errno = EIO;
		return -1;
	}
	while (off < len) {
		ssize_t wrote = real_write(fd, (const char *)buf + off, len - off);

		if (wrote < 0)
			return -1;
		off += (size_t)wrote;
	}
	if (real_read(fd, &ack, 1) != 1) {
		errno = EIO;
		return -1;
	}
	if (ack == 'E') {
		errno = EBUSY;
		return -1;
	}
	if (ack != '0') {
		errno = EIO;
		return -1;
	}
	return (ssize_t)len;
}

static int flush_accum(int fd)
{
	ssize_t wrote;

	if (fd < 0 || fd >= 1024 || accum_n[fd] == 0)
		return 0;
	wrote = protocol_write(fd, accum[fd], accum_n[fd]);
	accum_n[fd] = 0;
	return wrote < 0 ? -1 : 0;
}

ssize_t write(int fd, const void *buf, size_t len)
{
	init_syms();
	if (fd < 0 || fd >= 1024 || tracked[fd] != 2)
		return real_write(fd, buf, len);
	return protocol_write(fd, buf, len);
}

size_t fwrite(const void *ptr, size_t size, size_t nmemb, FILE *stream)
{
	int fd;
	size_t bytes;

	init_syms();
	fd = fileno(stream);
	bytes = size * nmemb;
	if (fd >= 0 && fd < 1024 && tracked[fd] == 2) {
		if (bytes == 0)
			return 0;
		if (flush_accum(fd) < 0 || protocol_write(fd, ptr, bytes) < 0)
			return 0;
		return nmemb;
	}
	return real_fwrite(ptr, size, nmemb, stream);
}

int fputc(int c, FILE *stream)
{
	int fd;

	init_syms();
	fd = fileno(stream);
	if (fd < 0 || fd >= 1024 || tracked[fd] != 2)
		return ((int (*)(int, FILE *))dlsym(RTLD_NEXT, "fputc"))(c, stream);
	if (accum_n[fd] < sizeof(accum[0]))
		accum[fd][accum_n[fd]++] = (char)c;
	if (c == '\n' || accum_n[fd] == sizeof(accum[0])) {
		if (flush_accum(fd) < 0)
			return EOF;
	}
	return (unsigned char)c;
}

#undef putc
int putc(int c, FILE *stream)
{
	return fputc(c, stream);
}

int __printf_chk(int flag, const char *fmt, ...)
{
	va_list ap;
	char buf[512];
	int fd, wrote;

	(void)flag;
	init_syms();
	fd = fileno(stdout);
	va_start(ap, fmt);
	if (fd >= 0 && fd < 1024 && tracked[fd] == 2) {
		wrote = vsnprintf(buf, sizeof(buf), fmt, ap);
		va_end(ap);
		if (wrote < 0)
			return -1;
		if (wrote >= (int)sizeof(buf))
			wrote = (int)sizeof(buf) - 1;
		if (flush_accum(fd) < 0 || protocol_write(fd, buf, (size_t)wrote) < 0) {
			stdout->_flags |= DGX_IO_ERR_SEEN;
			return -1;
		}
		return wrote;
	}
	wrote = vfprintf(stdout, fmt, ap);
	va_end(ap);
	return wrote;
}

int fflush(FILE *stream)
{
	init_syms();
	if (stream != NULL) {
		int fd = fileno(stream);

		if (fd >= 0 && fd < 1024 && tracked[fd] == 2 && flush_accum(fd) < 0)
			return EOF;
	}
	return ((int (*)(FILE *))dlsym(RTLD_NEXT, "fflush"))(stream);
}

int close(int fd)
{
	init_syms();
	if (fd >= 0 && fd < 1024) {
		if (tracked[fd] == 2)
			flush_accum(fd);
		tracked[fd] = 0;
		accum_n[fd] = 0;
	}
	return real_close(fd);
}
