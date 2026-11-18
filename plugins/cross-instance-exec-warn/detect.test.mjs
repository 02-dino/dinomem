// node detect.test.mjs -> exit 0 = all pass. Builds a fake multi-instance home.
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { foreignInstance, buildOwnerMap, selfFromEnv } from "./detect.js";

const H = mkdtempSync(join(tmpdir(), "xiew-"));
const inst = (dir, wss) => {
  mkdirSync(join(H, dir), { recursive: true });
  writeFileSync(join(H, dir, "openclaw.json"), JSON.stringify({ agents: { list: wss.map((w) => ({ workspace: join(H, ".openclaw", w) })) } }));
};
inst(".openclaw", ["workspace-main"]);
inst(".openclaw-analyst", ["workspace-analyst", "workspace-summarizer"]);
inst(".openclaw-cs", ["workspace-cs"]);
inst(".openclaw-sales", ["workspace-sales", "workspace-wasales"]);
mkdirSync(join(H, ".openclaw-broken"));
writeFileSync(join(H, ".openclaw-broken", "openclaw.json"), "{ not json");

const map = buildOwnerMap(H);
const ws = (w) => join(H, ".openclaw", w);
const ctx = { own: "analyst", map, cwd: ws("workspace-analyst") };
const cases = [
  [`python3 ${ws("workspace-cs")}/procedures/extract_memory.py`, "cs"],
  [`bash ${ws("workspace-wasales")}/scripts/r.sh`, "sales"],          // name != instance
  [`./venv/bin/python ../workspace-cs/tools/x.py --a`, "cs"],          // relative, resolved vs cwd
  [`${ws("workspace-cs")}/scripts/drain.sh`, "cs"],                    // direct run
  [`timeout 20 env FOO=1 python3 -u ${ws("workspace-cs")}/x.py`, "cs"],
  [`cd /tmp; node ${H}/.openclaw-cs/extensions/x/run.js`, "cs"],
  [`python3 ${ws("workspace-main")}/x.py`, "default"],
  // own / shared / read-only / explicit -> null
  [`python3 ${ws("workspace-summarizer")}/x.py`, null],               // analyst owns it
  [`python3 tools/route.py pending`, null],
  [`node ${H}/.openclaw/extensions/x/index.js`, null],                 // shared infra
  [`cat ${ws("workspace-cs")}/x.py`, null],
  [`grep -n foo ${ws("workspace-cs")}/x.py`, null],
  [`python3 -c "open('${ws("workspace-cs")}/x.py')"`, null],
  [`bash scripts/run-as-instance.sh cs python3 ${ws("workspace-cs")}/x.py`, null],
  [`OPENCLAW_CONFIG_PATH=${H}/.openclaw-cs/openclaw.json python3 ${ws("workspace-cs")}/x.py`, null],
  [`python3 /opt/other/x.py`, null],
  [``, null],
];
let fail = 0;
for (const [cmd, want] of cases) {
  const got = foreignInstance(cmd, ctx);
  if (got !== want) { fail++; console.log(`FAIL ${JSON.stringify(cmd)} -> ${got}, want ${want}`); }
}
// default-instance caller: other instances still foreign, its own workspace not
const d = foreignInstance(`python3 ${ws("workspace-cs")}/x.py`, { own: "default", map, cwd: "/" });
if (d !== "cs") { fail++; console.log(`FAIL default caller -> ${d}`); }
const self = [
  [{ OPENCLAW_STATE_DIR: "/root/.openclaw-analyst/" }, "analyst", "/root"],
  [{ OPENCLAW_CONFIG_PATH: "/home/u/.openclaw-cs/openclaw.json" }, "cs", "/home/u"],
  [{ HOME: "/home/u" }, "default", "/home/u"],
];
for (const [env, own, home] of self) {
  const got = selfFromEnv(env);
  if (got.own !== own || got.home !== home) { fail++; console.log(`FAIL env ${JSON.stringify(env)} -> ${JSON.stringify(got)}`); }
}
const n = cases.length + 1 + self.length;
console.log(fail ? `${fail}/${n} failed` : `all ${n} passed`);
process.exit(fail ? 1 : 0);
