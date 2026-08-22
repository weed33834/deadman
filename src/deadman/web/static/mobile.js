
// ============================================================
// deadman 移动端 JS - 极简、触摸优先
// ============================================================

// --- 全局状态 ---
let currentTab = 'chat';
let currentAgent = '';
let agents = [];
let chatHistory = [];
let isStreaming = false;
let authToken = localStorage.getItem('m_token') || '';
let authTokenExpiry = localStorage.getItem('m_token_expiry') || '';
let pullStartY = 0;
let isPulling = false;

const API_BASE = '';
const TAB_TITLES = {
  chat: '对话',
  notes: '终活笔记',
  vault: '保险库',
  services: '服务',
  profile: '我的',
};

// --- Tab 切换 ---
function switchTab(tab) {
  currentTab = tab;
  document.querySelectorAll('.tab-item').forEach(el => {
    el.classList.toggle('active', el.dataset.tab === tab);
  });
  // 注意：chat 页容器 class 为 "chat-page"，其余为 "page"，需同时处理
  document.querySelectorAll('.page, .chat-page').forEach(el => {
    el.classList.toggle('active', el.dataset.tab === tab);
  });
  document.getElementById('navTitle').textContent = TAB_TITLES[tab] || '';
  document.getElementById('navLeftBtn').classList.add('hidden');
  document.getElementById('navRightBtn').classList.add('hidden');

  // 懒加载
  if (tab === 'notes') loadNotes();
  if (tab === 'vault') loadVault();
  if (tab === 'profile') loadProfile();
}

// --- API 请求封装 ---
function authHeaders() {
  return authToken ? { 'Authorization': 'Bearer ' + authToken } : {};
}

async function api(path, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...authHeaders(), ...(options.headers || {}) };
  try {
    const resp = await fetch(API_BASE + path, { ...options, headers });
    if (resp.status === 401 && authToken) {
      // 尝试刷新 token
      const refreshed = await refreshToken();
      if (refreshed) {
        return api(path, options); // 重试
      }
      // 刷新失败，清除 token
      authToken = '';
      localStorage.removeItem('m_token');
      showToast('请重新登录');
      loadProfile();
      return null;
    }
    const text = await resp.text();
    try { return JSON.parse(text); } catch { return text; }
  } catch (e) {
    showToast('网络错误，请检查连接');
    return null;
  }
}

async function refreshToken() {
  if (!authToken) return false;
  try {
    const resp = await fetch(API_BASE + '/api/auth/refresh', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + authToken },
    });
    if (!resp.ok) return false;
    const data = await resp.json();
    if (data.token) {
      authToken = data.token;
      authTokenExpiry = data.expires_at || '';
      localStorage.setItem('m_token', authToken);
      localStorage.setItem('m_token_expiry', authTokenExpiry);
      return true;
    }
  } catch { }
  return false;
}

function isLoggedIn() {
  return !!authToken;
}

// --- Toast ---
function showToast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 2200);
}

// --- Action Sheet ---
function showActionSheet(title, items) {
  document.getElementById('asTitle').textContent = title || '';
  const content = document.getElementById('asContent');
  content.innerHTML = '';
  items.forEach(item => {
    const el = document.createElement('div');
    el.className = 'action-sheet-item' + (item.danger ? ' danger' : '');
    el.textContent = item.label;
    el.onclick = () => { hideActionSheet(); item.action && item.action(); };
    content.appendChild(el);
  });
  document.getElementById('asOverlay').classList.add('show');
  document.getElementById('actionSheet').classList.add('show');
}
function hideActionSheet() {
  document.getElementById('asOverlay').classList.remove('show');
  document.getElementById('actionSheet').classList.remove('show');
}

// --- 对话功能 ---
async function loadAgents() {
  const data = await api('/api/agents');
  if (!data || !data.agents) return;
  agents = data.agents;
  const selector = document.getElementById('agentSelector');
  selector.innerHTML = '<button class="agent-chip active" data-agent="" data-action="select-agent" data-arg="">智能推荐</button>';
  agents.forEach(a => {
    const btn = document.createElement('button');
    btn.className = 'agent-chip';
    btn.dataset.agent = a.id || a.name;
    btn.textContent = a.name || a.id;
    btn.onclick = () => selectAgent(btn, btn.dataset.agent);
    selector.appendChild(btn);
  });
}

function selectAgent(el, agentId) {
  document.querySelectorAll('.agent-chip').forEach(c => c.classList.remove('active'));
  el.classList.add('active');
  currentAgent = agentId;
}

function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 100) + 'px';
}

async function sendMessage() {
  const input = document.getElementById('chatInput');
  const text = input.value.trim();
  if (!text || isStreaming) return;

  // 首条消息收起欢迎屏
  const hero = document.getElementById('mHero');
  if (hero) hero.remove();

  // 添加用户消息
  appendBubble(text, 'user');
  input.value = '';
  input.style.height = 'auto';
  document.getElementById('sendBtn').disabled = true;
  isStreaming = true;

  // 斜杠命令：/help /custom /family /vault /task 等 → 走对话命令接口（傻瓜式操作）
  if (text.trim().startsWith('/')) {
    const typingEl = document.createElement('div');
    typingEl.className = 'chat-bubble bot';
    typingEl.innerHTML = '<div class="typing-dots"><span></span><span></span><span></span></div>';
    document.getElementById('chatInner').appendChild(typingEl);
    scrollToBottom();
    try {
      const r = await fetch(`${API_BASE}/api/chat/command`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ command: text.trim() }) });
      const d = await r.json();
      typingEl.remove();
      appendBubble(d.text || JSON.stringify(d), 'bot');
    } catch (e) {
      typingEl.remove();
      appendBubble('命令执行失败', 'error');
    }
    document.getElementById('sendBtn').disabled = false;
    isStreaming = false;
    return;
  }

  // 显示 typing
  const typingEl = document.createElement('div');
  typingEl.className = 'chat-bubble bot';
  typingEl.innerHTML = '<div class="typing-dots"><span></span><span></span><span></span></div>';
  typingEl.id = 'typingIndicator';
  document.getElementById('chatInner').appendChild(typingEl);
  scrollToBottom();

  // SSE 流式请求
  try {
    await streamChat(text);
  } catch (e) {
    typingEl.remove();
    appendBubble('连接中断，请重试', 'error');
  }

  document.getElementById('sendBtn').disabled = false;
  isStreaming = false;
}

