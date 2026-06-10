# Trait solver re-derives `Send` proofs per impl when an outlives where-bound is in scope

One trait, M impls, each wrapping the same `Shared` struct (150 fields of `Arc<Mutex<Vec<…>>>` nested 10 deep); every method's future borrows `&self`. The two files differ only in how the method's lifetimes are spelled:

```rust
// outlives_*.rs — the shape #[async_trait] emits for every method
fn check<'life0, 'at>(&'life0 self, x: &'at [u8])
    -> Pin<Box<dyn Future<Output = u64> + Send + 'at>>
where Self: 'at, 'life0: 'at;

// unified_*.rs — same borrows under one lifetime, no outlives clause
fn check<'a>(&'a self, x: &'a [u8]) -> Pin<Box<dyn Future<Output = u64> + Send + 'a>>;
```

```
$ rustc -V
rustc 1.96.0 (ac68faa20 2026-05-25)
$ time rustc --edition 2021 --crate-type=lib --emit=metadata -o /tmp/r.rmeta outlives_150.rs
real	0m3.326s
$ time rustc --edition 2021 --crate-type=lib --emit=metadata -o /tmp/r.rmeta outlives_300.rs
real	0m6.627s
$ time rustc --edition 2021 --crate-type=lib --emit=metadata -o /tmp/r.rmeta unified_150.rs
real	0m0.134s
$ time rustc --edition 2021 --crate-type=lib --emit=metadata -o /tmp/r.rmeta unified_300.rs
real	0m0.180s
```

```
$ rustc -V
rustc 1.98.0-nightly (8954863c8 2026-06-05)
$ time rustc --edition 2021 --crate-type=lib --emit=metadata -o /tmp/r.rmeta outlives_300.rs
real	0m6.612s
$ time rustc --edition 2021 --crate-type=lib --emit=metadata -o /tmp/r.rmeta unified_300.rs
real	0m0.188s
```

97% of the outlives time is `evaluate_obligation`, re-deriving the same `Shared: Send` proof once per impl. The outlives bound puts a region in `caller_bounds`. The query canonicalizes the `ParamEnv`, so that region comes back as an infer var. `can_use_global_caches` then bails on `param_env.has_infer()`, and the proof never reaches `tcx.evaluation_cache`. `#[async_trait]` emits that bound on every method, so large async codebases pay this per method × impl.

Same diagnosis and fix as #92044 (validated on #87012), closed unmerged over selection soundness (`impl<T: 'static>` bounds participate in selection); it reproduces unchanged today.

Repro: <https://github.com/ashi009/region-cache-repro>. (`-Znext-solver=globally` on nightly also scales per-impl on this shape — slower still, 7.2 s / 14.6 s — cause not investigated.)
