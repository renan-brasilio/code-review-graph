# Cross-Tool Persistent Agent Memory

`code-review-graph` provides cross-tool persistent memory exposed as MCP tools. Facts or preferences remembered from one MCP client (Cursor, Claude Code, etc.) are recalled identically across all connected clients.

> [!NOTE]
> This system is separate from `code_review_graph/memory.py`, which logs user Q&A interactions for re-ingestion into the structural code graph.

---

## Key Features

1. **Repo vs. Global Scope**:
   - **Repo (`scope="repo"`)**: Saved to `<repo>/.code-review-graph/memory.db`. Project-specific context, e.g. `"This project uses the repository pattern for data access"`.
   - **Global (`scope="global"`)**: Saved to `~/.code-review-graph/global_memory.db`. Cross-project preferences, e.g. `"Prefer tab indentation over 4 spaces"`.
   - **Both (`scope="both"`)**: Default recall scope — queries both databases and merges results.

2. **Opt-in Semantic Embedding**:
   - Recall is fast, free, and keyword-based by default (no model downloads or API costs).
   - Passing `embed=True` to `save_memory_tool` or `recall_memories_tool` ranks memories using semantic vector embeddings (via `sentence-transformers` or configured cloud providers).

---

## MCP Tools

### `save_memory_tool`
Save a fact or preference to memory.
```json
{
  "content": "Use ruff for linting and formatting",
  "scope": "repo",
  "category": "convention",
  "embed": false
}
```

### `recall_memories_tool`
Recall memories matching a text query or list recent memories.
```json
{
  "query": "linting",
  "scope": "both",
  "limit": 10,
  "embed": false
}
```

### `list_memories_tool`
List all saved memories in scope ordered by update time.
```json
{
  "scope": "both"
}
```

### `forget_memory_tool`
Delete a memory by its ID.
```json
{
  "memory_id": 1,
  "scope": "repo"
}
```
