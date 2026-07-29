const state = {
  active: 'manager',
  processing: false,
  conversationId: localStorage.getItem('consilium_conversation_id'),
  context: null,
  urgency: 'routine',
  attachments: [],
  memories: [],
  profile: null,
  onboarding: null,
  onboardingStep: 0,
  onboardingAnswers: {},
  selectedTests: new Set(),
  mainInitialized: false,
  fontSize: 'standard',
  bodySymptoms: [],
  selectedBodyRegion: null,
  selectedBodyView: 'front',
  selectedSymptomType: null,
  healthHistory: [],
  healthHistoryFilter: 'all',
  labDocuments: [],
  identity: null,
  aiEnabled: true,
  humanStatus: 'none',
  lastMessageId: 0,
  returnToHumanAfterContextEdit: false,
  contextEditTicketId: null,
};

const $ = selector => document.querySelector(selector);
const messages = $('#messages');
const timeline = $('#timeline');
const input = $('#messageInput');
const ANONYMOUS_ACCESS_KEY = 'consilium_anonymous_access';
let userAudioContext = null;

function unlockUserSound() {
  if (!userAudioContext) {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) return null;
    userAudioContext = new AudioContextClass();
  }
  if (userAudioContext.state === 'suspended') userAudioContext.resume().catch(() => {});
  return userAudioContext;
}

function playUserMessageSound() {
  const context = unlockUserSound();
  if (!context || context.state !== 'running') return;
  const now = context.currentTime + 0.012;
  const oscillator = context.createOscillator();
  const gain = context.createGain();
  oscillator.type = 'sine';
  oscillator.frequency.setValueAtTime(760, now);
  oscillator.frequency.exponentialRampToValueAtTime(940, now + 0.12);
  gain.gain.setValueAtTime(0.0001, now);
  gain.gain.exponentialRampToValueAtTime(0.065, now + 0.012);
  gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.2);
  oscillator.connect(gain);
  gain.connect(context.destination);
  oscillator.start(now);
  oscillator.stop(now + 0.22);
}

const onboardingQuestions = [
  { key:'preferred_name', title:'Как к вам обращаться?', lead:'Имя необязательно, но с ним общение будет естественнее.', type:'text', placeholder:'Например, Алексей', optional:true },
  { key:'age', title:'Сколько вам полных лет?', lead:'Возраст помогает специалистам точнее учитывать риски и нормы.', type:'number', min:0, max:120, placeholder:'Возраст' },
  { key:'sex', title:'Укажите пол для медицинского контекста', lead:'Это важно для интерпретации части симптомов и обследований.', choices:[['female','Женский'],['male','Мужской']] },
  { key:'height_cm', title:'Какой у вас рост?', lead:'Введите значение в сантиметрах.', type:'number', min:30, max:250, step:'0.1', placeholder:'Например, 176' },
  { key:'weight_kg', title:'Какой у вас вес?', lead:'Введите актуальный вес в килограммах.', type:'number', min:1, max:500, step:'0.1', placeholder:'Например, 72' },
  { key:'smoking', title:'Вы курите?', lead:'Учитываются сигареты, электронные сигареты и другие способы употребления никотина.', choices:[['never','Не курю'],['former','Курил(а) раньше'],['current','Курю сейчас']] },
  { key:'alcohol', title:'Как часто вы употребляете алкоголь?', lead:'Выберите наиболее близкий вариант.', choices:[['never','Не употребляю'],['rarely','Редко / по праздникам'],['weekly','Примерно раз в неделю'],['often','Чаще раза в неделю']] },
  { key:'activity', title:'Какой у вас уровень активности?', lead:'Низкий — до 5 000 шагов, средний — 5–10 тысяч, высокий — более 10 тысяч или регулярный спорт.', choices:[['low','Низкий'],['moderate','Средний'],['high','Высокий']] },
  { key:'blood_pressure', title:'Как вы оцениваете своё давление?', lead:'Если не измеряли или не уверены, выберите «Не знаю».', choices:[['normal','Обычно в норме'],['high','Бывает повышенным'],['low','Бывает пониженным'],['unstable','Сильно меняется'],['unknown','Не знаю']] },
  { key:'dark_in_eyes', title:'Темнеет ли в глазах при резком подъёме?', lead:'Например, когда быстро встаёте с кровати или стула.', choices:[['no','Нет'],['yes','Да'],['unknown','Не уверен(а)']] },
  { key:'blood_sugar', title:'Знаете ли вы уровень сахара в крови?', lead:'Это не оценка диагноза — только уже известная вам информация.', choices:[['normal','Был в норме'],['high','Бывал повышен'],['unknown','Не измерял(а) / не знаю']] },
  { key:'joint_pain', title:'Бывают боли или отёчность суставов?', lead:'В том числе при нагрузке или смене погоды.', choices:[['no','Нет'],['yes','Да'],['unknown','Не уверен(а)']] },
  { key:'fatigue', title:'Беспокоит длительная усталость?', lead:'Имеется в виду усталость, которая сохраняется после обычного отдыха.', choices:[['no','Нет'],['yes','Да'],['unknown','Не уверен(а)']] },
  { key:'conditions', title:'Есть хронические заболевания?', lead:'Напишите по одному на строку. Если нет — этот шаг можно пропустить.', type:'textarea', placeholder:'Например:\nГипертония\nАстма', optional:true, list:true },
  { key:'medications', title:'Какие лекарства принимаете постоянно?', lead:'Название и дозировка, если известна. Шаг можно пропустить.', type:'textarea', placeholder:'По одному препарату на строку', optional:true, list:true },
  { key:'allergies', title:'Есть аллергии?', lead:'Укажите лекарства, продукты или другие известные аллергены. Шаг можно пропустить.', type:'textarea', placeholder:'По одному аллергену на строку', optional:true, list:true },
  { key:'pregnancy', title:'Есть ли беременность?', lead:'Этот вопрос показывается только когда он может быть релевантен.', choices:[['no','Нет'],['yes','Да'],['possible','Возможна'],['unknown','Не знаю']], femaleOnly:true },
];

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `Ошибка сервера: ${response.status}`);
  return data;
}

function setAuthStatus(message = '', error = false) {
  const status = $('#authStatus');
  status.textContent = message;
  status.classList.toggle('hidden', !message);
  status.classList.toggle('error', Boolean(error));
}

function showAuthGate() {
  $('#authGate').classList.remove('hidden');
  $('#onboarding').classList.add('hidden');
  $('#appShell').classList.add('hidden');
}

function closeAnonymousWarning() {
  $('#anonymousWarning').classList.add('hidden');
}

async function startApplication() {
  $('#authGate').classList.add('hidden');
  closeAnonymousWarning();
  await loadOnboarding();
}

async function startMessengerAuth(provider) {
  const button = provider === 'telegram' ? $('#telegramAuthButton') : $('#maxAuthButton');
  button.disabled = true;
  setAuthStatus('');
  try {
    const result = await api('/api/auth/messenger/start', {
      method:'POST',
      body:JSON.stringify({ provider }),
    });
    window.location.assign(result.bot_url);
  } catch (error) {
    setAuthStatus(`${error.message}. Анонимный вход уже доступен, а подключение бота можно завершить позже.`, true);
    button.disabled = false;
  }
}

$('#telegramAuthButton').addEventListener('click', () => startMessengerAuth('telegram'));
$('#maxAuthButton').addEventListener('click', () => startMessengerAuth('max'));
$('#anonymousAuthButton').addEventListener('click', () => $('#anonymousWarning').classList.remove('hidden'));
$('#anonymousWarningClose').addEventListener('click', closeAnonymousWarning);
$('#anonymousWarningCancel').addEventListener('click', closeAnonymousWarning);
$('#anonymousWarning').addEventListener('click', event => {
  if (event.target === $('#anonymousWarning')) closeAnonymousWarning();
});
$('#anonymousWarningConfirm').addEventListener('click', async () => {
  localStorage.setItem(ANONYMOUS_ACCESS_KEY, state.identity?.chel_id || '');
  try {
    await startApplication();
  } catch (error) {
    showAuthGate();
    setAuthStatus(`Не удалось открыть Консилиум: ${error.message}`, true);
  }
});

async function resetUser() {
  const confirmed = window.confirm(
    'Начать заново?\n\nБудут безвозвратно удалены анкета, все диалоги, память, история здоровья, симптомы, обращения человеку и сохранённый номер созвона.\n\nЕсли вы вошли через MAX, привязка к аккаунту и ваш chel_id сохранятся.'
  );
  if (!confirmed) return;
  const button = $('#resetUserButton');
  button.disabled = true;
  try {
    await api('/api/reset-user', {
      method:'POST',
      headers:{ 'X-Consilium-Action':'reset-user' },
      body:'{}',
    });
    localStorage.removeItem('consilium_conversation_id');
    window.location.reload();
  } catch (error) {
    button.disabled = false;
    alert(`Не удалось начать заново: ${error.message}`);
  }
}

function activeOnboardingQuestions() {
  return onboardingQuestions.filter(question => !question.femaleOnly || state.onboardingAnswers.sex === 'female');
}

function seedOnboardingAnswers(profile = {}) {
  for (const question of onboardingQuestions) {
    const value = profile[question.key];
    if (Array.isArray(value) && !value.length) continue;
    if (value !== undefined && value !== null && value !== 'unknown' && value !== 'not_applicable') {
      state.onboardingAnswers[question.key] = value;
    }
  }
}

function escapeAttr(value) {
  return String(value ?? '').replaceAll('&','&amp;').replaceAll('"','&quot;').replaceAll('<','&lt;').replaceAll('>','&gt;');
}

function setOnboardingMeta(stage, progress) {
  $('#onboardingStage').textContent = stage;
  $('#onboardingProgress').style.width = `${progress}%`;
  $('#onboardingError').classList.add('hidden');
}

const fontSizeLabels = { standard:'Обычный', large:'Крупный', extra:'Очень крупный' };

function applyFontSize(size) {
  state.fontSize = fontSizeLabels[size] ? size : 'standard';
  document.body.dataset.fontSize = state.fontSize;
  if ($('#menuFontSizeStatus')) $('#menuFontSizeStatus').textContent = fontSizeLabels[state.fontSize];
}

function fontSizeChoices(className) {
  return Object.entries(fontSizeLabels).map(([size,label]) => `
    <button type="button" class="${className} ${state.fontSize === size ? 'selected' : ''}" data-size="${size}">
      <i>Аа</i><span><b>${label}</b><small>${size === 'standard' ? 'Чуть крупнее базового интерфейса' : size === 'large' ? 'Комфортно для большинства экранов' : 'Максимальная читаемость'}</small></span>
    </button>`).join('');
}

function renderAppearance() {
  setOnboardingMeta('Настройка', 2);
  $('#onboardingContent').innerHTML = `<span class="onboarding-kicker">Перед началом</span><h1>Какой размер текста вам удобен?</h1><p class="onboarding-lead">Вы увидите изменение сразу. Позже размер можно поменять через меню функций.</p><div class="appearance-options">${fontSizeChoices('appearance-choice')}</div><div class="onboarding-actions"><button type="button" class="onboarding-next" data-onboarding-action="save-appearance">Продолжить</button></div>`;
}

