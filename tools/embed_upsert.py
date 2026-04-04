"""
embed_upsert.py — Step 2 of the RAG pipeline

Reads .tmp/kb_chunks.json, embeds each chunk using gemini-embedding-2-preview,
and upserts the vectors (with metadata) into a Pinecone index.

Usage:
    python tools/embed_upsert.py [--fresh]   # --fresh deletes all existing vectors first
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types
from google.cloud import storage as gcs
from pinecone import Pinecone, ServerlessSpec

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
load_dotenv()

CHUNKS_PATH = Path(__file__).parent.parent / ".tmp" / "kb_chunks.json"
EMBED_MODEL = "gemini-embedding-2-preview"  # latest multimodal embedding model
EMBED_DIM = 1536
PINECONE_INDEX = "tuktuk-kb"
GCS_BUCKET = "tuktuk-notes-attachments"
BATCH_SIZE = 50  # Pinecone upsert batch size


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_embedding(client: genai.Client, text: str) -> list[float]:
    """Embed a single text chunk using Gemini."""
    result = client.models.embed_content(
        model=EMBED_MODEL,
        contents=text,
        config=genai_types.EmbedContentConfig(
            task_type="RETRIEVAL_DOCUMENT",
            output_dimensionality=EMBED_DIM,
        ),
    )
    return result.embeddings[0].values


def truncate_metadata_images(images: list[str], max_images: int = 1, max_b64_bytes: int = 24_000) -> list[str]:
    """
    Pinecone metadata limit is 40KB per vector (text + all fields combined).
    Keep at most max_images images, each under max_b64_bytes.
    Never truncate base64 strings — a truncated image is a broken image.
    """
    result = []
    for img in images[:max_images]:
        if img and len(img.encode("utf-8")) <= max_b64_bytes:
            result.append(img)
        elif img:
            print(f"  Dropping oversized image ({len(img)//1024}KB) from metadata")
    return result


def upload_image_to_gcs(gcs_client: gcs.Client, image_entry: dict, object_name: str) -> str:
    """
    Upload an image to GCS and return its public URL.
    image_entry: {"data": "data:image/jpeg;base64,...", "caption": str}
    """
    b64_data = image_entry["data"].split(",", 1)[-1]
    raw_bytes = base64.b64decode(b64_data)
    bucket = gcs_client.bucket(GCS_BUCKET)
    blob = bucket.blob(object_name)
    blob.upload_from_string(raw_bytes, content_type="image/jpeg")
    return f"https://storage.googleapis.com/{GCS_BUCKET}/{object_name}"


def ensure_index(pc: Pinecone) -> object:
    """Create the Pinecone index if it doesn't exist, then return it."""
    existing = [idx.name for idx in pc.list_indexes()]
    if PINECONE_INDEX not in existing:
        print(f"Creating Pinecone index '{PINECONE_INDEX}' (dim={EMBED_DIM}, cosine)...")
        pc.create_index(
            name=PINECONE_INDEX,
            dimension=EMBED_DIM,
            metric="cosine",
            spec=ServerlessSpec(cloud="aws", region="us-east-1"),
        )
        # Wait for the index to be ready
        while not pc.describe_index(PINECONE_INDEX).status["ready"]:
            print("  Waiting for index to be ready...")
            time.sleep(3)
        print("  Index ready.")
    else:
        print(f"Using existing Pinecone index '{PINECONE_INDEX}'")
    return pc.Index(PINECONE_INDEX)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(fresh: bool = False):
    # Load chunks
    if not CHUNKS_PATH.exists():
        print(f"ERROR: {CHUNKS_PATH} not found. Run tools/fetch_kb.py first.")
        sys.exit(1)

    with open(CHUNKS_PATH, encoding="utf-8") as f:
        chunks = json.load(f)
    print(f"Loaded {len(chunks)} chunks from {CHUNKS_PATH}")

    # Init clients
    google_api_key = os.getenv("GOOGLE_API_KEY")
    pinecone_api_key = os.getenv("Pinecone_API_KEY")
    if not google_api_key or not pinecone_api_key:
        print("ERROR: GOOGLE_API_KEY and Pinecone_API_KEY must be set in .env")
        sys.exit(1)

    gemini_client = genai.Client(api_key=google_api_key)
    pc = Pinecone(api_key=pinecone_api_key)
    index = ensure_index(pc)
    gcs_client = gcs.Client()

    # Optionally wipe existing vectors
    if fresh:
        print("Deleting all existing vectors (--fresh mode)...")
        try:
            index.delete(delete_all=True)
            time.sleep(2)
        except Exception as e:
            # Index may be empty (no namespace yet) — safe to ignore
            print(f"  (delete skipped: {e})")

    # Embed and upsert in batches
    vectors = []
    errors = 0

    for i, chunk in enumerate(chunks):
        text = chunk.get("text", "").strip()
        raw_images = chunk.get("images", [])  # list of {"data": b64, "caption": str} or legacy str

        # Normalise to list of dicts
        image_entries: list[dict] = []
        for img in raw_images:
            if isinstance(img, str):
                image_entries.append({"data": img, "caption": chunk.get("section_title", "")})
            elif isinstance(img, dict):
                image_entries.append(img)

        # For image-only chunks, embed using the image caption(s)
        if not text:
            if not image_entries:
                continue  # truly empty, skip
            captions = [e["caption"] for e in image_entries if e.get("caption")]
            text = captions[0] if captions else chunk.get("section_title", "image from knowledge base") or "image from knowledge base"
            print(f"  Image-only chunk {i} — embedding with caption: '{text[:80]}'")

        try:
            embedding = get_embedding(gemini_client, text)
        except Exception as e:
            print(f"  ERROR embedding chunk {i}: {e}")
            errors += 1
            time.sleep(2)
            continue

        # Upload images to GCS, collect public URLs
        gcs_urls: list[str] = []
        for j, entry in enumerate(image_entries):
            object_name = f"kb_chunk{chunk.get('chunk_index', i)}_img{j}.jpg"
            try:
                url = upload_image_to_gcs(gcs_client, entry, object_name)
                gcs_urls.append(url)
                print(f"  Uploaded image → {url}")
            except Exception as e:
                print(f"  WARNING: GCS upload failed for chunk {i} image {j}: {e}")

        metadata = {
            "text": text[:3000],
            "section_title": chunk.get("section_title", ""),
            "chunk_index": chunk.get("chunk_index", i),
            "has_images": len(gcs_urls) > 0,
            "image_count": len(gcs_urls),
        }
        for j, url in enumerate(gcs_urls):
            metadata[f"image_{j}"] = url

        vectors.append({
            "id": f"chunk_{chunk.get('chunk_index', i)}",
            "values": embedding,
            "metadata": metadata,
        })

        # Upsert when batch is full
        if len(vectors) >= BATCH_SIZE:
            index.upsert(vectors=vectors)
            print(f"  Upserted batch up to chunk {i}")
            vectors.clear()
            time.sleep(0.5)  # small pause between batches

    # Upsert remaining
    if vectors:
        index.upsert(vectors=vectors)
        print(f"  Upserted final batch ({len(vectors)} vectors)")

    stats = index.describe_index_stats()
    print(f"\nDone. Index stats: {stats}")
    if errors:
        print(f"WARNING: {errors} chunks failed to embed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fresh", action="store_true", help="Delete all existing vectors before upserting")
    args = parser.parse_args()
    main(fresh=args.fresh)
