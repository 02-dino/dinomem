#!/usr/bin/env bash
# dinomem grep-guard — installer.
# Installs a PATH-ahead shim at /usr/local/bin/grep that blocks ONLY broad
# recursive greps over large directory trees (size/depth heuristic), returning
# exit 2 with a scope-down hint. Every other grep invocation is passed straight
# through to the real binary untouched.
#
# WHY: broad `grep -r` over huge roots (a home dir, a repo of session logs, a kb)
# is slow AND — for an LLM agent — floods the context window with noise. This
# guard nudges the caller toward a scoped search (rg on the smallest dir, or a
# timeout-bounded grep) instead of scanning the whole tree.
#
# ── ANNOUNCED, NOT SILENT ────────────────────────────────────────────────────
# This shim sits AHEAD of the real grep in PATH, so it changes `grep` behavior
# for the WHOLE machine (your shell, cron, build scripts). That is a big enough
# side effect that this installer prints a LOUD banner naming exactly what it
# does and how to remove it. A memory plugin must never silently hijack a core
# Unix binary. Default-on is fine — silent is not.
#
# Heuristic (size/depth-based, conservative — errs toward ALLOW):
#   A grep call is blocked ONLY when ALL of these hold:
#     - it is recursive (-r / -R / --recursive present), AND
#     - a target path resolves to a directory whose recursive file count
#       exceeds GREPGUARD_MAX_FILES (default 20000)
#       OR whose depth exceeds GREPGUARD_MAX_DEPTH (default 12), AND
#     - the caller is NOT the real binary path (/usr/bin/grep bypasses always).
#   Anything below the thresholds, or non-recursive, passes through.
#
# Usage:
#   bash install.sh [--prefix DIR] [--max-files N] [--max-depth N]
#                   [--force] [--dry-run] [--uninstall] [--quiet]
#
# Options:
#   --prefix DIR   dir to install the shim into (must be ahead of real grep in
#                  PATH). Default: /usr/local/bin
#   --max-files N  recursive-file-count threshold to block (default 20000)
#   --max-depth N  directory-depth threshold to block (default 12)
#   --force        overwrite an existing shim (default: skip if present & ours)
#   --dry-run      preview only, write nothing
#   --uninstall    remove the shim (only if it's ours — never touches a real grep)
#   --quiet        suppress the banner (for automated re-runs; still prints result)
#
# DUP-AWARE: if our shim already exists, re-running is a safe no-op unless --force.
# SAFETY: refuses to overwrite a target that is NOT our shim (won't clobber a real
# grep binary or someone else's wrapper).
set -euo pipefail
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="/usr/local/bin"
MAX_FILES=20000
MAX_DEPTH=12
FORCE=0
DRY_RUN=0
UNINSTALL=0
QUIET=0
MARKER="# dinomem-grep-guard v1 (managed shim — safe to delete)"

ok()   { printf '  \033[32m[ok]\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m[warn]\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m[fail]\033[0m %s\n' "$*"; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix)     PREFIX="$2"; shift 2 ;;
    --max-files)  MAX_FILES="$2"; shift 2 ;;
    --max-depth)  MAX_DEPTH="$2"; shift 2 ;;
    --force)      FORCE=1; shift ;;
    --dry-run)    DRY_RUN=1; shift ;;
    --uninstall)  UNINSTALL=1; shift ;;
    --quiet)      QUIET=1; shift ;;
    -h|--help)    grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) fail "unknown arg: $1" ;;
  esac
done

# Track whether --prefix was passed EXPLICITLY (vs the default). If explicit, we
# honor it verbatim. If NOT, we self-heal: adopt an existing shim's location, or
# auto-pick the first writable PATH dir that precedes the real grep.
PREFIX_EXPLICIT=0
for a in "$@"; do [ "$a" = "--prefix" ] && PREFIX_EXPLICIT=1; done

# Resolve the REAL grep (never our own shim). Prefer known absolute paths.
REAL_GREP=""
for cand in /usr/bin/grep /bin/grep; do
  if [ -x "$cand" ] && ! grep -qF "$MARKER" "$cand" 2>/dev/null; then
    REAL_GREP="$cand"; break
  fi
done
[ -n "$REAL_GREP" ] || REAL_GREP="/usr/bin/grep"
RG_DIR="$(dirname "$REAL_GREP")"

