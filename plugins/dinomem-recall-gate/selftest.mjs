// Language-agnostic recall-gate self-test. Mirrors the hook's state machine
// (no message parsing). Proves TWO tiers:
//   (A) COLD fs/exec reach nudge (original): cold fs reach fires; recall-first
//       stays silent; once-per-turn; cooldown; non-fs ignored.
//   (B) HOT-ZONE WRITE re-arm (2026-08-02): a write/edit/apply_patch to a
//       mechanism path requires its OWN fresh recall THIS TURN even if a general
//       recall latched earlier -- closes the "recalled once, then built blind"
//       hole (the Generator-retry / 12.4M-blowup near-miss). Loop-safe: writes to
//       memory/ or the gate's own dir are excluded so it can't self-perpetuate.
const cfg = {
  enforce: true, agentFilter: "analyst",
  recallTools: ["memory_search","memory_get","graph_search","session_search","semantic_search","docs_search","data_query"],
  fsTools: ["exec","read","grep","glob"],
  writeTools: ["write","edit","apply_patch","Write","Edit","NotebookEdit"],
  hotZoneGlobs: ["scripts/","procedures/","tools/","openclaw.json",".gen_control","crontab"],
  hotZoneExclude: ["memory/","extensions/dinomem-",".backups/","logs/"],
  cooldownTurns: 3,
};
const _state = new Map();
function fp(s){let h=0;for(let i=0;i<s.length;i++)h=(h*31+s.charCodeAt(i))|0;return String(h);}

// Path helpers (mirror index.ts). Hot-zone = matches a glob AND matches NO exclude.
function pathIsHotZone(paths){
  for(const p of paths){
    const s=String(p||"");
    if(cfg.hotZoneExclude.some(x=>s.includes(x))) continue;
    if(cfg.hotZoneGlobs.some(g=>s.includes(g))) return true;
  }
  return false;
}

// gate(sessionKey, toolName, msg, paths?) -> "BLOCK" | null
function gate(sessionKey, toolName, msg, paths){
  if(cfg.agentFilter && !sessionKey.includes(cfg.agentFilter)) return null;
  if(!toolName) return null;
  const turnId = fp(msg||sessionKey);
  let st=_state.get(sessionKey);
  if(!st||st.turnId!==turnId){
    const turnIndex=(st?.turnIndex??0)+1;
    st={turnId,turnIndex,recallDone:false,firedTurn:st?.firedTurn??-Infinity,writeFiredTurn:st?.writeFiredTurn??-Infinity};
    _state.set(sessionKey,st);
  }
  // recall tool latches general recall for the turn
  if(cfg.recallTools.includes(toolName)){st.recallDone=true;return null;}

  // ── TIER B: hot-zone write re-arm ──
  if(cfg.writeTools.includes(toolName)){
    const hot = pathIsHotZone(paths||[]);
    if(!hot) return null;                              // non-hot-zone write: never gated
    if(st.recallDone) return null;                     // fresh recall THIS turn satisfies it
    if(st.turnIndex-st.writeFiredTurn<cfg.cooldownTurns) return null;
    const already = st.writeFiredTurn===st.turnIndex;
    st.writeFiredTurn=st.turnIndex;
    if(already) return null;
    if(!cfg.enforce) return null;
    return "BLOCK";
  }

  // ── TIER A: cold fs/exec nudge ──
  if(!cfg.fsTools.includes(toolName)) return null;
  if(st.recallDone) return null;
  if(st.turnIndex-st.firedTurn<cfg.cooldownTurns) return null;
  const alreadyFired = st.firedTurn===st.turnIndex;
  st.firedTurn=st.turnIndex;
  if(alreadyFired) return null;
  if(!cfg.enforce) return null;
  return "BLOCK";
}

let pass=0, fail=0;
function chk(name, got, want){
  const ok = got===want;
  console.log(`${ok?"PASS":"FAIL"}  ${name}  (got=${got} want=${want})`);
  ok?pass++:fail++;
}
const SK="agent:analyst:telegram:direct:x";

