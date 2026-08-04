// Agentry Web UI — minimal chat over OpenAI-shape SSE.
// Pared down from the NoLlama UI (temp slider and no-think toggle removed),
// then re-lit as the backends grew up:
// - image attach/paste/drop -> OpenAI image_url data: URI parts (copilot
//   forwards them to the model; one image per message — the free-tier
//   models accept max_prompt_images=1)
// - live thinking: the server streams reasoning summaries as
//   delta.reasoning_content; the UI folds them into the think-block.

const chat = document.getElementById('chat');
const input = document.getElementById('message-input');
const sendBtn = document.getElementById('send-btn');
const modelSelect = document.getElementById('model-select');
const reasoningSelect = document.getElementById('reasoning-select');
const statusDot = document.getElementById('status-dot');
const newChatBtn = document.getElementById('new-chat-btn');
const attachBtn = document.getElementById('attach-btn');
const fileInput = document.getElementById('file-input');
const imagePreview = document.getElementById('image-preview');
const imagePreviewImg = document.getElementById('image-preview-img');
const imageRemove = document.getElementById('image-remove');
const dropOverlay = document.getElementById('drop-overlay');

let chatHistory = [];
let thinkExpanded = false;
let isGenerating = false;
let abortController = null;
let pendingImage = null;   // data: URI of the image attached to the next message

const MAX_IMAGE_BYTES = 3 * 1024 * 1024;   // backend vision limit (3 MB)

// --- Image attach ---

function setImage(file) {
    if (!file || !file.type.startsWith('image/')) return;
    if (file.size > MAX_IMAGE_BYTES) {
        alert(`Image too large (${(file.size / 1048576).toFixed(1)} MB > 3 MB limit)`);
        return;
    }
    const reader = new FileReader();
    reader.onload = () => {
        pendingImage = reader.result;
        imagePreviewImg.src = pendingImage;
        imagePreview.style.display = 'block';
        input.focus();
    };
    reader.readAsDataURL(file);
}

function clearImage() {
    pendingImage = null;
    imagePreviewImg.src = '';
    imagePreview.style.display = 'none';
}

function shouldAutoScroll() {
    return chat.scrollHeight - chat.scrollTop - chat.clientHeight < 80;
}

function scrollToBottom() {
    if (shouldAutoScroll()) chat.scrollTop = chat.scrollHeight;
}

// --- Init ---

async function init() {
    await checkHealth();
    await loadModels();
    setInterval(checkHealth, 15000);
    input.focus();
}

async function checkHealth() {
    try {
        const resp = await fetch('/health');
        const data = await resp.json();
        statusDot.className = 'status-dot ' + data.status;
        statusDot.title = data.status;
    } catch {
        statusDot.className = 'status-dot error';
        statusDot.title = 'disconnected';
    }
}

async function loadModels() {
    try {
        const resp = await fetch('/v1/models');
        const data = await resp.json();
        modelSelect.innerHTML = '';
        for (const m of data.data) {
            const opt = document.createElement('option');
            opt.value = m.id;
            const parts = m.id.split('@');
            const name = parts[0];
            const device = parts[1] || (m.owned_by || '').replace('local-', '').toUpperCase();
            opt.textContent = device ? `${name} (${device})` : name;
            modelSelect.appendChild(opt);
        }
    } catch {}
}

// --- Request ---

function buildRequestBody() {
    return {
        model: modelSelect.value,
        messages: [...chatHistory],
        stream: true,
        max_tokens: 16384,
        reasoning_effort: reasoningSelect.value,
    };
}

// --- Chat rendering ---

function addMessage(role, content, meta, rawHtml = false) {
    const div = document.createElement('div');
    div.className = `message ${role}`;
    div.innerHTML = rawHtml ? content : renderMarkdown(content);
    if (meta) {
        const metaDiv = document.createElement('div');
        metaDiv.className = 'meta';
        metaDiv.innerHTML = meta;
        div.appendChild(metaDiv);
    }
    chat.appendChild(div);
    scrollToBottom();
    return div;
}

