const $ = selector => document.querySelector(selector);
const state = {
  manager: null,
  queue: 'open',
  search: '',
  selectedId: null,
  detail: null,
  queueTimer: null,
  detailTimer: null,
  notificationTimer: null,
  notificationSnapshot: null,
  busy: false,
};
let managerAudioContext = null;

function unlockManagerSound() {
  if (!managerAudioContext) {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) return null;
    managerAudioContext = new AudioContextClass();
  }
  if (managerAudioContext.state === 'suspended') managerAudioContext.resume().catch(() => {});
  return managerAudioContext;
}

function managerTone(context, frequency, start, duration, volume) {
  const oscillator = context.createOscillator();
  const gain = context.createGain();
  oscillator.type = 'sine';
  oscillator.frequency.setValueAtTime(frequency, start);
  gain.gain.setValueAtTime(0.0001, start);
  gain.gain.exponentialRampToValueAtTime(volume, start + 0.012);
  gain.gain.exponentialRampToValueAtTime(0.0001, start + duration);
  oscillator.connect(gain);
  gain.connect(context.destination);
  oscillator.start(start);
  oscillator.stop(start + duration + 0.02);
}

function playManagerSignal(kind) {
  const context = unlockManagerSound();
  if (!context || context.state !== 'running') return;
  const now = context.currentTime + 0.015;
  if (kind === 'request') {
    managerTone(context, 880, now, 0.14, 0.09);
    managerTone(context, 1175, now + 0.17, 0.18, 0.08);
  } else {
    managerTone(context, 620, now, 0.16, 0.075);
  }
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({
    '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;',
  }[char]));
}

function safeUrl(value) {
  try {
    const url = new URL(String(value || ''));
    return ['http:', 'https:'].includes(url.protocol) ? url.href : '';
  } catch { return ''; }
}

function formatDate(value, includeDate = false) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return new Intl.DateTimeFormat('ru', includeDate
    ? { day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit' }
    : { hour:'2-digit', minute:'2-digit' }).format(date);
}

function initials(value) {
  const parts = String(value || 'П').trim().split(/\s+/).filter(Boolean);
  return parts.slice(0, 2).map(part => part[0]).join('').toUpperCase() || 'П';
}

function plural(count, one, few, many) {
  const mod100 = count % 100;
  const mod10 = count % 10;
  if (mod100 >= 11 && mod100 <= 14) return many;
  if (mod10 === 1) return one;
  if (mod10 >= 2 && mod10 <= 4) return few;
  return many;
}

