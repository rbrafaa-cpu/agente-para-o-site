# RAG Assistant — Operating Workflow

## Objective
Serve accurate, multimodal answers about I Took a Tuk Tuk's tours using a knowledge base embedded in Pinecone, with Llama 4 Scout as the reasoning model.

## Required Inputs
- Google Doc ID: `13Vrr76mp5RpQwYdnOFOStQHIyXqeiHJOotKLbOsWL4w`
- `.env` with: `GOOGLE_API_KEY`, `Pinecone_API_KEY`, `OpenRouter_API_KEY`

## Architecture
```
Google Doc → fetch_kb.py → .tmp/kb_chunks.json
                                ↓
                         embed_upsert.py → Pinecone index: tuktuk-kb
                                                    ↓
                              User query → backend/rag.py (embed → retrieve → LLM)
                                                    ↓
                                             FastAPI /chat endpoint
                                                    ↓
                                           frontend/index.html (chat widget)
```

---

## Setup (First Time)

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Populate the knowledge base
```bash
python tools/update_kb.py
```
This fetches the Google Doc, embeds all chunks, and upserts them into Pinecone.
Expected output: confirmation that N chunks were upserted and the Pinecone index stats.

### 3. Start the backend
```bash
uvicorn backend.main:app --reload
```
Server runs on http://localhost:8000

### 4. Open the frontend
Open `frontend/index.html` in your browser (or serve it with Live Server / any static server).

---

## Day-to-Day Operations

### Updating the knowledge base
When the Google Doc is modified:
```bash
python tools/update_kb.py
```
Or use the Admin panel: http://localhost:8000/admin → click "Update Knowledge Base".

### Changing the system prompt
**Option A — Admin UI:**
Go to http://localhost:8000/admin, edit the textarea, click "Save Prompt".

**Option B — Direct file edit:**
Edit `backend/config/system_prompt.md`. Changes take effect on the next request (no restart needed).

**Option C — API:**
```bash
curl -X PUT http://localhost:8000/system-prompt \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Your new system prompt here..."}'
```

### Checking current system prompt
```bash
curl http://localhost:8000/system-prompt
```

---

## API Reference

| Endpoint | Method | Description |
|---|---|---|
| `/chat` | POST | Chat with the assistant |
| `/system-prompt` | GET | Read current system prompt |
| `/system-prompt` | PUT | Update system prompt |
| `/update-kb` | POST | Trigger KB re-embedding |
| `/admin` | GET | Admin UI |
| `/health` | GET | Health check |

### Chat request body
```json
{
  "message": "What tours do you offer?",
  "session_id": "optional-session-uuid"
}
```

### Chat response body
```json
{
  "answer": "We offer...",
  "images": ["data:image/png;base64,..."],
  "sources": [{"section_title": "Tours", "score": 0.92}],
  "session_id": "abc-123"
}
```

---

## Tools

| Script | Purpose |
|---|---|
| `tools/fetch_kb.py` | Export and parse the Google Doc into chunks |
| `tools/embed_upsert.py` | Embed chunks with Gemini → upsert to Pinecone |
| `tools/update_kb.py` | One-shot: fetch + embed + upsert (full refresh) |

---

## Models Used

| Role | Model | Provider |
|---|---|---|
| Embeddings | `gemini-embedding-2-preview` | Google Gemini |
| LLM / Reasoning | `meta-llama/llama-4-scout` | OpenRouter |
| Vector DB | Pinecone index `tuktuk-kb` (dim=1536, cosine) | Pinecone |

---

## Troubleshooting

**"Doc is private — falling back to GWS CLI..."**
The Google Doc export URL requires authentication. The GWS CLI will be invoked automatically.
Ensure the GWS CLI is authenticated: `gws auth login`

**Pinecone upsert errors**
Pinecone metadata has a 40KB limit per vector. Images are auto-truncated to fit.
If errors persist, reduce `MAX_CHUNK_CHARS` in `embed_upsert.py`.

**OpenRouter 400 / model not found**
Verify the model ID is `meta-llama/llama-4-scout` at openrouter.ai/models.

**Rate limits on Gemini embeddings**
`embed_upsert.py` includes a small sleep between batches. If hitting limits, increase `time.sleep()` delay.

---

## Embedding Space Note
`gemini-embedding-2-preview` and `gemini-embedding-001` use incompatible spaces.
If you switch models, run `python tools/update_kb.py` (which does `--fresh`) to re-embed everything.
