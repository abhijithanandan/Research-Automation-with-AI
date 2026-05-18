"""Validate the graph checkpoint state after the Librarian runs."""

import asyncio
import os
import sys

# Ensure the backend package is importable
sys.path.insert(0, os.path.dirname(__file__))


async def main() -> None:
    from app.services.workflow import get_compiled_graph

    # The run_id is used as thread_id
    run_id = "07effa21-8a85-4a3a-8bb0-1bdfd0db66a2"

    graph = get_compiled_graph()
    config = {"configurable": {"thread_id": run_id}}
    snapshot = graph.get_state(config)

    state = snapshot.values
    candidates = state.get("candidates", [])
    expanded_queries = state.get("expanded_queries", [])

    print("=== Graph State Validation ===")
    print(f"Next node(s): {snapshot.next}")
    print(f"Phase: {state.get('phase')}")
    print(f"Expanded queries: {expanded_queries}")
    print(f"Total candidates: {len(candidates)}")
    print()

    # Validate each paper
    years = []
    citation_keys = []
    external_ids = []
    all_approved_false = True

    for i, paper in enumerate(candidates):
        year = paper.get("year")
        key = paper.get("citation_key", "")
        eid = paper.get("external_id", "")
        approved = paper.get("approved", False)
        citation_count = paper.get("citation_count")
        title = paper.get("title", "")[:60]

        years.append(year)
        citation_keys.append(key)
        external_ids.append(eid)
        if approved:
            all_approved_false = False

        print(f"  [{i + 1:2d}] {key:25s} | {year} | cc={citation_count!s:>5s} | {title}")

    print()

    # Validation checks
    old_papers = [y for y in years if y is not None and y < 2021]
    print(f"Papers with year < 2021: {len(old_papers)}")

    dup_ids = len(external_ids) - len(set(external_ids))
    print(f"Duplicate external_ids: {dup_ids}")

    empty_keys = [k for k in citation_keys if not k]
    print(f"Papers without citation key: {len(empty_keys)}")

    dup_keys = len(citation_keys) - len(set(citation_keys))
    print(f"Duplicate citation keys: {dup_keys}")

    print(f"All approved=False: {all_approved_false}")
    print(f"Graph paused at: {snapshot.next}")


asyncio.run(main())