async function api(path, options = {}) {
  const method = options.method || 'GET';
  const response = await fetch(path, {
    ...options,
    headers: {
      'Content-Type':'application/json',
      ...(method !== 'GET' ? {'X-Consilium-Manager':'1'} : {}),
      ...(options.headers || {}),
    },
  });
  let payload = {};
  try { payload = await response.json(); } catch {}
  if (!response.ok) {
    const error = new Error(payload.detail || `Ошибка ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

function toast(message, error = false) {
  const element = $('#managerToast');
  element.textContent = message;
  element.classList.toggle('error', error);
  element.classList.remove('hidden');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => element.classList.add('hidden'), 3200);
}

async function login(event) {
  event?.preventDefault();
  unlockManagerSound();
  $('#loginError').classList.add('hidden');
  try {
    const manager = await api('/api/manager/login', {
      method:'POST',
      body:JSON.stringify({
        login:$('#managerLogin').value.trim(),
        password:$('#managerPassword').value,
      }),
    });
    enterWorkspace(manager);
  } catch (error) {
    $('#loginError').textContent = error.message;
    $('#loginError').classList.remove('hidden');
  }
}

function enterWorkspace(manager) {
  state.manager = manager;
  $('#managerPassword').value = '';
  $('#loginShell').classList.add('hidden');
  $('#managerApp').classList.remove('hidden');
  $('#managerDisplayName').textContent = manager.display_name;
  $('#managerAvatar').textContent = initials(manager.display_name);
  loadQueue();
  pollManagerNotifications();
  clearInterval(state.queueTimer);
  clearInterval(state.detailTimer);
  clearInterval(state.notificationTimer);
  state.queueTimer = setInterval(loadQueue, 5000);
  state.detailTimer = setInterval(() => {
    if (state.selectedId && document.visibilityState === 'visible') loadDetail(false);
  }, 3000);
  state.notificationTimer = setInterval(pollManagerNotifications, 5000);
}

function showManagerLogin(message = '') {
  clearInterval(state.queueTimer);
  clearInterval(state.detailTimer);
  clearInterval(state.notificationTimer);
  state.notificationSnapshot = null;
  state.manager = null;
  state.selectedId = null;
  state.detail = null;
  $('#managerApp').classList.add('hidden');
  $('#managerApp').classList.remove('chat-open');
  $('#loginShell').classList.remove('hidden');
  $('#loginError').textContent = message;
  $('#loginError').classList.toggle('hidden', !message);
}

async function logout() {
  try { await api('/api/manager/logout', {method:'POST',body:'{}'}); } catch {}
  $('#managerPassword').value = '';
  showManagerLogin();
}

function queueLabel(item) {
  if (item.human_status === 'closed') return '<b>Закрыто</b>';
  if (!item.ai_enabled) return '<b class="ai-off">ИИ выключен</b>';
  if (item.human_channel === 'call') return '<b>Созвон</b>';
  if (item.human_channel === 'chat') return '<b>Чат</b>';
  return '<b>Формат не выбран</b>';
}

async function pollManagerNotifications() {
  if (!state.manager) return;
  try {
    const items = await api('/api/manager/conversations?queue=open&limit=250');
    const next = new Map(items.map(item => [item.id, {
      ticket: item.human_ticket_id || '',
      unanswered: Number(item.unanswered_user_messages || 0),
      aiEnabled: Boolean(item.ai_enabled),
    }]));
    if (state.notificationSnapshot) {
      let newRequest = false;
      let waitingMessage = false;
      for (const item of items) {
        const previous = state.notificationSnapshot.get(item.id);
        const ticketAppeared = Boolean(item.human_ticket_id)
          && (!previous || !previous.ticket);
        if (ticketAppeared) {
          newRequest = true;
          continue;
        }
        if (
          previous && !item.ai_enabled
          && Number(item.unanswered_user_messages || 0) > previous.unanswered
        ) {
          waitingMessage = true;
        }
      }
      if (newRequest) playManagerSignal('request');
      if (waitingMessage) {
        if (newRequest) setTimeout(() => playManagerSignal('message'), 500);
        else playManagerSignal('message');
      }
    }
    state.notificationSnapshot = next;
  } catch (error) {
    if (error.status === 401) showManagerLogin('Сеанс завершён. Войдите снова.');
  }
}

async function loadQueue() {
  if (!state.manager || document.visibilityState !== 'visible') return;
  const params = new URLSearchParams({ queue:state.queue, query:state.search, limit:'150' });
  try {
    const items = await api(`/api/manager/conversations?${params}`);
    $('#queueCount').textContent = `${items.length} ${plural(items.length, 'обращение', 'обращения', 'обращений')}`;
    $('#queueUpdated').textContent = `обновлено ${formatDate(new Date())}`;
    $('#requestList').innerHTML = items.length ? items.map(item => {
      const name = item.preferred_name || `Пользователь ${item.chel_id.slice(-6)}`;
      const unread = Number(item.unanswered_user_messages || 0);
      return `<button class="request-card ${item.id === state.selectedId ? 'active' : ''}" data-conversation-id="${escapeHtml(item.id)}">
        <span class="request-card-head"><strong>${escapeHtml(name)}</strong><time>${formatDate(item.updated_at)}</time></span>
        <p>${escapeHtml(item.last_message || item.title || 'Нет сообщений')}</p>
        <span class="request-meta">${queueLabel(item)}<span>${escapeHtml(item.human_ticket_id || item.id.slice(0, 8))}</span>${unread ? `<i class="unread">${unread}</i>` : ''}</span>
      </button>`;
    }).join('') : '<div class="queue-empty">В этой очереди пока нет обращений.</div>';
  } catch (error) {
    if (error.status === 401) return showManagerLogin('Сеанс завершён. Войдите снова.');
    toast(error.message, true);
  }
}

async function selectConversation(id) {
  state.selectedId = id;
  state.detail = null;
  document.querySelectorAll('.request-card').forEach(card => {
    card.classList.toggle('active', card.dataset.conversationId === id);
  });
  $('#emptyWorkspace').classList.add('hidden');
  $('#chatWorkspace').classList.remove('hidden');
  $('#managerApp').classList.add('chat-open');
  await loadDetail(true);
}

async function loadDetail(force = false) {
  if (!state.selectedId || state.busy) return;
  try {
    const detail = await api(`/api/manager/conversations/${encodeURIComponent(state.selectedId)}`);
    if (state.selectedId !== detail.conversation.id) return;
    const previousLast = state.detail?.messages?.at(-1)?.id || 0;
    const nextLast = detail.messages.at(-1)?.id || 0;
    const modeChanged = state.detail?.conversation?.ai_enabled !== detail.conversation.ai_enabled;
    state.detail = detail;
    renderHeader();
    if (force || nextLast !== previousLast) renderMessages();
    if (force || modeChanged) renderUserDetails();
  } catch (error) {
    if (error.status === 401) return showManagerLogin('Сеанс завершён. Войдите снова.');
    toast(error.message, true);
  }
}

function renderHeader() {
  const { conversation, profile } = state.detail;
  const name = profile.preferred_name || `Пользователь ${conversation.chel_id.slice(-6)}`;
  $('#chatUserName').textContent = name;
  $('#profileName').textContent = name;
  $('#chatAvatar').textContent = initials(name);
  $('#chatTicket').textContent = conversation.human_ticket_id
    ? `${conversation.human_ticket_id} · ${conversation.human_status === 'closed' ? 'закрыто' : conversation.human_channel === 'call' ? 'созвон' : conversation.human_channel === 'chat' ? 'чат' : 'формат не выбран'}`
    : `Диалог ${conversation.id.slice(0, 8)}`;
  $('#aiEnabled').checked = Boolean(conversation.ai_enabled);
  $('#aiSwitchTitle').textContent = conversation.ai_enabled ? 'ИИ отвечает' : 'ИИ выключен';
  $('#aiSwitchHint').textContent = conversation.ai_enabled
    ? 'Следующие вопросы получит ИИ'
    : 'Пользователь ждёт человека';
  const notice = $('#managerModeNotice');
  notice.classList.toggle('ai-off', !conversation.ai_enabled);
  $('#managerModeText').textContent = conversation.ai_enabled
    ? 'ИИ продолжает отвечать пользователю с учётом всей истории. Вы также можете написать в чат.'
    : 'ИИ приостановлен. Новые сообщения сохраняются, но ответит на них только менеджер.';
  const closeButton = $('#closeRequestButton');
  const closable = conversation.human_status !== 'closed'
    && (Boolean(conversation.human_ticket_id) || !conversation.ai_enabled);
  closeButton.classList.toggle('hidden', !closable);
  closeButton.disabled = state.busy;
}

function messageDocuments(metadata) {
  const attachments = (metadata.attachments || []).map(item =>
    `<span>▱ ${escapeHtml(item.name || 'Файл')}</span>`);
  const documents = (metadata.lab_result_documents || []).map(item => {
    const url = safeUrl(item.url);
    return url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">Открыть ${escapeHtml(item.title || 'результат')}</a>` : '';
  });
  return [...attachments, ...documents].filter(Boolean).join('');
}

