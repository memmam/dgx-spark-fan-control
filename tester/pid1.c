/* Pid 1 for the tester namespace.
 *
 * Bash wait only reaps jobs it started. A process reparented from the
 * launcher, including the hostlog tee, is a kernel child and not a bash
 * job, so wait -n returns 127 and cannot say whether a child exited 127
 * or no child remains. This process uses wait(2) until ECHILD. The exit
 * status is the launcher's, including 128+signal. Every other child is
 * reaped first, whatever its status, so a 127 does not abandon the rest.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>

static volatile sig_atomic_t primary_pid = -1;

static void forward(int sig)
{
	pid_t pid = primary_pid;

	if (pid > 0)
		kill(pid, sig);
}

static int status_code(int status)
{
	if (WIFEXITED(status))
		return WEXITSTATUS(status);
	if (WIFSIGNALED(status))
		return 128 + WTERMSIG(status);
	return 1;
}

int main(int argc, char **argv)
{
	struct sigaction action;
	pid_t child;
	int status;
	int primary_status = 1;
	int saw_primary = 0;

	if (argc < 2) {
		fprintf(stderr, "pid1: usage: pid1 COMMAND [ARG...]\n");
		return 125;
	}
	sigemptyset(&action.sa_mask);
	action.sa_flags = SA_RESTART;
	action.sa_handler = forward;
	if (sigaction(SIGTERM, &action, NULL) != 0 ||
	    sigaction(SIGINT, &action, NULL) != 0 ||
	    sigaction(SIGHUP, &action, NULL) != 0) {
		perror("pid1: sigaction");
		return 125;
	}
	child = fork();
	if (child < 0) {
		perror("pid1: fork");
		return 125;
	}
	if (child == 0) {
		signal(SIGTERM, SIG_DFL);
		signal(SIGINT, SIG_DFL);
		signal(SIGHUP, SIG_DFL);
		execvp(argv[1], argv + 1);
		perror("pid1: exec");
		_exit(127);
	}
	primary_pid = child;
	for (;;) {
		pid_t got = wait(&status);

		if (got < 0) {
			if (errno == EINTR)
				continue;
			if (errno == ECHILD)
				break;
			perror("pid1: wait");
			return 125;
		}
		if (got == child) {
			primary_status = status_code(status);
			saw_primary = 1;
			primary_pid = -1;
		}
	}
	if (!saw_primary)
		return 1;
	return primary_status;
}
