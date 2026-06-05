// Illustrative minimal shape (see gen.py / measure.sh for the scaling version).
//
// `Deep`'s `Send` proof recurses through many layers. Many distinct wrapper
// types share `Deep`. Each wrapper's `Send` is asserted inside a function whose
// ParamEnv carries a free region (`<'a, U: 'a>`).
//
// With the region: each `Wi: Send` re-derives `Deep: Send` (and all its nested
// sub-proofs) from scratch, because a region inference var in the ParamEnv
// forces the result into the per-inference-context LOCAL evaluation cache.
// Without the region (`fn check_i()`), `Deep: Send` is proven once and shared
// via the global cache.

use std::sync::{Arc, Mutex};

pub type Deep = Arc<Mutex<Vec<Arc<Mutex<Vec<Arc<Mutex<Vec<u8>>>>>>>>>;

pub struct W0(Deep);
pub struct W1(Deep);
pub struct W2(Deep);

pub fn assert_send<T: Send>() {}

// Region in ParamEnv  -> re-derives `Deep: Send` per wrapper (LOCAL cache).
pub fn check0<'a, U: 'a>(_u: U) { assert_send::<W0>(); }
pub fn check1<'a, U: 'a>(_u: U) { assert_send::<W1>(); }
pub fn check2<'a, U: 'a>(_u: U) { assert_send::<W2>(); }

// No region -> `Deep: Send` proven once (GLOBAL cache):
// pub fn check0() { assert_send::<W0>(); }