function renderMessages() {
  const container = $('#managerMessages');
  const nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 100;
  container.innerHTML = state.detail.messages.length ? state.detail.messages.map(message => {
    const metadata = message.metadata || {};
    const isUser = message.role === 'user';
    const isHuman = metadata.sender_type === 'human_manager';
    const author = isUser ? 'Пользователь' : isHuman
      ? (metadata.manager_name || 'Менеджер')
      : `${agentName(message.agent_id)} · ИИ`;
    const docs = messageDocuments(metadata);
    return `<article class="manager-message ${isUser ? 'user' : isHuman ? 'human' : 'ai'}">
      <div class="message-card"><div class="message-author"><strong>${escapeHtml(author)}</strong></div>
      <div class="message-bubble">${escapeHtml(message.content)}${docs ? `<div class="message-files">${docs}</div>` : ''}<time class="message-time">${formatDate(message.created_at, true)}</time></div></div>
    </article>`;
  }).join('') : '<div class="queue-empty">В диалоге пока нет сообщений.</div>';
  if (nearBottom || !container.dataset.rendered) container.scrollTop = container.scrollHeight;
  container.dataset.rendered = '1';
}

function agentName(id) {
  return ({
    manager:'Мария', safety:'Алексей', therapist:'Ирина', cardiologist:'Дмитрий',
    neurologist:'Ольга', dermatologist:'Анна', pediatrician:'Сергей',
    psychologist:'Елена', general:'Максим',
  })[id] || 'Специалист';
}

