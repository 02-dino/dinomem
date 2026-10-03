import { spawn } from "node:child_process";
import { existsSync, openSync } from "node:fs";
import { join, isAbsolute } from "node:path";

// dinomem-memory-warm: on gateway startup, fire one throwaway memory_search per
// configured agent so the first REAL query lands warm instead of paying the cold
// boot cost (model load + FTS/vector handle open + embedding-cache seed).
// ALSO fire one direct TEI embedding warm once per gateway process.
//
// Mandatory by default: every agent in the running gateway's own cfg.agents.list
// gets warmed (not opt-in). Plus one shared TEI warm via a direct HTTP POST
// (efficient on multi-agent gateways: N agents + 1 TEI probe, not N * (N+1)).
// Fire-and-forget: each launch is detached, output discarded; a slow/failed
// warmup never affects boot or any user-facing path. Independent per agent
// (one failure never blocks another).

type MaybeRecord = Record<string, unknown> | undefined | null;

function asString(v: unknown): string | undefined {
  return typeof v === "string" && v.length > 0 ? v : undefined;
}

function asRecord(v: unknown): Record<string, unknown> | undefined {
  return typeof v === "object" && v !== null ? (v as Record<string, unknown>) : undefined;
}

function asArray<T>(v: unknown): T[] | undefined {
  return Array.isArray(v) ? (v as T[]) : undefined;
}

function resolveWorkspaceDir(context: MaybeRecord): string | undefined {
  const ctx = (context ?? {}) as Record<string, unknown>;

  const direct = asString(ctx.workspaceDir);
  if (direct) return direct;

  const cfg = asRecord(ctx.cfg);
  if (cfg) {
    const ws = asRecord(cfg.workspace);
    if (ws) {
      const dir = asString(ws.dir);
      if (dir) return dir;
    }
  }

  return (
    asString(process.env.DINOMEM_WORKSPACE) ??
    asString(process.env.OPENCLAW_WORKSPACE)
  );
}

// Resolve TEI embedding endpoint + model with 4-step fallback.
// Returns { baseUrl, model } or undefined if nothing resolves.
function resolveTeiConfig(cfg: MaybeRecord): { baseUrl: string; model: string } | undefined {
  // Step 1: env overrides (DINOMEM_TEI_URL / DINOMEM_TEI_MODEL)
  const envUrl = asString(process.env.DINOMEM_TEI_URL);
  const envModel = asString(process.env.DINOMEM_TEI_MODEL);
  if (envUrl && envModel) return { baseUrl: envUrl, model: envModel };

  const cfgRecord = asRecord(cfg);
  if (!cfgRecord) return undefined;

  // Step 2: cfg.agents.defaults.memorySearch.remote (the standard config path)
  const agents = asRecord(cfgRecord.agents);
  if (agents) {
    const defaults = asRecord(agents.defaults);
    if (defaults) {
      const memSearch = asRecord(defaults.memorySearch);
      if (memSearch) {
        const remote = asRecord(memSearch.remote);
        if (remote) {
          const baseUrl = asString(remote.baseUrl);
          // BUG FIXED 2026-10-04: this previously read
          //   asString(defaults.memorySearch)
          // which hands a RECORD to a string guard, so it was ALWAYS undefined.
          // The model therefore never resolved from the standard config path and
          // step 2 silently fell through to the provider scan (step 3) or to no
          // warm at all. Read the model from where it actually lives, nearest
          // scope first: remote.model -> memorySearch.model -> defaults.model.
          const model =
            asString(remote.model) ??
            asString(memSearch.model) ??
            asString(defaults.model);
          if (baseUrl && model) {
            return { baseUrl, model };
          }
        }
      }
    }
  }

  // Step 3: scan cfg.models.providers for first openai-completions with baseUrl + models[]
  const models = asRecord(cfgRecord.models);
  if (models) {
    const providers = asArray<Record<string, unknown>>(models.providers);
    if (providers) {
      for (const provider of providers) {
        if (asString(provider.api) === "openai-completions") {
          const baseUrl = asString(provider.baseUrl);
          const modelsList = asArray<Record<string, unknown>>(provider.models);
          if (baseUrl && modelsList && modelsList.length > 0) {
            const firstModel = asRecord(modelsList[0]);
            if (firstModel) {
              const modelId = asString(firstModel.id);
              if (modelId) return { baseUrl, model: modelId };
            }
          }
        }
      }
    }
  }

  // Step 4: nothing resolved, skip silently (fail-open)
  return undefined;
}

// Fire one TEI embeddings warm via direct HTTP POST.
// Fire-and-forget, never awaited.
async function warmTei(cfg: MaybeRecord): Promise<void> {
  const teiConfig = resolveTeiConfig(cfg);
  if (!teiConfig) return;

  const { baseUrl, model } = teiConfig;
  if (!baseUrl || !model) return;

  const url = `${baseUrl}/embeddings`;
  const payload = JSON.stringify({ model, input: "warmup" });

  try {
    const req = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: payload,
    });
    // Discard response; just the fire matters.
    if (req.ok) {
      console.log("[dinomem-memory-warm] TEI warm completed for model=" + model);
    } else {
      console.warn(
        "[dinomem-memory-warm] TEI warm got " +
          req.status +
          " for model=" +
          model,
      );
    }
  } catch (err) {
    // Swallow error silently; TEI warm failure never blocks boot.
    console.warn("[dinomem-memory-warm] TEI warm failed: " + String(err));
  }
}

const handler = async (event: {
  type: string;
  context?: MaybeRecord;
}): Promise<void> => {
  try {
    if (event.type !== "gateway:startup") return;

    const ctx = asRecord(event.context);
    const cfg = ctx ? asRecord(ctx.cfg) : undefined;

    // TEI warm: once per gateway process, via direct HTTP (not through memory_search).
    // Fire-and-forget, do not await.
    warmTei(cfg).catch(() => {
      // Swallow any unhandled promise rejection.
    });

    // Local index warm: per-agent, via openclaw memory search (existing pattern).
    // Resolve agents from cfg.agents.list (mandatory by default, no env-var gate).
    const agentsCfg = cfg ? asRecord(cfg.agents) : undefined;
    const agentsList = agentsCfg ? asArray<Record<string, unknown>>(agentsCfg.list) : undefined;
    if (!agentsList || agentsList.length === 0) {
      // No agents to warm; skip silently (fail-open).
      return;
    }

    // Extract agent ids from the list.
    const agents = agentsList
      .map((entry) => asString(entry.id))
      .filter((id): id is string => id !== undefined);
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
          console.warn(
            "[dinomem-memory-warm] launch error for " + agentId + ": " + String(err),
          );
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
