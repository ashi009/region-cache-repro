# Trait solver re-derives region-independent auto-trait obligations once per root goal (`can_use_global_caches` bails on region inference vars)

### Summary

In the trait solver, `evaluate_obligation` time scales **linearly with the
number of structurally-overlapping root goals** when the goal is proven under a
`ParamEnv` carrying a **region outlives bound**, and is flat otherwise. Auto-traits
(`Send`/`Sync`) don't depend on regions, so the per-goal re-derivation is pure
waste. The trigger is exactly the `where Self: 'async_trait` bound that
`#[async_trait]` emits, which makes this pervasive in async/axum codebases.

### Mechanism

[`SelectionContext::can_use_global_caches`](https://github.com/rust-lang/rust/blob/61d7280f3c4c63fa24c56bdaa9a446151b5a30dc/compiler/rustc_trait_selection/src/traits/select/mod.rs#L1508-L1518)
routes a trait-evaluation result to the cross-goal `tcx.evaluation_cache` only if
the `ParamEnv` is infer-free:

```rust
// If there are any inference variables in the `ParamEnv`, then we
// always use a cache local to this particular scope.
if param_env.has_infer() || pred.has_infer() {
    return false;   // -> per-InferCtxt local cache, dies with the query
}
```

The reason a *lifetime* trips this — and specifically an **outlives where-bound**,
not a `&self` borrow — is a short chain through the source:

1. **`ParamEnv` is just its `caller_bounds`** — the in-scope where-clauses
   ([`pub struct ParamEnv { caller_bounds: Clauses }`](https://github.com/rust-lang/rust/blob/61d7280f3c4c63fa24c56bdaa9a446151b5a30dc/compiler/rustc_middle/src/ty/mod.rs#L1002-L1009)).
   `has_infer()` folds over those clauses and the regions inside them.

2. **An outlives bound lowers into a region-bearing clause in `caller_bounds`.**
   `where 'life0: 'a` → [`ClauseKind::RegionOutlives`](https://github.com/rust-lang/rust/blob/61d7280f3c4c63fa24c56bdaa9a446151b5a30dc/compiler/rustc_hir_analysis/src/collect/predicates_of.rs#L303-L322),
   `where Self: 'a` → `ClauseKind::TypeOutlives`, each carrying the item's
   early/late-bound regions. A `&self` parameter or a `+ '_` / RPITIT return is a
   *signature type*, not a where-clause — it adds **nothing** to `caller_bounds`, so
   that `ParamEnv` is region-free.

3. **The `evaluate_obligation` query re-instantiates those regions as inference
   vars.** It [canonicalizes `param_env.and(predicate)`](https://github.com/rust-lang/rust/blob/61d7280f3c4c63fa24c56bdaa9a446151b5a30dc/compiler/rustc_trait_selection/src/traits/query/evaluate_obligation.rs#L114-L116)
   and [`build_with_canonical`](https://github.com/rust-lang/rust/blob/61d7280f3c4c63fa24c56bdaa9a446151b5a30dc/compiler/rustc_traits/src/evaluate_obligation.rs#L22-L25)
   rebuilds the goal in a fresh `InferCtxt`, where
   [`instantiate_canonical_var` turns each canonical region into a fresh `ReVar`](https://github.com/rust-lang/rust/blob/61d7280f3c4c63fa24c56bdaa9a446151b5a30dc/compiler/rustc_infer/src/infer/canonical/mod.rs#L121-L123).
   So the regions the outlives clauses put in `caller_bounds` come back as `ReVar`s ⇒
   `param_env.has_infer()` is `true` ⇒ the result goes to the local cache and is
   **re-derived from scratch on every root goal**.

A region-free `ParamEnv` (no outlives clause) has no infer var, so the same
`Arc<T>: Send ⇒ T: Send + T: Sync ⇒ …` sub-proof is shared globally and proven once.

**Confirmation.** One trait `Svc` with `M` impls, each future genuinely borrowing
`&self` (so the `Send` proof recurses the shared graph identically), varying only
how the lifetime is written (`gen_variants.py`/`trigger.sh`, `M=150`, verified on
1.96.0 stable & 1.98.0-nightly):

| variant | lifetime on the method | `evaluate_obligation` | clause in `caller_bounds`? |
|---|---|---|---|
| `outlives` | `#[async_trait]`: `where Self: 'at, 'life0: 'at` | **1.2–1.3 s** | yes (region) → local cache |
| `borrowed` | future tied to `&self` via `+ '_` | **12–15 ms** | none → global cache |
| `unified`  | all borrows under one `'a`, `+ 'a` | **12–14 ms** | none → global cache |
| `owned`    | clone `self` into a `'static` future | **5–7 ms** | none → global cache |

The `&self` borrow, the boxed future, and the `async` block are all innocent — the
sole trigger is the explicit outlives clause. With the real `async_trait 0.1.88`
macro: a service trait over deep shared state at `M=150` spent **1.36 s** in
`evaluate_obligation` vs **13.8 ms** rewritten to `+ '_` — ~98× from the lifetime
alone. `#[async_trait]` emits that bound unconditionally (it only *needs* it to
unify *multiple* distinct input lifetimes), so even a `&self`-only method pays —
largely a macro artifact, not a language necessity.

### Why the gate is sound in general but too coarse for regions

The `has_infer()` bail is the conservative fix for historical unsoundness (#22019,
#18290): a result computed while a *type/const* var is unconstrained can become
wrong once it's resolved, and `tcx.evaluation_cache` outlives the `InferCtxt`. For
**region** vars it's unnecessary, and the codebase already treats these goals as
region-independent:

- The eval cache key's **predicate** half is region-erased by the freshener
  ([`fold_region → 'erased`](https://github.com/rust-lang/rust/blob/61d7280f3c4c63fa24c56bdaa9a446151b5a30dc/compiler/rustc_infer/src/infer/freshen.rs#L101-L114):
  [*"in general, we do not take region relationships into account when making
  type-overloaded decisions"*](https://github.com/rust-lang/rust/blob/61d7280f3c4c63fa24c56bdaa9a446151b5a30dc/compiler/rustc_infer/src/infer/freshen.rs#L26-L32)).
  Only the **`ParamEnv`** half is not — that's the asymmetry: the surviving region
  vars live there.
- Region-sensitivity is encoded in the *result* (`EvaluatedToOkModuloRegions`), not
  by cache exclusion. The framework already knows how to say "region-independent."
- The tools to narrow the gate exist and are used in the same file:
  `has_non_region_infer()` (e.g. the `TypeOutlives` fast path) and
  `erase_and_anonymize_regions`.

Caveat: full soundness for region-*sensitive selection* (vs evaluation) would need
review — the freshener deliberately keeps bound regions / the leak check, and
`EvaluatedToOkModuloRegions` exists precisely because some answers aren't
region-independent.

### Repro & measurements

`gen.py K D M region` emits `K` deeply-nested `Arc<Mutex<Vec<…>>>` fields in one
struct, `M` wrappers each containing it, and `M` `Send` assertions — with
(`<'a, U: 'a>`) or without a region outlives bound. `evaluate_obligation` self
time, `K=60`, depth `6`, verified 2026-06-06:

| M | 1.96.0 on | off | 1.98.0-nightly on | off |
|---|---|---|---|---|
| 75  | 279 ms | 4.2 ms | 286 ms | 4.3 ms |
| 150 | 559 ms | 4.4 ms | 568 ms | 4.4 ms |
| 300 | 1.12 s | 4.7 ms | 1.13 s | 4.7 ms |

region=on grows linearly with the root count; region=off is flat. Under
`-Znext-solver=globally` (nightly) the blowup is gone (`M=150`: 607 ms → 46 ms wall,
`evaluate_obligation` no longer a hot query) — the next-gen trait solver
canonicalizes the whole goal including the `ParamEnv`'s regions into its global cache
key, so it's the long-term home. But it's nightly-only (only `-Znext-solver=coherence`
is stable, since 1.84) and not yet at perf parity ([project-goals#113](https://github.com/rust-lang/rust-project-goals/issues/113),
tracking [#114862](https://github.com/rust-lang/rust/issues/114862)), so a fix in the
current solver is still worth doing.

### Real-world impact (axum/tokio shape)

The repro is the controlled form of an everyday async server: one `Arc<AppState>`
of nested `Arc<Mutex/RwLock<…>>` + pools, captured by `N` handler/service futures
that each must prove `Send` (`tokio::spawn` needs `Future: Send + 'static`; axum
`Handler` requires a `Send` future), each under a region from `#[async_trait]` /
`&self`. So *N handlers re-prove `Send`/`Sync` of structurally-overlapping shared
state*. On a large async-heavy codebase: `evaluate_obligation` ≈ 35% of frontend
(~68 s/crate at `-C opt`); one crate showed 60.5M evals of 50,783 distinct
predicates (**1,191×** re-derivation), 99.3% of inserts to the local cache, 100% of
those blocked by region-only `ParamEnv` inference; siblings 592× and 638×.
Overwhelmingly `Send`/`Sync` of ordinary std containers, not coroutine witnesses.
Even at `-Copt-level=0`, a mid-sized async crate has `evaluate_obligation` as the #1
query (~18% / 282 ms). The symptom is widely reported (e.g.
[#87012](https://github.com/rust-lang/rust/issues/87012), `evaluate_obligation`
~85%; axum [#200](https://github.com/tokio-rs/axum/issues/200)) but attributed to
monomorphization / nested builders, not region-driven cache bypass.

### Workarounds

**Zero-cost (preferred):** keep `&self` borrowed but tie the future's lifetime to
the inputs so no outlives clause is emitted — no clone, no `unsafe`, sound:

```rust
fn m(&self) -> Pin<Box<dyn Future<Output = R> + Send + '_>>;                 // &self only
fn m<'a>(&'a self, x: &'a X) -> Pin<Box<dyn Future<Output = R> + Send + 'a>>; // unify borrows
```

Drop-in for the common case where methods are `.await`ed in the caller's scope;
native async fn in traits (RPITIT, 1.75+) produce this shape automatically. Limit: a
`+ 'a` future isn't `'static`, so it can't be detached via `tokio::spawn`.

**When the future must be `'static`** (detached spawn, stored futures): own the
state — `let this = self.clone();` into an `async move` (`Self: Clone + 'static`;
pays a receiver clone + `to_owned` of `&T` args per call).

**Last resort** (multiple independent borrows *and* `'static`): wrap in
`struct AssertSend<F>(F); unsafe impl<F> Send for AssertSend<F> {}` — O(1) `Send`,
but disables the safety check (UB if a non-`Send` value is held across `.await`);
sound only under external discipline.

Orthogonal: opaque future boundary (`#[inline(never)]` helper returning boxed `dyn
Future`, caps proof *depth*), type-erasing state (`Arc<dyn Trait + Send + Sync>`),
splitting crates (caps `N` per crate). Non-fixes: `tower::ServiceBuilder` (different
pathology), `#[axum::debug_handler]` (diagnostics only), cranelift/linker (codegen,
not trait solving). Note even the clean fix means rewriting *every* async-trait def
+ impl — which is why the upstream cache fix is the right resolution.

### Related issues & PoC

- [#87012](https://github.com/rust-lang/rust/issues/87012) — `evaluate_obligation`
  dominating an async crate; likely the same root cause, without the diagnosis.
- [#106930](https://github.com/rust-lang/rust/issues/106930) — Send/Sync proof cost
  in `evaluate_trait_predicate_recursively` (framed as recursion, not per-root).
- [#132625](https://github.com/rust-lang/rust/pull/132625) — precedent for
  *narrowing* `can_use_global_caches` (the opaque-types branch) for perf.

PoC (mechanism demo, not a proposed patch): use `has_non_region_infer()` in
`can_use_global_caches` + `erase_and_anonymize_regions` on the global cache key →
on the affected crate, `evaluate_obligation` **76 s → 0.85 s** (re-derivation
1,191× → 10×), compiled correctly.

### Version

```
rustc 1.96.0 (ac68faa20 2026-05-25)          # stable
rustc 1.98.0-nightly (8954863c8 2026-06-05)  # nightly
```
Reproduces identically on both (default trait solver).
