# Trait solver re-derives `Send` proofs per impl when an outlives where-bound is in scope

One trait, M impls, each wrapping the same `Shared` struct (60 fields of `Arc<Mutex<Vec<…>>>` nested 6 deep); every method's future borrows `&self`. The two files differ only in how the method's lifetimes are spelled:

```rust
// outlives_*.rs — the shape #[async_trait] emits for every method
fn check<'life0, 'at>(&'life0 self, x: &'at [u8])
    -> Pin<Box<dyn Future<Output = u64> + Send + 'at>>
where Self: 'at, 'life0: 'at;

// unified_*.rs — same borrows under one lifetime, no outlives clause
fn check<'a>(&'a self, x: &'a [u8]) -> Pin<Box<dyn Future<Output = u64> + Send + 'a>>;
```

```
$ time rustc --edition 2021 --crate-type=lib --emit=metadata -o /tmp/r.rmeta outlives_150.rs
real	0m0.748s
$ time rustc --edition 2021 --crate-type=lib --emit=metadata -o /tmp/r.rmeta outlives_300.rs
real	0m1.464s
$ time rustc --edition 2021 --crate-type=lib --emit=metadata -o /tmp/r.rmeta unified_150.rs
real	0m0.110s
$ time rustc --edition 2021 --crate-type=lib --emit=metadata -o /tmp/r.rmeta unified_300.rs
real	0m0.156s
```

1.38 s of the outlives 1.46 s is `evaluate_obligation`, re-deriving the same `Shared: Send` proof once per impl: the outlives bounds lower to region-bearing clauses in `caller_bounds`, the canonicalized query re-instantiates those regions as infer vars, and `can_use_global_caches` bails on `param_env.has_infer()` — so the proof never reaches `tcx.evaluation_cache`. `#[async_trait]` emits that bound on every method, so large async codebases pay this per method × impl.

Same diagnosis and fix as #92044 (validated on #87012), closed unmerged over selection soundness (`impl<T: 'static>` bounds participate in selection); it reproduces unchanged today.

Repro: <https://github.com/ashi009/region-cache-repro>. Tested on 1.96.0 stable and 1.98.0-nightly. (`-Znext-solver=globally` on nightly also scales per-impl on this shape — slower still, 1.9 s / 3.3 s — cause not investigated.)
