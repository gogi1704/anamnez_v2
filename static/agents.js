/* Public UI metadata. Agent behavior and prompts live securely in Python. */
const AGENTS = {
  manager: { id: 'manager', name: 'Мария', role: 'ИИ-менеджер', initials: 'МН', icon: '✦', group: 'coordination' },
  safety: { id: 'safety', name: 'Алексей', role: 'Контроль безопасности', initials: 'АБ', icon: '◇', group: 'coordination' },
  therapist: { id: 'therapist', name: 'Ирина', role: 'Терапевт', initials: 'ИТ', icon: '✚', group: 'medical' },
  cardiologist: { id: 'cardiologist', name: 'Дмитрий', role: 'Кардиолог', initials: 'ДК', icon: '♡', group: 'medical' },
  neurologist: { id: 'neurologist', name: 'Ольга', role: 'Невролог', initials: 'ОН', icon: '⌁', group: 'medical' },
  dermatologist: { id: 'dermatologist', name: 'Анна', role: 'Дерматолог', initials: 'АД', icon: '◌', group: 'medical' },
  pediatrician: { id: 'pediatrician', name: 'Сергей', role: 'Педиатр', initials: 'СП', icon: '☆', group: 'medical' },
  psychologist: { id: 'psychologist', name: 'Елена', role: 'Психолог', initials: 'ЕП', icon: '☼', group: 'medical' },
  general: { id: 'general', name: 'Максим', role: 'Здоровье и образ жизни', initials: 'МО', icon: '◫', group: 'general' },
};

window.AGENTS = AGENTS;
