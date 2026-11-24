/**
 * dumper.h - Public API for the checkpoint dumper
 */
#ifndef DUMPER_H
#define DUMPER_H

/**
 * ckpt_dump() - Capture current process state and write it to a file.
 *
 * @path: Output file path (e.g. "dump.ckpt")
 *
 * Call this at the ROI entry point inside your Tailbench application.
 * When the loader restores the checkpoint, execution will resume at the
 * instruction immediately following the call to ckpt_dump() in the caller.
 *
 * Returns 0 on success and -1 on error in the original (native) run, and
 * 1 when execution resumes here from a restored checkpoint.
 *
 * NOTE: After ckpt_dump() returns, the original application continues
 *       executing normally (first run semantics).  The loader will invoke
 *       it again starting at the ROI entry (restore semantics).
 */
int ckpt_dump(const char *path);

/* Async-signal-safe logger to stderr (static buffer + write(2)). */
void ckpt_log(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

/*
 * Message convention for the whole repo:   [component] LEVEL: message
 *   CKPT_INFO   no level label      "[libckpt] Timer armed (5000 ns, tid 42)."
 *   CKPT_WARN   "WARN: "            something degraded, execution continues
 *   CKPT_ERROR  "ERROR: "           an operation failed, the caller handles it
 *   CKPT_FATAL  "FATAL: "           the process is about to terminate
 * The caller writes only the message: no prefix, no level, no trailing "\n".
 * Each .c file defines CKPT_LOG_COMPONENT (e.g. "ckpt") before including
 * this header. The macros only log; terminating is up to the call site.
 */
#define CKPT_LOG_(level_prefix, fmt, ...) \
	ckpt_log("[" CKPT_LOG_COMPONENT "] " level_prefix fmt "\n", ##__VA_ARGS__)

#define CKPT_INFO(fmt, ...) CKPT_LOG_("", fmt, ##__VA_ARGS__)
#define CKPT_WARN(fmt, ...) CKPT_LOG_("WARN: ", fmt, ##__VA_ARGS__)
#define CKPT_ERROR(fmt, ...) CKPT_LOG_("ERROR: ", fmt, ##__VA_ARGS__)
#define CKPT_FATAL(fmt, ...) CKPT_LOG_("FATAL: ", fmt, ##__VA_ARGS__)

#endif /* DUMPER_H */
