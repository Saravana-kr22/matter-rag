# tools/

Standalone utilities for inspecting, verifying, and extending the Matter RAG pipeline.
These scripts do **not** modify the pipeline's data stores — they are read-only analysis tools.

---

## kg_llm_transformer.py

Build a knowledge graph from Matter spec / test-plan documents using
**LangChain's `LLMGraphTransformer` + a local Ollama LLM**, then optionally
diff the result against the pipeline's rule-based knowledge graph.

### Why this tool?

The pipeline builds its KG deterministically from:
- Structured DM XML (CLUSTER/ATTRIBUTE/COMMAND/EVENT/FEATURE nodes)
- Regex + section-heading heuristics (REQUIREMENT/BEHAVIOR_RULE/TEST_CASE nodes)

This tool uses an LLM to extract the same ontology from raw text, then compares
the two graphs. The diff shows:
- Entities the LLM found that the rules missed
- Entities the rules found that the LLM missed (or hallucinated)

### Setup (one-time)

```bash
# Install extra dependencies (not needed for the main pipeline)
pip install langchain-experimental langchain-ollama

# If langchain-ollama is unavailable on your Python version:
pip install langchain-community

# Make sure Ollama is running locally with at least one model pulled
ollama serve          # in a separate terminal if not already running
ollama pull llama3.2  # or: ollama pull mistral, ollama pull llama3.1
```

### Usage

```bash
# Quick test — 20 chunks from a single HTML file
python tools/kg_llm_transformer.py \
    --source data/input_doc/appclusters_diff.html \
    --limit 20

# Full spec folder (takes time — ~1 min per 10 chunks on llama3.2)
python tools/kg_llm_transformer.py \
    --source data/matter_spec/

# Use a different Ollama model
python tools/kg_llm_transformer.py \
    --source data/matter_spec/ \
    --model mistral

# Extract + diff against the pipeline KG
python tools/kg_llm_transformer.py \
    --source data/matter_spec/ \
    --compare data/knowledge_graph/matter_kg.json \
    --output tools/llm_kg_output.json

# Limit chunks and write to a custom output path
python tools/kg_llm_transformer.py \
    --source data/input_doc/appclusters_diff.html \
    --model llama3.1 \
    --limit 50 \
    --output tools/llm_kg_avstream.json \
    --compare data/knowledge_graph/matter_kg.json
```

### CLI Options

| Option | Default | Description |
|---|---|---|
| `--source PATH` | (required) | Directory or single file (HTML / adoc / txt) |
| `--model NAME` | `llama3.2` | Ollama model name |
| `--ollama-url URL` | `http://localhost:11434` | Ollama server URL |
| `--output PATH` | `tools/llm_kg_output.json` | Path for extracted KG JSON |
| `--compare PATH` | — | Pipeline KG JSON to diff against |
| `--limit N` | — | Max text chunks to process (useful for quick tests) |

### Output

**`tools/llm_kg_output.json`** — extracted KG in this schema:
```json
{
  "node_count": 142,
  "edge_count": 89,
  "nodes": [
    {
      "id": "on_off_cluster",
      "type": "Cluster",
      "label": "On/Off Cluster",
      "properties": { "description": "..." },
      "source_files": ["appclusters_diff.html"]
    }
  ],
  "edges": [
    { "source": "onoff", "target": "on_off_cluster", "type": "BELONGS_TO" }
  ]
}
```

**Comparison report** (printed to stdout when `--compare` is given):
```
========================================================================
  KG CROSS-VERIFICATION REPORT
  Pipeline KG : data/knowledge_graph/matter_kg.json
========================================================================

Node counts
  Pipeline (rule-based) :   4821
  LLM-extracted         :    142

Node type distribution
  Type                   Pipeline        LLM
  ---------------------- ---------- ----------
  Cluster                       124         38
  Attribute                    2103         51
  ...

Nodes found by LLM but NOT in pipeline KG  (12 total)
  BehaviorRule: 7
  Requirement: 5
  Examples (up to 15):
    [BehaviorRule  ] Upon receipt of StartAVStream command
    ...

Nodes in pipeline KG but NOT found by LLM  (4701 total)
  ...
```

### Allowed Matter Ontology

The tool constrains `LLMGraphTransformer` to the same node and relationship
types as the pipeline KG so the comparison is meaningful:

**Node types:** `Cluster`, `Attribute`, `Command`, `Event`, `Feature`, `Requirement`, `BehaviorRule`, `TestCase`

**Relationship types:** `BELONGS_TO`, `COVERS`, `TESTS`, `IMPLEMENTS`, `VALIDATES`, `REFERENCES`, `RELATED_TO`, `IMPACTS`

### Performance Tips

- `--limit 20` is fast (~2 min) and good for verifying the setup works.
- Larger models (llama3.1:70b, mixtral) produce better entity extraction
  but are significantly slower.
- Run on a focused sub-directory (e.g. a single cluster's spec) rather than
  the full spec for targeted cross-checks.
- The extracted JSON is cached in `--output` — re-run `--compare` against it
  without re-extracting by loading the JSON manually.
