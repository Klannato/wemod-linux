/*
 * ctptrace — DEAD END, kept as documentation.
 *
 * The idea: a tiny setcap'd helper that hands CAP_SYS_PTRACE to the trainer
 * without making a Python interpreter privileged. It does not work, and the
 * kernel rule is worth remembering:
 *
 *   PR_CAP_AMBIENT_RAISE requires the capability to be in BOTH the process
 *   permitted set AND the process inheritable set. A file capability
 *   (`setcap cap_sys_ptrace=eip`) populates permitted+effective at exec, but the
 *   process inheritable set is only ever changed by root/CAP_SETPCAP — so the
 *   raise fails with EPERM and the exec'd child is unprivileged again.
 *
 * Verified here: `getcap` showed `cap_sys_ptrace=eip`, the binary still failed at
 * `prctl(PR_CAP_AMBIENT, ...)` with EPERM, and the trainer ended up with an empty
 * capability set either way.
 *
 * What actually works instead (no root, no capability): load the tracer shim into
 * a Steam-launched game via Launch Options —
 *
 *     LD_PRELOAD=/home/geo/wemod-linux/native/ptrace_allow.so \
 *     CT_TRACER_PID_FILE=/home/geo/.cache/ct_tracer.pid %command%
 *
 * See ctlaunch.steam_launch_options(). The only privileged alternatives are
 * `sudo sysctl kernel.yama.ptrace_scope=0` (machine-wide) or running the trainer
 * itself under a capped interpreter (`sudo setcap cap_sys_ptrace+ep <python>`),
 * which privileges every script that interpreter runs.
 */
#include <linux/capability.h>
#include <stdio.h>
#include <sys/prctl.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: ctptrace <program> [args...]\n");
        return 2;
    }
    if (prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_RAISE, CAP_SYS_PTRACE, 0, 0) != 0) {
        perror("ctptrace: PR_CAP_AMBIENT_RAISE refused (see the comment in this file)");
        return 1;
    }
    execvp(argv[1], argv + 1);
    perror("ctptrace: exec");
    return 127;
}
