import { spawn } from "node:child_process";
import { existsSync, openSync } from "node:fs";
import { join, isAbsolute } from "node:path";

// dinomem-memory-warm: on gateway startup, fire one throwaway memory_search per
// configured agent so the first REAL query lands warm instead of paying the cold
// boot cost (model load + FTS/vector handle open + embedding-cache seed).
//
// Opt-in via DINOMEM_WARM_AGENTS (comma-separated agent ids). Unset => no-op, so a
// multi-agent host never warms anything it wasn't told to. Fire-and-forget: each
// launch is detached, output discarded; a slow/failed warmup never affects boot or
// any user-facing path. Independent per agent (one failure never blocks another).

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
  context?: MaybeRecord;
}): Promise<void> => {
  try {
    if (event.type !== "gateway:startup") return;

    // Opt-in: comma-separated agent ids to warm. Unset => no-op.
    const raw = asString(process.env.DINOMEM_WARM_AGENTS);
    if (!raw) return;

    const agents = raw
      .split(",")
      .map((a) => a.trim())
      .filter((a) => a.length > 0 && /^[A-Za-z0-9._-]+$/.test(a));
    if (agents.length === 0) return;

    // Best-effort log fd (shared across launches). Falls back to ignore.
    const workspaceDir = resolveWorkspaceDir(event.context);
    let logFd: number | "ignore" = "ignore";
    if (workspaceDir && isAbsolute(workspaceDir)) {
      const logDir = join(workspaceDir, "logs");
      if (existsSync(logDir)) {
        try {
          logFd = openSync(join(logDir, "memory_warm.log"), "a");
        } catch {
          logFd = "ignore";
        }
      }
    }

    for (const agentId of agents) {
      try {
        const child = spawn(
          "openclaw",
          ["memory", "search", "warmup", "--agent", agentId],
          {
            detached: true,
            stdio: ["ignore", logFd, logFd],
            env: process.env,
          },
        );
        child.on("error", (err: Error) => {
          console.warn("[dinomem-memory-warm] launch error for " + agentId + ": " + String(err));
        });
        child.unref();
        console.log("[dinomem-memory-warm] warming memory_search for agent=" + agentId);
      } catch (err) {
        console.warn("[dinomem-memory-warm] spawn failed for " + agentId + ": " + String(err));
      }
    }
  } catch (err) {
    console.warn("[dinomem-memory-warm] handler error: " + String(err));
  }
};

export default handler;