async function streamChat(text) {
  const params = new URLSearchParams({ query: text });
  if (currentAgent) params.set('agent', currentAgent);

  const resp = await fetch(`${API_BASE}/api/stream?${params}`, {
    headers: authHeaders(),
  });

  if (!resp.ok) {
    document.getElementById('typingIndicator')?.remove();
    appendBubble('服务暂时不可用，请稍后再试', 'error');
    return;
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let botBubble = null;
  let botText = '';
  let eventType = '';
  let isCrisis = false;

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    const lines = buffer.split('\n');
    buffer = lines.pop() || '';

    for (const line of lines) {
      if (line.startsWith('event:')) {
        eventType = line.slice(6).trim();
      } else if (line.startsWith('data:')) {
        const dataStr = line.slice(5).trim();
        try {
          const data = JSON.parse(dataStr);
          if (eventType === 'error') {
            document.getElementById('typingIndicator')?.remove();
            appendBubble(data.error || '未知错误', 'error');
            return;
          }
          if (eventType === 'done') {
            if (data.crisis_resources) {
              showCrisisBanner(data.crisis_resources);
            }
            break;
          }
          if (eventType === 'trace') {
            // 移动端不显示详细 trace，仅保留低调指示
            continue;
          }
          // 默认 message 事件
          if (data.chunk) {
            document.getElementById('typingIndicator')?.remove();
            if (!botBubble) {
              botBubble = appendBubble('', 'bot');
            }
            botText += data.chunk;
            botBubble.textContent = botText;
            scrollToBottom();
          }
          if (data.risk_tier === 'crisis') isCrisis = true;
        } catch { }
        eventType = '';
      } else if (line === '') {
        eventType = '';
      }
    }
  }

  // 如果没有收到任何内容，降级到同步请求
  if (!botText) {
    document.getElementById('typingIndicator')?.remove();
    const data = await api('/api/chat', {
      method: 'POST',
      body: JSON.stringify({ query: text, agent: currentAgent || null }),
    });
    if (data && data.response) {
      appendBubble(data.response, 'bot');
    } else {
      appendBubble('未收到响应，请重试', 'error');
    }
  }
}

function appendBubble(text, type) {
  const inner = document.getElementById('chatInner');
  const el = document.createElement('div');
  el.className = 'chat-bubble ' + type;
  if (type === 'bot') {
    el.innerHTML = md(text || '') + '<span style="opacity:.5;font-size:11px"> · 点此朗读</span>';
    el.style.cursor = 'pointer';
    el.onclick = () => speakMobile(text);
  } else {
    el.textContent = text;
  }
  inner.appendChild(el);
  scrollToBottom();
  return el;
}

