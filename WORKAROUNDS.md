# Workarounds, measured

For rust-lang/rust#157595 — what to do in a real codebase until rustc caches these proofs, and what each option costs. All variants measured through one identical command (`cargo rustc --release -- -Zself-profile`, rustc 1.92-nightly, 120 impls, non-trivial future bodies — 12 `.await`s each).

## Keep borrowing, drop the outlives bound — zero runtime cost

Tie the future's lifetime to the inputs instead of routing through `'async_trait`; no clone, no `unsafe`, sound:

```rust
fn m(&self) -> Pin<Box<dyn Future<Output = R> + Send + '_>>;                  // &self only
fn m<'a>(&'a self, x: &'a X) -> Pin<Box<dyn Future<Output = R> + Send + 'a>>; // unify borrows
```

No outlives clause ⇒ region-free `ParamEnv` ⇒ the `Send` proof hits the global cache and is derived once. Drop-in where methods are `.await`ed in the caller's scope. Limit: a `+ 'a` future isn't `'static`, so it can't be detached via `tokio::spawn` — there, own the state instead (`let this = self.clone()` into `async move`; pays a clone).

## RPITIT-based alternatives — they move the cost, they don't remove it

Native `async fn`/`-> impl Future` in traits (RPITIT, 1.75+) capture input lifetimes in the opaque return type without an outlives where-bound, so they avoid the `evaluate_obligation` blowup — including shapes the boxed form can't rescue (extra reference args, generic methods).

But that frontend win does not make total compile time faster — it makes it slightly worse. `#[async_trait]` *erases* every future into `Pin<Box<dyn Future>>`, so the state machine isn't monomorphized at call sites. RPITIT keeps the future concrete, so LLVM optimizes each (large) state machine inline. The trait-solving saving is small; the extra codegen is larger:

| approach | total cpu | `evaluate_obligation` | LLVM | dyn? | Send? |
|---|---|---|---|---|---|
| `#[async_trait]` | **4.90 s** | 17.9 ms | 1.25 s | ✅ | ✅ |
| boxed `+ '_` (above) | **4.68 s** | 7.1 ms | 1.09 s | ✅ | ✅ |
| native RPITIT `-> impl Future + Send` | 5.26 s (+7%) | 8.0 ms | 1.46 s (+17%) | ❌ | ✅ |
| [`dynosaur`](https://crates.io/crates/dynosaur) + `Send` RPITIT | 5.11 s (+4%) | 8.7 ms | 1.39 s (+11%) | ✅ | ✅ |
| [`trait_variant`](https://crates.io/crates/trait-variant) | — | — | — | ❌ (E0038) | ✅ |

The split that matters: the boxed `+ '_` form keeps the boxing, so codegen is unchanged and the frontend saving is pure. RPITIT and `dynosaur` change the dispatch (erased → monomorphized), trading a small frontend saving for a bigger codegen cost — worse with non-trivial futures, and the gap widens with generic callers. Reach for them when you want native syntax or to drop the macro, not to speed up builds. (Two correctness notes: `trait_variant` is not dyn-compatible — E0038; `dynosaur`'s default `dyn(box)` mode yields **non-`Send`** futures, so you must write `-> impl Future + Send` for `tokio::spawn`.)

Caveat: one machine, one rustc, moderate future bodies — these numbers establish the direction (frontend saving < codegen cost for the monomorphizing forms), not a universal magnitude. On a crate where `evaluate_obligation` genuinely dominates, the balance can tilt the other way — measure your own.

## Orthogonal levers

Opaque future boundary (`#[inline(never)]` helper returning boxed `dyn Future`); type-erase state (`Arc<dyn Trait + Send + Sync>`); split crates. Don't help: `tower::ServiceBuilder` (different cost), `#[axum::debug_handler]` (diagnostics only), cranelift/linker (codegen, not trait solving).