async function saveAppearance(size = state.fontSize) {
  try {
    state.onboarding = await api('/api/onboarding/appearance', { method:'POST', body:JSON.stringify({ font_size:size }) });
    applyFontSize(state.onboarding.font_size);
    renderQuestion();
  } catch (error) { showOnboardingError(error.message); }
}

function showOnboardingError(message) {
  $('#onboardingError').textContent = message;
  $('#onboardingError').classList.remove('hidden');
}

function onboardingNextLabel(question, value, isLast) {
  if (isLast) return 'Завершить анкету';
  const empty = Array.isArray(value) ? !value.length : !String(value ?? '').trim();
  return question.optional && empty ? 'Пропустить' : 'Продолжить';
}

function renderQuestion() {
  const questions = activeOnboardingQuestions();
  state.onboardingStep = Math.max(0, Math.min(state.onboardingStep, questions.length - 1));
  const question = questions[state.onboardingStep];
  const value = state.onboardingAnswers[question.key] ?? '';
  setOnboardingMeta('Анкета', 5 + Math.round((state.onboardingStep / questions.length) * 60));
  const control = question.choices
    ? `<div class="choice-grid">${question.choices.map(([id,label]) => `<button type="button" class="choice-button ${value === id ? 'selected' : ''}" data-choice="${id}">${label}</button>`).join('')}</div>`
    : question.type === 'textarea'
      ? `<textarea class="onboarding-input onboarding-input-area" id="onboardingInput" placeholder="${escapeAttr(question.placeholder || '')}">${escapeHtml(Array.isArray(value) ? value.join('\n') : value)}</textarea>`
      : `<input class="onboarding-input" id="onboardingInput" type="${question.type || 'text'}" value="${escapeAttr(value)}" placeholder="${escapeAttr(question.placeholder || '')}" ${question.min !== undefined ? `min="${question.min}"` : ''} ${question.max !== undefined ? `max="${question.max}"` : ''} ${question.step ? `step="${question.step}"` : ''}>`;
  const bmi = question.key === 'weight_kg' && state.onboardingAnswers.height_cm && value
    ? `<div class="bmi-preview">Расчётный ИМТ: ${(Number(value) / ((Number(state.onboardingAnswers.height_cm) / 100) ** 2)).toFixed(1)}. Это ориентир, не диагноз.</div>` : '';
  $('#onboardingContent').innerHTML = `<span class="onboarding-kicker">Шаг ${state.onboardingStep + 1} из ${questions.length}</span><h1>${question.title}</h1><p class="onboarding-lead">${question.lead}</p>${control}${bmi}<div class="onboarding-actions">${state.onboardingStep ? '<button type="button" class="onboarding-back" data-onboarding-action="back">Назад</button>' : ''}<button type="button" class="onboarding-next" data-onboarding-action="next">${onboardingNextLabel(question, value, state.onboardingStep === questions.length - 1)}</button></div>`;
  $('#onboardingInput')?.focus();
}

function captureQuestionAnswer() {
  const question = activeOnboardingQuestions()[state.onboardingStep];
  if (!question.choices) {
    let value = $('#onboardingInput').value.trim();
    if (question.list) value = value.split('\n').map(item => item.trim()).filter(Boolean);
    if (!question.optional && (value === '' || (Array.isArray(value) && !value.length))) throw new Error('Ответьте на вопрос, чтобы продолжить');
    if (question.type === 'number' && value !== '') {
      const number = Number(value);
      if (!Number.isFinite(number) || number < question.min || number > question.max) throw new Error(`Введите значение от ${question.min} до ${question.max}`);
      value = number;
    }
    state.onboardingAnswers[question.key] = value;
  } else if (!state.onboardingAnswers[question.key]) {
    throw new Error('Выберите один из вариантов');
  }
}

async function nextQuestion() {
  try {
    captureQuestionAnswer();
    const questions = activeOnboardingQuestions();
    if (state.onboardingStep < questions.length - 1) {
      state.onboardingStep += 1;
      renderQuestion();
      return;
    }
    const payload = {
      preferred_name:'', age:'', sex:'', height_cm:'', weight_kg:'', pregnancy:'not_applicable',
      conditions:[], medications:[], allergies:[], smoking:'unknown', alcohol:'unknown', activity:'unknown',
      blood_pressure:'unknown', blood_sugar:'unknown', dark_in_eyes:'unknown', joint_pain:'unknown', fatigue:'unknown', notes:'',
      ...state.onboardingAnswers,
    };
    state.onboarding = await api('/api/onboarding/profile', { method:'POST', body:JSON.stringify(payload) });
    state.profile = state.onboarding.profile;
    renderExamOffer();
  } catch (error) { showOnboardingError(error.message); }
}

function renderExamOffer() {
  setOnboardingMeta('Обследования', 72);
  $('#onboardingContent').innerHTML = `<span class="onboarding-kicker">Необязательный шаг</span><h1>Хотите дополнить картину обследованиями?</h1><p class="onboarding-lead">Мы можем показать наборы из проекта медосмотров с учётом ответов анкеты. Это предварительная подборка: необходимость анализов и их интерпретацию стоит обсудить с врачом.</p><div class="exam-intro"><span>◎</span><div><strong>Можно отказаться</strong><br>Чат с консилиумом откроется в любом случае.</div></div><div class="onboarding-actions"><button type="button" class="onboarding-back" data-onboarding-action="question-back">Изменить анкету</button><button type="button" class="onboarding-next" data-onboarding-action="start-exams">Посмотреть варианты</button></div><button type="button" class="exam-skip" data-onboarding-action="skip-exams">Пропустить и открыть Консилиум</button>`;
}

function renderExamSelection() {
  setOnboardingMeta('Обследования', 80);
  const recommended = new Set(state.onboarding.recommended_test_ids || []);
  const cards = state.onboarding.tests.map(test => `<label class="exam-card ${state.selectedTests.has(test.id) ? 'selected' : ''}" data-test-card="${test.id}"><input type="checkbox" ${state.selectedTests.has(test.id) ? 'checked' : ''}><span class="exam-check">✓</span>${recommended.has(test.id) ? '<small class="recommended-badge">Подходит по анкете</small>' : ''}<strong>${test.name}</strong><b>${test.price.toLocaleString('ru')} ₽</b><small>${test.description}</small><em>${test.includes}</em></label>`).join('');
  const total = state.onboarding.tests.filter(test => state.selectedTests.has(test.id)).reduce((sum,test) => sum + test.price, 0);
  $('#onboardingContent').innerHTML = `<span class="onboarding-kicker">Выбор анализов</span><h1>Выберите интересующие наборы</h1><p class="onboarding-lead">Рекомендации отмечены по ответам анкеты и не являются назначением.</p><div class="exam-list">${cards}</div><div class="exam-total"><span>Выбрано: ${state.selectedTests.size}</span><strong>${total.toLocaleString('ru')} ₽</strong></div><div class="onboarding-actions"><button type="button" class="onboarding-back" data-onboarding-action="exam-offer">Назад</button><button type="button" class="onboarding-next" data-onboarding-action="continue-payment" ${state.selectedTests.size ? '' : 'disabled'}>К демо-оплате</button></div><button type="button" class="exam-skip" data-onboarding-action="skip-exams">Ничего не выбирать</button>`;
}

async function submitExamSelection(skip = false) {
  try {
    state.onboarding = await api('/api/onboarding/exams', { method:'POST', body:JSON.stringify({ selected_tests:skip ? [] : [...state.selectedTests] }) });
    if (skip) return openMainApp();
    renderPayment();
  } catch (error) { showOnboardingError(error.message); }
}

function selectedTestDetails() {
  return state.onboarding.tests.filter(test => state.onboarding.selected_tests.includes(test.id));
}

function renderPayment() {
  setOnboardingMeta('Демо-оплата', 92);
  const selected = selectedTestDetails();
  const total = selected.reduce((sum,test) => sum + test.price, 0);
  $('#onboardingContent').innerHTML = `<span class="onboarding-kicker">Последний шаг</span><h1>Проверим заказ</h1><p class="onboarding-lead">Это демонстрационный экран. Он не запрашивает платёжные данные и ничего не списывает.</p><div class="payment-stub"><span class="demo-badge">ДЕМО · БЕЗ СПИСАНИЯ</span><ul class="payment-lines">${selected.map(test => `<li><span>${test.name}</span><strong>${test.price.toLocaleString('ru')} ₽</strong></li>`).join('')}</ul><div class="payment-total"><span>Итого</span><strong>${total.toLocaleString('ru')} ₽</strong></div></div><div class="onboarding-actions"><button type="button" class="onboarding-back" data-onboarding-action="back-to-exams">Назад</button><button type="button" class="onboarding-next" data-onboarding-action="pay-demo">Имитировать оплату</button></div>`;
}

async function demoPayment() {
  try {
    state.onboarding = await api('/api/onboarding/payment', { method:'POST', body:'{}' });
    setOnboardingMeta('Готово', 100);
    $('#onboardingContent').innerHTML = `<div class="success-mark">✓</div><h1>Всё готово</h1><p class="onboarding-lead">Анкета сохранена, выбранные обследования добавлены в демо-заказ. Теперь специалисты будут учитывать ваши данные в подходящих вопросах.</p><div class="onboarding-actions"><button type="button" class="onboarding-next" data-onboarding-action="open-app">Открыть Консилиум</button></div>`;
  } catch (error) { showOnboardingError(error.message); }
}

async function loadOnboarding() {
  state.onboarding = await api('/api/onboarding');
  applyFontSize(state.onboarding.font_size || 'standard');
  state.profile = state.onboarding.profile;
  seedOnboardingAnswers(state.profile);
  state.selectedTests = new Set(state.onboarding.selected_tests || []);
  if (state.onboarding.status === 'complete') return openMainApp();
  $('#onboarding').classList.remove('hidden');
  $('#appShell').classList.add('hidden');
  if (state.onboarding.status === 'appearance') renderAppearance();
  else if (state.onboarding.status === 'payment') renderPayment();
  else if (state.onboarding.status === 'exams') renderExamOffer();
  else renderQuestion();
}

async function openMainApp() {
  $('#onboarding').classList.add('hidden');
  $('#appShell').classList.remove('hidden');
  if (!state.mainInitialized) await initMainApp();
  if (!state.onboarding?.intro_seen) requestAnimationFrame(openCapabilities);
}

