Follow-up to #297; works around rust-lang/rust#157595.

`async fn m(&self)` now expands to a future tied to the receiver's lifetime when the receiver is the only borrowed input and the method has no generic parameters, instead of the synthetic `'async_trait` lifetime with its outlives bounds. Those bounds defeat the trait solver's global `Send`/`Sync` cache, so the captured state's proofs are re-derived once per impl. The receiver-tied form is semantically identical — the future still borrows `self`, neither form is `'static` — and the proofs are derived once.

```rust
// async fn m(&self) -> R  currently:
fn m<'life0, 'async_trait>(&'life0 self) -> Pin<Box<dyn Future<Output = R> + Send + 'async_trait>>
where 'life0: 'async_trait, Self: 'async_trait;
// with this change:
fn m<'life0>(&'life0 self) -> Pin<Box<dyn Future<Output = R> + Send + 'life0>>;
```

One trait, 150 impls over a deep `Arc<Mutex<Vec<…>>>` state, rustc 1.96.0:

| | `evaluate_obligation` |
|---|---|
| current lowering | 1.19 s |
| this PR | 15.75 ms |

Same boxed `dyn Future + Send` either way — no runtime or output-contract change, only the lifetime spelling.

- eligible: `&self` / `&mut self` / `self: &Self` as the sole reference input, no generic/const params; owned args, borrowing returns, and default bodies remain eligible — pinned by a new `region_free_receiver_lifetime` test
- everything else keeps the current lowering: extra reference args, generic methods (the future captures `T`, needing `T: 'life0` either way), `impl Trait` args, anything naming `'async_trait`
- `tests/ui/lifetime-span.stderr` re-blessed: spans shift on already-invalid code

**Limitation.** The receiver lifetime becomes late-bound for eligible methods. The one observable break: a trait method written with `where …: 'async_trait` whose impl omits the clause now fails with E0195 — trait and impl expand in separate invocations and disagree on early/late-bound. Loud compile error, not UB. GitHub search finds no code with that combination: `where Self: 'async_trait` has zero hits, and the ~7 repos using `where T: 'async_trait` all pair it with a shape the fast path already excludes.
