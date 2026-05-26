// Copilot Proxy Web UI — minimal chat over OpenAI-shape SSE.
// Pared down from the NoLlama UI: temp slider, no-think toggle, and image
// attach were removed because Copilot CLI's -p mode doesn't expose them.
// Think-block rendering is kept (inert when no <think> tags appear) so a
// future backend that does emit reasoning can light it up for free.

const chat = document.getElementById('chat');
const input = document.getElementById('message-input');
const sendBtn = document.getElementById('send-btn');
const modelSelect = document.getElementById('model-select');
const statusDot = document.getElementById('status-dot');
const newChatBtn = document.getElementById('new-chat-btn');

let chatHistory = [];
let thinkExpanded = false;
let isGenerating = false;
let abortController = null;

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
    };
}

// --- Chat rendering ---

function addMessage(role, content, meta) {
    const div = document.createElement('div');
    div.className = `message ${role}`;
    div.innerHTML = typeof content === 'string' ? renderMarkdown(content) : content;
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
    html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
        return `<pre><code>${code.trim()}</code><button class="copy-btn" onclick="copyCode(this)">copy</button></pre>`;
    });
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
    html = html.replace(/\n/g, '<br>');

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

// --- Send / receive ---

async function sendMessage() {
    const text = input.value.trim();
    if (!text) return;
    if (isGenerating) return;
    thinkExpanded = false;

    addMessage('user', escapeHtml(text).replace(/\n/g, '<br>'));
    chatHistory.push({ role: 'user', content: text });

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
                        const delta = chunk.choices?.[0]?.delta?.content;
                        if (delta) {
                            fullText += delta;
                            assistantDiv.innerHTML = renderMarkdown(fullText, true);
                            scrollToBottom();
                        }
                    } catch {}
                }
            }

            assistantDiv.innerHTML = renderMarkdown(fullText, false);
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

document.addEventListener('keydown', (e) => {
    if (e.ctrlKey && e.key === 'n') {
        e.preventDefault();
        newChat();
    }
    if (e.key === 'Escape' && isGenerating && abortController) {
        abortController.abort();
        fetch('/v1/cancel', { method: 'POST' }).catch(() => {});
    }
});

init();
