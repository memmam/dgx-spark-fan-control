/* Small PATH stand-ins. A Python process per sample made a full ramp take
 * longer than the virtual clock it was supposed to replace.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <libgen.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

static char response[65536];

static int ask(const char *command)
{
	const char *path = getenv("DGX_TESTER_SOCK");
	struct sockaddr_un addr;
	int fd, n;
	size_t used = 0;
	char header[700];

	if (!path)
		return -1;
	fd = socket(AF_UNIX, SOCK_STREAM, 0);
	if (fd < 0)
		return -1;
	memset(&addr, 0, sizeof(addr));
	addr.sun_family = AF_UNIX;
	snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", path);
	if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
		close(fd);
		return -1;
	}
	n = snprintf(header, sizeof(header), "CMD %s\n", command);
	if (write(fd, header, (size_t)n) != n) {
		close(fd);
		return -1;
	}
	while (used + 1 < sizeof(response)) {
		ssize_t got = read(fd, response + used, sizeof(response) - 1 - used);

		if (got < 0) {
			close(fd);
			return -1;
		}
		if (got == 0)
			break;
		used += (size_t)got;
	}
	response[used] = '\0';
	close(fd);
	return 0;
}

static int write_text(const char *path, const char *text)
{
	FILE *file = fopen(path, "w");

	if (!file)
		return -1;
	fputs(text, file);
	fclose(file);
	return 0;
}

int main(int argc, char **argv)
{
	char *name = basename(argv[0]);
	const char *state = getenv("DGX_TESTER_STATE");

	if (strcmp(name, "cat") == 0 && argc == 2) {
		if (strstr(argv[1], "fan1_input") || strstr(argv[1], "fan2_input")) {
			char command[PATH_MAX + 8];

			snprintf(command, sizeof(command), "fan %s", argv[1]);
			if (ask(command) != 0)
				return 1;
			fputs(response, stdout);
			if (response[0] && response[strlen(response) - 1] != '\n')
				fputc('\n', stdout);
			return 0;
		}
		execv("/usr/bin/cat", argv);
		return 127;
	}
	if (strcmp(name, "sleep") == 0) {
		char command[64];

		snprintf(command, sizeof(command), "sleep %s", argc > 1 ? argv[1] : "0");
		return ask(command) == 0 ? 0 : 1;
	}
	if (strcmp(name, "nvidia-smi") == 0) {
		if (ask("nvidia") != 0)
			return 1;
		if (strncmp(response, "FAIL", 4) == 0)
			return 1;
		fputs(response, stdout);
		if (response[0] && response[strlen(response) - 1] != '\n')
			fputc('\n', stdout);
		return 0;
	}
	if (strcmp(name, "insmod") == 0 && argc == 2) {
		char resolved[PATH_MAX];
		char command[PATH_MAX + 16];

		if (!realpath(argv[1], resolved))
			return 1;
		snprintf(command, sizeof(command), "insmod %s", resolved);
		if (ask(command) != 0)
			return 1;
		if (strncmp(response, "ERR", 3) == 0)
			return 1;
		return 0;
	}
	if (strcmp(name, "rmmod") == 0 && argc == 2) {
		char command[128];

		snprintf(command, sizeof(command), "rmmod %s", argv[1]);
		return ask(command) == 0 ? 0 : 1;
	}
	if (strcmp(name, "lsmod") == 0) {
		if (ask("lsmod") != 0)
			return 1;
		fputs(response, stdout);
		return 0;
	}
	if (strcmp(name, "modinfo") == 0) {
		const char *field = NULL;
		const char *target = NULL;
		char path[PATH_MAX];
		FILE *file;
		int i;

		for (i = 1; i < argc; i++) {
			if (strcmp(argv[i], "-F") == 0 && i + 1 < argc) {
				field = argv[++i];
				continue;
			}
			target = argv[i];
		}
		if (!field || !target)
			return 1;
		snprintf(path, sizeof(path), "%s.%s", target, field);
		file = fopen(path, "r");
		if (!file)
			return 1;
		while (fgets(response, sizeof(response), file))
			fputs(response, stdout);
		fclose(file);
		return 0;
	}
	if (strcmp(name, "systemctl") == 0) {
		int i;
		const char *verb = NULL;

		for (i = 1; i < argc; i++) {
			if (argv[i][0] == '-')
				continue;
			verb = argv[i];
			break;
		}
		if (verb == NULL) {
			fprintf(stderr, "systemctl: missing command\n");
			return 1;
		}
		if (strcmp(verb, "is-active") == 0) {
			puts("inactive");
			return 0;
		}
		if (strcmp(verb, "is-enabled") == 0) {
			puts("disabled");
			return 0;
		}
		/* enable and disable are the verbs set-state and module use.
		 * show is what the daemon reads. Empty show output means the
		 * property is unknown, which is not a fabricated value.
		 */
		if (strcmp(verb, "enable") == 0 || strcmp(verb, "disable") == 0 ||
		    strcmp(verb, "show") == 0)
			return 0;
		fprintf(stderr, "systemctl: unknown command %s\n", verb);
		return 1;
	}
	if (strcmp(name, "fwupdmgr") == 0) {
		char path[PATH_MAX];
		FILE *file;

		snprintf(path, sizeof(path), "%s/fwupd.json", state ? state : "");
		file = fopen(path, "r");
		if (!file)
			return 1;
		while (fgets(response, sizeof(response), file))
			fputs(response, stdout);
		fclose(file);
		return 0;
	}
	if (strcmp(name, "dmesg") == 0) {
		char path[PATH_MAX];
		FILE *file;

		snprintf(path, sizeof(path), "%s/dmesg.txt", state ? state : "");
		file = fopen(path, "r");
		if (!file)
			return 0;
		while (fgets(response, sizeof(response), file))
			fputs(response, stdout);
		fclose(file);
		return 0;
	}
	if (strcmp(name, "mokutil") == 0) {
		int i;

		for (i = 1; i < argc; i++) {
			if (strcmp(argv[i], "--sb-state") == 0) {
				puts("SecureBoot enabled");
				return 0;
			}
			if (strcmp(argv[i], "--test-key") == 0) {
				puts("certificate is already enrolled");
				return 0;
			}
		}
		return 1;
	}
	if (strcmp(name, "make") == 0) {
		char path[PATH_MAX];
		static const char *kos[] = {
			"fsl110.ko", "fsl_ramp.ko", "fsl_probe.ko", "fst_status.ko", "fsl_reply.ko",
		};
		FILE *calls;
		size_t i;
		char cwd[PATH_MAX];

		if (!getcwd(cwd, sizeof(cwd)))
			return 1;
		snprintf(path, sizeof(path), "%s/make-calls", state ? state : ".");
		calls = fopen(path, "a");
		if (!calls)
			return 1;
		fprintf(calls, "%s\n", cwd);
		fclose(calls);
		for (i = 0; i < sizeof(kos) / sizeof(kos[0]); i++)
			if (write_text(kos[i], "UNSIGNED\n") != 0)
				return 1;
		return 0;
	}
	fprintf(stderr, "shim: unhandled %s\n", name);
	return 127;
}
