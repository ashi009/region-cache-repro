<!-- DRAFT for dtolnay/async-trait — for review before filing. -->
<!-- Title: -->
# Desugaring emits `where Self: 'async_trait` even for `&self`-only methods, triggering a ~100× rustc trait-eval blowup; a receiver-tied lifetime avoids it

This is the macro-side angle on #174 (which you closed, correctly, pointing at
[rust-lang/rust#87012](https://github.com/rust-lang/rust/issues/87012)). I've now
pinned the precise rustc trigger, and it turns out the desugaring has a choice that
sidesteps it at **zero runtime cost** — so there may be something actionable here
after all, without changing the output contract.

### The trigger

For `async fn m(&self)`, the macro (v0.1.89) emits a fresh `'async_trait` lifetime
bounded by region **outlives** clauses, *unconditionally* — even when `&self` is the
only borrowed input:

```rust
fn m<'life0, 'async_trait>(&'life0 self)
    -> Pin<Box<dyn Future<Output = R> + Send + 'async_trait>>
where 'life0: 'async_trait, Self: 'async_trait;
```

Those `'life0: 'async_trait` / `Self: 'async_trait` outlives bounds land in the
method's `ParamEnv` as region-bearing clauses. That is *specifically* what makes the
trait solver's evaluation cache
([`can_use_global_caches`](https://github.com/rust-lang/rust/blob/61d7280f3c4c63fa24c56bdaa9a446151b5a30dc/compiler/rustc_trait_selection/src/traits/select/mod.rs#L1508-L1518))
bail to a per-goal local cache, so `Send`/`Sync` of the captured state is re-proven
from scratch in every impl instead of being cached once. (Full root-cause writeup
and a standalone repro: https://github.com/ashi009/region-cache-repro ·
rustc issue: <RUSTC_ISSUE_LINK — filled after filing>.)

Measured with the real macro — one service trait over a deep `Arc<Mutex<…>>` shared
state, 150 impls: **1.36 s** in `evaluate_obligation` as desugared, vs **13.8 ms**
when the same methods return a receiver-tied future. ~100×, from the lifetime form
alone.

### The point #174 didn't have: it's a lowering choice, not inherent

When the receiver is the only reference input, the `'async_trait` indirection is
unnecessary — the future can be bound by the receiver lifetime directly, which is
**semantically identical** (both forms borrow `self`; neither is `'static`) but adds
no outlives clause, leaving the `ParamEnv` region-free:

```rust
// proposed lowering for `&self`-only (and `&self` + owned args) methods:
fn m(&self) -> Pin<Box<dyn Future<Output = R> + Send + '_>>;
//  equivalently: fn m<'life0>(&'life0 self) -> Pin<Box<… + 'life0>>;  (no where-clause)
```

This is a desugaring-internal change: same return type, same boxed `dyn Future +
Send`, same borrowing behavior — just the region-free spelling. It is **not** a new
Future representation (#137) or an `impl Future` surface (#274); the output contract
is unchanged.

### Scope / honesty

- Applies cleanly when the **only reference input is the receiver** (`&self` /
  `&mut self`), with all other params owned — the common service/handler shape.
- Methods that borrow **additional** reference parameters genuinely need to express
  "future lives at most as long as the shortest input," which is what the
  `'async_trait` + outlives scheme encodes; collapsing those to one lifetime would
  change the signature contract, so those should keep the current lowering. So this
  is a fast-path for the common case, not a wholesale change.
- I realize the established position (#174) is that the cost is rustc's to fix, and
  that's still true long-term — but this would help every stable user now, ahead of
  any compiler change, and only for the case where the macro is currently emitting a
  bound it doesn't need.

I'll follow up with a PR implementing this — gated strictly to the receiver-only
case, with the existing test suite as the guard — so the change is concrete to
evaluate rather than hypothetical. Entirely reasonable to close if you'd still
rather keep this a pure rustc concern; the aim is just to surface the precise
trigger and the lowering equivalence, which weren't known at #174.
