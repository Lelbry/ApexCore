// Расширенное тестирование процессора.
//
// Два главных режима экрана:
// - 01 Single / Multi сравнение — один движок (int_iops_64) на 1 P-ядре vs все
//   потоки, даёт speedup и эффективность масштабирования.
// - 02 Полный прогон — 12 микробенчмарков по 5 категориям, итоговый балл
//   системы с интервалом устойчивости. Реализуется через ScoringService с
//   фиксированным пресетом standard (3 прогона) — выбор точности скрыт,
//   для accurate (95% CI) пользователь идёт в CLI.
//
// Точечный запуск отдельных движков доступен только из CLI:
// `apexcore micro run --tests aes_256` (не критично для веба).
//
// Технические аннотации (// /api/.., // run UUID, scoring-version, raw
// field-names) в UI намеренно не показываются.

import { api } from '../api.js';
import { fmtNum } from '../format.js';

// Канонический список 12 микробенчмарков (по infrastructure/microbench/registry.py).
// Используется для блока «Выбрать тесты» внутри карточки «Полный прогон».
// label — что видит пользователь, name — внутреннее имя, передаваемое в backend.
const ALL_MICROS = [
  { category: 'Оперативная память', items: [
    { name: 'memory_read',   label: 'Read'  },
    { name: 'memory_write',  label: 'Write' },
    { name: 'memory_copy',   label: 'Copy'  },
  ]},
  { category: 'Вычисления с плавающей запятой', items: [
    { name: 'flops_sp',      label: 'FP32 (single precision)' },
    { name: 'flops_dp',      label: 'FP64 (double precision)' },
  ]},
  { category: 'Целочисленные операции', items: [
    { name: 'int_iops_24',   label: 'INT24 IOPS' },
    { name: 'int_iops_32',   label: 'INT32 IOPS' },
    { name: 'int_iops_64',   label: 'INT64 IOPS' },
  ]},
  { category: 'Криптография', items: [
    { name: 'aes_256',       label: 'AES-256' },
    { name: 'sha1',          label: 'SHA-1'   },
  ]},
  { category: 'Фракталы (графика)', items: [
    { name: 'julia_sp',      label: 'Julia (fp32)'      },
    { name: 'mandelbrot_dp', label: 'Mandelbrot (fp64)' },
  ]},
];
const TOTAL_MICROS = ALL_MICROS.reduce((sum, g) => sum + g.items.length, 0);
const ALL_MICRO_NAMES = ALL_MICROS.flatMap(g => g.items.map(i => i.name));

// Текущее состояние выбора (Set имён). По умолчанию — все 12.
const selectedTests = new Set(ALL_MICRO_NAMES);

// Человеческие подписи + краткое пояснение к каждому микробенчу — для
// блока «Сырые значения» (вместо технических имён memory_read / flops_dp).
const MICRO_META = {
  memory_read:   { label: 'Чтение',           hint: 'последовательное чтение из ОЗУ' },
  memory_write:  { label: 'Запись',            hint: 'последовательная запись в ОЗУ' },
  memory_copy:   { label: 'Копирование',       hint: 'поток memcpy ОЗУ→ОЗУ' },
  flops_sp:      { label: 'FP32',              hint: 'плавающая запятая, одинарная точность' },
  flops_dp:      { label: 'FP64',              hint: 'плавающая запятая, двойная точность' },
  int_iops_24:   { label: 'INT24',             hint: '24-битные целочисленные операции' },
  int_iops_32:   { label: 'INT32',             hint: '32-битные целочисленные операции' },
  int_iops_64:   { label: 'INT64',             hint: '64-битные целочисленные операции' },
  aes_256:       { label: 'AES-256',           hint: 'шифрование (аппаратный AES-NI)' },
  sha1:          { label: 'SHA-1',             hint: 'криптографическое хеширование' },
  julia_sp:      { label: 'Julia (fp32)',      hint: 'фрактал, итеративный fp32' },
  mandelbrot_dp: { label: 'Mandelbrot (fp64)', hint: 'фрактал, итеративный fp64' },
};

const MOCK_DONE = {
  id: '7c4f3a91-2b8e-4d5a-9f1c-8e6b2a1c4d5e',
  duration_sec: 248,
  single: {
    score: 612,
    core_pinned: 'P-core 0',
    frequency_ghz: 4.90,
    flops_dp: 64.2,
    int_iops: 8.3,
    aes_256: 4280,
  },
  multi: {
    score: 1960,
    threads: 24,
    frequency_ghz: 4.28,
    flops_dp: 612.4,
    int_iops: 158.6,
    aes_256: 76400,
  },
  speedup: 3.2,
  efficiency: 0.13,  // 3.2 / 24 = 0.133
  notes: ['hybrid Intel · 8P+8E', 'E-cores ~85% частоты P'],
  ranking: {
    single_percentile: 18,
    multi_percentile: 22,
    neighbours_single: [
      { cpu: 'Apple M3 Max',           score: 690, here: false },
      { cpu: 'AMD Ryzen 9 7950X3D',    score: 645, here: false },
      { cpu: 'Intel Core i9-13900K',   score: 628, here: false },
      { cpu: 'Intel Core i9-12900K',   score: 612, here: true  },
      { cpu: 'AMD Ryzen 9 7900X',      score: 598, here: false },
      { cpu: 'Intel Core i7-13700K',   score: 580, here: false },
      { cpu: 'AMD Ryzen 7 7700X',      score: 562, here: false },
    ],
    neighbours_multi: [
      { cpu: 'AMD Threadripper 7980X', score: 4820, here: false },
      { cpu: 'Intel Xeon W-3495X',     score: 4120, here: false },
      { cpu: 'AMD Ryzen 9 7950X3D',    score: 2480, here: false },
      { cpu: 'Intel Core i9-13900K',   score: 2210, here: false },
      { cpu: 'Intel Core i9-12900K',   score: 1960, here: true  },
      { cpu: 'AMD Ryzen 9 5950X',      score: 1840, here: false },
      { cpu: 'AMD Ryzen 7 7700X',      score: 1620, here: false },
    ],
  },
};