function renderMarkdown(text, isStreaming) {
    let thinkHtml = '';
    let mainText = text;

    const thinkMatch = text.match(/^<think>([\s\S]*?)<\/think>\s*([\s\S]*)$/);
    const thinkOpen = !thinkMatch && text.match(/^<think>([\s\S]*)$/);
    const thinkStarting = !thinkMatch && !thinkOpen && /^<(?:t(?:h(?:i(?:n(?:k)?)?)?)?)?$/.test(text.trim());

    if (thinkMatch) {
        const thinkContent = thinkMatch[1].trim();
        mainText = thinkMatch[2].trim();
        if (thinkContent) {
            const lines = thinkContent.split('\n');
            const preview = lines.slice(-3).join('\n');
            const cls = thinkExpanded ? '' : 'collapsed';
            thinkHtml = `<div class="think-block ${cls}" data-think-toggle>
                <div class="think-header">Thinking... <span class="think-toggle">(click to expand)</span></div>
                <div class="think-full">${escapeHtml(thinkContent).replace(/\n/g, '<br>')}</div>
                <div class="think-preview">${escapeHtml(preview).replace(/\n/g, '<br>')}</div>
            </div>`;
        }
    } else if (thinkOpen) {
        const thinkContent = thinkOpen[1].trim();
        if (thinkContent) {
            const lines = thinkContent.split('\n');
            if (lines.length > 4) {
                const preview = lines.slice(-4).join('\n');
                const cls = thinkExpanded ? '' : 'collapsed';
                thinkHtml = `<div class="think-block streaming ${cls}" data-think-toggle>
                    <div class="think-header">Thinking... <span class="think-toggle">(click to expand)</span></div>
                    <div class="think-full">${escapeHtml(thinkContent).replace(/\n/g, '<br>')}</div>
                    <div class="think-preview">${escapeHtml(preview).replace(/\n/g, '<br>')}</div>
                </div>`;
            } else {
                thinkHtml = `<div class="think-block streaming">
                    <div class="think-header">Thinking...</div>
                    <div class="think-preview">${escapeHtml(thinkContent).replace(/\n/g, '<br>')}</div>
                </div>`;
            }
        } else {
            thinkHtml = `<div class="think-block streaming"><div class="think-header">Thinking...</div></div>`;
        }
        mainText = '';
    } else if (thinkStarting && isStreaming) {
        thinkHtml = `<div class="think-block streaming"><div class="think-header">Thinking...</div></div>`;
        mainText = '';
    }

    let html = escapeHtml(mainText);
    // Pull code fences out before the inline transforms so *, ** and `
    // inside code survive verbatim (matters doubly now that artifacts
    // re-read the fence source via textContent).
    const fences = [];
    const stashFence = (lang, code, title) => {
        const l = (lang || '').toLowerCase();
        let artifact = '';
        if (ARTIFACT_LANGS.has(l)) {
            const safe = title ? title.replace(/"/g, '&quot;') : '';
            const verb = (l === 'javascript' || l === 'js') ? 'run' : 'open';
            artifact = `<button class="artifact-btn" data-lang="${l}"`
                + (safe ? ` data-title="${safe}"` : '')
                + ` onclick="openArtifact(this)">${verb} &#9656;${safe ? ' ' + safe : ''}</button>`;
        }
        fences.push(`<pre><code>${code.trim()}</code><button class="copy-btn" onclick="copyCode(this)">copy</button>${artifact}</pre>`);
        return `\x00F${fences.length - 1}\x00`;
    };
    // Titled artifacts: YAML frontmatter immediately before a fence (see
    // .github/copilot-instructions.md). The frontmatter is consumed — it
    // labels the button and panel instead of rendering as text.
    html = html.replace(/(?:^|\n)---\n([^`]{0,300}?)\n---\n```(\w*)\n([\s\S]*?)```/g,
        (m, yaml, lang, code) => {
            const t = yaml.match(/^title:\s*["']?(.+?)["']?\s*$/m);
            if (!t) return m;
            return '\n' + stashFence(lang, code, t[1]);
        });
    html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => stashFence(lang, code));
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
    html = html.replace(/\n/g, '<br>');
    html = html.replace(/\x00F(\d+)\x00/g, (_, i) => fences[i]);

    return thinkHtml + html;
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function copyCode(btn) {
    const code = btn.parentElement.querySelector('code').textContent;
    navigator.clipboard.writeText(code);
    btn.textContent = 'copied';
    setTimeout(() => btn.textContent = 'copy', 1500);
}
window.copyCode = copyCode;

// --- Artifacts: render a fenced block as a live document ---

const ARTIFACT_LANGS = new Set(['html', 'svg', 'markdown', 'md', 'javascript', 'js']);

// Runner harness for javascript artifacts: executes the snippet in the
// sandboxed iframe and mirrors console output / errors into the page, so
// "open" shows the program's output instead of a blank document.
function jsRunnerDoc(code) {
    // </script> inside the snippet would terminate the harness script tag
    const safe = code.replace(/<\/script/gi, '<\\/script');
    return `<!doctype html><meta charset="utf-8">
<style>
  body { margin: 0; background: #fff; }
  #out { font: 13px/1.5 "Cascadia Code", "Fira Code", monospace;
         padding: 14px 18px; white-space: pre-wrap; word-break: break-word;
         color: #1a1a2a; }
  .err { color: #b91c1c; }
</style>
<pre id="out"></pre>
<script>
  const out = document.getElementById('out');
  const show = (cls, args) => {
    const span = document.createElement('span');
    if (cls) span.className = cls;
    span.textContent = args.map(a => {
      if (typeof a === 'string') return a;
      try { return JSON.stringify(a, null, 2); } catch { return String(a); }
    }).join(' ') + '\\n';
    out.appendChild(span);
  };
  for (const k of ['log', 'info', 'warn', 'debug'])
    console[k] = (...a) => show(k === 'warn' ? 'err' : '', a);
  console.error = (...a) => show('err', a);
  window.onerror = (msg, src, line) => { show('err', ['Error: ' + msg + ' (line ' + line + ')']); };
  window.addEventListener('unhandledrejection', e => show('err', ['Unhandled rejection: ' + e.reason]));
<\/script>
<script>${safe}<\/script>`;
}
const artifactPanel = document.getElementById('artifact-panel');
const artifactTitle = document.getElementById('artifact-title');
const artifactBody = document.getElementById('artifact-body');
const artifactClose = document.getElementById('artifact-close');

function openArtifact(btn) {
    // textContent un-escapes the entities renderMarkdown put in the <code>
    const source = btn.parentElement.querySelector('code').textContent;
    const lang = btn.dataset.lang;
    artifactBody.innerHTML = '';
    if (lang === 'html' || lang === 'svg' || lang === 'javascript' || lang === 'js') {
        const iframe = document.createElement('iframe');
        // scripts may run; the artifact stays cross-origin to the app
        // (no allow-same-origin), so it can't touch the chat page.
        iframe.setAttribute('sandbox', 'allow-scripts');
        iframe.srcdoc = (lang === 'javascript' || lang === 'js')
            ? jsRunnerDoc(source) : source;
        artifactBody.appendChild(iframe);
    } else {
        const doc = document.createElement('div');
        doc.className = 'artifact-doc';
        doc.innerHTML = mdToHtml(source);
        artifactBody.appendChild(doc);
    }
    artifactTitle.textContent = btn.dataset.title || `Artifact (${lang})`;
    artifactPanel.classList.add('active');
}
window.openArtifact = openArtifact;

function closeArtifact() {
    artifactPanel.classList.remove('active');
    artifactBody.innerHTML = '';
}

// Small markdown-document renderer for markdown artifacts. Deliberately
// modest: headers, lists, quotes, hr, tables, links, code — not CommonMark.
function mdToHtml(text) {
    // A markdown artifact may carry its own frontmatter inside the fence;
    // it's metadata, not document content.
    text = text.replace(/^---\n[\s\S]*?\n---\n/, '');
    let h = escapeHtml(text);
    const fences = [];
    h = h.replace(/```(\w*)\n([\s\S]*?)```/g, (_, l, c) => {
        fences.push(`<pre><code>${c.trim()}</code></pre>`);
        return `\x00F${fences.length - 1}\x00`;
    });
    h = h.replace(/^(#{1,6}) (.+)$/gm,
        (_, hashes, t) => `<h${hashes.length}>${t}</h${hashes.length}>`);
    h = h.replace(/^&gt; ?(.*)$/gm, '<blockquote>$1</blockquote>');
    h = h.replace(/<\/blockquote>\n<blockquote>/g, '<br>');
    h = h.replace(/^ {0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/gm, '<hr>');
    // tables: header | separator | rows
    h = h.replace(/^\|(.+)\|\n\|[\s\-:|]+\|\n((?:\|.*\|\n?)*)/gm, (_, head, rows) => {
        const th = head.split('|').map(c => `<th>${c.trim()}</th>`).join('');
        const trs = rows.trim().split('\n').filter(Boolean).map(r =>
            `<tr>${r.replace(/^\||\|$/g, '').split('|').map(c => `<td>${c.trim()}</td>`).join('')}</tr>`
        ).join('');
        return `<table><tr>${th}</tr>${trs}</table>`;
    });
    h = h.replace(/^[-*+] (.+)$/gm, '<li data-l="ul">$1</li>');
    h = h.replace(/^\d+[.)] (.+)$/gm, '<li data-l="ol">$1</li>');
    h = h.replace(/(?:<li data-l="(ul|ol)">[\s\S]*?<\/li>\n?)+/g,
        (m) => `<${m.includes('data-l="ol"') ? 'ol' : 'ul'}>${m.replace(/ data-l="(?:ul|ol)"/g, '')}</${m.includes('data-l="ol"') ? 'ol' : 'ul'}>`);
    h = h.replace(/`([^`]+)`/g, '<code>$1</code>');
    h = h.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    h = h.replace(/\*([^*\n]+)\*/g, '<em>$1</em>');
    h = h.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    h = h.replace(/\x00F(\d+)\x00/g, (_, i) => fences[i]);
    // paragraphs: blank-line-separated runs that aren't already block elements
    return h.split(/\n{2,}/).map(b => {
        b = b.trim();
        if (!b) return '';
        return /^<(h\d|ul|ol|pre|blockquote|hr|table)/.test(b)
            ? b : `<p>${b.replace(/\n/g, '<br>')}</p>`;
    }).join('\n');
}

// --- Send / receive ---

async function sendMessage() {
    const text = input.value.trim();
    if (!text && !pendingImage) return;
    if (isGenerating) return;
    thinkExpanded = false;

    let userHtml = escapeHtml(text).replace(/\n/g, '<br>');
    if (pendingImage) {
        userHtml += `<img class="attached" src="${pendingImage}" alt="attached image">`;
        chatHistory.push({ role: 'user', content: [
            { type: 'text', text: text },
            { type: 'image_url', image_url: { url: pendingImage } },
        ]});
    } else {
        chatHistory.push({ role: 'user', content: text });
    }
    addMessage('user', userHtml, null, true);   // userHtml is pre-escaped
    clearImage();

    input.value = '';
    input.style.height = 'auto';

    const assistantDiv = addMessage('assistant', '');
    assistantDiv.innerHTML = '<span class="typing-indicator"></span>';
    isGenerating = true;
    sendBtn.disabled = true;
    const t0 = performance.now();

    try {
        abortController = new AbortController();
        const resp = await fetch('/v1/chat/completions', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            signal: abortController.signal,
            body: JSON.stringify(buildRequestBody()),
        });

        const device = resp.headers.get('X-Device') || '';

        if (!resp.ok) {
            const err = await resp.json();
            assistantDiv.innerHTML = `<span style="color:var(--error)">${escapeHtml(err.error?.message || 'Error')}</span>`;
            return;
        }

        const contentType = resp.headers.get('content-type') || '';
        if (contentType.includes('text/event-stream')) {
            let fullText = '';
            let reasoningText = '';
            // Fold streamed reasoning into the <think> convention the
            // renderer already speaks: open while only reasoning has
            // arrived, closed once (or when) the answer starts.
            const displayText = (streaming) => {
                if (!reasoningText) return fullText;
                const closed = fullText || !streaming;
                return `<think>${reasoningText}${closed ? '</think>' : ''}${fullText}`;
            };
            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop();
                for (const line of lines) {
                    if (!line.startsWith('data: ')) continue;
                    const data = line.slice(6);
                    if (data === '[DONE]') continue;
                    try {
                        const chunk = JSON.parse(data);
                        const d = chunk.choices?.[0]?.delta || {};
                        if (d.content) fullText += d.content;
                        if (d.reasoning_content) reasoningText += d.reasoning_content;
                        if (d.content || d.reasoning_content) {
                            assistantDiv.innerHTML = renderMarkdown(displayText(true), true);
                            scrollToBottom();
                        }
                    } catch {}
                }
            }

            assistantDiv.innerHTML = renderMarkdown(displayText(false), false);
            const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
            const metaDiv = document.createElement('div');
            metaDiv.className = 'meta';
            metaDiv.innerHTML = device
                ? `<span class="device-tag">${device}</span> ${elapsed}s`
                : `${elapsed}s`;
            assistantDiv.appendChild(metaDiv);
            chatHistory.push({ role: 'assistant', content: fullText });
        } else {
            const data = await resp.json();
            const replyText = data.choices?.[0]?.message?.content || '';
            assistantDiv.innerHTML = renderMarkdown(replyText);
            const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
            const metaDiv = document.createElement('div');
            metaDiv.className = 'meta';
            metaDiv.innerHTML = device
                ? `<span class="device-tag">${device}</span> ${elapsed}s`
                : `${elapsed}s`;
            assistantDiv.appendChild(metaDiv);
            chatHistory.push({ role: 'assistant', content: replyText });
        }
    } catch (err) {
        if (err.name === 'AbortError') {
            assistantDiv.innerHTML += '<br><span style="color:var(--text-dim)">[cancelled]</span>';
        } else {
            assistantDiv.innerHTML = `<span style="color:var(--error)">${escapeHtml(err.message)}</span>`;
        }
    } finally {
        isGenerating = false;
        sendBtn.disabled = false;
        abortController = null;
        input.focus();
    }
}

function newChat() {
    chatHistory = [];
    chat.innerHTML = '';
    input.focus();
}

// --- Event wiring ---

chat.addEventListener('click', (e) => {
    const thinkBlock = e.target.closest('[data-think-toggle]');
    if (thinkBlock) {
        thinkExpanded = !thinkExpanded;
        thinkBlock.classList.toggle('collapsed');
    }
});

sendBtn.addEventListener('click', sendMessage);
input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
});

input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 150) + 'px';
});

