// Ram & Cache — real backend через api.ramCacheStart / ramCacheStatus.
//
// Матрица 4×4: Read/Write/Copy/Latency × DRAM/L1/L2/L3 (16 ячеек). Каждая
// ячейка показывает значение + bar (относительная скорость в строке для
// real-данных; для mock — ratio к теоретическому пику). Результат живёт
// in-memory в backend (без БД, как в CLI «apexcore ram-cache run»).
// Mock-данные остаются под кнопкой «Посмотреть пример».

import { api } from '../api.js';
import { fmtNum } from '../format.js';

const METRICS = ['Read', 'Write', 'Copy', 'Latency'];
const LEVELS  = ['DRAM', 'L3', 'L2', 'L1'];  // отображение сверху вниз: память → ближе к ядру
const LEVEL_LABEL = {
  DRAM: { name: 'Memory',   sub: 'DDR5 · оперативная память' },
  L3:   { name: 'L3 Cache', sub: 'cache · L3 (общий)' },
  L2:   { name: 'L2 Cache', sub: 'cache · L2' },
  L1:   { name: 'L1 Cache', sub: 'cache · L1 (ближайший к ядру)' },
};

// MOCK cells — реалистичные значения для DDR5-6400 + Alder/Raptor Lake.
// Структура: cells[metric][level] = { value, unit, r }
const MOCK_CELLS = {
  Read:    { DRAM: { value: 65200,   unit: 'GB/s' /*MB→GB ниже*/, r: 0.62 },
             L3:   { value: 248000,  unit: 'GB/s', r: 0.71 },
             L2:   { value: 842000,  unit: 'GB/s', r: 0.78 },
             L1:   { value: 3520000, unit: 'GB/s', r: 0.83 } },
  Write:   { DRAM: { value: 58400,   unit: 'GB/s', r: 0.56 },
             L3:   { value: 226000,  unit: 'GB/s', r: 0.65 },
             L2:   { value: 798000,  unit: 'GB/s', r: 0.74 },
             L1:   { value: 3180000, unit: 'GB/s', r: 0.79 } },
  Copy:    { DRAM: { value: 52100,   unit: 'GB/s', r: 0.50 },
             L3:   { value: 208000,  unit: 'GB/s', r: 0.60 },
             L2:   { value: 752000,  unit: 'GB/s', r: 0.70 },
             L1:   { value: 2980000, unit: 'GB/s', r: 0.74 } },
  Latency: { DRAM: { value: 76.4,    unit: 'нс',   r: 0.45 },
             L3:   { value: 12.1,    unit: 'нс',   r: 0.78 },
             L2:   { value: 3.4,     unit: 'нс',   r: 0.86 },
             L1:   { value: 1.1,     unit: 'нс',   r: 0.92 } },
};

let view = 'idle';           // idle | running | done
let mockMode = false;         // true = «Посмотреть пример», false = real
let realResult = null;        // RamCacheReport из /api/ram-cache/status.last_result
let realError = null;
let pollHandle = null;
let hwInfo = null;            // реальная DRAM-конфигурация из /api/hardware

