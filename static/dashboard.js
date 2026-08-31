const TOKEN_KEY = 'consilium_admin_dashboard_token';
const FAVORITES_KEY = 'consilium_admin_analytics_favorites_v1';
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
  succeeded:'Оплачено', canceled:'Отменено', abandoned:'Не завершено',
  failed:'Ошибка', creating:'Создаётся', waiting_for_capture:'Подтверждается',
};
const deviceNames = {
  desktop:'ПК', android:'Android', ios:'iOS', other:'Другое',
};
const registrationNames = {
  anonymous:'Анонимно', max:'MAX', telegram:'Telegram', result:'Ссылка результатов',
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
  ['result_entry_users','Пришли за результатами','по специальной ссылке result'],
  ['conversations_total','Диалоги','без текстов сообщений'],
  ['messages_total','Сообщения','входящие и ответы ИИ'],
  ['human_requests','Позвали человека','все обращения'],
  ['human_pending','Ожидают человека','текущая очередь'],
];

let dashboardLoading = false;
let topProgressRequests = 0;
let staffItems = [];
let examinationItems = [];
let activeAdminView = 'dashboard';
let analyticsRecentPage = 1;
let analyticsFunnelMode = 'start';
let latestAnalyticsData = null;
let latestMetric2Data = null;
let metric2ActiveFlow = 'standard';
const adminGetRequests = new Map();
const loadedAdminViews = new Set();
const panelLoadingCounts = new WeakMap();
const expandedFunnelRows = new Set();
const favoriteAnalytics = new Set((() => {
  try {
    const value = JSON.parse(localStorage.getItem(FAVORITES_KEY) || '[]');
    return Array.isArray(value) ? value.filter(item => typeof item === 'string') : [];
  } catch { return []; }
})());
const favoriteDefinitions = [
  ['dashboard-activity','.activity-panel','Активность','dashboard'],
  ['dashboard-agents','#agentDistribution','Ответы агентов','dashboard'],
  ['dashboard-channels','#channelDistribution','Каналы обращений','dashboard'],
  ['dashboard-devices','#deviceDistribution','Устройства пользователей','dashboard'],
  ['dashboard-os','#osDistribution','Операционные системы','dashboard'],
  ['dashboard-browsers','#browserDistribution','Браузеры','dashboard'],
  ['dashboard-users','#usersTable','Пользователи','dashboard'],
  ['dashboard-devices-table','#devicesTable','Таблица устройств','dashboard'],
  ['dashboard-conversations','#conversationsTable','Диалоги','dashboard'],
  ['dashboard-requests','#requestsTable','Обращения к человеку','dashboard'],
  ['analytics-funnel','#analyticsFunnel','Полная воронка','analytics'],
  ['analytics-daily','#analyticsDaily','Пользователи по дням','analytics'],
  ['analytics-devices','#analyticsDevices','Устройства','analytics'],
  ['analytics-registrations','#analyticsRegistrations','Способы регистрации','analytics'],
  ['analytics-sources','#analyticsSources','Источники','analytics'],
  ['analytics-managers','#managerAttributionTable','Статистика по менеджерам','analytics'],
  ['analytics-examinations','#analyticsExaminations','Популярность обследований','analytics'],
  ['analytics-payments','.payment-analytics-panel','Статистика по оплате','analytics'],
  ['analytics-questionnaire','#analyticsQuestionsChart','Статистика по анкете','analytics'],
  ['analytics-events','#analyticsRecent','Ошибки и последние события','analytics'],
  ['cost-daily','#costDailyChart','Расход по дням','costs'],
  ['cost-operations','#costOperationDistribution','Расходы по операциям','costs'],
  ['cost-models','#costModelsTable','Токены и стоимость по моделям','costs'],
  ['cost-recent','#costRecentTable','Последние вызовы ИИ','costs'],
  ['cost-pricing','#costPricingList','Цена за 1 млн токенов','costs'],
];
const tableStates = {
  users:{apiName:'users',prefix:'users',offset:0,limit:25,total:0,query:'',createdFrom:'',createdTo:'',sort:'last_seen_at',order:'desc'},
  devices:{apiName:'devices',prefix:'devices',offset:0,limit:25,total:0,query:'',sort:'last_seen_at',order:'desc'},
  conversations:{apiName:'conversations',prefix:'conversations',offset:0,limit:25,total:0,query:'',sort:'updated_at',order:'desc'},
  requests:{apiName:'human_requests',prefix:'requests',offset:0,limit:25,total:0,query:'',sort:'updated_at',order:'desc'},
};

function favoriteSourceBlock(node) {
  return node?.closest('.panel,.summary-card,.analytics-metric') || node;
}

function findFavoriteSource(id) {
  return [...document.querySelectorAll('[data-favorite-id]')]
    .find(node => node.dataset.favoriteId === id) || null;
}

function favoriteButton(id, title) {
  const button = document.createElement('button');
  const selected = favoriteAnalytics.has(id);
  button.type = 'button';
  button.className = `favorite-toggle${selected ? ' selected' : ''}`;
  button.dataset.favoriteToggle = id;
  button.setAttribute('aria-pressed', String(selected));
  button.setAttribute('aria-label', selected ? `Убрать «${title}» из избранного` : `Добавить «${title}» в избранное`);
  button.title = selected ? 'Убрать из избранного' : 'Добавить в избранное';
  button.textContent = selected ? '★' : '☆';
  return button;
}

function decorateFavoriteSources() {
  for (const [id,selector,title,view] of favoriteDefinitions) {
    const block = favoriteSourceBlock($(selector));
    if (!block) continue;
    block.dataset.favoriteId = id;
    block.dataset.favoriteTitle = title;
    block.dataset.favoriteView = view;
  }
  for (const block of document.querySelectorAll('[data-favorite-id]')) {
    block.classList.add('favorite-source');
    const title = block.dataset.favoriteTitle || 'Аналитика';
    const existing = block.querySelector(':scope > .favorite-toggle,:scope > .panel-heading > .favorite-toggle');
    if (existing) existing.remove();
    const host = block.querySelector(':scope > .panel-heading') || block;
    host.append(favoriteButton(block.dataset.favoriteId,title));
  }
  $('#favoritesCount').textContent = String(favoriteAnalytics.size);
}

function saveFavoriteAnalytics() {
  localStorage.setItem(FAVORITES_KEY,JSON.stringify([...favoriteAnalytics]));
  decorateFavoriteSources();
  if (activeAdminView === 'favorites') renderFavorites();
}

function renderFavorites() {
  const root = $('#favoritesGrid');
  root.replaceChildren();
  if (!favoriteAnalytics.size) {
    const empty = document.createElement('div');
    empty.className = 'favorites-empty';
    empty.innerHTML = '<span>☆</span><h3>Здесь пока пусто</h3><p>Откройте любой раздел аналитики и нажмите звезду у нужного показателя, графика или таблицы.</p>';
    root.append(empty);
    return;
  }
  for (const id of favoriteAnalytics) {
    const source = findFavoriteSource(id);
    if (!source) continue;
    const title = source.dataset.favoriteTitle || 'Аналитика';
    const view = source.dataset.favoriteView || 'dashboard';
    const card = document.createElement('article');
    card.className = `favorite-card${source.matches('.table-panel,.funnel-panel,.activity-panel,.questionnaire-analytics-panel,.examination-analytics-panel,.cost-chart-panel') ? ' wide' : ''}`;
    const header = document.createElement('header');
    const heading = document.createElement('div');
    const label = document.createElement('strong'); label.textContent = title;
    const sourceLabel = document.createElement('small');
    sourceLabel.textContent = ({dashboard:'Дашборд',analytics:'Воронка и поведение',costs:'Расходы'})[view] || 'Аналитика';
    heading.append(label,sourceLabel);
    const actions = document.createElement('div');
    const open = document.createElement('button'); open.type = 'button'; open.className = 'favorite-open'; open.dataset.favoriteOpen = view; open.dataset.favoriteTarget = id; open.textContent = 'Открыть раздел';
    actions.append(open,favoriteButton(id,title)); header.append(heading,actions);
    const snapshot = source.cloneNode(true);
    snapshot.classList.remove('favorite-source');
    snapshot.classList.add('favorite-snapshot');
    snapshot.removeAttribute('data-favorite-id');
    snapshot.removeAttribute('data-favorite-title');
    snapshot.removeAttribute('data-favorite-view');
    snapshot.querySelectorAll('.favorite-toggle').forEach(node => node.remove());
    snapshot.querySelectorAll('[id]').forEach(node => node.removeAttribute('id'));
    snapshot.querySelectorAll('button,input,select,textarea').forEach(node => { node.disabled = true; });
    card.append(header,snapshot); root.append(card);
  }
}

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
    card.dataset.favoriteId = `dashboard-summary-${key}`;
    card.dataset.favoriteTitle = label;
    card.dataset.favoriteView = 'dashboard';
    const name = document.createElement('span');
    const value = document.createElement('strong');
    const helper = document.createElement('small');
    name.textContent = label;
    value.textContent = Number(summary[key] || 0).toLocaleString('ru-RU');
    helper.textContent = note;
    card.append(name,value,helper);
    root.append(card);
  }
  decorateFavoriteSources();
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
  if (!items.length) emptyTable(root,9);
  for (const item of items) {
    const row = document.createElement('tr');
    textCell(row,item.chel_id,item.chel_id);
    textCell(row,item.from_manager || '—');
    statusCell(row,item.entry_flow === 'result' ? 'За результатами' : 'Обычный путь',item.entry_flow === 'result' ? ['complete'] : []);
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
  $('#generatedAt').textContent = `Данные обновлены ${formatDate(data.generated_at)} · следующее обновление только по кнопке «Обновить»`;
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
  for (const [index,[label,value,note]] of cards.entries()) {
    const card = document.createElement('article');
    card.className = 'summary-card';
    card.dataset.favoriteId = `cost-summary-${index}`;
    card.dataset.favoriteTitle = label;
    card.dataset.favoriteView = 'costs';
    const name = document.createElement('span');
    const amount = document.createElement('strong');
    const helper = document.createElement('small');
    name.textContent = label;
    amount.textContent = value;
    helper.textContent = note;
    card.append(name,amount,helper);
    root.append(card);
  }
  decorateFavoriteSources();
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
  return withPanelLoading('#costsAdminView', async () => {
    const period = $('#costsPeriod').value;
    const data = await adminFetch(`/api/admin/ai-costs?period=${encodeURIComponent(period)}&limit=100`);
    renderCostSummary(data);
    renderCostChart(data.daily || []);
    renderCostOperations(data.by_operation || []);
    renderCostModels(data.by_model || []);
    renderCostRecent(data.recent || []);
    renderPricing(data.pricing || []);
    decorateFavoriteSources();
    if (activeAdminView === 'favorites') renderFavorites();
    $('#costsNotice').textContent = `${data.notice} Обновлено ${formatDate(data.generated_at)}.`;
  }, 'Считаем расходы…');
}

const wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

function loadingTarget(target) {
  return typeof target === 'string' ? $(target) : target;
}

function setPanelLoading(target, loading, label = 'Загружаем данные…') {
  const node = loadingTarget(target);
  if (!node) return;
  const current = panelLoadingCounts.get(node) || 0;
  const next = Math.max(0, current + (loading ? 1 : -1));
  panelLoadingCounts.set(node, next);
  if (loading && !node.querySelector(':scope > .admin-panel-loader')) {
    const loader = document.createElement('div');
    loader.className = 'admin-panel-loader';
    loader.setAttribute('role', 'status');
    loader.setAttribute('aria-live', 'polite');
    loader.innerHTML = `<span class="admin-loading-spinner" aria-hidden="true"></span><strong>${escapeHtml(label)}</strong><small>Пожалуйста, подождите</small>`;
    node.append(loader);
  }
  node.classList.toggle('admin-loading', next > 0);
  node.setAttribute('aria-busy', String(next > 0));
  if (!next) node.querySelector(':scope > .admin-panel-loader')?.remove();
}

async function withPanelLoading(target, operation, label = 'Загружаем данные…') {
  setPanelLoading(target, true, label);
  try { return await operation(); }
  finally { setPanelLoading(target, false, label); }
}

function setTopProgress(loading, label = 'Обновляем данные…') {
  topProgressRequests = Math.max(0, topProgressRequests + (loading ? 1 : -1));
  const progress = $('#adminTopProgress');
  if (!progress) return;
  if (loading && label) $('#adminTopProgressLabel').textContent = label;
  const active = topProgressRequests > 0;
  progress.classList.toggle('hidden', !active);
  progress.setAttribute('aria-busy', String(active));
}

async function withTopProgress(operation, label = 'Обновляем данные…') {
  setTopProgress(true, label);
  try { return await operation; }
  finally { setTopProgress(false); }
}

function adminFetch(path, token = sessionStorage.getItem(TOKEN_KEY), options = {}, retryAttempt = 0) {
  const method = String(options.method || 'GET').toUpperCase();
  if (method !== 'GET' || retryAttempt > 0) {
    return withTopProgress(adminFetchRequest(path, token, options, retryAttempt));
  }
  const requestKey = `${token || ''}:${path}`;
  if (adminGetRequests.has(requestKey)) return adminGetRequests.get(requestKey);
  const request = withTopProgress(adminFetchRequest(path, token, options, retryAttempt))
    .finally(() => adminGetRequests.delete(requestKey));
  adminGetRequests.set(requestKey, request);
  return request;
}

async function adminFetchRequest(path, token, options = {}, retryAttempt = 0) {
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
    return adminFetchRequest(path, token, options, retryAttempt + 1);
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
  const panel = $('#staffList')?.closest('.panel');
  return withPanelLoading(panel, async () => {
    renderStaff(await adminFetch('/api/admin/managers'));
  }, 'Загружаем менеджеров…');
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

function adminExaminationPrices(item) {
  const parts = [];
  if (Number(item.competitor_price || 0) > 0) {
    parts.push(`У конкурентов: ${Number(item.competitor_price).toLocaleString('ru-RU')} ₽`);
  }
  if (Number(item.price_without_discount || 0) > 0) {
    parts.push(`Без скидки: ${Number(item.price_without_discount).toLocaleString('ru-RU')} ₽`);
  }
  parts.push(`С учётом скидки (фактическая): ${Number(item.price || 0).toLocaleString('ru-RU')} ₽`);
  return parts.map(part => `<span>${escapeHtml(part)}</span>`).join('');
}

function examinationPriceSettingEnabled(item, key) {
  return item[key] !== 0 && item[key] !== false;
}

function adminExaminationLabels(item) {
  const settings = [
    [item.competitor_label || 'У конкурентов', examinationPriceSettingEnabled(item, 'show_competitor_price')],
    [item.retail_price_label || 'Розничная цена', examinationPriceSettingEnabled(item, 'show_retail_price')],
    [item.discount_price_label || 'С учётом вашей скидки', examinationPriceSettingEnabled(item, 'show_discount_price')],
  ];
  return settings.map(([label, visible]) => `${label}${visible ? '' : ' (скрыто)'}`).join(' · ');
}

function renderExaminations(items) {
  examinationItems = items;
  $('#examinationsCount').textContent = `${items.length} позиций`;
  $('#examinationList').innerHTML = items.length ? items.map(item => `
    <article class="examination-admin-card" data-examination-id="${escapeHtml(item.id)}">
      <div class="examination-card-heading">
        <strong>${escapeHtml(item.name)}</strong>
        <div class="examination-admin-prices">${adminExaminationPrices(item)}</div>
      </div>
      <p>${escapeHtml(item.description)}</p>
      <small><b>Состав:</b> ${escapeHtml(item.includes || 'Не указан')}</small>
      <small><b>Подписи цен:</b> ${escapeHtml(adminExaminationLabels(item))}</small>
      <div class="examination-card-actions">
        <button type="button" class="edit-examination" data-examination-action="edit">Изменить</button>
        <button type="button" class="delete-examination" data-examination-action="delete">Удалить</button>
      </div>
    </article>`).join('') : '<p class="form-error">В каталоге пока нет обследований</p>';
}

async function loadExaminations() {
  const panel = $('#examinationList')?.closest('.panel');
  return withPanelLoading(panel, async () => {
    renderExaminations(await adminFetch('/api/admin/examinations'));
  }, 'Загружаем обследования…');
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
    selector.toLowerCase().includes('device') ? (deviceNames[value] || value)
      : selector.toLowerCase().includes('method') ? (registrationNames[value] || value)
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
    const managerName = item.manager_name || item.from_manager || '—';
    const managerLabel = item.from_manager && item.from_manager !== managerName
      ? `${managerName} (${item.from_manager})`
      : managerName;
    textCell(row,managerLabel);
    textCell(row,Number(item.users || 0).toLocaleString('ru-RU'));
    textCell(row,`${Number(item.percent_of_all || 0).toLocaleString('ru-RU')}%`);
    textCell(row,Number(item.users_with_examinations || 0).toLocaleString('ru-RU'));
    textCell(row,`${Number(item.examination_conversion || 0).toLocaleString('ru-RU')}%`);
    root.append(row);
  }
}

function formatRublesFromKopecks(value) {
  return `${(Number(value || 0) / 100).toLocaleString('ru-RU', {minimumFractionDigits:0, maximumFractionDigits:2})} ₽`;
}

function renderPaymentAnalytics(payments = {}) {
  const summary = payments.summary || {};
  const root = $('#paymentAnalyticsSummary');
  document.querySelectorAll('.payment-test-note').forEach(node => node.remove());
  root.innerHTML = [
    ['Попытки онлайн',summary.attempts || 0,'все созданные заказы'],
    ['Успешные оплаты',summary.succeeded || 0,`${Number(summary.conversion || 0).toLocaleString('ru-RU')}% пользователей оплатили`],
    ['Выручка',formatRublesFromKopecks(summary.revenue_kopecks),'без тестовых платежей'],
    ['Ожидают',summary.pending || 0,'создаются или обрабатываются'],
    ['Неуспешные',summary.unsuccessful || 0,'отменены, прерваны или с ошибкой'],
    ['На медосмотре',summary.at_exam_users || 0,'выбрали оплату при прохождении'],
  ].map(([label,value,note]) => `<article><span>${escapeHtml(String(label))}</span><strong>${typeof value === 'number' ? value.toLocaleString('ru-RU') : escapeHtml(String(value))}</strong><small>${escapeHtml(String(note))}</small></article>`).join('');
  if (Number(summary.test_attempts || 0)) {
    root.insertAdjacentHTML('afterend', `<p class="payment-test-note">Тестовых попыток за период: <b>${Number(summary.test_attempts).toLocaleString('ru-RU')}</b>. Они показаны в статусах, но исключены из выручки.</p>`);
  }
  renderDistribution('#paymentStatusDistribution',payments.statuses || [],'label','orders');
  renderDistribution('#paymentItemsDistribution',payments.items || [],'label','purchases');
  const recent = $('#paymentRecentTable');
  recent.replaceChildren();
  for (const item of payments.recent || []) {
    const row = document.createElement('tr');
    textCell(row,formatDate(item.created_at));
    textCell(row,`${String(item.id || '').slice(-8).toUpperCase()}${item.test ? ' · тест' : ''}`);
    textCell(row,item.chel_id || '—',item.chel_id || '');
    statusCell(row,item.status);
    textCell(row,formatRublesFromKopecks(item.amount_kopecks));
    recent.append(row);
  }
  if (!(payments.recent || []).length) {
    const row = document.createElement('tr');
    const cell = document.createElement('td'); cell.colSpan = 5; cell.textContent = 'За выбранный период платежей нет';
    row.append(cell); recent.append(row);
  }
}