newChatBtn.addEventListener('click', newChat);

// --- Image attach wiring ---

attachBtn.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', () => {
    setImage(fileInput.files[0]);
    fileInput.value = '';
});
imageRemove.addEventListener('click', clearImage);

document.addEventListener('paste', (e) => {
    for (const item of e.clipboardData?.items || []) {
        if (item.type.startsWith('image/')) {
            e.preventDefault();
            setImage(item.getAsFile());
            return;
        }
    }
});

let dragDepth = 0;
window.addEventListener('dragenter', (e) => {
    if ([...(e.dataTransfer?.types || [])].includes('Files')) {
        dragDepth++;
        dropOverlay.classList.add('active');
    }
});
window.addEventListener('dragleave', () => {
    if (--dragDepth <= 0) {
        dragDepth = 0;
        dropOverlay.classList.remove('active');
    }
});
window.addEventListener('dragover', (e) => e.preventDefault());
window.addEventListener('drop', (e) => {
    e.preventDefault();
    dragDepth = 0;
    dropOverlay.classList.remove('active');
    setImage(e.dataTransfer?.files?.[0]);
});

artifactClose.addEventListener('click', closeArtifact);

document.addEventListener('keydown', (e) => {
    if (e.ctrlKey && e.key === 'n') {
        e.preventDefault();
        newChat();
    }
    if (e.key === 'Escape') {
        if (isGenerating && abortController) {
            abortController.abort();
            fetch('/v1/cancel', { method: 'POST' }).catch(() => {});
        } else if (artifactPanel.classList.contains('active')) {
            closeArtifact();
        }
    }
});

init();
