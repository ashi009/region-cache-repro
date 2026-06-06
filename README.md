# Why a large axum/tokio server spends most of its compile time proving `Send`

If you have an async server — many axum handlers (or `#[async_trait]` service
methods) sharing one `Arc<AppState>` — you may notice the **frontend** compile
time is dominated by trait solving, and that it grows with the **number of
handlers**, not with how much code each one contains. A `-Zself-profile` will
show `evaluate_obligation` at the top, often 30–85% of frontend time.

This repo is a minimal, self-contained reproduction of the root cause: the old
(default) trait solver **re-proves `Send`/`Sync` of your shared state once per
handler**, instead of proving it once and reusing the result. The trigger is a
single lifetime in scope — the one `#[async_trait]` (and `&self` async methods)
put there.

## The pattern this hits (idiomatic axum/tokio)

Nothing exotic — this is the standard shape:

```rust
// One shared state, deeply nested, shared by every handler.
struct AppState {
    db: PgPool,
    cache: Arc<RwLock<HashMap<UserId, Arc<Session>>>>,
    queues: Arc<Mutex<Vec<Arc<Job>>>>,
    // ...
}
type Shared = Arc<AppState>;

async fn get_user(State(s): State<Shared>, /* ... */) -> Response { /* ... .await ... */ }
async fn list_orders(State(s): State<Shared>, /* ... */) -> Response { /* ... .await ... */ }
// ... dozens or hundreds more handlers, each capturing `Shared` across `.await`
```

Two facts make this expensive:

1. **Every handler future must be `Send`.** `tokio::spawn` requires
   `Future: Send + 'static`, and axum's `Handler` requires `type Future: Future +
   Send + 'static`. So for *each* handler the compiler proves that everything held
   across an `.await` — i.e. your whole `Arc<AppState>` graph — is `Send` (and
   recursively `Sync`). That proof recurses through `Arc<RwLock<HashMap<…, Arc<…>>>>`,
   `Vec`, `Box`, `RawTable`, … — the same nested graph for every handler.

2. **A lifetime is in scope during that proof.** Borrowed extractors, `&self`
   methods, and especially `#[async_trait]` put a region into the `ParamEnv`.
   `#[async_trait]` desugars `async fn m(&self) -> R` to a future that *borrows*
   `self`:

   ```rust
   fn m<'life0, 'async_trait>(&'life0 self)
       -> Pin<Box<dyn Future<Output = R> + Send + 'async_trait>>
   where Self: 'async_trait, 'life0: 'async_trait;
   ```

   Those `Self: 'async_trait` / `'life0: 'async_trait` outlives bounds are the
   region. `Send`/`Sync` don't depend on regions at all — but their presence is
   what triggers the bug.

The result: **N handlers ⇒ N full re-derivations of the same shared-state `Send`
proof.** On a real async-heavy crate we measured the same sub-proof re-derived
**592×–1191×**, with `evaluate_obligation` ≈ 35% of frontend time (~68 s/crate).

## Root cause (one line of rustc)

`SelectionContext::can_use_global_caches`
(`compiler/rustc_trait_selection/src/traits/select/mod.rs`) decides whether a
trait-evaluation result may go into the shared, `tcx`-level `evaluation_cache`
(reused across the whole crate) or only into a per-`InferCtxt` **local** cache
that dies at the end of one query:

```rust
// If there are any inference variables in the `ParamEnv`, then we
// always use a cache local to this particular scope.
if param_env.has_infer() || pred.has_infer() {
    return false;   // -> local cache, not shared
}
```

`has_infer()` is `true` for **region** inference variables. The
`evaluate_obligation` query canonicalizes the `ParamEnv` and re-instantiates its
free regions as region inference vars, so any obligation proven under a
lifetime-bearing `ParamEnv` (every `#[async_trait]` method, every `&self` async
method) is forced into the **local** cache and **cannot be reused** by the next
handler's query. The shared-state `Send` proof is rebuilt from scratch every
time.

This is sound but far too coarse for regions: the compiler already treats this
class of result as region-independent — the freshener that builds the cache key
erases all free regions to `'erased`, and region-sensitivity is reported in the
result itself (`EvaluatedToOkModuloRegions`), not by skipping the cache. The
next-gen solver (`-Znext-solver=globally`) canonicalizes the `ParamEnv` regions
into its global cache key and does **not** exhibit this — but it is nightly-only
and not yet at performance parity, so it's the long-term home, not a fix you can
ship today. (Full detail and a PoC compiler patch — 76 s → 0.85 s on the affected
crate — are in [`ISSUE.md`](ISSUE.md).)

## Reproduce (stock rustc)

`gen.py` emits the controlled version of the pattern above: one wide+deep shared
struct (`K` distinct nested `Arc<Mutex<Vec<…>>>` fields), `M` wrapper types each
containing it (≈ `M` handler futures over one `AppState`), and `M` functions each
asserting `Send` of a wrapper. `region=on` puts a lifetime in the `ParamEnv`
(the `#[async_trait]` `Self: 'async_trait` stand-in); `region=off` doesn't. The
**only** difference between the slow and fast programs is that lifetime.