function renderAnalytics(data) {
  latestAnalyticsData = data;
  const summary = $('#analyticsSummary');
  summary.replaceChildren();
  for (const [index,[label,value,note]] of [
    ['Пользователи',data.summary.users,'завершили выбор регистрации'],
    ['Посетители',data.summary.visitors,'включая экран входа'],
    ['Сессии',data.summary.sessions,'визиты в сервис'],
    ['События',data.summary.events,'технические действия'],
  ].entries()) {
    const card = document.createElement('article'); card.className = 'analytics-metric';
    card.dataset.favoriteId = `analytics-summary-${index}`;
    card.dataset.favoriteTitle = label;
    card.dataset.favoriteView = 'analytics';
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
  renderPaymentAnalytics(data.payments || {});

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
  decorateFavoriteSources();
  if (activeAdminView === 'favorites') renderFavorites();
}

async function loadAnalytics() {
  return withPanelLoading('#analyticsAdminView', async () => {
    const params = new URLSearchParams({period:$('#analyticsPeriod').value,recent_page:String(analyticsRecentPage),recent_limit:'25'});
    for (const [key,selector] of [['device','#analyticsDevice'],['method','#analyticsMethod'],['source','#analyticsSource']]) {
      if ($(selector).value) params.set(key,$(selector).value);
    }
    appendDateRange(params, '#analyticsDateFrom', '#analyticsDateTo');
    renderAnalytics(await adminFetch(`/api/admin/analytics?${params}`));
  }, 'Считаем воронку и поведение…');
}

function appendDateRange(params, fromSelector, toSelector) {
  const dateFrom = $(fromSelector).value;
  const dateTo = $(toSelector).value;
  if (dateFrom && dateTo && dateFrom > dateTo) {
    throw new Error('Дата начала периода не может быть позже даты окончания');
  }
  if (dateFrom) params.set('date_from', dateFrom);
  if (dateTo) params.set('date_to', dateTo);
}

const metric2QuestionContent = [
  {key:'company_inn',title:'Сообщите ИНН вашего предприятия',lead:'Если вы не владеете этой информацией, пожалуйста, уточните её у вашего работодателя.',placeholder:'10 или 12 цифр'},
  {key:'preferred_name',title:'Как к вам обращаться?',lead:'Имя необязательно, но с ним общение будет естественнее.',placeholder:'Например, Алексей',optional:true},
  {key:'age',title:'Сколько вам полных лет?',lead:'Возраст помогает специалистам точнее учитывать риски и нормы.',placeholder:'От 18 до 99'},
  {key:'sex',title:'Укажите пол для медицинского контекста',lead:'Это важно для интерпретации части симптомов и обследований.',choices:['Женский','Мужской']},
  {key:'height_cm',title:'Какой у вас рост?',lead:'Введите значение в сантиметрах.',placeholder:'От 50 до 250'},
  {key:'weight_kg',title:'Какой у вас вес?',lead:'Введите актуальный вес в килограммах.',placeholder:'От 40 до 250'},
  {key:'smoking',title:'Вы курите?',lead:'Учитываются сигареты, электронные сигареты и другие способы употребления никотина.',choices:['Не курю','Курил(а) раньше','Курю сейчас']},
  {key:'alcohol',title:'Как часто вы употребляете алкоголь?',lead:'Выберите наиболее близкий вариант.',choices:['Не употребляю','Редко / по праздникам','Примерно раз в неделю','Чаще раза в неделю']},
  {key:'activity',title:'Какой у вас уровень активности?',lead:'Ориентируйтесь на обычный день: низкий — до 5 000 шагов в день, средний — 5–10 тысяч шагов в день, высокий — более 10 тысяч шагов в день или регулярный спорт.',choices:['Низкий','Средний','Высокий']},
  {key:'blood_pressure',title:'Как вы оцениваете своё давление?',lead:'Если не измеряли или не уверены, выберите «Не знаю».',choices:['Обычно в норме','Бывает повышенным','Бывает пониженным','Сильно меняется','Не знаю']},
  {key:'dark_in_eyes',title:'Темнеет ли в глазах при резком подъёме?',lead:'Например, когда быстро встаёте с кровати или стула.',choices:['Нет','Да','Не уверен(а)']},
  {key:'blood_sugar',title:'Знаете ли вы уровень сахара в крови?',lead:'Это не оценка диагноза — только уже известная вам информация.',choices:['Был в норме','Бывал повышен','Не измерял(а) / не знаю']},
  {key:'joint_pain',title:'Бывают боли или отёчность суставов?',lead:'В том числе при нагрузке или смене погоды.',choices:['Нет','Да','Не уверен(а)']},
  {key:'fatigue',title:'Беспокоит длительная усталость?',lead:'Имеется в виду усталость, которая сохраняется после обычного отдыха.',choices:['Нет','Да','Не уверен(а)']},
  {key:'conditions',title:'Есть хронические заболевания?',lead:'Напишите по одному на строку. Если нет — этот шаг можно пропустить.',placeholder:'Например:\nГипертония\nАстма',optional:true,textarea:true},
  {key:'medications',title:'Какие лекарства принимаете постоянно?',lead:'Название и дозировка, если известна. Шаг можно пропустить.',placeholder:'По одному препарату на строку',optional:true,textarea:true},
  {key:'allergies',title:'Есть аллергии?',lead:'Укажите лекарства, продукты или другие известные аллергены. Шаг можно пропустить.',placeholder:'По одному аллергену на строку',optional:true,textarea:true},
  {key:'notes',title:'Есть ли у вас жалобы?',lead:'Введите в одном сообщении всё, что вас тревожит: проблему, симптомы и что вы принимаете в связи с ними. Если жалоб нет, этот вопрос можно пропустить.',placeholder:'Например: две недели болит голова по вечерам, принимаю ибупрофен',optional:true,textarea:true},
];

const metric2ExamAudiences = {
  fatigue_basic:'Тем, кого беспокоят слабость, сонливость или снижение работоспособности.',fatigue_extended:'Тем, у кого усталость сохраняется длительно или сочетается с другими жалобами.',weight_basic:'Тем, кто хочет разобраться в возможных обменных причинах набора веса.',weight_extended:'Тем, кому нужна более широкая оценка гормональных и обменных факторов веса.',hair_loss:'При заметном выпадении волос, ломкости и подозрении на дефициты.',lipids:'Для оценки сердечно-сосудистого риска, особенно при повышенном давлении или лишнем весе.',liver_basic:'Для базовой проверки показателей печени и поджелудочной железы.',liver_extended:'При необходимости более широкой оценки печени, поджелудочной и желчевыводящих путей.',iron:'При утомляемости, слабости, бледности или подозрении на дефицит железа.',kidneys:'Для базовой оценки функции почек и азотистого обмена.',protein:'Для оценки белкового обмена, питания и синтетической функции печени.',joints:'При боли, скованности или отёчности суставов.',inflammation:'Когда важно дополнительно оценить наличие воспалительной реакции.',thyroid:'При изменениях веса, утомляемости, сердцебиении или других возможных признаках нарушения функции щитовидной железы.',female_hormones:'Женщинам при наличии показаний к оценке гормонального фона; сроки сдачи важно обсудить с врачом.',male_health:'Мужчинам для оценки гормонального фона и показателей предстательной железы с учётом возраста и показаний.',cortisol:'При длительном стрессе и связанных с ним жалобах; показатель зависит от времени сдачи.',vitamin_d:'Тем, кому важно узнать уровень витамина D и обсудить необходимость коррекции.',ca125:'Только при наличии врачебных показаний; онкомаркер не подходит для самостоятельной диагностики.',ca153:'Только при наличии врачебных показаний; онкомаркер не подходит для самостоятельной диагностики.',ca199:'Только при наличии врачебных показаний; онкомаркер не подходит для самостоятельной диагностики.',
};

function metric2PreviewMarkup(screen, large = false) {
  const kind = screen.kind || '';
  const action = (label,secondary = false) => `<span class="metric2-mock-button${secondary ? ' secondary' : ''}">${escapeHtml(label)}</span>`;
  const messengerAction = (icon,label) => `<span class="metric2-mock-button metric2-mock-messenger"><i>${escapeHtml(icon)}</i><span><b>${escapeHtml(label)}</b><small>Подтверждение через бота</small></span></span>`;
  const examinations = latestMetric2Data?.examinations || [];
  let content = '';
  if (kind === 'welcome') content = `<div class="metric2-mock-brand"><span>К</span><div><strong>Консилиум</strong><small>Забота о здоровье начинается здесь</small></div></div><small>● ПЛАНОВЫЙ МЕДОСМОТР</small><b>Вам предстоит плановый медицинский осмотр</b><p>Анкетирование — обязательный этап медосмотра. Оно займёт не более 10 минут и поможет точнее оценить ваше состояние.</p><p>После этого вы сможете выбрать дополнительные обследования — они помогают обнаружить то, что обычно остаётся незамеченным.</p><span class="metric2-mock-pulse">⌁</span><span class="metric2-mock-welcome-highlight">☆ &nbsp; Все, кто пройдёт анкету до конца, получат <b>бесплатный доступ к новому сервису</b> — медицинскому ИИ-помощнику.</span><em class="metric2-mock-welcome-closing">Пройдите осмотр осознанно, с полной картиной своего здоровья — и без лишних переживаний.</em>${action('Начать анкету →')}<span class="metric2-mock-time">Анкета займёт около 10 минут</span>`;
  else if (kind === 'registration') content = `<div class="metric2-mock-brand"><span>К</span><div><strong>Консилиум</strong><small>Ваше личное пространство здоровья</small></div></div><small>БЕЗ ПАРОЛЯ</small><b>Войдите через удобный мессенджер</b><p>Так анкета, история диалогов и результаты останутся доступны на другом устройстве и после очистки браузера.</p>${messengerAction('➤','Продолжить с Telegram')}${messengerAction('М','Продолжить с MAX')}<span class="metric2-mock-link">Войти анонимно</span><p class="metric2-mock-note">Консилиум не получает пароль от мессенджера. Сохраняется только его технический ID для восстановления доступа.</p>`;
  else if (kind === 'warning') content = `<div class="metric2-mock-modal"><span class="metric2-mock-close">×</span><span class="metric2-mock-icon">!</span><b>Продолжить без мессенджера?</b><p>Данные будут связаны только с этим браузером.</p><ul><li>после очистки cookies доступ может потеряться;</li><li>на другом телефоне или компьютере история не откроется;</li><li>восстановить анонимный профиль служба поддержки не сможет.</li></ul><p class="metric2-mock-note">Мессенджер можно будет привязать позже без повторного заполнения анкеты.</p><div class="metric2-mock-actions">${action('Назад',true)}${action('Понимаю, продолжить')}</div></div>`;
  else if (kind === 'appearance') content = `<small>ПЕРЕД НАЧАЛОМ</small><b>Какой размер текста вам удобен?</b><p>Вы увидите изменение сразу. Позже размер можно поменять через меню функций.</p><span class="metric2-mock-choice"><b>Аа &nbsp; Обычный</b><small>Чуть крупнее базового интерфейса</small></span><span class="metric2-mock-choice"><b>Аа &nbsp; Крупный</b><small>Комфортно для большинства экранов</small></span><span class="metric2-mock-choice selected"><b>Аа &nbsp; Очень крупный</b><small>Максимальная читаемость</small></span>${action('Продолжить')}`;
  else if (kind.startsWith('question_')) {
    const index = metric2QuestionContent.findIndex(item => item.key === screen.question_key);
    const question = metric2QuestionContent[index] || {title:screen.title,lead:'',placeholder:''};
    const control = question.choices
      ? question.choices.map(label => `<span class="metric2-mock-choice">${escapeHtml(label)}</span>`).join('')
      : `<span class="metric2-mock-input${question.textarea ? ' textarea' : ''}">${escapeHtml(question.placeholder || '')}</span>`;
    const nextLabel = question.optional ? 'Пропустить' : 'Продолжить';
    const controls = index > 0 ? `<div class="metric2-mock-actions">${action('Назад',true)}${action(nextLabel)}</div>` : action(nextLabel);
    const notMedical = question.key === 'company_inn' ? action('Я не на мед-осмотр',true) : '';
    content = `<small>ШАГ ${index + 1} ИЗ ${metric2QuestionContent.length}</small><b>${escapeHtml(question.title)}</b><p>${escapeHtml(question.lead)}</p>${control}${controls}${notMedical}`;
  } else if (kind === 'exam_offer') content = `<small>ПОСЛЕ АНКЕТЫ</small><b>Дополнительные обследования</b><blockquote><strong>Давайте честно: здоровых людей не бывает.</strong><br>У каждого есть своё слабое место, и лучше бы его знать.<br>Пара быстрых обследований — и жить спокойнее.</blockquote><p>Чтобы получить более полную информацию о состоянии своего здоровья, вы можете пройти дополнительные обследования во время медосмотра.</p><p class="metric2-mock-note"><b>Можно пригласить родственника или друга</b> пройти один или несколько чек-апов. Позаботьтесь о близких — отправьте им ссылку на сервис.</p><span class="metric2-mock-info"><b>◫ Посмотреть описания чек-апов</b><small>Что входит, кому и для чего они нужны →</small></span><span class="metric2-mock-question"><b>Хотели бы вы сдать дополнительные анализы во время медосмотра на работе?</b><small>Выберите соответствующий вариант.</small></span>${action('Да, выбрать анализы')}${action('Нет, не сейчас',true)}<span class="metric2-mock-link">← Изменить ответы анкеты</span>`;
  else if (kind === 'exam_catalog') {
    const cards = examinations.map(test => `<span class="metric2-mock-catalog-card"><header><strong>${escapeHtml(test.name)}</strong><em>${Number(test.price || 0).toLocaleString('ru-RU')} ₽</em></header><small>КОМУ ПОДХОДИТ</small><p>${escapeHtml(metric2ExamAudiences[test.id] || test.description || 'Тем, кто хочет получить больше информации о состоянии здоровья.')}</p><small>ДЛЯ ЧЕГО</small><p>${escapeHtml(test.description || 'Для дополнительной оценки показателей здоровья.')}</p><small>ЧТО ВХОДИТ</small><p>${escapeHtml(test.includes || 'Состав уточняется')}</p></span>`).join('');
    content = `<small>ДОСТУПНЫЕ ЧЕК-АПЫ</small><b>Что можно проверить</b><p>Краткое описание поможет сориентироваться. Необходимость обследований и интерпретацию результатов лучше обсуждать с врачом.</p>${cards}${action('Выбрать анализы')}${action('Вернуться к вопросу',true)}`;
  } else if (kind === 'exam_objection') content = `<small>ПЕРЕД ТЕМ КАК ПРОДОЛЖИТЬ</small><b>После обследований вы получите больше, чем результаты</b><p>Врач высшей категории <strong>Татьяна Витальевна</strong> подготовит подробную расшифровку сложных показателей.</p><p>И самое главное — вы получите <strong>бесплатную консультацию</strong> по результатам.</p><p>Всё будет доступно в этом сервисе — без очередей и доплат за расшифровку.</p><span class="metric2-mock-benefit"><b>✓ Ничего дополнительно делать не нужно</b><small>Выберите обследования сейчас, а в день медосмотра сдайте всё вместе.</small></span><span class="metric2-mock-benefit"><b>✓ Один визит вместо отдельной поездки</b><small>Вы уже будете на осмотре — дополнительные анализы можно сдать за один раз.</small></span><span class="metric2-mock-benefit"><b>✓ Бесплатная консультация специалиста</b><small>После готовности дополнительных анализов врач высшей категории поможет разобраться в результатах.</small></span><span class="metric2-mock-benefit"><b>✓ Не придётся записываться отдельно</b><small>Если отложить обследования, позже могут потребоваться отдельная запись и поездка.</small></span><p class="metric2-mock-note">Дополнительные обследования добровольны — окончательное решение остаётся за вами.</p>${action('Выбрать обследования')}${action('Всё равно отказаться',true)}`;
  else if (kind === 'exam_selection') {
    const cards = examinations.map((test,index) => `<span class="metric2-mock-test${index === 0 ? ' selected' : ''}"><strong>${index === 0 ? '✓ ' : ''}${escapeHtml(test.name)}</strong><em>${Number(test.price || 0).toLocaleString('ru-RU')} ₽</em><small>${escapeHtml(test.description || '')}</small><small>${escapeHtml(test.includes || '')}</small></span>`).join('');
    const selected = examinations[0];
    content = `<small>ВЫБОР АНАЛИЗОВ</small><b>Выберите интересующие наборы</b><p>Рекомендации отмечены по ответам анкеты и не являются назначением.</p>${cards}<span class="metric2-mock-total">Выбрано: ${selected ? 1 : 0}<b>${Number(selected?.price || 0).toLocaleString('ru-RU')} ₽</b></span><div class="metric2-mock-actions">${action('Назад',true)}${action('Далее')}</div>${action('Ничего не выбирать',true)}`;
  } else if (kind === 'payment') {
    const selected = examinations[0];
    content = `<small>ПОСЛЕДНИЙ ШАГ</small><b>Проверим заказ</b><p>Выберите, как вам будет удобнее оплатить дополнительные обследования.</p><span class="metric2-mock-test"><strong>${escapeHtml(selected?.name || 'Выбранное обследование')}</strong><em>${Number(selected?.price || 0).toLocaleString('ru-RU')} ₽</em></span><span class="metric2-mock-total">Итого <b>${Number(selected?.price || 0).toLocaleString('ru-RU')} ₽</b></span>${action('Оплатить онлайн')}${action('Оплатить на медосмотре')}${action('← Вернуться к обследованиям',true)}`;
  } else if (kind === 'payment_processing') content = `<div class="metric2-mock-modal"><span class="metric2-mock-icon">⌛</span><b>Проверяем оплату</b><p>Обычно это занимает несколько секунд. Не закрывайте страницу.</p></div>`;
  else if (kind === 'payment_success') content = `<span class="metric2-mock-icon">✓</span><small>ОПЛАТА ПОДТВЕРЖДЕНА</small><b>Всё получилось!</b><p>ЮKassa подтвердила оплату. Выбранные обследования сохранены.</p><span class="metric2-mock-info"><b>Где потом найти оплату</b><small>Откройте чат → нажмите меню ☰ справа вверху → выберите «Мои покупки». Там будут сумма, дата, состав заказа и статус «Оплачено».</small></span><p class="metric2-mock-note">Успешная покупка хранится в истории и не удаляется. Электронный чек придёт на указанную при оплате почту.</p>${action('Открыть мои покупки')}${action('Перейти в чат',true)}`;
  else if (kind === 'payment_result') content = `<div class="metric2-mock-modal"><span class="metric2-mock-icon">!</span><b>Оплата не завершена</b><p>Попытка сохранена в разделе «Мои покупки». Можно проверить статус или повторить оплату.</p>${action('Вернуться к оплате')}${action('Мои покупки',true)}</div>`;
  else if (kind === 'payment_unavailable') content = `<div class="metric2-mock-modal"><span class="metric2-mock-icon">⌛</span><b>Онлайн-оплата временно недоступна</b><p>Мы уже работаем над её подключением. Пока вы можете выбрать оплату на медицинском осмотре.</p>${action('Понятно')}</div>`;
  else if (kind === 'completion') content = `<span class="metric2-mock-emoji">🎉</span><small>ОБСЛЕДОВАНИЯ ВЫБРАНЫ</small><b>Отлично! Вы выбрали дополнительные обследования.</b><p>В день медицинского осмотра наша бригада сообщит вам <strong>индивидуальный номер пробирки</strong>.</p><p>Чтобы получить результаты анализов, достаточно будет ввести этот номер в соответствующее поле нашего сервиса.</p><p class="metric2-mock-note">№ Сейчас у вас этого номера еще нет — это нормально. Когда он появится, просто напишите в чат нашему менеджеру. Он подскажет, куда ввести этот номер и как получить результаты.</p><p>После этого сообщения для вас откроется чат, в котором вы сможете:</p><ul><li>узнать, как получить результаты анализов;</li><li>задать любые вопросы о медицинском осмотре;</li><li>получить консультацию по анализам, питанию и вопросам здоровья.</li></ul><p>Как только результаты будут готовы, вы сможете <strong>бесплатно получить их расшифровку</strong> у нашего специалиста.</p><p class="metric2-mock-note"><strong>📱 Также рекомендуем установить наше приложение на смартфон.</strong> Так вы не потеряете доступ к своим результатам, сможете в любой момент обратиться к онлайн-врачу и всегда будете иметь все необходимые медицинские сервисы под рукой.</p><p>💬 Не стесняйтесь писать в чат — мы всегда рады помочь!</p><span class="metric2-mock-info"><b>↗ Сохраните результаты и расшифровки</b><small>Привяжите Telegram или MAX, чтобы вернуться к анкете, выбранным обследованиям и готовым результатам с другого устройства.</small></span>${action('Привязать мессенджер')}${action('＋ Установить приложение')}${action('Установлю позже',true)}`;
  else if (kind === 'completion_skipped') content = `<span class="metric2-mock-icon">✓</span><small>АНКЕТА ЗАВЕРШЕНА</small><b>Спасибо! Ваши ответы сохранены.</b><p>Вы решили пока не выбирать дополнительные обследования. Если захотите, к ним можно будет вернуться позже через меню сервиса.</p><p><strong>В Консилиуме вы сможете:</strong></p><ul><li>задавать медицинскому помощнику вопросы о здоровье, питании и медицинском осмотре;</li><li>получать результаты анализов по номеру пробирки и просить помочь с расшифровкой;</li><li>сохранять историю обращений и важные сведения о здоровье;</li><li>при необходимости пригласить медицинского специалиста в чат.</li></ul><span class="metric2-mock-info"><b>↗ Не потеряйте доступ</b><small>Привяжите Telegram или MAX, чтобы открыть анкету, историю диалогов и результаты с другого устройства или после очистки браузера.</small></span>${action('Привязать мессенджер')}<p class="metric2-mock-note"><strong>📱 Установите приложение на устройство.</strong> Так Консилиум будет всегда под рукой, а вернуться к вопросам о здоровье станет проще.</p>${action('＋ Установить приложение')}${action('Перейти в Консилиум',true)}`;
  else if (kind === 'result_existing') content = `<span class="metric2-mock-icon">▤</span><small>РЕЗУЛЬТАТЫ АНАЛИЗОВ</small><b>Введите номер пробирки</b><p>Чат уже открыт, а поверх него показано окно получения результатов. Если номер сохранён, поиск начинается автоматически.</p><span class="metric2-mock-input">Например, LAB-2026-0042</span>${action('Сохранить и продолжить')}`;
  else if (kind === 'result_welcome') content = `<span class="metric2-mock-icon">▤</span><small>РЕЗУЛЬТАТЫ ОБСЛЕДОВАНИЙ</small><b>Ваши анализы — в одном месте</b><p>Здесь можно получить документы по номеру пробирки, попросить помочь с расшифровкой и задать вопрос о результатах медицинскому помощнику.</p><span class="metric2-mock-info"><b>Что понадобится</b><small>Индивидуальный номер пробирки, который сообщили во время медицинского осмотра.</small></span>${action('Далее')}`;
  else if (kind === 'result_tube') content = `<span class="metric2-mock-icon">№</span><small>ПОИСК РЕЗУЛЬТАТОВ</small><b>Введите номер пробирки</b><p>Он указан на вашей наклейке или был сообщён бригадой на медицинском осмотре.</p><span class="metric2-mock-input">Например, 123456</span>${action('Продолжить')}`;
  else if (kind === 'result_messenger') content = `<span class="metric2-mock-icon">↗</span><small>РЕКОМЕНДУЕМ</small><b>Не потеряйте результаты</b><p>Привяжите Telegram или MAX: так профиль не потеряется после очистки браузера, а результаты, расшифровка и консультация останутся доступны на любом устройстве.</p><ul><li>доступ к документам с телефона и компьютера;</li><li>история расшифровок и консультаций;</li><li>уведомление о готовности результатов.</li></ul>${action('Привязать мессенджер')}${action('Продолжить без привязки',true)}`;
  else if (kind === 'result_search') content = `<span class="metric2-mock-icon">⌕</span><small>ПРОВЕРЯЕМ НОМЕР</small><b>Ищем ваши результаты</b><p>Обычно это занимает несколько секунд.</p>`;
  else if (kind === 'result_found') content = `<span class="metric2-mock-icon">✓</span><small>ГОТОВО</small><b>Результаты найдены</b><p>Документы уже доступны. В чате можно попросить Ольгу помочь с расшифровкой или пригласить медицинского специалиста.</p><span class="metric2-mock-info"><b>▤ Результаты анализов</b><small>Открыть документ →</small></span>${action('Перейти в чат и получить консультацию')}`;
  else if (kind === 'result_not_found') content = `<span class="metric2-mock-icon">…</span><small>ПОКА НЕ ГОТОВЫ</small><b>Результаты ещё не найдены</b><p>По этому номеру документы пока не появились. Проверьте номер или запросите уведомление о готовности.</p><span class="metric2-mock-info"><b>Сообщим о готовности</b><small>Для уведомления нужен привязанный Telegram или MAX.</small></span>${action('Получить уведомление')}${action('Проверить ещё раз',true)}${action('Перейти в чат',true)}`;
  else if (kind === 'result_notification') content = `<span class="metric2-mock-icon">✓</span><small>ЗАПРОС СОХРАНЁН</small><b>Сообщим, когда результаты появятся</b><p>Уведомление будет связано с номером пробирки и вашим профилем.</p>${action('Перейти в чат')}`;
  else content = `<small>${escapeHtml(screen.stage || '')}</small><b>${escapeHtml(screen.title)}</b><p>${escapeHtml(screen.description || '')}</p>`;

  const standalone = ['welcome','registration','warning'].includes(kind);
  const questionIndex = metric2QuestionContent.findIndex(item => item.key === screen.question_key);
  const stage = kind === 'appearance' ? 'Настройка'
    : kind.startsWith('question_') ? 'Анкета'
    : kind === 'exam_catalog' ? 'Описание чек-апов'
    : ['exam_offer','exam_objection','exam_selection'].includes(kind) ? 'Обследования'
    : ['payment','payment_processing','payment_success','payment_result','payment_unavailable'].includes(kind) ? 'Оплата'
    : ['completion','completion_skipped'].includes(kind) ? 'Готово'
    : kind.startsWith('result_') ? (screen.stage || 'Результаты').replace('Результаты · ', '')
    : (screen.stage || '').split(' · ')[0] || 'Анкета';
  const progress = kind === 'appearance' ? 2
    : kind.startsWith('question_') ? 5 + Math.round((Math.max(0,questionIndex) / metric2QuestionContent.length) * 60)
    : kind === 'exam_offer' ? 72
    : ['exam_catalog','exam_objection'].includes(kind) ? 76
    : kind === 'exam_selection' ? 80
    : kind === 'payment' ? 92
    : kind === 'payment_processing' ? 96
    : kind === 'payment_result' ? 96
    : kind === 'payment_success' ? 100
    : kind === 'payment_unavailable' ? 92
    : ['completion','completion_skipped'].includes(kind) ? 100
    : kind === 'result_welcome' ? 12 : kind === 'result_tube' ? 32
    : kind === 'result_messenger' ? 54 : kind === 'result_search' ? 72
    : kind === 'result_not_found' ? 88 : kind.startsWith('result_') ? 100 : 0;
  const appHeader = standalone ? '' : `<div class="metric2-phone-app"><span class="metric2-phone-brand"><b>К</b><span><strong>Консилиум</strong><small>Персональный старт</small></span></span><em>${escapeHtml(stage)}</em></div><div class="metric2-phone-progress"><i style="width:${progress}%"></i></div>`;
  const safety = standalone ? '' : '<div class="metric2-phone-safety">Данные используются для персонализации ответов. Сервис не заменяет очную диагностику и экстренную помощь.</div>';
  return `<div class="metric2-phone ${kind}${large ? ' large' : ''}"><div class="metric2-phone-status"><span>11:37</span><span>● ▰</span></div>${appHeader}<div class="metric2-phone-content">${content}</div>${safety}<div class="metric2-phone-home"></div></div>`;
}

function renderMetric2(data) {
  data = {
    ...data,
    screens:(data.screens || []).map(screen => screen.id === 'question_company_inn'
      ? {...screen,actions:(screen.actions || []).filter(action => action.id !== 'skip')}
      : screen),
  };
  latestMetric2Data = data;
  metric2ActiveFlow = data.flow === 'result' ? 'result' : 'standard';
  const resultFlow = metric2ActiveFlow === 'result';
  $('#metric2StandardFlow').classList.toggle('active', !resultFlow);
  $('#metric2StandardFlow').setAttribute('aria-selected', String(!resultFlow));
  $('#metric2ResultFlow').classList.toggle('active', resultFlow);
  $('#metric2ResultFlow').setAttribute('aria-selected', String(resultFlow));
  $('#metric2FlowEyebrow').textContent = resultFlow ? 'Ссылка /result' : 'Обычная ссылка';
  $('#metric2FlowTitle').textContent = resultFlow ? 'Получение результатов анализов' : 'Анкета и выбор обследований';
  $('#metric2FlowDescription').textContent = resultFlow
    ? 'Отдельная воронка для пользователей, которые пришли по специальной ссылке за результатами. Обычное анкетирование сюда не входит.'
    : 'Основная воронка новых пользователей: приветствие, регистрация, анкета, обследования и завершение. Переходы по ссылке /result сюда не входят.';
  const summary = $('#metric2Summary');
  summary.innerHTML = [
    [resultFlow ? 'Пришли по пути result' : 'На первом экране',data.summary?.start_users || 0,'100% — база этой ветки'],
    [resultFlow ? 'Получили результат пути' : 'Дошли до завершения',data.summary?.reached_completion || 0,'уникальных пользователей'],
  ].map(([label,value,note]) => `<article class="analytics-metric"><span>${label}</span><strong>${Number(value).toLocaleString('ru-RU')}</strong><small>${note}</small></article>`).join('');
  const root = $('#metric2Flow');
  root.replaceChildren();
  const titleById = Object.fromEntries((data.screens || []).map(item => [item.id,item.title]));
  const logicalGroups = resultFlow
    ? [['result_existing','result_welcome'],['result_found','result_not_found']]
    : [['payment_success','payment_result','payment_unavailable'],['completion','completion_skipped']];
  const logicalGroupById = new Map();
  logicalGroups.forEach(group => group.forEach(id => logicalGroupById.set(id, group)));
  const desktopIds = (data.screens || []).map(screen => screen.id);
  if (!resultFlow && desktopIds.includes('payment_processing')) {
    desktopIds.splice(desktopIds.indexOf('payment_processing'), 1);
    const outcomeEnd = Math.max(...['payment_success','payment_result','payment_unavailable'].map(id => desktopIds.indexOf(id)));
    desktopIds.splice(outcomeEnd + 1, 0, 'payment_processing');
  }
  const desktopOrder = new Map(desktopIds.map((id,index) => [id,index + 1]));
  const desktopLevel = new Map();
  const completedGroups = new Set();
  let desktopSequence = 0;
  for (const id of desktopIds) {
    const screen = (data.screens || []).find(item => item.id === id);
    const group = logicalGroupById.get(id);
    if (group) {
      const groupKey = group.join('|');
      if (!completedGroups.has(groupKey)) {
        desktopSequence += 1;
        completedGroups.add(groupKey);
        group.forEach(groupId => desktopLevel.set(groupId, desktopSequence));
      }
    } else if (screen && (!screen.branch || screen.display_as_main)) {
      desktopSequence += 1;
      desktopLevel.set(id, desktopSequence);
    }
  }
  let mainSequence = 0;
  for (const screen of (data.screens || [])) {
    const logicalGroup = logicalGroupById.get(screen.id);
    const visualBranch = Boolean(screen.branch && !screen.display_as_main && !logicalGroup);
    if (!visualBranch) mainSequence += 1;
    const item = document.createElement('article');
    item.className = `metric2-screen-row${visualBranch ? ' branch' : ''}${logicalGroup ? ` metric2-logical-level metric2-logical-level-${logicalGroup.length}` : ''}`;
    item.style.setProperty('--metric2-desktop-order', desktopOrder.get(screen.id) || 999);
    if (screen.branch) item.dataset.parent = screen.parent_id || '';
    const sequence = document.createElement('div'); sequence.className = 'metric2-sequence';
    sequence.innerHTML = `<span><b class="metric2-sequence-default">${visualBranch ? '↳' : mainSequence}</b><b class="metric2-sequence-desktop">${logicalGroup ? desktopLevel.get(screen.id) : visualBranch ? '↳' : desktopLevel.get(screen.id) || mainSequence}</b></span><i></i>`;
    const open = document.createElement('button'); open.type = 'button'; open.className = 'metric2-screen-open'; open.dataset.metric2Screen = screen.id;
    open.innerHTML = metric2PreviewMarkup(screen);
    const stats = document.createElement('div'); stats.className = 'metric2-screen-stats';
    const comparison = screen.root ? 'Первый экран этого пути — база расчёта'
      : screen.branch ? `${screen.percent_of_parent}% от экрана «${escapeHtml(titleById[screen.parent_id] || 'родительский экран')}»`
      : `${screen.percent_of_parent}% от предыдущего основного экрана`;
    const dropoff = screen.root ? '' : `<div class="metric2-row-dropoff"><b>Не перешли на этот экран: ${Number(screen.dropoff_users || 0).toLocaleString('ru-RU')} из ${Number(screen.comparison_users || 0).toLocaleString('ru-RU')}</b><span class="metric2-dropoff-percent"><span>${Number(screen.dropoff_percent_of_parent || 0).toLocaleString('ru-RU')}% от предыдущего</span><span>${Number(screen.dropoff_percent_of_start || 0).toLocaleString('ru-RU')}% от первого</span></span><small><span>Выбрали другой путь: ${Number(screen.alternate_path_users || 0).toLocaleString('ru-RU')}</span><span>Остановились на предыдущем: ${Number(screen.actual_dropoff_users || 0).toLocaleString('ru-RU')}</span></small></div>`;
    const quality = screen.data_quality === 'incomplete'
      ? `<span class="metric2-quality incomplete">Неполные данные · ${Number(screen.incomplete_transition_users || 0).toLocaleString('ru-RU')}</span>`
      : '<span class="metric2-quality complete">Маршрут полный</span>';
    stats.innerHTML = `<span class="metric2-stage">${visualBranch ? 'Ответвление · ' : ''}${escapeHtml(screen.stage || '')}</span>${quality}<h3>${escapeHtml(screen.title)}</h3><div class="metric2-reach"><strong>${Number(screen.percent_of_start || 0).toLocaleString('ru-RU')}%</strong><span>от первого экрана</span></div><p><b>${Number(screen.users || 0).toLocaleString('ru-RU')}</b> пользователей · ${comparison}</p>${dropoff}<div class="metric2-reach-track"><i style="width:${Math.min(100,Number(screen.percent_of_start || 0))}%"></i></div><button type="button" data-metric2-screen="${escapeHtml(screen.id)}">Открыть экран и всю статистику →</button>`;
    item.append(sequence,open,stats); root.append(item);
  }
  if (!(data.screens || []).length) root.innerHTML = `<p class="form-error">Пока нет данных по ветке «${resultFlow ? 'Ссылка result' : 'Обычная ссылка'}»</p>`;
  fillAnalyticsSelect('#metric2Device',data.filter_options?.devices || [],'Все устройства');
  fillAnalyticsSelect('#metric2Method',data.filter_options?.methods || [],'Все способы');
  fillAnalyticsSelect('#metric2Source',data.filter_options?.sources || [],'Все источники');
}

function openMetric2Screen(screenId) {
  const screen = latestMetric2Data?.screens?.find(item => item.id === screenId);
  if (!screen) return;
  const titleById = Object.fromEntries(latestMetric2Data.screens.map(item => [item.id,item.title]));
  $('#metric2ModalPreview').innerHTML = metric2PreviewMarkup(screen,true);
  $('#metric2ModalStage').textContent = screen.stage || '';
  const quality = $('#metric2ModalQuality');
  quality.className = `metric2-quality ${screen.data_quality === 'incomplete' ? 'incomplete' : 'complete'}`;
  quality.textContent = screen.data_quality === 'incomplete'
    ? `Неполные данные маршрута · ${Number(screen.incomplete_transition_users || 0).toLocaleString('ru-RU')} пользователей`
    : 'Маршрут полный';
  $('#metric2ModalTitle').textContent = screen.title;
  $('#metric2ModalDescription').textContent = screen.description || '';
  const comparisonTitle = screen.comparison_id ? titleById[screen.comparison_id] || 'предыдущий экран' : '';
  const finishLabel = screen.terminal ? 'Завершили путь здесь' : 'Не перешли дальше';
  $('#metric2ModalReach').innerHTML = `
    <article><span>Пришли на экран</span><strong>${Number(screen.users || 0).toLocaleString('ru-RU')}</strong><small><span>${Number(screen.percent_of_start || 0).toLocaleString('ru-RU')}% от первого</span>${screen.comparison_id ? `<span>${Number(screen.percent_of_parent || 0).toLocaleString('ru-RU')}% от «${escapeHtml(comparisonTitle)}»</span>` : ''}</small></article>
    <article><span>Перешли на другой экран</span><strong>${Number(screen.outgoing_users || 0).toLocaleString('ru-RU')}</strong><small><span>${Number(screen.outgoing_percent_of_screen || 0).toLocaleString('ru-RU')}% от этого экрана</span><span>${Number(screen.outgoing_percent_of_start || 0).toLocaleString('ru-RU')}% от первого</span></small></article>
    <article class="${screen.terminal ? 'terminal' : 'dropoff'}"><span>${finishLabel}</span><strong>${Number(screen.stopped_users || 0).toLocaleString('ru-RU')}</strong><small><span>${Number(screen.stopped_percent_of_screen || 0).toLocaleString('ru-RU')}% от этого экрана</span><span>${Number(screen.stopped_percent_of_start || 0).toLocaleString('ru-RU')}% от первого</span></small></article>
    ${screen.comparison_id ? `<article class="dropoff"><span>Не перешли на этот экран</span><strong>${Number(screen.dropoff_users || 0).toLocaleString('ru-RU')} из ${Number(screen.comparison_users || 0).toLocaleString('ru-RU')}</strong><small><span>${Number(screen.dropoff_percent_of_parent || 0).toLocaleString('ru-RU')}% от «${escapeHtml(comparisonTitle)}»</span><span>${Number(screen.dropoff_percent_of_start || 0).toLocaleString('ru-RU')}% от первого</span><span>Другой путь: ${Number(screen.alternate_path_users || 0).toLocaleString('ru-RU')}</span><span>Остановились на предыдущем: ${Number(screen.actual_dropoff_users || 0).toLocaleString('ru-RU')}</span></small></article>` : ''}`;
  const actions = $('#metric2ModalActions'); actions.replaceChildren();
  const actionGroups = [
    {mode:'final_transition', title:'Итоговый переход', note:'Для каждого пользователя учитывается только последнее действие, определившее дальнейший путь.'},
    {mode:'final_choice', title:'Итоговый выбор', note:'Если пользователь менял выбор, учитывается только последний вариант.'},
    {mode:'interaction', title:'Дополнительные действия', note:'Один пользователь может выполнить несколько таких действий, поэтому эти строки не нужно складывать между собой.'},
  ];
  for (const group of actionGroups) {
    const groupActions = (screen.actions || []).filter(action => (action.counting_mode || 'interaction') === group.mode);
    if (!groupActions.length) continue;
    const section = document.createElement('section'); section.className = `metric2-action-group ${group.mode}`;
    section.innerHTML = `<h4>${group.title}</h4><p>${group.note}</p>`;
    for (const action of groupActions) {
      const target = action.target ? titleById[action.target] : action.target_label;
      const row = document.createElement('article'); row.className = 'metric2-action-row';
      row.innerHTML = `<div><strong>${escapeHtml(action.label)}</strong><b>${Number(action.percent_of_screen || 0).toLocaleString('ru-RU')}%</b></div><div class="metric2-action-track"><i style="width:${Math.min(100,Number(action.percent_of_screen || 0))}%"></i></div><p>${Number(action.users || 0).toLocaleString('ru-RU')} уникальных пользователей · ${Number(action.percent_of_start || 0).toLocaleString('ru-RU')}% от первого${target ? ` · действие ведёт → ${escapeHtml(target)}` : ''}</p>`;
      section.append(row);
    }
    actions.append(section);
  }
  if (!(screen.actions || []).length) actions.innerHTML = '<p class="form-error">На этом экране нет отдельных действий</p>';
  const transitions = $('#metric2ModalTransitions'); transitions.replaceChildren();
  const routeNote = document.createElement('p');
  routeNote.className = 'metric2-route-note';
  routeNote.textContent = 'Прямые переходы подтверждены логикой экранов. Если между двумя записанными экранами не хватает обязательных шагов, такая связь вынесена отдельно как неполный маршрут и не считается прямым переходом.';
  transitions.append(routeNote);
  const transitionSection = (title,items,direction) => {
    const section = document.createElement('section');
    section.innerHTML = `<h4>${title}</h4>`;
    const list = document.createElement('div'); list.className = 'metric2-transition-list';
    for (const item of items || []) {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = `metric2-transition-row${item.direct === false ? ' incomplete' : ''}`;
      row.dataset.metric2Screen = item.screen_id;
      row.setAttribute('aria-label', `Открыть экран «${item.title}»`);
      const localPercent = direction === 'in' ? item.percent_of_source : item.percent_of_screen;
      row.innerHTML = `${item.direct === false ? '<em>Неполный маршрут</em>' : ''}<div><strong>${escapeHtml(item.title)}</strong><span><b>${Number(item.users || 0).toLocaleString('ru-RU')}</b><i aria-hidden="true">→</i></span></div><p><span>${Number(localPercent || 0).toLocaleString('ru-RU')}% ${direction === 'in' ? 'от исходного экрана' : 'от этого экрана'}</span><span>${Number(item.percent_of_start || 0).toLocaleString('ru-RU')}% от первого экрана</span></p><small>${escapeHtml(item.explanation || '')}</small>`;
      list.append(row);
    }
    if (!(items || []).length) list.innerHTML = `<p class="form-error">${direction === 'in' ? 'Это первый экран или переходов пока нет' : 'Переходов на другие экраны пока нет'}</p>`;
    section.append(list); transitions.append(section);
  };
  const incomingTransitions = screen.incoming_transitions || [];
  const outgoingTransitions = screen.outgoing_transitions || [];
  transitionSection('Откуда пришли — прямые переходы', incomingTransitions.filter(item => item.direct !== false), 'in');
  transitionSection('Куда перешли — прямые переходы', outgoingTransitions.filter(item => item.direct !== false), 'out');
  const incompleteTransitions = [
    ...incomingTransitions.filter(item => item.direct === false).map(item => ({...item, gapDirection: 'in'})),
    ...outgoingTransitions.filter(item => item.direct === false).map(item => ({...item, gapDirection: 'out'})),
  ];
  if (incompleteTransitions.length) {
    const section = document.createElement('section');
    section.className = 'metric2-incomplete-section';
    section.innerHTML = '<h4>Неполные связи данных</h4><p>Это не прямые переходы. В истории этих пользователей отсутствуют обязательные промежуточные экраны.</p>';
    const list = document.createElement('div');
    list.className = 'metric2-transition-list';
    for (const item of incompleteTransitions) {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'metric2-transition-row incomplete';
      row.dataset.metric2Screen = item.screen_id;
      row.setAttribute('aria-label', `Открыть связанный экран «${item.title}»`);
      const localPercent = item.gapDirection === 'in' ? item.percent_of_source : item.percent_of_screen;
      row.innerHTML = `<em>${item.gapDirection === 'in' ? 'До этого экрана' : 'После этого экрана'} · неполный маршрут</em><div><strong>${escapeHtml(item.title)}</strong><span><b>${Number(item.users || 0).toLocaleString('ru-RU')}</b><i aria-hidden="true">→</i></span></div><p><span>${Number(localPercent || 0).toLocaleString('ru-RU')}% ${item.gapDirection === 'in' ? 'от связанного экрана' : 'от этого экрана'}</span><span>${Number(item.percent_of_start || 0).toLocaleString('ru-RU')}% от первого экрана</span></p><small>${escapeHtml(item.explanation || '')}</small>`;
      list.append(row);
    }
    section.append(list);
    transitions.append(section);
  }
  $('#metric2Modal').classList.remove('hidden');
  document.body.classList.add('metric2-modal-open');
  $('.metric2-modal-close').focus();
}

function closeMetric2Modal() {
  $('#metric2Modal').classList.add('hidden');
  document.body.classList.remove('metric2-modal-open');
}

async function loadMetric2(flow = metric2ActiveFlow) {
  metric2ActiveFlow = flow === 'result' ? 'result' : 'standard';
  closeMetric2Modal();
  return withPanelLoading('#metric2AdminView', async () => {
    const params = new URLSearchParams({period:$('#metric2Period').value,flow:metric2ActiveFlow});
    for (const [key,selector] of [['device','#metric2Device'],['method','#metric2Method'],['source','#metric2Source']]) {
      if ($(selector).value) params.set(key,$(selector).value);
    }
    appendDateRange(params, '#metric2DateFrom', '#metric2DateTo');
    const requestedFlow = metric2ActiveFlow;
    const report = await adminFetch(`/api/admin/metric2?${params}`);
    if (requestedFlow === metric2ActiveFlow) renderMetric2(report);
  }, metric2ActiveFlow === 'result' ? 'Строим путь получения результатов…' : 'Строим обычный стартовый путь…');
}

async function loadFavoriteSources() {
  const ids = [...favoriteAnalytics];
  const tasks = [];
  if (ids.some(id => id.startsWith('analytics-'))) tasks.push(loadAnalytics());
  if (ids.some(id => id.startsWith('cost-'))) tasks.push(loadCosts());
  for (const [id,key] of [
    ['dashboard-users','users'],
    ['dashboard-devices-table','devices'],
    ['dashboard-conversations','conversations'],
    ['dashboard-requests','requests'],
  ]) {
    if (favoriteAnalytics.has(id)) tasks.push(loadTable(key));
  }
  await withPanelLoading('#favoritesAdminView', () => Promise.all(tasks), 'Собираем избранную аналитику…');
  decorateFavoriteSources();
  renderFavorites();
}

function showAdminView(view) {
  activeAdminView = ['favorites','analytics','metric2','managers','examinations','costs'].includes(view) ? view : 'dashboard';
  const favoritesVisible = activeAdminView === 'favorites';
  const analyticsVisible = activeAdminView === 'analytics';
  const metric2Visible = activeAdminView === 'metric2';
  const managersVisible = activeAdminView === 'managers';
  const examinationsVisible = activeAdminView === 'examinations';
  const costsVisible = activeAdminView === 'costs';
  $('#dashboard').classList.toggle('show-managers', managersVisible);
  $('#dashboard').classList.toggle('show-examinations', examinationsVisible);
  $('#dashboard').classList.toggle('show-costs', costsVisible);
  $('#dashboard').classList.toggle('show-analytics', analyticsVisible);
  $('#dashboard').classList.toggle('show-metric2', metric2Visible);
  $('#dashboard').classList.toggle('show-favorites', favoritesVisible);
  $('#favoritesAdminView').classList.toggle('hidden', !favoritesVisible);
  $('#analyticsAdminView').classList.toggle('hidden', !analyticsVisible);
  $('#metric2AdminView').classList.toggle('hidden', !metric2Visible);
  $('#managerAdminView').classList.toggle('hidden', !managersVisible);
  $('#examinationAdminView').classList.toggle('hidden', !examinationsVisible);
  $('#costsAdminView').classList.toggle('hidden', !costsVisible);
  $('#dashboardTab').classList.toggle('active', activeAdminView === 'dashboard');
  $('#favoritesTab').classList.toggle('active', favoritesVisible);
  $('#analyticsTab').classList.toggle('active', analyticsVisible);
  $('#metric2Tab').classList.toggle('active', metric2Visible);
  $('#managersTab').classList.toggle('active', managersVisible);
  $('#examinationsTab').classList.toggle('active', examinationsVisible);
  $('#costsTab').classList.toggle('active', costsVisible);
  // На экране входа токена ещё нет: запросы к защищённым таблицам в этот
  // момент не запускаем, иначе каждый ответ 401 снова откроет форму входа.
  if (sessionStorage.getItem(TOKEN_KEY)) {
    loadAdminViewData(activeAdminView).catch(showDashboardError);
  }
}

async function loadAdminViewData(view = activeAdminView, {force = false} = {}) {
  if (!force && loadedAdminViews.has(view)) return;
  let request;
  if (view === 'dashboard') request = loadAllTables();
  else if (view === 'favorites') request = loadFavoriteSources();
  else if (view === 'managers') request = loadStaff();
  else if (view === 'examinations') request = loadExaminations();
  else if (view === 'costs') request = loadCosts();
  else if (view === 'analytics') request = loadAnalytics();
  else if (view === 'metric2') request = loadMetric2();
  else return;
  await request;
  loadedAdminViews.add(view);
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
    competitor_price:$('#examinationCompetitorPrice').value,
    price_without_discount:$('#examinationPriceWithoutDiscount').value,
    competitor_label:$('#examinationCompetitorLabel').value.trim(),
    retail_price_label:$('#examinationRetailPriceLabel').value.trim(),
    discount_price_label:$('#examinationDiscountPriceLabel').value.trim(),
    show_competitor_price:$('#showExaminationCompetitorPrice').checked,
    show_retail_price:$('#showExaminationRetailPrice').checked,
    show_discount_price:$('#showExaminationDiscountPrice').checked,
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
    $('#examinationCompetitorPrice').value = Number(item.competitor_price || 0) || '';
    $('#examinationPriceWithoutDiscount').value = Number(item.price_without_discount || 0) || '';
    $('#examinationCompetitorLabel').value = item.competitor_label || 'У конкурентов';
    $('#examinationRetailPriceLabel').value = item.retail_price_label || 'Розничная цена';
    $('#examinationDiscountPriceLabel').value = item.discount_price_label || 'С учётом вашей скидки';
    $('#showExaminationCompetitorPrice').checked = examinationPriceSettingEnabled(item, 'show_competitor_price');
    $('#showExaminationRetailPrice').checked = examinationPriceSettingEnabled(item, 'show_retail_price');
    $('#showExaminationDiscountPrice').checked = examinationPriceSettingEnabled(item, 'show_discount_price');
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

function updateTableSortIndicators(state) {
  const table = $(`#${state.prefix}Table`)?.closest('table');
  if (!table) return;
  table.querySelectorAll('[data-table-sort]').forEach(button => {
    const active = button.dataset.tableSort === state.sort;
    button.classList.toggle('active',active);
    button.setAttribute('aria-pressed',String(active));
    button.title = active
      ? `Сортировка ${state.order === 'asc' ? 'по возрастанию' : 'по убыванию'}. Нажмите, чтобы изменить направление.`
      : 'Сортировать по этому столбцу';
    const icon = button.querySelector('i');
    if (icon) icon.textContent = active ? (state.order === 'asc' ? '↑' : '↓') : '↕';
  });
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
    sort:state.sort,order:state.order,
  });
  if (key === 'users') {
    state.createdFrom = $('#usersDateFrom').value;
    state.createdTo = $('#usersDateTo').value;
    if (state.createdFrom) params.set('created_from',state.createdFrom);
    if (state.createdTo) params.set('created_to',state.createdTo);
  }
  const panel = $(`#${state.prefix}Table`)?.closest('.table-panel');
  return withPanelLoading(panel, async () => {
    const data = await adminFetch(`/api/admin/table?${params}`);
    state.total = data.total;
    state.offset = data.offset;
    state.sort = data.sort || state.sort;
    state.order = data.order || state.order;
    if (key === 'users') renderUsers(data.rows,data.total,{
      overallTotal:data.overall_total,
      periodTotal:data.period_total,
      filterActive:Boolean(state.query || state.createdFrom || state.createdTo),
    });
    else if (key === 'devices') renderDevices(data.rows,data.total);
    else if (key === 'conversations') renderConversations(data.rows,data.total);
    else renderRequests(data.rows,data.total);
    renderTablePage(state);
    updateTableSortIndicators(state);
  }, 'Загружаем таблицу…');
}

