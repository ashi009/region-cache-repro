# Why a large axum/tokio server spends most of its compile time proving `Send`

Many axum handlers (or `#[async_trait]` service methods) sharing one
`Arc<AppState>`? Your **frontend** compile time is likely dominated by trait
solving, growing with the **number of handlers** rather than the code in each. A
`-Zself-profile` shows `evaluate_obligation` on top (often 30–85%).

Cause: the trait solver **re-proves `Send`/`Sync` of your shared state once per
handler** instead of caching it. The trigger is one lifetime in
scope — specifically the `where Self: 'async_trait` bound `#[async_trait]` adds.

## The pattern (idiomatic axum/tokio)

```rust
struct AppState { db: PgPool, cache: Arc<RwLock<HashMap<UserId, Arc<Session>>>>, /* … */ }
type Shared = Arc<AppState>;

async fn get_user(State(s): State<Shared>, /* … */) -> Response { /* … .await … */ }
// … dozens–hundreds of handlers, each capturing `Shared` across `.await`
```

1. **Every handler future must be `Send`.** `tokio::spawn` needs `Future: Send +
   'static`; axum `Handler` needs `type Future: Future + Send`. So for *each*
   handler the compiler recurses the whole `Arc<AppState>` graph
   (`Arc<RwLock<HashMap<…>>>`, `Vec`, `Box`, `RawTable`, …) proving `Send`/`Sync`.

2. **A region outlives bound is in scope.** `#[async_trait]` desugars
   `async fn m(&self)` to a `self`-borrowing future:

   ```rust
   fn m<'life0, 'async_trait>(&'life0 self) -> Pin<Box<dyn Future<…> + Send + 'async_trait>>
   where Self: 'async_trait, 'life0: 'async_trait;   // ← this bound is the trigger
   ```

Result: **N handlers ⇒ N re-derivations of the same shared-state `Send` proof.** On
a real async crate we measured the same sub-proof rebuilt **592×–1191×**, with
`evaluate_obligation` ≈ 35% of frontend (~68 s/crate).

## Root cause, in rustc