let view = 'idle';           // idle | running | done
let mockMode = false;        // true = «Посмотреть пример», false = real
let realResult = null;       // last_result из /api/micro/status (single_multi | full_run)
let runningMode = null;      // 'single_multi' | 'full_run' — что мы только что запустили
let pollHandle = null;

export function render(host) {
  renderHost(host);
  void syncStatus(host);
}

export function dispose() {
  if (pollHandle) { clearInterval(pollHandle); pollHandle = null; }
}

function renderHost(host) {
  if (view === 'running') return renderRunning(host);
  if (view === 'done')    return renderDone(host);
  return renderIdle(host);
}

// ─── status sync (real backend) ──────────────────────────────────────

async function syncStatus(host) {
  if (mockMode) return;
  try {
    const s = await api.microStatus();
    if (s.running) {
      view = 'running';
      renderHost(host);
      if (!pollHandle) pollHandle = setInterval(() => { void syncStatus(host); }, 1500);
      updateRunningProgress(s);
    } else if (s.status === 'completed' && s.last_result) {
      if (pollHandle) { clearInterval(pollHandle); pollHandle = null; }
      realResult = s.last_result;
      view = 'done';
      renderHost(host);
    } else if (s.status === 'failed') {
      if (pollHandle) { clearInterval(pollHandle); pollHandle = null; }
      realResult = { error: s.error || 'Прогон упал' };
      view = 'done';
      renderHost(host);
    }
  } catch { /* tихо */ }
}

async function onStartSingleMulti(host) {
  try {
    // bench и threads опускаем — backend использует те же дефолты, что и
    // CLI-меню («Тест Single-Core / Multi-Core»): int_iops_64 + все логические
    // потоки. Это даёт идентичные с CLI числа.
    await api.microStartSingleMulti({ duration_sec: 5.0 });
    runningMode = 'single_multi';
    view = 'running';
    renderHost(host);
    if (!pollHandle) pollHandle = setInterval(() => { void syncStatus(host); }, 1500);
  } catch (err) {
    alert('Не удалось запустить: ' + err.message);
  }
}

async function onStartFullRun(host) {
  if (selectedTests.size === 0) {
    alert('Выберите хотя бы один тест.');
    return;
  }
  // Если выбраны все — не передаём поле tests (backend сам возьмёт полный набор).
  const body = { duration_sec: 5.0, threads: 0 };
  if (selectedTests.size < TOTAL_MICROS) {
    body.tests = Array.from(selectedTests);
  }
  try {
    await api.microStartFullRun(body);
    runningMode = 'full_run';
    view = 'running';
    renderHost(host);
    if (!pollHandle) pollHandle = setInterval(() => { void syncStatus(host); }, 1500);
  } catch (err) {
    alert('Не удалось запустить полный прогон: ' + err.message);
  }
}

// ─── IDLE ────────────────────────────────────────────────────────────

function renderIdle(host) {
  host.innerHTML = `
    <div class="cpu-adv-screen">
      ${renderHeader('idle', 'два режима тестирования')}

      <div class="cpu-adv-grid cpu-adv-grid--two">
        ${renderMode1Card()}
        ${renderMode2Card()}
      </div>

      <div style="text-align: center; margin-top: var(--gap-lg);">
        <button class="btn sm" id="btn-cpu-adv-mock">
          Посмотреть пример результата Single / Multi →
        </button>
      </div>
    </div>
  `;
  document.getElementById('btn-cpu-adv-mock')?.addEventListener('click', () => {
    mockMode = true;
    realResult = null;
    view = 'done';
    renderHost(host);
  });
  // Активная кнопка Single/Multi (реальный запуск).
  document.getElementById('btn-mode-single-multi')?.addEventListener('click', () => {
    void onStartSingleMulti(host);
  });
  // Активная кнопка Полный прогон.
  document.getElementById('btn-mode-full-run')?.addEventListener('click', () => {
    void onStartFullRun(host);
  });
  // Бинды чекбоксов выбора тестов.
  bindTestSelection();
}

// ─── Test selection (collapsible «Выбрать тесты») ────────────────────

