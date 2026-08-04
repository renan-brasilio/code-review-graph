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
Every Salesforce metadata node kind (`Field`, `SalesforceFlow`, `Object`,
`Label`, `PermissionSet`, `Layout`, `AuraComponent`) is embedded too —
natural-language questions like "which field stores the acceptance status"
work without knowing the exact API name.

### 4. Optional: metadata indexing configuration

Fields, Flows, Labels, Objects, Permission Sets, and Layouts are all
indexed automatically: it reads `packageDirectories` from
`sfdx-project.json` when present (so non-`force-app` package layouts are
covered), falling back to `force-app/` if there's no `sfdx-project.json`.
To override the search paths explicitly, create
`.code-review-graph/metadata.toml`:

```toml
[metadata]
enabled = true
paths = ["force-app"]
include_formulas = true
```

Point `paths` at the package root, not a specific subfolder like
`force-app/main/default/objects` — every metadata type is discovered by
recursively scanning `paths`, so narrowing it to one subfolder silently
excludes the metadata types that live elsewhere (Flows under `flows/`,
Labels under `labels/`, Permission Sets under `permissionsets/`, and so
on).

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
| What LWC or Aura components call this Apex method? | `query_graph` | `callers_of` on the method — resolves `@salesforce/apex` LWC imports and Aura `component.get("c.method")` calls automatically |
| What uses this Custom Label? | `query_graph` | `references_to` on the label name — covers both `Label.X` in Apex and `@salesforce/label/c.X` in LWC |

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
| `lwc_apex_resolver` | `@salesforce/apex/Class.method` / `@salesforce/schema/Object.Field` / `@salesforce/label/c.LabelName` imports in LWC `.js`/`.ts` | Rewrites the bare-string `IMPORTS_FROM` target to the real Apex method / Field / Label node, and resolves same-file `CALLS`/`REFERENCES` edges on the imported local name (both `@wire` and imperative usage) |
| `metadata_indexer` | `labels/CustomLabels.labels-meta.xml` | `Label` node per entry (value, categories, short description in `extra`) |
| `apex_label_resolver` | `Label.X` field access in Apex `.cls`/`.trigger` | `REFERENCES` edge to the `Label` node, scoped to the enclosing method when one contains the reference |
| `metadata_indexer` | `objects/X/X.object-meta.xml` | Upgrades the `Object` node from stub to real (label, description, sharing model, `is_custom_metadata_type`) |
| `metadata_indexer` | `permissionsets/X.permissionset-meta.xml` | `PermissionSet` node + `GRANTS` edges to Apex classes (enabled `classAccesses`), Fields (`fieldPermissions` with read or edit), and Objects (`objectPermissions` with any permission) |
| `metadata_indexer` | `layouts/Object-Layout Name.layout-meta.xml` | `Layout` node + `REFERENCES` edges to the Object and every Field on it |
| `aura_apex_resolver` | Aura `.cmp`/`.app` `controller` attribute + sibling `.js` `component.get("c.method")` calls | `AuraComponent` node + `INVOKES` edge to the resolved Apex method |

Aura wires to Apex completely differently from LWC: the bundle's root tag
names exactly one Apex class as `controller`, and the JS controller/helper
files dispatch server actions by *string* name
(`component.get("c.methodName")`) rather than an ES6 import — so it needed
its own resolver, not an extension of `lwc_apex_resolver`.

`Object` nodes are stubs (name only) unless/until real `.object-meta.xml` is
found — this deliberately still covers objects with no local object
metadata at all, which is the normal case when extending a managed
package's object (adding a field or a flow that queries it) without
redeclaring metadata that would drift from the installed package version.
When a stub is later backed by real metadata (or the reverse — the file
disappears), the node's identity is unchanged, so existing `BELONGS_TO`/
`REFERENCES` edges to it never dangle either way.

Custom Metadata Type *definitions* (`objects/X__mdt/X__mdt.object-meta.xml`)
use the exact same format as regular objects, so they're indexed by the
same path, flagged `is_custom_metadata_type` in `extra`. CMT *records*
(the configuration data under `customMetadata/`) are data, not code
structure, and aren't indexed.

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
- No Profiles yet (Permission Sets and Layouts are covered)
- Layout field references to standard fields (`Name`, `OwnerId`, ...) are
  unresolved, not linked — same limitation as formula field references,
  since standard fields are never indexed
- No CMT *records* (only type definitions — see above)
- LWC resolution covers the standard `import x from '@salesforce/apex/...'`
  / `'@salesforce/schema/...'` / `'@salesforce/label/...'` default-import
  form only (the only form Salesforce's own framework allows for these
  module families)
- Aura resolution requires a literal `controller="ClassName"` attribute on
  the `.cmp`/`.app` root tag and a literal `component.get("c.method")`
  string in a sibling `.js` file — dynamically constructed action names
  (built from a variable rather than a string literal) aren't resolved
- `Object` nodes are stubs (name only) until real `.object-meta.xml`
  parsing is added
- No cross-org / deployed metadata
- No runtime Apex semantics (governor limits, sharing, etc.)

See also [CUSTOM_LANGUAGES.md](CUSTOM_LANGUAGES.md).
