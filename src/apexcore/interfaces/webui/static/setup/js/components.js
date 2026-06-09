// Atomic DOM-helpers + shared installer components (WindowChrome, StepRail, FooterBar,
// PrimaryBtn, SecondaryBtn, Checkbox, Radio, WipBanner). Порт из installer-shared.jsx.

import { toggleTheme, getTheme } from './theme.js';

/* ─── DOM helpers ──────────────────────────────────────────────────────── */

export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class' || k === 'className') {
      if (v) node.setAttribute('class', v);
    } else if (k === 'style' && typeof v === 'object') {
      Object.assign(node.style, v);
    } else if (k === 'html') {
      node.innerHTML = v;
    } else if (k === 'text') {
      node.textContent = v;
    } else if (k.startsWith('on') && typeof v === 'function') {
      node.addEventListener(k.slice(2).toLowerCase(), v);
    } else if (k === 'data' && typeof v === 'object') {
      for (const [dk, dv] of Object.entries(v)) node.dataset[dk] = dv;
    } else if (v != null && v !== false) {
      node.setAttribute(k, String(v));
    }
  }
  if (children) {
    const arr = Array.isArray(children) ? children : [children];
    for (const c of arr) {
      if (c == null || c === false) continue;
      node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    }
  }
  return node;
}

export function svgEl(tag, attrs = {}, children = []) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v != null) node.setAttribute(k, String(v));
  }
  if (children) {
    const arr = Array.isArray(children) ? children : [children];
    for (const c of arr) if (c) node.appendChild(c);
  }
  return node;
}

/* ─── Steps registry ───────────────────────────────────────────────────── */

export const INSTALLER_STEPS = [
  { key: 'welcome',    n: '01', label: 'Добро пожаловать' },
  { key: 'license',    n: '02', label: 'Лицензия' },
  { key: 'location',   n: '03', label: 'Папка установки' },
  { key: 'components', n: '04', label: 'Компоненты' },
  { key: 'progress',   n: '05', label: 'Установка' },
  { key: 'done',       n: '06', label: 'Готово' },
];

/* ─── WindowChrome ─────────────────────────────────────────────────────── */

export function renderWindowChrome({ title, onToggleTheme, onWindowAction }) {
  const chrome = el('div', { class: 'window-chrome' });

  const toggleBtn = el('button', {
    class: 'window-chrome__theme-toggle',
    type: 'button',
    onClick: () => {
      const t = toggleTheme();
      toggleBtn.textContent = t === 'dark' ? '☀ light' : '☾ dark';
      if (onToggleTheme) onToggleTheme(t);
    },
  });
  toggleBtn.textContent = getTheme() === 'dark' ? '☀ light' : '☾ dark';
  window.addEventListener('theme:change', (ev) => {
    toggleBtn.textContent = ev.detail.theme === 'dark' ? '☀ light' : '☾ dark';
  });
  chrome.appendChild(toggleBtn);

  chrome.appendChild(el('span', { class: 'window-chrome__title', text: title }));

  const controls = el('div', { class: 'window-chrome__controls' });
  const mkBtn = (titleAttr, svgInner, extraClass = '') => {
    const b = el('button', {
      class: `window-chrome__btn ${extraClass}`.trim(),
      type: 'button',
      title: titleAttr,
      onClick: () => onWindowAction && onWindowAction(titleAttr.toLowerCase() === 'закрыть' ? 'close'
        : titleAttr.toLowerCase() === 'развернуть' ? 'maximize' : 'minimize'),
    });
    b.innerHTML = `<svg width="10" height="10" viewBox="0 0 10 10">${svgInner}</svg>`;
    return b;
  };
  controls.appendChild(mkBtn('Свернуть',  '<line x1="1" y1="5" x2="9" y2="5" stroke="currentColor" stroke-width="1"/>'));
  controls.appendChild(mkBtn('Развернуть', '<rect x="1" y="1" width="8" height="8" fill="none" stroke="currentColor" stroke-width="1"/>'));
  controls.appendChild(mkBtn('Закрыть',
    '<line x1="1" y1="1" x2="9" y2="9" stroke="currentColor" stroke-width="1"/>' +
    '<line x1="9" y1="1" x2="1" y2="9" stroke="currentColor" stroke-width="1"/>',
    'window-chrome__btn--close'));
  chrome.appendChild(controls);

  return chrome;
}

/* ─── StepRail ─────────────────────────────────────────────────────────── */

