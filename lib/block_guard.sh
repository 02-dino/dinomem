# shellcheck shell=bash
# block_guard.sh — detect a HAND-EDITED managed block before an installer
# re-injects over it.
#
# WHY THIS EXISTS (measured 2026-10-19): the dinotrust block in
# workspace-analyst/AGENTS.md was hand-hardened — the stock market-analyst preset
# emits `memory_search: true` under non_owner.allowed, while the hand-edited copy
# DENIES the whole dinomem read path. A plain `install.sh --force` would have
# silently re-emitted the preset and reopened that hole, with no diff shown and no
# error raised. Marker-scoped upsert is idempotent for the INSTALLER's own output,
# but it is destructive for a human's edit inside the same markers.
#
# CONTRACT: the installer records a checksum of the block IT wrote. On the next
# run, the block currently on disk is re-checksummed. Equal -> installer output,
# safe to refresh. Different -> a human edited it, so STOP and make them merge.
# No recorded checksum (pre-guard install) -> fail OPEN, write, and record, so one
# upgrade arms the guard instead of blocking every legacy box.
#
# GOTCHA: the sidecar is the only state. Deleting it downgrades the next run to
# fail-open (writes + re-arms), it never hard-fails — losing a sidecar must not
# brick an install.

# sha256 of stdin. shasum fallback covers macOS, where sha256sum is absent.
_bg_sha() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum | awk '{print $1}'
  else shasum -a 256 | awk '{print $1}'
  fi
}

# _bg_extract FILE BEGIN END — print the inclusive BEGIN..END span, or nothing.
# Fixed-string match (index(), not regex) so marker text containing regex
# metacharacters — '# --- dinotrust begin (v1.30.0) ---', '<!-- BEGIN:x -->' —
# matches literally. Stops at the FIRST complete span: a second span means the
# file is already corrupt, which is the installer's own marker-invariant problem.
_bg_extract() {
  awk -v b="$2" -v e="$3" '
    !inblk && index($0,b) { inblk=1; print; next }
    inblk { print; if (index($0,e)) exit }
  ' "$1" 2>/dev/null
}

# _bg_sidecar FILE TAG — hidden sidecar beside the target file.
_bg_sidecar() {
  printf '%s/.%s.%s.blockhash' "$(dirname "$1")" "$(basename "$1")" "$2"
}

# block_guard_check FILE BEGIN END TAG FORCE_FLAG
#   0 = safe to write. 1 = hand-edited, caller must NOT write.
#   FORCE_FLAG in {1,true} overrides (owner explicitly asked to overwrite).
# Prints nothing on the happy path; the caller owns user-facing messaging so this
# stays usable from installers with different log helpers (ok/warn vs info/error).
block_guard_check() {
  _bg_file="$1"; _bg_begin="$2"; _bg_end="$3"; _bg_tag="$4"; _bg_force="${5:-0}"
  [ -f "$_bg_file" ] || return 0
  case "$_bg_force" in 1|true|yes) return 0 ;; esac
  _bg_side="$(_bg_sidecar "$_bg_file" "$_bg_tag")"
  [ -f "$_bg_side" ] || return 0          # never recorded -> fail open, arm later
  # Sentinel: a block hand-edited BEFORE the guard existed has no trustworthy
  # baseline to compare against — recording its current hash would mark the
  # hand-edit as "installer output" and green-light the very overwrite we want to
  # stop. Writing PROTECTED pins it closed until someone passes --force.
  if [ "$(head -n1 "$_bg_side" 2>/dev/null)" = "PROTECTED" ]; then return 1; fi
  # Hash the TEXT only when a span actually exists. Hashing unconditionally is the
  # trap: sha256sum of empty input is a valid digest (e3b0c442...), not an empty
  # string, so a file with NO block would hash-mismatch and read as hand-edited.
  _bg_txt="$(_bg_extract "$_bg_file" "$_bg_begin" "$_bg_end")"
  [ -n "$_bg_txt" ] || return 0           # no block on disk -> fresh wire
  _bg_cur="$(printf '%s\n' "$_bg_txt" | _bg_sha)"
  _bg_was="$(cat "$_bg_side" 2>/dev/null)"
  [ "$_bg_cur" = "$_bg_was" ]
}

# block_guard_record FILE BEGIN END TAG — call AFTER a successful write.
block_guard_record() {
  _bg_side="$(_bg_sidecar "$1" "$4")"
  # Never silently clear a PROTECTED pin: only an explicit --force write (which
  # removes the sidecar upstream) should retire it.
  [ "$(head -n1 "$_bg_side" 2>/dev/null)" = "PROTECTED" ] && return 0
  _bg_txt="$(_bg_extract "$1" "$2" "$3")"
  # Same empty-input trap as above: only record when a span was really found,
  # otherwise we'd arm the guard with the digest of nothing.
  [ -n "$_bg_txt" ] || return 0
  printf '%s\n' "$_bg_txt" | _bg_sha > "$_bg_side" 2>/dev/null
  return 0
}
