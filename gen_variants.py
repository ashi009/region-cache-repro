#!/usr/bin/env python3
# Isolates the *exact* trigger of the cache blowup and demonstrates the
# zero-runtime-cost fix. Emits a real trait `Svc` with `M` impls, each whose
# returned future genuinely borrows `&self` (so the `Send` proof recurses through
# the shared `Arc<Mutex<Vec<...>>>` graph). The ONLY thing that varies between
# variants is how the method's lifetimes are written:
#
#   outlives  : #[async_trait]'s desugaring — a fresh `'async_trait` lifetime with
#               explicit `Self: 'async_trait`, `'life0: 'async_trait` OUTLIVES
#               where-bounds. -> SLOW (region outlives bound in the ParamEnv
#               defeats the global evaluation cache; re-derived per impl).
#   borrowed  : future lifetime tied directly to `&self` via `+ '_`, NO outlives
#               where-bound. -> FAST. Still borrows `&self` (zero clone). Sound.
#               Works when `&self` is the only borrowed input.
#   unified   : multiple borrowed inputs unified under ONE lifetime `'a`, return
#               `+ 'a`, NO outlives where-bound. -> FAST. Keeps every arg borrowed
#               (zero clone, zero `to_owned`). Sound. Covers the multi-arg case.
#   owned     : the heavier workaround — clone `self` into a `'static` future.
#               -> FAST, but pays a receiver clone (+ `to_owned` of `&T` args) at
#               runtime. Needed only when the future must outlive the borrow
#               (e.g. detached `tokio::spawn`).
#
# Result: `borrowed`/`unified`/`owned` are all ~100x faster than `outlives`,
# proving the trigger is the *outlives where-bound*, not the `&self` borrow, the
# boxed future, or the async block. `borrowed`/`unified` get there with no runtime
# cost at all.
import sys

K = int(sys.argv[2]) if len(sys.argv) > 2 else 60   # fields per Shared (width)
D = int(sys.argv[3]) if len(sys.argv) > 3 else 6    # nesting depth per field
M = int(sys.argv[4]) if len(sys.argv) > 4 else 150  # impls (root goals)
variant = sys.argv[1] if len(sys.argv) > 1 else "outlives"

o = ["use std::sync::{Arc, Mutex};", "use std::pin::Pin;", "use std::future::Future;", ""]
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
elif variant == "borrowed":
    o.append("pub trait Svc { fn check(&self)"
             " -> Pin<Box<dyn Future<Output = u64> + Send + '_>>; }")
    body = ("impl Svc for W{i} {{ fn check(&self)"
            " -> Pin<Box<dyn Future<Output = u64> + Send + '_>>"
            " {{ Box::pin(async move {{ self.n }}) }} }}")
elif variant == "unified":
    o.append("pub trait Svc { fn check<'a>(&'a self, x: &'a [u8])"
             " -> Pin<Box<dyn Future<Output = u64> + Send + 'a>>; }")
    body = ("impl Svc for W{i} {{ fn check<'a>(&'a self, x: &'a [u8])"
            " -> Pin<Box<dyn Future<Output = u64> + Send + 'a>>"
            " {{ Box::pin(async move {{ let _ = x; self.n }}) }} }}")
elif variant == "owned":
    o.append("pub trait Svc: Clone + 'static { fn check(&self)"
             " -> Pin<Box<dyn Future<Output = u64> + Send>>; }")
    body = ("impl Svc for W{i} {{ fn check(&self)"
            " -> Pin<Box<dyn Future<Output = u64> + Send>>"
            " {{ let this = self.clone(); Box::pin(async move {{ this.n }}) }} }}")
    # owned form clones self -> needs Clone; derive it cheaply.
    o = [ln.replace("pub struct W", "#[derive(Clone)]\npub struct W") if ln.startswith("pub struct W") else ln for ln in o]
    o = [ln.replace("pub struct Shared", "#[derive(Clone)]\npub struct Shared") if ln.startswith("pub struct Shared") else ln for ln in o]
    o = [ln.replace("pub struct M", "#[derive(Clone)]\npub struct M") if ln.startswith("pub struct M") else ln for ln in o]
else:
    sys.exit(f"unknown variant {variant!r}; use outlives|borrowed|unified|owned")

for i in range(M):
    o.append(body.format(i=i))
print("\n".join(o))
