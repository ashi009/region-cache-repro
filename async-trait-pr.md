Follow-up to #297; works around rust-lang/rust#157595.

For a method whose only borrowed input is the receiver and which has no generic parameters, the boxed future is now tied to the receiver's lifetime and no `'async_trait` is emitted:

```rust
// async fn m(&self) -> R  currently:
fn m<'life0, 'async_trait>(&'life0 self) -> Pin<Box<dyn Future<Output = R> + Send + 'async_trait>>
where 'life0: 'async_trait, Self: 'async_trait;
// with this change:
fn m<'life0>(&'life0 self) -> Pin<Box<dyn Future<Output = R> + Send + 'life0>>;
```

The two forms are semantically identical — the future still borrows `self`, neither is `'static` — but the avoided outlives bounds defeat the trait solver's global `Send`/`Sync` cache, re-proving the captured state once per impl. One trait, 300 `&self`-only impls over a deep `Arc<Mutex<Vec<…>>>` state, rustc 1.96.0:

| | `evaluate_obligation` |
|---|---|
| async-trait 0.1.89 | 18.07 s |
| this PR | 77.7 ms |

- `transform_sig` skips the `'async_trait` param and its outlives bounds for eligible signatures and reuses the receiver's `'life0` on the future; the inferred `Send`/`Sync` bound for default methods is kept (`Self: 'life0` is implied by `&'life0 self`)
- eligibility is decided from the signature alone — receiver (`&self` / `&mut self` / `self: &Self`) as the sole reference input, no generic/const params, no `impl Trait` arg, nothing naming `'async_trait` (new `NeedsAsyncTrait` visitor) — because the trait and its impls expand in separate invocations and must agree on whether `'life0` is late-bound
- new `region_free_receiver_lifetime` test pins the eligible forms (`&mut self`, borrowing return, owned args, default body); `tests/ui/lifetime-span.stderr` re-blessed (spans shift on already-invalid code)

**Limitation.** The receiver lifetime becomes late-bound for eligible methods. The one observable break: a trait method written with `where …: 'async_trait` whose impl omits the clause now fails with E0195 (the two sides disagree on early/late-bound). Loud compile error, not UB. GitHub search finds no code with that combination: `where Self: 'async_trait` has zero hits, and the ~7 repos using `where T: 'async_trait` all pair it with shapes the gate already excludes.
