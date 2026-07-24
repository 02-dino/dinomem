import { readdirSync, readFileSync, statSync, existsSync } from "node:fs";
import { join, isAbsolute } from "node:path";
import { spawnSync } from "node:child_process";

// dinomem-open-notes (NEURON SUPERSET of the base hook): on agent:bootstrap,
// inject a blocking manifest of OPEN dinomem notes (status in_progress|pending)
// so the model cannot miss unfinished work. Same mechanism as the base hook,
// PLUS a neuron-only cold-start recall line: neuron ships a session_search tool
// (semantic recall over recent conversation transcripts) that base does not, so
// the manifest also reminds the model to run session_search when the user's
// opener names an entity/feature/continuation that no open note covers. This
// hook OVERWRITES base's dinomem-open-notes (same hook name, superset behavior);
// neuron's installer copies it after base's, so neuron's version wins.
// Zero-op when no open notes.

type MaybeRecord = Record<string, unknown> | undefined | null;

interface BootstrapFileEntry {
  name: string;
  content: string;
  [k: string]: unknown;
}

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
  return asString(process.env.OPENCLAW_WORKSPACE) ?? asString(process.env.DINOMEM_WORKSPACE);
}

function parseMaxNotes(): number {
  const raw = process.env.DINOMEM_OPEN_NOTES_MAX;
  const n = raw ? Number.parseInt(raw, 10) : NaN;
  return Number.isFinite(n) && n > 0 ? n : 5;
}

// session_search recall reminder can be disabled (e.g. if a workspace lacks the
// tool) by setting DINOMEM_SESSION_RECALL=0.
function sessionRecallEnabled(): boolean {
  const raw = (process.env.DINOMEM_SESSION_RECALL ?? "").trim().toLowerCase();
  return raw !== "0" && raw !== "false" && raw !== "off";
}

// Pull a "key: value" style field from a note's header. Tolerant of leading
// whitespace and case; returns the first match's trimmed value.
function field(text: string, key: string): string | undefined {
  const re = new RegExp("^\\s*" + key + "\\s*:\\s*(.+)$", "im");
  const m = text.match(re);
  return m ? m[1].trim() : undefined;
}

