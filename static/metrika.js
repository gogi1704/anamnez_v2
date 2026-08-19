(function initializeConsiliumMetrika() {
  'use strict';

  const safeGoals = new Set([
    'welcome_continue', 'registration_max', 'registration_telegram',
    'registration_anonymous', 'font_size_selected', 'questionnaire_started',
    'questionnaire_completed', 'exam_offer_viewed', 'exam_options_opened',
    'exam_selection_completed', 'payment_online', 'payment_at_exam',
    'onboarding_completed', 'capabilities_viewed', 'chat_opened',
    'first_message_sent', 'human_requested', 'install_clicked',
  ]);
  let counterId = null;
  let counterReady = false;
  const pendingGoals = [];

  function markElements(root, selector, className) {
    if (root?.nodeType === Node.ELEMENT_NODE && root.matches(selector)) {
      root.classList.add(className);
    }
    root.querySelectorAll?.(selector).forEach(element => element.classList.add(className));
  }

  function protectSensitiveContent(root = document) {
    markElements(root, 'input, textarea, select, [contenteditable="true"]', 'ym-disable-keys');
    markElements(root, 'form', 'ym-disable-submit');

    // Оставляем Метрике структуру интерфейса и кнопки, но скрываем только
    // пользовательские медицинские данные и содержимое диалогов.
    [
      '#messages', '#conversationList', '#attachmentList', '#timeline',
      '#handoffPreview', '#ticketNumber', '#memoryList',
      '#profileStatus', '#profileCompletion', '#bodyMapStatus',
      '#bodySymptomCount', '#healthHistoryCount',
      '#humanModal', '#contextModal', '#bodyMapModal', '#healthHistoryModal',
      '#profileModal', '#labResultsModal',
    ].forEach(selector => markElements(root, selector, 'ym-hide-content'));

    [
      '#messages button', '#humanModal button', '#contextModal button',
      '#bodyMapModal button', '#healthHistoryModal button',
      '#profileModal button', '#labResultsModal button',
    ].forEach(selector => markElements(root, selector, 'ym-show-content'));
  }

  function sendGoal(goal) {
    if (!safeGoals.has(goal)) return;
    if (!counterReady || !counterId || typeof window.ym !== 'function') {
      if (!pendingGoals.includes(goal)) pendingGoals.push(goal);
      return;
    }
    window.ym(counterId, 'reachGoal', goal);
  }

  window.consiliumMetrikaGoal = sendGoal;

  function loadCounter() {
    if (!counterId || document.querySelector('script[data-consilium-metrika]')) return;
    protectSensitiveContent();
    new MutationObserver(mutations => {
      for (const mutation of mutations) {
        for (const node of mutation.addedNodes) {
          if (node.nodeType === Node.ELEMENT_NODE) protectSensitiveContent(node);
        }
      }
    }).observe(document.body, { childList:true, subtree:true });
    window.ym = window.ym || function metrikaQueue() {
      (window.ym.a = window.ym.a || []).push(arguments);
    };
    window.ym.l = Date.now();
    const script = document.createElement('script');
    script.async = true;
    script.dataset.consiliumMetrika = '1';
      script.src = 'https://mc.yandex.ru/metrika/tag.js';
      script.onload = () => {
        window.ym(counterId, 'init', {
          accurateTrackBounce: true,
          clickmap: true,
          defer: true,
          sendTitle: false,
          trackLinks: true,
          webvisor: true,
        });
        window.ym(counterId, 'hit', `${location.origin}${location.pathname}`);
        counterReady = true;
        while (pendingGoals.length) sendGoal(pendingGoals.shift());
      };
    document.head.append(script);
  }

  fetch('/api/public-config', { credentials: 'same-origin', cache: 'no-store' })
    .then(response => response.ok ? response.json() : {})
    .then(config => {
      const rawId = String(config.yandex_metrika_counter_id || '');
      if (!/^\d{5,12}$/.test(rawId)) return;
      counterId = Number(rawId);
      loadCounter();
    })
    .catch(() => {});
})();