// Конфигурация памяти. mockMode → демо-значения; иначе реальные из
// /api/hardware. Объём доступен всегда (psutil); тип/частота/модули —
// Windows WMI / Linux dmidecode (root); где недоступно — «н/д».
// Тайминги (CL) убраны — они нигде реально не определяются (был mock).
function renderDramRows() {
  if (mockMode) {
    return `
      <div class="row"><span class="k">Объём</span><span class="v">32 ГБ</span></div>
      <div class="row"><span class="k">Тип</span><span class="v">DDR5-6400</span></div>
      <div class="row"><span class="k">Слотов</span><span class="v">2 × DIMM</span></div>
      <div class="row"><span class="k">Каналов</span><span class="v">2 (dual-channel)</span></div>`;
  }
  const d = hwInfo && hwInfo.dram;
  if (!d) {
    return `<div class="row"><span class="k">Конфигурация</span><span class="v">определяется…</span></div>`;
  }
  const nd = '<span class="v" style="color:var(--muted)">н/д</span>';
  const vol = d.total_gb != null ? `<span class="v">${fmtNum(d.total_gb, 1)} ГБ</span>` : nd;
  // Тип + частота: «DDR5-6400» если оба, иначе по отдельности / н/д.
  let typeStr = nd;
  // Грейд памяти («DDR5-6400») — без разделителя тысяч (это маркировка, не число).
  if (d.type && d.speed_mts) typeStr = `<span class="v">${d.type}-${Math.round(d.speed_mts)}</span>`;
  else if (d.type) typeStr = `<span class="v">${d.type}</span>`;
  else if (d.speed_mts) typeStr = `<span class="v">${fmtNum(d.speed_mts, 0)} MT/s</span>`;
  const slots = d.modules != null ? `<span class="v">${d.modules} × DIMM</span>` : nd;
  const chan = d.channels != null ? `<span class="v">${d.channels} (channel)</span>` : nd;
  let rows = `
      <div class="row"><span class="k">Объём</span>${vol}</div>
      <div class="row"><span class="k">Тип</span>${typeStr}</div>
      <div class="row"><span class="k">Слотов</span>${slots}</div>
      <div class="row"><span class="k">Каналов</span>${chan}</div>`;
  // Если детали недоступны (Linux без root) — поясняем почему «н/д».
  if (!d.available) {
    rows += `<div class="row"><span class="k" style="font-size:11px;color:var(--muted)">источник</span>` +
            `<span class="v" style="font-size:11px;color:var(--muted)">тип/частота требуют прав root</span></div>`;
  }
  return rows;
}

async function loadHardware() {
  if (hwInfo || mockMode) { updateDramDom(); return; }
  try { hwInfo = await api.getHardware(); } catch { /* «определяется…» */ }
  updateDramDom();
}

function updateDramDom() {
  const el = document.getElementById('ramcache-dram-config');
  if (el) el.innerHTML = renderDramRows();
}

export function render(host) {
  renderHost(host);
  void syncStatus(host);
  void loadHardware();
}

export function dispose() {
  if (pollHandle) { clearInterval(pollHandle); pollHandle = null; }
}

function renderHost(host) {
  if (view === 'running') return renderRunning(host);
  if (view === 'done')    return renderDone(host);
  return renderIdle(host);
}

// ─── status sync ─────────────────────────────────────────────────────

async function syncStatus(host) {
  if (mockMode) return;
  try {
    const s = await api.ramCacheStatus();
    if (s.running) {
      view = 'running';
      renderHost(host);
      updateRunningProgress(s);
      if (!pollHandle) pollHandle = setInterval(() => { void syncStatus(host); }, 1200);
    } else if ((s.status === 'completed' || s.status === 'cancelled') && s.last_result) {
      if (pollHandle) { clearInterval(pollHandle); pollHandle = null; }
      realResult = s.last_result;
      realError = null;
      view = 'done';
      renderHost(host);
    } else if (s.status === 'failed') {
      if (pollHandle) { clearInterval(pollHandle); pollHandle = null; }
      realResult = null;
      realError = s.error || 'Прогон упал';
      view = 'done';
      renderHost(host);
    }
  } catch { /* тихо */ }
}

async function onStart(host) {
  try {
    await api.ramCacheStart({ duration_sec_per_metric: 2.0 });
    view = 'running';
    realError = null;
    renderHost(host);
    if (!pollHandle) pollHandle = setInterval(() => { void syncStatus(host); }, 1200);
  } catch (err) {
    alert('Не удалось запустить Ram & Cache: ' + err.message);
  }
}

// ─── IDLE ────────────────────────────────────────────────────────────

