# Trait solver re-derives `Send` proofs per impl when an outlives where-bound is in scope

Repro for rust-lang/rust#157595. One trait, M impls, each wrapping the same `Shared` struct (150 fields of `Arc<Mutex<Vec<…>>>` nested 10 deep); every method's future borrows `&self`. `outlives_*.rs` and `unified_*.rs` differ only in how the method's lifetimes are spelled:

```rust
// outlives_*.rs — the shape #[async_trait] emits for every method
fn check<'life0, 'at>(&'life0 self, x: &'at [u8])
    -> Pin<Box<dyn Future<Output = u64> + Send + 'at>>
where Self: 'at, 'life0: 'at;

// unified_*.rs — same borrows under one lifetime, no outlives clause
fn check<'a>(&'a self, x: &'a [u8]) -> Pin<Box<dyn Future<Output = u64> + Send + 'a>>;
```

```
$ git clone https://github.com/ashi009/region-cache-repro && cd region-cache-repro
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

97% of the outlives time is `evaluate_obligation`, re-deriving the same `Shared: Send` proof once per impl. Why it can't cache: the outlives bound puts a region in `caller_bounds`. The `evaluate_obligation` query canonicalizes the `ParamEnv`, so that region comes back as an infer var. [`can_use_global_caches`](https://github.com/rust-lang/rust/blob/61d7280f3c4c63fa24c56bdaa9a446151b5a30dc/compiler/rustc_trait_selection/src/traits/select/mod.rs#L1508-L1518) bails on `param_env.has_infer()`. The proof lands in the per-query local cache instead of `tcx.evaluation_cache`, and every impl re-derives it. This is why frontend time scales with handler/impl count in `#[async_trait]`-heavy codebases. Previously diagnosed and fixed in rust-lang/rust#92044; closed unmerged over selection soundness.

## Files

- `generate.py <variant> [K D M]` — emits the repro; variants `outlives` / `unified` / `borrowed` (`&self`-only, `+ '_`) / `owned` (clone into `'static`). Only `outlives` is slow.
- `measure.sh` — times every variant at M=150/300 and shows the `evaluate_obligation` self-time.
- `WORKAROUNDS.md` — measured tradeoffs for real codebases (boxed `+ '_`, RPITIT, `dynosaur`, owned).
- `rustc-issue.md` / `async-trait-issue.md` / `async-trait-pr.md` — the upstream reports (rust-lang/rust#157595, dtolnay/async-trait#297, dtolnay/async-trait#298).
