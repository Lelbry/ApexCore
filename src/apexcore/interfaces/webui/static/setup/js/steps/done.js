// 06 · Done
import { el, sectionTag, renderCheckbox } from '../components.js';

export function renderDone({ state }) {
  const root = el('div', { class: 'step step-done' });
  root.appendChild(sectionTag('// 06 · ГОТОВО', { score: true }));
  root.appendChild(el('h1', { class: 'h1', text: 'ApexCore установлен.' }));

  const isLinux = state.bridge?.platform === 'linux';
  const url = `http://127.0.0.1:8765${isLinux ? '/dashboard' : ''}`;
  const p = el('p', { class: 'p p--large' });
  p.innerHTML = `Можно запускать. Web UI откроется в браузере по адресу ` +
    `<span class="p--accent mono">${url}</span>.`;
  root.appendChild(p);

  // 2 callout cards
  const cards = el('div', { class: 'step-done__cards' });
  const platformLabel = isLinux ? 'Astra Linux' : 'Windows 11';
  cards.appendChild(el('div', { class: 'step-done__card step-done__card--score' }, [
    el('div', { class: 'step-done__card-label', text: '// УСТАНОВЛЕНО' }),
    el('div', { class: 'step-done__card-value', text: isLinux ? '5 компонентов' : '5 компонентов' }),
    el('div', { class: 'step-done__card-meta', text: `${isLinux ? '107 MB' : '126 MB'} · ${platformLabel}` }),
  ]));
  cards.appendChild(el('div', { class: 'step-done__card step-done__card--next' }, [
    el('div', { class: 'step-done__card-label', text: '// СЛЕДУЮЩИЙ ШАГ' }),
    el('div', { class: 'step-done__card-value', text: '$ apexcore webui' }),
    el('div', {
      class: 'step-done__card-meta',
      text: isLinux ? 'или закладка в браузере на /dashboard' : 'или используйте ярлык на рабочем столе',
    }),
  ]));
  root.appendChild(cards);

  // Final 4 checkboxes
  const choices = state.finishChoices || {
    launchWebUI: true,
    launchCLI: false,
    desktopShortcut: false,
    openReadme: false,
  };
  state.finishChoices = choices;

  const cbBox = el('div', { class: 'step-done__checkboxes' });
  cbBox.appendChild(renderCheckbox({
    on: choices.launchWebUI,
    label: 'Запустить ApexCore Web UI сразу',
    sub: `Откроется браузер на ${url}`,
    onChange: (v) => { choices.launchWebUI = v; },
  }));
  if (!isLinux) {
    cbBox.appendChild(renderCheckbox({
      on: choices.launchCLI,
      label: 'Запустить консольное приложение ApexCore (CLI)',
      sub: 'PowerShell с TUI-меню · от администратора (UAC) — нужно для полного первого прогона Winsat',
      onChange: (v) => { choices.launchCLI = v; },
    }));
    cbBox.appendChild(renderCheckbox({
      on: choices.desktopShortcut,
      label: 'Создать ярлык на рабочем столе',
      onChange: (v) => { choices.desktopShortcut = v; },
    }));
  } else {
    // Astra: CLI запускается без прав администратора — базовые сенсоры
    // (CPU/GPU/диск через hwmon) доступны обычному пользователю, а capability
    // для dmidecode мастер уже выставил на шаге установки.
    cbBox.appendChild(renderCheckbox({
      on: choices.launchCLI,
      label: 'Запустить консольное приложение ApexCore (CLI)',
      sub: 'Откроется терминал с интерактивным меню · команда apexcore',
      onChange: (v) => { choices.launchCLI = v; },
    }));
  }
  cbBox.appendChild(renderCheckbox({
    on: choices.openReadme,
    label: 'Открыть README.md в браузере',
    onChange: (v) => { choices.openReadme = v; },
  }));
  root.appendChild(cbBox);

  return { node: root, hasBackground: true };
}
