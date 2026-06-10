<!-- Rewrite of dtolnay/async-trait#297. -->
# The `'async_trait` outlives bounds trigger a rustc caching bug, re-proving `Send` once per impl

Follow-up to #174. For `async fn m(&self)` the macro (v0.1.89) emits outlives clauses:

```rust
fn m<'life0, 'async_trait>(&'life0 self)
    -> Pin<Box<dyn Future<Output = R> + Send + 'async_trait>>
where 'life0: 'async_trait, Self: 'async_trait;
```

Those clauses are exactly the trigger for rust-lang/rust#157595 — a region outlives bound in scope makes the trait solver re-prove `Send`/`Sync` of the captured state once per impl instead of caching it; repro and numbers there. When the receiver is the only reference input the bound isn't needed: #298 emits a receiver-tied future (`… + '_`) for that case, which sidesteps the bug at no change to the output contract.
