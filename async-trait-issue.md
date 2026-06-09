<!-- DRAFT for dtolnay/async-trait — not filed. -->
# `&self`-only methods get a `where Self: 'async_trait` bound they don't need, and it's ~100× of rustc trait-solving time

Follow-up to #174, which you closed pointing at rust-lang/rust#87012. That's still the right call — it is a rustc bug — but I've pinned the trigger, and the macro emits it unnecessarily for the common case.

For `async fn m(&self)` the desugaring (v0.1.89) is `fn m<'life0, 'async_trait>(&'life0 self) -> Pin<Box<… + 'async_trait>> where 'life0: 'async_trait, Self: 'async_trait`. Those outlives bounds are region-bearing clauses in the method's `ParamEnv`, which forces the solver to re-prove `Send`/`Sync` of the captured state once per impl instead of caching it (rust-lang/rust#157595). When `&self` is the only borrowed input the `'async_trait` indirection isn't needed — binding the future to the receiver directly is semantically identical (still borrows `self`, still not `'static`) but leaves the `ParamEnv` region-free:

```rust
fn m(&self) -> Pin<Box<dyn Future<Output = R> + Send + '_>>;
```

Measured: one trait, 150 impls over a deep `Arc<Mutex<…>>` state — `evaluate_obligation` 1.36 s as desugared vs 13.8 ms receiver-tied.

Same return type, same boxing, same borrowing — just the region-free spelling, so it's not a new Future type (#137) or an `impl Future` surface (#274). It only applies when the receiver is the sole reference input and the method is non-generic; extra ref args, generic methods (the boxed future captures `T`, needing `T: 'lt` either way), and anything naming `'async_trait` keep the current lowering.

Implemented in #298. One regression there worth your call: a method using `where …: 'async_trait` overridden by an impl omitting it hits `E0195` (trait and impl are lowered in separate invocations) — couldn't find code in the wild that does this, but it's real.
