"""
update_kb.py — One-shot Knowledge Base update runner

Run this script whenever the Google Doc is updated to refresh the Pinecone index.

Steps:
  1. Fetch Google Doc → .tmp/kb_chunks.json  (tools/fetch_kb.py)
  2. Embed chunks → upsert to Pinecone        (tools/embed_upsert.py)

Usage:
    python tools/update_kb.py
"""

import sys
import time
from pathlib import Path

# Allow importing sibling scripts
sys.path.insert(0, str(Path(__file__).parent))

import fetch_kb
import embed_upsert


def main():
    print("=" * 60)
    print("STEP 1 — Fetching and parsing knowledge base document")
    print("=" * 60)
    fetch_kb.main()

    print()
    print("=" * 60)
    print("STEP 2 — Embedding chunks and upserting to Pinecone")
    print("=" * 60)
    embed_upsert.main(fresh=True)  # always do a clean refresh

    print()
    print("=" * 60)
    print("Knowledge base update complete.")
    print("=" * 60)


if __name__ == "__main__":
    main()
