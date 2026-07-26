const TOKEN_KEY = 'consilium_admin_dashboard_token';
const $ = selector => document.querySelector(selector);
const agentNames = {
  manager:'Мария · Менеджер', safety:'Алексей · Безопасность',
  therapist:'Ирина · Терапевт', cardiologist:'Дмитрий · Кардиолог',
  neurologist:'Ольга · Невролог', dermatologist:'Анна · Дерматолог',
  pediatrician:'Сергей · Педиатр', psychologist:'Елена · Психолог',
  general:'Максим · Общий специалист',
};
const statusNames = {
  complete:'Завершена', questionnaire:'Анкета', appearance:'Настройка',
  exams:'Обследования', payment:'Оплата', not_started:'Не начата',
  active:'Активен', waiting_human:'Ожидает человека', pending:'Ожидает',
  none:'Нет', chat:'Чат', call:'Созвон', not_selected:'Не выбран',
};
const summaryCards = [
  ['users_total','Пользователи','за всё время'],
  ['users_active_7d','Активные','за последние 7 дней'],
  ['onboarding_complete','Завершили старт','анкета и обследования'],
  ['max_users','Связаны с MAX','надёжная идентификация'],
  ['profiles_with_tube','С номером пробирки','могут получать результаты'],
  ['conversations_total','Диалоги','без текстов сообщений'],
  ['messages_total','Сообщения','входящие и ответы ИИ'],
  ['human_requests','Позвали человека','все обращения'],
  ['human_pending','Ожидают человека','текущая очередь'],
];

let refreshTimer;

function formatDate(value, dateOnly = false) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat('ru-RU', dateOnly
    ? {day:'2-digit',month:'2-digit'}
    : {day:'2-digit',month:'2-digit',year:'2-digit',hour:'2-digit',minute:'2-digit'}
  ).format(date);
}

function textCell(row, value, title = '') {
  const cell = document.createElement('td');
  cell.textContent = value ?? '—';
  if (title) cell.title = title;
  row.append(cell);
  return cell;
}

function statusCell(row, value, goodValues = []) {
  const cell = document.createElement('td');
  const badge = document.createElement('span');
  badge.className = `status ${goodValues.includes(value) ? 'good' : value === 'pending' || value === 'waiting_human' ? 'warn' : ''}`;
  badge.textContent = statusNames[value] || value || '—';
  cell.append(badge);
  row.append(cell);
}

function renderSummary(summary) {
  const root = $('#summaryGrid');
  root.replaceChildren();
  for (const [key,label,note] of summaryCards) {
    const card = document.createElement('article');
    card.className = 'summary-card';
    const name = document.createElement('span');
    const value = document.createElement('strong');
    const helper = document.createElement('small');
    name.textContent = label;
    value.textContent = Number(summary[key] || 0).toLocaleString('ru-RU');
    helper.textContent = note;
    card.append(name,value,helper);
    root.append(card);
  }
}

function renderActivity(items) {
  const root = $('#activityChart');
  root.replaceChildren();
  const max = Math.max(1, ...items.flatMap(item => [item.messages, item.conversations]));
  for (const item of items) {
    const day = document.createElement('div');
    day.className = 'activity-day';
    const bars = document.createElement('div');
    bars.className = 'day-bars';
    const messages = document.createElement('i');
    messages.className = 'messages';
    messages.style.height = `${Math.max(2, item.messages / max * 100)}%`;
    messages.title = `Сообщения: ${item.messages}`;
    const conversations = document.createElement('i');
    conversations.className = 'conversations';
    conversations.style.height = `${Math.max(2, item.conversations / max * 100)}%`;
    conversations.title = `Диалоги: ${item.conversations}`;
    const label = document.createElement('small');
    label.textContent = formatDate(`${item.date}T00:00:00Z`, true);
    bars.append(messages, conversations);
    day.append(bars,label);
    root.append(day);
  }
}

function renderDistribution(rootSelector, items, labelKey, valueKey, labels = {}) {
  const root = $(rootSelector);
  root.replaceChildren();
  const max = Math.max(1, ...items.map(item => Number(item[valueKey] || 0)));
  if (!items.length) {
    const empty = document.createElement('p');
    empty.textContent = 'Пока нет данных';
    empty.className = 'form-error';
    root.append(empty);
    return;
  }
  for (const item of items) {
    const row = document.createElement('div');
    row.className = 'distribution-row';
    const label = document.createElement('span');
    label.textContent = labels[item[labelKey]] || item[labelKey] || 'Не указано';
    const progress = document.createElement('div');
    progress.className = 'progress';
    const bar = document.createElement('i');
    bar.style.width = `${Number(item[valueKey] || 0) / max * 100}%`;
    const value = document.createElement('b');
    value.textContent = Number(item[valueKey] || 0).toLocaleString('ru-RU');
    progress.append(bar);
    row.append(label,progress,value);
    root.append(row);
  }
}

