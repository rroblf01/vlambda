# Workflows

Workflows are declarative JSON graphs of existing lambdas owned by the same
user.  They do not contain code: every node invokes a lambda that already
exists, so workflows are an orchestration layer rather than a new execution
surface.

## Concepts

- `next` declares trigger edges.  A trigger edge runs the target once per
  activation.
- `waits_for` and `mode: "join"` declare dependency edges.  A join node runs
  once after all of its active predecessors complete.
- A node may have outgoing triggers and incoming dependencies, but not mixed
  incoming edge kinds.
- `END` may be used as a terminal node marker in authoring tools.
- Conditions are declarative objects (`path`, `equals`, `exists`, ...), never
  `eval`.
- Data is passed automatically by default; node or edge `input` can override it
  with `$` references.
- Only lambdas owned by the workflow owner may be referenced.
- Recovery is at least once: a process restart may rerun nodes that were in
  flight.

## Node modes

- `trigger`: run once per incoming activation.
- `join`: wait for all active incoming dependencies.
- `first`: run only for the first activation.
- `any`: run for the first successful activation.

Join default completion policy is `all_success`.

## API

- `POST /workflows` create a workflow.
- `GET /workflows` list workflows.
- `GET /workflows/{id}` get a workflow.
- `PUT /workflows/{id}` update a workflow.
- `DELETE /workflows/{id}` delete a workflow.
- `POST /workflows/{id}/validate` validate a definition.
- `POST /workflows/{id}/run` run a workflow with `{"input": {...}}`.
- `GET /workflows/{id}/runs` list runs.
- `GET /workflow-runs/{id}` get a run and node states.
- `POST /workflow-runs/{id}/cancel` cancel a run.
- `POST /workflow-runs/{id}/nodes/{node_id}/retry` retry a node.

Lambdas can start a controlled workflow with:

```python
request.trigger_workflow(workflow_id, {"key": "value"})
```
