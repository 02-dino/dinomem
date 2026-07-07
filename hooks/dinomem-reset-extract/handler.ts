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
    if (event.type !== "command") return;
    if (event.action !== "new" && event.action !== "reset") return;

    const workspaceDir = resolveWorkspaceDir(event.context);
    if (!workspaceDir || !isAbsolute(workspaceDir)) {
      console.warn("[dinomem-reset-extract] could not resolve workspace dir; skipping action=" + event.action);
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

    const child = spawn("python3", [script], {
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
