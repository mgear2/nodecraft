# Pipeline DAG

This document maps the runtime DAG to the commands, modules, and artifacts
that exist in the repository.

```mermaid
flowchart TD
    CLI["cli.py run"] --> ORCH["orchestrator.py"]
    ORCH --> SCAN["scripts/scan.py<br/>build_snapshot()"]
    SCAN --> SNAP["tree_snapshot.json"]
    SNAP --> CLASSIFY["agent/classify.py<br/>classify_all()"]
    CLASSIFY --> CL["classification.vN.json"]
    CL --> RENDER["scripts/render_proposal.py<br/>render_proposal()"]
    RENDER --> PROP["proposal.vN.json"]
    PROP --> APPROVE{"scripts/approve.py<br/>approval decision"}
    APPROVE -- "reject + feedback" --> REVISE["agent/revise.py"]
    REVISE --> CL
    APPROVE -- approve --> EXEC["scripts/execute.py<br/>execute_proposal()"]
    EXEC --> LOG["execution_log.json + undo.py"]
    LOG --> SUMMARY["scripts/summarize.py"]
    SUMMARY --> OUT["summary.md + run_manifest.json"]
```

## Stage mapping

| Stage | Repository implementation | Primary artifact |
| --- | --- | --- |
| T1 | `scripts/scan.py` | `tree_snapshot.json` |
| T2 | `agent/classify.py` | `classification.vN.json` |
| T3 | `scripts/render_proposal.py` | `proposal.vN.json` |
| T4 | `scripts/approve.py` | `approval_decision.vN.json` |
| T5 | `agent/revise.py` | next `classification.vN.json` |
| T6 | `scripts/execute.py` | `execution_log.json`, `undo.py` |
| T7 | `scripts/summarize.py` | `summary.md`, `run_manifest.json` |

`orchestrator.py` sequences these stages and enforces schema validation and the
approval gate. A rejection starts a new classification/proposal iteration; it
does not overwrite earlier artifacts. Execution is only reachable after an
approval decision for the current proposal.