function renderUsers(items) {
  const root = $('#usersTable');
  root.replaceChildren();
  $('#usersCount').textContent = `${items.length} записей`;
  for (const item of items) {
    const row = document.createElement('tr');
    textCell(row,item.chel_id,item.chel_id);
    textCell(row,formatDate(item.created_at));
    textCell(row,formatDate(item.last_seen_at));
    statusCell(row,item.onboarding_status,['complete']);
    statusCell(row,item.max_linked ? 'Связан' : 'Нет',item.max_linked ? ['Связан'] : []);
    textCell(row,item.conversations);
    textCell(row,item.messages);
    root.append(row);
  }
}

function shortId(value) {
  const text = String(value || '');
  return text.length > 16 ? `${text.slice(0,8)}…${text.slice(-6)}` : text;
}

function renderConversations(items) {
  const root = $('#conversationsTable');
  root.replaceChildren();
  $('#conversationsCount').textContent = `${items.length} записей`;
  for (const item of items) {
    const row = document.createElement('tr');
    textCell(row,shortId(item.id),item.id);
    textCell(row,item.chel_id,item.chel_id);
    textCell(row,agentNames[item.active_agent] || item.active_agent);
    statusCell(row,item.status,['active']);
    statusCell(row,item.human_channel || item.human_status,['chat','call']);
    textCell(row,item.messages);
    textCell(row,formatDate(item.updated_at));
    root.append(row);
  }
}

function renderRequests(items) {
  const root = $('#requestsTable');
  root.replaceChildren();
  $('#requestsCount').textContent = `${items.length} записей`;
  for (const item of items) {
    const row = document.createElement('tr');
    textCell(row,item.ticket_id);
    textCell(row,item.chel_id,item.chel_id);
    statusCell(row,item.channel,['chat','call']);
    statusCell(row,item.human_status);
    textCell(row,item.phone || '—');
    textCell(row,formatDate(item.updated_at));
    root.append(row);
  }
}

function renderDashboard(data) {
  renderSummary(data.summary || {});
  renderActivity(data.activity || []);
  renderDistribution('#agentDistribution',data.agents || [],'agent','messages',agentNames);
  renderDistribution('#channelDistribution',data.human_channels || [],'channel','requests',statusNames);
  renderUsers(data.tables?.users || []);
  renderConversations(data.tables?.conversations || []);
  renderRequests(data.tables?.human_requests || []);
  $('#generatedAt').textContent = `Данные обновлены ${formatDate(data.generated_at)} · автообновление раз в минуту`;
}

function showLogin(message = '') {
  clearInterval(refreshTimer);
  $('#dashboard').classList.add('hidden');
  $('#loginPanel').classList.remove('hidden');
  $('#loginError').textContent = message;
  $('#loginError').classList.toggle('hidden',!message);
  requestAnimationFrame(() => $('#adminToken').focus());
}

function showDashboard() {
  $('#loginPanel').classList.add('hidden');
  $('#dashboard').classList.remove('hidden');
}

async function loadDashboard(token = sessionStorage.getItem(TOKEN_KEY)) {
  if (!token) return showLogin();
  const button = $('#refreshButton');
  button.disabled = true;
  $('#dashboardError').classList.add('hidden');
  try {
    const response = await fetch('/api/admin/dashboard', {
      headers:{Authorization:`Bearer ${token}`},
      cache:'no-store',
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 401) {
        sessionStorage.removeItem(TOKEN_KEY);
        return showLogin(data.detail || 'Неверный токен');
      }
      throw new Error(data.detail || `Ошибка сервера: ${response.status}`);
    }
    sessionStorage.setItem(TOKEN_KEY,token);
    showDashboard();
    renderDashboard(data);
    clearInterval(refreshTimer);
    refreshTimer = setInterval(() => loadDashboard(),60000);
  } catch (error) {
    if ($('#dashboard').classList.contains('hidden')) showLogin(error.message);
    else {
      $('#dashboardError').textContent = error.message;
      $('#dashboardError').classList.remove('hidden');
    }
  } finally {
    button.disabled = false;
  }
}

$('#loginForm').addEventListener('submit', event => {
  event.preventDefault();
  const token = $('#adminToken').value.trim();
  if (token) loadDashboard(token);
});
$('#refreshButton').addEventListener('click', () => loadDashboard());
$('#logoutButton').addEventListener('click', () => {
  sessionStorage.removeItem(TOKEN_KEY);
  $('#adminToken').value = '';
  showLogin();
});

loadDashboard();