$('#onboardingContent').addEventListener('click', event => {
  const choice = event.target.closest('[data-choice]');
  if (choice) {
    const question = activeOnboardingQuestions()[state.onboardingStep];
    state.onboardingAnswers[question.key] = choice.dataset.choice;
    renderQuestion();
    return;
  }
  const appearance = event.target.closest('.appearance-choice[data-size]');
  if (appearance) {
    applyFontSize(appearance.dataset.size);
    renderAppearance();
    return;
  }
  const card = event.target.closest('[data-test-card]');
  if (card) {
    const id = card.dataset.testCard;
    state.selectedTests.has(id) ? state.selectedTests.delete(id) : state.selectedTests.add(id);
    renderExamSelection();
    return;
  }
  const action = event.target.closest('[data-onboarding-action]')?.dataset.onboardingAction;
  if (!action) return;
  if (action === 'next') nextQuestion();
  else if (action === 'back') { try { captureQuestionAnswer(); } catch {} state.onboardingStep -= 1; renderQuestion(); }
  else if (action === 'question-back') { state.onboardingStep = activeOnboardingQuestions().length - 1; renderQuestion(); }
  else if (action === 'start-exams') renderExamSelection();
  else if (action === 'exam-offer') renderExamOffer();
  else if (action === 'skip-exams') submitExamSelection(true);
  else if (action === 'continue-payment') submitExamSelection(false);
  else if (action === 'back-to-exams') renderExamSelection();
  else if (action === 'pay-demo') demoPayment();
  else if (action === 'open-app') openMainApp();
  else if (action === 'save-appearance') saveAppearance();
});

$('#onboardingContent').addEventListener('input', event => {
  if (event.target.id !== 'onboardingInput') return;
  const questions = activeOnboardingQuestions();
  const question = questions[state.onboardingStep];
  const nextButton = $('#onboardingContent [data-onboarding-action="next"]');
  if (nextButton) nextButton.textContent = onboardingNextLabel(
    question, event.target.value, state.onboardingStep === questions.length - 1,
  );
});
$('#onboardingContent').addEventListener('focusin', event => {
  if (!event.target.matches('.onboarding-input-area') || !window.matchMedia('(max-width: 720px)').matches) return;
  setTimeout(() => {
    $('#onboardingContent .onboarding-actions')?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }, 180);
});

function renderAgentList() {
  const groups = [['coordination', 'Помощь'], ['medical', 'Медицина'], ['general', 'Здоровье и образ жизни']];
  $('#agentList').innerHTML = groups.map(([group, title]) => `
    <div class="agent-group-label">${title}</div>
    ${Object.values(AGENTS).filter(a => a.group === group).map(agent => `
      <div class="agent-row ${agent.id === state.active ? 'active' : ''}">
        <span class="agent-icon">${agent.icon}</span>
        <span><strong>${agent.name}</strong><small>${agent.role}</small></span>
        <i class="status-dot"></i>
      </div>`).join('')}
  `).join('');
}

function setActiveAgent(id) {
  if (!AGENTS[id]) id = 'manager';
  state.active = id;
  const agent = AGENTS[id];
  $('#headerAvatar').textContent = agent.initials;
  $('#headerName').textContent = agent.name;
  $('#headerRole').textContent = agent.role;
  renderAgentList();
}

function addMessage(sender, text, agentId = state.active, urgent = false, createdAt = null, metadata = {}) {
  const agent = AGENTS[agentId] || AGENTS.manager;
  const humanManager = sender === 'agent' && metadata.sender_type === 'human_manager';
  const wrapper = document.createElement('div');
  wrapper.className = `message-row ${sender}${urgent ? ' urgent' : ''}${humanManager ? ' human-manager' : ''}`;
  if (metadata._message_id) wrapper.dataset.messageId = String(metadata._message_id);
  const date = createdAt ? new Date(createdAt) : new Date();
  const time = new Intl.DateTimeFormat('ru', { hour: '2-digit', minute: '2-digit' }).format(date);
  const attachmentBadges = (metadata.attachments || []).map(item => `<em class="message-file">▱ ${escapeHtml(item.name)}</em>`).join('');
  const special = metadata.action === 'second_opinion' ? '<b class="special-label">Второе мнение</b>' : '';
  const cached = metadata.action === 'lab_interpretation' && metadata.interpretation_cached
    ? '<b class="special-label">Сохранённая расшифровка</b>' : '';
  const labDocuments = sender === 'agent'
    ? labDocumentsMarkup(metadata.lab_result_documents || [], 'message') : '';
  wrapper.innerHTML = sender === 'user'
    ? `<div class="bubble user-bubble">${attachmentBadges}<p>${escapeHtml(text)}</p><span>${time}</span></div>`
    : `<div class="message-avatar">${humanManager ? 'Ч' : agent.initials}</div><div><div class="message-author"><strong>${humanManager ? escapeHtml(metadata.manager_name || 'Менеджер') : agent.name}</strong><span>${humanManager ? 'Человек' : agent.role}</span>${special}${cached}</div><div class="bubble agent-bubble"><p>${formatAssistantText(text)}</p>${labDocuments}<span>${time}</span></div></div>`;
  messages.appendChild(wrapper);
  scrollChatToBottom();
}

function updateChatMode(aiEnabled = true, humanStatus = 'none', humanTicketId = null) {
  state.aiEnabled = Boolean(aiEnabled);
  state.humanStatus = humanStatus || 'none';
  const banner = $('#chatModeBanner');
  const relevant = !state.aiEnabled || ['pending', 'connected'].includes(state.humanStatus);
  banner.classList.toggle('hidden', !relevant);
  banner.classList.toggle('ai-paused', !state.aiEnabled);
  input.placeholder = state.aiEnabled
    ? 'Задайте вопрос о здоровье...'
    : 'Напишите менеджеру...';
  if (!relevant) return;
  $('#chatModeIcon').textContent = state.aiEnabled ? '✓' : '♙';
  $('#chatModeTitle').textContent = state.aiEnabled ? 'Обращение менеджеру открыто' : 'С вами общается менеджер';
  $('#chatModeText').textContent = state.aiEnabled
    ? `ИИ продолжает отвечать${humanTicketId ? ` · обращение ${humanTicketId}` : ''}.`
    : 'ИИ приостановлен. Ваше сообщение сохранится и будет ждать ответа человека.';
}

function addCouncilResult(result, createdAt = null) {
  const wrapper = document.createElement('div');
  wrapper.className = 'council-result';
  wrapper.innerHTML = `<div class="council-title"><span>◎</span><div><strong>Консилиум завершён</strong><small>${result.agents.length} специалиста дали разные профильные оценки</small></div></div><div class="opinion-grid">${result.opinions.map(opinion => { const agent = AGENTS[opinion.agent] || AGENTS.manager; return `<article><div><i>${agent.initials}</i><span><strong>${agent.name}</strong><small>${agent.role}</small></span></div>${opinion.focus ? `<em class="council-focus">${escapeHtml(opinion.focus)}</em>` : ''}<p>${escapeHtml(opinion.message)}</p></article>`; }).join('')}</div><div class="council-synthesis"><strong>Общий вывод Марии</strong><p>${formatAssistantText(result.message.content)}</p></div>`;
  messages.appendChild(wrapper);
  scrollChatToBottom();
}

function showTyping(agentId = 'manager') {
  $('#typing')?.remove();
  const el = document.createElement('div');
  el.className = 'message-row agent typing-row';
  el.id = 'typing';
  el.innerHTML = `<div class="message-avatar">${AGENTS[agentId].initials}</div><div class="typing"><i></i><i></i><i></i></div>`;
  messages.appendChild(el);
  scrollChatToBottom();
}

function resetTimeline() {
  timeline.innerHTML = `<div class="empty-state"><div class="empty-icon">⌁</div><strong>Здесь появятся следующие шаги</strong><p>Покажем, кто из специалистов помогает и что происходит дальше.</p></div>`;
}

function addTimeline(agentId, title, detail, status = 'done') {
  if (timeline.querySelector('.empty-state')) timeline.innerHTML = '';
  const agent = AGENTS[agentId] || AGENTS.manager;
  const item = document.createElement('div');
  item.className = `timeline-item ${status}`;
  item.innerHTML = `<div class="timeline-marker">${status === 'active' ? '<i></i>' : '✓'}</div><div><strong>${escapeHtml(title)}</strong><p>${escapeHtml(detail)}</p><span>${agent.name} · ${agent.role}</span></div>`;
  timeline.appendChild(item);
}

function showHandoff(fromId, toId) {
  if (!fromId || fromId === toId || !AGENTS[fromId] || !AGENTS[toId]) return;
  const banner = $('#handoffBanner');
  $('#handoffFrom').textContent = AGENTS[fromId].initials;
  $('#handoffTo').textContent = AGENTS[toId].initials;
  $('#handoffText').textContent = `${AGENTS[fromId].name} передаёт диалог: ${AGENTS[toId].role.toLowerCase()}`;
  banner.classList.remove('hidden');
  setTimeout(() => banner.classList.add('hidden'), 3800);
}

async function processMessage(text) {
  if (state.processing) return;
  state.processing = true;
  $('#taskStatus').textContent = 'Подбираю подходящего специалиста';
  $('#suggestions').classList.add('hidden');
  const outgoingAttachments = [...state.attachments];
  addMessage('user', text || 'Прикреплён файл для анализа', state.active, false, null, { attachments: outgoingAttachments });
  clearAttachments();
  addTimeline('manager', 'Изучаю вопрос', 'Учитываю контекст и выбираю, кто лучше поможет');
  showTyping('manager');

  try {
    const result = await api('/api/chat', {
      method: 'POST',
      body: JSON.stringify({ conversation_id: state.conversationId, message: text, attachments: outgoingAttachments }),
    });
    $('#typing')?.remove();
    state.conversationId = result.conversation_id;
    localStorage.setItem('consilium_conversation_id', state.conversationId);
    state.lastMessageId = Math.max(
      state.lastMessageId,
      Number(result.user_message?.id || 0),
      Number(result.assistant_message?.id || 0),
    );
    updateChatMode(result.ai_enabled !== false, result.human_status, result.human_ticket_id);
    if (result.assistant_message) {
      showHandoff(result.handoff_from, result.agent);
      if (result.handoff_from) addTimeline(result.handoff_from, 'Подключён специалист', result.handoff_reason);
      setActiveAgent(result.agent);
      addTimeline(result.agent, result.emergency ? 'Срочная оценка' : 'Специалист ответил', result.handoff_reason, 'active');
      addMessage(
        'agent', result.assistant_message.content, result.agent, result.emergency,
        result.assistant_message.created_at,
        { ...(result.assistant_message.metadata || {}), _message_id:result.assistant_message.id },
      );
      playUserMessageSound();
    }
    state.context = result.context || state.context;
    state.urgency = result.urgency || 'routine';
    renderInsights();
    if (result.action !== 'human') toggleAdvancedActions(Boolean(result.council_available));

    if (result.action === 'waiting_human') {
      $('#taskStatus').textContent = 'Сообщение ожидает ответа менеджера';
      addTimeline('manager', 'Сообщение передано', 'Менеджер увидит его в своей очереди', 'active');
    } else if (result.action === 'lab_results_prompt') {
      await openLabResults();
      $('#taskStatus').textContent = 'Укажите номер пробирки';
    } else if (result.human_escalation) {
      await openHumanModal(result.human_ticket_id);
      $('#taskStatus').textContent = 'Выберите формат связи · ИИ на связи';
    } else if (result.human_channel_prompt === 'call') {
      await openHumanModal(result.human_ticket_id);
      showCallPhoneStep();
      $('#taskStatus').textContent = 'Укажите номер для созвона';
    } else {
      $('#taskStatus').textContent = result.emergency ? 'Требуется срочное действие' : 'Контекст сохранён';
    }
    await loadConversationList();
  } catch (error) {
    $('#typing')?.remove();
    addSystemError(error.message);
    $('#taskStatus').textContent = 'Ошибка подключения';
  } finally {
    state.processing = false;
    focusChatInput();
  }
}