function valueLabel(key, value) {
  const labels = {
    female:'Женский', male:'Мужской', yes:'Да', no:'Нет', possible:'Возможно',
    not_applicable:'Не применимо', never:'Никогда', former:'Раньше', current:'Сейчас',
    rarely:'Редко', weekly:'Еженедельно', often:'Часто', low:'Низкая',
    moderate:'Средняя', high:'Высокая', normal:'Норма', unstable:'Нестабильное',
    unknown:'Не указано',
  };
  if (value === null || value === undefined || value === '') return 'Не указано';
  if (key === 'height_cm') return `${value} см`;
  if (key === 'weight_kg') return `${value} кг`;
  return labels[value] || String(value);
}

function renderUserDetails() {
  const { conversation, profile, symptoms, memories, onboarding, lab, identities } = state.detail;
  $('#identityCard').innerHTML = `<strong>${escapeHtml(profile.preferred_name || 'Имя не указано')}</strong>
    <div>ID: <code>${escapeHtml(conversation.chel_id)}</code></div>
    <div>Мессенджеры: ${escapeHtml(identities.map(item => item.provider.toUpperCase()).join(', ') || 'не привязаны')}</div>
    <div>Телефон для созвона: ${escapeHtml(conversation.human_phone || 'не указан')}</div>`;
  const fields = [
    ['age','Возраст'], ['sex','Пол'], ['height_cm','Рост'], ['weight_kg','Вес'],
    ['smoking','Курение'], ['alcohol','Алкоголь'], ['activity','Активность'],
    ['blood_pressure','Давление'], ['blood_sugar','Сахар'], ['fatigue','Утомляемость'],
  ];
  $('#profileDetails').innerHTML = fields.map(([key,label]) =>
    `<div class="detail-item"><small>${label}</small><strong>${escapeHtml(valueLabel(key, profile[key]))}</strong></div>`).join('');
  const list = (value) => Array.isArray(value) && value.length ? value.join(', ') : 'Не указано';
  $('#medicalDetails').innerHTML = [
    ['Хронические заболевания',list(profile.conditions)],
    ['Постоянные лекарства',list(profile.medications)],
    ['Аллергии',list(profile.allergies)],
    ['Примечания',profile.notes || 'Не указано'],
  ].map(([label,value]) => `<div class="medical-group"><small>${label}</small><p>${escapeHtml(value)}</p></div>`).join('');
  $('#symptomDetails').innerHTML = symptoms.length ? symptoms.map(item =>
    `<div class="stack-item"><strong>${escapeHtml(item.symptom_type)} · ${escapeHtml(item.region)}</strong>
      Интенсивность ${item.intensity}/10 · ${item.status === 'active' ? 'активен' : 'завершён'}
      ${item.notes ? `<br>${escapeHtml(item.notes)}` : ''}</div>`).join('')
    : '<span class="empty-detail">Симптомы не отмечены</span>';
  const examNames = (onboarding.selected_tests || []).map(escapeHtml).join(', ');
  const docs = (lab.documents || []).map(item => {
    const url = safeUrl(item.url);
    return url ? `<div class="stack-item"><strong>${escapeHtml(item.title || 'Результат анализа')}</strong><a href="${escapeHtml(url)}" target="_blank" rel="noopener">Открыть документ</a></div>` : '';
  }).join('');
  const interpretations = (lab.interpretations || []).map(item =>
    `<div class="stack-item"><strong>Расшифровка ${escapeHtml(item.scope_key)}</strong>${escapeHtml(item.interpretation)}</div>`).join('');
  $('#labDetails').innerHTML = `<div class="stack-item"><strong>Номер пробирки</strong>${escapeHtml(lab.tube_number || 'Не указан')}</div>
    <div class="stack-item"><strong>Выбранные обследования</strong>${examNames || 'Не выбраны'}</div>${docs}${interpretations}`;
  $('#memoryDetails').innerHTML = memories.length ? memories.map(item =>
    `<div class="stack-item"><strong>${escapeHtml(item.category)}</strong>${escapeHtml(item.content)}</div>`).join('')
    : '<span class="empty-detail">Сохранённых фактов нет</span>';
}