function renderIdle(host) {
  host.innerHTML = `
    <div class="ramcache-screen">
      ${renderHeader('idle', 'готов к запуску')}

      <div class="ramcache-layout">
        <div class="card ramcache-main">
          <div class="ramcache-intro">
            Каждая комбинация <b>операции</b> (Read / Write / Copy / Latency) и
            <b>уровня памяти</b> (DRAM / L1 / L2 / L3) меряется отдельно.
            ~30 секунд на полный прогон.
          </div>
          ${renderMatrix(null)}
        </div>

        <div class="ramcache-side">
          <div class="card">
            <div class="card__title">Конфигурация памяти</div>
            <div class="rows" id="ramcache-dram-config">
              ${renderDramRows()}
            </div>
          </div>

          <div class="card">
            <div class="card__title">Что меряется</div>
            <div class="ramcache-explainer">
              <p><b>Read / Write / Copy</b> — пропускная способность (ГБ/с,
              больше = лучше). Зависит от тайминги, channel-mode, NUMA.</p>
              <p><b>Latency</b> — задержка одиночного доступа (нс,
              меньше = лучше). Зависит от частоты, tCAS, ассоциативности кешей.</p>
              <p><b>L1 → DRAM</b> — каждый уровень на порядок медленнее
              предыдущего: L1 ~ 1 нс, L2 ~ 3, L3 ~ 12, DRAM ~ 75-100 нс.</p>
            </div>
          </div>

          <button class="btn lg primary" id="btn-ramcache-start" style="margin-top: auto;">
            ▶ &nbsp; Запустить полный прогон (~30 с)
          </button>
          <button class="btn sm" id="btn-ramcache-toggle">
            Посмотреть пример результата →
          </button>
        </div>
      </div>
    </div>
  `;
  document.getElementById('btn-ramcache-toggle')?.addEventListener('click', () => {
    mockMode = true;
    realResult = null;
    realError = null;
    view = 'done';
    renderHost(host);
  });
  document.getElementById('btn-ramcache-start')?.addEventListener('click', () => {
    mockMode = false;
    void onStart(host);
  });
}

// ─── RUNNING ─────────────────────────────────────────────────────────

const OPERATION_RU = {
  read: 'Чтение', write: 'Запись', copy: 'Копирование', latency: 'Задержка',
};

function renderRunning(host) {
  host.innerHTML = `
    <div class="ramcache-screen">
      ${renderHeader('running', 'идёт замер ячеек матрицы…')}

      <div class="ramcache-layout">
        <div class="card ramcache-main">
          <div class="card__title">Текущая ячейка <span class="card__title-tag" id="ramcache-cell-tag">0 / 16</span></div>
          <div class="progress" style="margin-top: var(--gap);">
            <div class="progress__fill" id="ramcache-progress-fill" style="width: 5%;"></div>
          </div>
          <div class="rows" style="margin-top: var(--gap);">
            <div class="row"><span class="k">уровень</span><span class="v" id="ramcache-cur-level">—</span></div>
            <div class="row"><span class="k">операция</span><span class="v" id="ramcache-cur-op">—</span></div>
          </div>
          ${renderMatrixSkeleton()}
        </div>

        <div class="ramcache-side">
          <div class="card">
            <div class="card__title">Что меряется</div>
            <div class="ramcache-explainer">
              <p><b>Read / Write / Copy</b> — пропускная способность (ГБ/с, больше = лучше).</p>
              <p><b>Latency</b> — задержка одиночного доступа (нс, меньше = лучше).</p>
              <p>Один замер ~2 секунды × 16 ячеек = ~30 секунд на полный прогон.</p>
            </div>
          </div>
        </div>
      </div>
    </div>
  `;
}

function renderMatrixSkeleton() {
  // Пустая матрица для running-view: показывает что будет 16 ячеек.
  return renderMatrix(null);
}

function updateRunningProgress(s) {
  const p = s.progress || {};
  const idx = p.idx || 0;
  const total = p.total || 16;
  const tag = document.getElementById('ramcache-cell-tag');
  const lvlEl = document.getElementById('ramcache-cur-level');
  const opEl = document.getElementById('ramcache-cur-op');
  const fill = document.getElementById('ramcache-progress-fill');
  if (tag) tag.textContent = `${idx} / ${total}`;
  if (lvlEl) lvlEl.textContent = p.level || '—';
  if (opEl) opEl.textContent = p.operation ? (OPERATION_RU[p.operation] || p.operation) : '—';
  if (fill) {
    const pct = total > 0 ? Math.max(5, Math.round((idx / total) * 100)) : 5;
    fill.style.width = `${pct}%`;
  }
}

// ─── DONE ────────────────────────────────────────────────────────────

