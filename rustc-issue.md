# `evaluate_obligation` re-derives region-independent `Send`/`Sync` once per impl when a `where Self: 'a` bound is in scope

Proving `Send` of the same shared state in M impls takes time linear in M when the method carries a region outlives bound, and stays flat without it. Same goal, varying only the lifetime spelling ([repro](https://github.com/ashi009/region-cache-repro), `Arc<Mutex<Vec<…>>>` state, M=150, 1.96 stable / 1.98 nightly):

| method signature | `evaluate_obligation` |
|---|---|
| `where Self: 'at, 'life0: 'at` (the `#[async_trait]` desugaring) | **1.2–1.3 s** |
| future tied to `&self` via `+ '_`, no outlives bound | **12–15 ms** |

Real `async_trait 0.1.88`: 1.36 s → 13.8 ms rewriting the trait to `+ '_`.

The cause is that `can_use_global_caches` bails when `param_env.has_infer()`, and a `where Self: 'a` bound is a region-bearing clause in `caller_bounds` that the `evaluate_obligation` query canonicalizes and `build_with_canonical` re-instantiates as a `ReVar` — so `has_infer()` is true, the result goes to the per-`InferCtxt` local cache, and the region-independent proof is re-derived per impl instead of shared. A `&self` borrow or `+ '_`/RPITIT return is a signature type, not a clause, so it doesn't trigger this; only the explicit outlives bound does. `#[async_trait]` emits it unconditionally (it only needs it to unify multiple input lifetimes), so every `&self` async-trait method pays it.

This is [#92044](https://github.com/rust-lang/rust/pull/92044) (@Aaron1011, 2021), confirmed to fix #87012 (280 ms → 9.87 ms) but closed unmerged over two soundness problems that still hold: a region bound can gate selection (`impl<T: 'static>`), and instantiating distinct early-bound regions as inference vars lets them equate without surfacing it. [Aaron's proposed fix](https://github.com/rust-lang/rust/pull/92044#issuecomment-1004471710) — anonymize regions positionally instead of dropping them — is now `tcx.erase_and_anonymize_regions`, which didn't exist then, so this may be more tractable than in 2022. The next-gen solver canonicalizes the whole goal including `ParamEnv` regions and doesn't have the bug, but the old solver is what everyone's on.

I have a PoC (`has_non_region_infer()` + `erase_and_anonymize_regions` on the key, 76 s → 0.85 s on an internal crate) but it shares #92044's selection-soundness gap, so it measures the ceiling, not a fix.