function addSystemError(text) {
  const el = document.createElement('div');
  el.className = 'system-error';
  el.innerHTML = `<strong>Не удалось получить ответ</strong><p>${escapeHtml(text)}</p>`;
  messages.appendChild(el);
  scrollChatToBottom();
}

function toggleAdvancedActions(show) {
  $('#secondOpinionButton').classList.toggle('hidden', !show);
  $('#councilButton').classList.toggle('hidden', !show);
  document.body.classList.toggle('advanced-actions-visible', show);
}

function renderInsights() {
  const context = state.context;
  const visible = context && (context.current_topic || context.user_goal || context.known_facts?.length);
  const riskBar = $('#headerRiskBar');
  const chatHeader = document.querySelector('.chat-header');
  riskBar.classList.toggle('hidden', !visible);
  chatHeader.classList.toggle('has-risk', Boolean(visible));
  if (!visible) return;
  const urgencyMap = { routine: [8, 'Плановая оценка'], soon: [38, 'Стоит заняться в ближайшее время'], urgent: [70, 'Нужна срочная оценка'], emergency: [94, 'Немедленное действие'] };
  const [position, label] = urgencyMap[state.urgency] || urgencyMap.routine;
  riskBar.dataset.urgency = state.urgency || 'routine';
  $('#urgencyMarker').style.left = `${position}%`;
  $('#urgencyLabel').textContent = label;
  $('#stateTopic').textContent = context.current_topic || 'Тема уточняется';
  const count = context.open_questions?.length || 0;
  $('#stateQuestions').textContent = count ? `Осталось вопросов: ${count}` : 'Вопросы собраны';
}

async function loadConversationList() {
  try {
    const items = await api('/api/conversations');
    $('#conversationList').innerHTML = items.length ? items.slice(0, 8).map(item => `
      <button class="conversation-row ${item.id === state.conversationId ? 'active' : ''}" data-id="${item.id}">
        <strong>${escapeHtml(item.title)}</strong><small>${formatRelative(item.updated_at)}</small>
      </button>`).join('') : '<p class="no-conversations">Пока нет сохранённых диалогов</p>';
  } catch { $('#conversationList').innerHTML = ''; }
}

async function syncConversationUpdates() {
  if (
    !state.conversationId || state.processing || !state.mainInitialized
  ) return;
  try {
    const data = await api(
      `/api/conversations/${state.conversationId}/updates?after_id=${state.lastMessageId}`,
    );
    updateChatMode(
      data.ai_enabled, data.human_status, data.human_ticket_id,
    );
    let incomingMessageReceived = false;
    for (const message of data.messages || []) {
      state.lastMessageId = Math.max(state.lastMessageId, Number(message.id || 0));
      const exists = messages.querySelector(`[data-message-id="${message.id}"]`);
      if (exists) continue;
      addMessage(
        message.role === 'user' ? 'user' : 'agent',
        message.content,
        message.agent_id || 'manager',
        Boolean(message.metadata?.emergency),
        message.created_at,
        { ...(message.metadata || {}), _message_id:message.id },
      );
      if (message.role !== 'user') incomingMessageReceived = true;
      if (message.metadata?.sender_type === 'human_manager') {
        $('#taskStatus').textContent = 'Менеджер ответил';
      }
    }
    if (incomingMessageReceived) playUserMessageSound();
  } catch (error) {
    if (error.message === 'Диалог не найден') {
      state.conversationId = null;
      localStorage.removeItem('consilium_conversation_id');
    }
  }
}

async function openConversation(id) {
  if (state.processing) return;
  try {
    const data = await api(`/api/conversations/${id}`);
    state.conversationId = id;
    localStorage.setItem('consilium_conversation_id', id);
    messages.innerHTML = '';
    resetTimeline();
    state.lastMessageId = 0;
    data.messages.forEach(message => {
      state.lastMessageId = Math.max(state.lastMessageId, Number(message.id || 0));
      if (message.metadata?.action === 'council' && message.metadata?.opinions) {
        addCouncilResult({ agents: message.metadata.agents || [], opinions: message.metadata.opinions, message }, message.created_at);
      } else {
        addMessage(
          message.role === 'user' ? 'user' : 'agent', message.content,
          message.agent_id || 'manager', Boolean(message.metadata?.emergency), message.created_at,
          { ...(message.metadata || {}), _message_id:message.id },
        );
      }
    });
    data.handoffs.forEach(h => addTimeline(h.to_agent, 'Передача управления', h.reason));
    setActiveAgent(data.active_agent);
    try { state.context = JSON.parse(data.context_summary || '{}'); } catch { state.context = null; }
    const medicalAgents = ['therapist','cardiologist','neurologist','dermatologist','pediatrician','psychologist'];
    const lastAssistant = [...data.messages].reverse().find(item => item.role === 'assistant' && item.metadata?.urgency && medicalAgents.includes(item.agent_id));
    state.urgency = lastAssistant?.metadata?.urgency || 'routine';
    updateChatMode(
      data.ai_enabled === undefined ? true : Boolean(data.ai_enabled),
      data.human_status, data.human_ticket_id,
    );
    renderInsights();
    toggleAdvancedActions(medicalAgents.includes(data.active_agent) || data.messages.some(item => medicalAgents.includes(item.agent_id)));
    $('#suggestions').classList.add('hidden');
    $('#taskStatus').textContent = 'Диалог загружен';
    await loadConversationList();
    document.body.classList.remove('show-team');
  } catch (error) {
    if (id === state.conversationId) {
      state.conversationId = null;
      localStorage.removeItem('consilium_conversation_id');
      newConversation();
    } else {
      addSystemError(error.message);
    }
  }
}

function newConversation() {
  state.conversationId = null;
  localStorage.removeItem('consilium_conversation_id');
  messages.innerHTML = '';
  resetTimeline();
  state.context = null;
  state.urgency = 'routine';
  state.lastMessageId = 0;
  updateChatMode(true, 'none', null);
  clearAttachments();
  renderInsights();
  toggleAdvancedActions(false);
  setActiveAgent('manager');
  $('#suggestions').classList.remove('hidden');
  $('#taskStatus').textContent = 'Ожидает задачу';
  addMessage('agent', 'Здравствуйте! Я Мария, ваш ИИ-менеджер. Задавайте вопросы о здоровье, питании, спорте или возможностях сервиса — я помогу разобраться и при необходимости подключу подходящего специалиста.', 'manager');
  loadConversationList();
}

async function checkHealth() {
  try {
    const health = await api('/api/health');
    $('#aiStatus').textContent = health.ai_configured ? 'AI подключён' : 'Нужен API-ключ';
    $('#aiModels').textContent = health.ai_configured ? `${health.orchestrator_model} → ${health.specialist_model}` : 'Укажите OPENAI_API_KEY';
    document.querySelector('.pulse-dot').classList.toggle('configured', health.ai_configured);
  } catch {
    $('#aiStatus').textContent = 'Python-сервер не запущен';
    $('#aiModels').textContent = 'Запустите python run.py';
  }
}

async function openHumanModal(ticketId = null) {
  $('#ticketNumber').textContent = ticketId || `H-${String(Date.now()).slice(-6)}`;
  setHumanChoiceDisabled(false);
  resetCallPhoneStep();
  $('#handoffPreview').innerHTML = '<span>Подготавливаю сводку…</span>';
  $('#humanModal').classList.remove('hidden');
  if (state.conversationId) {
    try {
      const preview = await api(`/api/handoff-preview/${state.conversationId}`);
      const facts = preview.facts?.length ? `<ul>${preview.facts.slice(0, 5).map(item => `<li>${escapeHtml(item)}</li>`).join('')}</ul>` : '<span>Подтверждённые факты пока не собраны.</span>';
      $('#handoffPreview').innerHTML = `<strong>${escapeHtml(preview.goal || preview.topic || 'Цель обращения')}</strong>${facts}${preview.open_questions?.length ? `<span>Открытых вопросов: ${preview.open_questions.length}</span>` : ''}<button class="preview-edit" id="editHandoffContext">Исправить сведения</button>`;
    } catch { $('#handoffPreview').innerHTML = '<span>Контекст диалога будет передан вместе с обращением.</span>'; }
  }
}
function closeHumanModal() {
  $('#humanModal').classList.add('hidden');
  resetCallPhoneStep();
}
function resumeAfterHuman() {
  closeHumanModal();
  $('#taskStatus').textContent = 'Обращение подготовлено · ИИ на связи';
  input.placeholder = 'Продолжите диалог — контекст сохранён...';
  focusChatInput();
}
function setHumanChoiceDisabled(disabled) {
  $('#humanChatButton').disabled = disabled;
  $('#humanCallButton').disabled = disabled;
  $('#confirmCallButton').disabled = disabled || !normalizeRussianPhone($('#callPhoneInput').value);
}

function normalizeRussianPhone(value) {
  let digits = String(value || '').replace(/\D/g, '');
  if (digits.length === 10) digits = `7${digits}`;
  else if (digits.length === 11 && digits.startsWith('8')) digits = `7${digits.slice(1)}`;
  return /^7[3489]\d{9}$/.test(digits) ? `+${digits}` : null;
}

function resetCallPhoneStep() {
  document.querySelector('.human-options').classList.remove('hidden');
  $('#callPhoneStep').classList.add('hidden');
  $('#callPhoneInput').value = '';
  $('#callPhoneError').classList.add('hidden');
  $('#confirmCallButton').disabled = true;
}

function showCallPhoneStep() {
  document.querySelector('.human-options').classList.add('hidden');
  $('#callPhoneStep').classList.remove('hidden');
  $('#callPhoneInput').focus();
}

function validateCallPhone(showError = false) {
  const normalized = normalizeRussianPhone($('#callPhoneInput').value);
  const hasValue = Boolean($('#callPhoneInput').value.trim());
  $('#confirmCallButton').disabled = !normalized || state.processing;
  $('#callPhoneError').classList.toggle('hidden', !showError && !hasValue || Boolean(normalized));
  return normalized;
}

