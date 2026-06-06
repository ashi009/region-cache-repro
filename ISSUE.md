# Old trait solver re-derives region-independent auto-trait obligations once per root goal (`can_use_global_caches` bails on region inference vars)

### Summary

In the old (default) trait solver, `evaluate_obligation` compile time scales
**linearly with the number of structurally-overlapping root goals** when the goal
is proven under a `ParamEnv` that carries a **region**, and is **flat**
otherwise. The only difference between the slow and fast versions is a lifetime
in the `ParamEnv`. Auto-traits (`Send`/`Sync`/…) don't depend on regions, so the
extra work is pure redundant re-derivation.

`SelectionContext::can_use_global_caches`
(`compiler/rustc_trait_selection/src/traits/select/mod.rs`) refuses the shared
global evaluation cache whenever `param_env.has_infer()` — and that fires on
**region** inference variables. The `evaluate_obligation` query canonicalizes the
ambient `ParamEnv` and `build_with_canonical` re-instantiates its regions as
inference vars in a fresh `InferCtxt`, so practically every obligation proven
under a lifetime-bearing `ParamEnv` (e.g. `&'a self` methods) is forced into the
**per-`InferCtxt` local** cache and re-derived from scratch on every root goal.

### Repro

`gen.py` (in the linked repo) emits a program with `K` distinct deeply-nested
`Arc<Mutex<Vec<…>>>` field types in one shared struct, `M` wrapper types that
each contain it, and `M` functions asserting `Send` of a wrapper. The wrapper's
`Send` is proven either with a region in the `ParamEnv` or without:

```rust
// region in ParamEnv (slow):
pub fn check0<'a, U: 'a>(_u: U) { assert_send::<W0>(); }
// no region (fast):
pub fn check0() { assert_send::<W0>(); }
```

```console
$ RUSTC_BOOTSTRAP=1 rustc --crate-type=lib -Copt-level=3 --emit=metadata \
      -Zself-profile=. -Zself-profile-events=default region_on.rs
$ summarize summarize region_on
```

### Measurements (rustc 1.94.1, `-C opt-level=3`, `--emit=metadata`)

A/B at `M=150`, `K=60`, depth `6` — identical goals, only the ParamEnv region differs:

| program | `evaluate_obligation` self time | wall |
|---|---|---|
| region in ParamEnv (`<'a, U: 'a>`) | **455.5 ms** | 492 ms |
| no region (`()`) | **3.9 ms** | 38 ms |

Scaling in number of root goals `M` (`evaluate_obligation` self time):

| M | region=on | region=off |
|---|---|---|
| 75 | 224 ms | 3.30 ms |
| 150 | 453 ms | 3.44 ms |
| 300 | 894 ms | 3.67 ms |

region=on grows ~linearly with the root count; region=off is flat. The next-gen solver
(`-Znext-solver=globally`) does not exhibit the blowup on this repro.

### Root cause

```rust
// rustc_trait_selection/src/traits/select/mod.rs, can_use_global_caches:
if param_env.has_infer() || pred.has_infer() {
    return false; // -> per-InferCtxt local cache, not shared across queries
}
```

Each `evaluate_obligation` query builds a fresh `InferCtxt`
(`build_with_canonical`) whose `ParamEnv` regions are inference vars, so
`has_infer()` is true and the result never reaches `tcx.evaluation_cache`.
Structurally-shared sub-proofs (`Arc<T>: Send ⇒ T: Send + T: Sync ⇒ …`) are
re-derived per root. The result does not actually depend on the regions
(region-sensitive results are already surfaced as `EvaluatedToOkModuloRegions`).

### Why the `has_infer()` gate is sound in general but too coarse here