# ── Scan the ENTIRE PATH for existing dinomem shims (dedup + adopt) ───────────
# EXISTING_SHIMS = every dir on PATH that currently holds OUR shim. This is what
# makes reinstall idempotent across the whole machine instead of per-prefix:
#   - adopt: an unset --prefix re-targets the shim that already exists (update in
#     place, no second copy elsewhere).
#   - sweep: any OTHER shim dirs beyond the one we keep are removed at write time
#     so exactly one shim survives.
declare -a EXISTING_SHIMS=()
IFS=: read -ra _PATH_DIRS <<< "$PATH"
for d in "${_PATH_DIRS[@]}"; do
  [ -n "$d" ] || continue
  if [ -f "$d/grep" ] && grep -qF "$MARKER" "$d/grep" 2>/dev/null; then
    EXISTING_SHIMS+=("$d")
  fi
done

# ── Pick the install dir when --prefix was NOT explicit ──────────────────────
# Priority: (1) adopt an existing shim's dir (update in place), else (2) the
# first writable PATH dir that PRECEDES the real grep (so the shim actually
# fires), else (3) fall back to the compiled-in default $PREFIX (with a warning
# later if it turns out inert).
if [ "$PREFIX_EXPLICIT" != 1 ]; then
  if [ "${#EXISTING_SHIMS[@]}" -gt 0 ]; then
    PREFIX="${EXISTING_SHIMS[0]}"
  else
    for d in "${_PATH_DIRS[@]}"; do
      [ -n "$d" ] || continue
      [ "$d" = "$RG_DIR" ] && break   # reached real grep's dir first -> stop; nothing earlier is writable
      if [ -d "$d" ] && [ -w "$d" ]; then PREFIX="$d"; break; fi
    done
  fi
fi
SHIM_DST="$PREFIX/grep"

# ── Uninstall ────────────────────────────────────────────────────────────────
if [ "$UNINSTALL" = 1 ]; then
  if [ -f "$SHIM_DST" ] && grep -qF "$MARKER" "$SHIM_DST" 2>/dev/null; then
    if [ "$DRY_RUN" = 1 ]; then ok "[dry-run] would remove shim at $SHIM_DST"; exit 0; fi
    rm -f "$SHIM_DST" && ok "grep-guard removed from $SHIM_DST" || fail "could not remove $SHIM_DST"
  else
    ok "no dinomem grep-guard shim at $SHIM_DST — nothing to remove"
  fi
  exit 0
fi

# ── Pre-flight ───────────────────────────────────────────────────────────────
[ -d "$PREFIX" ] || { [ "$DRY_RUN" = 1 ] || mkdir -p "$PREFIX" 2>/dev/null || fail "cannot create $PREFIX"; }

if [ -f "$SHIM_DST" ]; then
  if grep -qF "$MARKER" "$SHIM_DST" 2>/dev/null; then
    if [ "$FORCE" != 1 ]; then ok "grep-guard already installed at $SHIM_DST (idempotent no-op; --force to refresh)"; exit 0; fi
  else
    fail "$SHIM_DST exists and is NOT our shim (looks like a real grep or foreign wrapper) — refusing to overwrite. Move it or pick a different --prefix."
  fi
fi

# Warn if PREFIX is not actually ahead of the real grep in PATH (shim would be inert).
case ":$PATH:" in
  *":$PREFIX:"*)
    # confirm ordering: PREFIX must appear before the real grep's dir
    RG_DIR="$(dirname "$REAL_GREP")"
    if [ "$PREFIX" != "$RG_DIR" ]; then
      before=1
      IFS=: read -ra _pd <<< "$PATH"
      for d in "${_pd[@]}"; do
        [ "$d" = "$PREFIX" ] && break
        [ "$d" = "$RG_DIR" ] && { before=0; break; }
      done
      [ "$before" = 1 ] || warn "$PREFIX is in PATH but AFTER $RG_DIR — the shim may be inert. Ensure $PREFIX precedes $RG_DIR."
    fi
    ;;
  *) warn "$PREFIX is not in PATH — the shim will not intercept grep until it is.";;
esac

# ── Shim body ────────────────────────────────────────────────────────────────
read -r -d '' SHIM_BODY <<SHIM_EOF || true
#!/usr/bin/env bash
$MARKER
# Blocks ONLY broad recursive greps over large trees (size/depth heuristic).
# Everything else is passed straight through to the real grep. Bypass anytime by
# calling the real binary directly: $REAL_GREP
# Tunables (env overrides win): GREPGUARD_MAX_FILES / GREPGUARD_MAX_DEPTH / GREPGUARD_OFF=1
set -u
REAL="$REAL_GREP"
MAXF="\${GREPGUARD_MAX_FILES:-$MAX_FILES}"
MAXD="\${GREPGUARD_MAX_DEPTH:-$MAX_DEPTH}"

