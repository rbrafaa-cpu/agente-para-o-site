"""
main.py — FastAPI backend for the I Took a Tuk Tuk RAG Assistant

Endpoints:
  POST /chat            — main chat endpoint
  GET  /system-prompt   — read current system prompt
  PUT  /system-prompt   — update system prompt
  POST /update-kb       — trigger knowledge base re-embedding
  GET  /admin           — admin UI (HTML)
  GET  /health          — health check

Run:
    uvicorn backend.main:app --reload
    or
    uvicorn main:app --reload  (from inside backend/)
"""

from __future__ import annotations

import base64
import io
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from PIL import Image
from pydantic import BaseModel
import secrets
from apscheduler.schedulers.background import BackgroundScheduler

load_dotenv()

# ---------------------------------------------------------------------------
# App init
# ---------------------------------------------------------------------------
app = FastAPI(title="I Took a Tuk Tuk — AI Assistant API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost",
        "http://localhost:3000",
        "http://localhost:5500",
        "http://127.0.0.1:5500",
        "https://www.itookatuktuk.com",
        "https://itookatuktuk.com",
        "https://*.up.railway.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Admin auth (HTTP Basic)
# ---------------------------------------------------------------------------
_http_security = HTTPBasic()

def require_admin(credentials: HTTPBasicCredentials = Depends(_http_security)):
    ok_user = secrets.compare_digest(
        credentials.username.encode(), os.getenv("ADMIN_USER", "admin").encode()
    )
    ok_pass = secrets.compare_digest(
        credentials.password.encode(), os.getenv("ADMIN_PASSWORD", "changeme").encode()
    )
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )

# ---------------------------------------------------------------------------
# DB init (conversation logging)
# ---------------------------------------------------------------------------
try:
    from backend import db as _db
except ImportError:
    import db as _db  # type: ignore

_db.init_db()

# ---------------------------------------------------------------------------
# Gmail drafts integration (optional — requires GMAIL_* env vars)
# ---------------------------------------------------------------------------
_gmail_drafts = None
try:
    try:
        from backend import gmail_drafts as _gmail_drafts
    except ImportError:
        import gmail_drafts as _gmail_drafts  # type: ignore
except Exception:
    _gmail_drafts = None  # gracefully disabled if dependencies missing

_scheduler = BackgroundScheduler(daemon=True)


@app.on_event("startup")
async def _start_scheduler():
    if _gmail_drafts and _gmail_drafts.is_configured():
        _scheduler.add_job(
            _gmail_drafts.process_email_queries,
            "interval",
            minutes=15,
            id="email_check",
            replace_existing=True,
            misfire_grace_time=300,
        )
        _scheduler.start()
        print("[Email Scheduler] Started — checking inbox every 15 minutes")
    else:
        print("[Email Scheduler] Skipped — GMAIL_* credentials not set")


@app.on_event("shutdown")
async def _stop_scheduler():
    if _scheduler.running:
        _scheduler.shutdown(wait=False)


# ---------------------------------------------------------------------------
# In-memory session store  {session_id: [{"role": ..., "content": ...}, ...]}
# ---------------------------------------------------------------------------
sessions: dict[str, list[dict]] = {}
MAX_HISTORY = 20  # messages (10 turns)

SYSTEM_PROMPT_PATH = Path(__file__).parent / "config" / "system_prompt.md"
MODEL_PATH = Path(__file__).parent / "config" / "model.txt"
UPDATE_KB_SCRIPT = Path(__file__).parent.parent / "tools" / "update_kb.py"
PROJECT_ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    message: str
    session_id: str = ""


class ChatResponse(BaseModel):
    answer: str
    images: list[str] = []
    sources: list[dict] = []
    session_id: str


class SystemPromptPayload(BaseModel):
    prompt: str


class ModelPayload(BaseModel):
    model: str


