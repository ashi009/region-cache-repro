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

### Real-world impact

On a large async-heavy codebase, `evaluate_obligation` is the dominant frontend
cost (~35%, ≈68 s/crate at `-C opt`). Instrumenting the nested
`evaluate_trait_predicate_recursively` showed, for one crate: 60.5M actual
evaluations of only 50,783 distinct predicates (**1,191× re-derivation**), with
**99.3%** of cache inserts going to the local cache, **100%** of those blocked
solely by region-only `ParamEnv` inference. Two sibling crates: 592× and 638×.
The cost is overwhelmingly `Send`/`Sync` of ordinary `Adt` std containers, not
coroutine witnesses.

A proof-of-concept (use `has_non_region_infer()` in `can_use_global_caches` and
`erase_and_anonymize_regions` the global cache key) took that crate's
`evaluate_obligation` from **76 s → 0.85 s** (re-derivation 1,191× → 10×) and
compiled correctly. Posted as a mechanism demonstration; general soundness for
region-sensitive *selection* would need review, and the new solver may be the
right long-term home.

### Version

```
rustc 1.94.1 (e408947bf 2026-03-25)
```
(Old/default solver. Reproduces on current nightly is expected; please confirm.)