// ── TIER A (original behavior, must still hold) ──
_state.clear();
chk("cold exec fires", gate(SK,"exec","msg1"), "BLOCK");
_state.clear();
gate(SK,"memory_search","msg2");
chk("recall-first -> exec silent", gate(SK,"exec","msg2"), null);
_state.clear();
gate(SK,"exec","x");
chk("2nd cold exec same turn = silent", gate(SK,"exec","x"), null);
_state.clear();
chk("web_search never gated", gate(SK,"web_search","btc price"), null);
_state.clear();
gate(SK,"exec","turn-a");
gate(SK,"exec","turn-b");
chk("cooldown suppresses turn2", gate(SK,"exec","turn-b2-distinct"), null);
chk("other agent ignored", gate("agent:cs:telegram:x","exec","y"), null);
_state.clear();
gate(SK,"exec","t1"); gate(SK,"noop","t2"); gate(SK,"noop","t3");
chk("fires again after cooldown", gate(SK,"exec","t4"), "BLOCK");

// ── TIER B (hot-zone write re-arm — the new behavior) ──
// THE core case: recalled in an EARLIER turn, then a LATER turn writes a mechanism
// script with NO fresh recall this turn -> re-arm BLOCK. (Same-turn fresh recall
// would legitimately clear it -- that's the separate 'fresh recall' case below.)
_state.clear();
gate(SK,"memory_search","investigateturn");                            // recall in turn 1
chk("recalled-EARLIER-turn, then hotzone WRITE next turn re-arms -> BLOCK",
    gate(SK,"edit","buildturn",["scripts/gen_dispatch.sh"]), "BLOCK");
// fresh recall THIS turn (scoped to the build) satisfies it
_state.clear();
gate(SK,"memory_search","bt2");
chk("fresh recall this turn -> hotzone write silent",
    gate(SK,"write","bt2",["scripts/foo.sh"]), null);
// non-hot-zone write never gated (e.g. a memory note, a doc)
_state.clear();
gate(SK,"exec","bt3");                                                  // no recall yet
chk("non-hotzone write (README) never gated",
    gate(SK,"write","bt3",["README.md"]), null);
// LOOP-SAFETY: writing to memory/ is excluded -> never fires (prevents self-loop)
_state.clear();
chk("write to memory/ excluded -> silent",
    gate(SK,"write","bt4",["memory/2026-08-02.md"]), null);
// LOOP-SAFETY: writing to the gate's own dir excluded
_state.clear();
chk("write to extensions/dinomem- excluded -> silent",
    gate(SK,"edit","bt5",["extensions/dinomem-recall-gate/index.ts"]), null);
// LOOP-SAFETY: .backups/ excluded (file-backup.sh writes there)
_state.clear();
chk("write to .backups/ excluded -> silent",
    gate(SK,"write","bt6",[".backups/x.bak"]), null);
// apply_patch to a hot-zone path (paths parsed from patch body upstream), NO fresh
// recall this turn -> BLOCK.
_state.clear();
gate(SK,"memory_get","priorturn7");                                    // recall in an earlier turn
chk("apply_patch hotzone (no fresh recall) re-arms -> BLOCK",
    gate(SK,"apply_patch","bt7",["tools/gen_research.py"]), "BLOCK");
// write cooldown independent of fs cooldown: 2nd hotzone write same turn = silent
_state.clear();
gate(SK,"edit","bt8",["scripts/a.sh"]);                                // fires
chk("2nd hotzone write same turn = silent",
    gate(SK,"edit","bt8",["scripts/b.sh"]), null);
// hotzone write with NO paths (path extraction failed) -> not hot -> silent (fail-open)
_state.clear();
chk("hotzone write with no path info -> silent (fail-open)",
    gate(SK,"edit","bt9",[]), null);