[`SelectionContext::can_use_global_caches`](https://github.com/rust-lang/rust/blob/61d7280f3c4c63fa24c56bdaa9a446151b5a30dc/compiler/rustc_trait_selection/src/traits/select/mod.rs#L1508-L1518)
shares a result via the crate-wide `tcx.evaluation_cache` only if the `ParamEnv` is
infer-free:

```rust
if param_env.has_infer() || pred.has_infer() {
    return false;   // -> per-InferCtxt local cache, dies with the query
}
```

How an outlives bound trips this:

- **`ParamEnv` is just its [`caller_bounds`](https://github.com/rust-lang/rust/blob/61d7280f3c4c63fa24c56bdaa9a446151b5a30dc/compiler/rustc_middle/src/ty/mod.rs#L1002-L1009)**
  — the in-scope where-clauses. `has_infer()` walks the regions inside them.
- **`where Self: 'a` / `'life0: 'a` lowers to region-bearing clauses**
  ([`ClauseKind::TypeOutlives` / `RegionOutlives`](https://github.com/rust-lang/rust/blob/61d7280f3c4c63fa24c56bdaa9a446151b5a30dc/compiler/rustc_hir_analysis/src/collect/predicates_of.rs#L303-L322))
  that land in `caller_bounds`. A `&self` borrow or `+ '_` return is a signature
  *type*, not a where-clause — it adds nothing, so that `ParamEnv` is region-free.
- **The `evaluate_obligation` query** [canonicalizes `param_env.and(predicate)`](https://github.com/rust-lang/rust/blob/61d7280f3c4c63fa24c56bdaa9a446151b5a30dc/compiler/rustc_trait_selection/src/traits/query/evaluate_obligation.rs#L114-L116)
  and rebuilds it in a fresh `InferCtxt` where each canonical region becomes a fresh
  [`ReVar`](https://github.com/rust-lang/rust/blob/61d7280f3c4c63fa24c56bdaa9a446151b5a30dc/compiler/rustc_infer/src/infer/canonical/mod.rs#L121-L123)
  (`build_with_canonical` → `instantiate_canonical_var`). So the outlives clauses'
  regions return as infer vars ⇒ `has_infer()` is true ⇒ local cache ⇒ re-derived
  per handler.

(The eval cache key's *predicate* half is already region-erased by the freshener;
only the `ParamEnv` half isn't — that's the asymmetry. Region-sensitive results are
reported separately as `EvaluatedToOkModuloRegions`, so this caching is sound; the
gate is just too coarse for regions. Full analysis + PoC patch in [`ISSUE.md`](ISSUE.md).)

The next-gen trait solver (`-Znext-solver=globally`) canonicalizes the `ParamEnv`
regions into its global cache key and doesn't blow up — but it's nightly-only and not
yet at perf parity, so it's the long-term home, not a fix to ship today.

## Reproduce

```console
$ RUSTC=$(rustup which rustc) ./measure.sh    # region on vs off, scaling in M
$ RUSTC=$(rustup which rustc) ./trigger.sh    # isolate the exact trigger
```

`evaluate_obligation` self time (`K=60`, depth 6, verified 2026-06-06 on stable
1.96.0 & nightly 1.98.0):

| M (handlers) | region on | region off |
|---|---|---|
| 75  | ~280 ms | ~4 ms |
| 150 | ~560 ms | ~4 ms |
| 300 | ~1.1 s  | ~5 ms |

region=on scales linearly; region=off is flat. A lifetime in scope turns an O(1)
cached proof into O(N). `trigger.sh` proves the *same* `Send` goal four ways (each
future borrows `&self`):

```console
  outlives  evaluate_obligation=1.2-1.3s   # #[async_trait] desugaring (outlives bound)
  borrowed  evaluate_obligation=~12-15ms   # future tied to &self via `+ '_`
  unified   evaluate_obligation=~12-14ms   # all borrows under one `'a`, `+ 'a`
  owned     evaluate_obligation= ~5-7ms    # clone self into a 'static future
```

~100×, with the outlives where-bound the only thing that varies. With the real
`async_trait 0.1.88` macro: 1.36 s vs 13.8 ms rewritten to `+ '_`.

## Workaround: keep borrowing, drop the outlives bound

**Zero runtime cost** — tie the future's lifetime to the inputs instead of routing
through `'async_trait`; no clone, no `unsafe`, sound:

```rust
fn m(&self) -> Pin<Box<dyn Future<Output = R> + Send + '_>>;                 // &self only
fn m<'a>(&'a self, x: &'a X) -> Pin<Box<dyn Future<Output = R> + Send + 'a>>; // unify borrows
```

No outlives clause ⇒ region-free `ParamEnv` ⇒ the `Send` proof hits the global cache
and is derived once. Drop-in where methods are `.await`ed in the caller's scope;
native async fn in traits (RPITIT, 1.75+) produce this automatically. Limit: a
`+ 'a` future isn't `'static`, so it can't be detached via `tokio::spawn` — there,
own the state instead (`let this = self.clone()` into `async move`; pays a clone).

Orthogonal levers: opaque future boundary (`#[inline(never)]` helper returning boxed
`dyn Future`); type-erase state (`Arc<dyn Trait + Send + Sync>`); split crates.
Don't help: `tower::ServiceBuilder` (different cost), `#[axum::debug_handler]`
(diagnostics only), cranelift/linker (codegen, not trait solving).

## Files

- `gen.py` / `measure.sh` — region on/off scaling driver
- `gen_variants.py` / `trigger.sh` — isolate the trigger (`outlives` / `borrowed` /
  `unified` / `owned`)
- `minimal.rs` — the shape at a glance, mapped to `#[async_trait]`'s desugaring
- `ISSUE.md` — upstream issue draft (code-grounded root cause, soundness, PoC patch)
