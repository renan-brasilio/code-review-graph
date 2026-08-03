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

**Tips:**

- Prefer `callers_of` on the **method** node (not just the class).
- If method query returns 0 on older builds, retry on the parent class.
- Managed-package symbols (`sitetracker__`, `strk__`): still use strk-mcp in parallel.

## What gets resolved

| Resolver | Input | Output |
|----------|-------|--------|
| `apex_static_resolver` | `Class.method()` CALLS with bare class target | Qualified method target |
| `apex_trigger_resolver` | `createAndExecuteHandler(Handler.class)` in triggers | `INVOKES` edge to handler class |
| `metadata_indexer` | `*.field-meta.xml` | `Field` nodes (type, relationship, formula in `extra`) + resolved `REFERENCES` edges for formula field/relationship traversal |

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

## Limitations (v1)

- No Flow XML, CMT, permission sets, or layouts
- No cross-org / deployed metadata
- No runtime Apex semantics (governor limits, sharing, etc.)

See also [CUSTOM_LANGUAGES.md](CUSTOM_LANGUAGES.md).