async function chooseHumanChannel(channel, phone = null) {
  if (!state.conversationId || state.processing) return;
  state.processing = true;
  setHumanChoiceDisabled(true);
  try {
    const result = await api('/api/human-preference', {
      method: 'POST',
      body: JSON.stringify({ conversation_id: state.conversationId, channel, phone }),
    });
    closeHumanModal();
    setActiveAgent('manager');
    addMessage('agent', result.assistant_message.content, 'manager', false, result.assistant_message.created_at);
    $('#taskStatus').textContent = channel === 'chat'
      ? 'Чат со специалистом запрошен · ИИ на связи'
      : 'Созвон выбран · ИИ на связи';
    input.placeholder = 'Продолжите диалог — контекст сохранён...';
    await loadConversationList();
  } catch (error) {
    addSystemError(error.message);
    setHumanChoiceDisabled(false);
    if (channel === 'call') {
      $('#callPhoneStep').classList.remove('hidden');
      document.querySelector('.human-options').classList.add('hidden');
      validateCallPhone(true);
    }
  } finally {
    state.processing = false;
    focusChatInput();
  }
}

async function requestSecondOpinion() {
  if (!state.conversationId || state.processing) return;
  state.processing = true;
  $('#taskStatus').textContent = 'Независимый специалист изучает контекст';
  addTimeline('manager', 'Запрошено второе мнение', 'Другой специалист оценивает ситуацию независимо', 'active');
  showTyping('manager');
  try {
    const result = await api('/api/second-opinion', { method: 'POST', body: JSON.stringify({ conversation_id: state.conversationId }) });
    $('#typing')?.remove();
    setActiveAgent(result.agent);
    addMessage('agent', result.message.content, result.agent, result.urgency === 'emergency', result.message.created_at, { action: 'second_opinion' });
    addTimeline(result.agent, 'Второе мнение готово', 'Независимая оценка добавлена в диалог');
    $('#taskStatus').textContent = 'Второе мнение получено';
  } catch (error) { $('#typing')?.remove(); addSystemError(error.message); }
  finally { state.processing = false; }
}

function councilAgentIds() {
  const mapping = { therapist:['neurologist','cardiologist'], neurologist:['therapist','cardiologist'], cardiologist:['therapist','neurologist'], dermatologist:['therapist','pediatrician'], pediatrician:['therapist','neurologist'], psychologist:['therapist','neurologist'] };
  return [state.active, ...(mapping[state.active] || ['therapist','neurologist'])]
    .filter((agent,index,list) => AGENTS[agent] && !['manager','safety','general'].includes(agent) && list.indexOf(agent) === index)
    .slice(0,3);
}

function openCouncilModal() {
  const agents = councilAgentIds();
  $('#councilMemberList').innerHTML = agents.map(id => { const agent=AGENTS[id]; return `<span class="council-member"><i>${agent.initials}</i>${agent.name} · ${agent.role}</span>`; }).join('');
  $('#councilModal').classList.remove('hidden');
}

function closeCouncilModal() { $('#councilModal').classList.add('hidden'); }

function closeFunctionMenu() {
  $('#functionMenu').classList.add('hidden');
  $('#functionMenuButton').setAttribute('aria-expanded', 'false');
}

function toggleFunctionMenu() {
  const willOpen = $('#functionMenu').classList.contains('hidden');
  $('#functionMenu').classList.toggle('hidden', !willOpen);
  $('#functionMenuButton').setAttribute('aria-expanded', String(willOpen));
}

function openCapabilities() {
  closeFunctionMenu();
  $('#capabilitiesModal').classList.remove('hidden');
}

function renderFontSizeModal() {
  $('#fontSizeOptions').innerHTML = fontSizeChoices('font-size-choice');
}

function openFontSizeModal() {
  closeFunctionMenu();
  renderFontSizeModal();
  $('#fontSizeModal').classList.remove('hidden');
}

function closeFontSizeModal() { $('#fontSizeModal').classList.add('hidden'); }

async function updateFontSize(size) {
  applyFontSize(size);
  renderFontSizeModal();
  try {
    state.onboarding = await api('/api/onboarding/appearance', { method:'POST', body:JSON.stringify({ font_size:size }) });
  } catch (error) { addSystemError(error.message); }
}

async function closeCapabilities() {
  $('#capabilitiesModal').classList.add('hidden');
  if (state.onboarding?.status !== 'complete' || state.onboarding.intro_seen) return;
  try {
    state.onboarding = await api('/api/onboarding/intro-seen', { method:'POST', body:'{}' });
  } catch (error) { console.warn('Не удалось сохранить просмотр возможностей', error); }
}

async function requestCouncil() {
  if (!state.conversationId || state.processing) return;
  closeCouncilModal();
  state.processing = true;
  $('#taskStatus').textContent = 'Специалисты собирают консилиум';
  const likelyAgents = councilAgentIds();
  likelyAgents.forEach((agent, index) => addTimeline(agent, index ? 'Сверяет позицию' : 'Ведущий специалист', 'Изучает общий контекст', 'active'));
  showTyping('manager');
  try {
    const result = await api('/api/council', { method: 'POST', body: JSON.stringify({ conversation_id: state.conversationId }) });
    $('#typing')?.remove();
    addCouncilResult(result);
    playUserMessageSound();
    setActiveAgent('manager');
    result.agents.forEach(agent => addTimeline(agent, 'Мнение учтено', 'Консилиум завершён'));
    $('#taskStatus').textContent = 'Общий вывод консилиума готов';
  } catch (error) { $('#typing')?.remove(); addSystemError(error.message); }
  finally { state.processing = false; }
}

function openContextEditor() {
  if (!state.context) return;
  $('#contextGoal').value = state.context.user_goal || '';
  $('#contextFacts').value = (state.context.known_facts || []).join('\n');
  $('#contextQuestions').value = (state.context.open_questions || []).join('\n');
  $('#contextModal').classList.remove('hidden');
}

async function saveContext() {
  if (!state.conversationId || !state.context) return;
  const updated = { ...state.context,
    user_goal: $('#contextGoal').value.trim(),
    known_facts: $('#contextFacts').value.split('\n').map(x => x.trim()).filter(Boolean),
    open_questions: $('#contextQuestions').value.split('\n').map(x => x.trim()).filter(Boolean),
  };
  try {
    const result = await api('/api/context', { method: 'POST', body: JSON.stringify({ conversation_id: state.conversationId, context: updated }) });
    state.context = result.context;
    renderInsights();
    $('#contextModal').classList.add('hidden');
    $('#taskStatus').textContent = 'Контекст подтверждён пользователем';
    if (state.returnToHumanAfterContextEdit) {
      const ticketId = state.contextEditTicketId;
      state.returnToHumanAfterContextEdit = false;
      state.contextEditTicketId = null;
      await openHumanModal(ticketId);
    }
  } catch (error) { addSystemError(error.message); }
}

function closeContextEditor() {
  $('#contextModal').classList.add('hidden');
  if (!state.returnToHumanAfterContextEdit) return;
  const ticketId = state.contextEditTicketId;
  state.returnToHumanAfterContextEdit = false;
  state.contextEditTicketId = null;
  openHumanModal(ticketId);
}

async function loadMemories() {
  try { state.memories = await api('/api/memories'); } catch { state.memories = []; }
  $('#memoryCount').textContent = state.memories.length;
  const labels = { preference:'Предпочтение', health:'Здоровье', constraint:'Ограничение', goal:'Цель' };
  $('#memoryList').innerHTML = state.memories.length ? state.memories.map(item => `<div class="memory-item"><small>${labels[item.category] || item.category}</small><span>${escapeHtml(item.content)}</span><button data-memory-delete="${item.id}" aria-label="Удалить">×</button></div>`).join('') : '<p class="no-conversations">Пока ничего не сохранено</p>';
}

async function loadProfile() {
  try { state.profile = await api('/api/profile'); }
  catch { state.profile = null; }
  renderProfileStatus();
}

function profileCompletion(profile = state.profile) {
  if (!profile) return 0;
  const checks = [profile.age, profile.sex, profile.height_cm, profile.weight_kg,
    profile.smoking && profile.smoking !== 'unknown', profile.alcohol && profile.alcohol !== 'unknown',
    profile.activity && profile.activity !== 'unknown', profile.blood_pressure && profile.blood_pressure !== 'unknown'];
  return Math.round(checks.filter(Boolean).length / checks.length * 100);
}

function renderProfileStatus() {
  const completion = profileCompletion();
  $('#profileCompletion').textContent = `${completion}%`;
  $('#profileStatus').textContent = completion ? `Заполнено на ${completion}%` : 'Не заполнена';
  $('#capabilityProfileStatus').textContent = completion ? `${completion}%` : 'Заполнить';
  const hasTubeNumber = Boolean(state.profile?.tube_number?.trim());
  $('#menuLabResultsStatus').textContent = hasTubeNumber ? 'Номер пробирки сохранён' : 'Нужно ввести номер пробирки';
  $('#capabilityLabResultsStatus').textContent = hasTubeNumber ? 'Проверить' : 'Ввести номер';
}

function openProfile() {
  const profile = state.profile || {};
  $('#profileChelId').value = profile.chel_id || '';
  $('#profileName').value = profile.preferred_name || '';
  $('#profileAge').value = profile.age ?? '';
  $('#profileSex').value = profile.sex || '';
  $('#profileHeight').value = profile.height_cm ?? '';
  $('#profileWeight').value = profile.weight_kg ?? '';
  $('#profilePregnancy').value = profile.pregnancy || 'not_applicable';
  $('#profileSmoking').value = profile.smoking || 'unknown';
  $('#profileAlcohol').value = profile.alcohol || 'unknown';
  $('#profileActivity').value = profile.activity || 'unknown';
  $('#profilePressure').value = profile.blood_pressure || 'unknown';
  $('#profileSugar').value = profile.blood_sugar || 'unknown';
  $('#profileFatigue').value = profile.fatigue || 'unknown';
  $('#profileJoints').value = profile.joint_pain || 'unknown';
  $('#profileConditions').value = (profile.conditions || []).join('\n');
  $('#profileMedications').value = (profile.medications || []).join('\n');
  $('#profileAllergies').value = (profile.allergies || []).join('\n');
  $('#profileTubeNumber').value = profile.tube_number || '';
  $('#profileNotes').value = profile.notes || '';
  updateProfileBmi();
  $('#profileModal').classList.remove('hidden');
}

function updateProfileBmi() {
  const height = Number($('#profileHeight').value);
  const weight = Number($('#profileWeight').value);
  $('#profileBmi').textContent = height > 0 && weight > 0
    ? `Расчётный ИМТ: ${(weight / ((height / 100) ** 2)).toFixed(1)}. Специалисты учтут его только вместе с остальными данными.`
    : 'ИМТ появится после заполнения роста и веса';
}

function profileLines(selector) {
  return $(selector).value.split('\n').map(item => item.trim()).filter(Boolean);
}

