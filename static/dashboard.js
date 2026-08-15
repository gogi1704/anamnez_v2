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
const deviceNames = {
  desktop:'ПК', android:'Android', ios:'iOS', other:'Другое',
};
const registrationNames = {
  anonymous:'Анонимно', max:'MAX', telegram:'Telegram',
};
const operationNames = {
  routing:'Выбор специалиста', agent_response:'Ответ ИИ-агента',
  lab_interpretation:'Расшифровка анализов', council_opinion:'Мнение консилиума',
  council_summary:'Итог консилиума', other:'Другой запрос',
};
const searchAliases = {
  'завершена':'complete','завершено':'complete','анкета':'questionnaire',
  'не начата':'not_started','активен':'active','активный':'active',
  'ожидает':'pending','созвон':'call','звонок':'call','чат':'chat',
  'менеджер':'manager','терапевт':'therapist','кардиолог':'cardiologist',
  'невролог':'neurologist','дерматолог':'dermatologist',
  'педиатр':'pediatrician','психолог':'psychologist',
  'пк':'desktop','компьютер':'desktop','айфон':'ios',
};
const summaryCards = [
  ['users_total','Пользователи','за всё время'],
  ['users_active_7d','Активные','за последние 7 дней'],
  ['onboarding_complete','Завершили старт','анкета и обследования'],
  ['messenger_users','Связаны с мессенджером','надёжная идентификация'],
  ['tracked_devices','Определено устройство','уникальные пользователи'],
  ['profiles_with_tube','С номером пробирки','могут получать результаты'],
  ['conversations_total','Диалоги','без текстов сообщений'],
  ['messages_total','Сообщения','входящие и ответы ИИ'],
  ['human_requests','Позвали человека','все обращения'],
  ['human_pending','Ожидают человека','текущая очередь'],
];

let refreshTimer;
let dashboardLoading = false;
let staffItems = [];
let examinationItems = [];
let activeAdminView = 'dashboard';
let analyticsRecentPage = 1;
let analyticsFunnelMode = 'start';
let latestAnalyticsData = null;
const expandedFunnelRows = new Set();
const tableStates = {
  users:{apiName:'users',prefix:'users',offset:0,limit:25,total:0,query:'',createdFrom:'',createdTo:''},
  devices:{apiName:'devices',prefix:'devices',offset:0,limit:25,total:0,query:''},
  conversations:{apiName:'conversations',prefix:'conversations',offset:0,limit:25,total:0,query:''},
  requests:{apiName:'human_requests',prefix:'requests',offset:0,limit:25,total:0,query:''},
};

function formatDate(value, dateOnly = false) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat('ru-RU', dateOnly
    ? {day:'2-digit',month:'2-digit'}
    : {day:'2-digit',month:'2-digit',year:'2-digit',hour:'2-digit',minute:'2-digit'}
  ).format(date);
}

function formatTokens(value) {
  return Number(value || 0).toLocaleString('ru-RU');
}