export function renderStepRail({ currentStep, version, platform }) {
  const rail = el('div', { class: 'step-rail' });

  const brand = el('div', { class: 'step-rail__brand' }, [
    el('img', {
      class: 'step-rail__brand-logo',
      // Абсолютный путь работает и под FastAPI (/static/setup/assets/...)
      // и под WebView2 (https://apexcore-setup.localhost/assets/...).
      // Bootstrapper копирует apex-logo.png в Resources/wwwroot/assets/
      // и подменяет это URL через AddScriptToExecuteOnDocumentCreatedAsync.
      src: window.__SETUP_LOGO_URL__ || '/static/setup/assets/apex-logo.png',
      width: 52,
      height: 52,
      alt: 'ApexCore',
    }),
    el('div', { class: 'step-rail__brand-text' }, [
      el('div', { class: 'step-rail__brand-name', text: 'ApexCore' }),
      el('div', { class: 'step-rail__brand-version', text: `v${version || '0.0.0'} · setup` }),
    ]),
  ]);
  rail.appendChild(brand);

  const list = el('div', { class: 'step-rail__list' });
  const currentIdx = INSTALLER_STEPS.findIndex((s) => s.key === currentStep);
  for (let i = 0; i < INSTALLER_STEPS.length; i++) {
    const step = INSTALLER_STEPS[i];
    const done = i < currentIdx;
    const active = step.key === currentStep;
    const future = i > currentIdx;
    const cls = ['step-rail__item',
      active && 'step-rail__item--active',
      done   && 'step-rail__item--done',
      future && 'step-rail__item--future',
    ].filter(Boolean).join(' ');
    list.appendChild(el('div', { class: cls }, [
      el('div', { class: 'step-rail__badge', text: done ? '✓' : step.n }),
      el('span', { class: 'step-rail__label', text: step.label }),
    ]));
  }
  rail.appendChild(list);

  rail.appendChild(el('div', { class: 'step-rail__spacer' }));
  const isLinux = platform === 'linux';
  rail.appendChild(el('div', {
    class: 'step-rail__footnote',
    html: `${isLinux ? 'Astra Linux · x64' : 'Windows 11 · x64'}<br>open-source<br>~140 MB на диске`,
  }));

  return rail;
}

/* ─── FooterBar ─────────────────────────────────────────────────────────── */

export function renderFooterBar({
  onCancel, onBack, onNext,
  nextLabel = 'Далее',
  backDisabled = false,
  nextDisabled = false,
  footnote = null,
  nextPrimary = true,
}) {
  const footer = el('div', { class: 'footer-bar' });

  const cancelBtn = el('button', {
    class: 'btn btn-secondary',
    type: 'button',
    onClick: onCancel,
    text: 'Отмена',
  });
  const backBtn = el('button', {
    class: 'btn btn-secondary',
    type: 'button',
    onClick: onBack,
    text: '← Назад',
  });
  if (backDisabled) backBtn.disabled = true;
  footer.appendChild(cancelBtn);
  footer.appendChild(backBtn);

  if (footnote) {
    footer.appendChild(el('span', { class: 'footer-bar__footnote', text: footnote }));
  }
  footer.appendChild(el('div', { class: 'footer-bar__spacer' }));

  const nextBtn = el('button', {
    class: nextPrimary ? 'btn btn-primary' : 'btn btn-secondary',
    type: 'button',
    onClick: onNext,
    text: nextPrimary ? `${nextLabel} →` : nextLabel,
  });
  if (nextDisabled) nextBtn.disabled = true;
  footer.appendChild(nextBtn);

  return footer;
}

/* ─── PrimaryBtn / SecondaryBtn (для встраивания вне footer) ───────────── */

export function primaryBtn(label, onClick, disabled = false) {
  const b = el('button', { class: 'btn btn-primary', type: 'button', onClick, text: label });
  if (disabled) b.disabled = true;
  return b;
}

export function secondaryBtn(label, onClick, disabled = false) {
  const b = el('button', { class: 'btn btn-secondary', type: 'button', onClick, text: label });
  if (disabled) b.disabled = true;
  return b;
}

/* ─── Checkbox / Radio ─────────────────────────────────────────────────── */

export function renderCheckbox({ on = false, locked = false, label, sub, onChange }) {
  const root = el('label', {
    class: `cb ${on ? 'cb--on' : ''} ${locked ? 'cb--locked' : ''}`.trim(),
  });
  const box = el('span', { class: 'cb__box' });
  root.appendChild(box);
  const txt = el('div', { class: 'cb__text' }, [
    el('span', { class: 'cb__label', text: label }),
    sub ? el('span', { class: 'cb__sub', text: sub }) : null,
  ]);
  root.appendChild(txt);
  if (!locked) {
    root.addEventListener('click', (ev) => {
      ev.preventDefault();
      const next = !root.classList.contains('cb--on');
      root.classList.toggle('cb--on', next);
      if (onChange) onChange(next);
    });
  }
  return root;
}

export function renderRadio({ on = false, label, sub, onChange }) {
  const root = el('label', { class: `rb ${on ? 'rb--on' : ''}`.trim() });
  const box = el('span', { class: 'rb__box' }, [el('span', { class: 'rb__dot' })]);
  root.appendChild(box);
  root.appendChild(el('div', { class: 'cb__text' }, [
    el('span', { class: 'cb__label', text: label }),
    sub ? el('span', { class: 'cb__sub', text: sub }) : null,
  ]));
  if (onChange) {
    root.addEventListener('click', (ev) => {
      ev.preventDefault();
      onChange(true);
    });
  }
  return root;
}

/* ─── WipBanner ─────────────────────────────────────────────────────────── */

export function renderWipBanner(text = 'раздел находится в разработке') {
  return el('div', { class: 'wip-banner' }, [
    el('span', { class: 'wip-banner__badge', text: 'WIP' }),
    el('span', { class: 'wip-banner__text', text }),
  ]);
}

/* ─── Section tag ───────────────────────────────────────────────────────── */

export function sectionTag(text, { score = false } = {}) {
  return el('div', { class: `section-tag ${score ? 'section-tag--score' : ''}`.trim(), text });
}
