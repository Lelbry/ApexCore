// Theme toggle (dark ↔ light) для setup wizard'а.
//
// Дизайн-решение: wizard НЕ персистит тему между запусками. Каждый запуск
// (первый или повторный через `apexcore setup --force`) открывается в
// дизайн-default тёмной теме. Переключение на light доступно прямо в
// title bar wizard'а, действует только до закрытия окна. Это даёт
// предсказуемый first-run experience независимо от localStorage в браузере.
//
// Если в будущем понадобится persistent тема — сделать через ?theme=light
// query-param от bootstrapper.exe / apexcore setup --theme=light.

export function getTheme() {
  return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
}

export function setTheme(theme) {
  const next = theme === 'light' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  window.dispatchEvent(new CustomEvent('theme:change', { detail: { theme: next } }));
  return next;
}

export function toggleTheme() {
  return setTheme(getTheme() === 'dark' ? 'light' : 'dark');
}

export function initTheme(defaultTheme = 'dark') {
  // Сознательно игнорируем localStorage — wizard всегда дизайн-default.
  setTheme(defaultTheme);
}
