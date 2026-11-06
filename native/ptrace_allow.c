/*
 * ptrace_allow.so — let the launcher (the trainer GUI) trace this process tree
 * even while kernel.yama.ptrace_scope = 1.
 *
 * Two kernel conditions have to hold for another process to read our memory or
 * attach to us:
 *
 *   1. Yama: with ptrace_scope=1 only descendants may attach, *unless* we named
 *      the tracer ourselves with prctl(PR_SET_PTRACER, pid). That relation is
 *      per-task, so it must be declared inside every process — not in the
 *      launcher, and not inherited across fork.
 *   2. dumpable: even a declared tracer is refused if our mm is non-dumpable.
 *      Wine marks its processes non-dumpable, so put it back to 1.
 *
 * Both are re-applied from three places, because Wine creates its PE processes
 * by fork (and occasionally exec) long after the loader's constructor ran:
 *   - the ELF constructor (runs in every new image)
 *   - a pthread_atfork child handler (fork without exec)
 *   - a 1 Hz watchdog thread (Wine re-marks processes as it initialises them)
 *
 * Build:  gcc -shared -fPIC -O2 -o native/ptrace_allow.so native/ptrace_allow.c -lpthread
 * Use:    LD_PRELOAD=/abs/path/ptrace_allow.so CT_TRACER_PID=<pid> <program>
 *
 * Env:
 *   CT_TRACER_PID  tracer pid to whitelist (required; no-op when unset)
 *   CT_TRACE_DEBUG set to 1 to report on stderr what happened
 */
#define _GNU_SOURCE
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/prctl.h>
#include <unistd.h>

#ifndef PR_SET_PTRACER
#define PR_SET_PTRACER 0x59616d61
#endif

static long ct_tracer = -1;

/* The tracer pid can also arrive through a file, because Steam launch options
 * cannot know a pid at config time: `CT_TRACER_PID_FILE=~/.cache/ct_tracer.pid`
 * with `LD_PRELOAD=…/ptrace_allow.so` in the game's launch options is enough for
 * a Steam-launched game to declare the trainer as its tracer — no root, no
 * capability, and Steam's runtime container stays intact. */
static long ct_pid_from_file(void) {
    const char *path = getenv("CT_TRACER_PID_FILE");
    if (!path || !*path) return -1;
    FILE *f = fopen(path, "r");
    if (!f) return -1;
    long pid = -1;
    if (fscanf(f, "%ld", &pid) != 1) pid = -1;
    fclose(f);
    return pid;
}

static void ct_trace_log(const char *msg, long pid, int rc) {
    if (!getenv("CT_TRACE_DEBUG")) return;
    fprintf(stderr, "ptrace_allow[%d]: %s pid=%ld rc=%d\n", (int)getpid(), msg, pid, rc);
}

static void ct_apply(int may_log) {
    if (ct_tracer <= 0) return;
    /* Only syscalls here: this also runs in a fork child, where calling into
     * getenv/stdio could deadlock on a lock another thread held at fork time. */
    int drc = 1;
    if (prctl(PR_GET_DUMPABLE, 0, 0, 0, 0) != 1)
        drc = prctl(PR_SET_DUMPABLE, 1, 0, 0, 0);
    int rc = prctl(PR_SET_PTRACER, ct_tracer, 0, 0, 0);
    if (may_log) {
        ct_trace_log(rc == 0 ? "allowed tracer" : "prctl failed", ct_tracer, rc);
        if (drc != 0) ct_trace_log("PR_SET_DUMPABLE failed", ct_tracer, drc);
    }
    /* Never abort the game: a refusal only means the trainer cannot attach. */
}

/* Wine re-marks processes non-dumpable during initialisation, after the
 * constructor has run. Keep the declaration fresh; the cost is one prctl a
 * second while the game is up. */
static void ct_apply(int may_log);
static void ct_apply_child(void) { ct_apply(0); }   /* fork child: syscalls only */

static void *ct_watchdog(void *arg) {
    (void)arg;
    for (;;) {
        long from_file = ct_pid_from_file();
        if (from_file > 0) ct_tracer = from_file;
        ct_apply(1);
        sleep(1);
    }
    return NULL;
}

__attribute__((constructor))
static void ct_init(void) {
    const char *s = getenv("CT_TRACER_PID");
    if (!s || !*s) {
        long from_file = ct_pid_from_file();
        if (from_file <= 0) return;
        ct_tracer = from_file;
        ct_apply(1);
        pthread_atfork(NULL, NULL, ct_apply_child);
        pthread_t th;
        if (pthread_create(&th, NULL, ct_watchdog, NULL) == 0)
            pthread_detach(th);
        return;
    }
    char *end = NULL;
    long pid = strtol(s, &end, 10);
    if (end == s || pid <= 0) {
        ct_trace_log("bad CT_TRACER_PID", pid, -1);
        return;
    }
    ct_tracer = pid;
    ct_apply(1);
    pthread_atfork(NULL, NULL, ct_apply_child);

    pthread_t th;
    if (pthread_create(&th, NULL, ct_watchdog, NULL) == 0)
        pthread_detach(th);
}
