# Old trait solver re-derives region-independent auto-trait obligations once per root goal

`evaluate_obligation` compile time scales **linearly with the number of
structurally-overlapping root goals** when the goal is proven under a ParamEnv
that carries a **region**, but is **flat** otherwise. The only difference
between the slow and fast programs is a lifetime in the ParamEnv
(`fn check<'a, U: 'a>(_: U)` vs `fn check()`).

`Send`/`Sync` (and other auto-traits) do **not** depend on regions, so this is
pure redundant re-derivation.

## Reproduce (stock rustc)

```console
$ RUSTC=$(rustup which rustc) ./measure.sh
== A/B at M=150 (K=60 fields, depth=6) ==
  region in ParamEnv     wall=  492ms   evaluate_obligation=455.53ms
  no region              wall=   38ms   evaluate_obligation=3.89ms
== scaling, region=on  (grows ~linearly with root count) ==
  M=75                   wall=  260ms
  M=150                  wall=  492ms
  M=300                  wall=  946ms
== scaling, region=off (stays flat) ==
  M=75                   wall=   35ms
  M=150                  wall=   38ms
  M=300                  wall=   45ms
== (optional) next-gen solver removes the gap ==
  region=on -Znext-solver  wall= ...    (no evaluate_obligation blowup)
```

`gen.py K D M region` generates the program: `K` distinct deeply-nested
(`Arc<Mutex<Vec<…>>>`, depth `D`) fields in one shared struct, `M` wrapper types
that each contain it, and `M` functions that assert `Send` of a wrapper —
`region=on` puts a free region in the ParamEnv, `region=off` does not.
See `minimal.rs` for the shape at a glance.

## Root cause

`SelectionContext::can_use_global_caches`
(`compiler/rustc_trait_selection/src/traits/select/mod.rs`) decides whether a
trait-evaluation result may go into the shared, tcx-level
`evaluation_cache` (reused across inference contexts) or only into the
per-`InferCtxt` **local** cache:

```rust
// If there are any inference variables in the `ParamEnv`, then we
// always use a cache local to this particular scope.
if param_env.has_infer() || pred.has_infer() {
    return false;
}
```

`has_infer()` is true for **region** inference variables. The
`evaluate_obligation` query canonicalizes `param_env.and(predicate)` and
`build_with_canonical` re-instantiates the ParamEnv's regions as fresh region
inference vars. So **any** obligation proven under a ParamEnv that contains a
region (extremely common: `&'a self` methods, lifetime-generic fns/impls,
outlives bounds) is forced into the local cache and **cannot be shared across
the fresh `InferCtxt` that each canonical query builds**. Structurally-shared
sub-proofs (e.g. `Arc<T>: Send ⇒ T: Send + T: Sync ⇒ …`, deep container graphs)
are therefore re-derived once per root goal.

Because the result is the same for every region instantiation (auto-traits are
region-independent; region-sensitive results are already reported as
`EvaluatedToOkModuloRegions`), this re-derivation is wasted work.

The next-gen solver canonicalizes regions in its global cache and does not
exhibit this on the repro.

## Real-world impact

Measured on a large async-heavy codebase (custom rustc instrumenting the nested
`evaluate_trait_predicate_recursively`), per crate, `-C opt`, rustc 1.94.1:

| crate | root `evaluate_obligation` goals | nested evals (work) | distinct preds | re-derivation | results to LOCAL cache |
|---|---|---|---|---|---|
| A | 53,296 | 60.5M | 50,783 | **1,191×** | 99.3% (100% region-only-infer) |
| B | 130,794 | 62.4M | 62,718 | **592×** | 98.6% |
| C | 113,854 | 58.9M | 58,575 | **638×** | 98.8% |

`evaluate_obligation` is ~35% of frontend time (≈68s/crate) and dominated by
`Send`/`Sync` of ordinary `Adt` std containers (`Arc`/`Mutex`/`HashMap`/
`RawTable`/`RawVec`/`Vec`/`Box`/…), not coroutine witnesses.

A proof-of-concept patch (allow global caching when the only ParamEnv inference
is regions — `has_non_region_infer()` — and `erase_and_anonymize_regions` in the
global cache key) collapsed one crate's `evaluate_obligation` from **76s → 0.85s**
and re-derivation from 1,191× → 10×, compiling correctly. (General soundness for
region-sensitive *selection* needs review; this PoC is a mechanism demonstration,
not a proposed patch.)

## Files

- `gen.py` — repro generator
- `measure.sh` — A/B + scaling driver (stock rustc + `-Zself-profile`)
- `minimal.rs` — the shape, at a glance
- `ISSUE.md` — upstream issue draft