function formatUsd(value, compact = false) {
  const amount = Number(value || 0);
  const digits = compact && Math.abs(amount) >= 0.01 ? 4 : 6;
  return `$${amount.toLocaleString('en-US',{minimumFractionDigits:digits,maximumFractionDigits:digits})}`;
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

function emptyTable(root, columns) {
  const row = document.createElement('tr');
  row.className = 'table-empty';
  const cell = document.createElement('td');
  cell.colSpan = columns;
  cell.textContent = 'Ничего не найдено';
  row.append(cell);
  root.append(row);
}

function renderUsers(items, total = items.length, counts = {}) {
  const root = $('#usersTable');
  root.replaceChildren();
  const overall = Number(counts.overallTotal ?? total);
  const period = Number(counts.periodTotal ?? total);
  $('#usersTotalCount').textContent = `Всего: ${overall.toLocaleString('ru-RU')}`;
  $('#usersPeriodCount').textContent = `Новых за период: ${period.toLocaleString('ru-RU')}`;
  $('#usersCount').textContent = `Найдено: ${Number(total).toLocaleString('ru-RU')}`;
  $('#usersCount').classList.toggle('hidden',!counts.filterActive);
  if (!items.length) emptyTable(root,8);
  for (const item of items) {
    const row = document.createElement('tr');
    textCell(row,item.chel_id,item.chel_id);
    textCell(row,item.from_manager || '—');
    textCell(row,formatDate(item.created_at));
    textCell(row,formatDate(item.last_seen_at));
    statusCell(row,item.onboarding_status,['complete']);
    const messengerNames = String(item.messengers || '').split(',').filter(Boolean)
      .map(value => value === 'telegram' ? 'Telegram' : value.toUpperCase());
    statusCell(row,messengerNames.join(' + ') || 'Нет',messengerNames.length ? ['Связан'] : []);
    textCell(row,item.conversations);
    textCell(row,item.messages);
    root.append(row);
  }
}

function shortId(value) {
  const text = String(value || '');
  return text.length > 16 ? `${text.slice(0,8)}…${text.slice(-6)}` : text;
}

function renderConversations(items, total = items.length) {
  const root = $('#conversationsTable');
  root.replaceChildren();
  $('#conversationsCount').textContent = `${total} записей`;
  if (!items.length) emptyTable(root,7);
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

function renderDevices(items, total = items.length) {
  const root = $('#devicesTable');
  root.replaceChildren();
  $('#devicesCount').textContent = `${total} записей`;
  if (!items.length) emptyTable(root,7);
  for (const item of items) {
    const row = document.createElement('tr');
    textCell(row,item.chel_id,item.chel_id);
    textCell(row,deviceNames[item.device_type] || item.device_type || 'Другое');
    textCell(row,item.operating_system);
    textCell(row,item.browser);
    textCell(row,formatDate(item.first_seen_at));
    textCell(row,formatDate(item.last_seen_at));
    textCell(row,Number(item.visit_count || 0).toLocaleString('ru-RU'));
    root.append(row);
  }
}

function renderRequests(items, total = items.length) {
  const root = $('#requestsTable');
  root.replaceChildren();
  $('#requestsCount').textContent = `${total} записей`;
  if (!items.length) emptyTable(root,6);
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
  renderDistribution('#deviceDistribution',data.devices || [],'device_type','users',deviceNames);
  renderDistribution('#osDistribution',data.operating_systems || [],'operating_system','users');
  renderDistribution('#browserDistribution',data.browsers || [],'browser','users');
  $('#generatedAt').textContent = `Данные обновлены ${formatDate(data.generated_at)} · автообновление раз в минуту`;
}

function renderCostSummary(data) {
  const summary = data.summary || {};
  const allTime = data.all_time || {};
  const cards = [
    ['Расход за период',formatUsd(summary.total_cost_usd,true),'по выбранному периоду'],
    ['Расход за всё время',formatUsd(allTime.total_cost_usd,true),'с момента установки учёта'],
    ['Всего токенов',formatTokens(summary.total_tokens),'входные и выходные'],
    ['Запросы к ИИ',formatTokens(summary.requests),'успешные ответы API'],
    ['Кешированные',formatTokens(summary.cached_input_tokens),'входные токены со скидкой'],
    ['Выходные',formatTokens(summary.output_tokens),`reasoning: ${formatTokens(summary.reasoning_tokens)}`],
  ];
  const root = $('#costSummaryGrid');
  root.replaceChildren();
  for (const [label,value,note] of cards) {
    const card = document.createElement('article');
    card.className = 'summary-card';
    const name = document.createElement('span');
    const amount = document.createElement('strong');
    const helper = document.createElement('small');
    name.textContent = label;
    amount.textContent = value;
    helper.textContent = note;
    card.append(name,amount,helper);
    root.append(card);
  }
  if (Number(summary.unpriced_requests || 0) > 0) {
    const warning = document.createElement('p');
    warning.className = 'cost-warning';
    warning.textContent = `${formatTokens(summary.unpriced_requests)} запросов не вошли в стоимость: для их модели нет тарифа в справочнике.`;
    root.append(warning);
  }
}

function renderCostChart(items) {
  const root = $('#costDailyChart');
  root.replaceChildren();
  const max = Math.max(0.000000001,...items.map(item => Number(item.total_cost_usd || 0)));
  root.style.setProperty('--cost-days',Math.max(7,items.length));
  for (const item of items) {
    const day = document.createElement('div');
    day.className = 'cost-day';
    const bar = document.createElement('i');
    const amount = Number(item.total_cost_usd || 0);
    bar.style.height = `${amount ? Math.max(3,amount/max*100) : 1}%`;
    bar.title = `${formatDate(`${item.date}T00:00:00Z`,true)} · ${formatUsd(amount)} · ${formatTokens(item.total_tokens)} токенов`;
    const label = document.createElement('small');
    label.textContent = formatDate(`${item.date}T00:00:00Z`,true);
    day.append(bar,label);
    root.append(day);
  }
}

function renderCostOperations(items) {
  const root = $('#costOperationDistribution');
  root.replaceChildren();
  const max = Math.max(0.000000001,...items.map(item => Number(item.total_cost_usd || 0)));
  if (!items.length) {
    const empty = document.createElement('p');
    empty.className = 'form-error';
    empty.textContent = 'Пока нет данных';
    root.append(empty);
    return;
  }
  for (const item of items) {
    const row = document.createElement('div');
    row.className = 'distribution-row cost-operation-row';
    const label = document.createElement('span');
    label.textContent = operationNames[item.operation] || item.operation;
    const progress = document.createElement('div');
    progress.className = 'progress';
    const bar = document.createElement('i');
    bar.style.width = `${Number(item.total_cost_usd || 0)/max*100}%`;
    progress.append(bar);
    const value = document.createElement('b');
    value.textContent = formatUsd(item.total_cost_usd);
    value.title = `${formatTokens(item.requests)} запросов · ${formatTokens(item.total_tokens)} токенов`;
    row.append(label,progress,value);
    root.append(row);
  }
}

function renderCostModels(items) {
  const root = $('#costModelsTable');
  root.replaceChildren();
  if (!items.length) emptyTable(root,7);
  for (const item of items) {
    const row = document.createElement('tr');
    textCell(row,item.model);
    textCell(row,formatTokens(item.requests));
    textCell(row,formatTokens(item.input_tokens));
    textCell(row,formatTokens(item.cached_input_tokens));
    textCell(row,formatTokens(item.output_tokens));
    textCell(row,formatTokens(item.total_tokens));
    textCell(row,item.pricing_known ? formatUsd(item.total_cost_usd) : 'Тариф не задан');
    root.append(row);
  }
}

function renderCostRecent(items) {
  const root = $('#costRecentTable');
  root.replaceChildren();
  $('#costRequestsCount').textContent = `${formatTokens(items.length)} последних`;
  if (!items.length) emptyTable(root,10);
  for (const item of items) {
    const row = document.createElement('tr');
    textCell(row,formatDate(item.created_at));
    textCell(row,operationNames[item.operation] || item.operation);
    textCell(row,item.model);
    textCell(row,shortId(item.chel_id),item.chel_id);
    textCell(row,formatTokens(item.input_tokens));
    textCell(row,formatTokens(item.cached_input_tokens));
    textCell(row,formatTokens(item.output_tokens));
    textCell(row,formatTokens(item.reasoning_tokens));
    textCell(row,formatTokens(item.total_tokens));
    textCell(row,item.pricing_known ? formatUsd(item.total_cost_usd) : '—');
    root.append(row);
  }
}

function renderPricing(items) {
  const root = $('#costPricingList');
  root.replaceChildren();
  for (const item of items) {
    const card = document.createElement('div');
    card.className = 'pricing-card';
    const model = document.createElement('strong');
    const rates = document.createElement('span');
    model.textContent = item.model;
    rates.textContent = `вход $${item.input} · кеш $${item.cached_input} · выход $${item.output}`;
    card.append(model,rates);
    root.append(card);
  }
}

async function loadCosts() {
  const period = $('#costsPeriod').value;
  const data = await adminFetch(`/api/admin/ai-costs?period=${encodeURIComponent(period)}&limit=100`);
  renderCostSummary(data);
  renderCostChart(data.daily || []);
  renderCostOperations(data.by_operation || []);
  renderCostModels(data.by_model || []);
  renderCostRecent(data.recent || []);
  renderPricing(data.pricing || []);
  $('#costsNotice').textContent = `${data.notice} Обновлено ${formatDate(data.generated_at)}.`;
}

const wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

async function adminFetch(path, token = sessionStorage.getItem(TOKEN_KEY), options = {}, retryAttempt = 0) {
  const response = await fetch(path, {
    ...options,
    headers:{
      Authorization:`Bearer ${token || ''}`,
      'Content-Type':'application/json',
      ...(options.headers || {}),
    },
    cache:'no-store',
  });
  const data = await response.json().catch(() => ({}));
  const method = String(options.method || 'GET').toUpperCase();
  if (response.status === 429 && method === 'GET' && retryAttempt < 3) {
    const retryAfterSeconds = Number(response.headers.get('Retry-After') || 0);
    const delay = retryAfterSeconds > 0
      ? Math.min(retryAfterSeconds * 1000, 10000)
      : 1000 * (2 ** retryAttempt);
    await wait(delay);
    return adminFetch(path, token, options, retryAttempt + 1);
  }
  if (!response.ok) {
    const error = new Error(data.detail || `Ошибка сервера: ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function showManagerStatus(message, error = false) {
  const status = $('#managerFormStatus');
  status.textContent = message;
  status.classList.toggle('error', error);
  status.classList.toggle('hidden', !message);
}

function renderStaff(items) {
  staffItems = items;
  $('#staffCount').textContent = `${items.length} сотрудников`;
  $('#staffList').innerHTML = items.length ? items.map(item => {
    const initial = escapeHtml(String(item.display_name || 'М').trim().charAt(0).toUpperCase() || 'М');
    const telegramLinked = Boolean(item.telegram_id);
    const maxLinked = Boolean(item.max_id);
    const userIds = Array.isArray(item.user_chel_ids) ? item.user_chel_ids : [];
    const userIdBlock = userIds.length
      ? userIds.map(chelId => `
          <div class="staff-user-id-row">
            <code title="${escapeHtml(chelId)}">${escapeHtml(chelId)}</code>
          </div>`).join('')
      : '<small>Не найден. Он появится здесь, если менеджер зарегистрируется в приложении через привязанный Telegram или MAX.</small>';
    return `
    <article class="staff-card ${item.is_active ? '' : 'inactive'}" data-staff-id="${item.id}">
      <header class="staff-card-header">
        <div class="staff-identity">
          <span class="staff-avatar" aria-hidden="true">${initial}</span>
          <div>
            <strong>${escapeHtml(item.display_name)}</strong>
            <small>Логин: ${escapeHtml(item.login)} · ID менеджера: ${item.id}</small>
          </div>
        </div>
        <span class="staff-access-status ${item.is_active ? 'active' : 'disabled'}">${item.is_active ? 'Доступ активен' : 'Доступ отключён'}</span>
      </header>

      <div class="staff-card-content">
        <section class="staff-info-section" aria-label="Мессенджеры">
          <h3>Мессенджеры</h3>
          <div class="staff-channel-row">
            <span class="staff-channel-badge telegram" aria-hidden="true">TG</span>
            <div><strong>Telegram</strong><small>${escapeHtml(item.telegram_id || 'Не привязан')}</small></div>
            <button class="staff-inline-action" data-staff-action="telegram-link" type="button">${telegramLinked ? 'Перепривязать' : 'Привязать'}</button>
          </div>
          <div class="staff-channel-row">
            <span class="staff-channel-badge max" aria-hidden="true">MAX</span>
            <div><strong>MAX</strong><small>${escapeHtml(item.max_id || 'Не привязан')}</small></div>
            <button class="staff-inline-action" data-staff-action="max-link" type="button">${maxLinked ? 'Перепривязать' : 'Привязать'}</button>
          </div>
          <button class="staff-text-action" data-staff-action="messenger-ids" type="button">Изменить ID вручную</button>
          <div class="staff-user-id">
            <h4>chel_id для тестирования анкеты</h4>
            ${userIdBlock}
          </div>
        </section>

        <section class="staff-info-section" aria-label="Уведомления">
          <h3>Уведомления</h3>
          <div class="staff-setting-row">
            <div><strong>Новые обращения</strong><small>Сигнал при каждом новом обращении</small></div>
            <button class="staff-switch ${item.notify_new_requests ? 'is-on' : ''}" data-staff-action="requests" aria-pressed="${item.notify_new_requests}" type="button">${item.notify_new_requests ? 'Включены' : 'Выключены'}</button>
          </div>
          <div class="staff-setting-row">
            <div><strong>Сообщения без ИИ</strong><small>Сигнал, когда пользователь ждёт человека</small></div>
            <button class="staff-switch ${item.notify_new_messages ? 'is-on' : ''}" data-staff-action="messages" aria-pressed="${item.notify_new_messages}" type="button">${item.notify_new_messages ? 'Включены' : 'Выключены'}</button>
          </div>
          <p class="staff-last-login">Последний вход: <strong>${escapeHtml(formatDate(item.last_login_at))}</strong></p>
        </section>
      </div>

      <footer class="staff-card-actions">
        <div class="staff-main-actions">
          <button class="reset-password" data-staff-action="name" type="button">Изменить имя</button>
          <button class="reset-password" data-staff-action="password" type="button">Новый пароль</button>
        </div>
        <div class="staff-danger-actions">
          <button class="toggle-staff" data-staff-action="toggle" data-active="${item.is_active}" type="button">${item.is_active ? 'Отключить' : 'Включить'}</button>
          <button class="delete-staff" data-staff-action="delete" type="button">Удалить</button>
        </div>
      </footer>
    </article>`;
  }).join('') : '<p class="form-error">Менеджеры ещё не созданы</p>';
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;',
  }[char]));
}

async function loadStaff() {
  renderStaff(await adminFetch('/api/admin/managers'));
}

async function deleteUserData(event) {
  event.preventDefault();
  const chelId = $('#userDataCleanupId').value.trim();
  const status = $('#userDataCleanupStatus');
  if (!/^chel_[A-Za-z0-9_-]{8,64}$/.test(chelId)) {
    status.textContent = 'Укажите корректный chel_id пользователя.';
    status.classList.add('error');
    status.classList.remove('hidden');
    return;
  }
  const confirmation = window.prompt(
    `Это действие нельзя отменить. Для полного удаления данных повторите ID пользователя:\n\n${chelId}`,
  );
  if (confirmation === null) return;
  if (confirmation.trim() !== chelId) {
    status.textContent = 'Удаление отменено: введённый ID не совпадает.';
    status.classList.add('error');
    status.classList.remove('hidden');
    return;
  }
  const button = $('#deleteUserDataButton');
  button.disabled = true;
  status.classList.add('hidden');
  try {
    const result = await adminFetch('/api/admin/users/delete-data', undefined, {
      method:'POST',
      headers:{'X-Consilium-Action':'delete-user-data'},
      body:JSON.stringify({chel_id:chelId,confirmation:chelId}),
    });
    $('#userDataCleanupForm').reset();
    status.textContent = `Все данные ${result.chel_id} удалены. Пользователь сможет пройти регистрацию и анкету заново.`;
    status.classList.remove('error','hidden');
    await loadDashboard();
  } catch (error) {
    status.textContent = error.message;
    status.classList.add('error');
    status.classList.remove('hidden');
  } finally {
    button.disabled = false;
  }
}

function showExaminationStatus(message, error = false) {
  const status = $('#examinationFormStatus');
  status.textContent = message;
  status.classList.toggle('error', error);
  status.classList.toggle('hidden', !message);
}

function resetExaminationForm() {
  $('#examinationForm').reset();
  $('#examinationId').value = '';
  $('#examinationFormTitle').textContent = 'Добавить обследование';
  $('#saveExaminationButton').textContent = 'Добавить обследование';
  $('#cancelExaminationEdit').classList.add('hidden');
}

function renderExaminations(items) {
  examinationItems = items;
  $('#examinationsCount').textContent = `${items.length} позиций`;
  $('#examinationList').innerHTML = items.length ? items.map(item => `
    <article class="examination-admin-card" data-examination-id="${escapeHtml(item.id)}">
      <div class="examination-card-heading">
        <strong>${escapeHtml(item.name)}</strong>
        <b>${Number(item.price || 0).toLocaleString('ru-RU')} ₽</b>
      </div>
      <p>${escapeHtml(item.description)}</p>
      <small><b>Состав:</b> ${escapeHtml(item.includes || 'Не указан')}</small>
      <div class="examination-card-actions">
        <button type="button" class="edit-examination" data-examination-action="edit">Изменить</button>
        <button type="button" class="delete-examination" data-examination-action="delete">Удалить</button>
      </div>
    </article>`).join('') : '<p class="form-error">В каталоге пока нет обследований</p>';
}

async function loadExaminations() {
  renderExaminations(await adminFetch('/api/admin/examinations'));
}

const analyticsEventNames = {
  registration_completed:'Регистрация завершена', appearance_completed:'Размер текста выбран',
  questionnaire_started:'Вход в анкету', questionnaire_completed:'Выход из анкеты',
  examinations_offer_viewed:'Предложение обследований', examinations_opened:'Список обследований открыт',
  examinations_selection_completed:'Выбор обследований завершён', examination_selection_confirmed:'Обследование подтверждено', onboarding_completed:'Старт завершён',
  capabilities_viewed:'Возможности показаны', chat_opened:'Чат открыт', first_message_sent:'Первое сообщение',
  human_requested:'Запрошен человек', api_error:'Ошибка API', javascript_error:'Ошибка браузера',
};

function fillAnalyticsSelect(selector, values, emptyLabel) {
  const select = $(selector);
  const current = select.value;
  select.replaceChildren(new Option(emptyLabel,''), ...values.map(value => new Option(
    selector === '#analyticsDevice' ? (deviceNames[value] || value)
      : selector === '#analyticsMethod' ? (registrationNames[value] || value)
      : value,
    value,
  )));
  if ([...select.options].some(option => option.value === current)) select.value = current;
}

function renderAnalyticsFunnel(items = []) {
  const funnel = $('#analyticsFunnel');
  funnel.replaceChildren();
  const fromPrevious = analyticsFunnelMode === 'previous';
  $('#funnelFromStart').classList.toggle('active', !fromPrevious);
  $('#funnelFromPrevious').classList.toggle('active', fromPrevious);
  $('#funnelFromStart').setAttribute('aria-selected', String(!fromPrevious));
  $('#funnelFromPrevious').setAttribute('aria-selected', String(fromPrevious));
  for (const item of items) {
    const percent = fromPrevious ? item.from_previous : item.from_start;
    const stage = document.createElement('section'); stage.className = 'funnel-stage';
    const expandable = Boolean(item.details?.length);
    const row = document.createElement(expandable ? 'button' : 'div'); row.className = `funnel-row${expandable ? ' funnel-row-button' : ''}`;
    if (expandable) row.type = 'button';
    const label = document.createElement('span'); label.className = 'funnel-label'; label.textContent = item.label;
    if (expandable) {
      const chevron = document.createElement('i'); chevron.className = 'funnel-chevron'; chevron.textContent = '⌄'; label.append(chevron);
      row.setAttribute('aria-expanded', String(expandedFunnelRows.has(item.event_name)));
    }
    const progress = document.createElement('div'); progress.className = 'progress';
    const bar = document.createElement('i'); bar.style.width = `${Math.max(0,percent || 0)}%`; progress.append(bar);
    const users = document.createElement('b'); users.textContent = Number(item.users || 0).toLocaleString('ru-RU');
    const conversion = document.createElement('small'); conversion.className = 'funnel-conversion';
    conversion.textContent = `${percent || 0}% ${fromPrevious ? 'от предыдущего' : 'от начала'}`;
    const dropoff = document.createElement('small'); dropoff.className = 'dropoff'; dropoff.textContent = item.dropoff ? `−${item.dropoff}` : '—';
    row.append(label,progress,users,conversion,dropoff); stage.append(row);
    if (expandable) {
      const details = document.createElement('div'); details.className = 'funnel-breakdown';
      details.classList.toggle('hidden', !expandedFunnelRows.has(item.event_name));
      const max = Math.max(1,...item.details.map(detail => Number(detail.users || 0)));
      for (const detail of item.details) {
        const detailRow = document.createElement('div'); detailRow.className = 'funnel-breakdown-row';
        const detailLabel = document.createElement('span'); detailLabel.textContent = detail.label;
        const detailProgress = document.createElement('div'); detailProgress.className = 'progress';
        const detailBar = document.createElement('i'); detailBar.style.width = `${Number(detail.users || 0) / max * 100}%`; detailProgress.append(detailBar);
        const detailUsers = document.createElement('b'); detailUsers.textContent = Number(detail.users || 0).toLocaleString('ru-RU'); detailUsers.title = 'Уникальные пользователи';
        const detailEvents = document.createElement('small'); detailEvents.textContent = `${Number(detail.events || 0).toLocaleString('ru-RU')} нажатий`;
        detailRow.append(detailLabel,detailProgress,detailUsers,detailEvents); details.append(detailRow);
      }
      row.addEventListener('click', () => {
        if (expandedFunnelRows.has(item.event_name)) expandedFunnelRows.delete(item.event_name);
        else expandedFunnelRows.add(item.event_name);
        const open = expandedFunnelRows.has(item.event_name);
        row.setAttribute('aria-expanded', String(open)); details.classList.toggle('hidden', !open);
      });
      stage.append(details);
    }
    funnel.append(stage);
  }
}

function renderExaminationAnalytics(items = [], summary = {}) {
  const root = $('#analyticsExaminations');
  const summaryNode = $('#analyticsExaminationsSummary');
  root.replaceChildren();
  summaryNode.textContent = [
    `${Number(summary.users_with_selection || 0).toLocaleString('ru-RU')} пользователей выбрали`,
    `${Number(summary.selected_items || 0).toLocaleString('ru-RU')} позиций`,
  ].join(' · ');
  if (!items.length) {
    const empty = document.createElement('p');
    empty.className = 'form-error';
    empty.textContent = 'Пока нет подтверждённых выборов обследований за этот период';
    root.append(empty);
    return;
  }
  const max = Math.max(1, ...items.map(item => Number(item.users || 0)));
  for (const item of items) {
    const row = document.createElement('div');
    row.className = 'examination-popularity-row';
    const heading = document.createElement('div');
    heading.className = 'examination-popularity-heading';
    const label = document.createElement('strong');
    label.textContent = item.label || item.exam_id || 'Обследование';
    const count = document.createElement('b');
    count.textContent = `${Number(item.users || 0).toLocaleString('ru-RU')} чел.`;
    heading.append(label, count);
    const track = document.createElement('div');
    track.className = 'examination-popularity-track';
    const bar = document.createElement('i');
    bar.style.width = `${Number(item.users || 0) / max * 100}%`;
    track.append(bar);
    const details = document.createElement('div');
    details.className = 'examination-popularity-details';
    details.textContent = `${Number(item.percent_of_selectors || 0).toLocaleString('ru-RU')}% среди выбравших обследования · ${Number(item.percent_of_completed || 0).toLocaleString('ru-RU')}% среди завершивших этап`;
    row.append(heading, track, details);
    root.append(row);
  }
}

function renderManagerAttribution(data = {}) {
  const root = $('#managerAttributionTable');
  const summary = $('#managerAttributionSummary');
  const items = data.managers || [];
  root.replaceChildren();
  summary.textContent = [
    `${Number(data.attributed_users || 0).toLocaleString('ru-RU')} с меткой`,
    `${Number(data.attributed_percent || 0).toLocaleString('ru-RU')}% от всех`,
  ].join(' · ');
  if (!items.length) {
    emptyTable(root,5);
    return;
  }
  for (const item of items) {
    const row = document.createElement('tr');
    textCell(row,item.from_manager || '—');
    textCell(row,Number(item.users || 0).toLocaleString('ru-RU'));
    textCell(row,`${Number(item.percent_of_all || 0).toLocaleString('ru-RU')}%`);
    textCell(row,Number(item.users_with_examinations || 0).toLocaleString('ru-RU'));
    textCell(row,`${Number(item.examination_conversion || 0).toLocaleString('ru-RU')}%`);
    root.append(row);
  }
}

function renderAnalytics(data) {
  latestAnalyticsData = data;
  const summary = $('#analyticsSummary');
  summary.replaceChildren();
  for (const [label,value,note] of [
    ['Пользователи',data.summary.users,'завершили выбор регистрации'],
    ['Посетители',data.summary.visitors,'включая экран входа'],
    ['Сессии',data.summary.sessions,'визиты в сервис'],
    ['События',data.summary.events,'технические действия'],
  ]) {
    const card = document.createElement('article'); card.className = 'analytics-metric';
    const title = document.createElement('span'); title.textContent = label;
    const number = document.createElement('strong'); number.textContent = Number(value || 0).toLocaleString('ru-RU');
    const helper = document.createElement('small'); helper.textContent = note;
    card.append(title,number,helper); summary.append(card);
  }

  renderAnalyticsFunnel(data.funnel || []);

  const daily = $('#analyticsDaily'); daily.replaceChildren();
  const maxDaily = Math.max(1,...(data.daily || []).map(item => Number(item.users || 0)));
  for (const item of data.daily || []) {
    const day = document.createElement('div'); day.className = 'analytics-day';
    const bar = document.createElement('i'); bar.style.height = `${Math.max(2,Number(item.users || 0)/maxDaily*100)}%`; bar.title = `${item.users} пользователей`;
    const label = document.createElement('small'); label.textContent = formatDate(`${item.date}T00:00:00Z`,true);
    day.append(bar,label); daily.append(day);
  }
  if (!(data.daily || []).length) daily.textContent = 'Пока нет данных';
  renderDistribution('#analyticsDevices',data.devices || [],'label','users',deviceNames);
  renderDistribution('#analyticsRegistrations',data.registrations || [],'label','users',registrationNames);
  renderDistribution('#analyticsSources',data.sources || [],'label','users');
  renderManagerAttribution(data.manager_attribution || {});
  renderExaminationAnalytics(data.examinations || [], data.examination_summary || {});

  const questions = $('#analyticsQuestionsChart'); questions.replaceChildren();
  if (!(data.questions || []).length) {
    const empty = document.createElement('p'); empty.className = 'form-error'; empty.textContent = 'Пока нет данных по заполнению анкеты'; questions.append(empty);
  }
  for (const item of data.questions || []) {
    const row = document.createElement('div'); row.className = 'questionnaire-row';
    const heading = document.createElement('div'); heading.className = 'questionnaire-row-heading';
    const label = document.createElement('strong'); label.textContent = item.label;
    const conversion = document.createElement('b'); conversion.textContent = `${item.conversion}%`;
    heading.append(label,conversion);
    const track = document.createElement('div'); track.className = 'questionnaire-track';
    const answered = document.createElement('i'); answered.style.width = `${Math.max(0,Math.min(100,item.conversion || 0))}%`; track.append(answered);
    const details = document.createElement('div'); details.className = 'questionnaire-details';
    const duration = item.avg_duration_ms ? `${Math.round(item.avg_duration_ms / 100) / 10} с` : '—';
    details.textContent = `Увидели: ${item.viewed} · Ответили: ${item.answered} · Пропустили: ${item.skipped} · Назад: ${item.back_count} · Ошибки: ${item.validation_errors} · Среднее время: ${duration}`;
    row.append(heading,track,details); questions.append(row);
  }
  const recent = $('#analyticsRecent'); recent.replaceChildren();
  if (!(data.recent || []).length) emptyTable(recent,6);
  for (const item of data.recent || []) {
    const row = document.createElement('tr');
    textCell(row,formatDate(item.received_at));
    textCell(row,analyticsEventNames[item.event_name] || item.event_name);
    textCell(row,item.chel_id || '—'); textCell(row,item.screen || item.step_key || '—');
    textCell(row,deviceNames[item.device_type] || item.device_type); textCell(row,item.browser);
    recent.append(row);
  }
  const errorCount = (data.errors || []).reduce((sum,item) => sum + Number(item.events || 0),0);
  $('#analyticsErrors').textContent = `${errorCount.toLocaleString('ru-RU')} ошибок`;
  const pagination = data.recent_pagination || {page:1,pages:1,total:data.recent?.length || 0};
  analyticsRecentPage = pagination.page;
  $('#analyticsRecentPage').textContent = `Страница ${pagination.page} из ${pagination.pages} · ${Number(pagination.total || 0).toLocaleString('ru-RU')} событий`;
  $('#analyticsRecentPrev').disabled = pagination.page <= 1;
  $('#analyticsRecentNext').disabled = pagination.page >= pagination.pages;
  fillAnalyticsSelect('#analyticsDevice',data.filter_options?.devices || [],'Все устройства');
  fillAnalyticsSelect('#analyticsMethod',data.filter_options?.methods || [],'Все способы');
  fillAnalyticsSelect('#analyticsSource',data.filter_options?.sources || [],'Все источники');
}

async function loadAnalytics() {
  const params = new URLSearchParams({period:$('#analyticsPeriod').value,recent_page:String(analyticsRecentPage),recent_limit:'25'});
  for (const [key,selector] of [['device','#analyticsDevice'],['method','#analyticsMethod'],['source','#analyticsSource']]) {
    if ($(selector).value) params.set(key,$(selector).value);
  }
  renderAnalytics(await adminFetch(`/api/admin/analytics?${params}`));
}

function showAdminView(view) {
  activeAdminView = ['analytics','managers','examinations','costs'].includes(view) ? view : 'dashboard';
  const analyticsVisible = activeAdminView === 'analytics';
  const managersVisible = activeAdminView === 'managers';
  const examinationsVisible = activeAdminView === 'examinations';
  const costsVisible = activeAdminView === 'costs';
  $('#dashboard').classList.toggle('show-managers', managersVisible);
  $('#dashboard').classList.toggle('show-examinations', examinationsVisible);
  $('#dashboard').classList.toggle('show-costs', costsVisible);
  $('#dashboard').classList.toggle('show-analytics', analyticsVisible);
  $('#analyticsAdminView').classList.toggle('hidden', !analyticsVisible);
  $('#managerAdminView').classList.toggle('hidden', !managersVisible);
  $('#examinationAdminView').classList.toggle('hidden', !examinationsVisible);
  $('#costsAdminView').classList.toggle('hidden', !costsVisible);
  $('#dashboardTab').classList.toggle('active', activeAdminView === 'dashboard');
  $('#analyticsTab').classList.toggle('active', analyticsVisible);
  $('#managersTab').classList.toggle('active', managersVisible);
  $('#examinationsTab').classList.toggle('active', examinationsVisible);
  $('#costsTab').classList.toggle('active', costsVisible);
  if (managersVisible) loadStaff().catch(showDashboardError);
  if (examinationsVisible) loadExaminations().catch(showDashboardError);
  if (costsVisible) loadCosts().catch(showDashboardError);
  if (analyticsVisible) loadAnalytics().catch(showDashboardError);
}

async function createManager(event) {
  event.preventDefault();
  showManagerStatus('');
  const form = $('#managerCreateForm');
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    await adminFetch('/api/admin/managers', undefined, {
      method:'POST',
      body:JSON.stringify({
        display_name:$('#staffDisplayName').value.trim(),
        login:$('#staffLogin').value.trim(),
        password:$('#staffPassword').value,
        telegram_id:$('#staffTelegramId').value.trim(),
        max_id:$('#staffMaxId').value.trim(),
        notify_new_requests:$('#staffNotifyRequests').checked,
        notify_new_messages:$('#staffNotifyMessages').checked,
      }),
    });
    form.reset();
    showManagerStatus('Менеджер создан. Теперь он может войти на странице /manager.');
    await loadStaff();
  } catch (error) {
    showManagerStatus(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function updateManager(event) {
  const button = event.target.closest('[data-staff-action]');
  if (!button) return;
  const card = button.closest('[data-staff-id]');
  const manager = staffItems.find(item => item.id === Number(card?.dataset.staffId));
  if (!manager) return;
  const action = button.dataset.staffAction;
  const payload = {};
  if (action === 'telegram-link' || action === 'max-link') {
    button.disabled = true;
    try {
      const provider = action === 'telegram-link' ? 'telegram' : 'max';
      const result = await adminFetch(`/api/admin/managers/${manager.id}/messenger-link`, undefined, {
        method:'POST', body:JSON.stringify({provider}),
      });
      window.prompt(`Отправьте эту одноразовую ссылку менеджеру ${manager.display_name}. Она действует 7 дней:`, result.bot_url);
      showManagerStatus(`Ссылка для привязки ${provider === 'telegram' ? 'Telegram' : 'MAX'} создана.`);
    } catch (error) {
      showManagerStatus(error.message, true);
    } finally {
      button.disabled = false;
    }
    return;
  } else if (action === 'messenger-ids') {
    const telegramId = window.prompt('Telegram ID (оставьте пустым, чтобы отвязать)', manager.telegram_id || '');
    if (telegramId === null) return;
    const maxId = window.prompt('MAX ID (оставьте пустым, чтобы отвязать)', manager.max_id || '');
    if (maxId === null) return;
    payload.telegram_id = telegramId.trim();
    payload.max_id = maxId.trim();
  } else if (action === 'requests') {
    payload.notify_new_requests = !manager.notify_new_requests;
  } else if (action === 'messages') {
    payload.notify_new_messages = !manager.notify_new_messages;
  } else if (action === 'name') {
    const displayName = window.prompt('Новое имя менеджера', manager.display_name);
    if (displayName === null) return;
    payload.display_name = displayName.trim();
  } else if (action === 'password') {
    const password = window.prompt('Новый пароль (не короче 6 символов)');
    if (password === null) return;
    payload.password = password;
  } else if (action === 'toggle') {
    if (manager.is_active && !window.confirm(`Отключить доступ для ${manager.display_name}? Его активные сеансы будут завершены.`)) return;
    payload.is_active = !manager.is_active;
  } else if (action === 'delete') {
    if (!window.confirm(
      `Удалить менеджера ${manager.display_name} (${manager.login}) навсегда? ` +
      'Его активные сеансы будут завершены. История ответов в диалогах сохранится.'
    )) return;
  }
  button.disabled = true;
  showManagerStatus('');
  try {
    if (action === 'delete') {
      await adminFetch(`/api/admin/managers/${manager.id}`, undefined, {method:'DELETE'});
      showManagerStatus('Менеджер удалён. История его ответов в пользовательских диалогах сохранена.');
    } else {
      await adminFetch(`/api/admin/managers/${manager.id}`, undefined, {
        method:'POST',
        body:JSON.stringify(payload),
      });
      showManagerStatus(action === 'password'
        ? 'Пароль изменён. Все прежние сеансы менеджера завершены.'
        : 'Данные менеджера обновлены.');
    }
    await loadStaff();
  } catch (error) {
    showManagerStatus(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function saveExamination(event) {
  event.preventDefault();
  const examinationId = $('#examinationId').value;
  const button = $('#saveExaminationButton');
  button.disabled = true;
  showExaminationStatus('');
  const payload = {
    name:$('#examinationName').value.trim(),
    description:$('#examinationDescription').value.trim(),
    includes:$('#examinationIncludes').value.trim(),
    price:$('#examinationPrice').value,
  };
  try {
    await adminFetch(
      examinationId
        ? `/api/admin/examinations/${encodeURIComponent(examinationId)}`
        : '/api/admin/examinations',
      undefined,
      {method:'POST',body:JSON.stringify(payload)},
    );
    showExaminationStatus(
      examinationId ? 'Обследование обновлено.' : 'Обследование добавлено в каталог.',
    );
    resetExaminationForm();
    await loadExaminations();
  } catch (error) {
    showExaminationStatus(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function manageExamination(event) {
  const button = event.target.closest('[data-examination-action]');
  if (!button) return;
  const card = button.closest('[data-examination-id]');
  const item = examinationItems.find(
    examination => examination.id === card?.dataset.examinationId,
  );
  if (!item) return;
  if (button.dataset.examinationAction === 'edit') {
    $('#examinationId').value = item.id;
    $('#examinationName').value = item.name;
    $('#examinationDescription').value = item.description;
    $('#examinationIncludes').value = item.includes || '';
    $('#examinationPrice').value = item.price;
    $('#examinationFormTitle').textContent = 'Изменить обследование';
    $('#saveExaminationButton').textContent = 'Сохранить изменения';
    $('#cancelExaminationEdit').classList.remove('hidden');
    showExaminationStatus('');
    $('#examinationForm').scrollIntoView({behavior:'smooth',block:'start'});
    return;
  }
  if (!window.confirm(
    `Удалить обследование «${item.name}»? Оно исчезнет из выбора пользователей.`
  )) return;
  button.disabled = true;
  try {
    await adminFetch(
      `/api/admin/examinations/${encodeURIComponent(item.id)}`,
      undefined,
      {method:'DELETE'},
    );
    if ($('#examinationId').value === item.id) resetExaminationForm();
    showExaminationStatus('Обследование удалено из каталога.');
    await loadExaminations();
  } catch (error) {
    showExaminationStatus(error.message, true);
    button.disabled = false;
  }
}

function renderTablePage(state) {
  const page = Math.floor(state.offset / state.limit) + 1;
  const pages = Math.max(1,Math.ceil(state.total / state.limit));
  $(`#${state.prefix}Page`).textContent = `Страница ${page} из ${pages}`;
  $(`#${state.prefix}Prev`).disabled = state.offset <= 0;
  $(`#${state.prefix}Next`).disabled = state.offset + state.limit >= state.total;
}

async function loadTable(key, reset = false) {
  const state = tableStates[key];
  if (reset) state.offset = 0;
  const input = $(`#${state.prefix}Search`);
  state.query = input.value.trim();
  const apiQuery = searchAliases[state.query.toLocaleLowerCase('ru-RU')] || state.query;
  const params = new URLSearchParams({
    name:state.apiName,query:apiQuery,
    limit:String(state.limit),offset:String(state.offset),
  });
  if (key === 'users') {
    state.createdFrom = $('#usersDateFrom').value;
    state.createdTo = $('#usersDateTo').value;
    if (state.createdFrom) params.set('created_from',state.createdFrom);
    if (state.createdTo) params.set('created_to',state.createdTo);
  }
  const data = await adminFetch(`/api/admin/table?${params}`);
  state.total = data.total;
  state.offset = data.offset;
  if (key === 'users') renderUsers(data.rows,data.total,{
    overallTotal:data.overall_total,
    periodTotal:data.period_total,
    filterActive:Boolean(state.query || state.createdFrom || state.createdTo),
  });
  else if (key === 'devices') renderDevices(data.rows,data.total);
  else if (key === 'conversations') renderConversations(data.rows,data.total);
  else renderRequests(data.rows,data.total);
  renderTablePage(state);
}

async function loadAllTables() {
  for (const key of Object.keys(tableStates)) await loadTable(key);
}

function bindTableControls(key) {
  const state = tableStates[key];
  const input = $(`#${state.prefix}Search`);
  let debounce;
  input.addEventListener('input', () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => loadTable(key,true).catch(showDashboardError),300);
  });
  $(`#${state.prefix}Clear`).addEventListener('click', () => {
    input.value = '';
    if (key === 'users') resetUserPeriod();
    loadTable(key,true).catch(showDashboardError);
    input.focus();
  });
  $(`#${state.prefix}Prev`).addEventListener('click', () => {
    state.offset = Math.max(0,state.offset-state.limit);
    loadTable(key).catch(showDashboardError);
  });
  $(`#${state.prefix}Next`).addEventListener('click', () => {
    if (state.offset + state.limit < state.total) state.offset += state.limit;
    loadTable(key).catch(showDashboardError);
  });
}

function inputDate(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth()+1).padStart(2,'0');
  const day = String(date.getDate()).padStart(2,'0');
  return `${year}-${month}-${day}`;
}

function setUserPeriod(value) {
  const custom = value === 'custom';
  $('#usersCustomPeriod').classList.toggle('hidden',!custom);
  if (custom) return;
  const from = $('#usersDateFrom');
  const to = $('#usersDateTo');
  if (value === 'all') {
    from.value = '';
    to.value = '';
    return;
  }
  const today = new Date();
  const first = new Date(today);
  if (value !== 'today') first.setDate(first.getDate()-(Number(value)-1));
  from.value = inputDate(first);
  to.value = inputDate(today);
}

function resetUserPeriod() {
  $('#usersPeriod').value = 'all';
  setUserPeriod('all');
}

function showDashboardError(error) {
  if (error.status === 401) {
    sessionStorage.removeItem(TOKEN_KEY);
    return showLogin(error.message);
  }
  $('#dashboardError').textContent = error.message;
  $('#dashboardError').classList.remove('hidden');
}

function showLogin(message = '') {
  clearInterval(refreshTimer);
  $('#dashboard').classList.add('hidden');
  showAdminView('dashboard');
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
  if (dashboardLoading) return;
  dashboardLoading = true;
  const button = $('#refreshButton');
  button.disabled = true;
  $('#dashboardError').classList.add('hidden');
  try {
    const data = await adminFetch('/api/admin/dashboard',token);
    sessionStorage.setItem(TOKEN_KEY,token);
    showDashboard();
    renderDashboard(data);
    if (activeAdminView === 'dashboard') await loadAllTables();
    else if (activeAdminView === 'managers') await loadStaff();
    else if (activeAdminView === 'examinations') await loadExaminations();
    else if (activeAdminView === 'costs') await loadCosts();
    else if (activeAdminView === 'analytics') await loadAnalytics();
    clearInterval(refreshTimer);
    refreshTimer = setInterval(() => loadDashboard(),60000);
  } catch (error) {
    if (error.status === 401) {
      sessionStorage.removeItem(TOKEN_KEY);
      return showLogin(error.message);
    }
    if ($('#dashboard').classList.contains('hidden')) showLogin(error.message);
    else showDashboardError(error);
  } finally {
    dashboardLoading = false;
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
$('#dashboardTab').addEventListener('click', () => showAdminView('dashboard'));
$('#analyticsTab').addEventListener('click', () => showAdminView('analytics'));
$('#managersTab').addEventListener('click', () => showAdminView('managers'));
$('#examinationsTab').addEventListener('click', () => showAdminView('examinations'));
$('#costsTab').addEventListener('click', () => showAdminView('costs'));
$('#managerCreateForm').addEventListener('submit', createManager);
$('#userDataCleanupForm').addEventListener('submit', deleteUserData);
$('#staffList').addEventListener('click', updateManager);
$('#examinationForm').addEventListener('submit', saveExamination);
$('#examinationList').addEventListener('click', manageExamination);
$('#cancelExaminationEdit').addEventListener('click', () => {
  resetExaminationForm();
  showExaminationStatus('');
});
$('#costsPeriod').addEventListener('change', () => loadCosts().catch(showDashboardError));
$('#analyticsApply').addEventListener('click', () => {
  analyticsRecentPage = 1;
  loadAnalytics().catch(showDashboardError);
});
$('#funnelFromStart').addEventListener('click', () => {
  analyticsFunnelMode = 'start';
  renderAnalyticsFunnel(latestAnalyticsData?.funnel || []);
});
$('#funnelFromPrevious').addEventListener('click', () => {
  analyticsFunnelMode = 'previous';
  renderAnalyticsFunnel(latestAnalyticsData?.funnel || []);
});
$('#analyticsRecentPrev').addEventListener('click', () => {
  if (analyticsRecentPage <= 1) return;
  analyticsRecentPage -= 1;
  loadAnalytics().catch(showDashboardError);
});
$('#analyticsRecentNext').addEventListener('click', () => {
  analyticsRecentPage += 1;
  loadAnalytics().catch(showDashboardError);
});

$('#usersPeriod').addEventListener('change', event => {
  setUserPeriod(event.target.value);
  if (event.target.value !== 'custom') loadTable('users',true).catch(showDashboardError);
});
for (const selector of ['#usersDateFrom','#usersDateTo']) {
  $(selector).addEventListener('change', () => {
    $('#usersPeriod').value = 'custom';
    $('#usersCustomPeriod').classList.remove('hidden');
    loadTable('users',true).catch(showDashboardError);
  });
}

Object.keys(tableStates).forEach(bindTableControls);
loadDashboard();
