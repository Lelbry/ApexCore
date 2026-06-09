// Sidebar: 10 пунктов навигации в порядке частоты использования.
// Winsat прячется на Linux.

import { state, subscribe } from '../store.js';

const ITEMS = [
  // [index, hash, icon, label, options]
  ['01', '#dashboard',    '▦', 'Dashboard',                  {}],
  ['02', '#sensors',      '◉', 'Sensors',                    {}],
  ['03', '#stress',       '⚡', 'Стресс-тест',                {}],
  ['04', '#general',      '⏱', 'Общая оценка системы',       {}],
  ['05', '#cpu-advanced', '✦', 'Расш. тест CPU',             {}],
  ['06', '#ramcache',     '▤', 'Ram & Cache',                {}],
  ['07', '#winsat',       '◷', 'Наследие Winsat',            { windowsOnly: true }],
  ['08', '#history',      '☷', 'История',                    {}],
  ['09', '#diagnose',     '⚕', 'Diagnose',                   {}],
  ['10', '#settings',     '⚙', 'Settings',                   {}],
];

export function renderSidebar(host) {
  host.innerHTML = `<div class="sidebar__group-title">// Navigation</div>` +
    ITEMS.map(renderItem).join('');
  attachClicks(host);
  refreshActive();
  refreshPlatform();
  window.addEventListener('hashchange', refreshActive);
  subscribe((ev) => {
    if (ev.type === 'config') refreshPlatform();
  });
}

function renderItem([index, hash, icon, label, opts]) {
  const platformClass = opts.windowsOnly ? 'js-windows-only' : '';
  return `<a class="sidebar__item ${platformClass}" href="${hash}" data-hash="${hash}">
    <span class="sidebar__item__index">${index}</span>
    <span class="sidebar__item__icon">${icon}</span>
    <span>${label}</span>
  </a>`;
}

function attachClicks(host) {
  for (const el of host.querySelectorAll('.sidebar__item')) {
    el.addEventListener('click', (e) => {
      // браузер сам поменяет hash; refreshActive вызовется через hashchange.
    });
  }
}

function refreshActive() {
  const hash = window.location.hash || '#dashboard';
  for (const el of document.querySelectorAll('.sidebar__item')) {
    el.classList.toggle('active', el.dataset.hash === hash);
  }
}

function refreshPlatform() {
  const platform = state.config?.platform;
  if (!platform) return;
  for (const el of document.querySelectorAll('.js-windows-only')) {
    if (platform === 'windows') {
      el.classList.remove('disabled');
      el.title = '';
    } else {
      el.classList.add('disabled');
      el.title = 'Доступно только на Windows';
    }
  }
}