class SaveImagePayload(BaseModel):
    caption: str
    image_b64: str = ""
    image_id: str = ""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def demo():
    base = os.getenv("WIDGET_BASE_URL", "").rstrip("/")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>I Took a Tuk Tuk — Chat Assistant</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
  <style>
    body {{ font-family: 'Inter', sans-serif; background: #f5f5f4; padding: 2rem 1rem; }}
    .wrap {{ max-width: 680px; margin: 0 auto; }}
    h1 {{ font-size: 1.5rem; font-weight: 700; color: #F97415; margin-bottom: 0.25rem; }}
    p {{ color: #78716c; font-size: 0.9rem; margin-bottom: 2rem; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>I Took a Tuk Tuk — AI Assistant</h1>
    <p>Live preview of the embeddable chat widget.</p>
    <script src="{base}/widget.js"></script>
  </div>
</body>
</html>"""


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    # Support both `uvicorn backend.main:app` (from project root)
    # and `uvicorn main:app` (from inside backend/)
    try:
        from backend import rag
    except ImportError:
        import rag  # type: ignore

    session_id = req.session_id or str(uuid.uuid4())
    history = sessions.get(session_id, [])

    try:
        result = rag.answer(query=req.message, history=history)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Update history
    history.append({"role": "user", "content": req.message})
    history.append({"role": "assistant", "content": result["answer"]})
    sessions[session_id] = history[-MAX_HISTORY:]

    # Persist conversation
    try:
        _db.log_conversation(session_id, req.message, result["answer"], result.get("sources", []))
    except Exception:
        pass  # logging failure must never break the chat response

    return ChatResponse(
        answer=result["answer"],
        images=result.get("images", []),
        sources=result.get("sources", []),
        session_id=session_id,
    )


@app.get("/system-prompt")
def get_system_prompt():
    if SYSTEM_PROMPT_PATH.exists():
        return {"prompt": SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")}
    return {"prompt": ""}


@app.get("/model")
def get_model():
    if MODEL_PATH.exists():
        return {"model": MODEL_PATH.read_text(encoding="utf-8").strip()}
    return {"model": "meta-llama/llama-4-scout"}


@app.put("/model")
def put_model(payload: ModelPayload):
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    MODEL_PATH.write_text(payload.model.strip(), encoding="utf-8")
    return {"status": "saved", "model": payload.model.strip()}


@app.put("/system-prompt")
def put_system_prompt(payload: SystemPromptPayload):
    SYSTEM_PROMPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SYSTEM_PROMPT_PATH.write_text(payload.prompt, encoding="utf-8")
    return {"status": "saved"}


@app.post("/update-kb")
def update_kb():
    """Trigger a full knowledge base re-embedding (runs update_kb.py as subprocess)."""
    if not UPDATE_KB_SCRIPT.exists():
        raise HTTPException(status_code=500, detail="update_kb.py not found")

    result = subprocess.run(
        [sys.executable, str(UPDATE_KB_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=300,
    )

    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"KB update failed:\n{result.stderr}"
        )

    return {"status": "ok", "output": result.stdout[-2000:]}  # last 2k chars


@app.post("/upload-kb")
async def upload_kb(file: UploadFile = File(...)):
    """
    Upload a new .docx knowledge base file, replace the existing one,
    then trigger a full re-embedding.
    """
    if not file.filename or not file.filename.lower().endswith(".docx"):
        raise HTTPException(status_code=400, detail="Only .docx files are accepted.")

    # Remove any existing .docx files in project root (excluding lock files)
    for old in PROJECT_ROOT.glob("*.docx"):
        if not old.name.startswith("~$"):
            old.unlink()

    # Save the uploaded file
    dest = PROJECT_ROOT / file.filename
    contents = await file.read()
    dest.write_bytes(contents)

    # Run the pipeline
    if not UPDATE_KB_SCRIPT.exists():
        raise HTTPException(status_code=500, detail="update_kb.py not found")

    result = subprocess.run(
        [sys.executable, str(UPDATE_KB_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=300,
    )

    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"KB update failed:\n{result.stderr}"
        )

    return {
        "status": "ok",
        "filename": file.filename,
        "size_kb": round(len(contents) / 1024, 1),
        "output": result.stdout[-2000:],
    }


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def compress_image_b64(raw_bytes: bytes, max_px: int = 320, quality: int = 50) -> str:
    """Resize + compress to JPEG, return as base64 data URI."""
    img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
    img.thumbnail((max_px, max_px), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/jpeg;base64,{b64}"


def _pinecone_index():
    from pinecone import Pinecone
    return Pinecone(api_key=os.getenv("Pinecone_API_KEY")).Index("tuktuk-kb")


def _gemini_client():
    from google import genai
    return genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))


# ---------------------------------------------------------------------------
# Image KB endpoints
# ---------------------------------------------------------------------------

@app.post("/caption-image")
async def caption_image(file: UploadFile = File(...)):
    """Send image to a vision model via OpenRouter and return an auto-generated caption."""
    raw = await file.read()
    content_type = file.content_type or "image/jpeg"

    # Compress first so the base64 we send is smaller
    image_b64 = compress_image_b64(raw, max_px=800, quality=72)  # higher res for captioning

    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=os.getenv("OpenRouter_API_KEY"),
            base_url="https://openrouter.ai/api/v1",
        )
        response = client.chat.completions.create(
            model="google/gemini-2.5-flash",
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": image_b64},
                    },
                    {
                        "type": "text",
                        "text": (
                            "Write a short, factual 1-sentence caption for this image for a tuk-tuk tour company in Lisbon. "
                            "State what is shown and where, nothing more. No fluff, no adjectives like 'stunning' or 'beautiful'."
                        ),
                    },
                ],
            }],
            max_tokens=80,
        )
        thumb_b64 = compress_image_b64(raw)
        return {"caption": response.choices[0].message.content.strip(), "image_b64": thumb_b64}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Caption generation failed: {e}")


@app.post("/save-image")
async def save_image(payload: SaveImagePayload):
    """Embed caption and upsert image + caption into Pinecone."""
    caption = payload.caption.strip()
    image_b64 = payload.image_b64
    image_id = payload.image_id or f"img_{uuid.uuid4().hex[:10]}"

    if not caption:
        raise HTTPException(status_code=400, detail="Caption is required.")

    try:
        from google.genai import types as gtypes
        client = _gemini_client()
        result = client.models.embed_content(
            model="gemini-embedding-2-preview",
            contents=caption,
            config=gtypes.EmbedContentConfig(
                task_type="RETRIEVAL_DOCUMENT",
                output_dimensionality=1536,
            ),
        )
        vector = result.embeddings[0].values
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Embedding failed: {e}")

    # Upload image to GCS and get public URL
    gcs_url = None
    if image_b64:
        try:
            import subprocess, tempfile
            GCS_BUCKET = "tuktuk-notes-attachments"
            b64_data = image_b64.split(",", 1)[-1]
            raw_again = base64.b64decode(b64_data)
            # Re-compress to a reasonable web size before uploading
            recompressed = compress_image_b64(raw_again, max_px=800, quality=72)
            img_bytes = base64.b64decode(recompressed.split(",", 1)[-1])
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp.write(img_bytes)
                tmp_path = tmp.name
            result = subprocess.run(
                ["gsutil", "cp", tmp_path, f"gs://{GCS_BUCKET}/{image_id}.jpg"],
                capture_output=True, text=True
            )
            os.unlink(tmp_path)
            if result.returncode != 0:
                raise RuntimeError(result.stderr)
            gcs_url = f"https://storage.googleapis.com/{GCS_BUCKET}/{image_id}.jpg"
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"GCS upload failed: {e}")

    try:
        metadata: dict = {
            "text": caption,
            "section_title": "Tour Images",
            "source": "manual_image",
            "has_images": bool(gcs_url),
            "image_count": 1 if gcs_url else 0,
        }
        if gcs_url:
            metadata["image_0"] = gcs_url

        _pinecone_index().upsert(vectors=[{
            "id": image_id,
            "values": vector,
            "metadata": metadata,
        }])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Pinecone upsert failed: {e}")

    return {"status": "saved", "id": image_id}


@app.get("/images")
def list_images():
    """Return all manually added images from Pinecone."""
    index = _pinecone_index()
    # List all IDs with img_ prefix, then fetch their metadata
    try:
        ids = [v for v in index.list(prefix="img_")]
        # list() returns an iterator of lists; flatten
        flat_ids = []
        for item in ids:
            if isinstance(item, list):
                flat_ids.extend(item)
            else:
                flat_ids.append(item)
        if not flat_ids:
            return {"images": []}
        fetched = index.fetch(ids=flat_ids)
        images = []
        for vid, vec in fetched.vectors.items():
            m = vec.metadata or {}
            images.append({
                "id": vid,
                "caption": m.get("text", ""),
                "image_b64": m.get("image_0", ""),
            })
        return {"images": images}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/image/{image_id}")
def delete_image(image_id: str):
    """Remove a manually added image from Pinecone."""
    _pinecone_index().delete(ids=[image_id])
    return {"status": "deleted", "id": image_id}


# ---------------------------------------------------------------------------
# Conversation log endpoints (admin-protected)
# ---------------------------------------------------------------------------

@app.get("/conversations")
def get_conversations(
    limit: int = 50,
    offset: int = 0,
    search: str = "",
    _: HTTPBasicCredentials = Depends(require_admin),
):
    rows = _db.get_conversations(limit=limit, offset=offset, search=search)
    total = _db.get_conversation_count(search=search)
    return {"total": total, "conversations": rows}


@app.get("/conversations/export")
def export_conversations(_: HTTPBasicCredentials = Depends(require_admin)):
    import csv, io as _io
    rows = _db.get_conversations(limit=100_000)
    buf = _io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["id", "session_id", "timestamp", "user_message", "bot_response", "sources"])
    writer.writeheader()
    writer.writerows(rows)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=conversations.csv"},
    )


# ---------------------------------------------------------------------------
# Email draft endpoints (admin-protected)
# ---------------------------------------------------------------------------

@app.post("/process-emails")
def process_emails(_: HTTPBasicCredentials = Depends(require_admin)):
    """Manually trigger email query processing and Gmail draft creation."""
    if not _gmail_drafts:
        raise HTTPException(
            status_code=503,
            detail="Gmail integration unavailable — install google-auth and google-api-python-client",
        )
    if not _gmail_drafts.is_configured():
        raise HTTPException(
            status_code=503,
            detail="Gmail not configured — set GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN",
        )
    return _gmail_drafts.process_email_queries()


@app.get("/email-drafts")
def get_email_drafts_endpoint(
    limit: int = 50,
    offset: int = 0,
    search: str = "",
    _: HTTPBasicCredentials = Depends(require_admin),
):
    rows = _db.get_email_drafts(limit=limit, offset=offset, search=search)
    total = _db.get_email_draft_count(search=search)
    return {"total": total, "drafts": rows}


# ---------------------------------------------------------------------------
# Gmail OAuth re-authentication (admin-protected)
# ---------------------------------------------------------------------------

# Short-lived state tokens: {state_token: redirect_uri}
_gmail_oauth_states: dict[str, str] = {}

_GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


@app.get("/gmail-auth-test")
def gmail_auth_test(_: HTTPBasicCredentials = Depends(require_admin)):
    """Quick connectivity test — tries to get the Gmail profile."""
    if not _gmail_drafts or not _gmail_drafts.is_configured():
        return {"ok": False, "detail": "Gmail credentials not configured"}
    try:
        service = _gmail_drafts.get_gmail_service()
        profile = service.users().getProfile(userId="me").execute()
        return {"ok": True, "email": profile.get("emailAddress", "unknown")}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


@app.get("/gmail-auth-start")
def gmail_auth_start(request: Request, _: HTTPBasicCredentials = Depends(require_admin)):
    """
    Start the Gmail OAuth re-authentication flow.
    Redirects the browser to Google's consent screen.
    After consent, Google redirects to /gmail-auth-callback.
    """
    client_id = os.getenv("GMAIL_CLIENT_ID")
    client_secret = os.getenv("GMAIL_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise HTTPException(status_code=503, detail="GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET not set")

    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError:
        raise HTTPException(status_code=503, detail="google-auth-oauthlib not installed")

    # Derive the callback URL from the incoming request
    base = str(request.base_url).rstrip("/")
    redirect_uri = f"{base}/gmail-auth-callback"

    state = secrets.token_urlsafe(32)
    _gmail_oauth_states[state] = redirect_uri

    client_config = {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }
    flow = Flow.from_client_config(client_config, scopes=_GMAIL_SCOPES, redirect_uri=redirect_uri)
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        state=state,
        prompt="consent",  # Force refresh token to be returned
        include_granted_scopes="true",
    )
    return RedirectResponse(auth_url)


@app.get("/gmail-auth-callback")
def gmail_auth_callback(code: str = "", state: str = "", error: str = ""):
    """
    OAuth callback — Google redirects here after the user grants access.
    Exchanges the authorization code for tokens and saves the refresh token.
    """
    if error:
        return HTMLResponse(
            f"""<html><body style="font-family:sans-serif;padding:2rem;">
            <h2 style="color:#dc2626;">Authentication error</h2>
            <p>{error}</p>
            <a href="/admin">← Back to Admin</a></body></html>"""
        )

    if state not in _gmail_oauth_states:
        return HTMLResponse(
            """<html><body style="font-family:sans-serif;padding:2rem;">
            <h2 style="color:#dc2626;">Invalid or expired state</h2>
            <p>This auth link has already been used or has expired. Please try again from the admin panel.</p>
            <a href="/admin">← Back to Admin</a></body></html>""",
            status_code=400,
        )

    redirect_uri = _gmail_oauth_states.pop(state)

    client_id = os.getenv("GMAIL_CLIENT_ID")
    client_secret = os.getenv("GMAIL_CLIENT_SECRET")

    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError:
        raise HTTPException(status_code=503, detail="google-auth-oauthlib not installed")

    client_config = {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }
    flow = Flow.from_client_config(client_config, scopes=_GMAIL_SCOPES, redirect_uri=redirect_uri)
    flow.fetch_token(code=code)
    creds = flow.credentials

    new_refresh_token = creds.refresh_token
    if not new_refresh_token:
        return HTMLResponse(
            """<html><body style="font-family:sans-serif;padding:2rem;">
            <h2 style="color:#dc2626;">No refresh token received</h2>
            <p>Google did not return a refresh token. This can happen if the app was already
            authorised. Try revoking access at
            <a href="https://myaccount.google.com/permissions">Google Account Permissions</a>
            and authenticating again.</p>
            <a href="/admin">← Back to Admin</a></body></html>""",
            status_code=400,
        )

    # Update in-memory env (takes effect immediately)
    os.environ["GMAIL_REFRESH_TOKEN"] = new_refresh_token

    # Persist to local .env file
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        env_text = env_path.read_text(encoding="utf-8")
        if "GMAIL_REFRESH_TOKEN=" in env_text:
            env_text = re.sub(
                r"^GMAIL_REFRESH_TOKEN=.*$",
                f"GMAIL_REFRESH_TOKEN={new_refresh_token}",
                env_text,
                flags=re.MULTILINE,
            )
        else:
            env_text += f"\nGMAIL_REFRESH_TOKEN={new_refresh_token}\n"
        env_path.write_text(env_text, encoding="utf-8")

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Gmail Authentication</title>
  <style>
    body {{ font-family: 'Inter', sans-serif; background: #f5f5f4; padding: 2rem; color: #1c1917; }}
    .card {{ background: white; border-radius: 16px; padding: 1.5rem; max-width: 580px; margin: 0 auto;
              box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
    h2 {{ color: #166534; margin-bottom: 0.5rem; }}
    .token-box {{ background: #f5f5f4; border-radius: 8px; padding: 0.75rem 1rem; font-family: monospace;
                  font-size: 0.8rem; word-break: break-all; margin: 1rem 0; border: 1px solid #e7e5e4; }}
    .warning {{ background: #fef9c3; border-radius: 8px; padding: 0.75rem 1rem; font-size: 0.875rem;
                margin: 1rem 0; color: #854d0e; }}
    a.btn {{ display: inline-block; background: #F97415; color: white; text-decoration: none;
             border-radius: 10px; padding: 0.65rem 1.25rem; font-weight: 600; margin-top: 0.75rem; }}
  </style>
</head>
<body>
  <div class="card">
    <h2>✓ Gmail Authentication Successful</h2>
    <p>The refresh token has been updated in memory and saved to <code>.env</code>.</p>
    <p><strong>New refresh token:</strong></p>
    <div class="token-box">{new_refresh_token}</div>
    <div class="warning">
      <strong>Action required on Railway:</strong> Copy the token above and update the
      <code>GMAIL_REFRESH_TOKEN</code> environment variable in your Railway project settings.
      Otherwise the token will reset on the next deploy.
    </div>
    <a class="btn" href="/admin">← Back to Admin</a>
  </div>
</body>
</html>""")


# ---------------------------------------------------------------------------
# Embeddable widget JS
# ---------------------------------------------------------------------------

@app.get("/widget.js")
def widget_js():
    """Serve a self-contained JS snippet that embeds the chat widget on any page."""
    frontend_dir = Path(__file__).parent.parent / "frontend"
    js_path = frontend_dir / "widget.js"
    if not js_path.exists():
        raise HTTPException(status_code=404, detail="widget.js not built yet")
    api_base = os.getenv("WIDGET_BASE_URL", "").rstrip("/")
    content = js_path.read_text(encoding="utf-8").replace("__API_BASE__", api_base)
    return Response(content=content, media_type="application/javascript")


# ---------------------------------------------------------------------------
# Admin UI
# ---------------------------------------------------------------------------

@app.get("/admin", response_class=HTMLResponse)
def admin_ui(_: HTTPBasicCredentials = Depends(require_admin)):
    current_prompt = ""
    if SYSTEM_PROMPT_PATH.exists():
        current_prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    current_prompt_escaped = current_prompt.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    current_model = "meta-llama/llama-4-scout"
    if MODEL_PATH.exists():
        current_model = MODEL_PATH.read_text(encoding="utf-8").strip() or current_model

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Tuk Tuk AI — Admin Panel</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: 'Inter', sans-serif; background: #f5f5f4; color: #1c1917; min-height: 100vh; padding: 2rem; }}
    .container {{ max-width: 800px; margin: 0 auto; }}
    h1 {{ font-size: 1.75rem; font-weight: 700; color: #F97415; margin-bottom: 0.25rem; }}
    .subtitle {{ color: #78716c; margin-bottom: 2rem; font-size: 0.95rem; }}
    .card {{ background: white; border-radius: 16px; padding: 1.5rem; margin-bottom: 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
    .card h2 {{ font-size: 1.1rem; font-weight: 600; margin-bottom: 1rem; }}
    textarea {{ width: 100%; min-height: 220px; border: 1px solid #e7e5e4; border-radius: 10px; padding: 0.875rem; font-family: inherit; font-size: 0.9rem; line-height: 1.6; resize: vertical; color: #1c1917; }}
    textarea:focus {{ outline: none; border-color: #F97415; box-shadow: 0 0 0 3px rgba(249,116,21,0.12); }}
    .btn {{ display: inline-flex; align-items: center; gap: 0.5rem; background: #F97415; color: white; border: none; border-radius: 10px; padding: 0.65rem 1.25rem; font-family: inherit; font-size: 0.9rem; font-weight: 600; cursor: pointer; transition: background 0.15s; }}
    .btn:hover {{ background: #ea6a0e; }}
    .btn.secondary {{ background: #1c1917; }}
    .btn.secondary:hover {{ background: #292524; }}
    .btn:disabled {{ opacity: 0.6; cursor: not-allowed; }}
    .actions {{ display: flex; gap: 0.75rem; margin-top: 1rem; }}
    .status {{ margin-top: 0.75rem; padding: 0.65rem 1rem; border-radius: 8px; font-size: 0.875rem; display: none; }}
    .status.success {{ background: #dcfce7; color: #166534; display: block; }}
    .status.error {{ background: #fee2e2; color: #991b1b; display: block; }}
    .kb-output {{ margin-top: 1rem; background: #1c1917; color: #a8a29e; border-radius: 10px; padding: 1rem; font-family: monospace; font-size: 0.8rem; line-height: 1.5; max-height: 200px; overflow-y: auto; display: none; white-space: pre-wrap; }}
    .model-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; }}
    .model-card {{ display: block; cursor: pointer; }}
    .model-card input[type=radio] {{ display: none; }}
    .model-card-inner {{ border: 2px solid #e7e5e4; border-radius: 12px; padding: 1rem; transition: border-color 0.15s, background 0.15s; background: #fafaf9; height: 100%; }}
    .model-card:hover .model-card-inner {{ border-color: #f9b486; background: #fff8f4; }}
    .model-card input:checked + .model-card-inner {{ border-color: #F97415; background: #fff4ed; }}
    .model-header {{ display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.2rem; }}
    .model-name {{ font-weight: 700; font-size: 0.95rem; }}
    .model-badge {{ font-size: 0.68rem; font-weight: 700; padding: 0.15rem 0.5rem; border-radius: 100px; }}
    .model-badge.free {{ background: #dcfce7; color: #166534; }}
    .model-badge.cheap {{ background: #fef9c3; color: #854d0e; }}
    .model-meta {{ font-size: 0.72rem; color: #a8a29e; margin-bottom: 0.4rem; }}
    .model-desc {{ font-size: 0.8rem; color: #57534e; line-height: 1.45; }}
    .model-pricing {{ margin-top: 0.5rem; font-size: 0.75rem; font-weight: 600; color: #78716c; border-top: 1px solid #f0efee; padding-top: 0.4rem; }}
    .per-msg {{ font-weight: 400; color: #a8a29e; margin-left: 0.35rem; }}
    @media (max-width: 600px) {{ .model-grid {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <div class="container">
    <h1>Tuk Tuk AI — Admin</h1>
    <p class="subtitle">Manage the AI assistant's behavior and knowledge base.</p>

    <div class="card">
      <h2>System Prompt</h2>
      <textarea id="promptTextarea">{current_prompt_escaped}</textarea>
      <div class="actions">
        <button class="btn" onclick="savePrompt()">Save Prompt</button>
      </div>
      <div id="promptStatus" class="status"></div>
    </div>

    <div class="card">
      <h2>AI Model</h2>
      <p style="color:#78716c; font-size:0.9rem; margin-bottom:1.25rem;">
        Choose the language model that powers the assistant. Changes take effect on the next message.
      </p>

      <div class="model-grid" id="modelGrid">

        <label class="model-card {'selected' if current_model == 'meta-llama/llama-4-scout' else ''}">
          <input type="radio" name="model" value="meta-llama/llama-4-scout" {'checked' if current_model == 'meta-llama/llama-4-scout' else ''}>
          <div class="model-card-inner">
            <div class="model-header">
              <span class="model-name">Llama 4 Scout</span>
              <span class="model-badge free">Free tier</span>
            </div>
            <div class="model-meta">Meta · OpenRouter</div>
            <div class="model-desc">Fast, multimodal, handles images in context. Current default — great all-round RAG model.</div>
            <div class="model-pricing">$0.08 in · $0.30 out <span class="per-msg">≈ $0.00025 / msg</span></div>
          </div>
        </label>

        <label class="model-card {'selected' if current_model == 'google/gemini-2.5-flash' else ''}">
          <input type="radio" name="model" value="google/gemini-2.5-flash" {'checked' if current_model == 'google/gemini-2.5-flash' else ''}>
          <div class="model-card-inner">
            <div class="model-header">
              <span class="model-name">Gemini 2.5 Flash</span>
              <span class="model-badge cheap">$</span>
            </div>
            <div class="model-meta">Google · OpenRouter</div>
            <div class="model-desc">Strong reasoning and context handling. The stable, affordable Gemini chat model.</div>
            <div class="model-pricing">$0.15 in · $0.60 out <span class="per-msg">≈ $0.00045 / msg</span></div>
          </div>
        </label>

        <label class="model-card {'selected' if current_model == 'mistralai/mistral-small-3.1-24b-instruct' else ''}">
          <input type="radio" name="model" value="mistralai/mistral-small-3.1-24b-instruct" {'checked' if current_model == 'mistralai/mistral-small-3.1-24b-instruct' else ''}>
          <div class="model-card-inner">
            <div class="model-header">
              <span class="model-name">Mistral Small 3.1</span>
              <span class="model-badge cheap">$</span>
            </div>
            <div class="model-meta">Mistral · OpenRouter</div>
            <div class="model-desc">Excellent at RAG tasks and instruction-following. Multimodal, very affordable, strong European alternative.</div>
            <div class="model-pricing">$0.35 in · $0.56 out <span class="per-msg">≈ $0.00087 / msg</span></div>
          </div>
        </label>

        <label class="model-card {'selected' if current_model == 'google/gemini-3-flash-preview' else ''}">
          <input type="radio" name="model" value="google/gemini-3-flash-preview" {'checked' if current_model == 'google/gemini-3-flash-preview' else ''}>
          <div class="model-card-inner">
            <div class="model-header">
              <span class="model-name">Gemini 3 Flash</span>
              <span class="model-badge cheap">$</span>
            </div>
            <div class="model-meta">Google · OpenRouter</div>
            <div class="model-desc">Powers the Gemini app's fast mode. 1M token context, multimodal (text, image, audio, video).</div>
            <div class="model-pricing">$0.50 in · $3.00 out <span class="per-msg">≈ $0.0019 / msg</span></div>
          </div>
        </label>

      </div>

      <div class="actions" style="margin-top:1.25rem;">
        <button class="btn" onclick="saveModel()">Save Model</button>
      </div>
      <div id="modelStatus" class="status"></div>
    </div>

    <div class="card">
      <h2>Knowledge Base</h2>
      <p style="color:#78716c; font-size:0.9rem; margin-bottom:1rem;">
        Upload a new <strong>.docx</strong> file to replace the current knowledge base.
        The document will be parsed, embedded, and pushed to Pinecone automatically.
      </p>

      <div id="dropZone" class="drop-zone" onclick="document.getElementById('docxInput').click()" ondragover="onDragOver(event)" ondragleave="onDragLeave(event)" ondrop="onDrop(event)">
        <div class="drop-icon">📄</div>
        <div class="drop-label">Drop your .docx here or <span class="drop-link">browse</span></div>
        <div id="dropFileName" class="drop-filename"></div>
        <input type="file" id="docxInput" accept=".docx" style="display:none" onchange="onFileSelected(event)">
      </div>

      <div class="actions" style="margin-top:1rem;">
        <button class="btn secondary" id="uploadKbBtn" onclick="uploadKb()" disabled>Upload &amp; Update KB</button>
      </div>
      <div id="kbStatus" class="status"></div>
      <div id="kbOutput" class="kb-output"></div>
    </div>

    <!-- ================================================================
         CONVERSATIONS LOG
         ================================================================ -->
    <div class="card">
      <h2>Conversation Log</h2>
      <p style="color:#78716c; font-size:0.9rem; margin-bottom:1rem;">
        All chat exchanges via the assistant. Newest first.
      </p>
      <div style="display:flex; gap:0.75rem; margin-bottom:1rem; flex-wrap:wrap; align-items:center;">
        <input id="convSearch" type="text" placeholder="Search messages or session ID…"
          style="flex:1; min-width:180px; border:1px solid #e7e5e4; border-radius:10px; padding:0.55rem 0.9rem; font-family:inherit; font-size:0.875rem;"
          oninput="debounceSearch()">
        <button class="btn secondary" onclick="exportConversations()">Export CSV</button>
        <span id="convCount" style="font-size:0.8rem; color:#78716c;"></span>
      </div>
      <div id="convTable" style="overflow-x:auto;">
        <table style="width:100%; border-collapse:collapse; font-size:0.82rem;">
          <thead>
            <tr style="border-bottom:2px solid #e7e5e4; text-align:left;">
              <th style="padding:0.5rem 0.6rem; color:#57534e; font-weight:600; white-space:nowrap;">Time</th>
              <th style="padding:0.5rem 0.6rem; color:#57534e; font-weight:600;">Session</th>
              <th style="padding:0.5rem 0.6rem; color:#57534e; font-weight:600;">User</th>
              <th style="padding:0.5rem 0.6rem; color:#57534e; font-weight:600;">Assistant</th>
            </tr>
          </thead>
          <tbody id="convBody"></tbody>
        </table>
      </div>
      <div style="display:flex; gap:0.75rem; margin-top:1rem; align-items:center;">
        <button class="btn secondary" id="convLoadMore" onclick="loadMoreConvs()" style="display:none;">Load more</button>
      </div>
    </div>

    <!-- ================================================================
         GMAIL AUTHENTICATION
         ================================================================ -->
    <div class="card">
      <h2>Gmail Authentication</h2>
      <p style="color:#78716c; font-size:0.9rem; margin-bottom:1rem;">
        If Gmail is returning a token error, re-authenticate here to get a fresh token.
        After authorising, copy the new token and update <code>GMAIL_REFRESH_TOKEN</code>
        in your Railway environment variables.
      </p>
      <div style="background:#fef9c3; border-radius:8px; padding:0.75rem 1rem; font-size:0.82rem; color:#854d0e; margin-bottom:1rem;">
        <strong>One-time setup required:</strong> For this button to work, add
        <code id="callbackUrl" style="word-break:break-all;">{os.getenv("WIDGET_BASE_URL", "https://&lt;your-railway-url&gt;").rstrip("/")}/gmail-auth-callback</code>
        as an authorised redirect URI on your OAuth client in
        <a href="https://console.cloud.google.com/apis/credentials" target="_blank" style="color:#854d0e;">Google Cloud Console</a>.
        You also need to change (or add) the OAuth client type to <strong>Web application</strong>.
      </div>
      <div class="actions">
        <a href="/gmail-auth-start" class="btn">Re-authenticate Gmail</a>
        <button class="btn secondary" onclick="testGmailConnection()">Test Connection</button>
      </div>
      <div id="gmailTestStatus" class="status"></div>
    </div>

    <!-- ================================================================
         EMAIL DRAFTS
         ================================================================ -->
    <div class="card">
      <h2>Email Drafts</h2>
      <p style="color:#78716c; font-size:0.9rem; margin-bottom:1rem;">
        Auto-generated draft replies to contact form inquiries from the website.
        Review and send from
        <a href="https://mail.google.com/mail/u/0/#drafts" target="_blank"
           style="color:#F97415;">Gmail Drafts ↗</a>.
      </p>
      <div style="display:flex; gap:0.75rem; margin-bottom:1rem; flex-wrap:wrap; align-items:center;">
        <button class="btn" id="checkEmailsBtn" onclick="checkEmails()">Check Emails Now</button>
        <input id="draftSearch" type="text" placeholder="Search name, email or subject…"
          style="flex:1; min-width:180px; border:1px solid #e7e5e4; border-radius:10px;
                 padding:0.55rem 0.9rem; font-family:inherit; font-size:0.875rem;"
          oninput="debounceDraftSearch()">
        <span id="draftCount" style="font-size:0.8rem; color:#78716c;"></span>
      </div>
      <div id="emailStatus" class="status"></div>
      <div id="draftsTable" style="overflow-x:auto; margin-top:0.75rem;">
        <table style="width:100%; border-collapse:collapse; font-size:0.82rem;">
          <thead>
            <tr style="border-bottom:2px solid #e7e5e4; text-align:left;">
              <th style="padding:0.5rem 0.6rem; color:#57534e; font-weight:600; white-space:nowrap;">Time</th>
              <th style="padding:0.5rem 0.6rem; color:#57534e; font-weight:600;">Name</th>
              <th style="padding:0.5rem 0.6rem; color:#57534e; font-weight:600;">Email</th>
              <th style="padding:0.5rem 0.6rem; color:#57534e; font-weight:600;">Subject</th>
              <th style="padding:0.5rem 0.6rem; color:#57534e; font-weight:600;">Message</th>
              <th style="padding:0.5rem 0.6rem; color:#57534e; font-weight:600;">AI Reply</th>
              <th style="padding:0.5rem 0.6rem; color:#57534e; font-weight:600;">Drafts</th>
            </tr>
          </thead>
          <tbody id="draftsBody"></tbody>
        </table>
      </div>
      <div style="margin-top:1rem;">
        <button class="btn secondary" id="draftLoadMore" onclick="loadMoreDrafts()" style="display:none;">Load more</button>
      </div>
    </div>

    <!-- ================================================================
         IMAGE KNOWLEDGE BASE
         ================================================================ -->
    <div class="card">
      <h2>Image Knowledge Base</h2>
      <p style="color:#78716c; font-size:0.9rem; margin-bottom:1.25rem;">
        Upload individual images with captions so the assistant can retrieve and show them when relevant.
        Gemini Vision auto-generates the caption — review and edit it before saving.
      </p>

      <div style="display:grid; grid-template-columns:1fr 1fr; gap:1.25rem; align-items:start;">

        <!-- Left: upload + caption -->
        <div>
          <div id="imgDropZone" class="drop-zone" onclick="document.getElementById('imgInput').click()" ondragover="onImgDragOver(event)" ondragleave="onImgDragLeave(event)" ondrop="onImgDrop(event)" style="min-height:140px; display:flex; flex-direction:column; align-items:center; justify-content:center; position:relative;">
            <div id="imgPreviewWrap" style="display:none; width:100%;">
              <img id="imgPreview" style="max-height:130px; max-width:100%; border-radius:8px; object-fit:contain;">
            </div>
            <div id="imgDropHint">
              <div class="drop-icon">🖼️</div>
              <div class="drop-label">Drop image or <span class="drop-link">browse</span></div>
            </div>
            <input type="file" id="imgInput" accept="image/*" style="display:none" onchange="onImgSelected(event)">
          </div>
          <button class="btn" id="captionBtn" onclick="generateCaption()" disabled style="margin-top:0.75rem; width:100%; justify-content:center;">Auto-caption with Gemini</button>
        </div>

        <!-- Right: caption textarea + save -->
        <div style="display:flex; flex-direction:column; gap:0.75rem;">
          <textarea id="captionText" placeholder="Caption will appear here after auto-generation, or type your own..." style="min-height:120px; flex:1;"></textarea>
          <button class="btn secondary" id="saveImgBtn" onclick="saveImage()" disabled style="justify-content:center;">Save to Knowledge Base</button>
        </div>

      </div>

      <div id="imgStatus" class="status" style="margin-top:0.75rem;"></div>

      <!-- Saved images grid -->
      <div id="savedImagesSection" style="margin-top:1.5rem; display:none;">
        <div style="font-size:0.85rem; font-weight:600; margin-bottom:0.75rem; color:#57534e;">Saved Images</div>
        <div id="savedImagesGrid" style="display:grid; grid-template-columns:repeat(auto-fill, minmax(140px,1fr)); gap:0.75rem;"></div>
      </div>
    </div>

  </div>

  <style>
    .drop-zone {{
      border: 2px dashed #e7e5e4;
      border-radius: 12px;
      padding: 2rem 1rem;
      text-align: center;
      cursor: pointer;
      transition: border-color 0.15s, background 0.15s;
      background: #fafaf9;
    }}
    .drop-zone:hover, .drop-zone.drag-over {{
      border-color: #F97415;
      background: #fff4ed;
    }}
    .drop-icon {{ font-size: 2rem; margin-bottom: 0.5rem; }}
    .drop-label {{ font-size: 0.9rem; color: #78716c; }}
    .drop-link {{ color: #F97415; font-weight: 600; }}
    .drop-filename {{ margin-top: 0.5rem; font-size: 0.85rem; font-weight: 600; color: #1c1917; }}
  </style>

  <script>
    // ---- Image KB ----
    let selectedImgFile = null;
    let currentImgB64 = null;

    function onImgDragOver(e) {{ e.preventDefault(); document.getElementById('imgDropZone').classList.add('drag-over'); }}
    function onImgDragLeave() {{ document.getElementById('imgDropZone').classList.remove('drag-over'); }}
    function onImgDrop(e) {{
      e.preventDefault();
      document.getElementById('imgDropZone').classList.remove('drag-over');
      const f = e.dataTransfer.files[0];
      if (f && f.type.startsWith('image/')) setImgFile(f);
    }}
    function onImgSelected(e) {{ const f = e.target.files[0]; if (f) setImgFile(f); }}

    function setImgFile(file) {{
      selectedImgFile = file;
      currentImgB64 = null;
      const reader = new FileReader();
      reader.onload = ev => {{
        document.getElementById('imgPreview').src = ev.target.result;
        document.getElementById('imgPreviewWrap').style.display = 'block';
        document.getElementById('imgDropHint').style.display = 'none';
      }};
      reader.readAsDataURL(file);
      document.getElementById('captionBtn').disabled = false;
      document.getElementById('saveImgBtn').disabled = false;
    }}

    async function generateCaption() {{
      if (!selectedImgFile) return;
      const btn = document.getElementById('captionBtn');
      const status = document.getElementById('imgStatus');
      btn.disabled = true;
      btn.textContent = 'Generating…';
      status.className = 'status';
      try {{
        const fd = new FormData();
        fd.append('file', selectedImgFile);
        const res = await fetch('/caption-image', {{ method: 'POST', body: fd }});
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Error');
        document.getElementById('captionText').value = data.caption;
        currentImgB64 = data.image_b64;
      }} catch(e) {{
        status.textContent = 'Caption error: ' + e.message;
        status.className = 'status error';
      }} finally {{
        btn.disabled = false;
        btn.textContent = 'Auto-caption with Gemini';
      }}
    }}

    async function saveImage() {{
      const caption = document.getElementById('captionText').value.trim();
      if (!caption) {{ alert('Please add a caption before saving.'); return; }}
      const btn = document.getElementById('saveImgBtn');
      const status = document.getElementById('imgStatus');
      btn.disabled = true;
      btn.textContent = 'Saving…';
      status.className = 'status';

      // If no b64 yet (user typed caption without auto-generating), read file
      let imgB64 = currentImgB64;
      if (!imgB64 && selectedImgFile) {{
        imgB64 = await new Promise(resolve => {{
          const r = new FileReader();
          r.onload = e => resolve(e.target.result);
          r.readAsDataURL(selectedImgFile);
        }});
      }}

      try {{
        const res = await fetch('/save-image', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ caption, image_b64: imgB64 || '' }})
        }});
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Error');
        status.textContent = '✓ Image saved to knowledge base.';
        status.className = 'status success';
        // Reset
        selectedImgFile = null; currentImgB64 = null;
        document.getElementById('captionText').value = '';
        document.getElementById('imgPreviewWrap').style.display = 'none';
        document.getElementById('imgDropHint').style.display = '';
        document.getElementById('captionBtn').disabled = true;
        document.getElementById('saveImgBtn').disabled = true;
        loadSavedImages();
      }} catch(e) {{
        status.textContent = 'Save error: ' + e.message;
        status.className = 'status error';
      }} finally {{
        btn.disabled = false;
        btn.textContent = 'Save to Knowledge Base';
      }}
    }}

    async function loadSavedImages() {{
      try {{
        const res = await fetch('/images');
        const data = await res.json();
        const images = data.images || [];
        const section = document.getElementById('savedImagesSection');
        const grid = document.getElementById('savedImagesGrid');
        if (images.length === 0) {{ section.style.display = 'none'; return; }}
        section.style.display = 'block';
        grid.innerHTML = images.map(img => `
          <div style="border:1px solid #e7e5e4; border-radius:10px; overflow:hidden; background:#fafaf9;">
            ${{img.image_b64 ? `<img src="${{img.image_b64}}" style="width:100%; height:100px; object-fit:cover; display:block;">` : '<div style="height:100px; background:#f5f5f4; display:flex; align-items:center; justify-content:center; font-size:1.5rem;">🖼️</div>'}}
            <div style="padding:0.5rem;">
              <div style="font-size:0.72rem; color:#57534e; line-height:1.4; max-height:3.5em; overflow:hidden;">${{img.caption}}</div>
              <button onclick="deleteImage('${{img.id}}')" style="margin-top:0.4rem; font-size:0.7rem; color:#dc2626; background:none; border:none; cursor:pointer; padding:0; font-family:inherit;">Delete</button>
            </div>
          </div>
        `).join('');
      }} catch(e) {{ console.error('Could not load images', e); }}
    }}

    async function deleteImage(id) {{
      if (!confirm('Delete this image from the knowledge base?')) return;
      await fetch('/image/' + id, {{ method: 'DELETE' }});
      loadSavedImages();
    }}

    // Load saved images on page open
    loadSavedImages();

    // ---- DOCX KB ----
    let selectedFile = null;

    function onFileSelected(e) {{
      const file = e.target.files[0];
      if (file) setFile(file);
    }}

    function onDragOver(e) {{
      e.preventDefault();
      document.getElementById('dropZone').classList.add('drag-over');
    }}

    function onDragLeave() {{
      document.getElementById('dropZone').classList.remove('drag-over');
    }}

    function onDrop(e) {{
      e.preventDefault();
      document.getElementById('dropZone').classList.remove('drag-over');
      const file = e.dataTransfer.files[0];
      if (file && file.name.endsWith('.docx')) setFile(file);
      else alert('Please drop a .docx file.');
    }}

    function setFile(file) {{
      selectedFile = file;
      document.getElementById('dropFileName').textContent = file.name + '  (' + (file.size / 1024).toFixed(0) + ' KB)';
      document.getElementById('uploadKbBtn').disabled = false;
    }}

    async function saveModel() {{
      const selected = document.querySelector('input[name="model"]:checked');
      if (!selected) return;
      const status = document.getElementById('modelStatus');
      status.className = 'status';
      try {{
        const res = await fetch('/model', {{
          method: 'PUT',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ model: selected.value }})
        }});
        if (res.ok) {{
          status.textContent = 'Model saved: ' + selected.value;
          status.className = 'status success';
        }} else {{
          throw new Error(await res.text());
        }}
      }} catch (e) {{
        status.textContent = 'Error: ' + e.message;
        status.className = 'status error';
      }}
    }}

    async function savePrompt() {{
      const prompt = document.getElementById('promptTextarea').value;
      const status = document.getElementById('promptStatus');
      status.className = 'status';
      try {{
        const res = await fetch('/system-prompt', {{
          method: 'PUT',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ prompt }})
        }});
        if (res.ok) {{
          status.textContent = 'Prompt saved successfully.';
          status.className = 'status success';
        }} else {{
          throw new Error(await res.text());
        }}
      }} catch (e) {{
        status.textContent = 'Error: ' + e.message;
        status.className = 'status error';
      }}
    }}

    async function uploadKb() {{
      if (!selectedFile) return;
      const btn = document.getElementById('uploadKbBtn');
      const status = document.getElementById('kbStatus');
      const output = document.getElementById('kbOutput');
      btn.disabled = true;
      btn.textContent = 'Processing…';
      status.className = 'status';
      output.style.display = 'none';

      try {{
        const formData = new FormData();
        formData.append('file', selectedFile);
        const res = await fetch('/upload-kb', {{ method: 'POST', body: formData }});
        const data = await res.json();
        if (res.ok) {{
          status.textContent = `✓ "${{data.filename}}" uploaded (${{data.size_kb}} KB) — knowledge base updated.`;
          status.className = 'status success';
          if (data.output) {{
            output.textContent = data.output;
            output.style.display = 'block';
          }}
        }} else {{
          throw new Error(data.detail || 'Unknown error');
        }}
      }} catch (e) {{
        status.textContent = 'Error: ' + e.message;
        status.className = 'status error';
      }} finally {{
        btn.disabled = false;
        btn.textContent = 'Upload & Update KB';
      }}
    }}

    // ---- Conversations ----
    let convOffset = 0;
    const CONV_BATCH = 50;
    let convSearch = '';
    let convTotal = 0;
    let convDebounce = null;

    function debounceSearch() {{
      clearTimeout(convDebounce);
      convDebounce = setTimeout(() => {{
        convSearch = document.getElementById('convSearch').value.trim();
        convOffset = 0;
        document.getElementById('convBody').innerHTML = '';
        loadConvs();
      }}, 350);
    }}

    function escHtml(s) {{
      return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }}

    async function loadConvs() {{
      const params = new URLSearchParams({{ limit: CONV_BATCH, offset: convOffset, search: convSearch }});
      try {{
        const res = await fetch('/conversations?' + params);
        if (res.status === 401) {{ document.getElementById('convCount').textContent = '(auth required — refresh page)'; return; }}
        const data = await res.json();
        convTotal = data.total;
        const rows = data.conversations || [];
        const tbody = document.getElementById('convBody');
        rows.forEach(r => {{
          const ts = new Date(r.timestamp).toLocaleString('en-GB', {{dateStyle:'short', timeStyle:'short'}});
          const sid = r.session_id.slice(0,8) + '…';
          const tr = document.createElement('tr');
          tr.style.borderBottom = '1px solid #f0efee';
          tr.innerHTML = `
            <td style="padding:0.5rem 0.6rem; color:#78716c; white-space:nowrap;">${{ts}}</td>
            <td style="padding:0.5rem 0.6rem; font-family:monospace; color:#a8a29e; white-space:nowrap;">${{escHtml(sid)}}</td>
            <td style="padding:0.5rem 0.6rem; max-width:240px;">${{escHtml(r.user_message.slice(0,120))}}${{r.user_message.length > 120 ? '…' : ''}}</td>
            <td style="padding:0.5rem 0.6rem; max-width:300px; color:#57534e;">${{escHtml(r.bot_response.slice(0,180))}}${{r.bot_response.length > 180 ? '…' : ''}}</td>
          `;
          tbody.appendChild(tr);
        }});
        convOffset += rows.length;
        document.getElementById('convCount').textContent = `${{convOffset}} / ${{convTotal}} shown`;
        document.getElementById('convLoadMore').style.display = convOffset < convTotal ? '' : 'none';
      }} catch(e) {{ console.error('convs error', e); }}
    }}

    function loadMoreConvs() {{ loadConvs(); }}

    function exportConversations() {{
      window.location.href = '/conversations/export';
    }}

    loadConvs();

    // ---- Email Drafts ----
    let draftOffset = 0;
    const DRAFT_BATCH = 50;
    let draftSearch = '';
    let draftTotal = 0;
    let draftDebounce = null;

    function debounceDraftSearch() {{
      clearTimeout(draftDebounce);
      draftDebounce = setTimeout(() => {{
        draftSearch = document.getElementById('draftSearch').value.trim();
        draftOffset = 0;
        document.getElementById('draftsBody').innerHTML = '';
        loadDrafts();
      }}, 350);
    }}

    async function checkEmails() {{
      const btn = document.getElementById('checkEmailsBtn');
      const status = document.getElementById('emailStatus');
      btn.disabled = true;
      btn.textContent = 'Checking…';
      status.className = 'status';
      try {{
        const res = await fetch('/process-emails', {{ method: 'POST' }});
        const data = await res.json();
        if (res.ok) {{
          const n = data.processed || 0;
          const errs = data.errors || [];
          if (n === 0 && errs.length > 0) {{
            status.textContent = 'Error: ' + errs[0];
            status.className = 'status error';
          }} else {{
            status.textContent = n === 0
              ? 'No new emails found.'
              : `✓ Created ${{n}} draft${{n === 1 ? '' : 's'}} — check Gmail Drafts.`;
            status.className = 'status success';
            if (n > 0) {{
              draftOffset = 0;
              document.getElementById('draftsBody').innerHTML = '';
              loadDrafts();
            }}
          }}
        }} else {{
          throw new Error(data.detail || 'Error');
        }}
      }} catch(e) {{
        status.textContent = 'Error: ' + e.message;
        status.className = 'status error';
      }} finally {{
        btn.disabled = false;
        btn.textContent = 'Check Emails Now';
      }}
    }}

    function stripHtml(html) {{
      const tmp = document.createElement('div');
      tmp.innerHTML = html;
      return tmp.textContent || tmp.innerText || '';
    }}

    async function loadDrafts() {{
      const params = new URLSearchParams({{ limit: DRAFT_BATCH, offset: draftOffset, search: draftSearch }});
      try {{
        const res = await fetch('/email-drafts?' + params);
        if (res.status === 401) {{ document.getElementById('draftCount').textContent = '(auth required — refresh page)'; return; }}
        const data = await res.json();
        draftTotal = data.total;
        const rows = data.drafts || [];
        const tbody = document.getElementById('draftsBody');
        rows.forEach(r => {{
          const ts = new Date(r.created_at).toLocaleString('en-GB', {{dateStyle:'short', timeStyle:'short'}});
          const aiPreview = stripHtml(r.ai_reply_html || '');
          const tr = document.createElement('tr');
          tr.style.borderBottom = '1px solid #f0efee';
          tr.innerHTML = `
            <td style="padding:0.5rem 0.6rem; color:#78716c; white-space:nowrap;">${{ts}}</td>
            <td style="padding:0.5rem 0.6rem; white-space:nowrap;">${{escHtml(r.from_name || '')}}</td>
            <td style="padding:0.5rem 0.6rem;">
              <a href="mailto:${{escHtml(r.from_email || '')}}" style="color:#F97415;">
                ${{escHtml(r.from_email || '')}}
              </a>
            </td>
            <td style="padding:0.5rem 0.6rem; max-width:150px;">
              ${{escHtml((r.subject || '').slice(0,55))}}${{(r.subject||'').length > 55 ? '…' : ''}}
            </td>
            <td style="padding:0.5rem 0.6rem; max-width:180px; color:#57534e;">
              ${{escHtml((r.customer_message || '').slice(0,80))}}${{(r.customer_message||'').length > 80 ? '…' : ''}}
            </td>
            <td style="padding:0.5rem 0.6rem; max-width:200px; color:#57534e; font-style:italic;">
              ${{escHtml(aiPreview.slice(0,100))}}${{aiPreview.length > 100 ? '…' : ''}}
            </td>
            <td style="padding:0.5rem 0.6rem; white-space:nowrap;">
              <a href="https://mail.google.com/mail/u/0/#drafts" target="_blank"
                 style="color:#F97415; font-size:0.8rem;">Open Drafts ↗</a>
            </td>
          `;
          tbody.appendChild(tr);
        }});
        draftOffset += rows.length;
        document.getElementById('draftCount').textContent = `${{draftOffset}} / ${{draftTotal}} shown`;
        document.getElementById('draftLoadMore').style.display = draftOffset < draftTotal ? '' : 'none';
      }} catch(e) {{ console.error('drafts error', e); }}
    }}

    function loadMoreDrafts() {{ loadDrafts(); }}

    loadDrafts();

    // ---- Gmail auth test ----
    async function testGmailConnection() {{
      const btn = event.target;
      const status = document.getElementById('gmailTestStatus');
      btn.disabled = true;
      btn.textContent = 'Testing…';
      status.className = 'status';
      try {{
        const res = await fetch('/gmail-auth-test');
        const data = await res.json();
        if (data.ok) {{
          status.textContent = '✓ Connected as ' + data.email;
          status.className = 'status success';
        }} else {{
          status.textContent = 'Error: ' + (data.detail || 'Unknown error');
          status.className = 'status error';
        }}
      }} catch(e) {{
        status.textContent = 'Request failed: ' + e.message;
        status.className = 'status error';
      }} finally {{
        btn.disabled = false;
        btn.textContent = 'Test Connection';
      }}
    }}
  </script>
</body>
</html>"""