function bindTestSelection() {
  // Отдельный чекбокс — переключает один тест.
  for (const cb of document.querySelectorAll('.cpu-adv-select__cb')) {
    cb.addEventListener('change', (ev) => {
      const name = ev.target.dataset.name;
      if (ev.target.checked) selectedTests.add(name);
      else                   selectedTests.delete(name);
      syncGroupCheckbox(ev.target.dataset.group);
      updateSelectCount();
      updateFullRunHint();
    });
  }
  // Чекбокс категории — переключает все тесты группы.
  for (const cb of document.querySelectorAll('.cpu-adv-select__group-cb')) {
    cb.addEventListener('change', (ev) => {
      const group = ev.target.dataset.group;
      const on = ev.target.checked;
      for (const item of document.querySelectorAll(`.cpu-adv-select__cb[data-group="${group}"]`)) {
        item.checked = on;
        const name = item.dataset.name;
        if (on) selectedTests.add(name); else selectedTests.delete(name);
      }
      updateSelectCount();
      updateFullRunHint();
    });
  }
  // «Выбрать все» / «Снять все».
  document.getElementById('cpu-adv-select-all')?.addEventListener('click', (ev) => {
    ev.preventDefault();
    setAllSelection(true);
  });
  document.getElementById('cpu-adv-select-none')?.addEventListener('click', (ev) => {
    ev.preventDefault();
    setAllSelection(false);
  });
}

function setAllSelection(on) {
  selectedTests.clear();
  if (on) ALL_MICRO_NAMES.forEach(n => selectedTests.add(n));
  for (const item of document.querySelectorAll('.cpu-adv-select__cb')) {
    item.checked = on;
  }
  for (const grp of document.querySelectorAll('.cpu-adv-select__group-cb')) {
    grp.checked = on;
    grp.indeterminate = false;
  }
  updateSelectCount();
  updateFullRunHint();
}

// Если выбраны не все тесты группы и не ноль — ставим indeterminate.
function syncGroupCheckbox(groupName) {
  const items = document.querySelectorAll(`.cpu-adv-select__cb[data-group="${groupName}"]`);
  const total = items.length;
  let checked = 0;
  items.forEach(i => { if (i.checked) checked++; });
  const grpCb = document.querySelector(`.cpu-adv-select__group-cb[data-group="${groupName}"]`);
  if (!grpCb) return;
  grpCb.checked = checked === total;
  grpCb.indeterminate = checked > 0 && checked < total;
}

function updateSelectCount() {
  const el = document.getElementById('cpu-adv-select-count');
  if (el) el.textContent = `${selectedTests.size}/${TOTAL_MICROS}`;
}

function updateFullRunHint() {
  const el = document.getElementById('cpu-adv-full-hint');
  const btnLabel = document.getElementById('btn-mode-full-run-label');
  const n = selectedTests.size;
  if (el) {
    if (n === TOTAL_MICROS) {
      el.textContent = '≈ 3 минуты. На выходе: итоговый балл · разбивка по категориям.';
    } else if (n === 0) {
      el.textContent = 'Нужно выбрать хотя бы один тест.';
    } else {
      const approxSec = Math.round(n * 5 * 3 + 5); // n тестов × 5 сек × 3 повтора + overhead
      const min = Math.floor(approxSec / 60);
      const sec = approxSec % 60;
      const tStr = min > 0 ? (sec > 0 ? `${min} мин ${sec} с` : `${min} мин`) : `${sec} с`;
      const word = pluralRu(n, ['тест', 'теста', 'тестов']);
      el.textContent = `Выбрано ${n} ${word} из ${TOTAL_MICROS}. Прогон ≈ ${tStr}.`;
    }
  }
  if (btnLabel) {
    btnLabel.textContent = (n === TOTAL_MICROS || n === 0)
      ? '▶ Начать полный прогон'
      : '▶ Начать прогон выбранных тестов';
  }
}

// Простой плюрализатор по правилам русского.
function pluralRu(n, forms) {
  const a = Math.abs(n) % 100;
  const b = a % 10;
  if (a > 10 && a < 20) return forms[2];
  if (b > 1 && b < 5)   return forms[1];
  if (b === 1)          return forms[0];
  return forms[2];
}

function renderMode1Card() {
  return `<div class="card cpu-adv-mode">
    <div class="card__title">
      <span class="cpu-adv-mode__index">01</span>
      <span>Single / Multi сравнение</span>
    </div>
    <div class="cpu-adv-mode__desc">
      Прогон одной нагрузки сначала на <b>одном P-ядре</b>, затем
      <b>на всех потоках CPU</b>. Даёт speedup, эффективность
      масштабирования и место среди популярных процессоров.
    </div>
    <div class="cpu-adv-mode__compare">
      <div class="cpu-adv-mode__compare-item">
        <div class="cpu-adv-mode__compare-label">SINGLE</div>
        <div class="cpu-adv-mode__compare-value">1 поток на P-ядре</div>
        <div class="cpu-adv-mode__compare-hint">pinning · ~5 сек</div>
      </div>
      <div class="cpu-adv-mode__compare-item">
        <div class="cpu-adv-mode__compare-label">MULTI</div>
        <div class="cpu-adv-mode__compare-value">все потоки CPU</div>
        <div class="cpu-adv-mode__compare-hint">P+E (если hybrid) · ~5 сек</div>
      </div>
    </div>
    <div class="cpu-adv-mode__params-label">Параметры</div>
    <div class="cpu-adv-mode__params">
      <div class="param-field">
        <div class="param-field__label">Нагрузка</div>
        <div class="param-field__value">целочисленные IOPS (int64)</div>
      </div>
      <div class="param-field">
        <div class="param-field__label">Длительность</div>
        <div class="param-field__value">по 5 сек на замер</div>
      </div>
    </div>
    <div class="cpu-adv-mode__result-hint">
      На выходе: speedup ×N · эффективность масштабирования.
    </div>
    <button class="btn primary cpu-adv-mode__launch" id="btn-mode-single-multi">▶ Запустить Single / Multi</button>
  </div>`;
}

