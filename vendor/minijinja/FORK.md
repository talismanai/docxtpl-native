# Compatibility fork

Upstream MiniJinja 2.24.0, commit
`0ca749f7ba507514fa6b052c74130ae6ae472e03`, Apache-2.0. LICENSE and upstream
authors are retained. UPSTREAM.json records every original Git blob hash and
the exact local changes; scripts/check_vendor.py verifies them before builds.
This is a maintained local fork, not an unmodified upstream release.

## Local changes

- `Environment::set_loaded_template_validator` decides, on each reuse, whether
  a template the shared loader cache holds is still valid; an invalid one is
  loaded again. An `import` or `from ... import` without context reuses the
  module its template produced (like Jinja's `Template._module`) while that
  loaded template is cached; a module with native macros is reused only in
  the state that created it. A Rust test pins reuse and invalidation.

- A scoped block emits `RuntimeContextEvent::ScopedBlock` with the visible
  template variables (assignments and loop variables); a host may return the
  context the block runs with, as Jinja passes `context.derived(locals())`.
  A Rust test pins the variables offered.

- A host-provided `self.<name>` value (the self-block prepare hook) is
  awaited like an attribute read. `without_host_state` runs code with no
  state installed, for a host that suspends a render.

- Blocks can be rendered for a host: `render_block_in_host_state(name, depth)`
  in a fork of the installed state, and `Template::render_block_with_root`
  outside a render, in a new state whose root is the given context.
  `State::block_stacks` lists the blocks and their definitions,
  `Environment::set_runtime_blocks_loaded` reports the definitions an
  `extends` added, and `has_host_state` tells whether a state is installed.
  A Rust test pins rendering inside and outside the render.

- The module an `import` creates is public as `RuntimeModule`: the imported
  template's name, the body's output joined and as its fragments (kept by the
  capture that ends the import), the exports, and a host object recorded once
  that then answers attribute reads. `Environment::set_runtime_module_created`
  observes each module before it is assigned. A Rust test pins these.

- A filtered loop resolves the context names its predicate looks up once,
  when the loop starts and before its source is iterated, as Jinja's filter
  generator does; names bound by template frames stay live. The bindings are
  root bindings of the predicate's frame. The handoff hook also runs with
  `"start"` when a filtered loop starts. A Rust test pins one resolution and
  the start call.

- The loop recursion hook receives the called loop and may complete the call
  with a `LoopCallOutcome` instead of recursing; `Loop::recurse_from_host`
  renders the loop body for an iterable in a fork of the installed state. The
  handoff hook also runs before a loop's `changed` or `cycle`. A Rust test
  pins a completed call and a host recursion.

- `Environment::set_runtime_loop_handoff` lets a host take over a filtered
  loop (with the state installed) before the VM computes `length`,
  `revindex`, `revindex0`, `last` or `nextitem`. `Loop::retain_predicate_state`
  (unsafe: `Environment<'static>`) keeps a copy of the installed state,
  without loop controllers, so accepted items can still be pulled after the
  render. `DetachedActivation` shares that copy type. A Rust test pins the
  hook and post-render pulls.

- Filtered loops that use `loop` evaluate their predicate as a VM subroutine
  for any predicate and body (`lazy_filtered_predicates`, on by default;
  `Environment::set_lazy_filtered_predicates(false)` keeps only the simple
  cases), so predicates run when the next accepted item is needed. Such a loop
  can be handed to a host delegate: `with_host_state` installs the state that
  `Loop::next_remaining_item` forks to run predicates when the host pulls
  items, and the VM installs it while it advances or reads a delegated loop.
  Rust tests pin the interleaved predicate order and host pulls.

- A loop can be handed to a host object (`LoopDelegate`) when it escapes to
  host code. `Loop::delegate_with` gives the host the native state (index,
  depth, iterable, cached length, lookahead, adjacent items, last `changed`
  arguments); afterwards the VM advances the loop and reads its attributes and
  methods through the host, which pulls unconsumed items with
  `Loop::next_remaining_item`. Filtered loops are not handed over. A Rust test
  pins the handoff state, the lookahead transfer and the delegated reads.

- The `start` loop handoff also runs at the start of an unfiltered loop that
  uses `loop`, after its frame is pushed, so a host can build its loop object
  there (as Jinja's LoopContext constructor runs). A Rust test pins when it
  runs.

- `Environment::set_runtime_loop_constructed` (default: false) tells the VM
  that a filtered loop which uses `loop` builds its loop object through a host
  constructor at the `start` handoff. The predicate's context names are then
  resolved after that handoff, from the enclosing frame's base, because the
  constructor may change them. Otherwise they resolve before the source is
  iterated, as before. The single name a simple comparison captures is
  emitted as `PrepareCapture`, which defers it to the same point in that case.
  A Rust test pins the order.

- `RuntimeLoop::release_remaining` drops a delegated loop's unconsumed items
  and iterators (after unlocking them, since a host finalizer may advance the
  loop) once the host's item source over the loop is released. A loop that is
  not delegated keeps them. A Rust test pins both cases.

- `detach_current_host_state` copies the state installed with
  `with_host_state` (see `State::detach_for_host`), so host code can pull a
  filtered loop's predicates on its own coroutine while the render that
  installed the state cannot suspend. A Rust test pins that it detaches only
  inside the scope.

- A host may resume a suspended render on another thread (the Python binding
  moves its fiber at a clean await). The host-state slot and the
  loader-compilation flag are therefore read and written only through
  functions that are never inlined, so an activation live across the
  suspension computes their thread-local address on the thread that runs it.
  Behaviour on one thread is unchanged.

- `LoopDelegate::released` (default: nothing) is called when the VM pops a
  delegated loop's frame, with the number of other references to the loop, so
  a host that keeps the loop alive from its own object can break the cycle when
  nothing else needs it. A Rust test pins the count with and without a macro
  that captures the loop.

- A deferred `super()`, self-block call or recursive loop call can be detached
  from the activation that created it. While the deferral hook runs, the state
  records the operation; `State::detach_deferred` (unsafe: the environment must
  be `Environment<'static>`) copies the frames and block stacks, shares macros,
  temporaries and the retained template arena, and owns a clone of the
  environment, so `DetachedActivation::run` executes the operation after the
  render and its environment are gone. Renders from owned compilations retain
  their root template in the arena. Detaching is refused when instructions
  could be borrowed from outside it (no shared loaded cache, templates added
  from borrowed sources, or a root that is not an owned compilation). Rust
  tests run `super()`, a recursive loop and a self block after dropping the
  environment and pin the refusals.

- `State::detach_for_host` (unsafe: `Environment<'static>`) returns the same
  detached copy as `detach_deferred` without a pending operation, as a
  `DetachedHostState` whose `run` hands host code the copied state. The binding
  runs an async macro body on it, in its own coroutine, when the body is awaited
  inside a host callback during which the running render cannot suspend. A Rust
  test calls a macro on the copy after the render and environment are gone.

- The host can construct the root `self` reference in the symbol prologue and
  resolve an overridden block attribute before evaluating call arguments. The
  selected callback follows the existing call/await machinery; returning no
  override retains native block execution. A Rust test pins constructor, lookup,
  argument and call order, lookup failures, and the unconfigured native path.

- Imported names resolve only against module-local exports. Missing names use
  a host hook at the import site, with the importing template context, source
  position and selected template name retained through the lookup.
  In the host's include-context compatibility mode, private assignments and
  imported bindings are excluded from those exports. An assignment following an
  import makes the name public again. Import targets publish to the host context
  separately, discarding their public export markers; the default engine mode
  retains its original export behavior.
  A completion event can supply the module's copied export mapping, honoring
  context mutations and a replaced `get_exported`. Native macro values remain
  owned alongside their exported facades while the module is alive.

- Import statements retain their context option in the AST. Isolated imports
  transfer module exports to the importing frame, and the Python AST adapter
  lowers import names and aliases. Module reuse and invalidation are not
  equivalent to Jinja's; see `docs/STATUS.md#accepted-debts`.

- Metadata-filter eligibility examines the loop body's AST and conservatively
  rejects direct loop escapes, dynamic lookups and template composition there.

- Predicate variable checks use the expression AST, so identifiers containing
  the text `loop` do not trigger loop-specific behavior.
- Factory argument capture checks also use AST variable references rather than
  matching variable names inside attribute names or string literals.

- Filtered loops without loop metadata stream based on the loop body's AST,
  rather than scanning unrelated source for references to `loop`.

- Membership in a native iterable propagates iterator errors and stops at the
  first matching item.

- Boolean conversion of test results propagates errors from foreign objects.

- Filtered inputs close after a source, predicate or predicate-truth error;
  the previous iterator and buffered values are released outside their locks.

- A materialized loop iterator is replaced under the iterator lock and then
  released outside it, allowing its finalizer to reenter the new iterator.

- Formatting a loop snapshots its cached length before invoking the host's
  formatter, so the formatter may reenter without holding the length lock.

- Replacing a length cached during a reentrant callback drops the old value
  after releasing the cache lock, allowing host finalizers to reenter. Filtered
  loops release replaced cached and supplied lengths outside their respective
  locks, preserving the order of the two assignments.

- Reverse-index properties read the current loop position after evaluating
  length, because a host length callback can reenter and advance the loop.

- Loop reverse-index arithmetic carries the index as a `Value` through the
  host operation rather than narrowing the host interface to `usize`. Current
  native loop positions remain `usize`; the host adapter is not implemented.

- State tracks scoped VM render frames across root rendering, includes, blocks,
  super and inheritance transitions. Macro bodies share their caller's count,
  reentrant root renders use independent counters, and scoped guards restore
  counts on success and error. Snapshots retain the frame kind, executing
  template name and a process-wide activation token. The depth and snapshots
  describe VM execution frames, not all Jinja or Python coroutine frames.
  A separate scoped counter tracks active native filtered-input evaluation in
  lazy, streamed and collected paths, including suspension and nested macro
  state. It does not count host-language generator frames or change warning
  dispatch. An optional loop-next wrapper exposes a scope snapshot while
  native input advancement is active, including suspension; it is disabled
  unless the host installs it.
  A failed super frame push restores the prior instruction and block depth;
  a failed super body discards only captures opened during that call.
  Failed block and super bodies restore VM frames and lexical autoescape;
  foreign evaluation-context side effects remain visible to the host.
  Nested VM evaluations restore the caller's pending and prepared dispatch
  state on success or error, dropping replaced values outside RefCell borrows.
  A host may handle an explicitly replaced block at its stack depth; declining
  executes the existing native block, including for `super`.
  Required-block checks run at the declaration rather than when a block is
  called through `self`; a host may check the effective foreign block stack.
  A `self` block expression can use the host's deferred block hook before its
  body executes; direct output optimization retains its existing path.
  The expression carries the captured host call-policy decision to an
  optional host guard, which runs before the block and deferred hook.
  When a host policy factory is installed, direct `self` output also takes
  that guarded path, even when prepared output is disabled.
  Direct `self` output checks the deferred hook at execution time, including
  when the hook was installed after the template was compiled.
  Self-block call arguments are evaluated and unpacked before the guard and
  passed to an optional host validator; empty expansions work without a host.
  A configured call dispatcher is captured before those arguments and retained
  across validation and deferred execution, including nested argument calls.
  Include errors restore caller frames, context, closure and capture state;
  failed recursion-depth increments do not alter the caller's depth.

- A filtered-loop length hook can supply a host value or propagate an error
  before the VM consumes the remaining filtered input. It is disabled by
  default; declining the hook uses the existing native collection path. This
  primitive does not create a Python generator or implement Python callbacks.
  Hosts may also remove an already accepted buffered input without changing
  loop metadata or consuming its separate lookahead. If another predicate must
  run, the operation returns an error without consuming raw input. A separate
  length hook receives a scoped continuation that can ask the VM to evaluate
  predicates and return the next accepted item lazily. The continuation cannot
  escape the hook; this does not provide a retained foreign generator.

- Loop advancement updates the index and previous item before releasing the
  previous value, and drops replaced values outside the iterator mutex. A
  finalizer can therefore inspect or advance the same native loop reentrantly.
  This does not provide a Python `LoopContext` adapter.
  `loop.changed` also releases a reentrantly replaced comparison value outside
  its mutex, so its finalizer can update the slot without deadlocking.
  A host may receive the active state when comparing `changed` arguments; the
  original stateless hook remains available, with the last setter taking effect.
  An optional operation scope also covers the release of replaced arguments.

- An optional runtime error observer can inspect live frame bindings and the
  error before the root VM state is dropped. It is disabled by default; it
  does not reconstruct nested tracebacks or change error handling by itself.

- Safe or ordinary strings can carry an optional packed host provenance identifier while
  retaining their string and autoescape behavior. Copies and the `string`
  filter preserve it; operations that construct new values need explicit
  propagation. The representation keeps the upstream Value size invariant.
- Hosts can request ordered root output fragments without joining them. Nested
  captures still use their configured joiner. This exposes a primitive for a
  provenance-aware document pipeline; the root stream is distinguished from
  intermediate captures so host constant guards still run at the root. This does not
  itself tag deferred values or change any other render path. A read-only
  output queries expose whether the current target is that unjoined root and
  whether a nested capture marked restricted content. Hosts distinguish ordinary
  untrusted text from restricted deferred output; nested captures and text
  renderers are not reported as the unjoined root.
- An optional host guard checks emitted raw and constant text containing
  U+0001 and foreign constant fragments at the output instruction. Constant evaluation
  stays at compile time, and output in an unexecuted branch is not checked.
- Marked intermediate captures may be wrapped by a host callback at the capture
  boundary. Nested captures retain separate marks; environments without this
  callback keep their original capture behavior.
- Foreign loop iterators can supply a materialized remainder with a separately
  declared length, preserving lazy iteration of a custom collector result; a
  separate lookahead hook preserves the host iterator protocol.
- Loop `changed` retains its state and control flow in Rust. An optional host
  comparison callback applies foreign tuple equality and propagates errors;
  pure Rust environments retain the existing native equality behavior.
- An optional host loop-argument binder preserves keyword protocols before
  native `changed` and `cycle` execute. Hosts can identify opaque loop values
  and reject unsupported lookups before filtered-loop lookahead, then recheck
  policy after native metadata access can invoke user iteration. A host key
  adapter lets string item lookups use loop metadata and methods while keeping
  filtered lookahead in the VM; the conservative source scan recognizes these
  item accesses and ordinary loop metadata when choosing lazy filtered loops.
  Captured call policy and dispatcher are inspectable without consuming their
  lifetime.
- Recursive loop calls can use an optional host argument binder before the VM
  enters recursion. Recursive depth follows the called loop object, including
  aliases invoked inside another loop; ordinary nested loops start at depth 0.
- An owned compilation can be rendered without inserting its diagnostic name
  into the loader cache. The binding uses it for anonymous Jinja sources so a
  same-named loader entry is not overwritten; the caller must render with the
  environment whose configuration compiled the source.
- An opt-in loader cache is shared across environment clones. Loader and
  compiler callbacks run outside its locks. Each render retains its own loaded
  compilation, while the shared entry keeps the last completed compilation;
  local explicit templates take precedence, and removal or clearing detaches
  the modified clone's cache. Direct Rust template handles own their loaded
  compilation, including across cache replacement. VM states retain loaded
  compilations for their own lifetimes and share that retention with nested
  macro states. The arena is append-only and deduplicated; its private lifetime
  extension and VM call sites document their safety invariants.
- An optional host metadata value is captured after source validation and
  retained by each compilation's instructions, including block instructions.
  Runtime callbacks can access it without keying state by the public name.
- Optional compilation keys isolate host callbacks for simultaneous or nested
  compilations while preserving the public template name. A paired cleanup hook
  releases the host's compilation-only state on success or error.

- Opt-in fragment output preserves foreign values through nested captures and
  macro bodies, joining them at each capture boundary. Finalized constants may
  carry a foreign fragment without repeating compile-time callbacks. Filter
  blocks emit their result without expression formatting in fragment mode.
  Opt-in boundaries preserve output groups across control flow and loop
  iterations, while adjacent constant fragments are joined as one group.
  Ordinary Rust string rendering retains its original behavior.

- Comparison and conditional expression spans begin at their first token and
  retain that start across conditional chains. The constant-output adapter
  receives complete byte ranges, including after non-ASCII template text.
  This keeps source lookup and compile-time callback effects aligned with ASTs.

- Optional activation events attach foreign Context/EvalContext objects to the
  existing VM, create include scopes, publish completed assignments and preserve
  evaluation-context identity through autoescape save/revert. Publication is
  opt-in; pure Rust environments do not emit the extra instructions. Loop/block
  assignments are distinct from iteration targets and local macro/with scopes.
- A foreign macro factory constructs one stable object at definition time; a
  foreign call hook binds arguments before the existing VM executes the body.
  Macro definitions retain their defining context. The existing closure tracker
  keeps foreign bodies alive through deferred calls and releases them at render
  teardown. Checked body argument counts prevent malformed binders from entering
  bytecode with an invalid stack. Await hooks receive the current State so host
  callbacks can borrow the active VM safely across native macro calls.
  Shared identity tokens distinguish instruction entries in sibling macro
  scopes; an escaped value cannot accidentally execute a reused table slot.
  Macros retain their defining template's block chain, including parents loaded
  after the macro definition; includes keep distinct block ownership. Captured
  chains start at the most-derived block even when definition occurs in super.
  The parser accepts blocks in macro bodies, as Jinja does.

- Runtime container factories can retain dynamic keyword mapping sources until
  the host binds the call. This avoids recursively hashing foreign string
  objects as native iterable map keys. Native Kwargs consumers request a checked
  string-keyed view; ordinary Rust execution retains the existing merge path.
  The CPython binding reuses CPython's dictionary merge and callable formatting
  primitives, preserving key objects, mapping callbacks and duplicate errors.
  When positional splats and keywords coexist, positional batches are unpacked
  before the keyword wrapper is appended; a foreign list factory cannot erase
  the wrapper or turn keyword sources into an extra positional dictionary.
  Unpacking also validates every yielded value immediately, propagating foreign
  iterator failures before evaluating later keyword expressions.

- Optional runtime await points resolve calls, filters, tests and attribute/item
  lookups using the existing VM stack, frames, captures and recursive execution.
  The host owns suspension and scheduling; the fork has no Python dependency.
- Object::get_method can prepare a foreign callable before argument evaluation,
  including suspension during attribute lookup. Native objects retain their
  call_method/unknown-method dispatch through a deferred bound method.
- Object::loop_iterator and LoopIterator::materialization_iterator preserve
  foreign async iterator acquisition and lazy-length protocols. The VM owns
  buffering, validation and stopping at the first error. Synchronous unpacking
  keeps the ordinary iterator protocol. Loop-context selection reuses the AST
  name tracker across closure scopes and records scoped blocks explicitly.
- Object::get_attribute is separate from item lookup. The VM and public
  attribute access use it; root-context resolution keeps its item semantics.
  The default delegates to the upstream string-key lookup, preserving native
  Rust map behavior.
- Object/Value::is_true_fallible permits a foreign __bool__ exception to stop
  VM control flow before entering a branch. Native Object implementors inherit
  the upstream behavior through a default method.

- Loop metadata peeks only for last/nextitem and computes/caches length
  on demand. Unsized iterators buffer the remaining values when length is
  requested; errors propagate immediately. Foreign length is fallible, live
  Python list iterators can grow/shrink, and terminal iteration preserves the
  last successful index/current/previous item. Filtered loops with a simple
  single-variable test or function predicate use a native predicate subroutine
  and buffer accepted items on demand for direct metadata access. Predicate
  callbacks see the outer template context. Recursion, else, compound targets,
  aliases and more complex predicates retain their prior path pending parity.
- Macros capture implicit varargs/kwargs, report their capture metadata and
  bind all parameters before evaluating defaults from left to right. An optional
  environment container factory creates original foreign tuples/maps without
  adding private helper names to template globals. This preserves Python
  container types and mutations in the CPython binding.
- Optional runtime operator dispatch preserves CPython arithmetic, reflected
  operators, comparison results, containment, concatenation and slicing while
  evaluation order remains in the Rust VM. Foreign namespace assignment uses
  an explicit fallible Object protocol. Default Rust execution keeps its prior
  operator and assignment implementations.
- An optional source validator runs before native compilation. The CPython
  bridge uses the original Jinja parser for source-language errors and disabled
  extension tags; parsing does not execute a Python template.
- Foreign-runtime environments disable native constant folding and construct
  fresh original lists/maps and actual tuple literals. Unary plus has its own
  instruction and foreign operator dispatch. Native Rust defaults retain their
  existing folding and tuple-as-list behavior.
- Optional constant-output conversion runs during compilation, retaining static
  autoescape scopes, finalized text and early compiler aborts. The Rust VM emits
  owned finalized text directly. A separate callback supports a single literal
  filter argument to a named function, rebuilding mutable constants on each
  evaluation. Other expressions and complete Jinja compiler behavior remain open.
- A separate opt-in flag retains custom test return objects. Truth testing occurs
  when VM control flow needs it, preserving identity and the timing of __bool__.
- An opt-in output preparation instruction captures formatter dependencies
  before expression evaluation, with a paired prepared formatter instruction.
  This retains Python's callable-load ordering for finalizers changed by a
  callback. Native defaults retain their original emit instructions.

These changes do not establish full Jinja2 compatibility. Loop object types,
macros, extension ASTs, sandbox/async and further
fallible operations remain executable requirements in the protocol matrix.

Root Rust regression tests exercise attribute/item distinction and branch
side-effect ordering. CI also runs the upstream crate's inline library unit
tests with all features; the complete upstream integration/snapshot corpus is
not vendored or claimed as executed.

Further scoped corrections:

- Filtered-loop collection counters use an internal increment instruction,
  bypassing foreign arithmetic and returning errors instead of panicking on an
  invalid count. Filtering is still eager; lazy side-effect ordering requires
  a separate implementation and qualification.
- An opt-in completed-iteration policy matches Jinja loopcontrols: an iteration
  interrupted by break/continue does not clear the loop's else indicator.
- Include context markers are preserved by the parser. An opt-in environment
  policy isolates `without context` while preserving environment globals and
  restoring the caller scope. Imports retain their existing behavior and still
  require separate context/cache qualification.
- Foreign objects may expose template-name strings without display conversion;
  candidate includes distinguish names from sequences of names. Arbitrary
  Python Template instances and loader error identity remain unqualified.

Filtered loops with variable targets, including nested unpacking, no recursion
and no source-wide reference to the loop object evaluate each predicate
immediately before the body. The
lexer distinguishes actual `loop` identifiers from unrelated names and text;
external templates retain a conservative path. An accepted-item marker keeps
`else`, `break` and `continue` behavior under both loop policies. Filtered loops
that need metadata or more complex semantics retain eager collection and remain
explicitly unqualified.

Unpacking reads at most the requested number of values plus one, and propagates
iterator errors immediately. An optional mismatch handler lets a foreign runtime
expose its native length-error type and message; the default reports a
MiniJinja unpacking error. A separate iterator-start error hook preserves
foreign error semantics without changing ordinary iteration. An optional
unpack handler can check arity with foreign objects before converting them,
avoiding inspection of discarded values.

Concat expressions retain their grouping and evaluate all operands before an
optional environment concat callback. The native default also shares the same
instruction, and undeclared-variable tracking visits every operand.

An opt-in owned TemplateAst adapter lowers foreign syntax trees to the same
compiler/VM used by the native parser. Loader and temporary-template storage
keep the AST owner with its compiled instructions; clones and captured states
retain that storage. Per-node constant IDs are separate from diagnostic spans.
Extends uses the existing foreign template-name protocol. Python parser hooks,
unsupported foreign nodes and async interactions remain separate open work.

The opt-in include-context policy gives ordinary includes a lexical frame for
their assignments, reusing the existing VM frame stack. The compiler marks
module imports separately so their frame still exports assigned values. Caller
bindings survive nested/loop/macro includes; shared objects retain their normal
mutation semantics. Foreign Context/EvalContext identity remains separate work.

An opt-in compiler hook selects intercepted arithmetic and emits preparation
before operand evaluation. The existing VM/Object calls resolve the callee and
apply operands without implicit awaiting; ordinary calls keep their async
behavior. Blocks and standalone expressions share that policy and propagate
compilation errors. The default consumer needs no foreign runtime.

Unary signs wrap calls, attributes and subscripts before outer filters,
matching Jinja's precedence (`-value()` negates the returned value). A sign
following a bare test is a binary operator, not an unparenthesized test argument.
Unary parsing uses the existing recursion guard.

Empty subscripts are parsed as empty tuple expressions, matching Jinja's
`value[]` behavior. Multi-item non-slice subscripts lower to tuple expressions
through the existing container instruction. An opt-in runtime hook preserves
identity of constant tuple subscripts, while dynamic tuples remain fresh.
Mixed-slice subscripts remain separate compatibility work.
Signed numeric literals participate only when their compiled instructions do
not dispatch an intercepted operator; sandbox callbacks remain per evaluation.

An optional runtime hook resolves non-finite floating constants when their
load instruction executes. The default runtime retains MiniJinja's values;
the Python binding can reproduce generated-code name lookup and exceptions.
Non-finite items are excluded from tuple-subscript interning because their
runtime global can be an arbitrary mutable or unhashable object.

An optional arithmetic-folding hook evaluates supported numeric expressions
containing non-finite literals using the foreign runtime's compile policy.
Its operands are compiled recursively, and rejected folds retain the ordinary
operator path. Finite folded results use the existing foreign constant pool;
non-finite results are resolved at runtime by the non-finite constant hook.

The compatibility compiler also interns finite numeric and string literals at
the foreign runtime boundary, sharing the pool with folded arithmetic results.
Signed numeric constants are interned after an ordinary unary operation, but
intercepted sandbox operators retain their per-call result identity.

The foreign arithmetic-folding hook covers supported finite numeric binary
expressions as well as expressions containing non-finite constants. The native
AST traversal restricts the hook to primitive numeric arithmetic; the foreign
runtime retains control of optimization and operator interception policy.

When Jinja optimization is disabled or a frame is volatile, supported numeric
expressions also follow CPython's bounded constant-folding rules. A raw load
keeps non-finite results as bytecode constants rather than resolving names;
NaN identities are scoped to their source sites through an optional hook.

An optional call policy is selected at compile time for each source call. At
runtime its dispatcher is captured before the callee and arguments, and nested
calls retain their own pending dispatchers. The host consumes the prepared
policy and callable at the binding boundary; the default VM has no such hook.
Supported constant numeric call arguments are folded before policy selection
when the Jinja optimizer is active, preserving the ordering of those callbacks.

For supported numeric trees with the Jinja optimizer disabled, the compatibility
compiler selects operator policies in source visitation order before asking the
foreign runtime to fold arithmetic. The selected factory is retained for an
intercepted operation and reused during emission; no policy is selected twice.
Other optimization modes continue through their existing path.

For supported numeric expressions, the compatibility optimizer visits child
nodes before their parent and retains partial folds. Constant evaluation checks
the parent policy before revisiting an unfolded child, matching the source
optimizer's observable order in the covered arithmetic trees. Foreign policy
and numeric conversion remain optional; the default VM retains its behavior.
The arithmetic traversal also visits variable leaves, which cannot be folded,
so that sibling constants and parent interception decisions occur in order.
Variable-rooted attribute chains are likewise visited: their constant attempt
observes the foreign attribute-dispatch lookup before the variable prevents
folding. The binding decides which lookup failures mean a nonconstant value,
and retains and releases the dispatcher at the source-language boundary.

An optional runtime attribute hook captures the foreign dispatcher on the VM
stack before evaluating a receiver, including method-call receivers. The
binding can dispatch foreign objects with that capture and release it before
awaiting the result; VM-native objects retain their existing lookup path.

The compile-time traversal handles variable-rooted item chains with supported
keys. It visits the key during optimization and retains the foreign `getitem`
dispatcher while the nonconstant base is evaluated, with the same failure and
release boundary used for attribute dispatch.

An optional runtime item hook captures the foreign dispatcher before the base
and key are evaluated. The binding uses it for foreign objects and releases it
before awaiting the result; VM-native objects keep their existing lookup path.

The loop post-lookup hook receives the native loop, the original item key or
attribute name, the captured dispatcher and the result, and may replace that
result. This makes those values available to a future host policy adapter; it
does not itself implement Python loop identity or sandbox callbacks.

An optional template rendering entry point retains host values in nested
captures while leaving the root output as a normal text stream. This lets a
binding preserve Python capture identity without weakening root output guards.

Loop length may carry a host value instead of coercing it to `usize`; the
foreign iterable can also supply subtraction for reverse indices and addition
after materializing the remaining iterator. Native iterables retain numeric
defaults. Exact Python loop-object identity remains unimplemented.

An additive loop-iterator protocol lets a host own and recheck its lookahead
state on each peek and advance, and retain that state when collecting remaining
items. Existing loop iterators keep the original VM-managed cache. This does
not implement filtered-loop callback identity or a Python LoopContext adapter.

An opt-in loop-advance hook exposes the stable native loop object after each
valid advance of an exposed loop, before its body executes. It skips internal
filter loops, empty loops, failed items and terminal advances; it does not
itself consume the iterator. The Python binding does not install this hook,
and it does not implement Python loop identity or sandbox policy callbacks.

The native loop also exposes host advancement of its shared iterator, using the
same index update as VM iteration. End-of-iteration and invalid values retain
the previous index. A loop with a deferred predicate can advance an already
accepted buffered item or observe a known end; it is rejected without consuming
input when another predicate must execute in the VM. The host must supply the
appropriate runtime context; invalid values become errors. This API does not
implement a Python loop object, callback reentrancy, or filtered-loop execution.

The opt-in filtered-loop subroutine also accepts a single equality or ordering
comparison between the loop target and a literal, in either operand order.
Other predicates retain their existing paths; this is not a claim of general
filtered-loop laziness.

For a filtered comparison against a second variable and a simple iterable
variable, the VM captures the external value before entering the iterator and
reuses it in the predicate, including when the loop body shadows that name.
The captured object is not copied. The same path supports a zero-argument call
to a distinct named factory, capturing the external value after the factory
returns and before opening its result. If the factory is the comparison value,
an opt-in VM instruction retains the resolved callable across its invocation
for the predicate. Named factories with arguments use a prepared callable;
when the only argument is the captured comparison variable, its value is
retained and reused. Other occurrences of that variable inside arguments and
other iterable expressions keep their existing paths and remain unqualified.
Preparing a factory retains the distinction between a missing binding and an
explicit undefined value until the call, preserving Rust error kinds and the
evaluation of arguments before either call error.

An optional prepared-lookup hook lets a host distinguish a captured missing
binding from an already materialized value. It is used only for the prepared
factory and filtered-comparison captures; ordinary lookups keep their existing
path. Returning a handled missing result continues searching outer frames and
globals. The hook does not implement a general foreign undefined protocol.

Prepared filtered factories can retain a binding used inside argument
expressions containing only that binding and literals. A stack-position scope
keeps the binding available while intermediate operands are pushed; an optional
host hook materializes the callable before the arguments and the binding at
each expression use. Expressions with other names, calls, filters, tests or
slices take the upstream path and are not qualified by this extension.

For supported argument expressions with multiple names, the compiler collects
their variable reads in symbol-visitor order and stores each resolved binding in
an isolated VM scope; the factory reuses its already resolved value. These
reads follow Jinja's positional, keyword, positional-splat and keyword-splat
visitor order, independently of the order of argument evaluation. The
local argument capture occurs at the loop; an optional root prologue can
preload bindings earlier.
Completed capture scopes retain their values until the VM evaluation ends so
that removing context references inside a factory does not release them during
the loop body; each scope releases its values in capture order. This is not a
general Jinja frame-lifetime guarantee.

The fork also contains an ordered root-frame symbol planner and an offline
diagnostic example. The planner records Jinja-style resolve, missing-local and
special-self initialization candidates across root statements and frame
boundaries. An opt-in VM prologue stores ordinary resolved and missing root
bindings separately from callback-visible context values; includes save and
restore that state. Named calls in this mode prepare their callable before
argument evaluation, except for the VM's special `super` and `loop` calls and
the separately prepared filtered-loop factory. Special `self` initialization
is not implemented by the prologue. Compatibility remains limited by the parser and untested constructs;
the synthetic differential in `scripts/check_root_symbol_plan.py` reports
parser rejections as mismatches.
Calls, filters, tests, slices and the special `loop` name remain outside this
extension. The scope does not claim complete Python Jinja name-resolution timing.

Optional template-prelude and template-finalizer callbacks receive compilation
metadata respectively after initial autoescape and before native code generation,
and after successful code generation before the template is returned. The Python
bridge uses them to plan runtime imports and capture asynchronous dispatch per
template. Failed native compilations do not run the finalizer; host import
semantics and all compilation-time mutations remain subject to separate tests.
An opt-in await-preparation instruction runs before an awaited expression's
operands, allowing the host to reject a missing compiled dispatch name before
user callbacks. It does not model every Python frame or source import hook.

An opt-in `super` continuation hook can resolve the parent block on demand,
including the optimized output instruction. The host must keep its callback
within the borrowed VM state lifetime and decide how an escaped continuation
behaves. This hook does not by itself provide a Python coroutine, establish
Python frame identity, or implement deferred recursive-loop execution.

An independent opt-in recursive-loop continuation captures the nested output
and returns to the call site only when the host drives it. On a nested error,
the VM restores its context depth, autoescape state and output capture depth.
The host owns the continuation lifetime; this mechanism does not by itself
provide retained Python generator or coroutine semantics outside that scope.
# Block execution context

Unscoped block calls temporarily hide nested template frames and restore them
after success or error; suspended frames remain alive and count toward the
recursion limit. Scoped block calls retain the active frames. A `self` block
call uses the nearest block or template context rather than later nested locals.
This does not by itself adapt Python `Context` objects for scoped callbacks.

# Block declaration inventory

Code generation records block declarations in traversal order alongside the
existing lookup map. `Template::block_names` exposes this inventory for a host
to construct its context; it does not create foreign block callables or
implement inheritance, identity, or lazy execution at the host boundary.

# Compilation origin

The compilation scope exposes whether source came from the loader or directly
from the host. The scoped flag restores its previous value after nested calls
and on unwinding; the host uses it to select runtime globals at the appropriate
source-loading phase. It does not itself implement Python loader protocols.

# Per-frame runtime error observation

An optional runtime error observer receives the current VM state and instruction
line before each `bail!` unwinds. It is unset by default and is used only by an
experimental binding probe. It does not cover every error exit or establish
Python traceback, callback, reentrancy, or lifetime equivalence.

# Host loop iterator selection

An optional host hook selects a loop iterator using the current state and
reports metadata and filtering. Without the hook, or when it declines, the
existing iterator path is unchanged. This primitive does not implement a
foreign filtered generator or establish callback and lifetime equivalence.

# Streamed filtered-loop input flag

Streamed filtered loops tag their input loop so an optional host iterator hook
can distinguish it from an ordinary loop. The flag does not create a foreign
generator, implement its lifetime, or alter iteration when the hook declines.

An opt-in `Environment::set_normalize_newlines` converts CRLF and bare CR to
LF after the source validator runs and before tokenizing, as Jinja's lexer
does after extension preprocessing. Every compile path applies it; sources
that would need rewriting are compiled as owned templates, and a borrowed
`CompiledTemplate::new` request with such a source is rejected rather than
compiled unnormalized. The default remains MiniJinja's verbatim newlines.

An opt-in `Environment::set_string_format_operator` implements `string %
arguments` (Python printf formatting) for a string left operand only; other
operands and operators keep MiniJinja's behavior, and constant folding of a
string `%` still defers to runtime. The browser engine uses it; the default is
unchanged.

A test module in `compiler/parser.rs` (`jinja_grammar_inventory`) pins where
this parser and Jinja 3.1.6's disagree on whether a source parses, so a host
that skips Jinja's parser can keep those constructs out of what it accepts.
It changes no behaviour.

Jinja semantics found by the language partition probes (tests in
`vm/mod.rs`, module `jinja_partition_tests`):
- `{% with a = x, b = a %}` evaluates every value in the enclosing scope.
- The macro of a call block is anonymous (`caller.name` is none), as Jinja's.
- The undefined of an inline `if` without `else` carries its line
  (`Value::silent_undefined_line`), so a host can make Jinja's plain
  `Undefined` with its hint.
- A macro body escapes with the autoescape of where it is defined; its result
  is safe when the caller's evaluation context autoescapes
  (`State::eval_auto_escape`, Jinja's runtime `eval_ctx.autoescape`, which
  autoescape blocks set and macro bodies inherit from their caller).
- An `include` of an empty candidate list fails like a missing template, and
  `Environment::set_runtime_template_not_found` lets a host build the error
  (Jinja's `TemplateNotFound` / `TemplatesNotFound`).
- `Environment::set_runtime_host_error` lets a host type a runtime error the
  reference raises as its own exception (`extended multiple times`).
- `Environment::set_runtime_include_foreign` lets a host render an include
  target it owns, such as a Jinja `Template` object.
- A recursive filtered loop runs its predicate between bodies, as Jinja's
  generator does (`PushFilteredLoop` takes a recursion flag).
