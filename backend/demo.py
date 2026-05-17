import asyncio
from uuid import uuid4

from app.agents.librarian import Librarian, LibrarianInput


async def demo_librarian():
    print("Initializing Librarian Agent...")
    librarian = Librarian()

    input_data = LibrarianInput(seed_query="multi-agent llm architectures", project_id=uuid4())

    print(f"Running search for: '{input_data.seed_query}'")
    result = await librarian.run(input_data)

    print("\n" + "=" * 50)
    print("LIBRARIAN SEARCH RESULTS")
    print("=" * 50)
    print(f"Expanded Queries: {result.expanded_queries}")
    print(f"Total Unique Candidates Found: {len(result.candidates)}")
    print("-" * 50)

    for i, p in enumerate(result.candidates[:5]):
        print(f"{i + 1}. [{p.citation_key}] {p.title}")
        print(f"   Source: {p.source} | Year: {p.year}")
        if p.external_id:
            print(f"   External ID: {p.external_id}")
        print()


asyncio.run(demo_librarian())