async function setAiMode(enabled) {
  if (!state.selectedId || state.busy) return;
  state.busy = true;
  $('#aiSwitchLabel').classList.add('busy');
  try {
    const result = await api(`/api/manager/conversations/${encodeURIComponent(state.selectedId)}/ai-mode`, {
      method:'POST',
      body:JSON.stringify({ enabled }),
    });
    state.detail.conversation = result.conversation;
    renderHeader();
    toast(enabled ? 'ИИ включён для этого диалога' : 'ИИ выключен — пользователь ждёт менеджера');
    loadQueue();
  } catch (error) {
    $('#aiEnabled').checked = !enabled;
    toast(error.message, true);
  } finally {
    state.busy = false;
    $('#aiSwitchLabel').classList.remove('busy');
  }
}

async function sendReply(event) {
  event.preventDefault();
  const input = $('#managerReply');
  const message = input.value.trim();
  if (!message || !state.selectedId || state.busy) return;
  state.busy = true;
  try {
    const result = await api(`/api/manager/conversations/${encodeURIComponent(state.selectedId)}/reply`, {
      method:'POST',
      body:JSON.stringify({ message }),
    });
    input.value = '';
    input.style.height = '';
    state.detail.messages.push(result.message);
    state.detail.conversation = result.conversation;
    renderHeader();
    renderMessages();
    loadQueue();
  } catch (error) {
    toast(error.message, true);
  } finally { state.busy = false; }
}

async function closeRequest() {
  if (!state.selectedId || state.busy) return;
  if (!window.confirm('Закрыть обращение? ИИ снова будет отвечать пользователю, а обращение переместится из открытой очереди.')) return;
  state.busy = true;
  $('#closeRequestButton').disabled = true;
  try {
    await api(`/api/manager/conversations/${encodeURIComponent(state.selectedId)}/close`, {
      method:'POST',
      body:'{}',
    });
    toast('Обращение закрыто. ИИ снова доступен пользователю.');
    state.selectedId = null;
    state.detail = null;
    $('#chatWorkspace').classList.add('hidden');
    $('#emptyWorkspace').classList.remove('hidden');
    $('#managerApp').classList.remove('chat-open');
    await Promise.all([loadQueue(), pollManagerNotifications()]);
  } catch (error) {
    toast(error.message, true);
  } finally {
    state.busy = false;
  }
}

function closeProfile() {
  $('#userPanel').classList.remove('open');
  $('#panelBackdrop').classList.remove('open');
}

$('#loginForm').addEventListener('submit', login);
$('#logoutButton').addEventListener('click', logout);
$('#requestList').addEventListener('click', event => {
  const card = event.target.closest('[data-conversation-id]');
  if (card) selectConversation(card.dataset.conversationId);
});
$('#queueFilters').addEventListener('click', event => {
  const button = event.target.closest('[data-queue]');
  if (!button) return;
  state.queue = button.dataset.queue;
  document.querySelectorAll('#queueFilters button').forEach(item => item.classList.toggle('active', item === button));
  loadQueue();
});
let searchTimer;
$('#queueSearch').addEventListener('input', event => {
  state.search = event.target.value.trim();
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadQueue, 250);
});
$('#aiEnabled').addEventListener('change', event => setAiMode(event.target.checked));
$('#closeRequestButton').addEventListener('click', closeRequest);
$('#managerReplyForm').addEventListener('submit', sendReply);
$('#managerReply').addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    $('#managerReplyForm').requestSubmit();
  }
});
$('#managerReply').addEventListener('input', event => {
  event.target.style.height = 'auto';
  event.target.style.height = `${Math.min(event.target.scrollHeight, 130)}px`;
});
$('#profileToggle').addEventListener('click', () => {
  $('#userPanel').classList.add('open');
  $('#panelBackdrop').classList.add('open');
});
$('#profileClose').addEventListener('click', closeProfile);
$('#panelBackdrop').addEventListener('click', closeProfile);
$('#mobileQueueButton').addEventListener('click', () => $('#managerApp').classList.remove('chat-open'));
document.addEventListener('pointerdown', unlockManagerSound, {once:true});
document.addEventListener('keydown', unlockManagerSound, {once:true});

api('/api/manager/me').then(enterWorkspace).catch(() => showManagerLogin());