// config path (openclaw.json) is hot-zone
_state.clear();
gate(SK,"exec","bt10");
chk("openclaw.json write re-arms -> BLOCK",
    gate(SK,"edit","bt10",["/root/.openclaw/openclaw.json"]), "BLOCK");

// ── TIER C: fuzzy-recall route enforcement (neuron-only, config-gated) ──
// Mirror the engine's Tier C with a route-enabled cfg. Base default is
// routeEnforce:false (proven below: gatedTool passes untouched); neuron sets true.
// NOTE: base cfg.recallTools LISTS memory_search (it counts as a recall). But under
// Tier C (neuron), memory_search is the GATED raw tool -- the door is hybrid_recall.
// So the neuron recallTools set must DROP memory_search (it no longer "satisfies"
// recall by itself; you go through the door). This mirrors what neuron's plugin.json
// ships. If memory_search stayed in recallTools, the recall-latch would fire before
// Tier C ever sees it -> gate dead. This is the key base-vs-neuron config difference.
const cfgC = {
  ...cfg,
  recallTools: ["memory_get","graph_search","session_search","semantic_search","docs_search","data_query","hybrid_recall"],
  routeEnforce: true, routeTool: "hybrid_recall", gatedTool: "memory_search",
};
const _stateC = new Map();
function gateC(sessionKey, toolName, msg){
  const c = cfgC;
  if(c.agentFilter && !sessionKey.includes(c.agentFilter)) return null;
  if(!toolName) return null;
  const turnId = fp(msg||sessionKey);
  let st=_stateC.get(sessionKey);
  if(!st||st.turnId!==turnId){
    const turnIndex=(st?.turnIndex??0)+1;
    st={turnId,turnIndex,recallDone:false,firedTurn:st?.firedTurn??-Infinity,writeFiredTurn:st?.writeFiredTurn??-Infinity,routeFiredTurn:st?.routeFiredTurn??-Infinity};
    _stateC.set(sessionKey,st);
  }
  if(c.recallTools.includes(toolName)){st.recallDone=true;return null;}
  if(c.routeEnforce && toolName===c.gatedTool){
    if(st.recallDone) return null;               // door opened this turn -> exempt
    if(!c.enforce) return null;
    const firedThisTurn = st.routeFiredTurn===st.turnIndex;
    st.routeFiredTurn = st.turnIndex;
    if(!firedThisTurn) return "SOFT";            // first reach = soft nudge
    return "HARD";                                // repeat = hard block
  }
  return null;
}
const SKC = "agent:analyst:telegram:direct:1";
// base-safe: with routeEnforce OFF (base default cfg), gatedTool passes untouched
chk("TierC OFF (base): memory_search passes untouched",
    gate(SKC,"memory_search","c0"), null);
// neuron ON: first raw memory_search this turn = soft nudge
_stateC.clear();
chk("TierC ON: 1st raw memory_search -> SOFT nudge",
    gateC(SKC,"memory_search","c1"), "SOFT");
// same turn, repeat raw memory_search = hard block
chk("TierC ON: 2nd raw memory_search same turn -> HARD block",
    gateC(SKC,"memory_search","c1"), "HARD");
// EXEMPT: hybrid_recall ran this turn -> memory_search allowed (protects --external-hits)
_stateC.clear();
gateC(SKC,"hybrid_recall","c2");                 // door opened (in recallTools -> recallDone)
chk("TierC ON: memory_search AFTER hybrid_recall -> exempt (null)",
    gateC(SKC,"memory_search","c2"), null);
// new turn resets: soft nudge fires again
gateC(SKC,"memory_search","c3-newturn");
chk("TierC ON: new turn re-arms soft nudge",
    gateC(SKC,"memory_search","c3-newturn"), "HARD");  // 2nd in the c3 turn = hard
// non-gated tool untouched under Tier C
_stateC.clear();
chk("TierC ON: memory_get (not gatedTool) untouched",
    gateC(SKC,"memory_get","c4"), null);

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail?1:0);
