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
  unreadCounts: {},
  lastMessageId: 0,
  returnToHumanAfterContextEdit: false,
  contextEditTicketId: null,
  returnToChatAfterExaminations: false,
  paymentReviewSource: '',
  paymentReviewOrderId: '',
  paymentReceiptEmail: '',
  publicConfig: {},
  messengerLinkJustCompleted: '',
  purchases: [],
  resultFlowActive: false,
  resultFlowDocuments: [],
  miniProfilePurpose: 'interpretation',
};

const $ = selector => document.querySelector(selector);
const messages = $('#messages');
const timeline = $('#timeline');
const input = $('#messageInput');
const ANONYMOUS_ACCESS_KEY = 'consilium_anonymous_access';
const WELCOME_SEEN_KEY = 'consilium_welcome_seen';
const INSTALL_DISMISSED_KEY = 'consilium_install_dismissed_at';
const MESSENGER_LINK_PENDING_KEY = 'consilium_messenger_link_pending';
const PAYMENT_PENDING_ORDER_KEY = 'consilium_pending_payment_order';
const RESULT_FLOW_KEY = 'consilium_result_flow_v1';
const INSTALL_REOFFER_AFTER_MS = 7 * 24 * 60 * 60 * 1000;
let userAudioContext = null;
let deferredInstallPrompt = null;
let installOfferTimer = null;
let viewportSyncFrame = null;
let backNavigationArmed = false;
let allowBackNavigation = false;
let welcomeNextAction = null;
let installAfterCapabilities = false;
let analyticsFlushTimer = null;
let analyticsSending = false;
let questionShownAt = 0;
let currentOnboardingAnalyticsScreen = '';

const ANALYTICS_QUEUE_KEY = 'consilium_analytics_queue_v1';
const ANALYTICS_SESSION_KEY = 'consilium_analytics_session_v1';
const ANALYTICS_SESSION_TTL = 30 * 60 * 1000;

