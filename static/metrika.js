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
  const pendingGoals = [];

  function sendGoal(goal) {
    if (!safeGoals.has(goal)) return;
    if (!counterId || typeof window.ym !== 'function') {
      if (!pendingGoals.includes(goal)) pendingGoals.push(goal);
      return;
    }
    window.ym(counterId, 'reachGoal', goal);
  }

  window.consiliumMetrikaGoal = sendGoal;

  function loadCounter() {
    if (!counterId || document.querySelector('script[data-consilium-metrika]')) return;
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
          clickmap: false,
          defer: true,
        trackLinks: false,
        webvisor: false,
      });
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
