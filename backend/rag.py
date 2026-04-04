"""
rag.py — Core RAG pipeline

Provides:
  - embed_query()     → embed user query with Gemini
  - retrieve()        → fetch top-k chunks from Pinecone
  - build_context()   → format retrieved chunks into LLM context
  - call_llm()        → call Llama 4 Scout via OpenRouter
  - answer()          → full pipeline: query → retrieve → LLM → response
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types
from openai import OpenAI
from pinecone import Pinecone

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
EMBED_MODEL = "gemini-embedding-2-preview"
EMBED_DIM = 1536
PINECONE_INDEX = "tuktuk-kb"
LLM_MODEL_DEFAULT = "meta-llama/llama-4-scout"
TOP_K = 5
MAX_HISTORY_TURNS = 10
SYSTEM_PROMPT_PATH = Path(__file__).parent / "config" / "system_prompt.md"
MODEL_PATH = Path(__file__).parent / "config" / "model.txt"


def load_model() -> str:
    """Read the active LLM model from config, falling back to default."""
    if MODEL_PATH.exists():
        m = MODEL_PATH.read_text(encoding="utf-8").strip()
        if m:
            return m
    return LLM_MODEL_DEFAULT


# ---------------------------------------------------------------------------
# Lazy-init clients (avoid re-creating on every request)
# ---------------------------------------------------------------------------
_gemini_client: genai.Client | None = None
_pinecone_index: Any | None = None
_openrouter_client: OpenAI | None = None


def _get_gemini() -> genai.Client:
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
    return _gemini_client


def _get_pinecone_index():
    global _pinecone_index
    if _pinecone_index is None:
        pc = Pinecone(api_key=os.getenv("Pinecone_API_KEY"))
        _pinecone_index = pc.Index(PINECONE_INDEX)
    return _pinecone_index


def _get_openrouter() -> OpenAI:
    global _openrouter_client
    if _openrouter_client is None:
        _openrouter_client = OpenAI(
            api_key=os.getenv("OpenRouter_API_KEY"),
            base_url="https://openrouter.ai/api/v1",
            timeout=120.0,
        )
    return _openrouter_client


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------

def embed_query(query: str) -> list[float]:
    """Embed the user query for retrieval."""
    client = _get_gemini()
    result = client.models.embed_content(
        model=EMBED_MODEL,
        contents=query,
        config=genai_types.EmbedContentConfig(
            task_type="RETRIEVAL_QUERY",
            output_dimensionality=EMBED_DIM,
        ),
    )
    return result.embeddings[0].values


def retrieve(query_vector: list[float], top_k: int = TOP_K) -> list[dict]:
    """Query Pinecone and return the top-k chunks with metadata."""
    index = _get_pinecone_index()
    response = index.query(
        vector=query_vector,
        top_k=top_k,
        include_metadata=True,
    )
    results = []
    for match in response.matches:
        meta = match.metadata or {}
        # Collect images from metadata keys image_0, image_1, image_2
        images = []
        for j in range(10):
            img = meta.get(f"image_{j}")
            if img:
                images.append(img)
        results.append({
            "score": match.score,
            "text": meta.get("text", ""),
            "section_title": meta.get("section_title", ""),
            "images": images,
        })
    return results


def build_context(chunks: list[dict]) -> tuple[str, list[str]]:
    """
    Format retrieved chunks into a text context string and collect all images.

    Returns:
        (context_text, all_images_base64)
    """
    context_parts = []
    all_images: list[str] = []

    for i, chunk in enumerate(chunks, 1):
        section = chunk.get("section_title", "")
        text = chunk.get("text", "")
        header = f"[Source {i}]{f' — {section}' if section else ''}"
        context_parts.append(f"{header}\n{text}")
        all_images.extend(chunk.get("images", []))

    context_text = "\n\n---\n\n".join(context_parts)
    return context_text, all_images


def load_system_prompt() -> str:
    """Read the current system prompt from disk."""
    if SYSTEM_PROMPT_PATH.exists():
        return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
    return "You are a helpful assistant for I Took a Tuk Tuk, a tuk-tuk tour company in Lisbon."


def call_llm(
    system_prompt: str,
    context: str,
    images: list[str],
    user_query: str,
    history: list[dict] | None = None,
) -> str:
    """
    Call Llama 4 Scout via OpenRouter.

    - system_prompt: base instructions
    - context: retrieved KB chunks formatted as text
    - images: list of base64 data URIs from retrieved chunks
    - user_query: current user message
    - history: list of {role, content} dicts (last N turns)
    """
    client = _get_openrouter()

    full_system = (
        f"{system_prompt}\n\n"
        "--- KNOWLEDGE BASE CONTEXT ---\n"
        f"{context}\n"
        "--- END CONTEXT ---\n\n"
        "Answer the user's question based on the context above. "
        "If the context contains relevant images, refer to them in your answer."
    )

    messages = [{"role": "system", "content": full_system}]

    # Add conversation history
    if history:
        messages.extend(history[-(MAX_HISTORY_TURNS * 2):])

    # Build the user message — include images if any
    if images:
        # Llama 4 Scout is multimodal; send images as vision content blocks
        user_content: list[dict] = [{"type": "text", "text": user_query}]
        for img_b64 in images[:3]:  # cap at 3 images per request
            user_content.append({
                "type": "image_url",
                "image_url": {"url": img_b64},
            })
        messages.append({"role": "user", "content": user_content})
    else:
        messages.append({"role": "user", "content": user_query})

    response = client.chat.completions.create(
        model=load_model(),
        messages=messages,
        temperature=0.4,
        max_tokens=1024,
    )

    return response.choices[0].message.content


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------

import json as _json
from datetime import date as _date

_PRICING_PATH = Path(__file__).parent / "config" / "pricing.json"
_pricing_data: dict | None = None


def _load_pricing() -> dict:
    global _pricing_data
    if _pricing_data is None:
        with open(_PRICING_PATH, encoding="utf-8") as f:
            _pricing_data = _json.load(f)
    return _pricing_data


def _current_season(tour_slug: str) -> str:
    """Determine active season key for a tour based on today's date."""
    today = _date.today()
    month = today.month
    pricing = _load_pricing()["tours"].get(tour_slug, {}).get("pricing", {})

    if "high" in pricing and month in (6, 7, 8, 9, 10):
        return "high"
    if "winter" in pricing and month in (11, 12, 1, 2):
        return "winter"
    return "standard"


