/**
 * cross-instance-exec-warn — SOFT warning on an exec that runs another instance's script.
 *
 * WHY: exec inherits the caller gateway's OPENCLAW_CONFIG_PATH/STATE_DIR/GATEWAY_PORT,
 * so a foreign instance's script silently reads the caller's config and calls the
 * caller's gateway. A memory pin can't catch this: memory is read only when searched,
 * and the dangerous moment is exactly when the agent doesn't know it crossed over.
 *
 * CONTRACT: the first matching exec per session is bounced ONCE with the fix
 * (scripts/run-as-instance.sh); re-issuing the IDENTICAL command passes. Single-
 * instance boxes never match (no foreign instance exists). Fail-open on any error.
 * Ownership comes from each instance's own openclaw.json (agents.list[].workspace),
 * refreshed every 5 min; detection lives in detect.js (node detect.test.mjs).
 */
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { foreignInstance, buildOwnerMap, selfFromEnv, warnText } from "./detect.js";

const EXEC_TOOLS = new Set(["exec", "bash", "shell"]);
const MAP_TTL_MS = 5 * 60 * 1000;
const warned = new Map<string, string>(); // sessionKey -> command already warned once

export default definePluginEntry({
  name: "cross-instance-exec-warn",
  description: "Soft one-time warning when exec runs a script owned by another OpenClaw instance.",
  register(api: any) {
    const self = selfFromEnv(process.env);
    let map: [string, string][] = [];
    let mapAt = 0;
    const ownerMap = () => {
      if (Date.now() - mapAt > MAP_TTL_MS) { map = buildOwnerMap(self.home); mapAt = Date.now(); }
      return map;
    };
    api.on("before_tool_call", async (event: any, ctx: any) => {
      try {
        const tool = String(event?.toolName ?? event?.tool ?? "");
        if (!EXEC_TOOLS.has(tool)) return undefined;
        const params = event?.params ?? {};
        const cmd = String(params.command ?? "");
        const cwd = String(params.workdir ?? ctx?.workspaceDir ?? process.cwd());
        const inst = foreignInstance(cmd, { own: self.own, map: ownerMap(), cwd });
        if (!inst) return undefined;
        const key = String(ctx?.sessionKey ?? event?.sessionKey ?? "");
        if (warned.get(key) === cmd) { warned.delete(key); return undefined; }
        warned.set(key, cmd);
        if (warned.size > 500) warned.delete(warned.keys().next().value as string);
        return { block: true, blockReason: warnText(inst, self.own) };
      } catch {
        return undefined;
      }
    }, { priority: 40 });
  },
});