// Преобразовать список RamCacheMetric (real) в формат MOCK_CELLS:
// cells[OperationCap][LEVEL] = { value, unit, r, error? }.
// Для real-данных bar нормализуем по максимуму в строке (operation): L1 read
// будет 100%, остальные — относительно него.
function realToCells(metrics) {
  const OP_CAP = { read: 'Read', write: 'Write', copy: 'Copy', latency: 'Latency' };
  const cells = { Read: {}, Write: {}, Copy: {}, Latency: {} };
  if (!Array.isArray(metrics)) return cells;
  // Сначала собираем raw значения.
  for (const m of metrics) {
    const opKey = OP_CAP[m.operation];
    if (!opKey) continue;
    cells[opKey][m.level] = {
      value: m.value,
      unit: m.unit === 'ns' ? 'нс' : m.unit,
      error: m.error,
    };
  }
  // Считаем r для каждой строки.
  for (const opKey of Object.keys(cells)) {
    const row = cells[opKey];
    const values = Object.values(row)
      .filter(c => !c.error && c.value > 0)
      .map(c => c.value);
    if (values.length === 0) continue;
    // Для latency «лучше = меньше», поэтому нормализуем обратной величиной.
    const isLatency = opKey === 'Latency';
    if (isLatency) {
      const minV = Math.min(...values);
      for (const c of Object.values(row)) {
        c.r = (!c.error && c.value > 0) ? Math.min(1, minV / c.value) : 0;
      }
    } else {
      const maxV = Math.max(...values);
      for (const c of Object.values(row)) {
        c.r = (!c.error && c.value > 0) ? Math.min(1, c.value / maxV) : 0;
      }
    }
  }
  return cells;
}

function formatDoneSubtitle(r) {
  if (!r) return '';
  const dur = (new Date(r.ended_at) - new Date(r.started_at)) / 1000;
  const dStr = dur >= 60 ? `${Math.round(dur / 60)} мин ${Math.round(dur % 60)} с` : `${Math.round(dur)} с`;
  // Дата прогона: ru-RU короткий формат.
  const d = new Date(r.ended_at).toLocaleString('ru-RU', {
    day: '2-digit', month: '2-digit', year: 'numeric',
    hour: '2-digit', minute: '2-digit',
  });
  return `прогон от ${d} · ${dStr}`;
}

function renderDone(host) {
  if (realError && !mockMode) {
    return renderDoneError(host);
  }
  const isReal = !mockMode && realResult != null;
  const cells = isReal ? realToCells(realResult.metrics) : MOCK_CELLS;
  const subtitle = isReal
    ? formatDoneSubtitle(realResult)
    : 'прогон от 19.05.2026, 17:30 · 1 мин 58 с';
  // Для mock — старая легенда про r-ratio к теоретическому пику.
  // Для real — нормализация по максимуму в строке, поэтому легенда другая.
  const legend = isReal
    ? `<div class="ramcache-legend">
        <span class="ramcache-legend-item"><span class="dot dot-ok"></span> ближе всех к самому быстрому уровню в строке</span>
        <span class="ramcache-legend-item"><span class="dot dot-accent"></span> промежуточный</span>
        <span class="ramcache-legend-item"><span class="dot dot-warm"></span> заметно медленнее</span>
       </div>`
    : `<div class="ramcache-legend">
        <span class="ramcache-legend-item"><span class="dot dot-ok"></span> r ≥ 85% — близко к пику</span>
        <span class="ramcache-legend-item"><span class="dot dot-accent"></span> r 65-85% — нормально</span>
        <span class="ramcache-legend-item"><span class="dot dot-warm"></span> r &lt; 65% — ниже ожидаемого</span>
       </div>`;
  host.innerHTML = `
    <div class="ramcache-screen">
      ${renderHeader('done', subtitle, true)}

      <div class="card">
        <div class="card__title">Матрица 4×4 · ${escapeHtml('Память + L1/L2/L3 кеш')}</div>
        ${renderMatrix(cells)}
        ${legend}
      </div>
    </div>
  `;
  document.getElementById('btn-ramcache-back')?.addEventListener('click', () => {
    view = 'idle';
    mockMode = false;
    realResult = null;
    renderHost(host);
  });
}

