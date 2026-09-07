# Pipeline DAG

This document maps the current **streaming, chunk-first** runtime DAG to the
commands, modules, artifacts, and delivery tasks in the repository. The
project intentionally does not maintain a legacy full-snapshot compatibility
path.

```mermaid
flowchart TD
    CLI["cli.py run"] --> ORCH["orchestrator.py"]
    ORCH --> SCAN["scripts/scan.py<br/>scan_tree_to_chunks()"]
    SCAN --> MANIFEST["chunk manifest"]
    MANIFEST --> CHUNKS["bounded tree chunks"]
    CHUNKS --> INDEX["global compact indexes"]
    CHUNKS --> CLASSIFY["agent/classify.py<br/>classify_manifest()"]
    INDEX --> CLASSIFY
    CLASSIFY --> CL["per-chunk classifications"]
    CL --> RENDER["scripts/render_proposal.py<br/>render_manifest_proposal()"]
    RENDER --> PROP["one reconciled proposal"]
    PROP --> APPROVE{"scripts/approve.py<br/>approval decision"}
    APPROVE -- "reject + feedback" --> REVISE["agent/revise.py"]
    REVISE --> CL
    APPROVE -- approve --> EXEC["scripts/execute.py<br/>execute_proposal()"]
    EXEC --> LOG["execution_log.json + undo.py<br/>(chunk provenance)"]
    LOG --> SUMMARY["scripts/summarize.py"]
    SUMMARY --> OUT["summary.md + run_manifest.json"]
```

## Runtime stage mapping

| Stage | Repository implementation | Primary artifact |
| --- | --- | --- |
| T1 | `scripts/scan.py` | `chunks/manifest.json` + bounded chunk snapshots |
| T2 | `agent/classify.py` | per-chunk `classification.vN.json` |
| T3 | `scripts/render_proposal.py` | one globally reconciled `proposal.vN.json` |
| T4 | `scripts/approve.py` | `approval_decision.vN.json` |
| T5 | `agent/revise.py` | next `classification.vN.json` |
| T6 | `scripts/execute.py` | `execution_log.json`, `undo.py` with chunk IDs |
| T7 | `scripts/summarize.py` | global summary + per-chunk status counts |

`orchestrator.py` sequences these stages and enforces schema validation and the
approval gate. A rejection starts a new classification/proposal iteration; it
does not overwrite earlier artifacts. Execution is only reachable after an
approval decision for the current proposal.

## Consolidated delivery DAG

```mermaid
flowchart TD
    CONTRACT["A. Streaming contracts<br/>(done)"]
    SCAN["B. DFS scan + chunk writer<br/>(done)"]
    STATE["C. Manifest + resumable state<br/>(done)"]
    INDEX["D. Global indexes + chunk classification<br/>(done)"]
    RECON["E. Global reconciliation<br/>(done)"]
    GATE["F. Approval-bound execution<br/>(done)"]
    REPORT["G. Summary, docs, and validation<br/>(done)"]
    BENCH["H. Production-scale benchmark<br/>(blocked: representative tree unavailable)"]

    CONTRACT --> SCAN --> STATE --> INDEX --> RECON --> GATE --> REPORT
    REPORT -. optional evidence .-> BENCH
```

### Task definitions

| ID | Deliverable | Depends on | Status |
| --- | --- | --- | --- |
| A | Define chunk, scope, provenance, and availability contracts | — | Done |
| B | Scan depth-first into bounded immutable chunks | A | Done |
| C | Persist manifest state and support restart/resume | B | Done |
| D | Build compact global indexes and classify chunks, including bounded parallelism | B, C | Done |
| E | Reconcile all chunk operations into one conflict-checked proposal | D | Done |
| F | Bind approval to proposal hash; execute safely with centralized undo and chunk provenance | E | Done |
| G | Aggregate summaries, document operation, and run repository validation | F | Done |
| H | Benchmark against a representative production-scale tree | G | Blocked by environment |

Compatibility/migration is deliberately **not** a dependency of delivery. A
full-snapshot compatibility mode would be a separate future project, not a
requirement for this single-project chunk-first pipeline.