async function saveProfile() {
  const payload = {
    preferred_name: $('#profileName').value.trim(), age: $('#profileAge').value,
    sex: $('#profileSex').value, height_cm: $('#profileHeight').value,
    weight_kg: $('#profileWeight').value, pregnancy: $('#profilePregnancy').value,
    smoking: $('#profileSmoking').value, conditions: profileLines('#profileConditions'),
    alcohol: $('#profileAlcohol').value, activity: $('#profileActivity').value,
    blood_pressure: $('#profilePressure').value, blood_sugar: $('#profileSugar').value,
    fatigue: $('#profileFatigue').value, joint_pain: $('#profileJoints').value,
    dark_in_eyes: state.profile?.dark_in_eyes || 'unknown',
    medications: profileLines('#profileMedications'), allergies: profileLines('#profileAllergies'),
    tube_number: $('#profileTubeNumber').value.trim(),
    notes: $('#profileNotes').value.trim(),
  };
  try {
    state.profile = await api('/api/profile', { method:'POST', body:JSON.stringify(payload) });
    renderProfileStatus();
    $('#profileModal').classList.add('hidden');
    $('#taskStatus').textContent = 'Анкета сохранена · специалисты учтут её в ответах';
  } catch (error) { addSystemError(error.message); }
}

function profilePayloadWithTube(tubeNumber) {
  const profile = state.profile || {};
  return {
    preferred_name: profile.preferred_name || '',
    age: profile.age ?? '',
    sex: profile.sex || '',
    height_cm: profile.height_cm ?? '',
    weight_kg: profile.weight_kg ?? '',
    pregnancy: profile.pregnancy || 'not_applicable',
    smoking: profile.smoking || 'unknown',
    alcohol: profile.alcohol || 'unknown',
    activity: profile.activity || 'unknown',
    blood_pressure: profile.blood_pressure || 'unknown',
    blood_sugar: profile.blood_sugar || 'unknown',
    dark_in_eyes: profile.dark_in_eyes || 'unknown',
    joint_pain: profile.joint_pain || 'unknown',
    fatigue: profile.fatigue || 'unknown',
    conditions: profile.conditions || [],
    medications: profile.medications || [],
    allergies: profile.allergies || [],
    tube_number: tubeNumber,
    notes: profile.notes || '',
  };
}

function renderLabResults() {
  const tubeNumber = state.profile?.tube_number?.trim() || '';
  $('#labTubeStep').classList.toggle('hidden', Boolean(tubeNumber));
  $('#labResultsReady').classList.toggle('hidden', !tubeNumber);
  $('#labTubeError').classList.add('hidden');
  if (tubeNumber) $('#labSavedTube').textContent = tubeNumber;
  else $('#labTubeInput').value = '';
  $('#labResultDocuments').classList.add('hidden');
}

async function openLabResults() {
  closeFunctionMenu();
  if (!state.profile) await loadProfile();
  renderLabResults();
  $('#labResultsModal').classList.remove('hidden');
  if (!state.profile?.tube_number?.trim()) requestAnimationFrame(() => $('#labTubeInput').focus());
  else await fetchLabResults();
}

function closeLabResults() { $('#labResultsModal').classList.add('hidden'); }

function changeLabTube() {
  $('#labTubeInput').value = state.profile?.tube_number || '';
  $('#labResultsReady').classList.add('hidden');
  $('#labTubeStep').classList.remove('hidden');
  $('#labTubeError').classList.add('hidden');
  requestAnimationFrame(() => $('#labTubeInput').focus());
}

async function saveLabTube() {
  const tubeNumber = $('#labTubeInput').value.trim();
  if (!tubeNumber) {
    $('#labTubeError').classList.remove('hidden');
    $('#labTubeInput').focus();
    return;
  }
  const button = $('#saveLabTubeButton');
  button.disabled = true;
  button.textContent = 'Сохраняю…';
  try {
    state.profile = await api('/api/profile', {
      method:'POST',
      body:JSON.stringify(profilePayloadWithTube(tubeNumber)),
    });
    renderProfileStatus();
    renderLabResults();
    $('#taskStatus').textContent = 'Номер пробирки сохранён';
    await fetchLabResults();
  } catch (error) {
    $('#labTubeError').textContent = error.message;
    $('#labTubeError').classList.remove('hidden');
  } finally {
    button.disabled = false;
    button.textContent = 'Сохранить и продолжить';
  }
}

function setLabResultsState(icon, title, text) {
  $('#labResultsStateIcon').textContent = icon;
  $('#labResultsStateTitle').textContent = title;
  $('#labResultsStateText').textContent = text;
}

function normalizeLabDocuments(items = []) {
  return items.map((item, index) => typeof item === 'string'
    ? { id:`legacy-${index}`, index, title:`Результаты анализов${items.length > 1 ? ` · документ ${index + 1}` : ''}`, url:item }
    : item
  ).filter(item => item?.url && item?.id);
}

function labDocumentsMarkup(items, placement = 'modal') {
  const documents = normalizeLabDocuments(items);
  if (!documents.length) return '';
  const cards = documents.map((document, index) => `
    <article class="lab-document-card">
      <a href="${escapeAttr(document.url)}" target="_blank" rel="noopener noreferrer">
        <i>▤</i><span><strong>${escapeHtml(document.title || `Документ ${index + 1}`)}</strong><small>Открыть оригинал</small></span>
      </a>
      <button type="button" data-lab-interpret="${escapeAttr(document.id)}">Расшифровать</button>
    </article>`).join('');
  const allButton = documents.length > 1
    ? '<button class="lab-interpret-all" type="button" data-lab-interpret="all">Расшифровать все вместе</button>'
    : '';
  return `<div class="lab-document-list" data-placement="${placement}">${cards}${allButton}<small class="lab-ai-note">ИИ сопоставит показатели с вашей анкетой. Это не заменяет заключение врача.</small></div>`;
}

function renderLabResultDocuments(documents) {
  const container = $('#labResultDocuments');
  state.labDocuments = normalizeLabDocuments(documents);
  container.innerHTML = labDocumentsMarkup(state.labDocuments);
  container.classList.toggle('hidden', !state.labDocuments.length);
}

async function fetchLabResults() {
  if (!state.profile?.tube_number?.trim()) return;
  const button = $('#fetchLabResultsButton');
  button.disabled = true;
  button.textContent = 'Проверяю…';
  renderLabResultDocuments([]);
  setLabResultsState('⌕', 'Ищу результаты', 'Проверяю номер пробирки в базе лаборатории.');
  try {
    const result = await api('/api/lab-results', { method:'POST' });
    if (result.status === 'found' && result.urls?.length) {
      setLabResultsState('✓', 'Результаты готовы', 'Откройте оригинал или попросите ИИ расшифровать один документ либо весь набор.');
      renderLabResultDocuments(result.documents || result.urls);
      $('#taskStatus').textContent = 'Результаты анализов найдены';
    } else if (result.status === 'processing') {
      setLabResultsState('…', 'Результаты обрабатываются', 'Номер найден, но ссылка на документ пока не добавлена. Попробуйте позже.');
      $('#taskStatus').textContent = 'Результаты ещё обрабатываются';
    } else {
      setLabResultsState('!', 'Результаты пока не найдены', 'Проверьте номер пробирки или повторите поиск позже.');
      $('#taskStatus').textContent = 'Результаты не найдены';
    }
  } catch (error) {
    setLabResultsState('!', 'Не удалось выполнить поиск', error.message);
    $('#taskStatus').textContent = 'Ошибка получения результатов';
  } finally {
    button.disabled = false;
    button.textContent = 'Проверить ещё раз';
  }
}

async function interpretLabResults(documentId, sourceButton) {
  if (state.processing) return;
  state.processing = true;
  const buttons = document.querySelectorAll('[data-lab-interpret]');
  buttons.forEach(button => { button.disabled = true; });
  const originalText = sourceButton?.textContent;
  if (sourceButton) sourceButton.textContent = 'Расшифровываю…';
  $('#taskStatus').textContent = 'Терапевт анализирует результаты и анкету';
  try {
    const result = await api('/api/lab-results/interpret', {
      method:'POST',
      body:JSON.stringify({
        conversation_id:state.conversationId,
        document_id:documentId,
      }),
    });
    state.conversationId = result.conversation_id;
    localStorage.setItem('consilium_conversation_id', state.conversationId);
    closeLabResults();
    addMessage(
      'user',
      result.user_message.content,
      state.active,
      false,
      result.user_message.created_at,
      result.user_message.metadata || {},
    );
    showHandoff(result.handoff_from, result.agent);
    setActiveAgent(result.agent);
    addMessage(
      'agent',
      result.assistant_message.content,
      result.agent,
      Boolean(result.emergency),
      result.assistant_message.created_at,
      result.assistant_message.metadata || {},
    );
    state.context = result.context;
    state.urgency = result.urgency || 'routine';
    renderInsights();
    toggleAdvancedActions(Boolean(result.council_available));
    if (result.action === 'lab_results_prompt') await openLabResults();
    $('#taskStatus').textContent = result.assistant_message.metadata?.interpretation_cached
      ? 'Показана сохранённая расшифровка'
      : 'Расшифровка готова';
    await loadConversationList();
  } catch (error) {
    addSystemError(error.message);
    $('#taskStatus').textContent = 'Не удалось расшифровать результаты';
  } finally {
    state.processing = false;
    buttons.forEach(button => { button.disabled = false; });
    if (sourceButton && originalText) sourceButton.textContent = originalText;
    focusChatInput();
  }
}

async function addMemory() {
  const content = $('#memoryText').value.trim();
  if (!content) return;
  try {
    await api('/api/memories', { method:'POST', body:JSON.stringify({ content, category:$('#memoryCategory').value }) });
    $('#memoryText').value = '';
    await loadMemories();
  } catch (error) { addSystemError(error.message); }
}

async function deleteMemory(id) {
  try { await api(`/api/memories/${id}`, { method:'DELETE' }); await loadMemories(); }
  catch (error) { addSystemError(error.message); }
}

const durationLabels = { minutes:'Несколько минут', hours:'Несколько часов', days:'Несколько дней', weeks:'Несколько недель', months:'Несколько месяцев' };
const patternLabels = { constant:'Постоянно', episodes:'Приступами', movement:'При движении', touch:'При прикосновении', unknown:'Не уверен(а)' };
const agentLabels = { manager:'ИИ-менеджер', safety:'Контроль безопасности', therapist:'Терапевт', cardiologist:'Кардиолог', neurologist:'Невролог', dermatologist:'Дерматолог', pediatrician:'Педиатр', psychologist:'Психолог', general:'Здоровье и образ жизни' };

async function loadBodySymptoms() {
  try { state.bodySymptoms = await api('/api/body-symptoms'); }
  catch { state.bodySymptoms = []; }
  const active = state.bodySymptoms.filter(item => item.status === 'active');
  $('#bodySymptomCount').textContent = active.length;
  $('#bodyMapStatus').textContent = active.length ? `Активных отметок: ${active.length}` : 'Отметить симптом';
  document.querySelectorAll('.body-zone').forEach(zone => {
    zone.classList.toggle('has-symptom', active.some(item => item.region === zone.dataset.region && item.view === zone.dataset.view));
  });
}

