# Direct asyncio laboratory

This isolated Rust prototype runs callbacks on a single Python asyncio task,
either directly or from requests supplied by a worker thread. It is not connected to the template
engine and does not establish Jinja compatibility. Async iteration, arbitrary
awaitable protocols, cross-loop futures, worker panics, reentrancy, context and
cancellation edge cases and performance remain unqualified.

The prototype consults Jinja's live `_common_primitives` value for each
callback, including mutations and replacements. It captures the function's
global and builtin dictionaries before executing callbacks; replacing the
function itself, import hooks and other mutated Jinja globals are unqualified.
Specific tests cover throwing invalid-yield, foreign-loop and self-await
errors into callbacks. Other asyncio implementations, scheduler and error
formatting edge cases, and races with cancellation remain unqualified.

`collect_iteration` explores synchronous and asynchronous iterator protocols;
it collects all values rather than streaming template output and is not a Jinja
loop implementation. Special-method edge cases, other mutable builtins, backpressure,
early exits and performance remain unqualified.

From the repository root, with the Rust environment configured:

```sh
CARGO_TARGET_DIR=/tmp/docxtpl-async-direct-target cargo build --release --locked --offline --manifest-path python/paritylab/async_direct_lab/Cargo.toml
python python/paritylab/async_direct_lab/test_async_direct.py --extension /tmp/docxtpl-async-direct-target/release/lib_async_direct.so
```

The thread-wakeup tests require an environment where asyncio's local
`call_soon_threadsafe` wakeup works. Some restricted sandboxes block the local
socketpair used by the Python event loop; a pure-Python wakeup control must
pass before interpreting these tests.
