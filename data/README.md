# Data Directory

This directory holds runtime data for the pipeline. All subdirectories are gitignored — create them manually or run the pipeline (it creates them automatically on first run).

## Expected Structure

```
data/
  data_model/          ← Matter DM XML cluster files (copy from connectedhomeip/data_model/clusters/)
  test_plans/          ← Test plan HTML files (allclusters.html, index.html)
  matter_spec/         ← Spec HTML files (index.html, appclusters.html)
  input_doc/           ← PR diff HTML files to analyze
  knowledge_graph/     ← Auto-generated: KG JSON (built by --build-knowledge-graph)
  faiss_index/         ← Auto-generated: FAISS vector DB (built by --build-test-plan-vectors)
  cache/               ← Auto-generated: TC routing index
```

## Setup

```bash
mkdir -p data/{data_model,test_plans,matter_spec,input_doc}
```

Then copy your source files into the appropriate directories. See the main README for details on where to get each file type.

## Important

All contents of this directory are gitignored to prevent accidental data leaks. Never force-add files from this directory to git.