The blanket `has_infer()` bail is the conservative fix for historical
unsoundness (#22019, #18290): a trait result computed while an inference variable
is still unconstrained can become wrong once that variable is later resolved, and
the `tcx`-global cache outlives the `InferCtxt` whose variables the result
depended on. So a result that depends on inference state must not be shared
globally. For **type/const** inference variables this is exactly right.

For **region** inference variables it is unnecessary, and the codebase already
treats region-independent goals as region-independent in two places:

1. **The cache key's predicate half is already region-erased.** The freshener
   (`compiler/rustc_infer/src/infer/freshen.rs`), which produces the evaluation
   cache key, erases *all* free regions to `'erased` — its own module comment:
   *"the freshener also replaces all free regions with `'erased` … in general, we
   do not take region relationships into account when making type-overloaded
   decisions."* (`fold_region` erases `ReVar | ReEarlyParam | ReLateParam |
   RePlaceholder | ReStatic | ReErased`.) The asymmetry is that the **predicate**
   is region-erased but the **`ParamEnv`** is not — it still carries the live
   region inference vars that `can_use_global_caches` then rejects on.

2. **Region-sensitivity is already encoded in the *result*, not by cache
   exclusion.** `EvaluatedToOk` vs `EvaluatedToOkModuloRegions` distinguishes
   "holds region-independently" from "holds only modulo region constraints, which
   borrowck must re-check separately." The framework already knows how to say "this
   answer is region-independent."

The tools to narrow the gate already exist and are used elsewhere in the same
file: `has_non_region_infer()` (e.g. the `TypeOutlives` fast path) and
`erase_and_anonymize_regions`. So treating region infer vars like type/const
infer vars is a missed optimization, not a required soundness constraint —
**caveat:** full soundness for region-*sensitive* **selection** (as opposed to
evaluation) would still need review, since the freshener deliberately preserves
bound regions / the leak check, and `EvaluatedToOkModuloRegions` exists precisely
because some answers are not region-independent.

### Why the next-gen solver is the long-term home, not a workaround today

The new solver caches by **canonicalization** rather than freshening, and
canonicalizes the *entire* goal including the `ParamEnv` — free regions and region
inference vars become canonical region variables instead of disqualifying the goal
from the global cache (per the
[canonicalization docs](https://rustc-dev-guide.rust-lang.org/traits/canonicalization.html),
"the trait system generally ignores all lifetimes … we will also replace any free
lifetime with a canonical variable"). Two goals differing only by a `ParamEnv`
lifetime canonicalize to the same key and share the answer — which is why the repro
does not blow up under `-Znext-solver=globally`.

But it is not a usable workaround on stable today:

- `-Znext-solver=globally` is nightly-only (`-Z` flag). Only `-Znext-solver=coherence`
  is stable (since 1.84).
- Global stabilization is an in-progress 2026 project goal
  ([rust-lang/rust-project-goals#113](https://github.com/rust-lang/rust-project-goals/issues/113),
  tracking [#114862](https://github.com/rust-lang/rust/issues/114862)) that has **not
  yet reached performance parity** with the old solver, with crater triage and a
  cycle-semantics RFC still open.

So the old-solver fix is worth doing on its own, with the new solver as the
eventual home.

### Real-world impact — and why this is a common async (axum/tokio) shape

On a large async-heavy codebase, `evaluate_obligation` is the dominant frontend
cost (~35%, ≈68 s/crate at `-C opt`). Instrumenting the nested
`evaluate_trait_predicate_recursively` showed, for one crate: 60.5M actual
evaluations of only 50,783 distinct predicates (**1,191× re-derivation**), with
**99.3%** of cache inserts going to the local cache, **100%** of those blocked
solely by region-only `ParamEnv` inference. Two sibling crates: 592× and 638×.
The cost is overwhelmingly `Send`/`Sync` of ordinary `Adt` std containers, not
coroutine witnesses.

The repro shape is the controlled version of an everyday async server:

| repro | axum/tokio |
|---|---|
| one shared `Arc<Mutex<Vec<…>>>` struct | one `Arc<AppState>` of nested `Arc<Mutex/RwLock<…>>`, pools, maps |
| `M` wrappers each containing it | `N` handler futures each capturing `State<Arc<AppState>>` across `.await` |
| `M` `assert_send::<Wi>()` | `N` handlers whose `type Future: … + Send` must hold (`tokio::spawn` needs `Future: Send + 'static`; axum `Handler` requires a `Send` future) |
| region in `ParamEnv` (`<'a, U: 'a>`) | free lifetimes from `&self`, borrowed extractors (`FromRequestParts`), `async fn` desugaring |

i.e. *N handlers each re-prove `Send`/`Sync` of structurally-overlapping shared
state under a region-bearing `ParamEnv`*. The symptom is widely reported in
async/axum codebases (e.g. [#87012](https://github.com/rust-lang/rust/issues/87012),
where `evaluate_obligation` is ~85% of compile time; axum
[#200](https://github.com/tokio-rs/axum/issues/200)) but is generally attributed
to generic monomorphization or nested builder types — not to region-driven cache
bypass.

### The precise trigger: a region *outlives where-bound*, not the `&self` borrow

`gen_variants.py` / `trigger.sh` isolate exactly what flips the cache. All four
variants below prove the *same* goal — `W`'s shared `Arc<Mutex<…>>` graph is
`Send` — and each future genuinely borrows `&self`; they differ only in how the
method's lifetimes are written (`M=150`, verified on 1.96.0 stable & 1.98.0
nightly, `evaluate_obligation` self time):

| variant | lifetimes on the method | self time | runtime cost |
|---|---|---|---|
| `outlives`  | `#[async_trait]`: `'async_trait` + `Self: 'async_trait`, `'life0: 'async_trait` | **1.19 s** | — |
| `borrowed`  | future tied to `&self` via `+ '_`, **no** outlives bound | **11.6 ms** | **none** |
| `unified`   | all borrows unified under one `'a`, `+ 'a`, **no** outlives bound | **11.6 ms** | **none** |
| `owned`     | clone `self` into a `'static` future | 5.4 ms | a clone per call |

So the trigger is specifically the **region outlives where-bound in the
`ParamEnv`** (`Self: 'async_trait`, `'life0: 'async_trait`). A `&self` async
future is *not* slow per se — only the outlives bound is. `#[async_trait]` emits
that bound unconditionally (it needs a single lower-bound lifetime to unify
*multiple* borrowed inputs), so even a method that borrows only `&self` pays for
it. This is largely an **`#[async_trait]`-the-macro artifact**, not a language
necessity.

Confirmed with the *real* `async_trait 0.1.88` macro on a production codebase
(rustc 1.94.1): a service trait over a deep shared state, scaled to `M=150` impls,
spent **1.36 s** in `evaluate_obligation` under `#[async_trait]` vs **13.8 ms**
rewritten to the borrowed `+ '_` form — a ~98× drop from the lifetime alone. In
that same codebase, ordinary crates already spend a large share of frontend time
here (e.g. one mid-sized async crate: `evaluate_obligation` is the #1 query at
**~18%** / 282 ms even at `-Copt-level=0`).

### Workarounds

**Zero-runtime-cost (preferred): write the future's lifetime so no outlives
where-bound is needed.** Keep `&self` borrowed; tie the returned future's lifetime
directly to the inputs instead of routing through `#[async_trait]`'s `'async_trait`:

```rust
// only &self borrowed:
fn m(&self) -> Pin<Box<dyn Future<Output = R> + Send + '_>>;

// also borrows args — unify every input ref under one lifetime 'a:
fn m<'a>(&'a self, x: &'a X, y: &'a Y) -> Pin<Box<dyn Future<Output = R> + Send + 'a>>;
```

No `where Self: …` outlives bound ⇒ region-free `ParamEnv` ⇒ the `Send` proof hits
the **global** cache and is derived once. No clone, no `to_owned`, no `unsafe`,
fully sound. This is a drop-in `#[async_trait]` replacement for the common case
where async-trait methods are `.await`ed in the caller's scope. The one limitation:
a `+ 'a` future is **not `'static`**, so it can't be detached via `tokio::spawn`
(borrows `self`); native async fn in traits (RPITIT, 1.75+) produce this shape
automatically and likewise avoid the bug where `dyn` dispatch isn't required.

**When the future must outlive the borrow** (detached `tokio::spawn`, stored
futures), make it `'static` by owning its state — clone the receiver into the
`async move`:

```rust
fn m(&self) -> Pin<Box<dyn Future<Output = R> + Send>> {
    let this = self.clone();   // Self: Clone + 'static — cheap for Arc bundles; &T args become owned
    Box::pin(async move { /* body, `self` -> `this` */ })
}
```

Fast too, but pays a receiver clone (and `to_owned` of any `&T` args) per call.

**Last-resort hack (only if you must keep multiple independent borrows *and* a
`'static` future):** wrap the future in `struct AssertSend<F>(F); unsafe impl<F>
Send for AssertSend<F> {}`, making `Send` O(1) and skipping the structural
recursion regardless of bounds. This **disables the `Send` safety check** for any
non-`Send` value held across an `.await` (which a macro can't re-verify) → UB if
tokio migrates the task. Sound only under external discipline (e.g. clippy
`await_holding_*`); prefer the borrowed or owned forms.

Complementary / orthogonal levers: an **opaque future boundary** (route each
`async fn` through an `#[inline(never)]` helper returning a boxed `dyn Future`, so
`Send` isn't proven transitively through nested state machines — caps proof
*depth*, doesn't touch the region); **type-erasing the shared state**
(`Arc<dyn Trait + Send + Sync>`); **splitting crates** (caps `N` per crate, the
global cache is per-crate). Non-fixes commonly reached for: `tower::ServiceBuilder`
(nested-builder cost, different pathology), `#[axum::debug_handler]` (diagnostics
only), cranelift/linker tweaks (codegen/link time, not trait solving).

Even the cleanest workaround means rewriting *every* async-trait definition and
impl in the codebase — which is why the upstream cache fix is the right resolution.

### Related issues / precedent

- [#87012](https://github.com/rust-lang/rust/issues/87012) — `evaluate_obligation`
  dominating compile time on an async-heavy crate. Likely the same root cause as a
  real-world report, but without this diagnosis.
- [#106930](https://github.com/rust-lang/rust/issues/106930) — Send/Sync proof cost
  in `evaluate_trait_predicate_recursively` (framed as recursion blowup, not
  per-root re-derivation).
- [#132625](https://github.com/rust-lang/rust/pull/132625) — precedent for
  *narrowing* `can_use_global_caches` (the opaque-types branch) to reclaim perf
  while accepting a documented, reviewed tradeoff.

### Root-cause patch sketch (PoC, not a proposed patch)

Use `has_non_region_infer()` in `can_use_global_caches` and
`erase_and_anonymize_regions` on the global cache key. On the affected crate this
took `evaluate_obligation` from **76 s → 0.85 s** (re-derivation 1,191× → 10×) and
compiled correctly. Posted as a mechanism demonstration; general soundness for
region-sensitive *selection* would need review, and the new solver may be the
right long-term home.

### Version

Reproduces identically on the latest stable and nightly (verified 2026-06-06):

```
rustc 1.94.1 (e408947bf 2026-03-25)      # originally measured
rustc 1.96.0 (ac68faa20 2026-05-25)      # stable  — region=on M=300 evaluate_obligation 1.12 s vs off 4.7 ms
rustc 1.98.0-nightly (8954863c8 2026-06-05)  # nightly — region=on M=300 evaluate_obligation 1.13 s vs off 4.7 ms
```

Old/default solver. Under `-Znext-solver=globally` (nightly) the blowup is gone
(`M=150` region=on: 607 ms → 46 ms wall, `evaluate_obligation` no longer a hot
query).
