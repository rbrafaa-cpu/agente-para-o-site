#!/usr/bin/env python3
"""
benchmark_models.py — Compare the selectable LLMs on the real RAG pipeline.

Retrieval runs ONCE per question (via rag.build_request), then every model
answers from that identical context. Only the LLM varies, so latency, tokens
and cost are directly comparable — and it keeps embedding/Pinecone cost down.

Cost is the actual figure OpenRouter reports in usage, not an estimate.

Usage:
    python tools/benchmark_models.py                  # all models, all questions
    python tools/benchmark_models.py --models openai/gpt-5.6-luna
    python tools/benchmark_models.py --runs 3         # average over N runs

Outputs:
    .tmp/model_benchmark.json    full results incl. every answer
    .tmp/model_benchmark.md      summary tables for reading/sharing
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

from backend import rag  # noqa: E402

# Models offered in the admin panel, in the order they appear there.
MODELS = [
    "meta-llama/llama-4-scout",
    "google/gemini-2.5-flash",
    "mistralai/mistral-small-3.1-24b-instruct",
    "google/gemini-3-flash-preview",
    "openai/gpt-5.6-luna",
]

# Representative of what the assistant actually gets asked. Each targets a
# capability the pipeline depends on, so a regression shows up as a bad answer
# rather than just a slower one.
QUESTIONS = [
    {"id": "tours_overview",   "capability": "KB retrieval",        "q": "What tours do you offer?"},
    {"id": "price_simple",     "capability": "Pricing lookup",      "q": "How much is the 4 hour tour for 2 people?"},
    {"id": "price_group",      "capability": "Multi-vehicle maths", "q": "We are 10 people, how much would the 4 hour tour cost us in total?"},
    {"id": "price_total_vs_pp","capability": "Total-vs-per-person", "q": "Is the price you quote per person or for the whole group?"},
    {"id": "pickup_zone",      "capability": "Pickup zone check",   "q": "Can you pick us up at Hotel Avenida Palace in Lisbon?"},
    {"id": "pickup_far",       "capability": "Out-of-zone pickup",  "q": "Can you pick us up in Cascais?"},
    {"id": "seasonal_fatima",  "capability": "Seasonal rule",       "q": "Is the Fatima tour available in December?"},
    {"id": "out_of_scope",     "capability": "Scope discipline",    "q": "Can you book me a flight to Paris and recommend a hotel there?"},
]


def run_one(model: str, req: dict, question: str) -> dict:
    """Send one question to one model and record timing, tokens and cost."""
    messages = rag.build_messages(
        system_prompt=req["system_prompt"],
        context=req["context"],
        images=req["images"],
        user_query=question,
        history=None,
    )
    params = rag.completion_params(model, messages)

    start = time.time()
    try:
        resp = rag._get_openrouter().chat.completions.create(**params)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "latency_s": round(time.time() - start, 2)}
    latency = time.time() - start

    u = resp.usage
    details = getattr(u, "completion_tokens_details", None)
    choice = resp.choices[0]
    answer = choice.message.content or ""

    return {
        "ok": True,
        "latency_s": round(latency, 2),
        # A truncated answer shows up here as "length" rather than "stop".
        "finish_reason": choice.finish_reason,
        "native_finish_reason": getattr(choice, "native_finish_reason", None),
        "prompt_tokens": u.prompt_tokens,
        # Cached input bills far cheaper; a warm cache changes cost/msg a lot.
        "cached_tokens": getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0,
        "completion_tokens": u.completion_tokens,
        "reasoning_tokens": getattr(details, "reasoning_tokens", 0) or 0,
        # OpenRouter reports the real charge for this call.
        "cost_usd": getattr(u, "cost", None),
        "answer_chars": len(answer),
        "answer": answer,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=MODELS, help="model ids to test")
    ap.add_argument("--runs", type=int, default=1, help="repeat each question N times and average")
    ap.add_argument("--out", default=str(PROJECT_ROOT / ".tmp"), help="output directory")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Benchmarking {len(args.models)} models x {len(QUESTIONS)} questions x {args.runs} run(s)\n")

    results: list[dict] = []
    for qi, item in enumerate(QUESTIONS, 1):
        print(f"[{qi}/{len(QUESTIONS)}] {item['id']} — retrieving context...")
        # Retrieval happens once; every model sees the identical prompt.
        req = rag.build_request(item["q"])
        ctx_note = f"    context {len(req['context'])} chars, {len(req['chunks'])} chunks, {len(req['images'])} images"
        print(ctx_note)

        for model in args.models:
            runs = [run_one(model, req, item["q"]) for _ in range(args.runs)]
            ok_runs = [r for r in runs if r["ok"]]
            if ok_runs:
                merged = dict(ok_runs[-1])
                merged["latency_s"] = round(statistics.mean(r["latency_s"] for r in ok_runs), 2)
                costs = [r["cost_usd"] for r in ok_runs if r["cost_usd"] is not None]
                merged["cost_usd"] = round(statistics.mean(costs), 8) if costs else None
                merged["runs"] = len(ok_runs)
                merged["cached_tokens"] = round(statistics.mean(r["cached_tokens"] for r in ok_runs))
                status = f"{merged['latency_s']}s  {merged['completion_tokens']}out"
                if merged["reasoning_tokens"]:
                    status += f" ({merged['reasoning_tokens']} reasoning)"
                if merged["cost_usd"] is not None:
                    status += f"  ${merged['cost_usd']:.6f}"
                if merged.get("finish_reason") not in ("stop", None):
                    status += f"  [finish={merged['finish_reason']}]"
                print(f"    {model:<45} {status}")
            else:
                merged = dict(runs[-1])
                merged["runs"] = 0
                print(f"    {model:<45} FAILED — {merged['error']}")

            merged.update({
                "model": model,
                "question_id": item["id"],
                "capability": item["capability"],
                "question": item["q"],
            })
            results.append(merged)
        print()

    json_path = out_dir / "model_benchmark.json"
    json_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    md_path = out_dir / "model_benchmark.md"
    md_path.write_text(build_report(results, args.models), encoding="utf-8")

    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")


def build_report(results: list[dict], models: list[str]) -> str:
    lines = ["# Model Benchmark", ""]

    lines += ["## Summary (averaged across all questions)", "",
              "| Model | Avg latency | Avg out tokens | Reasoning tokens | Avg cached in | Avg cost/msg | Cost per 1k msgs | Errors | Bad finish |",
              "|---|---|---|---|---|---|---|---|---|"]
    for m in models:
        rows = [r for r in results if r["model"] == m]
        ok = [r for r in rows if r["ok"]]
        fails = len(rows) - len(ok)
        if not ok:
            lines.append(f"| {m} | — | — | — | — | — | — | {fails} | — |")
            continue
        lat = statistics.mean(r["latency_s"] for r in ok)
        out = statistics.mean(r["completion_tokens"] for r in ok)
        reas = statistics.mean(r["reasoning_tokens"] for r in ok)
        costs = [r["cost_usd"] for r in ok if r["cost_usd"] is not None]
        cost = statistics.mean(costs) if costs else None
        cost_s = f"${cost:.6f}" if cost is not None else "—"
        k_s = f"${cost * 1000:.2f}" if cost is not None else "—"
        cached = statistics.mean(r["cached_tokens"] for r in ok)
        # HTTP 200 with finish_reason != "stop" means a truncated/aborted answer.
        bad = sum(1 for r in ok if r.get("finish_reason") not in ("stop", None))
        bad_s = f"**{bad}**" if bad else "0"
        lines.append(f"| {m} | {lat:.2f}s | {out:.0f} | {reas:.0f} | {cached:.0f} | {cost_s} | {k_s} | {fails} | {bad_s} |")

    lines += ["", "## Per-question latency (seconds)", ""]
    header = "| Question | Capability | " + " | ".join(m.split("/")[-1] for m in models) + " |"
    lines += [header, "|---" * (len(models) + 2) + "|"]
    for item in QUESTIONS:
        cells = []
        for m in models:
            r = next((x for x in results if x["model"] == m and x["question_id"] == item["id"]), None)
            if not (r and r["ok"]):
                cells.append("FAIL")
            elif r.get("finish_reason") not in ("stop", None):
                cells.append(f"{r['latency_s']:.2f} ⚠️")
            else:
                cells.append(f"{r['latency_s']:.2f}")
        lines.append(f"| {item['id']} | {item['capability']} | " + " | ".join(cells) + " |")

    lines += ["", "## Answers", ""]
    for item in QUESTIONS:
        lines += [f"### {item['id']} — {item['capability']}", "", f"**Q:** {item['q']}", ""]
        for m in models:
            r = next((x for x in results if x["model"] == m and x["question_id"] == item["id"]), None)
            if not r:
                continue
            lines.append(f"<details><summary><strong>{m}</strong></summary>\n")
            lines.append(r["answer"] if r["ok"] else f"FAILED — {r['error']}")
            lines.append("\n</details>\n")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
