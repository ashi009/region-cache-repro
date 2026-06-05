#!/usr/bin/env python3
# Minimal repro: old trait solver re-derives region-independent auto-trait
# (Send/Sync) proofs once per root goal, because a region inference var in the
# ambient ParamEnv forces results into the per-inference-context LOCAL cache
# (SelectionContext::can_use_global_caches bails on `param_env.has_infer()`,
# which fires on *region* vars). Send/Sync don't depend on regions, so this is
# pure redundant work that scales with the number of root goals.
#
# Shape: one WIDE+DEEP shared struct `Shared` (K distinct deeply-nested fields);
# M wrapper types each contain `Shared`; each wrapper's `Send` is asserted inside
# a function whose ParamEnv carries a free region (`<'a, U: 'a>`).
#
#   region=on  : fn check_i<'a, U: 'a>(_:U) { assert_send::<Wi>(); }  (region in ParamEnv)
#   region=off : fn check_i()              { assert_send::<Wi>(); }   (empty ParamEnv)
#
# region=on  -> evaluate_obligation time grows ~linearly with M (re-derivation).
# region=off -> evaluate_obligation stays ~flat (shared sub-proofs cached globally).
import sys

K = int(sys.argv[1]) if len(sys.argv) > 1 else 60    # fields per Shared (width)
D = int(sys.argv[2]) if len(sys.argv) > 2 else 6     # nesting depth per field
M = int(sys.argv[3]) if len(sys.argv) > 3 else 150   # wrapper types (root goals)
region = (sys.argv[4] if len(sys.argv) > 4 else "on") == "on"

o = ["use std::sync::{Arc, Mutex};", ""]
for i in range(K):
    o.append(f"pub struct M{i};")                    # distinct leaf markers
o.append("")
for i in range(K):
    t = f"M{i}"
    for _ in range(D):
        t = f"Arc<Mutex<Vec<{t}>>>"
    o.append(f"pub type F{i} = {t};")
o.append("")
o.append("pub struct Shared { " + ", ".join(f"pub f{i}: F{i}" for i in range(K)) + " }")
o.append("")
for i in range(M):
    o.append(f"pub struct W{i} {{ pub s: Shared }}")
o.append("")
o.append("pub fn assert_send<T: Send>() {}")
o.append("")
for i in range(M):
    if region:
        o.append(f"pub fn check{i}<'a, U: 'a>(_u: U) {{ assert_send::<W{i}>(); }}")
    else:
        o.append(f"pub fn check{i}() {{ assert_send::<W{i}>(); }}")
print("\n".join(o))