function resetBodySymptomForm() {
  state.selectedBodyRegion = null;
  state.selectedSymptomType = null;
  document.querySelectorAll('.body-zone,.symptom-choice-grid button').forEach(item => item.classList.remove('selected'));
  $('#selectedBodyRegion').innerHTML = '<span>1</span><div><small>Выбранная область</small><strong>Нажмите на карту</strong></div>';
  $('#symptomTypeField').disabled = true;
  $('#customSymptomField').classList.add('hidden');
  $('#customSymptomText').value = '';
  $('#symptomIntensity').value = 5;
  $('#symptomIntensityValue').textContent = '5 из 10';
  $('#symptomStartedAt').value = '';
  $('#symptomDuration').value = '';
  $('#symptomPattern').value = 'constant';
  $('#symptomNotes').value = '';
  $('#saveBodySymptomButton').disabled = true;
}

function setBodyView(view) {
  state.selectedBodyView = view;
  document.querySelectorAll('[data-body-view]').forEach(button => button.classList.toggle('selected', button.dataset.bodyView === view));
  $('#bodyFront').classList.toggle('hidden', view !== 'front');
  $('#bodyBack').classList.toggle('hidden', view !== 'back');
}

function selectBodyRegion(zone) {
  state.selectedBodyRegion = zone.dataset.region;
  state.selectedBodyView = zone.dataset.view;
  document.querySelectorAll('.body-zone').forEach(item => item.classList.toggle(
    'selected', item.dataset.region === state.selectedBodyRegion && item.dataset.view === state.selectedBodyView,
  ));
  $('#selectedBodyRegion').innerHTML = `<span>✓</span><div><small>Выбранная область</small><strong>${escapeHtml(state.selectedBodyRegion)}</strong></div>`;
  $('#symptomTypeField').disabled = false;
  updateBodySymptomSaveState();
}

function updateBodySymptomSaveState() {
  const customReady = state.selectedSymptomType !== 'Другое' || Boolean($('#customSymptomText').value.trim());
  $('#saveBodySymptomButton').disabled = !(state.selectedBodyRegion && state.selectedSymptomType && customReady);
}

function selectSymptomType(button) {
  state.selectedSymptomType = button.dataset.symptomType;
  document.querySelectorAll('#symptomTypeChoices button').forEach(item => item.classList.toggle('selected', item === button));
  const isCustom = state.selectedSymptomType === 'Другое';
  $('#customSymptomField').classList.toggle('hidden', !isCustom);
  updateBodySymptomSaveState();
  if (isCustom) $('#customSymptomText').focus();
}

async function openBodyMap() {
  closeFunctionMenu();
  await loadBodySymptoms();
  resetBodySymptomForm();
  setBodyView('front');
  $('#bodyMapModal').classList.remove('hidden');
  document.querySelector('#bodyMapModal .body-map-modal').scrollTop = 0;
}

function closeBodyMap() { $('#bodyMapModal').classList.add('hidden'); }

async function saveBodySymptom() {
  if (!state.selectedBodyRegion || !state.selectedSymptomType) return;
  const button = $('#saveBodySymptomButton');
  button.disabled = true;
  try {
    await api('/api/body-symptoms', {
      method:'POST',
      body:JSON.stringify({
        region:state.selectedBodyRegion, view:state.selectedBodyView,
        symptom_type:state.selectedSymptomType, intensity:Number($('#symptomIntensity').value),
        custom_symptom:$('#customSymptomText').value.trim(),
        started_at:$('#symptomStartedAt').value, duration:$('#symptomDuration').value,
        pattern:$('#symptomPattern').value, notes:$('#symptomNotes').value.trim(),
      }),
    });
    await loadBodySymptoms();
    resetBodySymptomForm();
    $('#taskStatus').textContent = 'Симптом добавлен в историю · специалисты учтут его';
    closeBodyMap();
    await openHealthHistory('symptom');
  } catch (error) {
    button.disabled = false;
    addSystemError(error.message);
  }
}

function healthEventDate(value) {
  if (!value) return 'Дата не указана';
  return new Intl.DateTimeFormat('ru', { day:'2-digit', month:'long', year:'numeric', hour:'2-digit', minute:'2-digit' }).format(new Date(value));
}

function healthEventMarkup(item) {
  const icons = { symptom:'●', consultation:'✚', document:'▱', council:'◎', profile:'♙', tests:'◫' };
  const details = item.details || {};
  let summary = item.summary || '';
  let meta = '';
  let actions = '';
  if (item.type === 'symptom') {
    summary = `${details.region} · ${details.symptom_type}`;
    meta = `<span>Интенсивность: ${details.intensity}/10</span><span>${patternLabels[details.pattern] || details.pattern}</span>${details.duration ? `<span>${durationLabels[details.duration] || details.duration}</span>` : ''}${details.notes ? `<span>${escapeHtml(details.notes)}</span>` : ''}`;
    actions = `<button data-symptom-status="${details.id}" data-next-status="${details.status === 'active' ? 'resolved' : 'active'}">${details.status === 'active' ? 'Отметить улучшение' : 'Вернуть в активные'}</button><button class="danger" data-symptom-delete="${details.id}">Удалить</button>`;
  } else if (item.type === 'consultation') {
    summary = `Специалист: ${agentLabels[details.agent_id] || details.agent_id || 'врач-консультант'}`;
    actions = `<button data-history-conversation="${escapeHtml(details.conversation_id)}">Открыть диалог</button>`;
  } else if (item.type === 'document') {
    summary = item.summary || 'Добавлен медицинский документ';
    if (details.conversation_id) actions = `<button data-history-conversation="${escapeHtml(details.conversation_id)}">Открыть диалог</button>`;
  } else if (item.type === 'council') {
    meta = (details.agents || []).map(agent => `<span>${escapeHtml(agentLabels[agent] || agent)}</span>`).join('');
    if (details.conversation_id) actions = `<button data-history-conversation="${escapeHtml(details.conversation_id)}">Открыть заключение</button>`;
  }
  return `<article class="health-event" data-type="${item.type}">
    <div class="health-event-icon">${icons[item.type] || '•'}</div>
    <div class="health-event-card"><div class="health-event-top"><div><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(summary)}</small></div><span class="health-event-date">${healthEventDate(item.occurred_at)}</span></div>
    ${meta ? `<div class="health-event-meta">${meta}</div>` : ''}${actions ? `<div class="health-event-actions">${actions}</div>` : ''}</div>
  </article>`;
}

function renderHealthHistory() {
  const items = state.healthHistoryFilter === 'all'
    ? state.healthHistory
    : state.healthHistory.filter(item => item.type === state.healthHistoryFilter);
  const symptoms = state.healthHistory.filter(item => item.type === 'symptom');
  const active = symptoms.filter(item => item.status === 'active');
  const documents = state.healthHistory.filter(item => item.type === 'document');
  $('#healthHistorySummary').innerHTML = `<div class="history-stat"><strong>${active.length}</strong><span>активных симптомов</span></div><div class="history-stat"><strong>${documents.length}</strong><span>документов</span></div><div class="history-stat"><strong>${state.healthHistory.length}</strong><span>событий всего</span></div>`;
  $('#healthHistoryList').innerHTML = items.length ? items.map(healthEventMarkup).join('') : '<div class="history-empty">В этой категории пока нет событий.</div>';
  $('#healthHistoryCount').textContent = state.healthHistory.length;
}

async function loadHealthHistory() {
  try { state.healthHistory = await api('/api/health-history'); }
  catch { state.healthHistory = []; }
  renderHealthHistory();
}

async function openHealthHistory(filter = 'all') {
  closeFunctionMenu();
  state.healthHistoryFilter = filter;
  document.querySelectorAll('[data-history-filter]').forEach(button => button.classList.toggle('selected', button.dataset.historyFilter === filter));
  await loadHealthHistory();
  $('#healthHistoryModal').classList.remove('hidden');
  document.querySelector('#healthHistoryModal .health-history-modal').scrollTop = 0;
}

function closeHealthHistory() { $('#healthHistoryModal').classList.add('hidden'); }

async function changeBodySymptomStatus(id, status) {
  try {
    await api('/api/body-symptoms/status', { method:'POST', body:JSON.stringify({ id, status }) });
    await Promise.all([loadBodySymptoms(), loadHealthHistory()]);
  } catch (error) { addSystemError(error.message); }
}

async function deleteBodySymptom(id) {
  try {
    await api(`/api/body-symptoms/${id}`, { method:'DELETE' });
    await Promise.all([loadBodySymptoms(), loadHealthHistory()]);
  } catch (error) { addSystemError(error.message); }
}

async function addAttachments(files) {
  for (const file of [...files].slice(0, 3 - state.attachments.length)) {
    if (file.size > 4 * 1024 * 1024) { addSystemError(`${file.name}: файл больше 4 МБ`); continue; }
    const dataUrl = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = reject; reader.readAsDataURL(file); });
    const text = file.type.startsWith('text/') ? await file.text() : '';
    state.attachments.push({ name:file.name, type:file.type || 'text/plain', data_url:dataUrl, text:text.slice(0,12000) });
  }
  renderAttachments();
}

function renderAttachments() {
  $('#attachmentList').classList.toggle('hidden', !state.attachments.length);
  $('#attachmentList').innerHTML = state.attachments.map((item,index) => `<div class="attachment-chip"><span>▱ ${escapeHtml(item.name)}</span><button type="button" data-attachment-remove="${index}">×</button></div>`).join('');
}
function clearAttachments() { state.attachments = []; renderAttachments(); }
function escapeHtml(value) { const d = document.createElement('div'); d.textContent = value ?? ''; return d.innerHTML; }
function formatAssistantText(value) {
  const separated = String(value ?? '').replace(
    /\s*(Обращение H-[A-ZА-Я0-9-]+ уже передано человеку; ожидайте ответа специалиста\.)/giu,
    '\n\n\n$1',
  ).trimStart();
  return escapeHtml(separated).replace(
    /(https?:\/\/[^\s<]+)/g,
    '<a class="message-link" href="$1" target="_blank" rel="noopener noreferrer">Открыть документ</a>',
  );
}
function scrollChatToBottom() {
  requestAnimationFrame(() => {
    messages.scrollTop = messages.scrollHeight;
    requestAnimationFrame(() => { messages.scrollTop = messages.scrollHeight; });
  });
}
function focusChatInput() {
  input.focus({ preventScroll: true });
  if (window.matchMedia('(min-width: 721px)').matches) window.scrollTo(0, 0);
}
function syncVisualViewport() {
  const height = window.visualViewport?.height || window.innerHeight;
  document.documentElement.style.setProperty('--app-height', `${Math.round(height)}px`);
}
function formatRelative(value) { return new Intl.DateTimeFormat('ru', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }).format(new Date(value)); }