def _price_for(tour_slug: str, pax: int, season: str | None = None) -> float | None:
    """Return total booking price for pax passengers on a tour."""
    pricing = _load_pricing()["tours"].get(tour_slug, {}).get("pricing", {})
    if not season:
        season = _current_season(tour_slug)
    tier = pricing.get(season) or pricing.get("standard", {})
    # Find exact match or closest lower key
    key = str(pax)
    if key in tier:
        return tier[key]
    # Fall back to highest available if pax exceeds table
    available = sorted(int(k) for k in tier)
    if pax > max(available):
        return tier[str(max(available))]
    return None


def _build_pricing_context() -> str:
    """Format all tour pricing as a context block for the LLM."""
    data = _load_pricing()
    today = _date.today()
    month = today.month
    lines = [
        "--- TOUR PRICING (total booking price in EUR, not per person) ---",
        f"Today: {today.isoformat()}  |  Current month: {month}",
        "",
        "IMPORTANT RULES:",
        "- Prices are TOTAL per booking (for the whole group), not per person.",
        "- Max 6 passengers per tuk-tuk booking. Max 8 passengers per van booking.",
        "- For groups larger than one vehicle: recommend MULTIPLE bookings and calculate",
        "  the total by splitting the group to minimise cost.",
        "  Example: 10 pax on 4hr tour = one booking for 6 (€304.50) + one for 4 (€284.20) = €588.70 total.",
        "  Example: 7 pax on 4hr tour = one booking for 4 (€284.20) + one for 3 (€274.05) = €558.25 total.",
        "- Fátima tour does not operate November–February.",
        "",
    ]

    for slug, tour in data["tours"].items():
        season = _current_season(slug)
        pricing = tour["pricing"]
        tier = pricing.get(season) or pricing.get("standard", {})
        season_note = ""
        if season == "winter":
            season_note = " [WINTER PRICES — Nov–Feb]"
        elif season == "high":
            season_note = " [HIGH SEASON — Jun–Oct]"

        pax_prices = " | ".join(
            f"{k} pax: €{v:.2f}" for k, v in sorted(tier.items(), key=lambda x: int(x[0]))
        )
        lines.append(f"• {tour['name']}{season_note}")
        lines.append(f"  {pax_prices}")
        lines.append(f"  Book: [Book here]({tour['booking_url']})")
        lines.append("")

    lines.append("--- END PRICING ---")
    return "\n".join(lines)


_PRICING_KEYWORDS = {
    "price", "prices", "pricing", "cost", "costs", "how much", "rate", "rates",
    "fee", "fees", "cheap", "expensive", "afford", "euro", "eur", "€",
    "charge", "charges", "pay", "paying", "paid", "person", "people", "group",
    "passenger", "passengers", "pax",
}
_PRODUCT_KEYWORDS = {
    "tour", "tours", "trip", "trips", "experience", "experiences",
    "book", "booking", "reserve", "offer", "option", "options",
}


def _needs_pricing(query: str) -> bool:
    q = query.lower()
    words = set(re.findall(r"\w+", q))
    return bool(_PRICING_KEYWORDS & words) or bool(_PRODUCT_KEYWORDS & words)


def answer(
    query: str,
    history: list[dict] | None = None,
) -> dict:
    """
    End-to-end RAG pipeline.

    Returns:
        {
            "answer": str,
            "images": [base64_data_uri, ...],
            "sources": [{"section_title": str, "score": float}, ...]
        }
    """
    query_vec = embed_query(query)
    chunks = retrieve(query_vec)
    context, images = build_context(chunks)
    system_prompt = load_system_prompt()

    # Inject pricing context when query is pricing/product related
    if _needs_pricing(query):
        pricing_context = _build_pricing_context()
        if pricing_context:
            context = pricing_context + "\n\n" + context

    # Inject pickup zone check when query mentions a location/hotel
    try:
        from backend.pickup_zone import needs_location_check, extract_location, check_zone, build_pickup_context
    except ImportError:
        from pickup_zone import needs_location_check, extract_location, check_zone, build_pickup_context  # type: ignore
    if needs_location_check(query):
        location = extract_location(query)
        if location:
            zone_result = check_zone(location)
            pickup_ctx = build_pickup_context(location, zone_result["status"], zone_result["resolved_address"])
            context = pickup_ctx + "\n\n---\n\n" + context

    llm_answer = call_llm(
        system_prompt=system_prompt,
        context=context,
        images=images,
        user_query=query,
        history=history,
    )

    sources = [
        {"section_title": c.get("section_title", ""), "score": round(c.get("score", 0), 3)}
        for c in chunks
    ]

    return {
        "answer": llm_answer,
        "images": images,
        "sources": sources,
    }
