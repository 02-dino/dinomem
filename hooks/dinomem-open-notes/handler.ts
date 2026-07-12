import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, isAbsolute } from "node:path";

// dinomem-open-notes: on agent:bootstrap, inject a blocking manifest of OPEN
// dinomem notes (status in_progress|pending) so the model cannot miss unfinished
// work. Pointer manifest (path + title + status + one-line done_when), capped and
// rendered as a must-read-before-answering directive. Zero-op when no open notes.

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
  lines.push("");
  return lines.join("\n");
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
