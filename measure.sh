#!/usr/bin/env bash
# Times every variant at M=150/300 and, if `summarize` is installed, shows the
# evaluate_obligation self-time from -Zself-profile.
set -euo pipefail
RUSTC="${RUSTC:-rustc}"
export RUSTC_BOOTSTRAP=1
K="${K:-60}"; D="${D:-6}"

run() { # variant M
  local d; d="$(mktemp -d)"; local t0 t1
  python3 generate.py "$1" "$K" "$D" "$2" > "$d/v.rs"
  t0=$(date +%s%N)
  "$RUSTC" --edition 2021 --crate-type=lib --emit=metadata \
    -Zself-profile="$d" -o "$d/r.rmeta" "$d/v.rs" 2>/dev/null
  t1=$(date +%s%N)
  local eo="(install \`summarize\` for the breakdown)"
  if command -v summarize >/dev/null 2>&1; then
    local p; p="$(ls "$d"/*.mm_profdata 2>/dev/null | head -1)"
    [ -n "$p" ] && eo="$(summarize summarize "${p%.mm_profdata}" 2>/dev/null \
      | grep 'evaluate_obligation ' | grep -oE '[0-9]+\.[0-9]+(ms|s|µs)' | head -1 || true)"
    [ -z "$eo" ] && eo="(not a hot query)"
  fi
  printf "  %-9s M=%-4s wall=%5sms   evaluate_obligation=%s\n" "$1" "$2" $(( (t1 - t0) / 1000000 )) "$eo"
  rm -rf "$d"
}

echo "rustc: $("$RUSTC" -V)   (K=$K fields, depth=$D)"
for v in outlives unified borrowed owned; do
  for M in 150 300; do run "$v" "$M"; done
done