function renderMode2Card() {
  const CATEGORIES = [
    ['Оперативная память',            'read / write / copy'],
    ['Вычисления с плавающей запятой', 'fp32 · fp64 matmul'],
    ['Целочисленные операции',         'IOPS 64 / 32 / 24'],
    ['Криптография',                   'AES-256 · SHA-1'],
    ['Фракталы (графика)',             'Julia · Mandelbrot'],
  ];
  return `<div class="card cpu-adv-mode">
    <div class="card__title">
      <span class="cpu-adv-mode__index">02</span>
      <span>Полный прогон</span>
    </div>
    <div class="cpu-adv-mode__desc">
      Все <b>12 микробенчмарков</b> по 5 категориям → итоговый
      <b>балл системы</b> с интервалом устойчивости результата.
    </div>
    <div class="cpu-adv-mode__params-label">5 категорий · 12 тестов</div>
    <div class="cpu-adv-mode__categories">
      ${CATEGORIES.map(([cat, sub]) => `
        <div class="cpu-adv-category">
          <div class="cpu-adv-category__head">
            <span class="cpu-adv-category__name">${escapeHtml(cat)}</span>
          </div>
          <div class="cpu-adv-category__sub">${escapeHtml(sub)}</div>
        </div>
      `).join('')}
    </div>
    <div class="cpu-adv-mode__result-hint" id="cpu-adv-full-hint">
      ≈ 3 минуты. На выходе: итоговый балл · разбивка по категориям.
    </div>
    <button class="btn primary cpu-adv-mode__launch" id="btn-mode-full-run">
      <span id="btn-mode-full-run-label">▶ Начать полный прогон</span>
    </button>
    <details class="cpu-adv-select" id="cpu-adv-select-details">
      <summary class="cpu-adv-select__summary">
        <span class="cpu-adv-select__chevron">▸</span>
        <span>Выбрать тесты</span>
        <span class="cpu-adv-select__count" id="cpu-adv-select-count">${TOTAL_MICROS}/${TOTAL_MICROS}</span>
      </summary>
      <div class="cpu-adv-select__body">
        ${ALL_MICROS.map(group => `
          <div class="cpu-adv-select__group">
            <div class="cpu-adv-select__group-head">
              <label class="cpu-adv-select__group-toggle">
                <input type="checkbox" class="cpu-adv-select__group-cb" data-group="${escapeHtml(group.category)}" checked>
                <span>${escapeHtml(group.category)}</span>
              </label>
            </div>
            <div class="cpu-adv-select__items">
              ${group.items.map(item => `
                <label class="cpu-adv-select__item">
                  <input type="checkbox" class="cpu-adv-select__cb"
                         data-name="${escapeHtml(item.name)}"
                         data-group="${escapeHtml(group.category)}"
                         checked>
                  <span>${escapeHtml(item.label)}</span>
                </label>
              `).join('')}
            </div>
          </div>
        `).join('')}
        <div class="cpu-adv-select__actions">
          <button class="btn xs" id="cpu-adv-select-all">Выбрать все</button>
          <button class="btn xs" id="cpu-adv-select-none">Снять все</button>
        </div>
      </div>
    </details>
  </div>`;
}

// ─── RUNNING ─────────────────────────────────────────────────────────

function renderRunning(host) {
  const isFull = runningMode === 'full_run';
  const nSel = selectedTests.size;
  const partial = isFull && nSel > 0 && nSel < TOTAL_MICROS;
  const testsWord = pluralRu(nSel, ['теста', 'тестов', 'тестов']);
  const subtitle = isFull
    ? (partial
        ? `идёт прогон ${nSel} из ${TOTAL_MICROS} тестов…`
        : 'идёт полный прогон 12 микробенчмарков…')
    : 'идёт Single / Multi сравнение…';
  const detailRow = isFull
    ? `<div class="row"><span class="k">набор</span><span class="v">${
        partial ? `${nSel} ${pluralRu(nSel, ['тест', 'теста', 'тестов'])} · подмножество`
                : `${TOTAL_MICROS} микробенчмарков · 5 категорий`
      }</span></div>`
    : `<div class="row"><span class="k">движок</span><span class="v">int_iops_64 · целочисленные IOPS</span></div>`;
  const partialSec = nSel * 5 * 3;
  const partialDur = partialSec >= 60
    ? `≈ ${Math.round(partialSec / 60)} мин`
    : `≈ ${partialSec} с`;
  const footHint = isFull
    ? (partial
        ? `Прогон ${nSel} ${testsWord} занимает ${partialDur} (3 повтора × ${nSel} × 5 сек).`
        : 'Полный прогон занимает ~3 минуты (3 повтора × 12 тестов × 5 секунд).')
    : 'Прогон занимает ~10-15 секунд (2 замера × 5 сек + warmup).';
  host.innerHTML = `
    <div class="cpu-adv-screen">
      ${renderHeader('running', subtitle)}

      <div class="card">
        <div class="card__title">Текущая фаза <span class="card__title-tag" id="cpu-adv-phase-tag">…</span></div>
        <div class="progress" style="margin-top: var(--gap);">
          <div class="progress__fill" id="cpu-adv-progress-fill" style="width: 0%;"></div>
        </div>
        <div class="rows" style="margin-top: var(--gap);">
          <div class="row"><span class="k">фаза</span><span class="v" id="cpu-adv-phase">подготовка</span></div>
          ${detailRow}
        </div>
      </div>

      <div style="margin-top: var(--gap-lg); text-align: center; color: var(--muted); font-size: 12px;">
        ${escapeHtml(footHint)}
      </div>
    </div>
  `;
}