function firstHeading(text: string): string | undefined {
  const m = text.match(/^#\s+(.+)$/m);
  if (!m) return undefined;
  // Strip a leading "Project:" label for compactness.
  return m[1].replace(/^Project:\s*/i, "").trim();
}

function oneLine(s: string, max = 100): string {
  const flat = s.replace(/\s+/g, " ").trim();
  return flat.length > max ? flat.slice(0, max - 1) + "\u2026" : flat;
}

interface OpenNote {
  file: string;
  title: string;
  status: string;
  doneWhen: string | undefined;
  mtimeMs: number;
}

function collectOpenNotes(memoryDir: string): OpenNote[] {
  let entries: string[];
  try {
    entries = readdirSync(memoryDir);
  } catch {
    return []; // no memory dir yet -> nothing to inject
  }
  const notes: OpenNote[] = [];
  for (const name of entries) {
    if (!name.startsWith("_note_") || !name.endsWith(".md")) continue;
    const full = join(memoryDir, name);
    let text: string;
    let mtimeMs = 0;
    try {
      const st = statSync(full);
      if (!st.isFile()) continue;
      mtimeMs = st.mtimeMs;
      text = readFileSync(full, "utf8");
    } catch {
      continue;
    }
    const status = (field(text, "status") ?? "").toLowerCase();
    if (status !== "in_progress" && status !== "pending") continue;
    notes.push({
      file: name,
      title: firstHeading(text) ?? name.replace(/^_note_/, "").replace(/\.md$/, ""),
      status,
      doneWhen: field(text, "done_when"),
      mtimeMs,
    });
  }
  notes.sort((a, b) => b.mtimeMs - a.mtimeMs);
  return notes;
}

function renderManifest(notes: OpenNote[], max: number): string {
  const shown = notes.slice(0, max);
  const overflow = notes.length - shown.length;
  const lines: string[] = [];
  lines.push("## dinomem: OPEN WORK (injected by dinomem-open-notes hook)");
  lines.push("");
  lines.push(
    "\u26A0\uFE0F You have unfinished dinomem notes. Before your FIRST answer this session, " +
      "if any note below is relevant to the user's message you **MUST** `read` its file and " +
      "resume from its `resume_state` — do not restart finished work or re-ask what a note already answers.",
  );
  lines.push("");
  for (const n of shown) {
    const dw = n.doneWhen ? ` \u2014 done_when: ${oneLine(n.doneWhen)}` : "";
    lines.push(`- \`memory/${n.file}\` \u00B7 **${oneLine(n.title, 80)}** \u00B7 _${n.status}_${dw}`);
  }
  if (overflow > 0) {
    lines.push(`- \u2026 +${overflow} more open note(s) in \`memory/_note_*.md\``);
  }
  if (sessionRecallEnabled()) {
    lines.push("");
    lines.push(
      "\u26A0\uFE0F **M2_M3 RECALL GATE (mandatory, not optional).** If the user's message names ANY entity, feature, " +
        "project, decision, person, date, or continuation — you **MUST** recall BEFORE any fs/exec/read/grep and BEFORE " +
        "asking the user 'what is X': (1) `memory_search \"<topic>\"` for distilled facts, AND (2) `session_search " +
        "\"<topic>\"` for recent raw-conversation detail (<14d) when memory_search is thin. Read the top hit, THEN act. " +
        "Do NOT grep the filesystem or ask the user as a substitute for recall — that is the exact failure this gate exists " +
        "to stop. This supersedes the soft `session_start_recall` AGENTS.md plea with an imperative mechanism.",
    );
  }
  lines.push("");
  return lines.join("\n");
}

// Derive a generic claimant id from the agent id (no hardcoded aliases). Falls
// back to "agent" so the claimant is always well-formed. Used as live-session-<id>.
function resolveAgentId(context: Record<string, unknown>): string {
  const ctx = context ?? {};
  const direct = asString((ctx as Record<string, unknown>).agentId);
  if (direct) return direct;
  const cfg = (ctx as Record<string, unknown>).cfg as MaybeRecord;
  if (cfg && typeof cfg === "object") {
    const a = asString((cfg as Record<string, unknown>).agentId);
    if (a) return a;
  }
  return asString(process.env.OPENCLAW_AGENT_ID) ?? "agent";
}

// Locate claim_note.sh generically: prefer <workspace>/scripts, else the hook's
// own repo scripts dir. Returns undefined if not found (neuron-only tool; on a
// base install it's absent and we simply skip auto-claim).
function resolveClaimScript(workspaceDir: string): string | undefined {
  const wsScripts = isAbsolute(workspaceDir)
    ? join(workspaceDir, "scripts", "claim_note.sh")
    : join(process.cwd(), workspaceDir, "scripts", "claim_note.sh");
  if (existsSync(wsScripts)) return wsScripts;
  return undefined;
}

// Auto-claim surfaced IN_PROGRESS notes at bootstrap so the Project Advancer /
// Improver / Deleter crons back off while this live session is active (120min
// lease). Removes the discipline-dependency of the old "claim before first edit"
// AGENTS.md plea. Fail-safe: any error is swallowed; auto-claim MUST NEVER break
// bootstrap. Only in_progress notes are claimed (pending = drafted, not handed to
// this session); an idle session's lease expires in 120min and crons resume.
function autoClaimInProgress(
  notes: OpenNote[],
  memoryDir: string,
  claimScript: string,
  claimant: string,
): number {
  let claimed = 0;
  for (const n of notes) {
    if (n.status !== "in_progress") continue;
    const notePath = join(memoryDir, n.file);
    try {
      const r = spawnSync("bash", [claimScript, "claim", notePath, claimant], {
        timeout: 5000,
        encoding: "utf8",
      });
      // exit 0 = took/refreshed/already-ours; exit 3 = held by another (leave it).
      if (r.status === 0) claimed++;
    } catch {
      // swallow — never break bootstrap on a claim failure
    }
  }
  return claimed;
}

const handler = async (event: {
  type?: string;
  action?: string;
  context?: MaybeRecord;
}): Promise<void> => {
  try {
    // agent:bootstrap only
    if (event?.type !== "agent" || event?.action !== "bootstrap") return;
    const context = (event.context ?? {}) as Record<string, unknown>;

    const workspaceDir = resolveWorkspaceDir(context);
    if (!workspaceDir) return;
    const memoryDir = isAbsolute(workspaceDir)
      ? join(workspaceDir, "memory")
      : join(process.cwd(), workspaceDir, "memory");

    const notes = collectOpenNotes(memoryDir);
    if (notes.length === 0) return; // zero-op on a clean workspace

    // Auto-claim in_progress notes for THIS live session so crons back off while
    // we're active. Neuron-only (needs claim_note.sh); skipped cleanly on base.
    const claimScript = resolveClaimScript(workspaceDir);
    if (claimScript) {
      const claimant = `live-session-${resolveAgentId(context)}`;
      const n = autoClaimInProgress(notes, memoryDir, claimScript, claimant);
      if (n > 0) {
        console.log(
          `[dinomem-open-notes] auto-claimed ${n} in_progress note(s) as ${claimant}`,
        );
      }
    }

    const manifest = renderManifest(notes, parseMaxNotes());

    const existing = Array.isArray(context.bootstrapFiles)
      ? (context.bootstrapFiles as BootstrapFileEntry[])
      : [];
    // Inject under the AGENTS.md name so the entry survives the subagent/cron
    // session bootstrap allowlist filter on main interactive sessions.
    const entry: BootstrapFileEntry = { name: "AGENTS.md", content: manifest };
    context.bootstrapFiles = [...existing, entry];

    console.log(
      `[dinomem-open-notes] injected ${notes.length} open note(s) for ${workspaceDir}`,
    );
  } catch (err) {
    // Never break bootstrap.
    console.warn("[dinomem-open-notes] handler error: " + String(err));
  }
};

export default handler;
