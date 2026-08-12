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

  function protectSensitiveContent(root = document) {
    root.querySelectorAll?.('input, textarea, select, [contenteditable="true"]')
      .forEach(element => element.classList.add('ym-disable-keys'));
    root.querySelectorAll?.('form')
      .forEach(element => element.classList.add('ym-disable-submit'));
    [
      '#onboarding', '#appShell', '#humanModal', '#contextModal',
      '#bodyMapModal', '#healthHistoryModal', '#profileModal', '#labResultsModal',
    ].forEach(selector => document.querySelector(selector)?.classList.add('ym-hide-content'));
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
