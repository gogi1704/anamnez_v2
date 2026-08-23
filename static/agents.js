/* Public UI metadata. Agent behavior and prompts live securely in Python. */
const MEDICAL_ASSISTANT = Object.freeze({
  name: 'Ольга', role: 'Медицинский помощник', initials: 'ОП', icon: '✦', group: 'assistant',
});

// Идентификаторы сохранены для старых диалогов и внутренней маршрутизации.
// Пользователь всегда видит одного медицинского помощника — Ольгу.
const AGENTS = Object.fromEntries([
  'manager', 'safety', 'therapist', 'cardiologist', 'neurologist',
  'dermatologist', 'pediatrician', 'psychologist', 'general',
].map(id => [id, { id, ...MEDICAL_ASSISTANT }]));

window.AGENTS = AGENTS;