function analyticsUuid() {
  return crypto.randomUUID?.() || `evt-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function getAnalyticsSession() {
  const now = Date.now();
  try {
    const saved = JSON.parse(sessionStorage.getItem(ANALYTICS_SESSION_KEY) || '{}');
    if (saved.id && now - Number(saved.lastSeen || 0) < ANALYTICS_SESSION_TTL) {
      saved.lastSeen = now;
      sessionStorage.setItem(ANALYTICS_SESSION_KEY, JSON.stringify(saved));
      return saved.id;
    }
  } catch {}
  const id = `ses-${analyticsUuid()}`;
  sessionStorage.setItem(ANALYTICS_SESSION_KEY, JSON.stringify({id,lastSeen:now}));
  return id;
}

const analyticsSessionId = getAnalyticsSession();

function analyticsAttribution() {
  const params = new URLSearchParams(location.search);
  return {
    source: isResultEntryUrl()
      ? 'result'
      : params.get('splitter_source') || params.get('utm_source') || params.get('source') || '',
    campaign: params.get('utm_campaign') || '',
    medium: params.get('utm_medium') || '',
    app_mode: isInstalledApp() ? 'standalone' : 'browser',
    page_version: '20260805',
  };
}

function readAnalyticsQueue() {
  try {
    const queue = JSON.parse(localStorage.getItem(ANALYTICS_QUEUE_KEY) || '[]');
    return Array.isArray(queue) ? queue.slice(-200) : [];
  } catch { return []; }
}

function writeAnalyticsQueue(queue) {
  try { localStorage.setItem(ANALYTICS_QUEUE_KEY, JSON.stringify(queue.slice(-200))); } catch {}
}

function trackEvent(eventName, properties = {}) {
  const queue = readAnalyticsQueue();
  queue.push({
    event_id:`web-${analyticsUuid()}`,
    session_id:analyticsSessionId,
    event_name:eventName,
    client_at:new Date().toISOString(),
    properties:{...analyticsAttribution(),...properties},
  });
  writeAnalyticsQueue(queue);
  clearTimeout(analyticsFlushTimer);
  analyticsFlushTimer = setTimeout(() => flushAnalytics(), queue.length >= 10 ? 800 : 6000);
  trackMetrikaGoal(eventName, properties);
}

function onboardingAnalyticsContext() {
  if (state.resultFlowActive) return 'result';
  return state.returnToChatAfterExaminations ? 'chat' : 'onboarding';
}

function trackOnboardingScreen(screen) {
  const previousScreen = currentOnboardingAnalyticsScreen;
  currentOnboardingAnalyticsScreen = screen;
  trackEvent('onboarding_screen_viewed', {
    screen, previous_screen:previousScreen, context:onboardingAnalyticsContext(),
  });
}

function trackOnboardingAction(action, screen = currentOnboardingAnalyticsScreen) {
  if (!screen || !action) return;
  trackEvent('onboarding_screen_action', {
    screen, action, context:onboardingAnalyticsContext(),
  });
}

function trackMetrikaGoal(eventName, properties = {}) {
  const fixedGoals = {
    welcome_continued:'welcome_continue', appearance_completed:'font_size_selected',
    questionnaire_started:'questionnaire_started', questionnaire_completed:'questionnaire_completed',
    examinations_offer_viewed:'exam_offer_viewed', examinations_opened:'exam_options_opened',
    examinations_selection_completed:'exam_selection_completed', onboarding_completed:'onboarding_completed',
    capabilities_viewed:'capabilities_viewed', chat_opened:'chat_opened',
    first_message_sent:'first_message_sent', human_requested:'human_requested',
    install_clicked:'install_clicked',
  };
  let goal = fixedGoals[eventName] || '';
  if (eventName === 'registration_method_selected') {
    const method = String(properties.method || properties.provider || '');
    if (['max','telegram','anonymous'].includes(method)) goal = `registration_${method}`;
  }
  if (eventName === 'payment_method_selected') {
    const method = String(properties.method || '');
    if (method === 'online') goal = 'payment_online';
    if (method === 'at_exam') goal = 'payment_at_exam';
  }
  if (goal) window.consiliumMetrikaGoal?.(goal);
}

async function flushAnalytics({ beacon = false } = {}) {
  if (analyticsSending) return;
  const queue = readAnalyticsQueue();
  if (!queue.length) return;
  const batch = queue.slice(0, 30);
  const body = JSON.stringify({events:batch});
  if (beacon && navigator.sendBeacon) {
    if (navigator.sendBeacon('/api/analytics/events', new Blob([body], {type:'application/json'}))) {
      writeAnalyticsQueue(queue.slice(batch.length));
    }
    return;
  }
  analyticsSending = true;
  try {
    const response = await fetch('/api/analytics/events', {
      method:'POST', body, keepalive:true,
      headers:{'Content-Type':'application/json','X-Analytics-Session':analyticsSessionId},
    });
    if (response.ok) {
      const sentIds = new Set(batch.map(event => event.event_id));
      writeAnalyticsQueue(readAnalyticsQueue().filter(item => !sentIds.has(item.event_id)));
    }
  } catch {}
  finally { analyticsSending = false; }
}

window.addEventListener('pagehide', () => flushAnalytics({beacon:true}));
window.addEventListener('online', () => flushAnalytics());

function isIosDevice() {
  return /iphone|ipad|ipod/i.test(navigator.userAgent)
    || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
}

function isInstalledApp() {
  return window.matchMedia('(display-mode: standalone)').matches
    || window.navigator.standalone === true;
}

function updateInstallMenu() {
  const button = $('#menuInstallAppButton');
  if (!button) return;
  const installed = isInstalledApp();
  button.classList.remove('hidden');
  button.disabled = installed;
  const status = $('#menuInstallAppStatus');
  if (status) {
    status.textContent = installed
      ? 'Уже установлено'
      : isIosDevice()
      ? 'Добавить на экран «Домой»'
      : 'Открывать как приложение';
  }
}

function closeInstallApp({ dismissed = false } = {}) {
  $('#installAppModal').classList.add('hidden');
  if (dismissed) {
    localStorage.setItem(INSTALL_DISMISSED_KEY, String(Date.now()));
    trackEvent('install_dismissed');
  }
}

function openInstallApp() {
  closeFunctionMenu();
  if (isInstalledApp()) return;
  const ios = isIosDevice();
  const hasNativePrompt = Boolean(deferredInstallPrompt);
  $('#iosInstallSteps').classList.toggle('hidden', !ios);
  $('#installAppConfirmButton').classList.toggle('hidden', !ios && !hasNativePrompt);
  $('#installAppConfirmButton').textContent = ios ? 'Понятно' : 'Добавить';
  $('#installAppDescription').textContent = ios
    ? 'На iPhone ярлык добавляется через меню Safari. После этого Консилиум будет открываться отдельным окном.'
    : hasNativePrompt
      ? 'Сервис будет открываться отдельным окном, почти как обычное приложение. Скачивать его из магазина не нужно.'
      : 'Откройте меню браузера и выберите «Установить приложение» или «Добавить на главный экран».';
  $('#installAppModal').classList.remove('hidden');
  trackEvent('install_offer_viewed', {install_method:ios ? 'ios_instructions' : hasNativePrompt ? 'native_prompt' : 'browser_instructions'});
}

function scheduleInstallOffer() {
  clearTimeout(installOfferTimer);
  if (
    isInstalledApp()
    || state.onboarding?.status !== 'complete'
    || !state.mainInitialized
    || (!isIosDevice() && !deferredInstallPrompt)
  ) return;
  const dismissedAt = Number(localStorage.getItem(INSTALL_DISMISSED_KEY) || 0);
  if (Date.now() - dismissedAt < INSTALL_REOFFER_AFTER_MS) return;
  installOfferTimer = setTimeout(() => {
    if (
      $('#capabilitiesModal').classList.contains('hidden')
      && $('#installAppModal').classList.contains('hidden')
    ) openInstallApp();
  }, 1800);
}

async function confirmInstallApp() {
  trackEvent('install_clicked', {install_method:isIosDevice() ? 'ios_instructions' : 'native_prompt'});
  if (isIosDevice()) {
    closeInstallApp({ dismissed: true });
    return;
  }
  if (!deferredInstallPrompt) return;
  const prompt = deferredInstallPrompt;
  deferredInstallPrompt = null;
  await prompt.prompt();
  const choice = await prompt.userChoice;
  if (choice.outcome !== 'accepted') {
    localStorage.setItem(INSTALL_DISMISSED_KEY, String(Date.now()));
  }
  closeInstallApp();
  updateInstallMenu();
}

window.addEventListener('beforeinstallprompt', event => {
  event.preventDefault();
  deferredInstallPrompt = event;
  updateInstallMenu();
  scheduleInstallOffer();
});

window.addEventListener('appinstalled', () => {
  trackEvent('app_installed', {install_method:'native_prompt'});
  deferredInstallPrompt = null;
  localStorage.removeItem(INSTALL_DISMISSED_KEY);
  closeInstallApp();
  updateInstallMenu();
});

if ('serviceWorker' in navigator) {
  let reloadingForServiceWorkerUpdate = false;
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    if (reloadingForServiceWorkerUpdate) return;
    reloadingForServiceWorkerUpdate = true;
    window.location.reload();
  });
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/service-worker.js')
      .then(registration => registration.update())
      .catch(error => {
        console.warn('Не удалось подключить режим приложения', error);
      });
  });
}

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
  { key:'company_inn', title:'Сообщите ИНН вашего предприятия', lead:'Если вы не владеете этой информацией, пожалуйста, уточните её у вашего работодателя.', type:'text', inputmode:'numeric', maxlength:12, placeholder:'10 или 12 цифр' },
  { key:'preferred_name', title:'Как к вам обращаться?', lead:'Имя необязательно, но с ним общение будет естественнее.', type:'text', placeholder:'Например, Алексей', optional:true },
  { key:'age', title:'Сколько вам полных лет?', lead:'Возраст помогает специалистам точнее учитывать риски и нормы.', type:'number', min:18, max:99, step:'1', placeholder:'От 18 до 99' },
  { key:'sex', title:'Укажите пол для медицинского контекста', lead:'Это важно для интерпретации части симптомов и обследований.', choices:[['female','Женский'],['male','Мужской']] },
  { key:'height_cm', title:'Какой у вас рост?', lead:'Введите значение в сантиметрах.', type:'number', min:50, max:250, step:'0.1', placeholder:'От 50 до 250' },
  { key:'weight_kg', title:'Какой у вас вес?', lead:'Введите актуальный вес в килограммах.', type:'number', min:40, max:250, step:'0.1', placeholder:'От 40 до 250' },
  { key:'smoking', title:'Вы курите?', lead:'Учитываются сигареты, электронные сигареты и другие способы употребления никотина.', choices:[['never','Не курю'],['former','Курил(а) раньше'],['current','Курю сейчас']] },
  { key:'alcohol', title:'Как часто вы употребляете алкоголь?', lead:'Выберите наиболее близкий вариант.', choices:[['never','Не употребляю'],['rarely','Редко / по праздникам'],['weekly','Примерно раз в неделю'],['often','Чаще раза в неделю']] },
  { key:'activity', title:'Какой у вас уровень активности?', lead:'Ориентируйтесь на обычный день: низкий — до 5 000 шагов в день, средний — 5–10 тысяч шагов в день, высокий — более 10 тысяч шагов в день или регулярный спорт.', choices:[['low','Низкий'],['moderate','Средний'],['high','Высокий']] },
  { key:'blood_pressure', title:'Как вы оцениваете своё давление?', lead:'Если не измеряли или не уверены, выберите «Не знаю».', choices:[['normal','Обычно в норме'],['high','Бывает повышенным'],['low','Бывает пониженным'],['unstable','Сильно меняется'],['unknown','Не знаю']] },
  { key:'dark_in_eyes', title:'Темнеет ли в глазах при резком подъёме?', lead:'Например, когда быстро встаёте с кровати или стула.', choices:[['no','Нет'],['yes','Да'],['unknown','Не уверен(а)']] },
  { key:'blood_sugar', title:'Знаете ли вы уровень сахара в крови?', lead:'Это не оценка диагноза — только уже известная вам информация.', choices:[['normal','Был в норме'],['high','Бывал повышен'],['unknown','Не измерял(а) / не знаю']] },
  { key:'joint_pain', title:'Бывают боли или отёчность суставов?', lead:'В том числе при нагрузке или смене погоды.', choices:[['no','Нет'],['yes','Да'],['unknown','Не уверен(а)']] },
  { key:'fatigue', title:'Беспокоит длительная усталость?', lead:'Имеется в виду усталость, которая сохраняется после обычного отдыха.', choices:[['no','Нет'],['yes','Да'],['unknown','Не уверен(а)']] },
  { key:'conditions', title:'Есть хронические заболевания?', lead:'Напишите по одному на строку. Если нет — этот шаг можно пропустить.', type:'textarea', placeholder:'Например:\nГипертония\nАстма', optional:true, list:true },
  { key:'medications', title:'Какие лекарства принимаете постоянно?', lead:'Название и дозировка, если известна. Шаг можно пропустить.', type:'textarea', placeholder:'По одному препарату на строку', optional:true, list:true },
  { key:'allergies', title:'Есть аллергии?', lead:'Укажите лекарства, продукты или другие известные аллергены. Шаг можно пропустить.', type:'textarea', placeholder:'По одному аллергену на строку', optional:true, list:true },
  { key:'notes', title:'Есть ли у вас жалобы?', lead:'Введите в одном сообщении всё, что вас тревожит: проблему, симптомы и что вы принимаете в связи с ними. Если жалоб нет, этот вопрос можно пропустить.', type:'textarea', maxlength:1000, placeholder:'Например: две недели болит голова по вечерам, принимаю ибупрофен', optional:true },
];

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: {
      'Content-Type': 'application/json',
      'X-Analytics-Session': analyticsSessionId,
      ...(options.headers || {}),
    },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    trackEvent('api_error', {status_code:response.status,error_code:`http_${response.status}`,reason:path.split('?')[0]});
    const error = new Error(data.detail || `Ошибка сервера: ${response.status}`);
    error.code = data.code || `http_${response.status}`;
    error.details = data;
    throw error;
  }
  return data;
}

function setAuthStatus(message = '', error = false) {
  const status = $('#authStatus');
  status.textContent = message;
  status.classList.toggle('hidden', !message);
  status.classList.toggle('error', Boolean(error));
}

function hideEntryScreens() {
  $('#welcomeScreen').classList.add('hidden');
  $('#authGate').classList.add('hidden');
  $('#onboarding').classList.add('hidden');
  $('#appShell').classList.add('hidden');
}

function showWelcome(nextAction) {
  welcomeNextAction = nextAction;
  hideEntryScreens();
  $('#welcomeScreen').classList.remove('hidden');
  $('#welcomeScreen').scrollTop = 0;
  trackEvent('welcome_viewed', {screen:'welcome'});
  trackOnboardingScreen('welcome');
}

async function continueFromWelcome() {
  const button = $('#welcomeNextButton');
  const nextAction = welcomeNextAction;
  button.disabled = true;
  localStorage.setItem(WELCOME_SEEN_KEY, '1');
  trackEvent('welcome_continued', {screen:'welcome'});
  trackOnboardingAction('continue', 'welcome');
  $('#welcomeScreen').classList.add('hidden');
  try {
    if (nextAction) await nextAction();
  } catch (error) {
    showAuthGate();
    setAuthStatus(`Не удалось продолжить: ${error.message}`, true);
  } finally {
    welcomeNextAction = null;
    button.disabled = false;
  }
}

function isResultEntryUrl() {
  const path = window.location.pathname.replace(/\/+$/, '') || '/';
  const params = new URLSearchParams(window.location.search);
  return path === '/result'
    || params.has('result')
    || params.get('flow') === 'result'
    || params.get('source') === 'result';
}

function readResultFlow() {
  try {
    const value = JSON.parse(localStorage.getItem(RESULT_FLOW_KEY) || 'null');
    if (!value?.stage || Date.now() - Number(value.updatedAt || 0) > 48 * 60 * 60 * 1000) {
      localStorage.removeItem(RESULT_FLOW_KEY);
      return null;
    }
    return value;
  } catch {
    localStorage.removeItem(RESULT_FLOW_KEY);
    return null;
  }
}

function writeResultFlow(stage, extra = {}) {
  const value = {...(readResultFlow() || {}), ...extra, stage, updatedAt:Date.now()};
  localStorage.setItem(RESULT_FLOW_KEY, JSON.stringify(value));
  return value;
}

function clearResultFlow() {
  state.resultFlowActive = false;
  state.resultFlowDocuments = [];
  localStorage.removeItem(RESULT_FLOW_KEY);
}

function trackResultScreen(screen, previousScreen = currentOnboardingAnalyticsScreen) {
  currentOnboardingAnalyticsScreen = screen;
  trackEvent('onboarding_screen_viewed', {screen, previous_screen:previousScreen, context:'result'});
}

function trackResultAction(action, screen = currentOnboardingAnalyticsScreen) {
  if (!screen || !action) return;
  trackEvent('onboarding_screen_action', {screen, action, context:'result'});
}

function showResultScreen(screen, stage, progress, content) {
  hideEntryScreens();
  state.resultFlowActive = true;
  $('#onboarding').classList.remove('hidden');
  setOnboardingMeta(stage, progress);
  $('#onboardingContent').innerHTML = `<div class="result-flow">${content}</div>`;
  $('#onboarding').scrollTop = 0;
  trackResultScreen(screen);
}

function renderResultWelcome() {
  writeResultFlow('welcome');
  showResultScreen('result_welcome', 'Результаты', 12, `
    <div class="result-flow-icon" aria-hidden="true">▤</div>
    <span class="onboarding-kicker">Результаты обследований</span>
    <h1>Ваши анализы — в одном месте</h1>
    <p class="onboarding-lead">Здесь можно получить документы по номеру пробирки, попросить помочь с расшифровкой и задать вопрос о результатах медицинскому помощнику.</p>
    <div class="result-flow-note"><strong>Что понадобится</strong><p>Индивидуальный номер пробирки, который сообщили во время медицинского осмотра.</p></div>
    <div class="onboarding-actions"><button type="button" class="onboarding-next" data-result-action="begin">Далее</button></div>`);
}

function renderResultTube() {
  const saved = state.profile?.tube_number || readResultFlow()?.tubeNumber || '';
  writeResultFlow('tube', {tubeNumber:saved});
  showResultScreen('result_tube', 'Номер пробирки', 32, `
    <div class="result-flow-icon" aria-hidden="true">№</div>
    <span class="onboarding-kicker">Поиск результатов</span>
    <h1>Введите номер пробирки</h1>
    <p class="onboarding-lead">Он указан на вашей наклейке или был сообщён бригадой на медицинском осмотре.</p>
    <label class="result-tube-field"><span>Номер пробирки</span><input id="resultTubeInput" type="text" maxlength="80" autocomplete="off" value="${escapeAttr(saved)}" placeholder="Например, 123456"></label>
    <p class="result-inline-error hidden" id="resultTubeError" role="alert"></p>
    <div class="onboarding-actions"><button type="button" class="onboarding-next" data-result-action="save-tube">Продолжить</button></div>`);
  requestAnimationFrame(() => $('#resultTubeInput')?.focus());
}

function renderResultMessenger() {
  const linked = linkedMessengerProviders().size > 0;
  if (linked) state.messengerLinkJustCompleted = '';
  writeResultFlow('messenger');
  showResultScreen('result_messenger', 'Сохранение доступа', 54, `
    <div class="result-flow-icon" aria-hidden="true">↗</div>
    <span class="onboarding-kicker">Рекомендуем</span>
    <h1>${linked ? 'Мессенджер привязан' : 'Не потеряйте результаты'}</h1>
    <p class="onboarding-lead">${linked
      ? 'Ваш профиль можно будет открыть с другого устройства. Теперь найдём документы по номеру пробирки.'
      : 'Привяжите Telegram или MAX: так профиль не потеряется после очистки браузера, а результаты, расшифровка и консультация останутся доступны на любом устройстве.'}</p>
    <ul class="result-benefits"><li>доступ к документам с телефона и компьютера;</li><li>история расшифровок и консультаций в одном профиле;</li><li>возможность получить уведомление, когда результаты будут готовы.</li></ul>
    <div class="result-flow-actions">
      ${linked ? '' : '<button type="button" class="result-link-button" data-result-action="link-messenger">Привязать мессенджер</button>'}
      <button type="button" class="onboarding-next" data-result-action="search">${linked ? 'Найти результаты' : 'Продолжить без привязки'}</button>
    </div>`);
}

function resultDocumentsMarkup(items) {
  const documents = normalizeLabDocuments(items);
  return `<div class="result-document-list">${documents.map((item, index) => `
    <a class="result-document" href="${escapeAttr(item.url)}" target="_blank" rel="noopener noreferrer">
      <i aria-hidden="true">▤</i><span><strong>${escapeHtml(item.title || `Результаты анализов · документ ${index + 1}`)}</strong><small>Открыть документ</small></span><b aria-hidden="true">→</b>
    </a>`).join('')}</div>`;
}

async function searchResultDocuments() {
  writeResultFlow('search');
  showResultScreen('result_search', 'Поиск результатов', 72, `
    <div class="result-search-spinner" aria-hidden="true"></div>
    <span class="onboarding-kicker">Проверяем номер</span>
    <h1>Ищем ваши результаты</h1>
    <p class="onboarding-lead">Обычно это занимает несколько секунд.</p>`);
  try {
    const result = await api('/api/lab-results', {method:'POST'});
    if (result.status === 'found' && result.urls?.length) {
      trackResultAction('found', 'result_search');
      renderResultFound(result.documents || result.urls);
    } else {
      trackResultAction('not_found', 'result_search');
      renderResultNotFound(result.status === 'processing');
    }
  } catch (error) {
    trackResultAction('not_found', 'result_search');
    renderResultNotFound(false, error.message);
  }
}

function renderResultFound(items) {
  state.resultFlowDocuments = normalizeLabDocuments(items);
  writeResultFlow('found');
  showResultScreen('result_found', 'Результаты готовы', 100, `
    <div class="result-flow-icon success" aria-hidden="true">✓</div>
    <span class="onboarding-kicker">Готово</span>
    <h1>Результаты найдены</h1>
    <p class="onboarding-lead">Документы уже доступны. В чате можно попросить Ольгу помочь с расшифровкой или пригласить медицинского специалиста.</p>
    ${resultDocumentsMarkup(state.resultFlowDocuments)}
    <div class="onboarding-actions"><button type="button" class="onboarding-next" data-result-action="open-chat">Перейти в чат и получить консультацию</button></div>`);
}

function renderResultNotFound(processing = false, errorMessage = '') {
  writeResultFlow('not_found');
  const text = errorMessage
    ? `Не удалось выполнить поиск: ${escapeHtml(errorMessage)}`
    : processing
      ? 'Номер найден, но документы ещё обрабатываются.'
      : 'По этому номеру документы пока не появились. Проверьте номер или запросите уведомление о готовности.';
  showResultScreen('result_not_found', 'Ожидание результатов', 88, `
    <div class="result-flow-icon waiting" aria-hidden="true">…</div>
    <span class="onboarding-kicker">Пока не готовы</span>
    <h1>Результаты ещё не найдены</h1>
    <p class="onboarding-lead">${text}</p>
    <div class="result-flow-note"><strong>Сообщим о готовности</strong><p>Для уведомления нужен привязанный Telegram или MAX — иначе мы не сможем связаться с вами.</p></div>
    <div class="result-flow-actions"><button type="button" class="result-link-button" data-result-action="notify">Получить уведомление</button><button type="button" class="onboarding-next" data-result-action="retry-search">Проверить ещё раз</button><button type="button" class="result-quiet-button" data-result-action="open-chat">Перейти в чат</button></div>`);
}

function renderResultNotification() {
  writeResultFlow('notification');
  showResultScreen('result_notification', 'Уведомление', 100, `
    <div class="result-flow-icon success" aria-hidden="true">✓</div>
    <span class="onboarding-kicker">Запрос сохранён</span>
    <h1>Сообщим, когда результаты появятся</h1>
    <p class="onboarding-lead">Уведомление будет связано с номером пробирки и вашим профилем.</p>
    <div class="onboarding-actions"><button type="button" class="onboarding-next" data-result-action="open-chat">Перейти в чат</button></div>`);
}

async function saveResultTube() {
  const input = $('#resultTubeInput');
  const tubeNumber = input?.value.trim() || '';
  if (!tubeNumber) {
    $('#resultTubeError').textContent = 'Введите номер пробирки';
    $('#resultTubeError').classList.remove('hidden');
    input?.focus();
    return;
  }
  const button = $('#onboardingContent [data-result-action="save-tube"]');
  button.disabled = true;
  try {
    state.profile = await api('/api/profile', {method:'POST', body:JSON.stringify(profilePayloadWithTube(tubeNumber))});
    writeResultFlow('messenger', {tubeNumber});
    trackResultAction('continue', 'result_tube');
    renderResultMessenger();
  } catch (error) {
    $('#resultTubeError').textContent = error.message;
    $('#resultTubeError').classList.remove('hidden');
    button.disabled = false;
  }
}

async function requestResultNotification() {
  if (!linkedMessengerProviders().size) {
    writeResultFlow('not_found', {pendingNotification:true});
    trackResultAction('notify', 'result_not_found');
    openMessengerLinkModal({source:'result_notification'});
    return;
  }
  try {
    state.messengerLinkJustCompleted = '';
    await api('/api/lab-results/notification', {method:'POST', body:'{}'});
    trackResultAction('notify', 'result_not_found');
    renderResultNotification();
  } catch (error) {
    showOnboardingError(error.message);
  }
}

async function finishResultFlow({openResults = false} = {}) {
  trackResultAction('open_chat');
  const documents = [...state.resultFlowDocuments];
  clearResultFlow();
  await openMainApp({skipIntro:true});
  if (openResults || documents.length) await openLabResults();
}

async function enterResultFlow({explicit = false} = {}) {
  const onboarding = await api('/api/onboarding');
  state.onboarding = onboarding;
  state.profile = onboarding.profile || await api('/api/profile');
  applyFontSize(onboarding.font_size || 'extra');
  const registration = await api('/api/result-entry/start', {method:'POST', body:'{}'});
  state.identity = {...state.identity, result_entry:true};
  localStorage.setItem(ANONYMOUS_ACCESS_KEY, registration.chel_id || state.identity?.chel_id || '');

  if (hasCompletedQuestionnaire(onboarding)) {
    clearResultFlow();
    trackResultScreen('result_existing', '');
    await openMainApp({skipIntro:true});
    await openLabResults();
    return;
  }

  state.resultFlowActive = true;
  const saved = readResultFlow();
  if (explicit || !saved) return renderResultWelcome();
  if (saved.stage === 'tube') return renderResultTube();
  if (saved.stage === 'messenger') return renderResultMessenger();
  if (saved.stage === 'not_found') {
    if (saved.pendingNotification && linkedMessengerProviders().size) return requestResultNotification();
    return renderResultNotFound();
  }
  if (saved.stage === 'notification') return renderResultNotification();
  return renderResultWelcome();
}

function hasCompletedQuestionnaire(onboarding) {
  return Boolean(
    onboarding?.profile?.updated_at
    && ['exams','payment','complete'].includes(onboarding?.status)
  );
}

async function enterKnownUser() {
  const onboarding = await api('/api/onboarding');
  if (hasCompletedQuestionnaire(onboarding)) {
    localStorage.setItem(WELCOME_SEEN_KEY, '1');
    await startApplication({ openCompletedMessengerAccount:true, initialOnboarding:onboarding });
    return;
  }
  if (localStorage.getItem(WELCOME_SEEN_KEY)) {
    await startApplication();
    return;
  }
  if (onboarding.status === 'complete') {
    localStorage.setItem(WELCOME_SEEN_KEY, '1');
    await startApplication();
    return;
  }
  showWelcome(startApplication);
}

function showAuthGate() {
  $('#welcomeScreen').classList.add('hidden');
  $('#authGate').classList.remove('hidden');
  $('#onboarding').classList.add('hidden');
  $('#appShell').classList.add('hidden');
  trackEvent('auth_gate_viewed', {screen:'registration'});
  trackOnboardingScreen('registration');
}

function messengerName(provider) {
  return provider === 'max' ? 'MAX' : 'Telegram';
}

function linkedMessengerProviders() {
  return new Set(state.identity?.providers || []);
}

function updateMessengerLinkMenu() {
  const status = $('#menuMessengerLinkStatus');
  if (!status) return;
  const linked = [...linkedMessengerProviders()];
  if (!linked.length) status.textContent = 'Telegram или MAX';
  else if (linked.length === 1) status.textContent = `${messengerName(linked[0])} привязан · можно добавить ещё`;
  else status.textContent = 'Telegram и MAX привязаны';
}

function closeMessengerLinkModal() {
  $('#messengerLinkModal').classList.add('hidden');
}

function renderMessengerLinkOptions() {
  const linked = linkedMessengerProviders();
  const providers = [
    {id:'telegram', icon:'➤', title:'Telegram'},
    {id:'max', icon:'М', title:'MAX'},
  ];
  $('#messengerLinkOptions').innerHTML = providers.map(item => {
    const configured = Boolean(state.identity?.messengers?.[item.id]?.configured);
    const connected = linked.has(item.id);
    const subtitle = connected
      ? 'Уже привязан к этому профилю'
      : configured ? 'Подтвердить через официального бота' : 'Временно недоступен';
    return `<button type="button" class="messenger-link-option ${item.id} ${connected ? 'connected' : ''}" data-link-provider="${item.id}" ${connected || !configured ? 'disabled' : ''}>
      <i aria-hidden="true">${item.icon}</i>
      <span><strong>${item.title}</strong><small>${subtitle}</small></span>
      <b>${connected ? '✓' : '→'}</b>
    </button>`;
  }).join('');
}

function openMessengerLinkModal({ source = 'menu', justLinked = '' } = {}) {
  closeFunctionMenu();
  renderMessengerLinkOptions();
  const success = $('#messengerLinkSuccess');
  success.classList.remove('error');
  success.classList.toggle('hidden', !justLinked);
  success.textContent = justLinked ? `Готово — ${messengerName(justLinked)} привязан к вашему профилю.` : '';
  $('#messengerLinkDescription').textContent = linkedMessengerProviders().size
    ? 'Добавьте ещё один способ входа или проверьте уже подключённые мессенджеры.'
    : 'Сохраните доступ к анкете, выбранным обследованиям, результатам и расшифровкам на любом устройстве.';
  $('#messengerLinkLater').textContent = justLinked ? 'Готово' : 'Не сейчас';
  $('#messengerLinkModal').dataset.source = source;
  $('#messengerLinkModal').classList.remove('hidden');
  trackEvent('messenger_link_modal_viewed', {source, linked_count:linkedMessengerProviders().size});
}

async function startMessengerLink(provider, button) {
  button.disabled = true;
  const source = $('#messengerLinkModal').dataset.source || 'menu';
  try {
    trackEvent('messenger_auth_started', {provider, method:provider, context:'profile_link', source});
    const result = await api('/api/auth/messenger/start', {
      method:'POST', body:JSON.stringify({provider}),
    });
    sessionStorage.setItem(MESSENGER_LINK_PENDING_KEY, JSON.stringify({provider, source}));
    window.location.assign(result.bot_url);
  } catch (error) {
    button.disabled = false;
    const success = $('#messengerLinkSuccess');
    success.textContent = error.message;
    success.classList.remove('hidden');
    success.classList.add('error');
  }
}

function consumeCompletedMessengerLink(identity) {
  let pending = null;
  try { pending = JSON.parse(sessionStorage.getItem(MESSENGER_LINK_PENDING_KEY) || 'null'); } catch {}
  if (!pending?.provider || !(identity.providers || []).includes(pending.provider)) return;
  sessionStorage.removeItem(MESSENGER_LINK_PENDING_KEY);
  state.messengerLinkJustCompleted = pending.provider;
  trackEvent('messenger_auth_completed', {provider:pending.provider, context:'profile_link', source:pending.source || 'menu'});
}

function closeAnonymousWarning() {
  $('#anonymousWarning').classList.add('hidden');
}

function returnFromAnonymousWarning(action = 'anonymous_close') {
  trackEvent('anonymous_warning_cancelled');
  trackOnboardingAction(action, 'anonymous_warning');
  closeAnonymousWarning();
  trackOnboardingScreen('registration');
}

async function startApplication(options = {}) {
  $('#welcomeScreen').classList.add('hidden');
  $('#authGate').classList.add('hidden');
  closeAnonymousWarning();
  await loadOnboarding(options);
}

$('#welcomeNextButton').addEventListener('click', continueFromWelcome);

async function startMessengerAuth(provider) {
  trackEvent('registration_method_selected', {provider,method:provider,screen:'registration'});
  trackOnboardingAction(provider, 'registration');
  trackEvent('messenger_auth_started', {provider,method:provider});
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
$('#anonymousAuthButton').addEventListener('click', () => {
  trackEvent('registration_method_selected', {provider:'anonymous',method:'anonymous',screen:'registration'});
  trackEvent('anonymous_warning_viewed', {screen:'registration'});
  trackOnboardingAction('anonymous', 'registration');
  trackOnboardingScreen('anonymous_warning');
  $('#anonymousWarning').classList.remove('hidden');
});
$('#anonymousWarningClose').addEventListener('click', () => returnFromAnonymousWarning('anonymous_close'));
$('#anonymousWarningCancel').addEventListener('click', () => returnFromAnonymousWarning('anonymous_cancel'));
$('#anonymousWarning').addEventListener('click', event => {
  if (event.target === $('#anonymousWarning')) returnFromAnonymousWarning('anonymous_close');
});
$('#anonymousWarningConfirm').addEventListener('click', async () => {
  try {
    trackEvent('registration_method_selected', {provider:'anonymous',method:'anonymous',result:'confirmed'});
    trackOnboardingAction('anonymous_confirm', 'anonymous_warning');
    const registration = await api('/api/register-choice', {
      method:'POST',
      body:JSON.stringify({ method:'anonymous' }),
    });
    localStorage.setItem(ANONYMOUS_ACCESS_KEY, registration.chel_id || state.identity?.chel_id || '');
    await startApplication();
  } catch (error) {
    showAuthGate();
    setAuthStatus(`Не удалось открыть Консилиум: ${error.message}`, true);
  }
});

function activeOnboardingQuestions() {
  return onboardingQuestions;
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
  trackEvent('appearance_viewed', {screen:'appearance'});
  trackOnboardingScreen('appearance');
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
  const empty = Array.isArray(value) ? !value.length : !String(value ?? '').trim();
  if (question.optional && empty) return 'Пропустить';
  return isLast ? 'Завершить анкету' : 'Продолжить';
}

function companyInnControl(inputMarkup, listId) {
  return `<div class="company-inn-field">${inputMarkup}<div class="company-inn-suggestions hidden" id="${listId}" role="listbox" aria-label="Организации по ИНН"></div></div>`;
}

function setupCompanyInnSuggestions(inputSelector, listSelector) {
  const field = $(inputSelector);
  const list = $(listSelector);
  if (!field || !list || field.dataset.suggestionsReady === '1') return;
  field.dataset.suggestionsReady = '1';
  field.setAttribute('autocomplete', 'off');
  field.setAttribute('aria-autocomplete', 'list');
  field.setAttribute('aria-controls', list.id);
  field.setAttribute('aria-expanded', 'false');
  let timer = null;
  let requestNumber = 0;
  let activeIndex = -1;

  const close = () => {
    list.classList.add('hidden');
    list.innerHTML = '';
    field.setAttribute('aria-expanded', 'false');
    activeIndex = -1;
  };
  const select = button => {
    if (!button) return;
    field.value = button.dataset.inn || '';
    if (field.id === 'onboardingInput') state.onboardingAnswers.company_inn = field.value;
    field.dispatchEvent(new Event('input', {bubbles:true}));
    close();
    field.focus();
  };
  const setActive = index => {
    const buttons = [...list.querySelectorAll('[data-inn]')];
    if (!buttons.length) return;
    activeIndex = (index + buttons.length) % buttons.length;
    buttons.forEach((button, buttonIndex) => button.classList.toggle('active', buttonIndex === activeIndex));
    buttons[activeIndex].scrollIntoView({block:'nearest'});
  };

  field.addEventListener('input', () => {
    const digits = field.value.replace(/\D/g, '').slice(0, 12);
    if (field.value !== digits) field.value = digits;
    clearTimeout(timer);
    requestNumber += 1;
    const currentRequest = requestNumber;
    if (!state.publicConfig.company_suggestions_enabled || digits.length < 4) {
      close();
      return;
    }
    list.innerHTML = '<div class="company-inn-suggestion-status">Ищем организацию…</div>';
    list.classList.remove('hidden');
    field.setAttribute('aria-expanded', 'true');
    timer = setTimeout(async () => {
      try {
        const result = await api('/api/company-suggestions', {
          method:'POST', body:JSON.stringify({query:digits}),
        });
        if (currentRequest !== requestNumber || field.value !== digits) return;
        const suggestions = Array.isArray(result.suggestions) ? result.suggestions : [];
        list.innerHTML = suggestions.length
          ? suggestions.map(item => `<button type="button" class="company-inn-suggestion" role="option" data-inn="${escapeAttr(item.inn)}"><strong>${escapeHtml(item.name)}</strong><small>ИНН ${escapeHtml(item.inn)}</small></button>`).join('')
          : '<div class="company-inn-suggestion-status">Совпадений не найдено. Можно ввести ИНН вручную.</div>';
        list.classList.remove('hidden');
        field.setAttribute('aria-expanded', 'true');
      } catch {
        if (currentRequest !== requestNumber) return;
        close();
      }
    }, 320);
  });
  field.addEventListener('keydown', event => {
    const buttons = [...list.querySelectorAll('[data-inn]')];
    if (event.key === 'ArrowDown' && buttons.length) {
      event.preventDefault(); setActive(activeIndex + 1);
    } else if (event.key === 'ArrowUp' && buttons.length) {
      event.preventDefault(); setActive(activeIndex - 1);
    } else if (event.key === 'Enter' && activeIndex >= 0) {
      event.preventDefault(); select(buttons[activeIndex]);
    } else if (event.key === 'Escape') close();
  });
  list.addEventListener('pointerdown', event => event.preventDefault());
  list.addEventListener('click', event => select(event.target.closest('[data-inn]')));
  field.addEventListener('blur', () => setTimeout(close, 150));
}

function renderQuestion() {
  const questions = activeOnboardingQuestions();
  state.onboardingStep = Math.max(0, Math.min(state.onboardingStep, questions.length - 1));
  const question = questions[state.onboardingStep];
  const value = state.onboardingAnswers[question.key] ?? '';
  questionShownAt = performance.now();
  if (state.onboardingStep === 0) trackEvent('questionnaire_started', {screen:'questionnaire'});
  trackEvent('question_viewed', {
    question_key:question.key, step_number:state.onboardingStep + 1,
    optional:Boolean(question.optional), screen:'questionnaire',
  });
  trackOnboardingScreen(`question_${question.key}`);
  setOnboardingMeta('Анкета', 5 + Math.round((state.onboardingStep / questions.length) * 60));
  let control = question.choices
    ? `<div class="choice-grid">${question.choices.map(([id,label]) => `<button type="button" class="choice-button ${value === id ? 'selected' : ''}" data-choice="${id}">${label}</button>`).join('')}</div>`
    : question.type === 'textarea'
      ? `<textarea class="onboarding-input onboarding-input-area" id="onboardingInput" placeholder="${escapeAttr(question.placeholder || '')}" ${question.maxlength ? `maxlength="${question.maxlength}"` : ''}>${escapeHtml(Array.isArray(value) ? value.join('\n') : value)}</textarea>`
      : `<input class="onboarding-input" id="onboardingInput" type="${question.type || 'text'}" value="${escapeAttr(value)}" placeholder="${escapeAttr(question.placeholder || '')}" ${question.inputmode ? `inputmode="${question.inputmode}"` : ''} ${question.maxlength ? `maxlength="${question.maxlength}"` : ''} ${question.min !== undefined ? `min="${question.min}"` : ''} ${question.max !== undefined ? `max="${question.max}"` : ''} ${question.step ? `step="${question.step}"` : ''}>`;
  if (question.key === 'company_inn') control = companyInnControl(control, 'onboardingCompanyInnSuggestions');
  const notMedicalExam = question.key === 'company_inn'
    ? '<button type="button" class="not-medical-exam-button" data-onboarding-action="skip-medical-exam">Я не на мед-осмотр</button>'
    : '';
  $('#onboardingContent').innerHTML = `<span class="onboarding-kicker">Шаг ${state.onboardingStep + 1} из ${questions.length}</span><h1>${question.title}</h1><p class="onboarding-lead">${question.lead}</p>${control}<div class="onboarding-actions">${state.onboardingStep ? '<button type="button" class="onboarding-back" data-onboarding-action="back">Назад</button>' : ''}<button type="button" class="onboarding-next" data-onboarding-action="next">${onboardingNextLabel(question, value, state.onboardingStep === questions.length - 1)}</button></div>${notMedicalExam}`;
  if (question.key === 'company_inn') setupCompanyInnSuggestions('#onboardingInput', '#onboardingCompanyInnSuggestions');
  $('#onboardingInput')?.focus();
}

function captureQuestionAnswer({ trackAction = true } = {}) {
  const question = activeOnboardingQuestions()[state.onboardingStep];
  if (!question.choices) {
    let value = $('#onboardingInput').value.trim();
    if (question.key === 'company_inn' && value !== '123123' && !/^\d{10}(?:\d{2})?$/.test(value)) {
      throw new Error('Введите ИНН предприятия: 10 или 12 цифр');
    }
    if (question.list) value = value.split('\n').map(item => item.trim()).filter(Boolean);
    if (!question.optional && (value === '' || (Array.isArray(value) && !value.length))) throw new Error('Ответьте на вопрос, чтобы продолжить');
    if (question.type === 'number' && value !== '') {
      const number = Number(value);
      if (!Number.isFinite(number) || number < question.min || number > question.max) throw new Error(`Введите значение от ${question.min} до ${question.max}`);
      if (question.key === 'age' && !Number.isInteger(number)) throw new Error('Введите возраст целым числом');
      value = number;
    }
    state.onboardingAnswers[question.key] = value;
  } else if (!state.onboardingAnswers[question.key]) {
    throw new Error('Выберите один из вариантов');
  }
  const stored = state.onboardingAnswers[question.key];
  const empty = Array.isArray(stored) ? !stored.length : !String(stored ?? '').trim();
  trackEvent(empty ? 'question_skipped' : 'question_answered', {
    question_key:question.key, step_number:state.onboardingStep + 1,
    optional:Boolean(question.optional), duration_ms:Math.round(performance.now() - questionShownAt),
  });
  if (trackAction) trackOnboardingAction(empty ? 'skip' : 'answer', `question_${question.key}`);
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
      company_inn:'', preferred_name:'', age:'', sex:'', height_cm:'', weight_kg:'', pregnancy:'not_applicable',
      conditions:[], medications:[], allergies:[], smoking:'unknown', alcohol:'unknown', activity:'unknown',
      blood_pressure:'unknown', blood_sugar:'unknown', dark_in_eyes:'unknown', joint_pain:'unknown', fatigue:'unknown', notes:'',
      ...state.onboardingAnswers,
    };
    state.onboarding = await api('/api/onboarding/profile', { method:'POST', body:JSON.stringify(payload) });
    state.profile = state.onboarding.profile;
    renderExamOffer();
  } catch (error) {
    const question = activeOnboardingQuestions()[state.onboardingStep];
    trackEvent('question_validation_error', {
      question_key:question?.key || '', step_number:state.onboardingStep + 1,
      error_code:'invalid_or_missing',
    });
    showOnboardingError(error.message);
  }
}

async function skipMedicalExam() {
  const button = $('#onboardingContent [data-onboarding-action="skip-medical-exam"]');
  if (button) button.disabled = true;
  try {
    trackOnboardingAction('not_medical_exam', 'question_company_inn');
    state.onboarding = await api('/api/onboarding/not-medical-exam', { method:'POST', body:'{}' });
    state.profile = state.onboarding.profile;
    await openMainApp();
  } catch (error) {
    if (button) button.disabled = false;
    showOnboardingError(error.message);
  }
}

const EXAMINATION_AUDIENCES = {
  fatigue_basic:'Тем, кого беспокоят слабость, сонливость или снижение работоспособности.',
  fatigue_extended:'Тем, у кого усталость сохраняется длительно или сочетается с другими жалобами.',
  weight_basic:'Тем, кто хочет разобраться в возможных обменных причинах набора веса.',
  weight_extended:'Тем, кому нужна более широкая оценка гормональных и обменных факторов веса.',
  hair_loss:'При заметном выпадении волос, ломкости и подозрении на дефициты.',
  lipids:'Для оценки сердечно-сосудистого риска, особенно при повышенном давлении или лишнем весе.',
  liver_basic:'Для базовой проверки показателей печени и поджелудочной железы.',
  liver_extended:'При необходимости более широкой оценки печени, поджелудочной и желчевыводящих путей.',
  iron:'При утомляемости, слабости, бледности или подозрении на дефицит железа.',
  kidneys:'Для базовой оценки функции почек и азотистого обмена.',
  protein:'Для оценки белкового обмена, питания и синтетической функции печени.',
  joints:'При боли, скованности или отёчности суставов.',
  inflammation:'Когда важно дополнительно оценить наличие воспалительной реакции.',
  thyroid:'При изменениях веса, утомляемости, сердцебиении или других возможных признаках нарушения функции щитовидной железы.',
  female_hormones:'Женщинам при наличии показаний к оценке гормонального фона; сроки сдачи важно обсудить с врачом.',
  male_health:'Мужчинам для оценки гормонального фона и показателей предстательной железы с учётом возраста и показаний.',
  cortisol:'При длительном стрессе и связанных с ним жалобах; показатель зависит от времени сдачи.',
  vitamin_d:'Тем, кому важно узнать уровень витамина D и обсудить необходимость коррекции.',
  ferritin:'Для оценки запасов железа, особенно при слабости или выпадении волос.',
  ca125:'Только при наличии врачебных показаний; онкомаркер не подходит для самостоятельной диагностики.',
  ca153:'Только при наличии врачебных показаний; онкомаркер не подходит для самостоятельной диагностики.',
  ca199:'Только при наличии врачебных показаний; онкомаркер не подходит для самостоятельной диагностики.',
};

function renderExamOffer() {
  trackEvent('examinations_offer_viewed', { screen:'examinations_offer' });
  trackOnboardingScreen('exam_offer');
  setOnboardingMeta('Обследования', 72);
  $('#onboardingContent').innerHTML = `
    <span class="onboarding-kicker">После анкеты</span>
    <h1>Дополнительные обследования</h1>
    <blockquote class="exam-offer-quote"><strong>Давайте честно: здоровых людей не бывает.</strong><span>У каждого есть своё слабое место, и лучше бы его знать.<br>Пара быстрых обследований — и жить спокойнее.</span></blockquote>
    <div class="exam-offer-copy">
      <p>Чтобы получить более полную информацию о состоянии своего здоровья, вы можете пройти дополнительные обследования во время медосмотра.</p>
      <p class="exam-relative-note"><b>Можно пригласить родственника или друга</b> пройти один или несколько чек-апов. Позаботьтесь о близких — отправьте им ссылку на сервис.</p>
    </div>
    <button type="button" class="exam-catalog-button" data-onboarding-action="open-exam-catalog-info"><span>◫</span><span><strong>Посмотреть описания чек-апов</strong><small>Что входит, кому и для чего они нужны</small></span><b>→</b></button>
    <div class="exam-offer-question"><strong>Хотели бы вы сдать дополнительные анализы во время медосмотра на работе?</strong><small>Выберите соответствующий вариант.</small></div>
    <div class="onboarding-actions exam-offer-actions"><button type="button" class="onboarding-next" data-onboarding-action="start-exams">Да, выбрать анализы</button><button type="button" class="exam-decline-button" data-onboarding-action="review-exam-skip">Нет, не сейчас</button></div>
    <button type="button" class="exam-edit-profile" data-onboarding-action="question-back">← Изменить ответы анкеты</button>`;
}

function renderExamCatalogInfo() {
  trackOnboardingScreen('exam_catalog');
  setOnboardingMeta('Описание чек-апов', 76);
  const tests = state.onboarding?.tests || [];
  const cards = tests.map(test => `
    <article class="exam-info-card">
      <header><strong>${escapeHtml(test.name)}</strong><b>${Number(test.price || 0).toLocaleString('ru')} ₽</b></header>
      <p><span>Кому подходит</span>${escapeHtml(EXAMINATION_AUDIENCES[test.id] || test.description || 'Тем, кто хочет получить больше информации о состоянии здоровья.')}</p>
      <p><span>Для чего</span>${escapeHtml(test.description || 'Для дополнительной оценки показателей здоровья.')}</p>
      <p class="exam-info-includes"><span>Что входит</span>${escapeHtml(test.includes || 'Состав уточняется')}</p>
    </article>`).join('');
  $('#onboardingContent').innerHTML = `
    <div class="exam-info-screen">
      <span class="onboarding-kicker">Доступные чек-апы</span>
      <h1>Что можно проверить</h1>
      <p class="onboarding-lead">Краткое описание поможет сориентироваться. Необходимость обследований и интерпретацию результатов лучше обсуждать с врачом.</p>
      <div class="exam-info-list">${cards}</div>
      <div class="exam-info-actions"><button type="button" class="onboarding-next" data-onboarding-action="start-exams">Выбрать анализы</button><button type="button" class="onboarding-back" data-onboarding-action="close-exam-catalog-info">Вернуться к вопросу</button></div>
    </div>`;
}

const EXAMINATION_UPGRADE_PAIRS = {
  fatigue_basic:'fatigue_extended',
  weight_basic:'weight_extended',
  liver_basic:'liver_extended',
};
const EXAMINATION_BASIC_BY_EXTENDED = Object.fromEntries(
  Object.entries(EXAMINATION_UPGRADE_PAIRS).map(([basicId, extendedId]) => [extendedId, basicId]),
);

function normalizeSelectedTestPairs() {
  Object.entries(EXAMINATION_UPGRADE_PAIRS).forEach(([basicId, extendedId]) => {
    if (state.selectedTests.has(extendedId)) state.selectedTests.delete(basicId);
  });
}

function selectExamination(id) {
  const extendedId = EXAMINATION_UPGRADE_PAIRS[id];
  if (extendedId && state.selectedTests.has(extendedId)) return { changed:false, removing:false };
  const removing = state.selectedTests.has(id);
  if (removing) state.selectedTests.delete(id);
  else {
    const basicId = EXAMINATION_BASIC_BY_EXTENDED[id];
    if (basicId) state.selectedTests.delete(basicId);
    state.selectedTests.add(id);
  }
  return { changed:true, removing };
}

function renderExamSelection(scrollPosition = null) {
  trackOnboardingScreen('exam_selection');
  normalizeSelectedTestPairs();
  setOnboardingMeta('Обследования', 80);
  const recommended = new Set(state.onboarding.recommended_test_ids || []);
  const cards = state.onboarding.tests.map(test => {
    const selected = state.selectedTests.has(test.id);
    const extendedId = EXAMINATION_UPGRADE_PAIRS[test.id];
    const disabled = Boolean(extendedId && state.selectedTests.has(extendedId));
    const extended = extendedId
      ? state.onboarding.tests.find(item => item.id === extendedId)
      : null;
    const disabledNote = disabled
      ? `<small class="exam-upgrade-note">Уже входит в «${escapeHtml(extended?.name || 'Расширенный комплекс')}»</small>`
      : '';
    return `<label class="exam-card ${selected ? 'selected' : ''} ${disabled ? 'disabled-by-upgrade' : ''}" data-test-card="${test.id}" ${disabled ? 'aria-disabled="true"' : ''}><input type="checkbox" ${selected ? 'checked' : ''} ${disabled ? 'disabled' : ''}><span class="exam-check">✓</span>${recommended.has(test.id) ? '<small class="recommended-badge">Подходит по анкете</small>' : ''}<strong>${escapeHtml(test.name)}</strong><b>${Number(test.price).toLocaleString('ru')} ₽</b><small>${escapeHtml(test.description)}</small><em>${escapeHtml(test.includes)}</em>${disabledNote}</label>`;
  }).join('');
  const total = state.onboarding.tests.filter(test => state.selectedTests.has(test.id)).reduce((sum,test) => sum + test.price, 0);
  $('#onboardingContent').innerHTML = `<span class="onboarding-kicker">Выбор анализов</span><h1>Выберите интересующие наборы</h1><p class="onboarding-lead">Рекомендации отмечены по ответам анкеты и не являются назначением.</p><div class="exam-list">${cards}</div><div class="exam-total"><span>Выбрано: ${state.selectedTests.size}</span><strong>${total.toLocaleString('ru')} ₽</strong></div><div class="onboarding-actions"><button type="button" class="onboarding-back" data-onboarding-action="exam-offer">Назад</button><button type="button" class="onboarding-next" data-onboarding-action="continue-payment" ${state.selectedTests.size ? '' : 'disabled'}>Далее</button></div><button type="button" class="exam-skip" data-onboarding-action="review-exam-skip">Ничего не выбирать</button>`;
  if (scrollPosition) {
    const examList = $('#onboardingContent .exam-list');
    const onboarding = $('#onboarding');
    examList.scrollTop = scrollPosition.examList || 0;
    onboarding.scrollTop = scrollPosition.onboarding || 0;
    requestAnimationFrame(() => {
      examList.scrollTop = scrollPosition.examList || 0;
      onboarding.scrollTop = scrollPosition.onboarding || 0;
    });
  }
}

function renderExamSkipConfirmation() {
  trackEvent('examinations_objection_viewed', { screen:'examinations_skip' });
  trackOnboardingScreen('exam_objection');
  setOnboardingMeta('Обследования', 76);
  $('#onboardingContent').innerHTML = `
    <span class="onboarding-kicker">Перед тем как продолжить</span>
    <h1>После обследований вы получите больше, чем результаты</h1>
    <div class="exam-objection-intro">
      <p>Врач высшей категории <strong>Татьяна Витальевна</strong> подготовит подробную расшифровку сложных показателей.</p>
      <p>И самое главное — вы получите <strong>бесплатную консультацию</strong> по результатам.</p>
      <p>Всё будет доступно в этом сервисе — без очередей и доплат за расшифровку.</p>
    </div>
    <ul class="exam-benefits">
      <li><span>✓</span><div><strong>Ничего дополнительно делать не нужно</strong><small>Выберите обследования сейчас, а в день медосмотра сдайте всё вместе.</small></div></li>
      <li><span>✓</span><div><strong>Один визит вместо отдельной поездки</strong><small>Вы уже будете на осмотре — дополнительные анализы можно сдать за один раз.</small></div></li>
      <li><span>✓</span><div><strong>Бесплатная консультация специалиста</strong><small>После готовности дополнительных анализов врач высшей категории поможет разобраться в результатах.</small></div></li>
      <li><span>✓</span><div><strong>Не придётся записываться отдельно</strong><small>Если отложить обследования, позже могут потребоваться отдельная запись и поездка.</small></div></li>
    </ul>
    <p class="exam-benefits-note">Дополнительные обследования добровольны — окончательное решение остаётся за вами.</p>
    <div class="onboarding-actions skip-decision-actions"><button type="button" class="onboarding-next" data-onboarding-action="start-exams">Выбрать обследования</button><button type="button" class="exam-refuse" data-onboarding-action="confirm-skip-exams">Всё равно отказаться</button></div>`;
}

async function submitExamSelection(skip = false) {
  try {
    state.onboarding = await api('/api/onboarding/exams', { method:'POST', body:JSON.stringify({ selected_tests:skip ? [] : [...state.selectedTests] }) });
    window.consiliumMetrikaGoal?.('exam_selection_completed');
    if (skip) {
      window.consiliumMetrikaGoal?.('onboarding_completed');
      if (state.returnToChatAfterExaminations) return openMainApp({ skipIntro:true });
      renderExamSkipCompletion();
      return;
    }
    renderPayment();
  } catch (error) { showOnboardingError(error.message); }
}

function selectedTestDetails() {
  return state.onboarding.tests.filter(test => state.onboarding.selected_tests.includes(test.id));
}

function renderCurrentExamSelectionSummary() {
  setOnboardingMeta('Обследования', 100);
  const selected = selectedTestDetails();
  const total = selected.reduce((sum,test) => sum + test.price, 0);
  const paymentLabels = {
    paid_online:'Оплачено онлайн',
    pay_at_exam:'Оплата на медосмотре',
    demo_paid:'Оплачено',
    pending:'Способ оплаты ещё не выбран',
    skipped:'Обследования не выбраны',
  };
  const paymentLabel = paymentLabels[state.onboarding?.payment_status] || 'Статус оплаты не указан';
  const selection = selected.length
    ? `<div class="current-exams-list">${selected.map(test => `
        <article class="current-exam-item">
          <div><strong>${escapeHtml(test.name)}</strong><small>${escapeHtml(test.description || '')}</small></div>
          <b>${Number(test.price || 0).toLocaleString('ru')} ₽</b>
        </article>`).join('')}</div>
       <div class="current-exams-total"><span>${escapeHtml(paymentLabel)}</span><strong>Итого: ${total.toLocaleString('ru')} ₽</strong></div>`
    : '<div class="current-exams-empty"><span aria-hidden="true">◫</span><strong>Дополнительные обследования пока не выбраны</strong><p>Вы можете выбрать подходящие наборы сейчас или вернуться к этому позже.</p></div>';
  $('#onboardingContent').innerHTML = `
    <div class="current-exams-summary">
      <span class="onboarding-kicker">Ваш выбор</span>
      <h1>${selected.length ? 'У вас выбраны обследования' : 'Ваши обследования'}</h1>
      <p class="onboarding-lead">Проверьте сохранённый выбор. Его можно изменить, не проходя анкету заново.</p>
      ${selection}
      <div class="current-exams-actions">
        <button type="button" class="onboarding-next" data-onboarding-action="edit-current-exams">${selected.length ? 'Изменить выбор' : 'Выбрать обследования'}</button>
        <button type="button" class="current-exams-close" data-onboarding-action="close-current-exams">Закрыть</button>
      </div>
    </div>`;
}

function renderPayment() {
  trackEvent('payment_viewed', { screen:'payment', selected_count:selectedTestDetails().length });
  trackOnboardingScreen('payment');
  setOnboardingMeta('Оплата', 92);
  const selected = selectedTestDetails();
  const total = selected.reduce((sum,test) => sum + test.price, 0);
  const emailField = state.publicConfig.online_payments_enabled && state.publicConfig.payment_receipt_email_required
    ? `<label class="payment-email-label">Электронная почта для онлайн-чека <span>только при оплате онлайн</span><input id="paymentReceiptEmail" type="email" autocomplete="email" maxlength="254" placeholder="name@example.ru" required></label>` : '';
  const exitAction = state.paymentReviewSource === 'purchases'
    ? '<button type="button" class="payment-back-button" data-onboarding-action="close-payment-review">Закрыть</button>'
    : '<button type="button" class="payment-back-button" data-onboarding-action="back-to-exams">← Вернуться к обследованиям</button>';
  $('#onboardingContent').innerHTML = `<span class="onboarding-kicker">Последний шаг</span><h1>Проверим заказ</h1><p class="onboarding-lead">Выберите, как вам будет удобнее оплатить дополнительные обследования.</p><div class="payment-stub"><span class="demo-badge">ВЫБРАННЫЕ ОБСЛЕДОВАНИЯ</span><ul class="payment-lines">${selected.map(test => `<li><span>${test.name}</span><strong>${test.price.toLocaleString('ru')} ₽</strong></li>`).join('')}</ul><div class="payment-total"><span>Итого</span><strong>${total.toLocaleString('ru')} ₽</strong></div></div>${emailField}<div class="payment-actions"><button type="button" class="payment-online-button" data-onboarding-action="pay-online">Оплатить онлайн</button><button type="button" class="payment-at-exam-button" data-onboarding-action="pay-at-exam">Оплатить на медосмотре</button></div>${exitAction}`;
}

function setPaymentActionsBusy(busy, activeLabel = '') {
  const controls = [...document.querySelectorAll('#onboardingContent [data-onboarding-action="pay-online"], #onboardingContent [data-onboarding-action="pay-at-exam"], #onboardingContent [data-onboarding-action="back-to-exams"], #onboardingContent [data-onboarding-action="close-payment-review"]')];
  controls.forEach(control => { control.disabled = Boolean(busy); });
  const online = $('#onboardingContent [data-onboarding-action="pay-online"]');
  const atExam = $('#onboardingContent [data-onboarding-action="pay-at-exam"]');
  if (online) online.textContent = busy && activeLabel === 'online' ? 'Открываем оплату…' : 'Оплатить онлайн';
  if (atExam) atExam.textContent = busy && activeLabel === 'at_exam' ? 'Сохраняем…' : 'Оплатить на медосмотре';
}

async function startOnlinePayment() {
  if (!state.publicConfig.online_payments_enabled) {
    showOnlinePaymentUnavailable();
    return;
  }
  const receiptInput = $('#paymentReceiptEmail');
  if (receiptInput && !receiptInput.checkValidity()) {
    receiptInput.reportValidity();
    return;
  }
  const receiptEmail = receiptInput?.value?.trim() || '';
  setPaymentActionsBusy(true, 'online');
  try {
    const result = await api('/api/payments/yookassa/create', {
      method:'POST', body:JSON.stringify({
        receipt_email:receiptEmail,
        return_to_chat:Boolean(state.returnToChatAfterExaminations),
        payment_source:state.paymentReviewSource || 'onboarding',
      }),
    });
    const url = result.order?.confirmation_url;
    window.consiliumMetrikaGoal?.('payment_online');
    if (url) {
      trackEvent('payment_redirected', {provider:'yookassa'});
      localStorage.setItem(PAYMENT_PENDING_ORDER_KEY, JSON.stringify({
        id:result.order.id,
        returnToChat:Boolean(state.returnToChatAfterExaminations),
        source:state.paymentReviewSource || 'onboarding',
        receiptEmail,
        savedAt:Date.now(),
      }));
      window.location.assign(url);
      return;
    }
    if (!result.order?.id) throw new Error('ЮKassa не вернула данные платежа');
    const returnUrl = new URL(location.href);
    returnUrl.searchParams.set('payment_return', result.order.id);
    if (state.returnToChatAfterExaminations) returnUrl.searchParams.set('return_to_chat', '1');
    returnUrl.searchParams.set('payment_source', state.paymentReviewSource || 'onboarding');
    history.replaceState({}, '', returnUrl);
    await handlePaymentReturn();
  } catch (error) {
    setPaymentActionsBusy(false);
    showOnboardingError(error.message);
  }
}

function showOnlinePaymentUnavailable() {
  document.querySelector('.payment-unavailable-backdrop')?.remove();
  const backdrop = document.createElement('div');
  backdrop.className = 'payment-unavailable-backdrop';
  backdrop.setAttribute('role', 'dialog');
  backdrop.setAttribute('aria-modal', 'true');
  backdrop.setAttribute('aria-labelledby', 'paymentUnavailableTitle');
  backdrop.innerHTML = `
    <div class="payment-unavailable-card">
      <span class="payment-unavailable-icon" aria-hidden="true">⌛</span>
      <h2 id="paymentUnavailableTitle">Онлайн-оплата временно недоступна</h2>
      <p>Мы уже работаем над её подключением. Пока вы можете выбрать оплату на медицинском осмотре.</p>
      <button type="button" data-close-payment-unavailable>Понятно</button>
    </div>`;
  const close = () => { trackOnboardingAction('close', 'payment_unavailable'); backdrop.remove(); trackOnboardingScreen('payment'); };
  backdrop.addEventListener('click', event => {
    if (event.target === backdrop || event.target.closest('[data-close-payment-unavailable]')) close();
  });
  document.body.appendChild(backdrop);
  backdrop.querySelector('button').focus();
  trackEvent('payment_unavailable_viewed', {provider:'yookassa'});
  trackOnboardingScreen('payment_unavailable');
}

async function handlePaymentReturn() {
  const params = new URLSearchParams(location.search);
  let pendingOrder = {};
  try { pendingOrder = JSON.parse(localStorage.getItem(PAYMENT_PENDING_ORDER_KEY) || '{}'); } catch {}
  const orderId = params.get('payment_return') || pendingOrder.id;
  if (!orderId) return false;
  // A saved source reflects the place where the user most recently launched
  // this redirect. It intentionally has priority over an older return_url
  // stored inside an already-created YooKassa payment.
  state.paymentReviewSource = pendingOrder.source || params.get('payment_source') || '';
  state.paymentReviewOrderId = orderId;
  state.paymentReceiptEmail = String(pendingOrder.receiptEmail || '').trim();
  state.returnToChatAfterExaminations = Boolean(
    pendingOrder.returnToChat || params.get('return_to_chat') === '1' || state.paymentReviewSource === 'purchases'
  );
  const clearPaymentReturn = () => {
    localStorage.removeItem(PAYMENT_PENDING_ORDER_KEY);
    history.replaceState({}, '', `${location.pathname}${location.hash || ''}`);
  };
  $('#onboarding').classList.remove('hidden');
  $('#appShell').classList.add('hidden');
  setOnboardingMeta('Проверка оплаты', 96);
  trackEvent('payment_return_viewed', {provider:'yookassa'});
  trackOnboardingScreen('payment_processing');
  $('#onboardingContent').innerHTML = `<div class="payment-result"><span class="payment-result-icon">⌛</span><h1>Проверяем оплату</h1><p class="onboarding-lead">Обычно это занимает несколько секунд. Не закрывайте страницу.</p></div>`;
  try {
    let result;
    for (let attempt = 0; attempt < 8; attempt += 1) {
      result = await api(`/api/payments/${encodeURIComponent(orderId)}`);
      const currentStatus = result.order?.status;
      if (['succeeded','canceled','abandoned'].includes(currentStatus)) break;
      if (attempt < 7) await new Promise(resolve => setTimeout(resolve, 1500));
    }
    let status = result.order?.status;
    if (status === 'succeeded' && result.order?.paid) {
      clearPaymentReturn();
      state.onboarding = await api('/api/onboarding');
      state.profile = state.onboarding.profile;
      state.selectedTests = new Set(state.onboarding.selected_tests || []);
      trackEvent('payment_succeeded', {provider:'yookassa'});
      trackEvent('payment_completed', {provider:'yookassa', result:'succeeded'});
      window.consiliumMetrikaGoal?.('onboarding_completed');
      renderPaymentSuccess(result.order);
      return true;
    }
    if (['pending','waiting_for_capture'].includes(status)) {
      result = await api(`/api/payments/${encodeURIComponent(orderId)}/abandon`, {method:'POST', body:'{}'});
      status = result.order?.status;
      if (status === 'succeeded' && result.order?.paid) {
        clearPaymentReturn();
        state.onboarding = await api('/api/onboarding');
        state.profile = state.onboarding.profile;
        state.selectedTests = new Set(state.onboarding.selected_tests || []);
        trackEvent('payment_succeeded', {provider:'yookassa'});
        trackEvent('payment_completed', {provider:'yookassa', result:'succeeded'});
        renderPaymentSuccess(result.order);
        return true;
      }
    }
    const canceled = status === 'canceled';
    const abandoned = status === 'abandoned';
    if (canceled || abandoned) clearPaymentReturn();
    trackEvent(canceled ? 'payment_canceled' : abandoned ? 'payment_abandoned' : 'payment_pending', {provider:'yookassa'});
    trackEvent('payment_result_viewed', {provider:'yookassa', status});
    trackOnboardingScreen('payment_result');
    const title = canceled ? 'Оплата не прошла' : abandoned ? 'Оплата не завершена' : 'Оплата ещё обрабатывается';
    const message = canceled
      ? 'ЮKassa отменила платёж. Можно попробовать ещё раз или выбрать оплату на медосмотре.'
      : abandoned
        ? 'Вы вернулись без подтверждённой оплаты. Попытка сохранена в разделе «Мои покупки».'
        : 'ЮKassa ещё не подтвердила платёж. Подождите немного и проверьте снова.';
    $('#onboardingContent').innerHTML = `<div class="payment-result"><span class="payment-result-icon ${(canceled || abandoned) ? 'error' : ''}">${(canceled || abandoned) ? '!' : '⌛'}</span><h1>${title}</h1><p class="onboarding-lead">${message}</p><div class="onboarding-actions payment-result-actions"><button type="button" class="onboarding-back" data-onboarding-action="back-to-payment">Вернуться к оплате</button><button type="button" class="onboarding-next" data-onboarding-action="open-purchases">Мои покупки</button><button type="button" class="payment-result-back" data-onboarding-action="leave-payment-result">Назад</button></div></div>`;
    return true;
  } catch (error) {
    $('#onboardingContent').innerHTML = `<div class="payment-result"><span class="payment-result-icon error">!</span><h1>Не удалось проверить оплату</h1><p class="onboarding-lead">${escapeHtml(error.message)}</p><button type="button" class="onboarding-next" data-onboarding-action="check-payment" data-order-id="${escapeHtml(orderId)}">Повторить проверку</button></div>`;
    return true;
  }
}

async function restorePaymentReviewState() {
  state.onboarding = await api('/api/onboarding');
  state.profile = state.onboarding.profile;
  state.selectedTests = new Set(state.onboarding.selected_tests || []);
  if (!state.selectedTests.size) throw new Error('В заказе не найдены выбранные обследования');
}

async function returnToOnlinePayment() {
  const buttons = [...document.querySelectorAll('#onboardingContent [data-onboarding-action]')];
  buttons.forEach(button => { button.disabled = true; });
  try {
    await restorePaymentReviewState();
    trackOnboardingAction('retry', 'payment_result');
    renderPayment();
    const receiptInput = $('#paymentReceiptEmail');
    if (receiptInput && state.paymentReceiptEmail) receiptInput.value = state.paymentReceiptEmail;
    if (receiptInput && !receiptInput.value.trim()) {
      receiptInput.focus();
      showOnboardingError('Укажите электронную почту для чека, затем нажмите «Оплатить онлайн».');
      return;
    }
    await startOnlinePayment();
  } catch (error) {
    buttons.forEach(button => { button.disabled = false; });
    showOnboardingError(error.message);
  }
}

async function leavePaymentResult() {
  const source = state.paymentReviewSource;
  const returnToChat = state.returnToChatAfterExaminations;
  const orderId = state.paymentReviewOrderId;
  const buttons = [...document.querySelectorAll('#onboardingContent [data-onboarding-action]')];
  buttons.forEach(button => { button.disabled = true; });
  try {
    trackOnboardingAction('back', 'payment_result');
    if (source === 'purchases') {
      await openMainApp({skipIntro:true});
      state.paymentReviewSource = '';
      state.paymentReviewOrderId = '';
      state.paymentReceiptEmail = '';
      await openPurchases({highlightOrderId:orderId});
    } else if (returnToChat) {
      state.paymentReviewSource = '';
      state.paymentReviewOrderId = '';
      state.paymentReceiptEmail = '';
      await openMainApp({skipIntro:true});
    } else {
      await restorePaymentReviewState();
      state.paymentReviewSource = '';
      state.paymentReviewOrderId = '';
      state.paymentReceiptEmail = '';
      renderExamSelection();
    }
  } catch (error) {
    buttons.forEach(button => { button.disabled = false; });
    showOnboardingError(error.message);
  }
}

function renderPaymentSuccess(order) {
  const orderNumber = String(order?.id || state.paymentReviewOrderId || '').slice(-8).toUpperCase();
  trackEvent('payment_success_viewed', {provider:'yookassa'});
  trackOnboardingScreen('payment_success');
  setOnboardingMeta('Оплачено', 100);
  $('#onboardingContent').innerHTML = `
    <div class="payment-success-result">
      <span class="payment-result-icon success" aria-hidden="true">✓</span>
      <span class="onboarding-kicker">Оплата подтверждена</span>
      <h1>Всё получилось!</h1>
      <p class="onboarding-lead">ЮKassa подтвердила оплату${orderNumber ? ` заказа № ${escapeHtml(orderNumber)}` : ''}. Выбранные обследования сохранены.</p>
      <section class="payment-find-guide">
        <strong>Где потом найти оплату</strong>
        <ol><li>Откройте чат Консилиума.</li><li>Нажмите кнопку меню <b>☰</b> справа вверху.</li><li>Выберите <b>«Мои покупки»</b> — там будут сумма, дата, состав заказа и статус «Оплачено».</li></ol>
        <p>Успешная покупка хранится в истории и не удаляется. Электронный чек придёт на указанную при оплате почту.</p>
      </section>
      <div class="onboarding-actions payment-success-actions">
        <button type="button" class="onboarding-next" data-onboarding-action="open-purchases-after-payment">Открыть мои покупки</button>
        <button type="button" class="onboarding-back" data-onboarding-action="continue-after-payment">Перейти в чат</button>
      </div>
    </div>`;
}

async function finishPaymentSuccess(openHistory = false) {
  const orderId = state.paymentReviewOrderId;
  if (!state.onboarding?.intro_seen) {
    state.onboarding = await api('/api/onboarding/intro-seen', {method:'POST', body:'{}'});
  }
  await openMainApp({skipIntro:true});
  if (openHistory) await openPurchases({highlightOrderId:orderId});
  state.paymentReviewSource = '';
  state.paymentReviewOrderId = '';
  state.paymentReceiptEmail = '';
}

async function confirmPaymentAtExam() {
  setPaymentActionsBusy(true, 'at_exam');
  try {
    const returnDirectlyToChat = state.returnToChatAfterExaminations;
    state.onboarding = await api('/api/onboarding/payment', { method:'POST', body:JSON.stringify({method:'at_exam'}) });
    window.consiliumMetrikaGoal?.('payment_at_exam');
    window.consiliumMetrikaGoal?.('onboarding_completed');
    if (returnDirectlyToChat) return openMainApp({ skipIntro:true });
    renderExamCompletion();
  } catch (error) { setPaymentActionsBusy(false); showOnboardingError(error.message); }
}

function renderExamCompletion() {
  trackEvent('completion_viewed', { screen:'exam_completion' });
  trackOnboardingScreen('completion');
  setOnboardingMeta('Готово', 100);
  const messengerOffer = state.identity?.authenticated ? '' : `
    <section class="exam-messenger-offer">
      <span class="exam-messenger-offer-icon" aria-hidden="true">↗</span>
      <div><strong>Сохраните результаты и расшифровки</strong><p>Привяжите Telegram или MAX, чтобы вернуться к анкете, выбранным обследованиям и готовым результатам с другого устройства.</p></div>
      <button type="button" data-onboarding-action="link-messenger-after-exams">Привязать мессенджер</button>
    </section>`;
  $('#onboardingContent').innerHTML = `
    <div class="exam-completion">
      <div class="exam-completion-mark" aria-hidden="true">🎉</div>
      <span class="onboarding-kicker">Обследования выбраны</span>
      <h1>Отлично! Вы выбрали дополнительные обследования.</h1>

      <div class="exam-completion-copy">
        <p>В день медицинского осмотра наша бригада сообщит вам <strong>индивидуальный номер пробирки</strong>.</p>
        <p>Чтобы получить результаты анализов, достаточно будет ввести этот номер в соответствующее поле нашего сервиса.</p>

        <div class="exam-tube-note">
          <span aria-hidden="true">№</span>
          <p>Сейчас у вас этого номера еще нет — это нормально. Когда он появится, просто напишите в чат нашему менеджеру. Он подскажет, куда ввести этот номер и как получить результаты.</p>
        </div>

        <p>После этого сообщения для вас откроется чат, в котором вы сможете:</p>
        <ul class="exam-chat-benefits">
          <li>узнать, как получить результаты анализов;</li>
          <li>задать любые вопросы о медицинском осмотре;</li>
          <li>получить консультацию по анализам, питанию и вопросам здоровья.</li>
        </ul>

        <p>Как только результаты будут готовы, вы сможете <strong>бесплатно получить их расшифровку</strong> у нашего специалиста.</p>

        <div class="exam-install-note">
          <span aria-hidden="true">📱</span>
          <p><strong>Также рекомендуем установить наше приложение на смартфон.</strong> Так вы не потеряете доступ к своим результатам, сможете в любой момент обратиться к онлайн-врачу и всегда будете иметь все необходимые медицинские сервисы под рукой.</p>
        </div>

        <p class="exam-help-note"><span aria-hidden="true">💬</span> Не стесняйтесь писать в чат — мы всегда рады помочь!</p>
        ${messengerOffer}
      </div>

      <div class="exam-completion-actions">
        <button type="button" class="onboarding-next exam-install-button" data-onboarding-action="install-after-exams"><span aria-hidden="true">＋</span> Установить приложение</button>
        <button type="button" class="exam-later-button" data-onboarding-action="later-after-exams">Установлю позже</button>
      </div>
    </div>`;
  if (state.messengerLinkJustCompleted) {
    const provider = state.messengerLinkJustCompleted;
    state.messengerLinkJustCompleted = '';
    requestAnimationFrame(() => openMessengerLinkModal({source:'exam_completion', justLinked:provider}));
  }
}

function renderExamSkipCompletion() {
  trackEvent('completion_skipped_viewed', { screen:'exam_skip_completion' });
  trackOnboardingScreen('completion_skipped');
  setOnboardingMeta('Готово', 100);
  const messengerOffer = state.identity?.authenticated ? '' : `
    <section class="exam-messenger-offer">
      <span class="exam-messenger-offer-icon" aria-hidden="true">↗</span>
      <div><strong>Не потеряйте доступ</strong><p>Привяжите Telegram или MAX, чтобы открыть анкету, историю диалогов и результаты с другого устройства или после очистки браузера.</p></div>
      <button type="button" data-onboarding-action="link-messenger-after-skip">Привязать мессенджер</button>
    </section>`;
  $('#onboardingContent').innerHTML = `
    <div class="exam-completion exam-skip-completion">
      <div class="exam-completion-mark" aria-hidden="true">✓</div>
      <span class="onboarding-kicker">Анкета завершена</span>
      <h1>Спасибо! Ваши ответы сохранены.</h1>
      <div class="exam-completion-copy">
        <p>Вы решили пока не выбирать дополнительные обследования. Если захотите, к ним можно будет вернуться позже через меню сервиса.</p>
        <p><strong>В Консилиуме вы сможете:</strong></p>
        <ul class="exam-chat-benefits">
          <li>задавать медицинскому помощнику вопросы о здоровье, питании и медицинском осмотре;</li>
          <li>получать результаты анализов по номеру пробирки и просить помочь с расшифровкой;</li>
          <li>сохранять историю обращений и важные сведения о здоровье;</li>
          <li>при необходимости пригласить медицинского специалиста в чат.</li>
        </ul>
        ${messengerOffer}
        <div class="exam-install-note">
          <span aria-hidden="true">📱</span>
          <p><strong>Установите приложение на устройство.</strong> Так Консилиум будет всегда под рукой, а вернуться к вопросам о здоровье станет проще.</p>
        </div>
      </div>
      <div class="exam-completion-actions">
        <button type="button" class="onboarding-next exam-install-button" data-onboarding-action="install-after-skip"><span aria-hidden="true">＋</span> Установить приложение</button>
        <button type="button" class="exam-later-button" data-onboarding-action="continue-after-skip">Перейти в Консилиум</button>
      </div>
    </div>`;
  if (state.messengerLinkJustCompleted) {
    const provider = state.messengerLinkJustCompleted;
    state.messengerLinkJustCompleted = '';
    requestAnimationFrame(() => openMessengerLinkModal({source:'exam_skip_completion', justLinked:provider}));
  }
}

async function finishExamOnboarding(installApp = false) {
  const buttons = $('#onboardingContent').querySelectorAll('[data-onboarding-action]');
  buttons.forEach(button => { button.disabled = true; });
  try {
    if (!state.onboarding?.intro_seen) {
      state.onboarding = await api('/api/onboarding/intro-seen', { method:'POST', body:'{}' });
    }
    if (!installApp) localStorage.setItem(INSTALL_DISMISSED_KEY, String(Date.now()));
    await openMainApp({ skipIntro:true });
    if (installApp) openInstallApp();
  } catch (error) {
    buttons.forEach(button => { button.disabled = false; });
    showOnboardingError(error.message);
  }
}

async function loadOnboarding({ openCompletedMessengerAccount = false, initialOnboarding = null } = {}) {
  state.returnToChatAfterExaminations = false;
  state.paymentReviewSource = '';
  state.paymentReviewOrderId = '';
  state.paymentReceiptEmail = '';
  state.onboarding = initialOnboarding || await api('/api/onboarding');
  applyFontSize(state.onboarding.font_size || 'extra');
  state.profile = state.onboarding.profile;
  seedOnboardingAnswers(state.profile);
  state.selectedTests = new Set(state.onboarding.selected_tests || []);
  if (openCompletedMessengerAccount && hasCompletedQuestionnaire(state.onboarding)) {
    return openMainApp({ skipIntro:true });
  }
  if (state.onboarding.status === 'complete') {
    if (
      state.onboarding.payment_status === 'skipped'
      && !state.onboarding.intro_seen
    ) {
      $('#onboarding').classList.remove('hidden');
      $('#appShell').classList.add('hidden');
      return renderExamSkipCompletion();
    }
    if (
      state.onboarding.selected_tests?.length
      && ['demo_paid','paid_online','pay_at_exam'].includes(state.onboarding.payment_status)
      && !state.onboarding.intro_seen
    ) {
      $('#onboarding').classList.remove('hidden');
      $('#appShell').classList.add('hidden');
      return renderExamCompletion();
    }
    return openMainApp();
  }
  $('#onboarding').classList.remove('hidden');
  $('#appShell').classList.add('hidden');
  if (state.onboarding.status === 'appearance') renderAppearance();
  else if (state.onboarding.status === 'payment') renderPayment();
  else if (state.onboarding.status === 'exams') renderExamOffer();
  else renderQuestion();
}

async function openMainApp({ skipIntro = false } = {}) {
  state.returnToChatAfterExaminations = false;
  $('#onboarding').classList.add('hidden');
  $('#appShell').classList.remove('hidden');
  if (!state.mainInitialized) await initMainApp();
  trackEvent('chat_opened', { screen:'chat' });
  if (!skipIntro && !state.onboarding?.intro_seen) {
    installAfterCapabilities = state.onboarding?.payment_status === 'not_medical_exam';
    requestAnimationFrame(openCapabilities);
  }
  else scheduleInstallOffer();
  if (state.messengerLinkJustCompleted) {
    const provider = state.messengerLinkJustCompleted;
    state.messengerLinkJustCompleted = '';
    requestAnimationFrame(() => openMessengerLinkModal({source:'return', justLinked:provider}));
  }
}

$('#onboardingContent').addEventListener('click', async event => {
  const resultAction = event.target.closest('[data-result-action]')?.dataset.resultAction;
  if (resultAction) {
    if (resultAction === 'begin') {
      trackResultAction('continue', 'result_welcome');
      renderResultTube();
    }
    else if (resultAction === 'save-tube') await saveResultTube();
    else if (resultAction === 'link-messenger') {
      trackResultAction('link_messenger', 'result_messenger');
      openMessengerLinkModal({source:'result_flow'});
    }
    else if (resultAction === 'search' || resultAction === 'retry-search') {
      trackResultAction(resultAction === 'search' ? 'continue' : 'retry', currentOnboardingAnalyticsScreen);
      await searchResultDocuments();
    }
    else if (resultAction === 'notify') await requestResultNotification();
    else if (resultAction === 'open-chat') await finishResultFlow({openResults:state.resultFlowDocuments.length > 0});
    return;
  }
  const choice = event.target.closest('[data-choice]');
  if (choice) {
    const question = activeOnboardingQuestions()[state.onboardingStep];
    trackOnboardingAction('select_option', `question_${question.key}`);
    state.onboardingAnswers[question.key] = choice.dataset.choice;
    renderQuestion();
    return;
  }
  const appearance = event.target.closest('.appearance-choice[data-size]');
  if (appearance) {
    trackOnboardingAction(`size_${appearance.dataset.size}`, 'appearance');
    applyFontSize(appearance.dataset.size);
    renderAppearance();
    return;
  }
  const card = event.target.closest('[data-test-card]');
  if (card) {
    const id = card.dataset.testCard;
    const scrollPosition = {
      examList:card.closest('.exam-list')?.scrollTop || 0,
      onboarding:$('#onboarding').scrollTop || 0,
    };
    const selection = selectExamination(id);
    if (!selection.changed) return;
    trackEvent(selection.removing ? 'examination_deselected' : 'examination_selected', {
      exam_id:id,
      recommended:(state.onboarding.recommended_test_ids || []).includes(id),
      selected_count:state.selectedTests.size,
    });
    if (!selection.removing) trackOnboardingAction('select_exam', 'exam_selection');
    renderExamSelection(scrollPosition);
    return;
  }
  const action = event.target.closest('[data-onboarding-action]')?.dataset.onboardingAction;
  if (!action) return;
  if (action === 'next') nextQuestion();
  else if (action === 'back') { const question = activeOnboardingQuestions()[state.onboardingStep]; try { captureQuestionAnswer({trackAction:false}); } catch {} trackOnboardingAction('back', `question_${question.key}`); trackEvent('question_back', { step_number:state.onboardingStep + 1 }); state.onboardingStep -= 1; renderQuestion(); }
  else if (action === 'question-back') { trackOnboardingAction('edit_questionnaire', 'exam_offer'); trackEvent('question_back', { screen:'examinations_offer' }); trackEvent('funnel_action', {stage:'examinations_offer',action:'edit_questionnaire'}); if (state.returnToChatAfterExaminations) editProfileFromChatExamFlow(); else { state.onboardingStep = activeOnboardingQuestions().length - 1; renderQuestion(); } }
  else if (action === 'open-exam-catalog-info') { trackOnboardingAction('catalog_info', 'exam_offer'); trackEvent('funnel_action', {stage:'examinations_offer',action:'catalog_info'}); renderExamCatalogInfo(); }
  else if (action === 'close-exam-catalog-info') { trackOnboardingAction('back', 'exam_catalog'); renderExamOffer(); }
  else if (action === 'start-exams') { const sourceScreen = currentOnboardingAnalyticsScreen; const afterObjection = sourceScreen === 'exam_objection'; trackOnboardingAction(sourceScreen === 'exam_catalog' ? 'choose' : afterObjection ? 'choose' : 'view_options', sourceScreen); trackEvent('funnel_action', {stage:'examinations_offer',action:afterObjection ? 'choose_after_objection' : 'view_options'}); trackEvent('examinations_opened', { screen:'examinations' }); renderExamSelection(); }
  else if (action === 'exam-offer') { trackOnboardingAction('back', 'exam_selection'); trackEvent('funnel_action', {stage:'examinations_options',action:'options_back'}); if (state.returnToChatAfterExaminations) { state.selectedTests = new Set(state.onboarding?.selected_tests || []); renderCurrentExamSelectionSummary(); } else renderExamOffer(); }
  else if (action === 'review-exam-skip') { const fromOptions = Boolean($('#onboardingContent .exam-list')); trackOnboardingAction(fromOptions ? 'nothing' : 'skip', fromOptions ? 'exam_selection' : 'exam_offer'); trackEvent('funnel_action', {stage:fromOptions ? 'examinations_options' : 'examinations_offer',action:fromOptions ? 'nothing_selected' : 'skip'}); trackEvent('examinations_skip_clicked', { selected_count:state.selectedTests.size }); renderExamSkipConfirmation(); }
  else if (action === 'confirm-skip-exams') { trackOnboardingAction('refuse', 'exam_objection'); trackEvent('funnel_action', {stage:'examinations_offer',action:'refuse'}); trackEvent('examinations_skipped', { screen:'examinations_skip' }); submitExamSelection(true); }
  else if (action === 'continue-payment') { trackOnboardingAction('continue', 'exam_selection'); submitExamSelection(false); }
  else if (action === 'back-to-exams') { trackOnboardingAction('back', 'payment'); renderExamSelection(); }
  else if (action === 'close-payment-review') {
    trackOnboardingAction('close', 'payment');
    const orderId = state.paymentReviewOrderId;
    await openMainApp({skipIntro:true});
    state.paymentReviewSource = '';
    state.paymentReviewOrderId = '';
    await openPurchases({highlightOrderId:orderId});
  }
  else if (action === 'pay-online') { trackOnboardingAction('pay_online', 'payment'); trackEvent('funnel_action', {stage:'examinations_options',action:'pay_online'}); startOnlinePayment(); }
  else if (action === 'pay-at-exam') { trackOnboardingAction('pay_at_exam', 'payment'); trackEvent('funnel_action', {stage:'examinations_options',action:'pay_at_exam'}); confirmPaymentAtExam(); }
  else if (action === 'back-to-payment') await returnToOnlinePayment();
  else if (action === 'leave-payment-result') await leavePaymentResult();
  else if (action === 'open-purchases') openPurchases();
  else if (action === 'open-purchases-after-payment') await finishPaymentSuccess(true);
  else if (action === 'continue-after-payment') await finishPaymentSuccess(false);
  else if (action === 'check-payment') { const orderId = event.target.closest('[data-order-id]')?.dataset.orderId; if (orderId) { const url = new URL(location.href); url.searchParams.set('payment_return', orderId); if (state.returnToChatAfterExaminations) url.searchParams.set('return_to_chat', '1'); history.replaceState({}, '', url); handlePaymentReturn(); } }
  else if (action === 'edit-current-exams') { trackEvent('funnel_action', {stage:'chat_examinations',action:'edit_selection'}); renderExamSelection(); }
  else if (action === 'close-current-exams') openMainApp({ skipIntro:true });
  else if (action === 'open-app') openMainApp();
  else if (action === 'install-after-exams') { trackOnboardingAction('install', 'completion'); trackEvent('install_clicked', { screen:'exam_completion' }); finishExamOnboarding(true); }
  else if (action === 'later-after-exams') { trackOnboardingAction('later', 'completion'); trackEvent('install_dismissed', { screen:'exam_completion' }); finishExamOnboarding(false); }
  else if (action === 'link-messenger-after-exams') { trackOnboardingAction('link_messenger', 'completion'); openMessengerLinkModal({source:'exam_completion'}); }
  else if (action === 'install-after-skip') { trackOnboardingAction('install', 'completion_skipped'); trackEvent('install_clicked', { screen:'exam_skip_completion' }); finishExamOnboarding(true); }
  else if (action === 'continue-after-skip') { trackOnboardingAction('continue', 'completion_skipped'); trackEvent('install_dismissed', { screen:'exam_skip_completion' }); finishExamOnboarding(false); }
  else if (action === 'link-messenger-after-skip') { trackOnboardingAction('link_messenger', 'completion_skipped'); openMessengerLinkModal({source:'exam_skip_completion'}); }
  else if (action === 'skip-medical-exam') skipMedicalExam();
  else if (action === 'save-appearance') { trackOnboardingAction('continue', 'appearance'); saveAppearance(); }
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

function setActiveAgent(id) {
  state.active = 'manager';
  const agent = AGENTS.manager;
  $('#headerAvatar').textContent = agent.initials;
  $('#headerName').textContent = agent.name;
  $('#headerRole').textContent = agent.role;
}

const labInterpretationSections = [
  { id:'summary', title:'Общая картина', icon:'✓', tone:'summary', test:/общ(?:ий|ая)\s+(?:вывод|картин)|коротк(?:ий|ая)\s+.*вывод/i },
  { id:'normal', title:'В пределах референсов', icon:'●', tone:'normal', test:/пределах\s+референс|в\s+норме|без\s+отклонен/i },
  { id:'deviations', title:'На что обратить внимание', icon:'!', tone:'attention', test:/отклонен|обратить\s+внимание|вне\s+референс/i },
  { id:'connections', title:'Связи и ограничения', icon:'↔', tone:'context', test:/связ(?:и|ь)\s+между|ограничен.*интерпретац|важн.*ограничен/i },
  { id:'next', title:'Что делать дальше', icon:'→', tone:'next', test:/обсудить\s+со\s+специалист|что\s+делать|следующ(?:ий|ие)\s+шаг|когда\s+.*сделать/i },
];

function labRichTextMarkup(value) {
  return window.ConsiliumRichText.render(value);
}
function labInterpretationMarkup(text, metadata = {}) {
  const raw = String(text || '').replace(/\r/g, '').trim();
  const buckets = new Map();
  const intro = [];
  let active = null;
  for (const originalLine of raw.split('\n')) {
    const line = originalLine.trim();
    if (!line) {
      if (active) buckets.get(active).push('');
      else intro.push('');
      continue;
    }
    const cleaned = line
      .replace(/^#{1,4}\s*/, '')
      .replace(/^\*\*(.*?)\*\*:?\s*$/, '$1')
      .replace(/^\d+[.)]\s*/, '')
      .replace(/:$/, '')
      .trim();
    const section = labInterpretationSections.find(item => item.test.test(cleaned));
    if (section && cleaned.length <= 150) {
      active = section.id;
      if (!buckets.has(active)) buckets.set(active, []);
      const colon = line.indexOf(':');
      if (colon >= 0 && line.slice(colon + 1).trim()) buckets.get(active).push(line.slice(colon + 1).trim());
      continue;
    }
    (active ? buckets.get(active) : intro).push(line);
  }
  if (intro.join('').trim()) {
    const target = buckets.has('summary') ? 'details' : 'summary';
    if (!buckets.has(target)) buckets.set(target, []);
    buckets.set(target, [...intro, ...buckets.get(target)]);
  }
  if (!buckets.size) buckets.set('details', [raw]);
  const scope = metadata.document_id === 'all'
    ? 'Все документы проанализированы вместе'
    : 'Проанализирован выбранный документ';
  const cards = [];
  for (const [id, lines] of buckets) {
    const section = labInterpretationSections.find(item => item.id === id) || {
      id:'details', title:'Подробная расшифровка', icon:'≡', tone:'context',
    };
    const body = lines.join('\n').trim();
    if (!body) continue;
    const initiallyOpen = ['summary', 'deviations'].includes(id);
    cards.push(`<details class="lab-report-section ${section.tone}" ${initiallyOpen ? 'open' : ''}>
      <summary><i>${section.icon}</i><strong>${section.title}</strong><span></span></summary>
      <div class="lab-report-body">${labRichTextMarkup(body)}</div>
    </details>`);
  }
  return `<div class="lab-report">
    <div class="lab-report-heading"><i>▤</i><div><strong>Расшифровка результатов</strong><small>${scope} · с учётом данных анкеты</small></div></div>
    <div class="lab-report-sections">${cards.join('')}</div>
    <p class="lab-report-disclaimer">Расшифровка помогает понять результаты, но не заменяет диагноз и очную консультацию врача.</p>
  </div>`;
}

function addMessage(sender, text, agentId = state.active, urgent = false, createdAt = null, metadata = {}) {
  const messageId = Number(metadata._message_id || 0);
  if (messageId) {
    state.lastMessageId = Math.max(state.lastMessageId, messageId);
    const existing = messages.querySelector(`[data-message-id="${messageId}"]`);
    if (existing) return existing;
  }
  const agent = AGENTS[agentId] || AGENTS.manager;
  const humanManager = sender === 'agent' && metadata.sender_type === 'human_manager';
  const wrapper = document.createElement('div');
  const labInterpretation = sender === 'agent' && metadata.action === 'lab_interpretation';
  wrapper.className = `message-row ${sender}${urgent ? ' urgent' : ''}${humanManager ? ' human-manager' : ''}${labInterpretation ? ' lab-interpretation' : ''}`;
  if (messageId) wrapper.dataset.messageId = String(messageId);
  const date = createdAt ? new Date(createdAt) : new Date();
  const time = new Intl.DateTimeFormat('ru', { hour: '2-digit', minute: '2-digit' }).format(date);
  const attachmentBadges = (metadata.attachments || []).map(item => `<em class="message-file">▱ ${escapeHtml(item.name)}</em>`).join('');
  const cached = metadata.action === 'lab_interpretation' && metadata.interpretation_cached
    ? '<b class="special-label">Сохранённая расшифровка</b>' : '';
  const labDocuments = sender === 'agent'
    ? labDocumentsMarkup(metadata.lab_result_documents || [], 'message') : '';
  const assistantContent = labInterpretation
    ? labInterpretationMarkup(text, metadata)
    : formatAssistantText(text);
  wrapper.innerHTML = sender === 'user'
    ? `<div class="bubble user-bubble">${attachmentBadges}<p>${escapeHtml(text)}</p><span>${time}</span></div>`
    : `<div class="message-avatar">${humanManager ? 'Ч' : agent.initials}</div><div><div class="message-author"><strong>${humanManager ? escapeHtml(metadata.manager_name || 'Менеджер') : agent.name}</strong><span>${humanManager ? 'Менеджер' : agent.role}</span>${cached}</div><div class="bubble agent-bubble">${assistantContent}${labDocuments}<span>${time}</span></div></div>`;
  messages.appendChild(wrapper);
  scrollChatToBottom();
  return wrapper;
}

function updateChatMode(aiEnabled = true, humanStatus = 'none', humanTicketId = null) {
  state.aiEnabled = Boolean(aiEnabled);
  state.humanStatus = humanStatus || 'none';
  const banner = $('#chatModeBanner');
  const newDialogButton = $('#chatModeNewDialog');
  const toggleButton = $('#chatModeToggle');
  const relevant = !state.aiEnabled || ['pending', 'connected'].includes(state.humanStatus);
  const expandable = !state.aiEnabled && relevant;
  banner.classList.toggle('hidden', !relevant);
  banner.classList.toggle('ai-paused', !state.aiEnabled);
  banner.classList.toggle('expandable', expandable);
  banner.tabIndex = expandable ? 0 : -1;
  if (!expandable) {
    banner.classList.remove('expanded');
    banner.setAttribute('aria-expanded', 'false');
    toggleButton.setAttribute('aria-expanded', 'false');
    toggleButton.setAttribute('aria-label', 'Показать подробности');
  }
  newDialogButton.classList.toggle('hidden', state.aiEnabled || !relevant);
  toggleButton.classList.toggle('hidden', state.aiEnabled || !relevant);
  input.placeholder = state.aiEnabled
    ? 'Задайте вопрос о здоровье...'
    : 'Напишите медицинскому специалисту...';
  if (!relevant) return;
  $('#chatModeIcon').textContent = state.aiEnabled ? '✓' : '♙';
  $('#chatModeTitle').textContent = state.aiEnabled
    ? 'ИИ снова отвечает в этом диалоге'
    : state.humanStatus === 'connected' ? 'С вами общается медицинский специалист' : 'Ожидаем ответа медицинского специалиста';
  $('#chatModeText').textContent = state.aiEnabled
    ? `Менеджер включил ИИ${humanTicketId ? ` · обращение ${humanTicketId}` : ''}.`
    : 'ИИ в этом диалоге приостановлен. Новые сообщения получит медицинский специалист.';
  $('#chatModeDetailsText').textContent = state.aiEnabled
    ? ''
    : 'Пока ожидаете ответ, вы можете продолжить общение с ИИ в новом диалоге.';
}

function toggleChatModeDetails() {
  const banner = $('#chatModeBanner');
  const toggle = $('#chatModeToggle');
  if (!banner.classList.contains('expandable')) return;
  const expanded = banner.classList.toggle('expanded');
  banner.setAttribute('aria-expanded', String(expanded));
  toggle.setAttribute('aria-expanded', String(expanded));
  toggle.setAttribute('aria-label', expanded ? 'Скрыть подробности' : 'Показать подробности');
}

function addCouncilResult(result, createdAt = null) {
  return addMessage(
    'agent', result.message?.content || '', 'manager', false, createdAt,
    { ...(result.message?.metadata || {}), _message_id:result.message?.id },
  );
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
  timeline.innerHTML = `<div class="empty-state"><div class="empty-icon">⌁</div><strong>Здесь появятся следующие шаги</strong><p>Покажем, что происходит с вашим обращением и какие шаги будут дальше.</p></div>`;
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
  $('#handoffBanner').classList.add('hidden');
}

async function processMessage(text) {
  if (state.processing) return;
  state.processing = true;
  $('#taskStatus').textContent = 'Ольга изучает вопрос';
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
      setActiveAgent(result.agent);
      addTimeline(result.agent, result.emergency ? 'Срочная оценка' : 'Ольга ответила', result.handoff_reason, 'active');
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

    if (result.action === 'waiting_human') {
      $('#taskStatus').textContent = 'Сообщение ожидает ответа медицинского специалиста';
      addTimeline('manager', 'Сообщение передано', 'Медицинский специалист увидит его в своей очереди', 'active');
    } else if (result.action === 'human_preference') {
      $('#taskStatus').textContent = 'Сообщения ждут ответа медицинского специалиста';
    } else if (result.action === 'lab_results_prompt') {
      await openLabResults();
      $('#taskStatus').textContent = 'Укажите номер пробирки';
    } else if (result.human_escalation) {
      await openHumanModal();
      $('#taskStatus').textContent = 'Ожидает вашего решения · ИИ продолжает работать';
    } else {
      $('#taskStatus').textContent = result.emergency ? 'Требуется срочное действие' : 'Контекст сохранён';
    }
    if (result.assistant_message) await markConversationRead(result.conversation_id);
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
}

async function loadConversationList() {
  try {
    const items = await api('/api/conversations');
    applyUnreadCounts(Object.fromEntries(items.map(item => [item.id, Number(item.unread_count || 0)])));
    $('#mobileConversationCount').textContent = String(items.length);
    $('#conversationList').innerHTML = items.length ? items.map(item => `
      <button class="conversation-row ${item.id === state.conversationId ? 'active' : ''} ${Number(item.unread_count || 0) ? 'unread' : ''}" data-id="${item.id}">
        <span class="conversation-row-title"><strong>${escapeHtml(item.title)}</strong>${Number(item.unread_count || 0) ? `<b>${formatUnreadCount(item.unread_count)}</b>` : ''}</span>
        <span>${escapeHtml(conversationSummary(item))}</span><small>${formatRelative(item.updated_at)}</small>
      </button>`).join('') : '<p class="no-conversations">Пока нет сохранённых диалогов</p>';
  } catch {
    $('#mobileConversationCount').textContent = '0';
    $('#conversationList').innerHTML = '<p class="no-conversations">Не удалось загрузить диалоги</p>';
  }
}

function formatUnreadCount(count) {
  const value = Math.max(0, Number(count || 0));
  return value > 99 ? '99+' : String(value);
}

function applyUnreadCounts(counts = {}) {
  state.unreadCounts = Object.fromEntries(
    Object.entries(counts || {}).map(([id, count]) => [id, Math.max(0, Number(count || 0))]),
  );
  const total = Object.values(state.unreadCounts).reduce((sum, count) => sum + count, 0);
  const badge = $('#mobileDialogsUnread');
  badge.textContent = formatUnreadCount(total);
  badge.classList.toggle('hidden', total === 0);
  const button = $('#mobileHeaderDialogsButton');
  button.classList.toggle('has-unread', total > 0);
  button.setAttribute('aria-label', total
    ? `Открыть диалоги, новых сообщений: ${total}`
    : 'Открыть диалоги, новых сообщений нет');
  document.querySelectorAll('.conversation-row[data-id]').forEach(row => {
    const count = state.unreadCounts[row.dataset.id] || 0;
    row.classList.toggle('unread', count > 0);
    const title = row.querySelector('.conversation-row-title');
    let rowBadge = title?.querySelector('b');
    if (count && title && !rowBadge) {
      rowBadge = document.createElement('b');
      title.appendChild(rowBadge);
    }
    if (rowBadge) {
      rowBadge.textContent = formatUnreadCount(count);
      rowBadge.classList.toggle('hidden', count === 0);
    }
  });
}

async function markConversationRead(conversationId = state.conversationId) {
  if (!conversationId) return;
  try {
    const result = await api(`/api/conversations/${encodeURIComponent(conversationId)}/read`, {
      method:'POST', body:'{}',
    });
    applyUnreadCounts(result.unread_counts || {});
  } catch {}
}

function conversationSummary(item) {
  try {
    const context = JSON.parse(item.context_summary || '{}');
    const topic = String(context.current_topic || context.user_goal || '').trim();
    if (topic && topic.toLocaleLowerCase('ru') !== String(item.title || '').toLocaleLowerCase('ru')) {
      return `Тема: ${topic}`;
    }
  } catch {}
  if (item.human_status === 'pending') return 'Ожидает ответа медицинского специалиста';
  if (item.human_status === 'connected') return 'Менеджер подключён';
  return 'История разговора сохранена';
}

async function syncConversationUpdates() {
  if (state.processing || !state.mainInitialized) return;
  if (!state.conversationId) {
    try {
      const summary = await api('/api/conversations/unread');
      applyUnreadCounts(summary.unread_counts || {});
    } catch {}
    return;
  }
  try {
    const data = await api(
      `/api/conversations/${state.conversationId}/updates?after_id=${state.lastMessageId}`,
    );
    updateChatMode(
      data.ai_enabled, data.human_status, data.human_ticket_id,
    );
    applyUnreadCounts(data.unread_counts || {});
    let incomingMessageReceived = false;
    for (const message of data.messages || []) {
      state.lastMessageId = Math.max(state.lastMessageId, Number(message.id || 0));
      const exists = messages.querySelector(`[data-message-id="${message.id}"]`);
      if (exists) continue;
      if (message.metadata?.action === 'council' && message.metadata?.opinions) {
        addCouncilResult({
          agents: message.metadata.agents || [],
          opinions: message.metadata.opinions,
          message,
        }, message.created_at);
      } else {
        addMessage(
          message.role === 'user' ? 'user' : 'agent',
          message.content,
          message.agent_id || 'manager',
          Boolean(message.metadata?.emergency),
          message.created_at,
          { ...(message.metadata || {}), _message_id:message.id },
        );
      }
      if (message.role !== 'user') incomingMessageReceived = true;
      if (message.metadata?.sender_type === 'human_manager') {
        $('#taskStatus').textContent = 'Менеджер ответил';
      }
    }
    if (incomingMessageReceived) {
      playUserMessageSound();
      await markConversationRead(state.conversationId);
    }
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
    $('#suggestions').classList.add('hidden');
    $('#taskStatus').textContent = 'Диалог загружен';
    await markConversationRead(id);
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
  setActiveAgent('manager');
  $('#suggestions').classList.remove('hidden');
  $('#taskStatus').textContent = 'Ожидает задачу';
  addMessage('agent', 'Здравствуйте! Я Ольга, ваш медицинский помощник. Задавайте вопросы о здоровье, питании, спорте или возможностях сервиса — я помогу разобраться и при необходимости предложу подключить человека.', 'manager');
  loadConversationList();
}

async function openHumanModal() {
  setHumanChoiceDisabled(false);
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
}
function declineHumanSpecialist() {
  closeHumanModal();
  $('#taskStatus').textContent = 'ИИ продолжает отвечать';
  updateChatMode(true, 'none', null);
  focusChatInput();
}
function setHumanChoiceDisabled(disabled) {
  $('#humanChatButton').disabled = disabled;
  $('#humanDeclineButton').disabled = disabled;
}

async function chooseHumanSpecialistChat() {
  if (!state.conversationId || state.processing) return;
  if (!state.profile) await loadProfile();
  if (!interpretationProfileComplete()) {
    closeHumanModal();
    openInterpretationProfileModal('consultation');
    return;
  }
  state.processing = true;
  setHumanChoiceDisabled(true);
  try {
    const result = await api('/api/human-preference', {
      method: 'POST',
      body: JSON.stringify({ conversation_id: state.conversationId, channel:'chat' }),
    });
    closeHumanModal();
    updateChatMode(false, result.human_status, result.ticket_id);
    setActiveAgent('manager');
    addMessage(
      'agent', result.assistant_message.content, 'manager', false,
      result.assistant_message.created_at,
      { ...(result.assistant_message.metadata || {}), _message_id:result.assistant_message.id },
    );
    $('#taskStatus').textContent = 'Сообщения ждут ответа медицинского специалиста';
    input.placeholder = 'Напишите медицинскому специалисту...';
    await markConversationRead(state.conversationId);
    await loadConversationList();
  } catch (error) {
    if (error.code === 'consultation_profile_required') {
      closeHumanModal();
      openInterpretationProfileModal('consultation');
      return;
    }
    addSystemError(error.message);
    setHumanChoiceDisabled(false);
  } finally {
    state.processing = false;
    focusChatInput();
  }
}
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
  trackEvent('capabilities_viewed', {screen:'capabilities'});
}

const purchaseStatusLabels = {
  creating:'Создаётся', pending:'Ожидает оплаты', waiting_for_capture:'Подтверждается',
  succeeded:'Оплачено', canceled:'Неуспешно', abandoned:'Не завершено', failed:'Ошибка',
};

const purchaseStatusDetails = {
  creating:{icon:'…',note:'Заказ создаётся. Если это занимает долго, попробуйте повторить.'},
  pending:{icon:'→',note:'Оплата ещё не завершена. Можно вернуться на защищённую страницу оплаты.'},
  waiting_for_capture:{icon:'⌛',note:'ЮKassa уже обрабатывает платёж. Дополнительных действий не требуется.'},
  succeeded:{icon:'✓',note:'Оплата подтверждена ЮKassa. Запись нельзя удалить.'},
  canceled:{icon:'×',note:'Платёж отменён или отклонён. Деньги не списаны.'},
  abandoned:{icon:'↩',note:'Вы вышли до подтверждения оплаты.'},
  failed:{icon:'!',note:'Не удалось создать платёж. Можно оформить заказ повторно.'},
};

function purchaseDate(value) {
  if (!value) return 'Дата не указана';
  return new Intl.DateTimeFormat('ru', {
    day:'2-digit', month:'2-digit', year:'numeric', hour:'2-digit', minute:'2-digit',
  }).format(new Date(value));
}

function renderPurchases(items, highlightOrderId = '') {
  const list = $('#purchasesList');
  const paidCount = items.filter(item => item.status === 'succeeded' && item.paid).length;
  const attentionCount = items.length - paidCount;
  $('#purchasesSummary').innerHTML = items.length
    ? `<span><small>Всего заказов</small><strong>${items.length}</strong></span><span><small>Оплачено</small><strong>${paidCount}</strong></span><span class="${attentionCount ? 'active' : ''}"><small>Требуют внимания</small><strong>${attentionCount}</strong></span>`
    : '';
  if (!items.length) {
    list.innerHTML = '<div class="purchases-empty"><span>₽</span><strong>Покупок пока нет</strong><p>Здесь появятся ваши попытки онлайн-оплаты.</p></div>';
    return;
  }
  list.innerHTML = items.map((item, index) => {
    const rawStatus = String(item.status || 'failed');
    const status = purchaseStatusLabels[rawStatus] ? rawStatus : 'failed';
    const detail = purchaseStatusDetails[status];
    const products = Array.isArray(item.items) ? item.items : [];
    const classes = ['purchase-card', `status-${status}`];
    if (item.id === highlightOrderId) classes.push('highlighted');
    return `<article class="${classes.join(' ')}" data-purchase-id="${escapeHtml(item.id)}">
      <header class="purchase-card-head">
        <div class="purchase-status-icon" aria-hidden="true">${detail.icon}</div>
        <div class="purchase-card-title"><small>Заказ ${items.length - index} · ${purchaseDate(item.created_at)}</small><strong>${escapeHtml(purchaseStatusLabels[status])}</strong></div>
        <b class="purchase-card-amount">${Number(item.amount || 0).toLocaleString('ru-RU', {minimumFractionDigits:0, maximumFractionDigits:2})} ₽</b>
      </header>
      <p class="purchase-status-note">${escapeHtml(detail.note)}</p>
      <div class="purchase-products"><span>Состав заказа</span><ul>${products.map(product => `<li><span>${escapeHtml(product.name || 'Обследование')}</span><b>${Number(product.price || 0).toLocaleString('ru-RU')} ₽</b></li>`).join('')}</ul></div>
      <footer><span>№ ${escapeHtml(String(item.id || '').slice(-8).toUpperCase())}</span>${item.test ? '<em>Тестовый платёж</em>' : '<em>ЮKassa</em>'}</footer>
      <div class="purchase-actions">
        ${status === 'pending' && item.confirmation_url ? `<button type="button" class="purchase-primary-action" data-purchase-action="continue">Продолжить оплату</button>` : ''}
        ${['creating','canceled','abandoned','failed'].includes(status) ? `<button type="button" class="purchase-primary-action" data-purchase-action="retry">Повторить заказ</button>` : ''}
        ${['pending','waiting_for_capture','abandoned'].includes(status) ? `<button type="button" class="purchase-refresh-action" data-purchase-action="refresh">Проверить статус</button>` : ''}
        ${['canceled','abandoned','failed'].includes(status) ? `<button type="button" class="purchase-delete-action" data-purchase-action="delete">Удалить из списка</button>` : ''}
      </div>
      <div class="purchase-delete-confirm hidden" data-purchase-confirm>
        <div><strong>Удалить эту попытку?</strong><small>Успешные оплаты всегда сохраняются.</small></div>
        <button type="button" data-purchase-action="cancel-delete">Оставить</button>
        <button type="button" data-purchase-action="confirm-delete">Удалить</button>
      </div>
    </article>`;
  }).join('');
  if (highlightOrderId) requestAnimationFrame(() => list.querySelector('.highlighted')?.scrollIntoView({block:'nearest'}));
}

async function openPurchases({ highlightOrderId = '' } = {}) {
  closeFunctionMenu();
  $('#purchasesSummary').innerHTML = '';
  $('#purchasesList').innerHTML = '<div class="purchases-loading"><span></span><p>Загружаем покупки…</p></div>';
  $('#purchasesModal').classList.remove('hidden');
  try {
    const result = await api('/api/purchases');
    state.purchases = result.purchases || [];
    renderPurchases(state.purchases, highlightOrderId);
    trackEvent('purchases_viewed', {purchase_count:state.purchases.length});
  } catch (error) {
    $('#purchasesList').innerHTML = `<div class="purchases-empty error"><strong>Не удалось загрузить покупки</strong><p>${escapeHtml(error.message)}</p></div>`;
  }
}

function closePurchases() { $('#purchasesModal').classList.add('hidden'); }

function purchaseById(orderId) {
  return state.purchases.find(item => item.id === orderId);
}

function continuePurchase(item) {
  if (!item?.confirmation_url?.startsWith('https://')) return;
  localStorage.setItem(PAYMENT_PENDING_ORDER_KEY, JSON.stringify({
    id:item.id, returnToChat:true, source:'purchases', savedAt:Date.now(),
  }));
  trackEvent('payment_continued', {provider:'yookassa'});
  window.location.assign(item.confirmation_url);
}

async function retryPurchase(item) {
  if (item?.status === 'abandoned') {
    const checked = await api(`/api/payments/${encodeURIComponent(item.id)}`);
    const refreshed = checked.order;
    state.purchases = state.purchases.map(purchase => purchase.id === item.id ? refreshed : purchase);
    if (['pending','waiting_for_capture','succeeded'].includes(refreshed.status)) {
      if (refreshed.status === 'succeeded' && refreshed.paid) {
        state.onboarding = await api('/api/onboarding');
        state.profile = state.onboarding.profile;
        state.selectedTests = new Set(state.onboarding.selected_tests || []);
        trackEvent('payment_succeeded', {provider:'yookassa', source:'purchases_retry'});
      }
      renderPurchases(state.purchases, item.id);
      return false;
    }
  }
  const available = new Set((state.onboarding?.tests || []).map(test => test.id));
  const selected = (item?.items || []).map(product => product.id).filter(id => available.has(id));
  if (!selected.length) throw new Error('Эти обследования больше недоступны. Выберите актуальные варианты заново.');
  closePurchases();
  state.returnToChatAfterExaminations = true;
  state.paymentReviewSource = 'purchases';
  state.paymentReviewOrderId = item.id;
  state.selectedTests = new Set(selected);
  $('#appShell').classList.add('hidden');
  $('#onboarding').classList.remove('hidden');
  state.onboarding = await api('/api/onboarding/exams', {
    method:'POST', body:JSON.stringify({selected_tests:selected}),
  });
  trackEvent('payment_retried', {provider:'yookassa', selected_count:selected.length});
  renderPayment();
  return true;
}

async function deletePurchaseAttempt(card, item) {
  const confirmBox = card.querySelector('[data-purchase-confirm]');
  confirmBox.querySelectorAll('button').forEach(button => { button.disabled = true; });
  try {
    await api(`/api/purchases/${encodeURIComponent(item.id)}`, {method:'DELETE'});
    trackEvent('purchase_attempt_removed', {provider:'yookassa', status:item.status});
    state.purchases = state.purchases.filter(purchase => purchase.id !== item.id);
    renderPurchases(state.purchases);
  } catch (error) {
    confirmBox.classList.add('error');
    confirmBox.querySelector('small').textContent = error.message;
    confirmBox.querySelectorAll('button').forEach(button => { button.disabled = false; });
  }
}

async function refreshPurchase(item, button) {
  button.disabled = true;
  const label = button.textContent;
  button.textContent = 'Проверяем…';
  try {
    const result = await api(`/api/payments/${encodeURIComponent(item.id)}`);
    const refreshed = result.order;
    state.purchases = state.purchases.map(purchase => purchase.id === item.id ? refreshed : purchase);
    if (refreshed.status === 'succeeded' && refreshed.paid) {
      state.onboarding = await api('/api/onboarding');
      state.profile = state.onboarding.profile;
      state.selectedTests = new Set(state.onboarding.selected_tests || []);
      trackEvent('payment_succeeded', {provider:'yookassa', source:'purchases'});
    }
    renderPurchases(state.purchases, item.id);
  } catch (error) {
    button.disabled = false;
    button.textContent = label;
    const confirmBox = button.closest('.purchase-card').querySelector('[data-purchase-confirm]');
    confirmBox.classList.remove('hidden');
    confirmBox.classList.add('error');
    confirmBox.querySelector('strong').textContent = 'Не удалось проверить оплату';
    confirmBox.querySelector('small').textContent = error.message;
  }
}

async function handlePurchaseAction(event) {
  const button = event.target.closest('[data-purchase-action]');
  const card = button?.closest('[data-purchase-id]');
  if (!button || !card) return;
  const item = purchaseById(card.dataset.purchaseId);
  if (!item) return;
  const action = button.dataset.purchaseAction;
  const confirmBox = card.querySelector('[data-purchase-confirm]');
  if (action === 'continue') continuePurchase(item);
  else if (action === 'refresh') await refreshPurchase(item, button);
  else if (action === 'retry') {
    button.disabled = true;
    button.textContent = 'Готовим заказ…';
    try {
      const started = await retryPurchase(item);
      if (!started) return;
    }
    catch (error) { button.disabled = false; button.textContent = 'Повторить заказ'; confirmBox.classList.remove('hidden'); confirmBox.classList.add('error'); confirmBox.querySelector('strong').textContent = 'Не удалось повторить заказ'; confirmBox.querySelector('small').textContent = error.message; }
  }
  else if (action === 'delete') { confirmBox.classList.remove('hidden'); button.closest('.purchase-actions').classList.add('hidden'); }
  else if (action === 'cancel-delete') { confirmBox.classList.add('hidden'); confirmBox.classList.remove('error'); card.querySelector('.purchase-actions').classList.remove('hidden'); }
  else if (action === 'confirm-delete') await deletePurchaseAttempt(card, item);
}

function openDeleteMyDataConfirmation() {
  const error = $('#deleteMyDataError');
  error.textContent = '';
  error.classList.add('hidden');
  $('#deleteMyDataConfirm').disabled = false;
  $('#deleteMyDataConfirm').textContent = 'Удалить всё';
  $('#deleteMyDataModal').classList.remove('hidden');
  requestAnimationFrame(() => $('#deleteMyDataCancel').focus());
}

function closeDeleteMyDataConfirmation() {
  if ($('#deleteMyDataConfirm').disabled) return;
  $('#deleteMyDataModal').classList.add('hidden');
}

async function clearLocalUserData() {
  localStorage.clear();
  sessionStorage.clear();
  if ('caches' in window) {
    try {
      const cacheNames = await caches.keys();
      await Promise.all(cacheNames.map(cacheName => caches.delete(cacheName)));
    } catch (error) {
      console.warn('Не удалось очистить кэш приложения', error);
    }
  }
}

async function deleteMyData() {
  const confirmButton = $('#deleteMyDataConfirm');
  const errorBox = $('#deleteMyDataError');
  confirmButton.disabled = true;
  confirmButton.textContent = 'Удаляем…';
  errorBox.textContent = '';
  errorBox.classList.add('hidden');
  try {
    await api('/api/delete-my-data', {
      method:'POST',
      headers:{'X-Consilium-Action':'delete-my-data'},
      body:JSON.stringify({confirmation:'delete-my-data'}),
    });
    await clearLocalUserData();
    window.location.replace('/');
  } catch (error) {
    errorBox.textContent = `Не удалось удалить данные: ${error.message}`;
    errorBox.classList.remove('hidden');
    confirmButton.disabled = false;
    confirmButton.textContent = 'Удалить всё';
  }
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

async function closeCapabilities({ suppressFollowup = false } = {}) {
  $('#capabilitiesModal').classList.add('hidden');
  trackEvent('capabilities_closed', {screen:'capabilities'});
  if (state.onboarding?.status === 'complete' && !state.onboarding.intro_seen) {
    try {
      state.onboarding = await api('/api/onboarding/intro-seen', { method:'POST', body:'{}' });
    } catch (error) { console.warn('Не удалось сохранить просмотр возможностей', error); }
  }
  if (suppressFollowup) {
    installAfterCapabilities = false;
  } else if (installAfterCapabilities) {
    installAfterCapabilities = false;
    openInstallApp();
  } else {
    scheduleInstallOffer();
  }
}

async function openExaminationsFromCapabilities() {
  await closeCapabilities({ suppressFollowup:true });
  state.returnToChatAfterExaminations = true;
  state.selectedTests = new Set(state.onboarding?.selected_tests || []);
  $('#appShell').classList.add('hidden');
  $('#onboarding').classList.remove('hidden');
  renderCurrentExamSelectionSummary();
}

async function editProfileFromChatExamFlow() {
  await openMainApp({ skipIntro:true });
  await openProfile();
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
      await openHumanModal();
    }
  } catch (error) { addSystemError(error.message); }
}

function closeContextEditor() {
  $('#contextModal').classList.add('hidden');
  if (!state.returnToHumanAfterContextEdit) return;
  const ticketId = state.contextEditTicketId;
  state.returnToHumanAfterContextEdit = false;
  state.contextEditTicketId = null;
  openHumanModal();
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
  const checks = [profile.company_inn, profile.age, profile.sex, profile.height_cm, profile.weight_kg,
    profile.smoking && profile.smoking !== 'unknown', profile.alcohol && profile.alcohol !== 'unknown',
    profile.activity && profile.activity !== 'unknown', profile.blood_pressure && profile.blood_pressure !== 'unknown'];
  return Math.round(checks.filter(Boolean).length / checks.length * 100);
}

function renderProfileStatus() {
  const completion = profileCompletion();
  $('#profileCompletion').textContent = `${completion}%`;
  $('#profileStatus').textContent = completion ? `Заполнено на ${completion}%` : 'Не заполнены';
  $('#capabilityProfileStatus').textContent = completion ? `${completion}%` : 'Заполнить';
  const hasTubeNumber = Boolean(state.profile?.tube_number?.trim());
  $('#menuLabResultsStatus').textContent = hasTubeNumber ? 'Номер пробирки сохранён' : 'Нужно ввести номер пробирки';
  $('#capabilityLabResultsStatus').textContent = hasTubeNumber ? 'Проверить' : 'Ввести номер';
}

async function openProfile() {
  await loadMemories();
  const profile = state.profile || {};
  $('#profileChelId').value = profile.chel_id || '';
  $('#profileCompanyInn').value = profile.company_inn || '';
  $('#profileName').value = profile.preferred_name || '';
  $('#profileAge').value = profile.age ?? '';
  $('#profileSex').value = profile.sex || '';
  $('#profileHeight').value = profile.height_cm ?? '';
  $('#profileWeight').value = profile.weight_kg ?? '';
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
  setupCompanyInnSuggestions('#profileCompanyInn', '#profileCompanyInnSuggestions');
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
  for (const selector of ['#profileAge', '#profileHeight', '#profileWeight']) {
    const field = $(selector);
    if (field.value && !field.checkValidity()) {
      field.reportValidity();
      return;
    }
  }
  const payload = {
    company_inn: $('#profileCompanyInn').value.trim(),
    preferred_name: $('#profileName').value.trim(), age: $('#profileAge').value,
    sex: $('#profileSex').value, height_cm: $('#profileHeight').value,
    weight_kg: $('#profileWeight').value,
    pregnancy: state.profile?.pregnancy || 'not_applicable',
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
    $('#taskStatus').textContent = 'Данные сохранены · специалисты учтут их в ответах';
  } catch (error) { addSystemError(error.message); }
}

function profilePayloadWithTube(tubeNumber) {
  const profile = state.profile || {};
  return {
    company_inn: profile.company_inn || '',
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
  setLabResultNotificationAction(false);
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

function setLabResultNotificationAction(visible, message = '', success = false) {
  const button = $('#requestLabResultNotificationButton');
  const status = $('#labResultNotificationStatus');
  button.classList.toggle('hidden', !visible);
  if (!button.disabled) button.textContent = 'Получить уведомление';
  status.textContent = message;
  status.classList.toggle('hidden', !message);
  status.classList.toggle('success', Boolean(message && success));
}

async function requestLabResultNotification() {
  const button = $('#requestLabResultNotificationButton');
  const status = $('#labResultNotificationStatus');
  if (!linkedMessengerProviders().size) {
    status.textContent = 'Чтобы получить уведомление о готовности результатов, сначала привяжите Telegram или MAX.';
    status.classList.remove('hidden', 'success');
    openMessengerLinkModal({source:'lab_results_notification'});
    return;
  }
  button.disabled = true;
  button.textContent = 'Подключаю…';
  try {
    await api('/api/lab-results/notification', {method:'POST', body:'{}'});
    button.textContent = 'Уведомление подключено';
    status.textContent = 'Готово. Мы напишем в привязанный мессенджер, когда результаты появятся.';
    status.classList.remove('hidden');
    status.classList.add('success');
    $('#taskStatus').textContent = 'Уведомление о результатах подключено';
  } catch (error) {
    button.disabled = false;
    button.textContent = 'Получить уведомление';
    status.textContent = error.message;
    status.classList.remove('hidden', 'success');
  }
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

function interpretationProfileComplete(profile = state.profile) {
  return Boolean(
    profile?.sex
    && profile?.age !== null && profile?.age !== undefined && profile?.age !== ''
    && profile?.height_cm !== null && profile?.height_cm !== undefined && profile?.height_cm !== ''
    && profile?.weight_kg !== null && profile?.weight_kg !== undefined && profile?.weight_kg !== ''
  );
}

function openInterpretationProfileModal(purpose = 'interpretation') {
  state.miniProfilePurpose = purpose;
  const profile = state.profile || {};
  $('#interpretationProfileSex').value = profile.sex || '';
  $('#interpretationProfileAge').value = profile.age ?? '';
  $('#interpretationProfileHeight').value = profile.height_cm ?? '';
  $('#interpretationProfileWeight').value = profile.weight_kg ?? '';
  $('#interpretationProfileError').classList.add('hidden');
  const consultation = purpose === 'consultation';
  $('#interpretationProfileKicker').textContent = consultation ? 'Перед консультацией' : 'Перед расшифровкой';
  $('#interpretationProfileDescription').textContent = consultation
    ? 'Для бесплатной консультации медицинскому специалисту нужны пол, возраст, рост и вес. Это займёт меньше минуты.'
    : 'Для корректной расшифровки результатов и бесплатной консультации специалисту нужны четыре основных показателя. Это займёт меньше минуты.';
  $('#saveInterpretationProfileButton').textContent = consultation
    ? 'Сохранить и вызвать специалиста'
    : 'Сохранить и перейти к анализам';
  $('#interpretationProfileModal').classList.remove('hidden');
  trackEvent(consultation ? 'human_consultation_profile_requested' : 'lab_interpretation_profile_requested', {
    source:consultation ? 'human_handoff' : 'lab_results',
    stage:'mini_profile',
  });
  requestAnimationFrame(() => {
    const firstMissing = [
      '#interpretationProfileSex', '#interpretationProfileAge',
      '#interpretationProfileHeight', '#interpretationProfileWeight',
    ].map(selector => $(selector)).find(field => !field.value);
    firstMissing?.focus();
  });
}

function closeInterpretationProfileModal() {
  $('#interpretationProfileModal').classList.add('hidden');
}

async function saveInterpretationProfile() {
  const fields = [
    $('#interpretationProfileSex'), $('#interpretationProfileAge'),
    $('#interpretationProfileHeight'), $('#interpretationProfileWeight'),
  ];
  const error = $('#interpretationProfileError');
  if (fields.some(field => !field.value)) {
    error.textContent = 'Заполните пол, возраст, рост и вес';
    error.classList.remove('hidden');
    fields.find(field => !field.value)?.focus();
    return;
  }
  for (const field of fields.slice(1)) {
    if (!field.checkValidity()) {
      field.reportValidity();
      return;
    }
  }
  const button = $('#saveInterpretationProfileButton');
  const purpose = state.miniProfilePurpose;
  button.disabled = true;
  button.textContent = 'Сохраняю…';
  try {
    const payload = profilePayloadWithTube(state.profile?.tube_number || '');
    payload.sex = $('#interpretationProfileSex').value;
    payload.age = $('#interpretationProfileAge').value;
    payload.height_cm = $('#interpretationProfileHeight').value;
    payload.weight_kg = $('#interpretationProfileWeight').value;
    state.profile = await api('/api/profile', {
      method:'POST',
      body:JSON.stringify(payload),
    });
    renderProfileStatus();
    trackEvent(purpose === 'consultation' ? 'human_consultation_profile_completed' : 'lab_interpretation_profile_completed', {
      source:purpose === 'consultation' ? 'human_handoff' : 'lab_results',
      stage:'mini_profile',
    });
    closeInterpretationProfileModal();
    $('#taskStatus').textContent = 'Мини-анкета сохранена';
    state.miniProfilePurpose = 'interpretation';
    if (purpose === 'consultation') await chooseHumanSpecialistChat();
    else await openLabResults();
  } catch (saveError) {
    error.textContent = saveError.message;
    error.classList.remove('hidden');
  } finally {
    button.disabled = false;
    button.textContent = state.miniProfilePurpose === 'consultation'
      ? 'Сохранить и вызвать специалиста'
      : 'Сохранить и перейти к анализам';
  }
}

async function fetchLabResults() {
  if (!state.profile?.tube_number?.trim()) return;
  const button = $('#fetchLabResultsButton');
  button.disabled = true;
  button.textContent = 'Проверяю…';
  setLabResultNotificationAction(false);
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
      setLabResultNotificationAction(true);
      $('#taskStatus').textContent = 'Результаты ещё обрабатываются';
    } else {
      setLabResultsState('!', 'Результаты пока не найдены', 'Проверьте номер пробирки или повторите поиск позже.');
      setLabResultNotificationAction(true);
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
  if (!state.profile) await loadProfile();
  if (!interpretationProfileComplete()) {
    openInterpretationProfileModal();
    return;
  }
  state.processing = true;
  const buttons = document.querySelectorAll('[data-lab-interpret]');
  buttons.forEach(button => { button.disabled = true; });
  const originalText = sourceButton?.textContent;
  if (sourceButton) sourceButton.textContent = 'Расшифровываю…';
  $('#taskStatus').textContent = 'Ольга анализирует результаты и анкету';
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
      { ...(result.user_message.metadata || {}), _message_id:result.user_message.id },
    );
    showHandoff(result.handoff_from, result.agent);
    setActiveAgent(result.agent);
    addMessage(
      'agent',
      result.assistant_message.content,
      result.agent,
      Boolean(result.emergency),
      result.assistant_message.created_at,
      { ...(result.assistant_message.metadata || {}), _message_id:result.assistant_message.id },
    );
    state.context = result.context;
    state.urgency = result.urgency || 'routine';
    renderInsights();
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
const agentLabels = Object.fromEntries(Object.keys(AGENTS).map(id => [id, 'Ольга · Медицинский помощник']));

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
    summary = agentLabels[details.agent_id] || 'Ольга · Медицинский помощник';
    actions = `<button data-history-conversation="${escapeHtml(details.conversation_id)}">Открыть диалог</button>`;
  } else if (item.type === 'document') {
    summary = item.summary || 'Добавлен медицинский документ';
    if (details.conversation_id) actions = `<button data-history-conversation="${escapeHtml(details.conversation_id)}">Открыть диалог</button>`;
  } else if (item.type === 'council') {
    meta = '<span>Ольга · Медицинский помощник</span>';
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
  return window.ConsiliumRichText.render(value);
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
  if (viewportSyncFrame) cancelAnimationFrame(viewportSyncFrame);
  viewportSyncFrame = requestAnimationFrame(() => {
    const viewport = window.visualViewport;
    const height = Math.max(1, viewport?.height || window.innerHeight);
    const top = Math.max(0, viewport?.offsetTop || 0);
    const editableFocused = Boolean(document.activeElement?.matches(
      'input:not([type="button"]):not([type="checkbox"]):not([type="radio"]), textarea, [contenteditable="true"]',
    ));
    document.documentElement.style.setProperty('--app-height', `${Math.round(height)}px`);
    document.documentElement.style.setProperty('--app-top', `${Math.round(top)}px`);
    document.body.classList.toggle(
      'keyboard-open',
      editableFocused && window.matchMedia('(max-width: 720px)').matches,
    );
    viewportSyncFrame = null;
  });
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
  syncVisualViewport();
  setTimeout(scrollChatToBottom, 120);
  setTimeout(scrollChatToBottom, 320);
});
document.addEventListener('focusin', syncVisualViewport);
document.addEventListener('focusout', () => setTimeout(syncVisualViewport, 30));
syncVisualViewport();
window.addEventListener('resize', syncVisualViewport, { passive: true });
window.addEventListener('orientationchange', syncVisualViewport, { passive: true });
const handleVisualViewportChange = () => {
  syncVisualViewport();
  if (document.activeElement === input) scrollChatToBottom();
};
window.visualViewport?.addEventListener('resize', handleVisualViewportChange, { passive: true });
window.visualViewport?.addEventListener('scroll', handleVisualViewportChange, { passive: true });

function openMobileSidebar() {
  closeFunctionMenu();
  document.body.classList.add('show-team');
  requestAnimationFrame(() => $('#conversationList').querySelector('.active')?.scrollIntoView({ block:'nearest' }));
}

function closeProfileModal() { $('#profileModal').classList.add('hidden'); }

function closeVisibleModal() {
  const layers = [...document.querySelectorAll(
    '.auth-warning-backdrop:not(.hidden), .modal-backdrop:not(.hidden)',
  )];
  const layer = layers[layers.length - 1];
  if (!layer) return false;
  switch (layer.id) {
    case 'anonymousWarning': returnFromAnonymousWarning('anonymous_close'); break;
    case 'fontSizeModal': closeFontSizeModal(); break;
    case 'humanModal': declineHumanSpecialist(); break;
    case 'contextModal': closeContextEditor(); break;
    case 'capabilitiesModal': void closeCapabilities(); break;
    case 'deleteMyDataModal': closeDeleteMyDataConfirmation(); break;
    case 'installAppModal': closeInstallApp(); break;
    case 'messengerLinkModal': closeMessengerLinkModal(); break;
    case 'purchasesModal': closePurchases(); break;
    case 'bodyMapModal': closeBodyMap(); break;
    case 'healthHistoryModal': closeHealthHistory(); break;
    case 'profileModal': closeProfileModal(); break;
    case 'labResultsModal': closeLabResults(); break;
    case 'interpretationProfileModal': closeInterpretationProfileModal(); break;
    default: layer.classList.add('hidden');
  }
  return true;
}

function closeTopUiLayer() {
  const paymentUnavailable = document.querySelector('.payment-unavailable-backdrop');
  if (paymentUnavailable) {
    trackOnboardingAction('close', 'payment_unavailable');
    paymentUnavailable.remove();
    trackOnboardingScreen('payment');
    return true;
  }
  const editable = document.activeElement?.matches(
    'input:not([type="button"]):not([type="checkbox"]):not([type="radio"]), textarea, [contenteditable="true"]',
  );
  if (document.body.classList.contains('keyboard-open') && editable) {
    document.activeElement.blur();
    syncVisualViewport();
    return true;
  }

  if (closeVisibleModal()) return true;
  if (!$('#functionMenu').classList.contains('hidden')) {
    closeFunctionMenu();
    return true;
  }
  if (document.body.classList.contains('show-team')) {
    closeMobileTeam();
    return true;
  }
  return false;
}

function armBackNavigationGuard() {
  if (history.state?.consiliumBackGuard) {
    backNavigationArmed = true;
    return;
  }
  history.pushState({ ...(history.state || {}), consiliumBackGuard:true }, '', window.location.href);
  backNavigationArmed = true;
}

window.addEventListener('popstate', () => {
  if (!backNavigationArmed || allowBackNavigation) return;
  if (closeTopUiLayer()) {
    history.pushState({ consiliumBackGuard:true }, '', window.location.href);
    return;
  }
  const shouldExit = window.confirm('Закрыть Консилиум? Несохранённый текст сообщения будет потерян.');
  if (!shouldExit) {
    history.pushState({ consiliumBackGuard:true }, '', window.location.href);
    return;
  }
  allowBackNavigation = true;
  if (isInstalledApp()) window.close();
  setTimeout(() => history.back(), 0);
});

$('#suggestions').addEventListener('click', event => { const button = event.target.closest('[data-prompt]'); if (button) processMessage(button.dataset.prompt); });
$('#humanButton').addEventListener('click', () => processMessage('Я хочу поговорить с человеком'));
$('#contextClose').addEventListener('click', closeContextEditor);
$('#contextModal').addEventListener('click', event => { if (event.target.id === 'contextModal') closeContextEditor(); });
$('#saveContextButton').addEventListener('click', saveContext);
$('#addMemoryButton').addEventListener('click', addMemory);
$('#capabilitiesButton').addEventListener('click', openCapabilities);
$('#capabilitiesClose').addEventListener('click', closeCapabilities);
$('#capabilitiesModal').addEventListener('click', event => { if (event.target.id === 'capabilitiesModal') closeCapabilities(); });
$('#capabilityProfile').addEventListener('click', () => { closeCapabilities(); openProfile(); });
$('#capabilityLabResults').addEventListener('click', () => { closeCapabilities(); openLabResults(); });
$('#capabilityBodyMap').addEventListener('click', () => { closeCapabilities(); openBodyMap(); });
$('#capabilityHealthHistory').addEventListener('click', () => { closeCapabilities(); openHealthHistory(); });
$('#capabilityExaminations').addEventListener('click', openExaminationsFromCapabilities);
$('#capabilityDeleteData').addEventListener('click', openDeleteMyDataConfirmation);
$('#deleteMyDataClose').addEventListener('click', closeDeleteMyDataConfirmation);
$('#deleteMyDataCancel').addEventListener('click', closeDeleteMyDataConfirmation);
$('#deleteMyDataConfirm').addEventListener('click', deleteMyData);
$('#deleteMyDataModal').addEventListener('click', event => {
  if (event.target.id === 'deleteMyDataModal') closeDeleteMyDataConfirmation();
});
$('#functionMenuButton').addEventListener('click', toggleFunctionMenu);
$('#functionMenu').addEventListener('click', event => { if (event.target.closest('button')) closeFunctionMenu(); });
$('#menuFontSizeButton').addEventListener('click', openFontSizeModal);
$('#fontSizeClose').addEventListener('click', closeFontSizeModal);
$('#fontSizeModal').addEventListener('click', event => { if (event.target.id === 'fontSizeModal') closeFontSizeModal(); });
$('#fontSizeOptions').addEventListener('click', event => { const option=event.target.closest('.font-size-choice[data-size]'); if (option) updateFontSize(option.dataset.size); });
$('#menuProfileButton').addEventListener('click', openProfile);
$('#menuMessengerLinkButton').addEventListener('click', () => openMessengerLinkModal({source:'menu'}));
$('#menuLabResultsButton').addEventListener('click', openLabResults);
$('#menuPurchasesButton').addEventListener('click', () => openPurchases());
$('#menuBodyMapButton').addEventListener('click', openBodyMap);
$('#menuHealthHistoryButton').addEventListener('click', () => openHealthHistory());
$('#menuInstallAppButton').addEventListener('click', openInstallApp);
$('#installAppClose').addEventListener('click', () => closeInstallApp({ dismissed:true }));
$('#installAppLaterButton').addEventListener('click', () => closeInstallApp({ dismissed:true }));
$('#installAppConfirmButton').addEventListener('click', confirmInstallApp);
$('#installAppModal').addEventListener('click', event => {
  if (event.target.id === 'installAppModal') closeInstallApp({ dismissed:true });
});
$('#messengerLinkClose').addEventListener('click', closeMessengerLinkModal);
$('#messengerLinkLater').addEventListener('click', closeMessengerLinkModal);
$('#messengerLinkModal').addEventListener('click', event => {
  if (event.target.id === 'messengerLinkModal') closeMessengerLinkModal();
  const button = event.target.closest('[data-link-provider]');
  if (button) startMessengerLink(button.dataset.linkProvider, button);
});
$('#purchasesClose').addEventListener('click', closePurchases);
$('#purchasesModal').addEventListener('click', event => { if (event.target.id === 'purchasesModal') closePurchases(); });
$('#purchasesList').addEventListener('click', handlePurchaseAction);
$('#profileButton').addEventListener('click', openProfile);
$('#bodyMapButton').addEventListener('click', openBodyMap);
$('#healthHistoryButton').addEventListener('click', () => openHealthHistory());
$('#profileClose').addEventListener('click', closeProfileModal);
$('#profileModal').addEventListener('click', event => { if (event.target.id === 'profileModal') closeProfileModal(); });
$('#saveProfileButton').addEventListener('click', saveProfile);
$('#labResultsClose').addEventListener('click', closeLabResults);
$('#labResultsModal').addEventListener('click', event => { if (event.target.id === 'labResultsModal') closeLabResults(); });
$('#interpretationProfileClose').addEventListener('click', closeInterpretationProfileModal);
$('#interpretationProfileModal').addEventListener('click', event => {
  if (event.target.id === 'interpretationProfileModal') closeInterpretationProfileModal();
});
$('#saveInterpretationProfileButton').addEventListener('click', saveInterpretationProfile);
['#interpretationProfileSex', '#interpretationProfileAge', '#interpretationProfileHeight', '#interpretationProfileWeight'].forEach(selector => {
  $(selector).addEventListener('input', () => $('#interpretationProfileError').classList.add('hidden'));
});
$('#saveLabTubeButton').addEventListener('click', saveLabTube);
$('#changeLabTubeButton').addEventListener('click', changeLabTube);
$('#fetchLabResultsButton').addEventListener('click', fetchLabResults);
$('#requestLabResultNotificationButton').addEventListener('click', requestLabResultNotification);
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
$('#chatModeNewDialog').addEventListener('click', newConversation);
$('#chatModeBanner').addEventListener('click', event => {
  if (event.target.closest('#chatModeNewDialog')) return;
  toggleChatModeDetails();
});
$('#chatModeBanner').addEventListener('keydown', event => {
  if (event.target !== event.currentTarget || !['Enter', ' '].includes(event.key)) return;
  event.preventDefault();
  toggleChatModeDetails();
});
$('#conversationList').addEventListener('click', event => { const row = event.target.closest('[data-id]'); if (row) openConversation(row.dataset.id); });
$('#modalClose').addEventListener('click', declineHumanSpecialist);
$('#humanChatButton').addEventListener('click', chooseHumanSpecialistChat);
$('#humanDeclineButton').addEventListener('click', declineHumanSpecialist);
$('#humanModal').addEventListener('click', event => { if (event.target.id === 'humanModal') declineHumanSpecialist(); });
$('#handoffPreview').addEventListener('click', event => {
  if (event.target.id !== 'editHandoffContext') return;
  state.returnToHumanAfterContextEdit = true;
  state.contextEditTicketId = null;
  closeHumanModal();
  openContextEditor();
});
function closeMobileTeam() { document.body.classList.remove('show-team'); }
$('#mobileDialogsButton').addEventListener('click', openMobileSidebar);
$('#mobileHeaderDialogsButton').addEventListener('click', openMobileSidebar);
$('#mobileTeamClose').addEventListener('click', closeMobileTeam);
$('#teamBackdrop').addEventListener('click', closeMobileTeam);
document.addEventListener('click', event => {
  if (!event.target.closest('.function-menu-wrap')) closeFunctionMenu();
});
document.addEventListener('keydown', event => {
  if (event.key !== 'Escape') return;
  closeTopUiLayer();
});

async function initMainApp() {
  state.mainInitialized = true;
  updateInstallMenu();
  await loadMemories();
  await loadProfile();
  await Promise.all([loadBodySymptoms(), loadHealthHistory()]);
  await loadConversationList();
  if (state.conversationId) await openConversation(state.conversationId);
  else newConversation();
}

async function init() {
  trackEvent('landing_viewed', {screen:'entry'});
  trackEvent('app_opened', {app_mode:isInstalledApp() ? 'standalone' : 'browser'});
  try {
    state.publicConfig = await api('/api/public-config');
    const identity = await api('/api/me');
    state.identity = identity;
    consumeCompletedMessengerLink(identity);
    updateMessengerLinkMenu();
    const entryParams = new URLSearchParams(window.location.search);
    const messengerLoginRequired = entryParams.get('auth') === 'messenger_required';
    const messengerLoginCompleted = entryParams.get('auth') === 'messenger_login';
    const forceWelcomePreview = entryParams.get('welcome') === '1';
    const resultEntryRequested = isResultEntryUrl();
    const pendingResultFlow = readResultFlow();
    if (messengerLoginCompleted) {
      entryParams.delete('auth');
      const cleanQuery = entryParams.toString();
      history.replaceState({}, '', `${window.location.pathname}${cleanQuery ? `?${cleanQuery}` : ''}`);
    }
    if ((resultEntryRequested || pendingResultFlow) && !messengerLoginRequired) {
      await enterResultFlow({explicit:resultEntryRequested && !pendingResultFlow});
      return;
    }
    if (identity.authenticated) {
      localStorage.removeItem(ANONYMOUS_ACCESS_KEY);
      if (forceWelcomePreview) showWelcome(startApplication);
      else if (!(await handlePaymentReturn())) await enterKnownUser();
    } else if (messengerLoginRequired) {
      showAuthGate();
      setAuthStatus('Эта ссылка принадлежит другому пользователю. Войдите через свой мессенджер.', true);
    } else if (localStorage.getItem(ANONYMOUS_ACCESS_KEY) === identity.chel_id) {
      if (forceWelcomePreview) showWelcome(startApplication);
      else if (!(await handlePaymentReturn())) await enterKnownUser();
    } else {
      if (forceWelcomePreview) showWelcome(() => showAuthGate());
      else if (localStorage.getItem(WELCOME_SEEN_KEY)) showAuthGate();
      else showWelcome(() => showAuthGate());
    }
  }
  catch (error) {
    showAuthGate();
    setAuthStatus(`Не удалось загрузить сервис: ${error.message}`, true);
  }
}
window.addEventListener('error', () => trackEvent('javascript_error', {error_code:'window_error',screen:'browser'}));
window.addEventListener('unhandledrejection', () => trackEvent('javascript_error', {error_code:'unhandled_rejection',screen:'browser'}));
window.addEventListener('load', () => {
  const navigation = performance.getEntriesByType?.('navigation')?.[0];
  if (navigation) trackEvent('performance_measured', {duration_ms:Math.round(navigation.duration),screen:'page_load'});
});
setInterval(syncConversationUpdates, 3000);
document.addEventListener('pointerdown', unlockUserSound, {once:true});
document.addEventListener('keydown', unlockUserSound, {once:true});
armBackNavigationGuard();
init();
