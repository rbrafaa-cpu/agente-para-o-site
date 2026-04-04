/**
 * app.js — I Took a Tuk Tuk AI Chat Widget
 *
 * Handles:
 *   - Sending messages to POST /chat
 *   - Rendering bot responses (text + images)
 *   - Session persistence (session_id in localStorage)
 *   - Suggested questions
 *   - Image lightbox
 *   - Textarea auto-resize
 */

const API_BASE = "http://localhost:8000"; // Change to your deployed backend URL

const messagesEl  = document.getElementById("chatMessages");
const inputEl     = document.getElementById("chatInput");
const sendBtn     = document.getElementById("sendBtn");
const lightbox    = document.getElementById("lightbox");
const lightboxImg = document.getElementById("lightboxImg");

let sessionId = localStorage.getItem("tuktuk_session_id") || "";
let isWaiting = false;

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", () => {
  renderWelcome();
  inputEl.addEventListener("keydown", onKeyDown);
  inputEl.addEventListener("input", autoResizeInput);
  sendBtn.addEventListener("click", sendMessage);

  // Close lightbox on backdrop click
  lightbox.addEventListener("click", (e) => {
    if (e.target === lightbox) closeLightbox();
  });
});

// ---------------------------------------------------------------------------
// Welcome screen
// ---------------------------------------------------------------------------
function renderWelcome() {
  const suggestions = [
    "What tours do you offer?",
    "How do I book a tour?",
    "How long are the tours?",
    "Do you offer private tours?",
    "What's included in the price?",
  ];

  const welcome = document.createElement("div");
  welcome.className = "welcome-message";
  welcome.id = "welcomeMsg";
  welcome.innerHTML = `
    <div class="wave">🛺</div>
    <p>Hello! I'm your Lisbon tour guide assistant.<br>Ask me anything about our tuk-tuk experiences.</p>
    <div class="suggestions">
      ${suggestions.map(q => `<button class="suggestion-chip" onclick="sendSuggestion(this)">${q}</button>`).join("")}
    </div>
  `;
  messagesEl.appendChild(welcome);
}

function removeWelcome() {
  const el = document.getElementById("welcomeMsg");
  if (el) el.remove();
}

function clearConversation() {
  sessionId = "";
  localStorage.removeItem("tuktuk_session_id");
  messagesEl.innerHTML = "";
  renderWelcome();
}

// ---------------------------------------------------------------------------
// Sending messages
// ---------------------------------------------------------------------------
function sendSuggestion(btn) {
  inputEl.value = btn.textContent;
  sendMessage();
}

async function sendMessage() {
  const text = inputEl.value.trim();
  if (!text || isWaiting) return;

  removeWelcome();
  isWaiting = true;
  sendBtn.disabled = true;
  inputEl.value = "";
  autoResizeInput();

  appendMessage("user", text, [], Date.now());

  const typingEl = appendTyping();

  try {
    const res = await fetch(`${API_BASE}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, session_id: sessionId }),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || "Server error");
    }

    const data = await res.json();

    // Persist session
    sessionId = data.session_id;
    localStorage.setItem("tuktuk_session_id", sessionId);

    typingEl.remove();
    appendMessage("bot", data.answer, data.images || [], Date.now());

  } catch (err) {
    typingEl.remove();
    appendMessage(
      "bot",
      `Sorry, I'm having trouble connecting right now. Please try again or fill out the contact form below.`,
      [],
      Date.now()
    );
    console.error("Chat error:", err);
  } finally {
    isWaiting = false;
    sendBtn.disabled = false;
    inputEl.focus();
  }
}

// ---------------------------------------------------------------------------
// Rendering helpers
// ---------------------------------------------------------------------------
function appendMessage(role, text, images, timestamp) {
  const msg = document.createElement("div");
  msg.className = `message ${role}`;

  const bubble = document.createElement("div");
  bubble.className = "message-bubble";
  bubble.innerHTML = formatText(text);

  msg.appendChild(bubble);

  // Render images below the text bubble
  if (images && images.length > 0) {
    const imgRow = document.createElement("div");
    imgRow.className = "message-images";
    images.forEach(src => {
      const img = document.createElement("img");
      img.className = "message-img";
      img.src = src;
      img.alt = "Tour image";
      img.loading = "lazy";
      img.addEventListener("click", () => openLightbox(src));
      imgRow.appendChild(img);
    });
    msg.appendChild(imgRow);
  }

  // Timestamp
  const meta = document.createElement("div");
  meta.className = "message-meta";
  meta.textContent = formatTime(timestamp);
  msg.appendChild(meta);

  messagesEl.appendChild(msg);
  scrollToBottom();
  return msg;
}

function appendTyping() {
  const msg = document.createElement("div");
  msg.className = "message bot typing-indicator";
  msg.innerHTML = `
    <div class="message-bubble">
      <span class="typing-dot"></span>
      <span class="typing-dot"></span>
      <span class="typing-dot"></span>
    </div>`;
  messagesEl.appendChild(msg);
  scrollToBottom();
  return msg;
}

function formatText(text) {
  // Extract links before HTML-escaping so the markup isn't corrupted
  const linkPlaceholders = [];
  text = text.replace(/\[([^\]]+)\]\((https?:\/\/[^\)]+)\)/g, (_, label, url) => {
    const idx = linkPlaceholders.length;
    linkPlaceholders.push(`<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`);
    return `\x00LINK${idx}\x00`;
  });

  // HTML-escape the rest
  text = text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    // Bold
    .replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>")
    // Italic
    .replace(/\*(.*?)\*/g, "<em>$1</em>")
    // Bullet lists (lines starting with - or •)
    .replace(/^[\-•]\s+(.+)$/gm, "<li>$1</li>")
    .replace(/(<li>.*<\/li>)/s, "<ul>$1</ul>")
    // Newlines → <br>
    .replace(/\n/g, "<br>");

  // Restore links
  text = text.replace(/\x00LINK(\d+)\x00/g, (_, i) => linkPlaceholders[+i]);
  return text;
}

function formatTime(ts) {
  return new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function scrollToBottom() {
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

// ---------------------------------------------------------------------------
// Input handling
// ---------------------------------------------------------------------------
function onKeyDown(e) {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
}

function autoResizeInput() {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(inputEl.scrollHeight, 120) + "px";
}

// ---------------------------------------------------------------------------
// Lightbox
// ---------------------------------------------------------------------------
function openLightbox(src) {
  lightboxImg.src = src;
  lightbox.classList.add("open");
}

function closeLightbox() {
  lightbox.classList.remove("open");
  lightboxImg.src = "";
}
