"""The agent workflow: a LangGraph graph from instruction to stored results.

    state.py       the typed state every node reads and updates
    routing.py     the conditional edges, as pure functions of the state
    nodes.py       the fifteen nodes (and the clarification wait)
    graph.py       how they are wired together
    checkpoint.py  where LangGraph's checkpoints are kept (PostgreSQL)
    runner.py      running, resuming and recovering a workflow in the worker
    deps.py        what nodes need from the outside world
    discovery.py   read-only look at the application through Playwright MCP
    retry.py       which failed cases may be run again
    report.py      the final report, built from stored facts

The shape of the graph is fixed. A model is asked for structured content at
four points (requirement analysis, test cases, scripts, failure explanations);
it never chooses the next node, never calls a tool, and never decides whether a
test passed.
"""
