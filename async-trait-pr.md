Follow-up to #297.

For a method whose only borrowed input is `&self`/`&mut self` and which has no generic parameters, this ties the boxed future's lifetime to the receiver instead of introducing `'async_trait`:

```rust
// async fn m(&self) -> R  currently:
fn m<'life0, 'async_trait>(&'life0 self) -> Pin<Box<dyn Future<Output = R> + Send + 'async_trait>>
where 'life0: 'async_trait, Self: 'async_trait;
// with this change:
fn m<'life0>(&'life0 self) -> Pin<Box<dyn Future<Output = R> + Send + 'life0>>;
```

The `'async_trait` outlives bounds are region-bearing clauses in the method's `ParamEnv`, which forces every `Send` proof over the captured state into the trait solver's per-query local cache and re-derives it once per impl (rust-lang/rust#157595). The receiver-tied form is semantically identical here — the future still borrows `self`, neither is `'static` — but keeps the `ParamEnv` region-free, so the proof is cached. 150 impls over a deep `Arc<Mutex<Vec<…>>>` state, rustc 1.96: `evaluate_obligation` 1.19 s → 15.75 ms.

Everything else falls back to the current lowering: extra reference args, generic methods (the boxed future captures `T`, needing `T: 'life0` either way), `impl Trait` args, and any signature that names `'async_trait`.

Known regression: a method that uses `where …: 'async_trait` and is overridden by an impl omitting the clause now fails with `E0195`, because `#[async_trait]` processes the trait and the impl separately and a region-free signature is late-bound while the general lowering is early-bound. Loud compile error, not UB. I searched GitHub before proposing this: zero hits for `where Self: 'async_trait`, and the ~7 repos using `where T: 'async_trait` all pair it with a shape the fast path already excludes, so I couldn't find code that actually breaks — but it's a real regression, so flagging it.

Happy to narrow or drop it. Tests: new `region_free_receiver_lifetime` covering the eligible forms; `lifetime-span.stderr` re-blessed (spans shift on already-invalid code); rustfmt/clippy clean.
