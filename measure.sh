#!/usr/bin/env bash
# Reproduces: old trait solver `evaluate_obligation` time scales ~linearly with
# the number of structurally-overlapping root goals when the ambient ParamEnv
# carries a region, but stays flat without it. The ONLY difference between the
# two programs is `<'a, U: 'a>` (a region in the ParamEnv) vs `()`.
#
# Requirements: a nightly rustc, or any rustc with RUSTC_BOOTSTRAP=1 (for
# -Zself-profile). Optional: the `summarize` tool from rust-lang/measureme for
# the per-pass breakdown (wall time alone already shows it).
#
# Usage:  RUSTC=/path/to/rustc ./measure.sh    (defaults to `rustc` on PATH)
set -euo pipefail
RUSTC="${RUSTC:-rustc}"
export RUSTC_BOOTSTRAP=1

run() { # rsfile label extra-flags
  local d; d="$(mktemp -d)"
  local t0 t1
  t0=$(date +%s%N)
  "$RUSTC" --crate-type=lib --edition 2021 -Copt-level=3 --emit=metadata ${3:-} \
    -Zself-profile="$d" -Zself-profile-events=default -o "$d/out.rmeta" "$1" 2>/dev/null || true
  t1=$(date +%s%N)
  local eo="(install \`summarize\` for breakdown)"
  if command -v summarize >/dev/null 2>&1; then
    local p; p="$(ls "$d"/*.mm_profdata 2>/dev/null | head -1)"
    # evaluate_obligation is absent from the profile under -Znext-solver (it's no
    # longer a hot query); tolerate that instead of failing under pipefail.
    [ -n "$p" ] && eo="$(summarize summarize "${p%.mm_profdata}" 2>/dev/null \
      | grep 'evaluate_obligation ' | grep -oE '[0-9]+\.[0-9]+(ms|s)' | head -1 || true)"
    [ -z "$eo" ] && eo="(not a hot query)"
  fi
  printf "  %-22s wall=%5sms   evaluate_obligation=%s\n" "$2" $(( (t1 - t0) / 1000000 )) "$eo"
  rm -rf "$d"
}

echo "rustc: $("$RUSTC" -V)"
echo "== A/B at M=150 (K=60 fields, depth=6) =="
python3 gen.py 60 6 150 on  > /tmp/_on.rs;  run /tmp/_on.rs  "region in ParamEnv"
python3 gen.py 60 6 150 off > /tmp/_off.rs; run /tmp/_off.rs "no region"

echo "== scaling, region=on  (grows ~linearly with root count) =="
for M in 75 150 300; do python3 gen.py 60 6 "$M" on  > /tmp/_s.rs; run /tmp/_s.rs "M=$M"; done

echo "== scaling, region=off (stays flat) =="
for M in 75 150 300; do python3 gen.py 60 6 "$M" off > /tmp/_s.rs; run /tmp/_s.rs "M=$M"; done

echo "== (optional) next-gen solver removes the gap =="
python3 gen.py 60 6 150 on > /tmp/_on.rs
run /tmp/_on.rs "region=on -Znext-solver" "-Znext-solver=globally"
