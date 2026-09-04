"""Guards the "계층 설계" invariant (wiki/agent-runtime.md): `search/*` sits
below `agent/*` (agent/tools.py wraps search/service functions, never the
other way around) - `search/planner.py` needing `ChatProvider`/`Message` for
query decomposition must not import them from the `agent` package, since
`agent/__init__.py` eagerly imports `agent.runtime` -> `agent.tools` ->
`search.planner`, which is a circular import whenever `search.planner`
(or any `search/*` module) is the *first* thing to touch `knowledge_forge.agent`.

A normal pytest run doesn't catch this: some other test file typically
imports `knowledge_forge.agent.tools` first (fully populating it in
sys.modules), which happens to make a later `import knowledge_forge.search.planner`
succeed regardless of whether the cycle is actually fixed. Only a fresh
interpreter that imports `search.planner` as the very first
`knowledge_forge` touch reproduces the failure - hence the subprocess.
"""

import subprocess
import sys


def test_search_planner_imports_standalone_without_touching_agent_package():
    result = subprocess.run(
        [sys.executable, "-c", "import knowledge_forge.search.planner"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_search_router_imports_standalone():
    result = subprocess.run(
        [sys.executable, "-c", "import knowledge_forge.search.router"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_search_rerank_imports_standalone():
    result = subprocess.run(
        [sys.executable, "-c", "import knowledge_forge.search.rerank"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
