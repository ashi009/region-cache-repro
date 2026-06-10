<!-- Rewrite of dtolnay/async-trait#297. -->
# Desugaring emits `where Self: 'async_trait` even for `&self`-only methods

Follow-up to #174: the slowness is a rustc caching bug (rust-lang/rust#157595), but the macro emits the trigger unconditionally, and the common case doesn't need it.

For `async fn m(&self)` the macro (v0.1.89) emits outlives clauses:

```rust
fn m<'life0, 'async_trait>(&'life0 self)
    -> Pin<Box<dyn Future<Output = R> + Send + 'async_trait>>
where 'life0: 'async_trait, Self: 'async_trait;
```

Those clauses make the trait solver re-prove `Send` of the captured state once per impl instead of caching it. When the receiver is the only reference input, the `'async_trait` indirection isn't needed — tying the future to the receiver is semantically identical (still borrows `self`, still not `'static`) and leaves the `ParamEnv` region-free:

```rust
fn m(&self) -> Pin<Box<dyn Future<Output = R> + Send + '_>>;
```

`outlives_300.rs` is the desugared shape, 300 impls over one shared struct; `unified_300.rs` spells the same borrows with one lifetime and no outlives clause:

```
$ git clone https://github.com/ashi009/region-cache-repro && cd region-cache-repro
$ rustc -V
rustc 1.96.0 (ac68faa20 2026-05-25)
$ time rustc --edition 2021 --crate-type=lib --emit=metadata -o /tmp/r.rmeta outlives_300.rs
real	0m6.627s
$ time rustc --edition 2021 --crate-type=lib --emit=metadata -o /tmp/r.rmeta unified_300.rs
real	0m0.180s
```

Implemented in #298: non-generic methods whose only reference input is the receiver get the receiver-tied form; extra reference args, generic methods, and anything naming `'async_trait` keep the current lowering. One regression worth your call: a trait method with a user-written `where …: 'async_trait` whose impl omits the clause now hits E0195 (trait and impl lower in separate invocations) — found no code in the wild that does this, but it's real.