function renderDoneError(host) {
  host.innerHTML = `
    <div class="ramcache-screen">
      ${renderHeader('done', 'ошибка', true)}
      <div class="banner danger">
        <b>Прогон Ram & Cache упал:</b> ${escapeHtml(realError || 'неизвестная ошибка')}
      </div>
    </div>
  `;
  document.getElementById('btn-ramcache-back')?.addEventListener('click', () => {
    realError = null;
    view = 'idle';
    renderHost(host);
  });
}

// ─── Matrix ──────────────────────────────────────────────────────────

function renderMatrix(cells) {
  const headerRow = `<div class="ramcache-matrix__corner"></div>` +
    METRICS.map(m => `<div class="ramcache-matrix__head">
      ${escapeHtml(m)}
      <div class="ramcache-matrix__head-sub">
        ${m === 'Latency' ? 'нс · меньше = лучше' : 'ГБ/с · больше = лучше'}
      </div>
    </div>`).join('');

  const dataRows = LEVELS.map(level => {
    const labelCell = `<div class="ramcache-matrix__level">
      <div class="ramcache-matrix__level-name">${LEVEL_LABEL[level].name}</div>
      <div class="ramcache-matrix__level-sub">${LEVEL_LABEL[level].sub}</div>
    </div>`;
    const cellHtml = METRICS.map(m => {
      const cell = cells ? cells[m]?.[level] : null;
      return renderMatrixCell(cell);
    }).join('');
    return labelCell + cellHtml;
  }).join('');

  return `<div class="ramcache-matrix">
    ${headerRow}
    ${dataRows}
  </div>`;
}

function renderMatrixCell(cell) {
  if (!cell) {
    return `<div class="ramcache-cell ramcache-cell--empty">—</div>`;
  }
  if (cell.error) {
    return `<div class="ramcache-cell ramcache-cell--empty" title="${escapeHtml(cell.error)}">ошибка</div>`;
  }
  const r = typeof cell.r === 'number' ? cell.r : 0;
  const tone = r >= 0.85 ? 'ok' : r >= 0.65 ? 'accent' : 'warm';
  // GB/s конвертация если значение в MB/s (>1000)
  const showVal = cell.unit === 'нс'
    ? fmtNum(cell.value, 1)
    : cell.value >= 1000
      ? fmtNum(cell.value / 1000, cell.value >= 100000 ? 0 : 1)
      : fmtNum(cell.value, 1);
  const showUnit = cell.unit === 'нс' ? 'нс' : (cell.value >= 1000 ? 'ГБ/с' : 'МБ/с');
  return `<div class="ramcache-cell">
    <div class="ramcache-cell__row">
      <span class="ramcache-cell__value ${tone}">${showVal}</span>
      <span class="ramcache-cell__unit">${showUnit}</span>
    </div>
    <div class="ramcache-cell__bar-row">
      <div class="ramcache-cell__bar">
        <div class="ramcache-cell__bar-fill ${tone}" style="width: ${Math.min(r, 1) * 100}%;"></div>
      </div>
      <span class="ramcache-cell__r">${(r * 100).toFixed(0)}%</span>
    </div>
  </div>`;
}

// ─── building blocks ─────────────────────────────────────────────────

function renderHeader(state, subtitle, isDone = false) {
  const showState = state === 'done';
  const stateLabel = isDone ? 'результат' : '';
  const action = isDone
    ? `<button class="btn sm" id="btn-ramcache-back">← Назад</button>
       <button class="btn sm" disabled title="Экспорт появится в следующей версии">Экспорт</button>`
    : `<button class="btn sm" disabled title="История появится в следующей версии">История</button>`;
  return `
    <div class="screen__header">
      <div class="screen__header__title">
        <h1>Ram &amp; Cache</h1>
        <span class="screen__header__index">06</span>
        ${showState ? `<span class="screen__header__state cool">· ${escapeHtml(stateLabel)}${subtitle ? ' · ' + escapeHtml(subtitle) : ''}</span>` : ''}
      </div>
      <div class="screen__header__chips">
        <span class="screen__header__goal screen__header__goal--amber">CPU L1/L2/L3 и DRAM</span>
      </div>
      <div class="screen__header__actions">${action}</div>
    </div>
  `;
}

function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[c]);
}