// 轻量 Markdown 渲染（安全：先 escape）
function md(t) {
  if (!t) return '';
  const esc = String(t).replace(/[&<>]/g, c => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;' }[c]));
  let h = '';
  esc.split('\n').forEach(line => {
    const trim = line.trim();
    if (/^###\s/.test(trim)) h += '<div style="font-weight:700;margin:.3em 0">' + trim.slice(4) + '</div>';
    else if (/^##\s/.test(trim)) h += '<div style="font-weight:700;margin:.3em 0">' + trim.slice(3) + '</div>';
    else if (/^#\s/.test(trim)) h += '<div style="font-weight:700;margin:.4em 0">' + trim.slice(2) + '</div>';
    else if (/^[-*]\s/.test(trim)) h += '<div>• ' + trim.slice(2) + '</div>';
    else if (/^\d+[.)]\s/.test(trim)) h += '<div>' + trim.replace(/^\d+[.)]\s/, i => i + ' ') + '</div>';
    else if (trim) h += '<div style="margin:.15em 0">' + trim + '</div>';
  });
  return h;
}

function scrollToBottom() {
  const container = document.getElementById('chatMessages');
  setTimeout(() => { container.scrollTop = container.scrollHeight; }, 50);
}

function showCrisisBanner(resources) {
  const banner = document.createElement('div');
  banner.className = 'crisis-banner';
  banner.innerHTML = `
    <div class="crisis-banner-title">如果您正在经历危机</div>
    <div class="crisis-banner-text">${resources}</div>
  `;
  document.getElementById('chatInner').appendChild(banner);
  scrollToBottom();
}

// --- 终活笔记 ---
async function loadNotes() {
  const content = document.getElementById('notesContent');

  if (!isLoggedIn()) {
    content.innerHTML = `
      <div class="disclaimer">终活笔记帮助您整理重要信息，仅您可见。可共享给信任的家人。</div>
      <div class="card text-center">
        <div class="card-title">登录后查看您的笔记</div>
        <div class="card-desc">登录后可填写终活笔记、查看完成度、共享给家人</div>
        <button class="btn btn-primary btn-block mt-16" data-action="switch-tab-profile">去登录</button>
      </div>
    `;
    return;
  }

  const [completion, guide] = await Promise.all([
    api('/api/ending-note/completion'),
    api('/api/ending-note/guide/next'),
  ]);

  // API 返回 { completion: { overall: 0.0-1.0, sections: {key: 0.0/1.0} } }
  const comp = completion?.completion || {};
  const overall = typeof comp.overall === 'number' ? comp.overall : 0;
  const sections = comp.sections || {};
  const totalSections = Object.keys(sections).length;
  const completedSections = Object.values(sections).filter(s => s >= 1.0).length;
  const pct = Math.round(overall * 100);
  const offset = 339.292 * (1 - overall);

  let guideHtml = '';
  if (guide && guide.question) {
    guideHtml = `
      <div class="card">
        <div class="card-title">下一章引导</div>
        <div class="card-desc">${escapeHtml(guide.question)}</div>
        <button class="btn btn-primary btn-block mt-16" data-action="answer-guide" data-arg="${encodeURIComponent(guide.chapter || '')}|||${encodeURIComponent(guide.question)}">开始填写</button>
      </div>
    `;
  }

  content.innerHTML = `
    <div class="card text-center">
      <div class="progress-ring">
        <svg width="120" height="120">
          <circle class="ring-bg" cx="60" cy="60" r="54"/>
          <circle class="ring-fg" cx="60" cy="60" r="54" style="stroke-dashoffset: ${offset}"/>
        </svg>
        <div class="progress-ring-text">
          <div class="pct">${pct}%</div>
          <div class="label">完成度</div>
        </div>
      </div>
      <div class="text-muted text-sm">${completedSections} / ${totalSections} 章已填写</div>
    </div>
    ${guideHtml}
    <div class="card" data-action="view-full-note" style="cursor:pointer">
      <div class="card-title">查看完整笔记</div>
      <div class="card-desc">浏览和编辑所有章节</div>
    </div>
    <div class="card" data-action="share-note" style="cursor:pointer">
      <div class="card-title">共享给家人</div>
      <div class="card-desc">与信任的家人共享笔记</div>
    </div>
  `;
}

async function answerGuide(chapter, question) {
  showFormPage('填写笔记', `
    <div class="field">
      <label>${question}</label>
      <textarea class="textarea" id="guideAnswer" placeholder="请输入您的回答..."></textarea>
    </div>
  `, async () => {
    const answer = document.getElementById('guideAnswer').value.trim();
    if (!answer) { showToast('请输入回答'); return; }
    const data = await api('/api/ending-note/section', {
      method: 'POST',
      body: JSON.stringify({ chapter, content: answer }),
    });
    if (data && !data.error) {
      showToast('已保存');
      closeFormPage();
      loadNotes();
    } else {
      showToast(data?.error || '保存失败');
    }
  });
}

async function viewFullNote() {
  const data = await api('/api/ending-note');
  showFormPage('完整笔记', `
    <div id="fullNoteContent">
      <div class="loading"><div class="loading-spinner"></div></div>
    </div>
  `);
  if (data && data.sections) {
    let html = '';
    data.sections.forEach(s => {
      html += `
        <div class="card">
          <div class="card-title">${s.title || s.chapter}</div>
          <div class="card-desc">${s.content || '<span class="text-muted">未填写</span>'}</div>
        </div>
      `;
    });
    document.getElementById('fullNoteContent').innerHTML = html || '<div class="empty-state"><div class="empty-text">暂无内容</div></div>';
  }
}

async function shareNote() {
  if (!isLoggedIn()) { showToast('请先登录'); switchTab('profile'); return; }
  showFormPage('共享笔记', `
    <div class="field">
      <label>家人邮箱</label>
      <input class="input" type="email" id="shareEmail" placeholder="family@example.com">
    </div>
    <div class="disclaimer">共享后，家人可查看您的终活笔记内容。</div>
  `, async () => {
    const email = document.getElementById('shareEmail').value.trim();
    if (!email) { showToast('请输入邮箱'); return; }
    const data = await api('/api/ending-note/share', {
      method: 'POST',
      body: JSON.stringify({ to_email: email }),
    });
    if (data && !data.error) {
      showToast('已发送共享邀请');
      closeFormPage();
    } else {
      showToast(data?.error || '共享失败');
    }
  });
}

// --- 保险库 ---
async function loadVault() {
  const content = document.getElementById('vaultContent');

  if (!isLoggedIn()) {
    content.innerHTML = `
      <div class="disclaimer">数字遗产保险库 - 安全存储密码、文档、账号信息，指定受益人。</div>
      <div class="card text-center">
        <div class="card-title">登录后管理您的保险库</div>
        <div class="card-desc">登录后可添加条目、指定受益人、触发投递</div>
        <button class="btn btn-primary btn-block mt-16" data-action="switch-tab-profile">去登录</button>
      </div>
    `;
    return;
  }

  const data = await api('/api/vault/items');

  if (!data || !data.items || data.items.length === 0) {
    content.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">🔒</div>
        <div class="empty-text">保险库为空</div>
        <button class="btn btn-primary btn-block mt-16" data-action="add-vault-item">添加条目</button>
      </div>
    `;
    return;
  }

  let html = '';
  data.items.forEach(item => {
    html += `
      <div class="list-item-wrapper" data-id="${item.id}">
        <div class="list-item-content" data-action="view-vault-item" data-id="${item.id}">
          <div class="list-item" style="margin:0;box-shadow:none">
            <div class="list-item-title">${escapeHtml(item.title || item.name || '未命名')}</div>
            <div class="list-item-desc text-muted text-sm">${escapeHtml(item.type || '通用')} · ${item.beneficiary ? '受益人: ' + escapeHtml(item.beneficiary) : '未指定受益人'}</div>
          </div>
        </div>
        <div class="list-item-delete" data-action="delete-vault-item" data-id="${item.id}">删除</div>
      </div>
    `;
  });
  content.innerHTML = `
    <button class="btn btn-primary btn-block mb-16" data-action="add-vault-item">+ 添加条目</button>
    ${html}
  `;

  // 绑定左滑删除
  bindSwipeDelete();
}

function addVaultItem() {
  if (!isLoggedIn()) { showToast('请先登录'); switchTab('profile'); return; }
  showFormPage('添加保险库条目', `
    <div class="field">
      <label>名称</label>
      <input class="input" id="vaultTitle" placeholder="如：银行卡密码">
    </div>
    <div class="field">
      <label>类型</label>
      <select class="select" id="vaultType">
        <option value="password">密码</option>
        <option value="document">文档</option>
        <option value="account">账号</option>
        <option value="crypto">加密货币</option>
        <option value="other">其他</option>
      </select>
    </div>
    <div class="field">
      <label>内容</label>
      <textarea class="textarea" id="vaultContent" placeholder="密码、账号信息等..."></textarea>
    </div>
    <div class="field">
      <label>受益人邮箱</label>
      <input class="input" type="email" id="vaultBeneficiary" placeholder="heir@example.com">
    </div>
    <div class="disclaimer">内容将加密存储，仅在触发条件满足时投递给受益人。</div>
  `, async () => {
    const title = document.getElementById('vaultTitle').value.trim();
    const content = document.getElementById('vaultContent').value.trim();
    if (!title || !content) { showToast('请填写名称和内容'); return; }
    const data = await api('/api/vault/items', {
      method: 'POST',
      body: JSON.stringify({
        title, content,
        type: document.getElementById('vaultType').value,
        beneficiary: document.getElementById('vaultBeneficiary').value.trim(),
      }),
    });
    if (data && !data.error) {
      showToast('已添加');
      closeFormPage();
      loadVault();
    } else {
      showToast(data?.error || '添加失败');
    }
  });
}

async function viewVaultItem(id) {
  const data = await api('/api/vault/items/' + id);
  if (!data) return;
  showFormPage('条目详情', `
    <div class="card">
      <div class="card-title">${escapeHtml(data.title || data.name || '未命名')}</div>
      <div class="text-muted text-sm mb-16">类型: ${escapeHtml(data.type || '通用')}</div>
      <div class="card-desc" style="white-space:pre-wrap">${escapeHtml(data.content || data.value || '')}</div>
      ${data.beneficiary ? `<div class="mt-16 text-sm text-muted">受益人: ${escapeHtml(data.beneficiary)}</div>` : ''}
    </div>
    ${data.beneficiary ? `<button class="btn btn-danger btn-block" data-action="trigger-vault-item" data-id="${id}">触发投递</button>` : ''}
  `);
}

async function deleteVaultItem(id) {
  showActionSheet('确认删除？', [
    { label: '删除', danger: true, action: async () => {
      const data = await api('/api/vault/items/' + id, { method: 'DELETE' });
      if (data && !data.error) {
        showToast('已删除');
        loadVault();
      }
    }},
  ]);
}

async function triggerVaultItem(id) {
  showActionSheet('触发投递？', [
    { label: '确认触发', danger: true, action: async () => {
      const data = await api('/api/vault/items/' + id + '/trigger', { method: 'POST' });
      if (data && !data.error) {
        showToast('已触发投递');
        closeFormPage();
      }
    }},
  ]);
}

// --- 服务 ---
async function showLetterTypes() {
  if (!isLoggedIn()) { showToast('请先登录'); switchTab('profile'); return; }
  const data = await api('/api/letters/types');
  if (!data || !data.types) { showToast('加载失败'); return; }
  const items = data.types.map(t => ({
    label: t.name || t.title || t.type,
    action: () => generateLetter(t.type || t.id),
  }));
  showActionSheet('选择信函类型', items);
}

async function generateLetter(type) {
  showFormPage('生成通知信', `
    <div class="field">
      <label>逝者姓名</label>
      <input class="input" id="letterName" placeholder="逝者姓名">
    </div>
    <div class="field">
      <label>补充信息</label>
      <textarea class="textarea" id="letterContext" placeholder="如：身份证号、关系、日期等"></textarea>
    </div>
  `, async () => {
    const name = document.getElementById('letterName').value.trim();
    const context = document.getElementById('letterContext').value.trim();
    if (!name) { showToast('请填写逝者姓名'); return; }
    showToast('正在生成...');
    const data = await api('/api/letters/generate', {
      method: 'POST',
      body: JSON.stringify({ type, deceased_name: name, context }),
    });
    if (data && data.content) {
      showFormPage('生成结果', `
        <div class="card">
          <div class="card-desc" style="white-space:pre-wrap">${escapeHtml(data.content)}</div>
        </div>
      `, null, '关闭');
    } else {
      showToast(data?.error || '生成失败');
    }
  });
}

async function showMemorialTypes() {
  const data = await api('/api/memorial/types');
  if (!data || !data.types) {
    // 降级到硬编码类型
    showActionSheet('选择悼文类型', [
      { label: '悼文', action: () => generateMemorial('eulogy') },
      { label: '讣告', action: () => generateMemorial('obituary') },
      { label: '答谢词', action: () => generateMemorial('thank_you') },
      { label: '墓志铭', action: () => generateMemorial('epitaph') },
    ]);
    return;
  }
  const items = data.types.map(t => ({
    label: t.name || t.title,
    action: () => generateMemorial(t.type || t.id),
  }));
  showActionSheet('选择悼文类型', items);
}

async function generateMemorial(type) {
  showFormPage('生成悼文', `
    <div class="field">
      <label>逝者姓名</label>
      <input class="input" id="memorialName" placeholder="逝者姓名">
    </div>
    <div class="field">
      <label>生平简介</label>
      <textarea class="textarea" id="memorialBio" placeholder="逝者的生平、性格、爱好等"></textarea>
    </div>
    <div class="field">
      <label>与逝者关系</label>
      <input class="input" id="memorialRelation" placeholder="如：儿子、女儿、配偶">
    </div>
  `, async () => {
    const name = document.getElementById('memorialName').value.trim();
    if (!name) { showToast('请填写姓名'); return; }
    showToast('正在生成...');
    const data = await api('/api/memorial/generate', {
      method: 'POST',
      body: JSON.stringify({
        type,
        deceased_name: name,
        bio: document.getElementById('memorialBio').value.trim(),
        relation: document.getElementById('memorialRelation').value.trim(),
      }),
    });
    if (data && data.content) {
      showFormPage('生成结果', `
        <div class="card">
          <div class="card-desc" style="white-space:pre-wrap; font-size:15px; line-height:1.8">${escapeHtml(data.content)}</div>
        </div>
      `, null, '关闭');
    } else {
      showToast(data?.error || '生成失败');
    }
  });
}

async function loadHotlines() {
  showFormPage('热线电话', '<div id="hotlinesList"><div class="loading"><div class="loading-spinner"></div></div></div>', null, '关闭');
  const data = await api('/api/hotlines');
  const list = document.getElementById('hotlinesList');
  if (data && data.hotlines && data.hotlines.length) {
    let html = '';
    data.hotlines.forEach(h => {
      html += `
        <div class="list-item" data-action="tel" data-arg="${h.phone || h.number || ''}">
          <div class="list-item-title">${escapeHtml(h.name || h.title || '')}</div>
          <div class="list-item-desc">${escapeHtml(h.phone || h.number || '')}</div>
          ${h.description ? `<div class="list-item-meta">${escapeHtml(h.description)}</div>` : ''}
        </div>
      `;
    });
    list.innerHTML = html;
  } else {
    list.innerHTML = '<div class="empty-state"><div class="empty-text">暂无热线信息</div></div>';
  }
}

async function loadInstitutions() {
  showFormPage('机构查询', `
    <div class="field">
      <input class="input" id="instSearch" placeholder="搜索机构名称..." data-input-action="search-institutions">
    </div>
    <div id="instList"><div class="loading"><div class="loading-spinner"></div></div></div>
  `, null, '关闭');
  searchInstitutions('');
}

async function searchInstitutions(query) {
  // 查询参数名与 API 对齐：keyword（非 q）
  const data = await api('/api/institutions' + (query ? '?keyword=' + encodeURIComponent(query) : ''));
  const list = document.getElementById('instList');
  if (data && data.institutions && data.institutions.length) {
    let html = '';
    data.institutions.forEach(i => {
      html += `
        <div class="list-item" data-action="view-institution" data-id="${i.id}">
          <div class="list-item-title">${escapeHtml(i.name || '')}</div>
          <div class="list-item-desc">${escapeHtml(i.type || '')} · ${escapeHtml(i.region || '')}</div>
        </div>
      `;
    });
    list.innerHTML = html;
  } else {
    list.innerHTML = '<div class="empty-state"><div class="empty-text">未找到机构</div></div>';
  }
}

async function viewInstitution(id) {
  const data = await api('/api/institutions/' + id);
  if (!data) return;
  showFormPage('机构详情', `
    <div class="card">
      <div class="card-title">${escapeHtml(data.name || '')}</div>
      <div class="text-muted text-sm mb-16">${escapeHtml(data.type || '')} · ${escapeHtml(data.region || '')}</div>
      ${data.address ? `<div class="card-desc mb-8">地址: ${escapeHtml(data.address)}</div>` : ''}
      ${data.phone ? `<div class="card-desc mb-8">电话: <a href="tel:${data.phone}" style="color:var(--accent)">${escapeHtml(data.phone)}</a></div>` : ''}
      ${data.hours ? `<div class="card-desc mb-8">营业时间: ${escapeHtml(data.hours)}</div>` : ''}
      ${data.description ? `<div class="card-desc mt-16">${escapeHtml(data.description)}</div>` : ''}
    </div>
  `, null, '关闭');
}

// --- 身后事向导（移动端）---
const MOBILE_GUIDE_STEPS = [
  { phase: '生前准备', icon: '✍️', items: [
    { label: '写终活笔记', page: 'notes', desc: '整理重要信息，仅您可见' },
    { label: '填医疗预嘱 (ACP)', action: 'load-acp', desc: '生前预嘱 · 缓和医疗 · 医疗代理' },
    { label: '存保险库', page: 'vault', desc: '密码、文档、账号、受益人' },
  ]},
  { phase: '身后办理', icon: '📋', items: [
    { label: '生成通知信函', action: 'show-letter-types', desc: '户口注销、社保丧葬费等' },
    { label: '查热线电话', action: 'load-hotlines', desc: '心理、法律、殡葬服务热线' },
    { label: '查机构', action: 'load-institutions', desc: '殡仪馆、公证处、社保局' },
  ]},
  { phase: '纪念陪伴', icon: '🕯️', items: [
    { label: '撰写悼文', action: 'show-memorial-types', desc: '悼文、讣告、答谢词、墓志铭' },
    { label: '遗码通案件', action: 'load-cases', desc: '逝者唯一标识案例管理' },
  ]},
];

async function loadMobileGuide() {
  showFormPage('身后事向导', `
    <div class="disclaimer">从生前预存到身后办理，按清单逐项完成，AI 全程引导。</div>
    <div id="mobileGuideBody"></div>
  `, null, '关闭');
  const body = document.getElementById('mobileGuideBody');
  if (!body) return;
  let html = '';
  MOBILE_GUIDE_STEPS.forEach((g, gi) => {
    html += `
      <div class="card" style="padding:12px 14px">
        <div class="card-title" style="font-size:14px;margin-bottom:8px">${g.icon} ${g.phase}</div>`;
    g.items.forEach(item => {
      const clickAttr = item.action
        ? `data-action="${item.action}"`
        : `data-action="switch-tab" data-arg="${item.page}"`;
      html += `
        <div class="list-item" ${clickAttr} style="border:1px solid var(--border);border-radius:10px;padding:10px 12px;margin-bottom:8px">
          <div class="list-item-title" style="font-size:14px">${item.label}</div>
          <div class="list-item-desc" style="font-size:12px">${item.desc}</div>
        </div>`;
    });
    html += `</div>`;
  });
  body.innerHTML = html;
}

// --- 预立医疗 ACP（移动端）---
const MOBILE_ACP_SECTIONS = [
  { title: '什么是 ACP？', body: '在您仍清醒、有决定能力时，提前说明：一旦病重无法表达，希望接受怎样的医疗与照护。包含生前预嘱、缓和医疗知情、医疗代理授权三部分。' },
  { title: '① 生前预嘱 Living Will', body: '写下生命末期对医疗干预的取舍意愿：是否使用生命支持（呼吸机/心肺复苏/插管）、是否接受疼痛缓解、希望在哪里走完最后一程。', action: 'switch-tab', actionPage: 'notes', actionLabel: '去写医疗预嘱' },
  { title: '② 缓和医疗知情 Palliative Care', body: '缓和医疗关注减轻痛苦、提升生活质量。知情要点：疼痛管理、安宁疗护病房、舒适护理、灵性陪伴。可按省份查询当地机构。', action: 'load-institutions', actionLabel: '查安宁疗护机构' },
  { title: '③ 医疗代理授权 Health Proxy', body: '指定一位信任的成年人作医疗代理：无法表达时，由 TA 依据您的意愿与医生沟通治疗方案。务必当面确认其意愿，并存放进保险库。', action: 'switch-tab', actionPage: 'vault', actionLabel: '存入保险库' },
  { title: '执行路径', body: '1) 如实写下医疗意愿；2) 与家人/代理充分沟通并取得同意；3) 文书存入保险库并告知代理人在哪找到。' },
];

async function loadMobileAcp() {
  showFormPage('预立医疗 ACP', `
    <div class="disclaimer">本模块仅提供信息引导，不构成法律或医学意见，不代办任何手续。生前预嘱等文书如需法律效力，请按当地规定办理公证并经执业医师/律师确认。</div>
    <div id="mobileAcpBody"></div>
  `, null, '关闭');
  const body = document.getElementById('mobileAcpBody');
  if (!body) return;
  let html = '';
  MOBILE_ACP_SECTIONS.forEach(s => {
    html += `
      <div class="card" style="padding:12px 14px">
        <div class="card-title" style="font-size:14px;margin-bottom:6px">${s.title}</div>
        <div class="card-desc" style="font-size:13px;line-height:1.7">${s.body}</div>
        ${s.action ? `<button class="btn btn-primary btn-block mt-8" style="min-height:40px;font-size:13px" data-action="${s.action}" ${s.actionPage ? `data-arg="${s.actionPage}"` : ''}>${s.actionLabel}</button>` : ''}
      </div>`;
  });
  body.innerHTML = html;
}

// --- 我的 ---
async function loadProfile() {
  const content = document.getElementById('profileContent');

  if (!isLoggedIn()) {
    content.innerHTML = `
      <div class="card text-center">
        <div class="card-title">登录后体验完整功能</div>
        <div class="card-desc">登录后可保存笔记、管理保险库、生成文档</div>
        <button class="btn btn-primary btn-block mt-16" data-action="show-login-form">登录</button>
        <button class="btn btn-secondary btn-block mt-8" data-action="show-register-form">注册</button>
      </div>
      <div class="card" data-action="load-cases" style="cursor:pointer">
        <div class="card-title">遗码通案件</div>
        <div class="card-desc">逝者唯一标识案例管理</div>
      </div>
      <div class="card" data-action="load-switch" style="cursor:pointer">
        <div class="card-title">Dead Man Switch</div>
        <div class="card-desc">多因子死亡推定状态机</div>
      </div>
      <div class="disclaimer">本平台仅提供信息引导，不代办任何手续，不出具法律或医学诊断意见。</div>
    `;
    return;
  }

  // 已登录
  const me = await api('/api/auth/me');
  const name = me?.display_name || me?.email || '用户';
  content.innerHTML = `
    <div class="card">
      <div class="card-title">${escapeHtml(name)}</div>
      ${me?.email ? `<div class="text-muted text-sm">${escapeHtml(me.email)}</div>` : ''}
    </div>
    <div class="card" data-action="load-cases" style="cursor:pointer">
      <div class="card-title">遗码通案件</div>
      <div class="card-desc">逝者唯一标识案例管理</div>
    </div>
    <div class="card" data-action="load-switch" style="cursor:pointer">
      <div class="card-title">Dead Man Switch</div>
      <div class="card-desc">多因子死亡推定状态机</div>
    </div>
    <div class="card" data-action="load-plan-score" style="cursor:pointer">
      <div class="card-title">规划评分</div>
      <div class="card-desc">身后事规划完整度评分</div>
    </div>
    <button class="btn btn-danger btn-block" data-action="logout">退出登录</button>
    <div class="disclaimer">本平台仅提供信息引导，不代办任何手续，不出具法律或医学诊断意见。</div>
  `;
}

function showLoginForm() {
  showFormPage('登录', `
    <div class="field">
      <label>邮箱</label>
      <input class="input" type="email" id="loginEmail" placeholder="your@email.com">
    </div>
    <div class="field">
      <label>密码</label>
      <input class="input" type="password" id="loginPassword" placeholder="密码">
    </div>
  `, async () => {
    const email = document.getElementById('loginEmail').value.trim();
    const password = document.getElementById('loginPassword').value;
    if (!email || !password) { showToast('请填写邮箱和密码'); return; }
    const data = await api('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    });
    if (data && data.token) {
      authToken = data.token;
      authTokenExpiry = data.expires_at || '';
      localStorage.setItem('m_token', authToken);
      localStorage.setItem('m_token_expiry', authTokenExpiry);
      showToast('登录成功');
      closeFormPage();
      loadProfile();
    } else {
      showToast(data?.error || '登录失败');
    }
  });
}

function showRegisterForm() {
  showFormPage('注册', `
    <div class="field">
      <label>昵称</label>
      <input class="input" id="regName" placeholder="您的称呼">
    </div>
    <div class="field">
      <label>邮箱</label>
      <input class="input" type="email" id="regEmail" placeholder="your@email.com">
    </div>
    <div class="field">
      <label>密码</label>
      <input class="input" type="password" id="regPassword" placeholder="至少 8 位">
    </div>
  `, async () => {
    const name = document.getElementById('regName').value.trim();
    const email = document.getElementById('regEmail').value.trim();
    const password = document.getElementById('regPassword').value;
    if (!name || !email || !password) { showToast('请填写完整信息'); return; }
    if (password.length < 8) { showToast('密码至少 8 位'); return; }
    const data = await api('/api/auth/register', {
      method: 'POST',
      body: JSON.stringify({ email, password, display_name: name }),
    });
    if (data && data.token) {
      authToken = data.token;
      authTokenExpiry = data.expires_at || '';
      localStorage.setItem('m_token', authToken);
      localStorage.setItem('m_token_expiry', authTokenExpiry);
      showToast('注册成功');
      closeFormPage();
      loadProfile();
    } else {
      showToast(data?.error || '注册失败');
    }
  });
}

function logout() {
  showActionSheet('退出登录？', [
    { label: '退出', danger: true, action: () => {
      authToken = '';
      authTokenExpiry = '';
      localStorage.removeItem('m_token');
      localStorage.removeItem('m_token_expiry');
      showToast('已退出');
      loadProfile();
    }},
  ]);
}

async function loadCases() {
  showFormPage('遗码通案件', '<div id="casesList"><div class="loading"><div class="loading-spinner"></div></div></div>', null, '关闭');
  const data = await api('/api/cases');
  const list = document.getElementById('casesList');
  if (data && data.cases && data.cases.length) {
    let html = '';
    data.cases.forEach(c => {
      html += `
        <div class="list-item" data-action="view-case" data-id="${c.id}">
          <div class="list-item-title">${escapeHtml(c.decedent_name || c.title || '未命名')}</div>
          <div class="list-item-desc">${escapeHtml(c.status || '')}</div>
          <div class="list-item-meta">${c.created_at || ''}</div>
        </div>
      `;
    });
    list.innerHTML = html + `<button class="btn btn-primary btn-block mt-16" data-action="create-case">+ 创建案件</button>`;
  } else {
    list.innerHTML = `
      <div class="empty-state">
        <div class="empty-text">暂无案件</div>
      </div>
      <button class="btn btn-primary btn-block mt-16" data-action="create-case">+ 创建案件</button>
    `;
  }
}

async function createCase() {
  showFormPage('创建案件', `
    <div class="field">
      <label>逝者姓名</label>
      <input class="input" id="caseName" placeholder="逝者姓名">
    </div>
    <div class="field">
      <label>逝者身份证号</label>
      <input class="input" id="caseId" placeholder="身份证号（选填）">
    </div>
    <div class="field">
      <label>备注</label>
      <textarea class="textarea" id="caseNote" placeholder="案件描述..."></textarea>
    </div>
  `, async () => {
    const name = document.getElementById('caseName').value.trim();
    if (!name) { showToast('请填写姓名'); return; }
    const data = await api('/api/cases', {
      method: 'POST',
      body: JSON.stringify({
        decedent_name: name,
        id_number: document.getElementById('caseId').value.trim(),
        note: document.getElementById('caseNote').value.trim(),
      }),
    });
    if (data && !data.error) {
      showToast('已创建');
      closeFormPage();
      loadCases();
    } else {
      showToast(data?.error || '创建失败');
    }
  });
}

async function viewCase(id) {
  const data = await api('/api/cases/' + id);
  if (!data) return;
  showFormPage('案件详情', `
    <div class="card">
      <div class="card-title">${escapeHtml(data.decedent_name || data.title || '')}</div>
      <div class="text-muted text-sm mb-16">状态: ${escapeHtml(data.status || '')}</div>
      ${data.id_number ? `<div class="card-desc mb-8">身份证: ${escapeHtml(data.id_number)}</div>` : ''}
      ${data.note ? `<div class="card-desc mt-16">${escapeHtml(data.note)}</div>` : ''}
    </div>
    ${data.timeline ? `
      <div class="card">
        <div class="card-title">时间线</div>
        <div id="caseTimeline"></div>
      </div>
    ` : ''}
  `, null, '关闭');
}

async function loadSwitch() {
  const data = await api('/api/switch/status');
  // API 返回 record.to_dict()，状态字段为 state（ACTIVE/SUSPECTED/.../CANCELLED）；
  // 未初始化时返回 404（data 含 error/detail），此时显示"未激活"
  const state = data?.state || (data?.error ? '' : '未激活');
  const isActive = state && state !== 'CANCELLED';
  showFormPage('Dead Man Switch', `
    <div class="card text-center">
      <div class="card-title">当前状态</div>
      <div style="font-size:24px; font-weight:600; color:var(--accent); margin:12px 0">${state || '未激活'}</div>
      ${data?.last_checkin ? `<div class="text-muted text-sm">上次签到: ${data.last_checkin}</div>` : ''}
    </div>
    ${!isActive ? `
      <button class="btn btn-primary btn-block" data-action="init-switch">初始化 Dead Man Switch</button>
    ` : `
      <button class="btn btn-secondary btn-block" data-action="checkin-switch">签到</button>
      <button class="btn btn-danger btn-block mt-8" data-action="cancel-switch">取消</button>
    `}
    <div class="disclaimer">Dead Man Switch 会在您长时间未签到时，按预设条件触发数字遗产投递。</div>
  `, null, '关闭');
}

async function initSwitch() {
  showFormPage('初始化 Switch', `
    <div class="field">
      <label>签到周期（天）</label>
      <input class="input" type="number" id="switchInterval" value="7" min="1">
    </div>
    <div class="field">
      <label>继承人邮箱</label>
      <input class="input" type="email" id="switchHeir" placeholder="heir@example.com">
    </div>
    <div class="disclaimer">超过签到周期未签到时，将触发遗产投递。</div>
  `, async () => {
    const interval = document.getElementById('switchInterval').value;
    const heir = document.getElementById('switchHeir').value.trim();
    // 字段名与 SwitchInitRequest schema 对齐：frequency（非 checkin_interval_days）
    const payload = { frequency: parseInt(interval) || 7, missed: 3, window: 7, cooldown: 7 };
    if (heir) { payload.email = heir; }
    const data = await api('/api/switch/init', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    if (data && !data.error) {
      showToast('已初始化');
      closeFormPage();
      loadSwitch();
    } else {
      showToast(data?.error || data?.message || '初始化失败');
    }
  });
}

async function checkinSwitch() {
  // 路径修正：/checkin → /api/switch/checkin（与 FastAPI 路由一致）
  const data = await api('/api/switch/checkin', { method: 'POST', body: JSON.stringify({}) });
  if (data && !data.error) {
    showToast('签到成功');
    closeFormPage();
    loadSwitch();
  } else {
    showToast(data?.error || '签到失败');
  }
}

async function cancelSwitch() {
  showActionSheet('取消 Switch？', [
    { label: '确认取消', danger: true, action: async () => {
      const data = await api('/api/switch/cancel', { method: 'POST', body: JSON.stringify({}) });
      if (data && !data.error) {
        showToast('已取消');
        closeFormPage();
      }
    }},
  ]);
}

async function loadPlanScore() {
  const data = await api('/api/plan-score');
  showFormPage('规划评分', `
    ${data?.total_score !== undefined ? `
      <div class="card text-center">
        <div class="progress-ring">
          <svg width="120" height="120">
            <circle class="ring-bg" cx="60" cy="60" r="54"/>
            <circle class="ring-fg" cx="60" cy="60" r="54" style="stroke-dashoffset: ${339.292 * (1 - (data.total_score || 0) / 100)}"/>
          </svg>
          <div class="progress-ring-text">
            <div class="pct">${Math.round(data.total_score || 0)}</div>
            <div class="label">总分</div>
          </div>
        </div>
      </div>
    ` : ''}
    ${data?.dimensions ? data.dimensions.map(d => `
      <div class="card">
        <div class="card-title">${escapeHtml(d.name || '')}</div>
        <div class="flex justify-between items-center">
          <div class="card-desc">${escapeHtml(d.status || '')}</div>
          <div style="font-weight:600; color:var(--accent)">${d.score || 0}/100</div>
        </div>
      </div>
    `).join('') : ''}
    <div class="empty-state ${data?.dimensions ? 'hidden' : ''}">
      <div class="empty-text">暂无评分数据</div>
    </div>
  `, null, '关闭');
}

// --- 全屏表单页 ---
function showFormPage(title, bodyHtml, onSave, closeLabel) {
  const existing = document.querySelector('.form-page');
  if (existing) existing.remove();

  const page = document.createElement('div');
  page.className = 'form-page show';
  page.innerHTML = `
    <div class="form-page-header">
      <button data-action="close-form-page">${closeLabel ? '' : '取消'}</button>
      <h2>${escapeHtml(title)}</h2>
      <div style="width:60px"></div>
    </div>
    <div class="form-page-body">${bodyHtml}</div>
    ${onSave ? `
      <div class="form-page-footer">
        <button class="btn btn-primary btn-block" data-action="form-page-save">保存</button>
      </div>
    ` : ''}
  `;
  if (closeLabel) {
    page.querySelector('.form-page-header button').textContent = closeLabel;
  }
  document.body.appendChild(page);
  window._formPageSave = onSave;
}

function formPageSave() {
  if (window._formPageSave) window._formPageSave();
}

function closeFormPage() {
  const page = document.querySelector('.form-page');
  if (page) {
    page.style.transform = 'translateY(100%)';
    setTimeout(() => page.remove(), 250);
  }
  window._formPageSave = null;
}

// --- 左滑删除 ---
function bindSwipeDelete() {
  document.querySelectorAll('.list-item-content').forEach(el => {
    let startX = 0;
    let currentX = 0;
    let isDragging = false;

    el.addEventListener('touchstart', (e) => {
      startX = e.touches[0].clientX;
      isDragging = true;
      el.style.transition = 'none';
    }, { passive: true });

    el.addEventListener('touchmove', (e) => {
      if (!isDragging) return;
      currentX = e.touches[0].clientX - startX;
      if (currentX < 0 && currentX > -80) {
        el.style.transform = `translateX(${currentX}px)`;
      }
    }, { passive: true });

    el.addEventListener('touchend', () => {
      if (!isDragging) return;
      isDragging = false;
      el.style.transition = 'transform 0.2s ease';
      if (currentX < -40) {
        el.classList.add('swiped');
        el.style.transform = '';
      } else {
        el.classList.remove('swiped');
        el.style.transform = '';
      }
      currentX = 0;
    });
  });
}

// --- 下拉刷新 ---
function initPullRefresh() {
  const content = document.getElementById('content');
  const indicator = document.getElementById('pullRefresh');

  content.addEventListener('touchstart', (e) => {
    if (content.scrollTop === 0) {
      pullStartY = e.touches[0].clientY;
      isPulling = true;
    }
  }, { passive: true });

  content.addEventListener('touchmove', (e) => {
    if (!isPulling) return;
    const pull = e.touches[0].clientY - pullStartY;
    if (pull > 0 && pull < 80) {
      indicator.classList.add('active');
      indicator.style.height = pull + 'px';
    }
  }, { passive: true });

  content.addEventListener('touchend', async () => {
    if (!isPulling) return;
    isPulling = false;
    const indicator = document.getElementById('pullRefresh');
    if (parseInt(indicator.style.height) > 50) {
      indicator.style.height = '50px';
      // 触发刷新
      if (currentTab === 'chat') loadAgents();
      if (currentTab === 'notes') loadNotes();
      if (currentTab === 'vault') loadVault();
      if (currentTab === 'profile') loadProfile();
      showToast('已刷新');
    }
    setTimeout(() => {
      indicator.classList.remove('active');
      indicator.style.height = '0';
    }, 800);
  });
}

// --- 工具函数 ---
function escapeHtml(str) {
  if (str == null) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

// --- 键盘处理：Enter 发送 ---
document.getElementById('chatInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});
// chatInput 的 oninput="autoResize(this)" 也在此绑定（CSP 友好）
document.getElementById('chatInput').addEventListener('input', function() {
  autoResize(this);
});

// --- Service Worker 注册（PWA）---
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}

// --- 初始化 ---
loadAgents();
initPullRefresh();

// 检测是否从主屏幕启动
if (window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone) {
  document.getElementById('navTitle').textContent = '身后事引导';
}

// ============================================================
// 事件委托层（CSP 友好：替代所有 inline onclick/oninput）
// 通过 data-action 属性统一分发点击事件，动态生成的元素也能生效。
// 这解决了 Trae Web 预览 iframe 注入 CSP（script-src）阻止 inline
// event handler 导致"点击完全无反应"的问题。
// ============================================================
const _ACTION_MAP = {
  'history-back': () => history.back(),
  'show-agent-selector': () => showAgentSelector(),
  'send-message': () => sendMessage(),
  'voice-input': () => toggleMobileVoice(),
  'show-letter-types': () => showLetterTypes(),
  'show-memorial-types': () => showMemorialTypes(),
  'load-hotlines': () => loadHotlines(),
  'load-institutions': () => loadInstitutions(),
  'load-mobile-guide': () => loadMobileGuide(),
  'load-acp': () => loadMobileAcp(),
  'switch-tab-profile': () => switchTab('profile'),
  'add-vault-item': () => addVaultItem(),
  'view-full-note': () => viewFullNote(),
  'share-note': () => shareNote(),
  'show-login-form': () => showLoginForm(),
  'show-register-form': () => showRegisterForm(),
  'load-cases': () => loadCases(),
  'load-switch': () => loadSwitch(),
  'load-plan-score': () => loadPlanScore(),
  'logout': () => logout(),
  'create-case': () => createCase(),
  'init-switch': () => initSwitch(),
  'checkin-switch': () => checkinSwitch(),
  'cancel-switch': () => cancelSwitch(),
  'hide-action-sheet': () => hideActionSheet(),
  'close-form-page': () => closeFormPage(),
  'form-page-save': () => formPageSave(),
};

// 带参数的动作：data-action="view-vault-item" data-id="xxx"
const _PARAM_ACTION_MAP = {
  'hero-send': (arg) => {
    const input = document.getElementById('chatInput');
    if (!arg || isStreaming) return;
    input.value = arg;
    sendMessage();
  },
  'switch-tab': (arg) => {
    closeFormPage();
    switchTab(arg);
  },
  'select-agent': (arg) => {
    const btn = document.querySelector('[data-agent="' + arg + '"]');
    if (btn) selectAgent(btn, arg);
  },
  'answer-guide': (arg) => {
    // arg 格式: "chapter\u0000question"，用 \u0001 分隔更安全
    const parts = arg.split('\u0001');
    answerGuide(parts[0] || '', parts[1] || '');
  },
  'view-vault-item': (id) => viewVaultItem(id),
  'delete-vault-item': (id) => deleteVaultItem(id),
  'trigger-vault-item': (id) => triggerVaultItem(id),
  'view-case': (id) => viewCase(id),
  'view-institution': (id) => viewInstitution(id),
  'tel': (num) => { window.location.href = 'tel:' + num; },
};

document.addEventListener('click', function(e) {
  const el = e.target.closest('[data-action]');
  if (!el) return;
  const action = el.dataset.action;
  if (_ACTION_MAP[action]) {
    e.preventDefault();
    _ACTION_MAP[action]();
    return;
  }
  // 带参数动作
  const arg = el.dataset.arg || el.dataset.id || '';
  if (_PARAM_ACTION_MAP[action]) {
    e.preventDefault();
    _PARAM_ACTION_MAP[action](arg);
    return;
  }
});

// oninput 事件委托（搜索框等）
document.addEventListener('input', function(e) {
  const el = e.target.closest('[data-input-action]');
  if (!el) return;
  const action = el.dataset.inputAction;
  if (action === 'search-institutions') {
    searchInstitutions(el.value);
  }
});


// ============================================================
// 移动端语音输入 + 朗读（傻瓜式操作）
// ============================================================
let _mRec = null, _mChunks = [], _mStream = null, _mRecording = false;
async function toggleMobileVoice() {
  const btn = document.getElementById('micBtn');
  if (!window.MediaRecorder) { appendBubble('当前浏览器不支持语音', 'error'); return; }
  if (_mRecording) { if (_mRec && _mRec.state !== 'inactive') _mRec.stop(); else _mRecording = false; return; }
  try {
    _mStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    _mRec = new MediaRecorder(_mStream); _mChunks = [];
    _mRec.ondataavailable = e => { if (e.data.size) _mChunks.push(e.data); };
    _mRec.onstop = async () => {
      _mRecording = false; if (btn) btn.textContent = '🎤';
      _mStream.getTracks().forEach(t => t.stop());
      if (!_mChunks.length) return;
      const fd = new FormData();
      const blob = new Blob(_mChunks, { type: _mRec.mimeType || 'audio/webm' });
      const ext = blob.type.includes('ogg') ? '.ogg' : '.webm';
      fd.append('audio', blob, 'rec' + ext); fd.append('language', 'auto');
      try {
        const r = await fetch(`${API_BASE}/api/voice/transcribe`, { method: 'POST', body: fd });
        const d = await r.json();
        if (d.text) {
          const input = document.getElementById('chatInput');
          input.value = (input.value ? input.value + ' ' : '') + d.text;
          input.focus();
        } else appendBubble('未识别到语音', 'error');
      } catch (e) { appendBubble('语音转写失败', 'error'); }
    };
    _mRec.start(); _mRecording = true; if (btn) btn.textContent = '🔴';
  } catch (e) { appendBubble('无法访问麦克风', 'error'); }
}

// 朗读 bot 消息（点击气泡）
function speakMobile(text) {
  if (window.speechSynthesis) window.speechSynthesis.cancel();
  fetch(`${API_BASE}/api/voice/speak?text=${encodeURIComponent((text||'').slice(0,500))}&voice_id=gentle_female`)
    .then(r => r.ok && r.headers.get('content-type').includes('audio') ? r.blob() : null)
    .then(blob => {
      if (blob) { new Audio(URL.createObjectURL(blob)).play(); return; }
      if (window.speechSynthesis) { const u = new SpeechSynthesisUtterance((text||'').slice(0,500)); u.lang='zh-CN'; window.speechSynthesis.speak(u); }
    })
    .catch(() => { if (window.speechSynthesis) { const u = new SpeechSynthesisUtterance((text||'').slice(0,500)); u.lang='zh-CN'; window.speechSynthesis.speak(u); } });
}

// 移动端首次运行引导
function dismissMobileGuide() {
  document.getElementById('mobileFirstRun').style.display = 'none';
  localStorage.setItem('deadman_mobile_first_run', '1');
}
(function initMobileGuide() {
  try {
    if (localStorage.getItem('deadman_mobile_first_run') !== '1') {
      var g = document.getElementById('mobileFirstRun');
      if (g) g.style.display = 'flex';
    }
  } catch (e) {}
})();
