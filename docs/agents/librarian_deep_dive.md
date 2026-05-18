# Phase 1 (Librarian) Deep-Dive Report

This report details the execution flow, triggering payload, and internal mechanisms of the Phase 1 (Discovery) Librarian Agent within ResearchFlow AI.

---

## 1. Triggering the Agent (The Input)

To trigger the Phase 1 Agent, a `POST` request is sent to the backend with a specific "seed query" representing the user's research topic.

**The Exact Input Payload:**
```json
{
  "title": "Coronary Vessel Segmentation Survey",
  "seed_query": "coronary vessel segmentation using deep learning",
  "output_format": "markdown",
  "token_cap_usd": 5.0
}
```

* **`seed_query`**: This is the critical foundation. It is the human-readable research topic the user provides, which the agent uses to begin its intelligent discovery process.

---

## 2. What Exactly Does the Phase 1 Agent Do?

When the workflow starts, the LangGraph state machine routes execution to the **Librarian Agent**. The Librarian performs a highly orchestrated, 5-step pipeline:

### Step A: Intelligent Query Expansion (LLM)
The Librarian sends the `seed_query` to the LLM (Gemini 2.5 Flash) to generate:
1. **Alternative Search Phrases:** e.g., *"deep learning for coronary artery segmentation"*, *"neural networks in cardiac vessel segmentation"*.
2. **Academic Taxonomies:** The LLM identifies specific ArXiv categories that match the domain (e.g., `cs.CV` for Computer Vision, `cs.LG` for Machine Learning, `eess.IV` for Image and Video Processing).

### Step B: Parallel Sourcing
With its newly generated list of smart queries and taxonomy categories, the Librarian simultaneously reaches out to external academic databases.
- **ArXiv:** It constructs complex compound searches leveraging the generated taxonomies (e.g., `(all:deep learning for coronary artery...) AND (cat:cs.CV OR cat:cs.LG)`).
- **Semantic Scholar:** It concurrently searches for the same phrases to capture published journal articles and papers outside of ArXiv.

### Step C: The 5-Year Age Filter
As the XML data streams back from ArXiv, the Librarian intercepts the raw data and applies a strict constraint: **it drops any ArXiv paper older than 5 years (published before 2021).** This ensures the discovery pool is heavily biased toward state-of-the-art research.

### Step D: Deduplication & Disambiguation (The Suffix Engine)
Because the system pulls from multiple sources using multiple queries, the agent will inevitably find the same paper multiple times.
1. **Deduplication:** It checks the `external_id` (like a DOI or ArXiv ID) and strips out all duplicates, leaving only unique papers.
2. **BibTeX Key Generation:** It generates a standardized citation key for every paper (e.g., `author_last_name + year`).
3. **Collision Disambiguation:** If two different papers end up with the exact same key (e.g., John Smith published two papers in 2024), the engine automatically detects the collision and applies a suffix constraint: `smith2024`, `smith2024a`, `smith2024b`.

### Step E: Citation Velocity Ranking
Once the Librarian has a clean, unique list of papers, it calculates **Citation Velocity** to rank the papers for user review. 
* **Formula:** `log1p(citations / (age_in_years + 1))`

This ensures that a paper published 6 months ago with 10 citations ranks competitively alongside a paper published 4 years ago with 100 citations. It isolates *impactful* research rather than just *old* research.

---

## 3. The Output (The Human-in-the-Loop Gate)

Once the Librarian finishes ranking, it **does not** automatically proceed to Phase 2 (Critic / Synthesis). 

Instead, it saves the top 30 ranked candidate papers into its LangGraph Checkpoint memory and forcefully **halts the system**. It flags the database with the state: `awaiting_approval`.

At this exact moment, the agent is waiting for the human (via the frontend UI):
- The user is presented with the curated list of top candidate papers.
- The user can read abstracts, delete irrelevant ones, or manually add missing ones.
- Only when the user clicks **"Approve"** (which sends a `POST /workflow/approve` to the backend) will the system take the curated pool of papers, write them to the persistent database, and hand them off to the **Phase 2 Critic Agent**.
