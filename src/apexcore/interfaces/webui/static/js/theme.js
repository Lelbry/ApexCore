// Theme management для WebUI ApexCore (dark ↔ light).
//
// Светлая тема нужна для двух сценариев:
//   1. Скриншоты в магистерскую диссертацию (тёмный фон плохо
//      печатается на белой бумаге);
//   2. Работа при ярком окружении.
//
// Архитектура — атрибут [data-theme] на <html>, переменные в tokens.css
// разнесены по двум блокам :root[data-theme="dark"] и :root[data-theme="light"].
// Никакого FOUC: index.html устанавливает data-theme="dark" сразу + inline
// script подхватывает сохранённую тему до загрузки CSS.
//
// Палитра светлой темы синхронизирована с installer wizard
// (tokens-installer.css :root[data-theme="light"]) — accent #2872d4,
// score #15a866. Это даёт визуальную согласованность инсталлера и
// дашборда.

const STORAGE_KEY = 'apexcore-theme';

export function getTheme() {
  return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
}

export function setTheme(theme) {
  const next = theme === 'light' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  try {
    localStorage.setItem(STORAGE_KEY, next);
  } catch {
    // SecurityError в incognito с заблокированным storage — игнор.
    // Тема всё равно применилась к атрибуту, просто не персистнется.
  }
  window.dispatchEvent(new CustomEvent('theme:change', { detail: { theme: next } }));
  return next;
}

export function toggleTheme() {
  return setTheme(getTheme() === 'dark' ? 'light' : 'dark');
}

export function initTheme(defaultTheme = 'dark') {
  let stored = null;
  try {
    stored = localStorage.getItem(STORAGE_KEY);
  } catch {
    // ignore
  }
  setTheme(stored === 'light' || stored === 'dark' ? stored : defaultTheme);
}
