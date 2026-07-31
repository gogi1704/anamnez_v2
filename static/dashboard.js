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
let staffItems = [];
let examinationItems = [];
let activeAdminView = 'dashboard';
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
  if (!items.length) emptyTable(root,7);
  for (const item of items) {
    const row = document.createElement('tr');
    textCell(row,item.chel_id,item.chel_id);
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

async function adminFetch(path, token = sessionStorage.getItem(TOKEN_KEY), options = {}) {
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
  $('#staffList').innerHTML = items.length ? items.map(item => `
    <article class="staff-card ${item.is_active ? '' : 'inactive'}" data-staff-id="${item.id}">
      <div><strong>${escapeHtml(item.display_name)}</strong><small>Логин: ${escapeHtml(item.login)} · ${item.is_active ? 'доступ активен' : 'доступ отключён'}</small><small>Последний вход: ${escapeHtml(formatDate(item.last_login_at))}</small></div>
      <div class="staff-card-actions">
        <button class="reset-password" data-staff-action="name" type="button">Изменить имя</button>
        <button class="reset-password" data-staff-action="password" type="button">Новый пароль</button>
        <button class="toggle-staff" data-staff-action="toggle" data-active="${item.is_active}" type="button">${item.is_active ? 'Отключить' : 'Включить'}</button>
        <button class="delete-staff" data-staff-action="delete" type="button">Удалить</button>
      </div>
    </article>`).join('') : '<p class="form-error">Менеджеры ещё не созданы</p>';
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;',
  }[char]));
}

async function loadStaff() {
  renderStaff(await adminFetch('/api/admin/managers'));
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

function showAdminView(view) {
  activeAdminView = ['managers','examinations'].includes(view) ? view : 'dashboard';
  const managersVisible = activeAdminView === 'managers';
  const examinationsVisible = activeAdminView === 'examinations';
  $('#dashboard').classList.toggle('show-managers', managersVisible);
  $('#dashboard').classList.toggle('show-examinations', examinationsVisible);
  $('#managerAdminView').classList.toggle('hidden', !managersVisible);
  $('#examinationAdminView').classList.toggle('hidden', !examinationsVisible);
  $('#dashboardTab').classList.toggle('active', activeAdminView === 'dashboard');
  $('#managersTab').classList.toggle('active', managersVisible);
  $('#examinationsTab').classList.toggle('active', examinationsVisible);
  if (managersVisible) loadStaff().catch(showDashboardError);
  if (examinationsVisible) loadExaminations().catch(showDashboardError);
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
  if (action === 'name') {
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
  const button = $('#refreshButton');
  button.disabled = true;
  $('#dashboardError').classList.add('hidden');
  try {
    const data = await adminFetch('/api/admin/dashboard',token);
    sessionStorage.setItem(TOKEN_KEY,token);
    showDashboard();
    renderDashboard(data);
    await loadAllTables();
    if (activeAdminView === 'managers') await loadStaff();
    if (activeAdminView === 'examinations') await loadExaminations();
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
$('#managersTab').addEventListener('click', () => showAdminView('managers'));
$('#examinationsTab').addEventListener('click', () => showAdminView('examinations'));
$('#managerCreateForm').addEventListener('submit', createManager);
$('#staffList').addEventListener('click', updateManager);
$('#examinationForm').addEventListener('submit', saveExamination);
$('#examinationList').addEventListener('click', manageExamination);
$('#cancelExaminationEdit').addEventListener('click', () => {
  resetExaminationForm();
  showExaminationStatus('');
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
