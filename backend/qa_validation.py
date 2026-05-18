"""QA Validation Check for Phase 1 (Librarian)."""

import sqlite3
import sys
import time

import httpx

# Extract IDs from arguments with bounds checking
if len(sys.argv) < 2:
    print("Usage: python qa_validation.py <project_id>")
    sys.exit(1)
PROJECT_ID = sys.argv[1]

API_BASE = "http://127.0.0.1:8000/api/v1"
HEADERS = {"Authorization": "Bearer dev-user-1"}

print(f"=== Beginning Validation for Project: {PROJECT_ID} ===")

# 1. Wait for Workflow to hit 'awaiting_approval'
print("Waiting for workflow to complete Phase 1...")
for _ in range(60):
    r = httpx.get(f"{API_BASE}/projects/{PROJECT_ID}/workflow", headers=HEADERS)
    if r.status_code == 200:
        state = r.json().get("state")
        if state == "awaiting_approval":
            break
    time.sleep(2)
else:
    print("ERROR: Workflow never reached 'awaiting_approval' state.")
    sys.exit(1)

# 2. Database validation check (Workflow state)
conn = sqlite3.connect("local_dev.db")
c = conn.cursor()
c.execute("SELECT state FROM workflow_runs WHERE project_id = ?", (PROJECT_ID.replace("-", ""),))
wf_state = c.fetchone()[0]
conn.close()

# 3. Retrieve candidates from Graph Checkpoint via our new API endpoint
r = httpx.get(f"{API_BASE}/projects/{PROJECT_ID}/workflow/candidates", headers=HEADERS)
if r.status_code != 200:
    print(f"ERROR: Failed to retrieve candidates. {r.status_code}: {r.text}")
    sys.exit(1)

candidates = r.json()

# 4. Perform Validations
total_papers = len(candidates)
years = [p.get("year") for p in candidates]
external_ids = [p.get("external_id") for p in candidates]
citation_keys = [p.get("citation_key") for p in candidates]

# Criteria:
# - The returned *ArXiv* papers have a publication year >= 2021 (confirming our 5-year age filter).
old_arxiv_papers = [
    p.get("year")
    for p in candidates
    if p.get("source") == "arxiv" and p.get("year") is not None and int(p.get("year")) < 2021
]

# - The list contains zero duplicate entries.
duplicates_eids = len(external_ids) - len(set(external_ids))

# - Every entry has a generated BibTeX citation key.
missing_keys = [k for k in citation_keys if not k]

# - The workflow run state is stuck at 'awaiting_approval'.
# (Already checked above via API and DB query)

print("\n=== Validation Results ===")
print(f"Total Candidate Papers: {total_papers}")
print(f"Workflow State in Database: '{wf_state}'")
print("-" * 30)

success = True

if old_arxiv_papers:
    print(f"[FAIL] Found {len(old_arxiv_papers)} ArXiv papers older than 5 years (year < 2021).")
    success = False
else:
    print("[PASS] All ArXiv papers are within the 5-year age filter (>= 2021).")

if duplicates_eids > 0:
    print(f"[FAIL] Found {duplicates_eids} duplicate external IDs in the pool.")
    success = False
else:
    print("[PASS] Zero duplicate entries found.")

if missing_keys:
    print(f"[FAIL] {len(missing_keys)} papers are missing a BibTeX citation key.")
    success = False
else:
    print("[PASS] Every entry has a generated BibTeX citation key.")

if wf_state != "awaiting_approval":
    print(f"[FAIL] Workflow is not stuck at 'awaiting_approval', found '{wf_state}'.")
    success = False
else:
    print("[PASS] Workflow run state is successfully stuck at 'awaiting_approval'.")

# Print the citation keys to demonstrate disambiguation engine worked
print("\n=== Generated Citation Keys ===")
for p in candidates:
    title = p.get("title", "")[:60].encode("ascii", "ignore").decode("ascii")
    print(f"  {p.get('citation_key'):<25} | Year: {p.get('year')} | Title: {title}...")

if success:
    print("\n[SUCCESS] ALL PHASE 1 INTEGRATION TESTS PASSED SUCCESSFULLY!")
else:
    print("\n[WARNING] SOME TESTS FAILED.")