# Kill switch + safety: if disabled or real grep missing, just exec real grep.
if [ "\${GREPGUARD_OFF:-0}" = 1 ] || [ ! -x "\$REAL" ]; then exec "\$REAL" "\$@"; fi

recursive=0
declare -a paths=()
for a in "\$@"; do
  case "\$a" in
    -[rR]|--recursive|--dereference-recursive) recursive=1 ;;
    -*[rR]*) case "\$a" in --*) : ;; -*) recursive=1 ;; esac ;;
    --) : ;;
  esac
  # collect existing directory operands as candidate targets
  if [ -d "\$a" ]; then paths+=("\$a"); fi
done

# Non-recursive, or no directory target => pass through untouched.
if [ "\$recursive" != 1 ] || [ "\${#paths[@]}" -eq 0 ]; then exec "\$REAL" "\$@"; fi

for p in "\${paths[@]}"; do
  # depth check (cheap): any dir deeper than MAXD under p ?
  if find "\$p" -type d -mindepth "\$MAXD" -print -quit 2>/dev/null | grep -q .; then
    printf 'grep-guard: BLOCKED — recursive grep over a deep tree (%s, depth>=%s).\n' "\$p" "\$MAXD" >&2
    printf 'grep-guard: scope down (rg <pat> <small-dir>) or bypass: %s <args>\n' "\$REAL" >&2
    exit 2
  fi
  # file-count check (bounded): stop counting once over the threshold.
  n=\$(find "\$p" -type f 2>/dev/null | head -n "\$((MAXF+1))" | wc -l | tr -d ' ')
  if [ "\$n" -gt "\$MAXF" ]; then
    printf 'grep-guard: BLOCKED — recursive grep over a large tree (%s, >%s files).\n' "\$p" "\$MAXF" >&2
    printf 'grep-guard: scope down (rg <pat> <small-dir>) or bypass: %s <args>\n' "\$REAL" >&2
    exit 2
  fi
done

# Under thresholds => allow.
exec "\$REAL" "\$@"
SHIM_EOF

# ── Write ────────────────────────────────────────────────────────────────────
if [ "$DRY_RUN" = 1 ]; then
  ok "[dry-run] would install grep-guard shim -> $SHIM_DST (real grep: $REAL_GREP; max-files=$MAX_FILES depth=$MAX_DEPTH)"
  exit 0
fi

printf '%s\n' "$SHIM_BODY" > "$SHIM_DST" || fail "could not write $SHIM_DST (need write perms on $PREFIX; try sudo)"
chmod 0755 "$SHIM_DST" || fail "could not chmod $SHIM_DST"

# ── Sweep stale dupes ────────────────────────────────────────────────────────
# Remove any OTHER dinomem shims found elsewhere on PATH so exactly one survives
# (the one we just wrote). This is what makes a reinstall to a different prefix
# self-heal instead of leaving a second, possibly-inert, shim behind.
for d in "${EXISTING_SHIMS[@]}"; do
  [ "$d" = "$PREFIX" ] && continue
  if [ -f "$d/grep" ] && grep -qF "$MARKER" "$d/grep" 2>/dev/null; then
    rm -f "$d/grep" 2>/dev/null && warn "swept stale duplicate shim at $d/grep (kept the one at $SHIM_DST)" || warn "could not remove stale shim at $d/grep — remove manually"
  fi
done

# ── Loud banner ──────────────────────────────────────────────────────────────
if [ "$QUIET" != 1 ]; then
  printf '\n'
  printf '\033[33m ⚠  dinomem grep-guard installed at %s\033[0m\n' "$SHIM_DST"
  printf '    Blocks ONLY broad recursive greps over large trees (>%s files or depth>=%s) → exit 2.\n' "$MAX_FILES" "$MAX_DEPTH"
  printf '    Normal / scoped grep is UNTOUCHED. Real binary always reachable at %s\n' "$REAL_GREP"
  printf '    Disable once:   GREPGUARD_OFF=1 grep ...\n'
  printf '    Remove anytime: bash %s --uninstall\n' "$SELF_DIR/install.sh"
  printf '\n'
fi
ok "grep-guard active (max-files=$MAX_FILES, max-depth=$MAX_DEPTH, real=$REAL_GREP)"
