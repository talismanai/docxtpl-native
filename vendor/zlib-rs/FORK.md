# Default-level deflate compatibility

Pinned crates.io zlib-rs 0.6.8, source commit
`0254e07204884bfc902d6ebb68c8b703d0ea49cf`; Zlib license retained.
UPSTREAM.json records each original file and the three changed files.

At level 6, use the rolling hash and slow matcher, accept three-byte matches,
and discard three-byte matches more than 4096 bytes away, matching the fixed
Python zlib reference. Other compression levels retain the upstream strategy.
The laboratory experiment preserves the original-versus-modified checks and
raw-deflate corpus. ZIP/OPC integration and metadata have separate gates.
