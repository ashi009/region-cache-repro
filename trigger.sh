#!/usr/bin/env bash
# Pin down the *exact* trigger and demonstrate the zero-cost fix.
# All four variants prove the SAME thing (`W`'s shared Arc/Mutex graph is Send);
# they differ only in how the async-trait method's lifetimes are written.
#
#   outlives  -> #[async_trait] desugaring (region OUTLIVES where-bound) : SLOW
#   borrowed  -> future tied to &self via `+ '_`, no outlives bound      : FAST, zero runtime cost
#   unified   -> all borrows unified under one `'a`, `+ 'a`, no bound    : FAST, zero runtime cost
#   owned     -> clone self into a `'static` future                      : FAST, pays a clone
#
# Usage:  RUSTC=/path/to/rustc ./trigger.sh
set -euo pipefail
RUSTC="${RUSTC:-rustc}"
export RUSTC_BOOTSTRAP=1
K="${K:-60}"; D="${D:-6}"; M="${M:-150}"

run() { # variant
  local d; d="$(mktemp -d)"; local t0 t1
  python3 gen_variants.py "$1" "$K" "$D" "$M" > "$d/v.rs"
  t0=$(date +%s%N)
  "$RUSTC" --crate-type=lib --edition 2021 -Copt-level=3 --emit=metadata \
    -Zself-profile="$d" -Zself-profile-events=default -o "$d/out.rmeta" "$d/v.rs" 2>"$d/err" || true
  t1=$(date +%s%N)
  local eo="(install \`summarize\`)"
  if command -v summarize >/dev/null 2>&1; then
    local p; p="$(ls "$d"/*.mm_profdata 2>/dev/null | head -1)"
    [ -n "$p" ] && eo="$(summarize summarize "${p%.mm_profdata}" 2>/dev/null \
      | grep 'evaluate_obligation ' | grep -oE '[0-9]+\.[0-9]+(ms|s|µs)' | head -1 || true)"
  fi
  grep -q '^error' "$d/err" 2>/dev/null && eo="COMPILE ERROR: $(grep -m1 '^error' "$d/err")"
  printf "  %-9s wall=%5sms   evaluate_obligation=%s\n" "$1" $(( (t1 - t0) / 1000000 )) "${eo:-<none>}"
  rm -rf "$d"
}

echo "rustc: $("$RUSTC" -V)   (K=$K, D=$D, M=$M impls; each future borrows &self)"
echo "== trigger is the region OUTLIVES where-bound, not the &self borrow =="
for v in outlives borrowed unified owned; do run "$v"; done