async function loadAllTables() {
  await Promise.all(Object.keys(tableStates).map(key => loadTable(key)));
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
  input.closest('.table-panel').querySelector('thead').addEventListener('click', event => {
    const button = event.target.closest('[data-table-sort]');
    if (!button) return;
    const nextSort = button.dataset.tableSort;
    state.order = state.sort === nextSort && state.order === 'desc' ? 'asc' : 'desc';
    state.sort = nextSort;
    state.offset = 0;
    loadTable(key).catch(showDashboardError);
  });
  updateTableSortIndicators(state);
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
  topProgressRequests = 0;
  loadedAdminViews.clear();
  $('#adminTopProgress')?.classList.add('hidden');
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
  // Дочерние панели используют токен из sessionStorage. Сохраняем его до
  // начала параллельных запросов; при отказе авторизации блок catch удалит его.
  sessionStorage.setItem(TOKEN_KEY,token);
  dashboardLoading = true;
  const button = $('#refreshButton');
  button.disabled = true;
  button.classList.add('is-loading');
  $('#dashboardError').classList.add('hidden');
  setPanelLoading('#summaryGrid', true, 'Обновляем сводку…');
  setPanelLoading($('.analytics-grid'), true, 'Обновляем графики…');
  try {
    // Запускаем содержимое текущей вкладки одновременно со сводкой. Ошибку
    // превращаем в значение сразу, чтобы отклонённый промис не оставался
    // необработанным, пока основной запрос ещё выполняется.
    const detailPromise = loadAdminViewData(activeAdminView, {force:true}).then(
      () => null,
      error => error,
    );
    const data = await adminFetch('/api/admin/dashboard',token);
    sessionStorage.setItem(TOKEN_KEY,token);
    showDashboard();
    renderDashboard(data);
    const detailError = await detailPromise;
    if (detailError) throw detailError;
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
    button.classList.remove('is-loading');
    setPanelLoading('#summaryGrid', false);
    setPanelLoading($('.analytics-grid'), false);
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
$('#favoritesTab').addEventListener('click', () => showAdminView('favorites'));
$('#analyticsTab').addEventListener('click', () => showAdminView('analytics'));
$('#metric2Tab').addEventListener('click', () => showAdminView('metric2'));
$('#managersTab').addEventListener('click', () => showAdminView('managers'));
$('#examinationsTab').addEventListener('click', () => showAdminView('examinations'));
$('#costsTab').addEventListener('click', () => showAdminView('costs'));
$('#metric2Apply').addEventListener('click', () => loadMetric2().catch(showDashboardError));
$('#metric2StandardFlow').addEventListener('click', () => {
  if (metric2ActiveFlow !== 'standard') loadMetric2('standard').catch(showDashboardError);
});
$('#metric2ResultFlow').addEventListener('click', () => {
  if (metric2ActiveFlow !== 'result') loadMetric2('result').catch(showDashboardError);
});
$('#metric2Flow').addEventListener('click', event => {
  const target = event.target.closest('[data-metric2-screen]');
  if (target) openMetric2Screen(target.dataset.metric2Screen);
});
$('#metric2Modal').addEventListener('click', event => {
  const screenLink = event.target.closest('[data-metric2-screen]');
  if (screenLink) {
    openMetric2Screen(screenLink.dataset.metric2Screen);
    return;
  }
  if (event.target.closest('[data-metric2-close]')) closeMetric2Modal();
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && !$('#metric2Modal').classList.contains('hidden')) closeMetric2Modal();
});
document.addEventListener('click', event => {
  const toggle = event.target.closest('[data-favorite-toggle]');
  if (toggle) {
    const id = toggle.dataset.favoriteToggle;
    if (favoriteAnalytics.has(id)) favoriteAnalytics.delete(id);
    else favoriteAnalytics.add(id);
    saveFavoriteAnalytics();
    return;
  }
  const open = event.target.closest('[data-favorite-open]');
  if (!open) return;
  showAdminView(open.dataset.favoriteOpen);
  requestAnimationFrame(() => {
    const source = findFavoriteSource(open.dataset.favoriteTarget);
    source?.scrollIntoView({behavior:'smooth',block:'center'});
    source?.classList.add('favorite-highlight');
    setTimeout(() => source?.classList.remove('favorite-highlight'),1400);
  });
});
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
decorateFavoriteSources();
loadDashboard();