$('#chatForm').addEventListener('submit', event => {
  event.preventDefault();
  const text = input.value.trim();
  if ((!text && !state.attachments.length) || state.processing) return;
  input.value = '';
  input.style.height = 'auto';
  processMessage(text);
});
input.addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); $('#chatForm').requestSubmit(); }
});
input.addEventListener('input', () => { input.style.height = 'auto'; input.style.height = `${Math.min(input.scrollHeight, 140)}px`; });
input.addEventListener('focus', () => {
  setTimeout(scrollChatToBottom, 120);
  setTimeout(scrollChatToBottom, 320);
});
syncVisualViewport();
window.addEventListener('resize', syncVisualViewport, { passive: true });
window.visualViewport?.addEventListener('resize', () => {
  syncVisualViewport();
  if (document.activeElement === input) scrollChatToBottom();
}, { passive: true });
$('#suggestions').addEventListener('click', event => { const button = event.target.closest('[data-prompt]'); if (button) processMessage(button.dataset.prompt); });
$('#humanButton').addEventListener('click', () => processMessage('Я хочу поговорить с человеком'));
$('#secondOpinionButton').addEventListener('click', requestSecondOpinion);
$('#councilButton').addEventListener('click', openCouncilModal);
$('#councilModalClose').addEventListener('click', closeCouncilModal);
$('#cancelCouncilButton').addEventListener('click', closeCouncilModal);
$('#startCouncilButton').addEventListener('click', requestCouncil);
$('#councilModal').addEventListener('click', event => { if (event.target.id === 'councilModal') closeCouncilModal(); });
$('#contextClose').addEventListener('click', closeContextEditor);
$('#saveContextButton').addEventListener('click', saveContext);
async function openMemory() { await loadMemories(); $('#memoryModal').classList.remove('hidden'); }
$('#memoryButton').addEventListener('click', openMemory);
$('#memoryClose').addEventListener('click', () => $('#memoryModal').classList.add('hidden'));
$('#addMemoryButton').addEventListener('click', addMemory);
$('#capabilitiesButton').addEventListener('click', openCapabilities);
$('#capabilitiesClose').addEventListener('click', closeCapabilities);
$('#capabilitiesModal').addEventListener('click', event => { if (event.target.id === 'capabilitiesModal') closeCapabilities(); });
$('#capabilityProfile').addEventListener('click', () => { closeCapabilities(); openProfile(); });
$('#capabilityLabResults').addEventListener('click', () => { closeCapabilities(); openLabResults(); });
$('#capabilityBodyMap').addEventListener('click', () => { closeCapabilities(); openBodyMap(); });
$('#capabilityHealthHistory').addEventListener('click', () => { closeCapabilities(); openHealthHistory(); });
$('#functionMenuButton').addEventListener('click', toggleFunctionMenu);
$('#functionMenu').addEventListener('click', event => { if (event.target.closest('button')) closeFunctionMenu(); });
$('#menuFontSizeButton').addEventListener('click', openFontSizeModal);
$('#fontSizeClose').addEventListener('click', closeFontSizeModal);
$('#fontSizeModal').addEventListener('click', event => { if (event.target.id === 'fontSizeModal') closeFontSizeModal(); });
$('#fontSizeOptions').addEventListener('click', event => { const option=event.target.closest('.font-size-choice[data-size]'); if (option) updateFontSize(option.dataset.size); });
$('#menuProfileButton').addEventListener('click', openProfile);
$('#menuLabResultsButton').addEventListener('click', openLabResults);
$('#menuBodyMapButton').addEventListener('click', openBodyMap);
$('#menuHealthHistoryButton').addEventListener('click', () => openHealthHistory());
$('#menuMemoryButton').addEventListener('click', openMemory);
$('#resetUserButton').addEventListener('click', resetUser);
$('#profileButton').addEventListener('click', openProfile);
$('#bodyMapButton').addEventListener('click', openBodyMap);
$('#healthHistoryButton').addEventListener('click', () => openHealthHistory());
$('#profileClose').addEventListener('click', () => $('#profileModal').classList.add('hidden'));
$('#saveProfileButton').addEventListener('click', saveProfile);
$('#labResultsClose').addEventListener('click', closeLabResults);
$('#labResultsModal').addEventListener('click', event => { if (event.target.id === 'labResultsModal') closeLabResults(); });
$('#saveLabTubeButton').addEventListener('click', saveLabTube);
$('#changeLabTubeButton').addEventListener('click', changeLabTube);
$('#fetchLabResultsButton').addEventListener('click', fetchLabResults);
function handleLabInterpretClick(event) {
  const button = event.target.closest('[data-lab-interpret]');
  if (button) interpretLabResults(button.dataset.labInterpret, button);
}
$('#labResultDocuments').addEventListener('click', handleLabInterpretClick);
messages.addEventListener('click', handleLabInterpretClick);
$('#labTubeInput').addEventListener('input', () => $('#labTubeError').classList.add('hidden'));
$('#profileHeight').addEventListener('input', updateProfileBmi);
$('#profileWeight').addEventListener('input', updateProfileBmi);
$('#memoryList').addEventListener('click', event => { const button = event.target.closest('[data-memory-delete]'); if (button) deleteMemory(Number(button.dataset.memoryDelete)); });
$('#bodyMapClose').addEventListener('click', closeBodyMap);
$('#bodyMapModal').addEventListener('click', event => { if (event.target.id === 'bodyMapModal') closeBodyMap(); });
document.querySelectorAll('[data-body-view]').forEach(button => button.addEventListener('click', () => setBodyView(button.dataset.bodyView)));
document.querySelectorAll('.body-zone').forEach(zone => {
  zone.setAttribute('aria-label', zone.dataset.region);
  zone.addEventListener('click', () => selectBodyRegion(zone));
  zone.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); selectBodyRegion(zone); } });
});
$('#symptomTypeChoices').addEventListener('click', event => { const button=event.target.closest('[data-symptom-type]'); if (button) selectSymptomType(button); });
$('#customSymptomText').addEventListener('input', updateBodySymptomSaveState);
$('#symptomIntensity').addEventListener('input', event => { $('#symptomIntensityValue').textContent = `${event.target.value} из 10`; });
$('#saveBodySymptomButton').addEventListener('click', saveBodySymptom);
$('#healthHistoryClose').addEventListener('click', closeHealthHistory);
$('#healthHistoryModal').addEventListener('click', event => { if (event.target.id === 'healthHistoryModal') closeHealthHistory(); });
$('#historyAddSymptom').addEventListener('click', () => { closeHealthHistory(); openBodyMap(); });
$('#historyFilters').addEventListener('click', event => {
  const button=event.target.closest('[data-history-filter]');
  if (!button) return;
  state.healthHistoryFilter = button.dataset.historyFilter;
  document.querySelectorAll('[data-history-filter]').forEach(item => item.classList.toggle('selected', item === button));
  renderHealthHistory();
});
$('#healthHistoryList').addEventListener('click', event => {
  const statusButton=event.target.closest('[data-symptom-status]');
  const deleteButton=event.target.closest('[data-symptom-delete]');
  const conversationButton=event.target.closest('[data-history-conversation]');
  if (statusButton) changeBodySymptomStatus(Number(statusButton.dataset.symptomStatus), statusButton.dataset.nextStatus);
  else if (deleteButton) deleteBodySymptom(Number(deleteButton.dataset.symptomDelete));
  else if (conversationButton) { closeHealthHistory(); openConversation(conversationButton.dataset.historyConversation); }
});
$('#attachButton').addEventListener('click', () => $('#attachmentInput').click());
$('#attachmentInput').addEventListener('change', async event => { await addAttachments(event.target.files); event.target.value = ''; });
$('#attachmentList').addEventListener('click', event => { const button = event.target.closest('[data-attachment-remove]'); if (button) { state.attachments.splice(Number(button.dataset.attachmentRemove), 1); renderAttachments(); } });
$('#newChatButton').addEventListener('click', newConversation);
$('#menuDashboardButton').addEventListener('click', () => {
  closeFunctionMenu();
  window.open('/dashboard', '_blank', 'noopener');
});
$('#conversationList').addEventListener('click', event => { const row = event.target.closest('[data-id]'); if (row) openConversation(row.dataset.id); });
$('#modalClose').addEventListener('click', resumeAfterHuman);
$('#modalOkay').addEventListener('click', resumeAfterHuman);
$('#humanChatButton').addEventListener('click', () => chooseHumanChannel('chat'));
$('#humanCallButton').addEventListener('click', showCallPhoneStep);
$('#callPhoneBack').addEventListener('click', resetCallPhoneStep);
$('#callPhoneInput').addEventListener('input', () => validateCallPhone(false));
$('#confirmCallButton').addEventListener('click', () => {
  const phone = validateCallPhone(true);
  if (phone) chooseHumanChannel('call', phone);
});
$('#humanModal').addEventListener('click', event => { if (event.target.id === 'humanModal') resumeAfterHuman(); });
$('#handoffPreview').addEventListener('click', event => {
  if (event.target.id !== 'editHandoffContext') return;
  state.returnToHumanAfterContextEdit = true;
  state.contextEditTicketId = $('#ticketNumber').textContent;
  closeHumanModal();
  openContextEditor();
});
function closeMobileTeam() { document.body.classList.remove('show-team'); }
$('#mobileTeamButton').addEventListener('click', () => document.body.classList.toggle('show-team'));
$('#mobileTeamClose').addEventListener('click', closeMobileTeam);
$('#teamBackdrop').addEventListener('click', closeMobileTeam);
document.addEventListener('click', event => {
  if (!event.target.closest('.function-menu-wrap')) closeFunctionMenu();
});
document.addEventListener('keydown', event => {
  if (event.key !== 'Escape') return;
  closeAnonymousWarning();
  closeMobileTeam();
  closeCouncilModal();
  closeFunctionMenu();
  closeFontSizeModal();
  closeLabResults();
  closeBodyMap();
  closeHealthHistory();
  if (!$('#capabilitiesModal').classList.contains('hidden')) closeCapabilities();
});

async function initMainApp() {
  state.mainInitialized = true;
  renderAgentList();
  await checkHealth();
  await loadMemories();
  await loadProfile();
  await Promise.all([loadBodySymptoms(), loadHealthHistory()]);
  await loadConversationList();
  if (state.conversationId) await openConversation(state.conversationId);
  else newConversation();
}

async function init() {
  try {
    const identity = await api('/api/me');
    state.identity = identity;
    const messengerLoginRequired = new URLSearchParams(window.location.search)
      .get('auth') === 'messenger_required';
    if (identity.authenticated) {
      localStorage.removeItem(ANONYMOUS_ACCESS_KEY);
      await startApplication();
    } else if (messengerLoginRequired) {
      showAuthGate();
      setAuthStatus('Эта ссылка принадлежит другому пользователю. Войдите через свой мессенджер.', true);
    } else if (localStorage.getItem(ANONYMOUS_ACCESS_KEY) === identity.chel_id) {
      await startApplication();
    } else {
      showAuthGate();
    }
  }
  catch (error) {
    showAuthGate();
    setAuthStatus(`Не удалось загрузить сервис: ${error.message}`, true);
  }
}
setInterval(syncConversationUpdates, 3000);
document.addEventListener('pointerdown', unlockUserSound, {once:true});
document.addEventListener('keydown', unlockUserSound, {once:true});
init();
