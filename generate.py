#!/usr/bin/env python3
# Emit one trait `Svc` with M impls, each wrapping the same `Shared` struct
# (K fields of Arc<Mutex<Vec<...>>> nested D deep); every method's future
# borrows `&self`. Variants differ ONLY in how the method's lifetimes are
# spelled — see README for which ones defeat the global evaluation cache.
#
#   outlives : `where Self: 'at, 'life0: 'at` — the #[async_trait] shape
#   unified  : same borrows under one lifetime `'a`, no outlives clause
#   borrowed : `&self`-only, future tied via `+ '_`
#   owned    : clone self into a 'static future
import sys

variant = sys.argv[1] if len(sys.argv) > 1 else "outlives"
K = int(sys.argv[2]) if len(sys.argv) > 2 else 150  # fields per Shared (width)
D = int(sys.argv[3]) if len(sys.argv) > 3 else 10   # nesting depth per field
M = int(sys.argv[4]) if len(sys.argv) > 4 else 150  # impls (root goals)

o = [f"// Auto-generated: generate.py {variant} {K} {D} {M}. Repro for rust-lang/rust#157595. See README.",
     "use std::sync::{Arc, Mutex};", "use std::pin::Pin;", "use std::future::Future;", ""]
for i in range(K):
    o.append(f"pub struct M{i};")
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
    o.append(f"pub struct W{i} {{ pub s: Shared, pub n: u64 }}")
o.append("")

if variant == "outlives":
    o.append("pub trait Svc { fn check<'life0, 'at>(&'life0 self, x: &'at [u8])"
             " -> Pin<Box<dyn Future<Output = u64> + Send + 'at>>"
             " where Self: 'at, 'life0: 'at; }")
    body = ("impl Svc for W{i} {{ fn check<'life0, 'at>(&'life0 self, x: &'at [u8])"
            " -> Pin<Box<dyn Future<Output = u64> + Send + 'at>>"
            " where Self: 'at, 'life0: 'at"
            " {{ Box::pin(async move {{ let _ = x; self.n }}) }} }}")
elif variant == "unified":
    o.append("pub trait Svc { fn check<'a>(&'a self, x: &'a [u8])"
             " -> Pin<Box<dyn Future<Output = u64> + Send + 'a>>; }")
    body = ("impl Svc for W{i} {{ fn check<'a>(&'a self, x: &'a [u8])"
            " -> Pin<Box<dyn Future<Output = u64> + Send + 'a>>"
            " {{ Box::pin(async move {{ let _ = x; self.n }}) }} }}")
elif variant == "borrowed":
    o.append("pub trait Svc { fn check(&self)"
             " -> Pin<Box<dyn Future<Output = u64> + Send + '_>>; }")
    body = ("impl Svc for W{i} {{ fn check(&self)"
            " -> Pin<Box<dyn Future<Output = u64> + Send + '_>>"
            " {{ Box::pin(async move {{ self.n }}) }} }}")
elif variant == "owned":
    o.append("pub trait Svc: Clone + 'static { fn check(&self)"
             " -> Pin<Box<dyn Future<Output = u64> + Send>>; }")
    body = ("impl Svc for W{i} {{ fn check(&self)"
            " -> Pin<Box<dyn Future<Output = u64> + Send>>"
            " {{ let this = self.clone(); Box::pin(async move {{ this.n }}) }} }}")
    o = ["#[derive(Clone)]\n" + ln if ln.startswith("pub struct ") else ln for ln in o]
else:
    sys.exit(f"unknown variant {variant!r}; use outlives|unified|borrowed|owned")

for i in range(M):
    o.append(body.format(i=i))
print("\n".join(o))
