/**
 * widget.js — Self-contained embeddable chat widget
 *
 * Served by GET /widget.js with __API_BASE__ replaced by the Railway URL.
 * Usage on any page: <script src="https://your-app.up.railway.app/widget.js"></script>
 *
 * The script injects its own <style> and DOM so it won't conflict with the
 * host page. All CSS is scoped under #tuktuk-widget-root.
 */
(function () {
  'use strict';

  const API_BASE = '__API_BASE__';

  // ------------------------------------------------------------------
  // Inject scoped CSS
  // ------------------------------------------------------------------
  const STYLE = `
    @import url('https://fonts.googleapis.com/css2?family=Montserrat:wght@400;500;600;700&display=swap');

    #tuktuk-widget-root *,
    #tuktuk-widget-root *::before,
    #tuktuk-widget-root *::after {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      font-family: 'Montserrat', sans-serif;
    }

    #tuktuk-widget-root {
      --tw-orange:      #c8963e;
      --tw-orange-dark: #b07e2a;
      --tw-bg-dark:     #0a0a0a;
      --tw-bg-mid:      #141414;
      --tw-bg-light:    #1e1e1e;
      --tw-bg-input:    #111111;
      --tw-white:       #ffffff;
      --tw-text-muted:  rgba(255,255,255,0.45);
      --tw-text-dim:    rgba(255,255,255,0.65);
      --tw-border:      rgba(255,255,255,0.1);
      --tw-border-focus:rgba(200,150,62,0.6);
      --tw-radius-sm:   4px;
      --tw-radius-md:   8px;
      --tw-shadow-md:   0 8px 32px rgba(0,0,0,0.6);
    }

    #tuktuk-widget-root .tw-widget {
      background: var(--tw-bg-dark);
      border: 1px solid var(--tw-border);
      border-radius: var(--tw-radius-md);
      box-shadow: var(--tw-shadow-md);
      overflow: hidden;
      display: flex;
      flex-direction: column;
      height: 560px;
      margin-bottom: 2rem;
      width: 100%;
    }

    /* Header */
    #tuktuk-widget-root .tw-header {
      background: var(--tw-bg-mid);
      border-bottom: 1px solid var(--tw-border);
      padding: 1rem 1.25rem;
      display: flex;
      align-items: center;
      gap: 0.75rem;
      flex-shrink: 0;
    }
    #tuktuk-widget-root .tw-avatar {
      width: 36px; height: 36px;
      border-radius: 50%;
      background: var(--tw-orange);
      display: flex; align-items: center; justify-content: center;
      font-size: 1rem; flex-shrink: 0;
    }
    #tuktuk-widget-root .tw-header-info { flex: 1; }
    #tuktuk-widget-root .tw-header-name {
      font-weight: 700;
      color: var(--tw-white);
      font-size: 0.8rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }
    #tuktuk-widget-root .tw-header-status {
      font-size: 0.7rem; color: var(--tw-text-muted);
      display: flex; align-items: center; gap: 0.35rem;
      margin-top: 0.2rem;
    }
    #tuktuk-widget-root .tw-status-dot {
      width: 6px; height: 6px; border-radius: 50%;
      background: #4ade80; display: inline-block;
    }
    #tuktuk-widget-root .tw-clear-btn {
      margin-left: auto;
      background: transparent;
      border: 1px solid var(--tw-border);
      color: var(--tw-text-muted);
      border-radius: 2px;
      padding: 0.3rem 0.75rem;
      font-size: 0.68rem;
      font-weight: 600;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      cursor: pointer;
      transition: border-color 0.15s, color 0.15s;
      white-space: nowrap;
    }
    #tuktuk-widget-root .tw-clear-btn:hover {
      border-color: var(--tw-orange);
      color: var(--tw-orange);
    }

    /* Messages */
    #tuktuk-widget-root .tw-messages {
      flex: 1; overflow-y: auto;
      padding: 1.25rem;
      display: flex; flex-direction: column; gap: 1rem;
      background: var(--tw-bg-dark);
      scroll-behavior: smooth;
    }
    #tuktuk-widget-root .tw-messages::-webkit-scrollbar { width: 4px; }
    #tuktuk-widget-root .tw-messages::-webkit-scrollbar-track { background: transparent; }
    #tuktuk-widget-root .tw-messages::-webkit-scrollbar-thumb {
      background: var(--tw-border); border-radius: 10px;
    }

    #tuktuk-widget-root .tw-msg {
      display: flex; flex-direction: column; max-width: 80%;
      animation: twFadeIn 0.2s ease;
    }
    @keyframes twFadeIn {
      from { opacity: 0; transform: translateY(6px); }
      to   { opacity: 1; transform: translateY(0); }
    }
    #tuktuk-widget-root .tw-msg.user { align-self: flex-end; align-items: flex-end; }
    #tuktuk-widget-root .tw-msg.bot  { align-self: flex-start; align-items: flex-start; }

    #tuktuk-widget-root .tw-bubble {
      padding: 0.75rem 1rem;
      border-radius: var(--tw-radius-sm);
      font-size: 0.875rem; line-height: 1.6;
      word-break: break-word;
    }
    #tuktuk-widget-root .tw-msg.user .tw-bubble {
      background: var(--tw-orange);
      color: var(--tw-white);
    }
    #tuktuk-widget-root .tw-msg.bot .tw-bubble {
      background: var(--tw-bg-light);
      color: var(--tw-white);
      border: 1px solid var(--tw-border);
    }
    #tuktuk-widget-root .tw-msg.bot .tw-bubble a {
      color: var(--tw-orange); font-weight: 600;
      text-decoration: underline; text-underline-offset: 2px;
    }
    #tuktuk-widget-root .tw-msg.bot .tw-bubble a:hover { color: var(--tw-orange-dark); }

    #tuktuk-widget-root .tw-meta {
      font-size: 0.65rem; color: var(--tw-text-muted);
      margin-top: 0.3rem; padding: 0 0.25rem;
      letter-spacing: 0.03em;
    }

    /* Images */
    #tuktuk-widget-root .tw-images {
      display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 0.6rem;
    }
    #tuktuk-widget-root .tw-img {
      max-width: 220px; max-height: 160px;
      border-radius: var(--tw-radius-sm);
      object-fit: cover; cursor: pointer;
      transition: opacity 0.15s;
      border: 1px solid var(--tw-border);
    }
    #tuktuk-widget-root .tw-img:hover { opacity: 0.85; }

    /* Typing dots */
    #tuktuk-widget-root .tw-typing .tw-bubble {
      display: flex; align-items: center; gap: 5px;
    }
    #tuktuk-widget-root .tw-dot {
      width: 6px; height: 6px; border-radius: 50%;
      background: var(--tw-text-muted);
      animation: twBounce 1.2s infinite;
    }
    #tuktuk-widget-root .tw-dot:nth-child(2) { animation-delay: 0.2s; }
    #tuktuk-widget-root .tw-dot:nth-child(3) { animation-delay: 0.4s; }
    @keyframes twBounce {
      0%, 60%, 100% { transform: translateY(0); }
      30%            { transform: translateY(-5px); }
    }

    /* Welcome */
    #tuktuk-widget-root .tw-welcome {
      text-align: center; padding: 2rem 1rem; color: var(--tw-text-dim);
    }
    #tuktuk-widget-root .tw-wave { font-size: 1.8rem; margin-bottom: 0.75rem; }
    #tuktuk-widget-root .tw-welcome-title {
      font-size: 0.75rem;
      font-weight: 700;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      color: var(--tw-white);
      margin-bottom: 0.5rem;
    }
    #tuktuk-widget-root .tw-welcome p {
      font-size: 0.82rem; line-height: 1.6;
      color: var(--tw-text-dim);
    }

    /* Input */
    #tuktuk-widget-root .tw-input-area {
      padding: 1rem 1.25rem 1rem;
      background: var(--tw-bg-mid);
      border-top: 1px solid var(--tw-border);
      flex-shrink: 0;
    }
    #tuktuk-widget-root .tw-input-row {
      display: flex; gap: 0; align-items: flex-end;
      border-bottom: 1px solid var(--tw-border);
      transition: border-color 0.2s;
    }
    #tuktuk-widget-root .tw-input-row:focus-within {
      border-bottom-color: var(--tw-orange);
    }
    #tuktuk-widget-root .tw-textarea {
      flex: 1;
      border: none;
      background: transparent;
      padding: 0.65rem 0.5rem 0.65rem 0;
      font-size: 0.875rem;
      color: var(--tw-white);
      resize: none;
      min-height: 44px;
      max-height: 120px;
      line-height: 1.5;
      overflow-y: auto;
    }
    #tuktuk-widget-root .tw-textarea:focus { outline: none; }
    #tuktuk-widget-root .tw-textarea::placeholder { color: var(--tw-text-muted); }

    #tuktuk-widget-root .tw-send {
      width: 36px; height: 36px;
      background: transparent;
      border: none;
      cursor: pointer;
      display: flex; align-items: center; justify-content: center;
      flex-shrink: 0;
      transition: opacity 0.15s;
      padding-bottom: 0.5rem;
    }
    #tuktuk-widget-root .tw-send:hover:not(:disabled) { opacity: 0.7; }
    #tuktuk-widget-root .tw-send:disabled { opacity: 0.25; cursor: not-allowed; }
    #tuktuk-widget-root .tw-send svg { width: 18px; height: 18px; fill: var(--tw-orange); }

    #tuktuk-widget-root .tw-footer {
      font-size: 0.62rem;
      color: var(--tw-text-muted);
      text-align: center;
      margin-top: 0.6rem;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }

    /* Lightbox */
    #tuktuk-lightbox {
      display: none; position: fixed; inset: 0;
      background: rgba(0,0,0,0.92); z-index: 99999;
      align-items: center; justify-content: center;
    }
    #tuktuk-lightbox.open { display: flex; }
    #tuktuk-lightbox img {
      max-width: 90vw; max-height: 90vh;
      border-radius: 4px; box-shadow: 0 20px 60px rgba(0,0,0,0.8);
    }
    #tuktuk-lightbox .tw-lb-close {
      position: absolute; top: 1.5rem; right: 1.5rem;
      background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.15);
      color: white; width: 40px; height: 40px; border-radius: 2px;
      font-size: 1rem; cursor: pointer;
      display: flex; align-items: center; justify-content: center;
    }
    #tuktuk-lightbox .tw-lb-close:hover { background: rgba(255,255,255,0.15); }

    @media (max-width: 600px) {
      #tuktuk-widget-root .tw-widget { height: 480px; border-radius: var(--tw-radius-sm); }
      #tuktuk-widget-root .tw-msg { max-width: 90%; }
    }
  `;

  // ------------------------------------------------------------------
  // Inject style tag
  // ------------------------------------------------------------------
  const styleEl = document.createElement('style');
  styleEl.textContent = STYLE;
  document.head.appendChild(styleEl);

  // ------------------------------------------------------------------
  // Build DOM
  // ------------------------------------------------------------------
  const root = document.createElement('div');
  root.id = 'tuktuk-widget-root';
  root.innerHTML = `
    <div class="tw-widget">
      <div class="tw-header">
        <div class="tw-avatar">🛺</div>
        <div class="tw-header-info">
          <div class="tw-header-name">I Took a Tuk Tuk</div>
          <div class="tw-header-status">
            <span class="tw-status-dot"></span>
            Online — usually replies instantly
          </div>
        </div>
        <button class="tw-clear-btn" id="twClearBtn">New chat</button>
      </div>

      <div class="tw-messages" id="twMessages"></div>

      <div class="tw-input-area">
        <div class="tw-input-row">
          <textarea
            class="tw-textarea"
            id="twInput"
            placeholder="Ask about our tours, prices, availability…"
            rows="1"
          ></textarea>
          <button class="tw-send" id="twSend" aria-label="Send">
            <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
              <path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/>
            </svg>
          </button>
        </div>
        <div class="tw-footer">Powered by AI · I Took a Tuk Tuk</div>
      </div>
    </div>
  `;

  // Insert the widget wherever the <script> tag is
  const currentScript = document.currentScript;
  if (currentScript && currentScript.parentNode) {
    currentScript.parentNode.insertBefore(root, currentScript);
  } else {
    document.body.appendChild(root);
  }

  // Lightbox (appended to body so it overlays everything)
  const lightbox = document.createElement('div');
  lightbox.id = 'tuktuk-lightbox';
  lightbox.innerHTML = `
    <button class="tw-lb-close" id="twLbClose">✕</button>
    <img id="twLbImg" src="" alt="">
  `;
  document.body.appendChild(lightbox);

  // ------------------------------------------------------------------
  // References
  // ------------------------------------------------------------------
  const messagesEl = document.getElementById('twMessages');
  const inputEl    = document.getElementById('twInput');
  const sendBtn    = document.getElementById('twSend');
  const clearBtn   = document.getElementById('twClearBtn');
  const lbImg      = document.getElementById('twLbImg');
  const lbClose    = document.getElementById('twLbClose');

  let sessionId = '';
  try { sessionId = localStorage.getItem('tuktuk_session_id') || ''; } catch (_) {}
  let isWaiting = false;

  // ------------------------------------------------------------------
  // Welcome screen
  // ------------------------------------------------------------------
  function renderWelcome() {
    const el = document.createElement('div');
    el.className = 'tw-welcome';
    el.id = 'twWelcome';
    el.innerHTML = `
      <div class="tw-wave">🛺</div>
      <div class="tw-welcome-title">I Took a Tuk Tuk</div>
      <p>Hello! I'm your I Took a TukTuk assistant.<br>Ask me anything about our tours, prices or custom services.</p>
    `;
    messagesEl.appendChild(el);
  }

  function removeWelcome() {
    const el = document.getElementById('twWelcome');
    if (el) el.remove();
  }

  function clearConversation() {
    sessionId = '';
    try { localStorage.removeItem('tuktuk_session_id'); } catch (_) {}
    messagesEl.innerHTML = '';
    renderWelcome();
  }

  renderWelcome();

  // ------------------------------------------------------------------
  // Sending
  // ------------------------------------------------------------------
  async function sendMessage() {
    const text = inputEl.value.trim();
    if (!text || isWaiting) return;

    removeWelcome();
    isWaiting = true;
    sendBtn.disabled = true;
    inputEl.value = '';
    autoResize();

    appendMessage('user', text, []);
    const typingEl = appendTyping();

    try {
      const res = await fetch(`${API_BASE}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text, session_id: sessionId }),
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail || 'Server error');
      }

      const data = await res.json();
      sessionId = data.session_id;
      try { localStorage.setItem('tuktuk_session_id', sessionId); } catch (_) {}

      typingEl.remove();
      appendMessage('bot', data.answer, data.images || []);

    } catch (err) {
      typingEl.remove();
      appendMessage('bot', 'Sorry, I\'m having trouble connecting right now. Please try again or fill out the contact form below.', []);
      console.error('[TukTuk widget]', err);
    } finally {
      isWaiting = false;
      sendBtn.disabled = false;
      inputEl.focus();
    }
  }

  // ------------------------------------------------------------------
  // Rendering helpers
  // ------------------------------------------------------------------
  function appendMessage(role, text, images) {
    const msg = document.createElement('div');
    msg.className = `tw-msg ${role}`;

    const bubble = document.createElement('div');
    bubble.className = 'tw-bubble';
    bubble.innerHTML = formatText(text);
    msg.appendChild(bubble);

    if (images && images.length > 0) {
      const row = document.createElement('div');
      row.className = 'tw-images';
      images.forEach(src => {
        const img = document.createElement('img');
        img.className = 'tw-img';
        img.src = src;
        img.alt = 'Tour image';
        img.loading = 'lazy';
        img.addEventListener('click', () => openLightbox(src));
        row.appendChild(img);
      });
      msg.appendChild(row);
    }

    const meta = document.createElement('div');
    meta.className = 'tw-meta';
    meta.textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    msg.appendChild(meta);

    messagesEl.appendChild(msg);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return msg;
  }

  function appendTyping() {
    const msg = document.createElement('div');
    msg.className = 'tw-msg bot tw-typing';
    msg.innerHTML = `<div class="tw-bubble"><span class="tw-dot"></span><span class="tw-dot"></span><span class="tw-dot"></span></div>`;
    messagesEl.appendChild(msg);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return msg;
  }

  function formatText(text) {
    const links = [];
    text = text.replace(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g, (_, label, url) => {
      const i = links.length;
      links.push(`<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`);
      return `\x00L${i}\x00`;
    });
    text = text
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
      .replace(/\*(.*?)\*/g, '<em>$1</em>')
      .replace(/^[\-•]\s+(.+)$/gm, '<li>$1</li>')
      .replace(/(<li>[\s\S]*<\/li>)/, '<ul>$1</ul>')
      .replace(/\n/g, '<br>');
    return text.replace(/\x00L(\d+)\x00/g, (_, i) => links[+i]);
  }

  // ------------------------------------------------------------------
  // Input handling
  // ------------------------------------------------------------------
  function autoResize() {
    inputEl.style.height = 'auto';
    inputEl.style.height = Math.min(inputEl.scrollHeight, 120) + 'px';
  }

  inputEl.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });
  inputEl.addEventListener('input', autoResize);
  sendBtn.addEventListener('click', sendMessage);
  clearBtn.addEventListener('click', clearConversation);

  // ------------------------------------------------------------------
  // Lightbox
  // ------------------------------------------------------------------
  function openLightbox(src) { lbImg.src = src; lightbox.classList.add('open'); }
  function closeLightbox()   { lightbox.classList.remove('open'); lbImg.src = ''; }

  lbClose.addEventListener('click', closeLightbox);
  lightbox.addEventListener('click', e => { if (e.target === lightbox) closeLightbox(); });

})();
