"""
gmail_drafts.py — Gmail integration for auto-generating draft email responses.

Workflow:
  1. Search itookatuktuk@gmail.com for unprocessed contact form emails
     (subject contains "new message from", not yet labelled "draft-created")
  2. Parse the structured fields: Nome, Assunto, Email, Telef, Mensagem
  3. Feed the inquiry into the same RAG pipeline as the website chatbot
  4. Wrap the AI reply in a branded HTML email template
  5. Create a Gmail draft addressed to the customer's email
  6. Apply the "draft-created" label to the original email (prevents reprocessing)
  7. Log everything to the email_drafts table for the admin panel

Prerequisites:
    Set GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN in .env
    (Run tools/gmail_auth.py once locally to obtain these values)
"""

from __future__ import annotations

import base64
import logging
import os
import re
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import markdown as md
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

load_dotenv()

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
PROCESSED_LABEL = "draft-created"
SEARCH_QUERY = 'subject:"new message from" -label:draft-created'


# ---------------------------------------------------------------------------
# Config check
# ---------------------------------------------------------------------------

def is_configured() -> bool:
    """Return True if all Gmail credentials are present in the environment."""
    return all([
        os.getenv("GMAIL_CLIENT_ID"),
        os.getenv("GMAIL_CLIENT_SECRET"),
        os.getenv("GMAIL_REFRESH_TOKEN"),
    ])


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def get_gmail_service():
    """
    Build an authenticated Gmail API service using stored OAuth credentials.
    No browser needed — uses stored refresh token to obtain a fresh access token.
    """
    creds = Credentials(
        token=None,
        refresh_token=os.getenv("GMAIL_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.getenv("GMAIL_CLIENT_ID"),
        client_secret=os.getenv("GMAIL_CLIENT_SECRET"),
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return build("gmail", "v1", credentials=creds)


# ---------------------------------------------------------------------------
# Label management
# ---------------------------------------------------------------------------

def get_or_create_label(service, label_name: str) -> str:
    """Return the label ID for label_name, creating it in Gmail if it doesn't exist."""
    labels_result = service.users().labels().list(userId="me").execute()
    for label in labels_result.get("labels", []):
        if label["name"].lower() == label_name.lower():
            return label["id"]

    created = service.users().labels().create(
        userId="me",
        body={
            "name": label_name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        },
    ).execute()
    return created["id"]


# ---------------------------------------------------------------------------
# Email fetching and parsing
# ---------------------------------------------------------------------------

def _decode_body_part(part: dict) -> str:
    """Decode a Gmail message body part from base64url encoding."""
    data = part.get("body", {}).get("data", "")
    if not data:
        return ""
    # Gmail uses base64url without padding; add padding before decoding
    padded = data + "=" * (4 - len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _extract_plain_text(payload: dict) -> str:
    """Recursively extract plain text from a Gmail message payload."""
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/plain":
        return _decode_body_part(payload)

    if mime_type.startswith("multipart/"):
        parts = payload.get("parts", [])
        # Prefer text/plain over other parts
        for part in parts:
            if part.get("mimeType") == "text/plain":
                text = _decode_body_part(part)
                if text.strip():
                    return text
        # Fall back to recursive extraction
        for part in parts:
            text = _extract_plain_text(part)
            if text.strip():
                return text

    return ""


def _parse_contact_fields(body: str) -> dict:
    """
    Parse structured contact form fields from email body text.

    Expected format (field names may appear in Portuguese or English):
        Nome: John Smith
        Assunto: Tour inquiry
        Email: john@example.com
        Telef: +351123456789
        Mensagem: I would like to book a tour...
                  (Mensagem may span multiple lines)
    """
    result = {"nome": "", "assunto": "", "email": "", "telef": "", "mensagem": ""}

    # Each field runs until the next field label or end of string
    field_labels = r"(?:Nome|Name|Assunto|Subject|Email|Telef|Phone|Tel|Mensagem|Message|Telephone)"
    pattern = re.compile(
        r"(?:^|\n)"
        r"(" + field_labels + r")\s*[:\-]\s*"
        r"(.*?)(?=\n" + field_labels + r"\s*[:\-]|$)",
        re.IGNORECASE | re.DOTALL,
    )

    field_map = {
        "nome": "nome", "name": "nome",
        "assunto": "assunto", "subject": "assunto",
        "email": "email",
        "telef": "telef", "tel": "telef", "phone": "telef", "telephone": "telef",
        "mensagem": "mensagem", "message": "mensagem",
    }

    for match in pattern.finditer(body):
        key = match.group(1).strip().lower()
        value = match.group(2).strip()
        canonical = field_map.get(key, key)
        if canonical in result:
            result[canonical] = value

    return result


def fetch_unprocessed_email_queries(service) -> list[dict]:
    """
    Search for unprocessed contact form emails and return parsed data.

    Returns a list of dicts, each containing:
        message_id, nome, assunto, email, telef, mensagem, original_subject
    """
    results = service.users().messages().list(
        userId="me",
        q=SEARCH_QUERY,
        maxResults=20,
    ).execute()

    messages = results.get("messages", [])
    parsed = []

    for msg_ref in messages:
        try:
            msg = service.users().messages().get(
                userId="me",
                id=msg_ref["id"],
                format="full",
            ).execute()

            payload = msg.get("payload", {})
            headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
            subject = headers.get("subject", "")

            body_text = _extract_plain_text(payload)
            fields = _parse_contact_fields(body_text)

            if not fields.get("email"):
                logger.warning(
                    "No customer email found in message %s (subject: %s), skipping",
                    msg_ref["id"], subject,
                )
                continue

            parsed.append({
                "message_id": msg_ref["id"],
                "original_subject": subject,
                **fields,
            })

        except Exception as e:
            logger.error("Error parsing message %s: %s", msg_ref["id"], e)

    return parsed


# ---------------------------------------------------------------------------
# AI reply generation
# ---------------------------------------------------------------------------

def generate_ai_reply(nome: str, assunto: str, mensagem: str) -> str:
    """
    Generate a reply using the same RAG pipeline as the website chatbot.
    The query is framed as a customer inquiry so the AI responds appropriately.
    """
    try:
        from backend import rag
    except ImportError:
        import rag  # type: ignore

    query = (
        f"Customer inquiry from {nome} regarding \"{assunto}\":\n\n"
        f"{mensagem}\n\n"
        "Please provide a helpful, friendly email reply to this customer's inquiry. "
        "Include any relevant booking links or pricing information if applicable. "
        "Keep the tone warm and professional."
    )

    result = rag.answer(query=query, history=None)
    return result["answer"]


# ---------------------------------------------------------------------------
# HTML email template
# ---------------------------------------------------------------------------

def build_html_email(ai_reply_text: str) -> str:
    """
    Wrap the AI reply (markdown) in a branded HTML email template.
    The template uses I Took a Tuk Tuk's orange brand colour (#F97415).
    """
    # Convert markdown to HTML (handles bold, links, lists, tables, etc.)
    reply_html = md.markdown(ai_reply_text, extensions=["extra"])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin:0; padding:0; background:#f5f5f4; font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f4; padding:32px 16px;">
    <tr>
      <td align="center">
        <table width="600" cellpadding="0" cellspacing="0"
               style="background:#ffffff; border-radius:12px; overflow:hidden;
                      box-shadow:0 2px 8px rgba(0,0,0,0.08); max-width:600px;">

          <!-- Header -->
          <tr>
            <td style="background:#F97415; padding:24px 32px;">
              <p style="margin:0; font-size:20px; font-weight:700; color:#ffffff; letter-spacing:-0.3px;">
                I Took a Tuk Tuk
              </p>
              <p style="margin:4px 0 0; font-size:13px; color:rgba(255,255,255,0.85);">
                Tuk Tuk Tours &middot; Lisbon
              </p>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="padding:32px;">
              <div style="font-size:15px; line-height:1.75; color:#1c1917;">
                {reply_html}
              </div>
            </td>
          </tr>

          <!-- Divider -->
          <tr>
            <td style="padding:0 32px;">
              <hr style="border:none; border-top:1px solid #f0efee; margin:0;">
            </td>
          </tr>

          <!-- Signature -->
          <tr>
            <td style="padding:24px 32px 32px;">
              <p style="margin:0 0 4px; font-size:14px; font-weight:600; color:#1c1917;">
                Best regards,
              </p>
              <p style="margin:0 0 2px; font-size:14px; font-weight:700; color:#F97415;">
                I Took a Tuk Tuk Team
              </p>
              <p style="margin:6px 0 0; font-size:13px; color:#78716c; line-height:1.7;">
                <a href="mailto:itookatuktuk@gmail.com"
                   style="color:#F97415; text-decoration:none;">itookatuktuk@gmail.com</a><br>
                <a href="https://www.itookatuktuk.com"
                   style="color:#F97415; text-decoration:none;">www.itookatuktuk.com</a>
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Draft creation
# ---------------------------------------------------------------------------

def create_gmail_draft(service, to_email: str, subject: str, html_body: str) -> str:
    """Create a Gmail draft and return the draft ID."""
    reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"

    msg = MIMEMultipart("alternative")
    msg["To"] = to_email
    msg["Subject"] = reply_subject

    msg.attach(MIMEText(html_body, "html", "utf-8"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    draft = service.users().drafts().create(
        userId="me",
        body={"message": {"raw": raw}},
    ).execute()
    return draft["id"]


def mark_as_processed(service, message_id: str, label_id: str) -> None:
    """Apply the 'draft-created' label to the original email so it isn't reprocessed."""
    service.users().messages().modify(
        userId="me",
        id=message_id,
        body={"addLabelIds": [label_id]},
    ).execute()


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def process_email_queries() -> dict:
    """
    Fetch unprocessed contact emails, generate AI replies, create Gmail drafts.

    Returns {"processed": N, "errors": [...]}
    Called both by the 15-minute background scheduler and the manual admin button.
    """
    if not is_configured():
        logger.info("Gmail credentials not configured — skipping email check")
        return {"processed": 0, "errors": ["Gmail credentials not configured"]}

    try:
        from backend import db as _db
    except ImportError:
        import db as _db  # type: ignore

    processed = 0
    errors: list[str] = []

    try:
        service = get_gmail_service()
        label_id = get_or_create_label(service, PROCESSED_LABEL)
        emails = fetch_unprocessed_email_queries(service)

        logger.info("Found %d unprocessed email queries", len(emails))

        for email_data in emails:
            try:
                # 1. Generate AI reply via RAG pipeline
                ai_reply = generate_ai_reply(
                    nome=email_data["nome"],
                    assunto=email_data["assunto"],
                    mensagem=email_data["mensagem"],
                )

                # 2. Build HTML email
                html_body = build_html_email(ai_reply)

                # 3. Create Gmail draft
                draft_id = create_gmail_draft(
                    service=service,
                    to_email=email_data["email"],
                    subject=email_data["original_subject"],
                    html_body=html_body,
                )

                # 4. Log to admin database
                _db.log_email_draft(
                    from_name=email_data["nome"],
                    from_email=email_data["email"],
                    subject=email_data["assunto"] or email_data["original_subject"],
                    customer_message=email_data["mensagem"],
                    ai_reply_html=html_body,
                    gmail_draft_id=draft_id,
                    original_email_id=email_data["message_id"],
                )

                # 5. Mark original email as processed
                mark_as_processed(service, email_data["message_id"], label_id)

                processed += 1
                logger.info(
                    "Draft created for %s (draft_id=%s)", email_data["email"], draft_id
                )

            except Exception as e:
                err = f"Failed to process message {email_data.get('message_id', '?')}: {e}"
                logger.error(err)
                errors.append(err)

    except Exception as e:
        err = f"Gmail service error: {e}"
        logger.error(err)
        errors.append(err)

    return {"processed": processed, "errors": errors}