```console
$ RUSTC=$(rustup which rustc) ./measure.sh
== A/B at M=150 (K=60 fields, depth=6) ==
  region in ParamEnv     wall=  492ms   evaluate_obligation=455.53ms   # ← lifetime in scope
  no region              wall=   38ms   evaluate_obligation=  3.89ms   # ← no lifetime
== scaling, region=on  (grows ~linearly with handler count) ==
  M=75                   wall=  260ms
  M=150                  wall=  492ms
  M=300                  wall=  946ms
== scaling, region=off (stays flat) ==
  M=75                   wall=   35ms
  M=150                  wall=   38ms
  M=300                  wall=   45ms
== (optional) next-gen solver removes the gap ==
  region=on -Znext-solver  wall= ...    (no evaluate_obligation blowup)
```

`region=on` scales linearly with the number of handlers; `region=off` is flat.
That is the whole bug: a lifetime in scope turns an O(1) cached proof into O(N).

### Verified on current toolchains (Apple M-series, 2026-06-06)

`evaluate_obligation` self time, `K=60`, depth `6`:

| M (handlers) | 1.96.0 stable on | stable off | 1.98.0-nightly on | nightly off |
|---|---|---|---|---|
| 75  | 279 ms | 4.2 ms | 286 ms | 4.3 ms |
| 150 | 559 ms | 4.4 ms | 568 ms | 4.4 ms |
| 300 | 1.12 s | 4.7 ms | 1.13 s | 4.7 ms |

Reproduces identically on the latest stable and nightly. With
`-Znext-solver=globally` (nightly), the `M=150` region=on case drops from **607 ms
→ 46 ms** wall and `evaluate_obligation` disappears from the profile entirely (it
is no longer a hot query) — confirming the old-solver cache is the cause and the
new solver is the fix, once it stabilizes.

## What exactly triggers it (and the zero-cost fix)

`trigger.sh` isolates it. The same `Send` proof, written four ways (M=150; each
future genuinely borrows `&self`):

```console
$ RUSTC=$(rustup which rustc) ./trigger.sh
  outlives  wall= 1286ms   evaluate_obligation=1.19s      # #[async_trait] desugaring
  borrowed  wall=   85ms   evaluate_obligation=11.6ms     # future tied to &self via `+ '_`
  unified   wall=   87ms   evaluate_obligation=11.6ms     # all borrows under one `'a`, `+ 'a`
  owned     wall=  154ms   evaluate_obligation=5.4ms      # clone self into a 'static future
```

The trigger is **not** the `&self` borrow — it's the region **outlives
where-bound** `#[async_trait]` puts in scope:

```rust
fn m<'life0, 'async_trait>(&'life0 self) -> Pin<Box<dyn Future<…> + Send + 'async_trait>>
where Self: 'async_trait, 'life0: 'async_trait;   // ← THIS is what poisons the cache
```

`#[async_trait]` emits that bound unconditionally (it needs one lower-bound
lifetime to unify *multiple* borrowed inputs), so even a `&self`-only method pays
for it. It's largely a macro artifact, not a language requirement.

## Workaround: keep borrowing, just write the lifetime so there's no outlives bound

The fix with **zero runtime cost** is to tie the returned future's lifetime
directly to the inputs instead of routing through `'async_trait` — no clone, no
`unsafe`, fully sound:

```rust
// only &self borrowed:
fn m(&self) -> Pin<Box<dyn Future<Output = R> + Send + '_>>;

// also borrows args — unify every input ref under one lifetime 'a:
fn m<'a>(&'a self, x: &'a X) -> Pin<Box<dyn Future<Output = R> + Send + 'a>>;
```

No `where Self: …` outlives bound ⇒ region-free `ParamEnv` ⇒ the `Send` proof
lands in the **global** cache and is derived **once** instead of per handler
(`borrowed`/`unified` above: ~100× faster than `outlives`, same as the clone form
but free). Drop-in for the common case where service-trait methods are `.await`ed
in the caller's scope. The one limit: a `+ 'a` future isn't `'static`, so it can't
be detached via `tokio::spawn`; native async fn in traits (RPITIT, 1.75+) produce
this shape automatically where `dyn` dispatch isn't needed.

**Only when the future must outlive the borrow** (detached spawn, stored futures)
do you need to make it `'static` by owning its state — clone the receiver into the
`async move`:

```rust
fn m(&self) -> Pin<Box<dyn Future<Output = R> + Send>> {
    let this = self.clone();   // Self: Clone + 'static — cheap for Arc bundles; &T args become owned
    Box::pin(async move { /* body, `self` -> `this` */ })
}
```

Fast, but pays a receiver clone (and `to_owned` of `&T` args) per call.

Orthogonal levers: an opaque future boundary (`#[inline(never)]` helper returning a
boxed `dyn Future`, capping proof *depth*); type-erasing the shared state
(`Arc<dyn Trait + Send + Sync>`); splitting crates (caps `N` per crate). Things
that **don't** help: `tower::ServiceBuilder` (a different, nested-builder cost),
`#[axum::debug_handler]` (diagnostics only), cranelift/linker tweaks (codegen, not
trait solving).

## Files

- `gen.py` — region on/off scaling generator (`gen.py K D M region`)
- `measure.sh` — A/B + scaling driver (stock rustc + `-Zself-profile`)
- `gen_variants.py` / `trigger.sh` — isolate the trigger and demo the zero-cost fix
  (`outlives` / `borrowed` / `unified` / `owned`)
- `minimal.rs` — the shape at a glance, with the `#[async_trait]` → region mapping
- `ISSUE.md` — upstream issue draft (full root-cause analysis, soundness
  discussion, next-solver status, PoC patch)
