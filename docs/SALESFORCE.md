# Salesforce / Apex Support

code-review-graph indexes Salesforce Apex via repo-local
`.code-review-graph/languages.toml`. Post-build resolvers improve static call
tracing, trigger → handler wiring, and optional metadata indexing.

## Quick start

### 1. Add Apex language config

Create `.code-review-graph/languages.toml`:

```toml
[languages.apex]
extensions = [".cls", ".trigger"]
grammar = "apex"
function_node_types = ["method_declaration", "constructor_declaration"]
class_node_types = ["class_declaration", "trigger_declaration"]
call_node_types = ["method_invocation"]
comment = "Salesforce Apex — classes, triggers, method call graph"
```

### 2. Build the graph

```bash
pipx install -e .   # fork/dev install
code-review-graph build
code-review-graph status
```

### 3. Optional: embeddings for semantic search

```bash
code-review-graph embed --provider local   # or openai
```

When `embeddings_count` is 0, `list_graph_stats` suggests running embed.
`Field`/`SalesforceFlow`/`Object` nodes are embedded too — natural-language
questions like "which field stores the acceptance status" work without
knowing the exact API name.

### 4. Optional: field formula metadata (Phase 6)

Metadata indexing runs automatically: it reads `packageDirectories` from
`sfdx-project.json` when present (so non-`force-app` package layouts are
covered), falling back to `force-app/` if there's no `sfdx-project.json`.
To override the search paths explicitly, create
`.code-review-graph/metadata.toml`:

```toml
[metadata]
enabled = true
paths = ["force-app/main/default/objects"]
include_formulas = true
```

## Agent query recipes

| Task | Tool | Example |
|------|------|---------|
| End-to-end handler pipeline | `trace_pipeline` | `task` + `include_source=true` |
| Who calls a static utility method? | `query_graph` | `callers_of` on `file::Class.method` |
| Trigger → handler chain | `query_graph` | `callees_of` on trigger qualified name |
| End-to-end flow | `traverse_graph` | Start at utility or trigger, depth 4 |
| Field formula lookup | `semantic_search_nodes` | Query field API name |
| What Apex does a Salesforce Flow invoke? | `query_graph` | `callees_of` on the flow's qualified name, or filter `INVOKES` edges |
| What object does a Flow act on? | `query_graph` | `REFERENCES` edges from the flow |
| What LWC/Aura components call this Apex method? | `query_graph` | `callers_of` on the method — resolves `@salesforce/apex` imports automatically |

**Tips:**

- Prefer `callers_of` on the **method** node (not just the class).
- If method query returns 0 on older builds, retry on the parent class.
- Managed-package symbols (`sitetracker__`, `strk__`): `query_graph`,
  `traverse_graph`, and `trace_symbol_context` now detect this automatically
  — a miss on a namespaced symbol (or a symbol this repo's Apex calls via a
  `sitetracker.`/`strk.` receiver) returns `managed_package_namespace` and a
  `next_tool_suggestions` entry pointing at strk-mcp instead of a bare
  "not found", so there's no need to retry with Grep/Read in this repo —
  the package's own source is never here.

## What gets resolved

| Resolver | Input | Output |
|----------|-------|--------|
| `apex_static_resolver` | `Class.method()` CALLS with bare class target | Qualified method target |
| `apex_trigger_resolver` | `createAndExecuteHandler(Handler.class)` in triggers | `INVOKES` edge to handler class |
| `metadata_indexer` | `*.field-meta.xml` | `Field` nodes (type, relationship, formula in `extra`) + resolved `REFERENCES` edges for formula field/relationship traversal + `BELONGS_TO` edge to an `Object` node |
| `metadata_indexer` | `*.flow-meta.xml` | `SalesforceFlow` node (process type, trigger, step summary in `extra`) + `INVOKES` edges to Apex classes (`actionCalls`) and subflows + `REFERENCES` edges to objects touched |
| `lwc_apex_resolver` | `@salesforce/apex/Class.method` / `@salesforce/schema/Object.Field` imports in LWC/Aura `.js`/`.ts` | Rewrites the bare-string `IMPORTS_FROM` target to the real Apex method / Field node, and resolves same-file `CALLS`/`REFERENCES` edges on the imported local name (both `@wire` and imperative usage) |

`Object` nodes are minimal stubs (name only) unless/until full
`.object-meta.xml` parsing is added — this deliberately covers objects with
no local object metadata at all, which is the normal case when extending a
managed package's object (adding a field or a flow that queries it) without
redeclaring metadata that would drift from the installed package version.

Note: the node kind is `SalesforceFlow`, not `Flow` — `flows.py` /
`get_flow_tool` already use "flow" for a derived concept (execution paths
through the call graph), unrelated to Salesforce's declarative Flow
automation metadata.

## Probe Apex AST locally

```python
import tree_sitter_language_pack as tslp

source = open("MyClass.cls").read().encode()
parser = tslp.get_parser("apex")
tree = parser.parse(source)
print(tree.root_node.sexp())
```

## Eval benchmark

```bash
code-review-graph eval --repo salesforce-apex-fixture --benchmark multi_hop_retrieval
```

Uses the sample trigger/handler fixture under `tests/fixtures/apex/acceptance_package_fixture/`.

## Limitations

- Flow XML indexing is one node per flow — the internal step sequence is
  a compact summary in `extra["steps"]`, not a fully exploded per-element
  graph. Decision branch logic (which rule leads where) isn't distinguished.
- No permission sets, profiles, layouts, or custom metadata type records
- LWC/Aura resolution covers the standard `import x from '@salesforce/apex/...'`
  / `'@salesforce/schema/...'` default-import form only (the only form
  Salesforce's own framework allows for these two module families)
- `Object` nodes are stubs (name only) until full `.object-meta.xml`
  parsing is added
- No cross-org / deployed metadata
- No runtime Apex semantics (governor limits, sharing, etc.)

See also [CUSTOM_LANGUAGES.md](CUSTOM_LANGUAGES.md).