function updateRunningProgress(status) {
  const phase = status.progress || 'подготовка';
  const tag = document.getElementById('cpu-adv-phase-tag');
  const phaseEl = document.getElementById('cpu-adv-phase');
  // Полный прогон рапортует "Прогон N/3", Single/Multi — "Single-Core"/"Multi-Core".
  const fullRunMatch = /^Прогон (\d+)\/(\d+)$/.exec(phase);
  let humanPhase;
  let pct;
  if (fullRunMatch) {
    const n = parseInt(fullRunMatch[1], 10);
    const total = parseInt(fullRunMatch[2], 10);
    humanPhase = `Прогон ${n} из ${total}`;
    pct = ((n - 1) / total) * 100 + (1 / total) * 50;
  } else {
    humanPhase = {
      'Single-Core': 'Single-Core · 1 поток на P-ядре',
      'Multi-Core':  'Multi-Core · все потоки',
      'подготовка':  'подготовка',
      'готово':      'завершено',
    }[phase] || phase;
    pct = phase === 'Single-Core' ? 25 :
          phase === 'Multi-Core'  ? 75 :
          phase === 'готово'      ? 100 : 5;
  }
  if (tag) tag.textContent = humanPhase;
  if (phaseEl) phaseEl.textContent = humanPhase;
  const fillEl = document.getElementById('cpu-adv-progress-fill');
  if (fillEl) fillEl.style.width = `${pct}%`;
}

// ─── DONE ────────────────────────────────────────────────────────────

function renderDone(host) {
  // Для real-результата собираем структуру совместимую с renderer'ом.
  // Real-данные имеют только single/multi/speedup/efficiency — рейтинг CPU
  // (база известных моделей) пока нет в backend, поэтому ranking блок
  // показываем только в mock-режиме до появления §9.3 ranking endpoint.
  if (!mockMode && realResult && !realResult.error) {
    if (realResult.mode === 'full_run') return renderDoneFullRun(host);
    return renderDoneReal(host);
  }
  if (realResult && realResult.error) {
    return renderDoneError(host);
  }
  const r = MOCK_DONE;
  host.innerHTML = `
    <div class="cpu-adv-screen">
      ${renderHeader('done', `прогон Single / Multi · 4 мин 8 с`, true)}

      <div class="card cpu-adv-result-banner">
        <div class="card__title">Single-Core vs Multi-Core</div>
        <div class="cpu-adv-result-grid">
          <div class="cpu-adv-result-cell">
            <div class="cpu-adv-result-cell__label">SINGLE-CORE</div>
            <div class="cpu-adv-result-cell__value">${formatScore(r.single.score)}</div>
            <div class="cpu-adv-result-cell__unit">балл</div>
            <div class="cpu-adv-result-cell__hint">${escapeHtml(r.single.core_pinned)} · ${fmtNum(r.single.frequency_ghz, 2)} GHz</div>
            <div class="cpu-adv-result-cell__metrics">
              <div><span class="k">FLOPS DP</span><span class="v">${fmtNum(r.single.flops_dp)} GFLOPS</span></div>
              <div><span class="k">Integer ops</span><span class="v">${fmtNum(r.single.int_iops)} GIOPS</span></div>
              <div><span class="k">AES-256</span><span class="v">${formatScore(r.single.aes_256)} MB/s</span></div>
            </div>
          </div>

          <div class="cpu-adv-result-cell cpu-adv-result-cell--accent">
            <div class="cpu-adv-result-cell__label">MULTI-CORE</div>
            <div class="cpu-adv-result-cell__value">${formatScore(r.multi.score)}</div>
            <div class="cpu-adv-result-cell__unit">балл</div>
            <div class="cpu-adv-result-cell__hint">${r.multi.threads} потоков · ${fmtNum(r.multi.frequency_ghz, 2)} GHz</div>
            <div class="cpu-adv-result-cell__metrics">
              <div><span class="k">FLOPS DP</span><span class="v">${fmtNum(r.multi.flops_dp)} GFLOPS</span></div>
              <div><span class="k">Integer ops</span><span class="v">${fmtNum(r.multi.int_iops)} GIOPS</span></div>
              <div><span class="k">AES-256</span><span class="v">${formatScore(r.multi.aes_256)} MB/s</span></div>
            </div>
          </div>

          <div class="cpu-adv-result-mid">
            <div class="cpu-adv-result-mid__mark">×</div>
            <div class="cpu-adv-result-mid__speedup">${fmtNum(r.speedup, 1)}</div>
            <div class="cpu-adv-result-mid__label">SPEEDUP</div>
          </div>

          <div class="cpu-adv-result-eff">
            <div class="cpu-adv-result-eff__label">Эффективность</div>
            <div class="cpu-adv-result-eff__value">${fmtNum(r.efficiency * 100, 1)}%</div>
            <div class="cpu-adv-result-eff__hint">
              speedup / ${r.multi.threads} потоков · норма для P+E hybrid<br>
              (E-ядра работают на ~85% частоты P-ядер)
            </div>
          </div>
        </div>
        ${r.notes.length > 0 ? `<div class="cpu-adv-notes">
          ${r.notes.map(n => `<span class="cpu-adv-note">· ${escapeHtml(n)}</span>`).join('')}
        </div>` : ''}
      </div>

      <div class="cpu-adv-rankings">
        <div class="card">
          <div class="card__title">
            <span>Положение · Single-Core</span>
            <span class="card__title-tag">топ ${100 - r.ranking.single_percentile}% среди CPU</span>
          </div>
          ${renderRankingTable(r.ranking.neighbours_single)}
        </div>
        <div class="card">
          <div class="card__title">
            <span>Положение · Multi-Core</span>
            <span class="card__title-tag">топ ${100 - r.ranking.multi_percentile}% среди CPU</span>
          </div>
          ${renderRankingTable(r.ranking.neighbours_multi)}
        </div>
      </div>
    </div>
  `;
  document.getElementById('btn-cpu-adv-back')?.addEventListener('click', () => {
    view = 'idle';
    renderHost(host);
  });
}

