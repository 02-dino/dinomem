import { spawn } from "node:child_process";
import { existsSync, openSync, closeSync } from "node:fs";
import { join, isAbsolute } from "node:path";

// dinomem-reset-extract: fire-and-forget memory pipeline on manual /new or /reset.
// Shells to procedures/auto_session_reset.py (adopt + extract + optional ingest).
// Dedup-safe: the script holds /tmp/dinomem_auto_reset.lock and uses processed-log + content-hash.

type MaybeRecord = Record<string, unknown> | undefined | null;

function asString(v: unknown): string | undefined {
  return typeof v === "string" && v.length > 0 ? v : undefined;
}

function resolveWorkspaceDir(context: MaybeRecord): string | undefined {
  const ctx = (context ?? {}) as Record<string, unknown>;

  const direct = asString(ctx.workspaceDir);
  if (direct) return direct;

  const cfg = ctx.cfg as MaybeRecord;
  if (cfg && typeof cfg === "object") {
    const ws = (cfg as Record<string, unknown>).workspace as MaybeRecord;
    if (ws && typeof ws === "object") {
      const dir = asString((ws as Record<string, unknown>).dir);
      if (dir) return dir;
    }
  }

  return (
    asString(process.env.DINOMEM_WORKSPACE) ??
    asString(process.env.OPENCLAW_WORKSPACE)
  );
}

const handler = async (event: {
  type: string;
  action: string;
  sessionKey?: string;
  context?: MaybeRecord;
}): Promise<void> => {
  try {
    // Two trigger classes:
    //  (a) manual /new or /reset  -> zero-delay memory pipeline (original behavior).
    //  (b) session:compact:after  -> HARD-FORCE compaction reset. The 15-min cron
    //      resets at compaction depth>=2, but a session compacting in a tight storm
    //      can pile up unbounded BETWEEN ticks (and never idles the 10-min grace).
    //      On every compaction we fire the pipeline in --force mode; session_reset.py
    //      then hard-resets ONLY sessions at depth>=FORCE_COMPACTION_THRESHOLD (5),
    //      bypassing grace, and extracts each forced session's archive to
    //      memory/YYYY-MM-DD.md BEFORE deleting its mapping (airtight flush-before-/new).
    //      Below the ceiling this is a no-op reset pass, so firing on every compaction
    //      is safe + self-throttling (the lock + processed-log dedup the pipeline).
    const isManualReset = event.type === "command" && (event.action === "new" || event.action === "reset");
    const isCompaction =
      event.type === "session:compact:after" ||
      (event.type === "session" && event.action === "compact:after") ||
      event.type === "after_compaction";
    if (!isManualReset && !isCompaction) return;

    const workspaceDir = resolveWorkspaceDir(event.context);
    if (!workspaceDir || !isAbsolute(workspaceDir)) {
      console.warn("[dinomem-reset-extract] could not resolve workspace dir; skipping type=" + event.type + " action=" + event.action);
      return;
    }

    const script = join(workspaceDir, "procedures", "auto_session_reset.py");
    if (!existsSync(script)) {
      console.warn("[dinomem-reset-extract] pipeline script not found at " + script + "; skipping");
      return;
    }

    const logPath = join(workspaceDir, "logs", "auto_reset.log");
    let logFd: number | "ignore" = "ignore";
    try {
      logFd = openSync(logPath, "a");
    } catch {
      logFd = "ignore";
    }

    // Compaction path forces the hard-reset threshold; manual /new /reset keeps the
    // normal pipeline (no --force -> tick thresholds + grace unchanged).
    const scriptArgs = isCompaction ? [script, "--force"] : [script];
    const child = spawn("python3", scriptArgs, {
      cwd: workspaceDir,
      detached: true,
      stdio: ["ignore", logFd, logFd],
      env: process.env,
    });

    child.on("error", (err: Error) => {
      console.warn("[dinomem-reset-extract] launch error: " + String(err));
    });

    // Close the parent's copy of the log fd: the detached child holds its own dup,
    // so this is behavior-preserving and avoids leaking one fd per /new or /reset
    // over the gateway's lifetime.
    if (typeof logFd === "number") {
      try {
        closeSync(logFd);
      } catch {
        // already closed / invalid — ignore
      }
    }

    child.unref();

    console.log("[dinomem-reset-extract] launched pipeline for " + workspaceDir + " action=" + event.action);
  } catch (err) {
    console.warn("[dinomem-reset-extract] handler error: " + String(err));
  }
};

export default handler;
