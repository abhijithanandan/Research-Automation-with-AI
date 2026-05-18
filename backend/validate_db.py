"""Database validation script for the Phase 1 live test."""

import sqlite3

PROJECT_ID = "fd36f739-1e0b-42e7-9c22-0efe2a5faaf6"

conn = sqlite3.connect("local_dev.db")
c = conn.cursor()

# Count papers
c.execute("SELECT COUNT(*) FROM papers WHERE project_id = ?", (PROJECT_ID,))
total = c.fetchone()[0]
print(f"Total papers in DB: {total}")

# Check year >= 2021 (5-year filter from 2026)
c.execute("SELECT COUNT(*) FROM papers WHERE project_id = ? AND year < 2021", (PROJECT_ID,))
old = c.fetchone()[0]
print(f"Papers with year < 2021: {old}")

# Check duplicates by external_id
c.execute(
    "SELECT external_id, COUNT(*) as cnt FROM papers WHERE project_id = ? GROUP BY external_id HAVING cnt > 1",
    (PROJECT_ID,),
)
dups = c.fetchall()
print(f"Duplicate external_ids: {len(dups)}")

# Check citation keys
c.execute(
    "SELECT COUNT(*) FROM papers WHERE project_id = ? AND (citation_key IS NULL OR citation_key = '')",
    (PROJECT_ID,),
)
no_key = c.fetchone()[0]
print(f"Papers without citation key: {no_key}")

# Check citation key uniqueness
c.execute(
    "SELECT citation_key, COUNT(*) as cnt FROM papers WHERE project_id = ? GROUP BY citation_key HAVING cnt > 1",
    (PROJECT_ID,),
)
dup_keys = c.fetchall()
print(f"Duplicate citation keys: {len(dup_keys)}")

# Show all citation keys
c.execute(
    "SELECT citation_key, year, title FROM papers WHERE project_id = ? ORDER BY citation_key",
    (PROJECT_ID,),
)
print("\n--- Citation Keys ---")
for row in c.fetchall():
    print(f"  {row[0]:25s} | {row[1]} | {row[2][:60]}")

# Check all approved = False
c.execute(
    "SELECT COUNT(*) FROM papers WHERE project_id = ? AND approved = 1",
    (PROJECT_ID,),
)
approved = c.fetchone()[0]
print(f"\nPapers with approved=True (should be 0): {approved}")

# Check workflow_runs state
c.execute("SELECT state FROM workflow_runs WHERE project_id = ?", (PROJECT_ID,))
wf = c.fetchone()
print(f"Workflow run state: {wf[0] if wf else 'NOT FOUND'}")

conn.close()