// REAL-DATA done view (без mock ranking)
function renderDoneReal(host) {
  const r = realResult;
  const speedup = r.speedup || (r.single.value > 0 ? r.multi.value / r.single.value : null);
  const efficiency = r.efficiency || (speedup && r.cores_used_multi
    ? speedup / r.cores_used_multi : null);
  const coreInfo = (r.physical_p_cores != null && r.physical_e_cores != null)
    ? `${r.physical_p_cores}P + ${r.physical_e_cores}E hybrid`
    : `${r.physical_cores ?? r.cores_used_multi} физ. ядер`;
  host.innerHTML = `
    <div class="cpu-adv-screen">
      ${renderHeader('done', `Single / Multi · ${r.bench || 'int_iops_64'} · ${fmtNum(r.duration_sec_per_test, 1)}c × 2`, true)}

      <div class="card cpu-adv-result-banner">
        <div class="card__title">Single-Core vs Multi-Core</div>
        <div class="cpu-adv-result-grid">
          <div class="cpu-adv-result-cell">
            <div class="cpu-adv-result-cell__label">SINGLE-CORE</div>
            <div class="cpu-adv-result-cell__value">${formatMetric(r.single.value)}</div>
            <div class="cpu-adv-result-cell__unit">${escapeHtml(r.single.unit || '')}</div>
            <div class="cpu-adv-result-cell__hint">${r.pinned_cpu != null ? `CPU #${r.pinned_cpu}${r.pinned_kind ? ' · ' + escapeHtml(r.pinned_kind) : ''}` : 'pinned 1 core'}</div>
            <div class="cpu-adv-result-cell__metrics">
              <div><span class="k">длительность</span><span class="v">${fmtNum(r.single.duration_actual_sec, 2)} с</span></div>
            </div>
          </div>

          <div class="cpu-adv-result-cell cpu-adv-result-cell--accent">
            <div class="cpu-adv-result-cell__label">MULTI-CORE</div>
            <div class="cpu-adv-result-cell__value">${formatMetric(r.multi.value)}</div>
            <div class="cpu-adv-result-cell__unit">${escapeHtml(r.multi.unit || '')}</div>
            <div class="cpu-adv-result-cell__hint">${r.cores_used_multi} потоков · ${escapeHtml(coreInfo)}</div>
            <div class="cpu-adv-result-cell__metrics">
              <div><span class="k">длительность</span><span class="v">${fmtNum(r.multi.duration_actual_sec, 2)} с</span></div>
            </div>
          </div>

          <div class="cpu-adv-result-mid">
            <div class="cpu-adv-result-mid__mark">×</div>
            <div class="cpu-adv-result-mid__speedup">${speedup != null ? fmtNum(speedup, 1) : '—'}</div>
            <div class="cpu-adv-result-mid__label">SPEEDUP</div>
          </div>

          <div class="cpu-adv-result-eff">
            <div class="cpu-adv-result-eff__label">Эффективность</div>
            <div class="cpu-adv-result-eff__value">${efficiency != null ? fmtNum(efficiency * 100, 1) + '%' : '—'}</div>
            <div class="cpu-adv-result-eff__hint">
              speedup / ${r.cores_used_multi} потоков<br>
              ${r.physical_p_cores ? 'норма для P+E hybrid: E-ядра работают на ~85% частоты P-ядер' : ''}
            </div>
          </div>
        </div>
      </div>

      ${renderRankingReal(r.ranking)}
    </div>
  `;
  document.getElementById('btn-cpu-adv-back')?.addEventListener('click', () => {
    view = 'idle'; realResult = null; mockMode = false; renderHost(host);
  });
}

// Блок «Положение среди популярных CPU» для реального прогона.
// Использует `ranking` из payload backend (см. _MicroController.start_single_multi).
// kind: 'exact' / 'approx_cores' / 'none'.
function renderRankingReal(ranking) {
  if (!ranking || ranking.kind === 'none') {
    return `<div class="banner info" style="margin-top: var(--gap);">
      <b>Положение среди популярных CPU</b> — модель CPU не найдена в базе
      (всего ${ranking?.total ?? 0} процессоров). Результат сравним
      только с предыдущими прогонами этой машины.
    </div>`;
  }
  const total = ranking.total || 0;
  const sRank = ranking.single_rank;
  const sPct  = ranking.single_percentile;
  const mRank = ranking.multi_rank;
  const mPct  = ranking.multi_percentile;
  const isApprox = ranking.kind === 'approx_cores';
  const matchedName = ranking.matched_cpu_name || '—';
  const matchLine = isApprox
    ? `Точного совпадения нет — ближайший по топологии: <b>${escapeHtml(matchedName)}</b>`
    : `Точное совпадение: <b>${escapeHtml(matchedName)}</b>`;

  return `
    <div class="cpu-adv-rankings">
      <div class="card">
        <div class="card__title">
          <span>Положение · Single-Core</span>
          ${sPct != null ? `<span class="card__title-tag">топ ${sPct}% среди ${total} CPU</span>` : ''}
        </div>
        <div class="cpu-adv-rank-block">
          <div class="cpu-adv-rank-big">${sRank ?? '—'}<span class="cpu-adv-rank-big__sub">/ ${total}</span></div>
          <div class="cpu-adv-rank-hint">${matchLine}</div>
        </div>
      </div>
      <div class="card">
        <div class="card__title">
          <span>Положение · Multi-Core</span>
          ${mPct != null ? `<span class="card__title-tag">топ ${mPct}% среди ${total} CPU</span>` : ''}
        </div>
        <div class="cpu-adv-rank-block">
          <div class="cpu-adv-rank-big">${mRank ?? '—'}<span class="cpu-adv-rank-big__sub">/ ${total}</span></div>
          <div class="cpu-adv-rank-hint">${matchLine}</div>
        </div>
      </div>
    </div>
  `;
}

// Done-view для полного прогона — итоговый балл + интервал устойчивости +
// подскоры по 5 категориям + список 12 тестов с сырыми значениями.
function renderDoneFullRun(host) {
  const r = realResult;
  const overall = r.overall;
  const nRuns = overall ? overall.n_runs : r.n_runs;
  const subscores = overall ? overall.subscores || {} : {};

  // Категория → человеческая подпись + что измеряет. r_* — 5 групп
  // микробенчей (см. application/scoring_service.py).
  const CATEGORY_META = {
    r_memory:  { label: 'Оперативная память',             sub: 'ОЗУ · read / write / copy' },
    r_flops:   { label: 'Вычисления с плавающей запятой',  sub: 'fp32 / fp64 (FLOPS)' },
    r_integer: { label: 'Целочисленные операции',          sub: 'IOPS · 24/32/64-бит' },
    r_crypto:  { label: 'Криптография',                    sub: 'AES-256 · SHA-1' },
    r_fractal: { label: 'Фракталы (графика)',              sub: 'Julia · Mandelbrot (итеративные)' },
  };
  const cats = Object.entries(CATEGORY_META)
    .filter(([k]) => subscores[k] != null)
    .map(([k, meta]) => ({ ...meta, v: subscores[k] }));
  // Длина/цвет бара — ОТНОСИТЕЛЬНО сильнейшей категории (как матрица
  // Ram&Cache): показывает, в чём CPU относительно силён. Само число
  // «% пика» — абсолютная доля от теоретического пика архитектуры
  // (она низкая по своей природе — известный разрыв Roofline).
  const maxV = cats.length ? Math.max(...cats.map((c) => c.v)) : 1;
  const catCards = cats.map((c) => {
    const rel = maxV > 0 ? c.v / maxV : 0;
    const tone = rel >= 0.85 ? 'ok' : rel >= 0.55 ? 'accent' : 'warm';
    return `
      <div class="cpu-adv-catcard">
        <div class="cpu-adv-catcard__name">${escapeHtml(c.label)}</div>
        <div class="cpu-adv-catcard__sub">${escapeHtml(c.sub)}</div>
        <div class="cpu-adv-catcard__pct ${tone}">${fmtNum(c.v * 100, 1)}<span class="cpu-adv-catcard__pct-u">% пика</span></div>
        <div class="cpu-adv-catcard__bar">
          <div class="cpu-adv-catcard__bar-fill ${tone}" style="width: ${Math.max(3, rel * 100).toFixed(1)}%;"></div>
        </div>
      </div>`;
  }).join('');

  const totalSeconds = (r.duration_sec_per_test || 0) * (r.n_tests || 12) * (nRuns || 1);
  const durationLabel = totalSeconds >= 60
    ? `≈${Math.round(totalSeconds / 60)} мин`
    : `≈${Math.round(totalSeconds)} с`;

  const testsByCategory = {};
  for (const t of (r.tests || [])) {
    if (!testsByCategory[t.category]) testsByCategory[t.category] = [];
    testsByCategory[t.category].push(t);
  }
  const CATEGORY_RU = { memory: 'Оперативная память', flops: 'Вычисления с плавающей запятой', integer: 'Целочисленные операции', crypto: 'Криптография', fractal: 'Фракталы (графика)' };

  // Балл системы тут не показываем — он живёт на экране «Общая оценка
  // системы» (#general). Здесь — разбивка по категориям + сырые значения.
  host.innerHTML = `
    <div class="cpu-adv-screen">
      ${renderHeader('done', `полный прогон · ${nRuns || 1}× повторов · ${durationLabel}`, true)}

      <div class="card">
        <div class="card__title">
          <span>Производительность по категориям</span>
          <span class="card__title-tag">% от теоретического пика</span>
        </div>
        <div class="cpu-adv-catcards">
          ${catCards || '<div class="muted">нет подскоров</div>'}
        </div>
        <div class="cpu-adv-legend">
          <span class="cpu-adv-legend__item"><span class="dot dot-ok"></span> сильная</span>
          <span class="cpu-adv-legend__item"><span class="dot dot-accent"></span> средняя</span>
          <span class="cpu-adv-legend__item"><span class="dot dot-warm"></span> слабее</span>
          <span class="cpu-adv-legend__note">длина бара — относительно сильнейшей категории · % — доля от пика архитектуры · <b>больше = лучше</b></span>
        </div>
      </div>

      <div class="card">
        <div class="card__title">Сырые значения тестов</div>
        <div class="cpu-adv-raw">
          ${Object.entries(testsByCategory).map(([cat, tests]) => `
            <div class="cpu-adv-raw__group">
              <div class="cpu-adv-raw__cat">${escapeHtml(CATEGORY_RU[cat] || cat)}</div>
              ${tests.map((t) => {
                const meta = MICRO_META[t.name] || { label: t.name, hint: '' };
                return `
                <div class="cpu-adv-raw__row">
                  <div class="cpu-adv-raw__name">
                    <span class="cpu-adv-raw__label">${escapeHtml(meta.label)}</span>
                    ${meta.hint ? `<span class="cpu-adv-raw__hint">${escapeHtml(meta.hint)}</span>` : ''}
                  </div>
                  <div class="cpu-adv-raw__val">
                    ${t.error ? '<span class="danger">ошибка</span>'
                      : `<span class="cpu-adv-raw__num">${fmtNum(t.value, 2)}</span> <span class="cpu-adv-raw__unit">${escapeHtml(t.unit || '')}</span>`}
                  </div>
                </div>`;
              }).join('')}
            </div>
          `).join('')}
        </div>
      </div>
    </div>
  `;
  document.getElementById('btn-cpu-adv-back')?.addEventListener('click', () => {
    view = 'idle'; realResult = null; mockMode = false; renderHost(host);
  });
}

function renderDoneError(host) {
  host.innerHTML = `
    <div class="cpu-adv-screen">
      ${renderHeader('done', 'ошибка', true)}
      <div class="banner danger">
        <b>Прогон упал:</b> ${escapeHtml(realResult.error || 'неизвестно')}
      </div>
    </div>
  `;
  document.getElementById('btn-cpu-adv-back')?.addEventListener('click', () => {
    view = 'idle'; realResult = null; mockMode = false; renderHost(host);
  });
}

function renderRankingTable(rows) {
  const max = Math.max(...rows.map(r => r.score));
  return `<div class="cpu-adv-ranking">
    ${rows.map(r => `
      <div class="cpu-adv-ranking__row ${r.here ? 'here' : ''}">
        <span class="cpu-adv-ranking__mark">${r.here ? '▸' : ''}</span>
        <span class="cpu-adv-ranking__cpu">${escapeHtml(r.cpu)}</span>
        <span class="cpu-adv-ranking__score">${formatScore(r.score)}</span>
        <div class="cpu-adv-ranking__bar">
          <div class="cpu-adv-ranking__bar-fill ${r.here ? 'here' : ''}"
               style="width: ${((r.score / max) * 100).toFixed(1)}%;"></div>
        </div>
      </div>
    `).join('')}
  </div>`;
}

// ─── building blocks ─────────────────────────────────────────────────

function renderHeader(state, subtitle, isDone = false) {
  const showState = state === 'running' || state === 'done';
  const stateLabel = state === 'running' ? 'в работе' : state === 'done' ? 'результат' : '';
  const stateColor = state === 'done' ? 'cool' : state === 'running' ? 'accent' : '';
  const action = isDone
    ? `<button class="btn sm" id="btn-cpu-adv-back">← Назад</button>
       <button class="btn sm" disabled title="Экспорт появится в следующей версии">Экспорт</button>
       <button class="btn sm primary" disabled title="Появится в следующей версии">Запустить снова</button>`
    : `<button class="btn sm" disabled title="История появится в следующей версии">История</button>`;
  return `
    <div class="screen__header">
      <div class="screen__header__title">
        <h1>Расш. тест процессора</h1>
        <span class="screen__header__index">05</span>
        ${showState ? `<span class="screen__header__state ${stateColor}">· ${escapeHtml(stateLabel)}${subtitle ? ' · ' + escapeHtml(subtitle) : ''}</span>` : ''}
      </div>
      <div class="screen__header__chips">
        <span class="screen__header__goal">цель · CPU-only, без RAM и диска, детальный анализ процессорной производительности</span>
      </div>
      <div class="screen__header__actions">${action}</div>
    </div>
  `;
}

function formatScore(score) { return Number(score).toLocaleString('ru-RU'); }

// Аналог cli/render.py:_format_metric — крупное число с подходящим
// числом знаков после запятой. Для GIOPS/GFLOPS/MB/s значения часто
// дробные (12.34 GIOPS), для AES MB/s — тысячи (76 400). Это
// форматирование используется в больших значениях single/multi-карточек.
function formatMetric(value) {
  const v = Number(value);
  if (!Number.isFinite(v)) return '—';
  if (v >= 1000) return v.toLocaleString('ru-RU', { maximumFractionDigits: 0 });
  if (v >= 100)  return v.toFixed(1);
  if (v >= 10)   return v.toFixed(2);
  return v.toFixed(3);
}

function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[c]);
}
