// Наследие Winsat — Windows-only.
// idle / running / done — real backend через api.winsatStart / winsatStatus.
// Windows: 6 стадий (AES-256, SHA-1, Memory Read, Disk Seq, Disk Random, DWM
// через native winsat) → 5 подскоров (CPU/Memory/Disk/Graphics/D3D) + общий
// уровень WinSPR.
// Linux: backend сразу вернёт failed → отрисовываем platform-restriction.
// Mock-данные остались только под кнопкой «Посмотреть пример».

import { api } from '../api.js';
import { state } from '../store.js';
import { fmtNum, fmtDate } from '../format.js';
import { renderPlatformRestriction } from '../components/stub.js';

// Поддерживаем оба варианта ключей: MOCK использует Title-case (CPU/Memory/D3D),
// real backend (WinsatCategory enum) — lowercase (cpu/memory/d3d).
const SUBSCORE_LABELS = {
  CPU: 'Процессор', cpu: 'Процессор',
  Memory: 'Память', memory: 'Память',
  Disk: 'Диск',     disk: 'Диск',
  Graphics: 'Графика', graphics: 'Графика',
  D3D: 'Игровая графика (D3D)', d3d: 'Игровая графика (D3D)',
};

const SUBSCORE_METRIC_RU = {
  // MOCK-данные (Title-case английский, оставлены для совместимости).
  'compression_speed': 'Скорость сжатия',
  'memory_bandwidth':  'Пропускная способность',
  'disk_sequential_read': 'Последовательное чтение',
  'graphics_dwm':      'Отрисовка рабочего стола',
  'd3d_3d':            '3D-производительность',
  // Real-данные из winsat_thresholds.yaml + winsat_scoring.compute_disk_score.
  'harmonic_mean(aes_256, sha1)': 'AES-256 + SHA-1 (гарм. среднее)',
  'memory_read':       'Чтение памяти',
  'disk_seq_read':     'Последовательное чтение',
  'disk_random_read':  'Случайное чтение',
  // Native winsat dwm — графика + DirectX 3D.
  'dwm_assessment':    'DWM (рабочий стол), FPS',
  'd3d_assessment':    'DirectX 3D, пропускная способность видеопамяти',
};

// Для real-данных disk_score возвращает metric_name вида
// "min(seq=6432,rnd=2511)" — собран в winsat_scoring.compute_disk_score через
// f-string. Заменим на короткую человеческую подпись.
function humanMetricName(raw) {
  if (!raw) return '';
  if (raw in SUBSCORE_METRIC_RU) return SUBSCORE_METRIC_RU[raw];
  if (/^min\(seq=/.test(raw)) return 'min(посл., случ.) — диск';
  return raw;
}

const MOCK = {
  run_id: '8f2a4b6c-1a4d-4d3a-9c2e-3f1b8e9d5a7c',
  timestamp: '2026-05-19T17:30:00+00:00',
  winspr_level: 8.7,
  subscores: [
    { category: 'CPU',      status: 'PASS', score: 9.1, metric_name: 'compression_speed',     metric_value: 1245.6, metric_unit: 'MB/s',  note: '' },
    { category: 'Memory',   status: 'PASS', score: 9.3, metric_name: 'memory_bandwidth',      metric_value: 38400,  metric_unit: 'MB/s',  note: '' },
    { category: 'Disk',     status: 'PASS', score: 8.9, metric_name: 'disk_sequential_read',  metric_value: 6420,   metric_unit: 'MB/s',  note: '' },
    { category: 'Graphics', status: 'PASS', score: 8.7, metric_name: 'graphics_dwm',          metric_value: null,   metric_unit: '',      note: 'самая слабая категория · задаёт уровень системы' },
    { category: 'D3D',      status: 'PASS', score: 9.4, metric_name: 'd3d_3d',                metric_value: 4280,   metric_unit: 'fps',   note: '' },
  ],
};

let view = 'idle';           // idle | running | done
let mockMode = false;         // true = «Посмотреть пример», false = real
let realResult = null;        // WinsatReport из /api/winsat/status.last_result
let realError  = null;        // строка ошибки если status == 'failed'
let pollHandle = null;

export function render(host) {
  // На Linux всегда показываем platform-restriction независимо от view.
  if (state.config?.platform && state.config.platform !== 'windows') {
    renderLinuxRestriction(host);
    return;
  }
  renderHost(host);
  // При первом монтировании синхронизируемся с backend (если уже идёт прогон
  // или есть кеш last_result — сразу перейдём в нужный view).
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

// ─── status sync ─────────────────────────────────────────────────────

async function syncStatus(host) {
  if (mockMode) return;
  try {
    const s = await api.winsatStatus();
    if (s.running) {
      view = 'running';
      renderHost(host);
      updateRunningProgress(s);
      if (!pollHandle) pollHandle = setInterval(() => { void syncStatus(host); }, 1200);
    } else if (s.status === 'completed' && s.last_result) {
      if (pollHandle) { clearInterval(pollHandle); pollHandle = null; }
      realResult = s.last_result;
      realError  = null;
      view = 'done';
      renderHost(host);
    } else if (s.status === 'failed') {
      if (pollHandle) { clearInterval(pollHandle); pollHandle = null; }
      realResult = null;
      realError  = s.error || 'Прогон упал';
      view = 'done';
      renderHost(host);
    } else if (s.status === 'cancelled') {
      if (pollHandle) { clearInterval(pollHandle); pollHandle = null; }
      view = 'idle';
      renderHost(host);
    }
  } catch { /* тихо */ }
}

async function onStart(host) {
  try {
    await api.winsatStart({ duration_sec: 5.0 });
    view = 'running';
    realError = null;
    renderHost(host);
    if (!pollHandle) pollHandle = setInterval(() => { void syncStatus(host); }, 1200);
  } catch (err) {
    alert('Не удалось запустить Winsat: ' + err.message);
  }
}

// ─── IDLE (Windows) ──────────────────────────────────────────────────

function renderIdle(host) {
  host.innerHTML = `
    <div class="winsat-screen">
      ${renderHeader('idle', 'готов к запуску')}

      <div class="winsat-layout">
        <div class="card winsat-main">
          <div class="winsat-intro">
            Winsat (Windows System Assessment Tool) — это бывший встроенный
            в Windows эталон оценки производительности. Шкала <b>1.0 – 9.9</b>.
            Итоговый уровень системы (WinSPR) = минимум среди 5 подскоров,
            потому что «самое слабое звено» определяет общую плавность.
          </div>
          <div class="winsat-categories">
            ${renderCategoryCard('Процессор', 'CPU', 'compression_speed', 'тест скорости сжатия')}
            ${renderCategoryCard('Память',    'Memory', 'memory_bandwidth', 'пропускная способность DDR')}
            ${renderCategoryCard('Диск',      'Disk', 'disk_sequential_read', 'sequential read загрузочного диска')}
            ${renderCategoryCard('Графика',   'Graphics', 'graphics_dwm', 'отрисовка рабочего стола')}
            ${renderCategoryCard('Игровая графика (D3D)', 'D3D', 'd3d_3d', '3D-производительность')}
          </div>
          <div class="winsat-cli-row">
            <button class="btn lg primary" id="btn-winsat-start">▶ Запустить Winsat</button>
          </div>
        </div>

        <div class="winsat-side">
          <div class="card">
            <div class="card__title">Как устроена оценка</div>
            <div class="winsat-explainer">
              <p>Раздел воспроизводит привычную шкалу Windows (<b>WinSPR, 1.0–9.9</b>),
              но обновляет измерение под современное железо — оценка <b>гибридная</b>:</p>
              <p>• <b>Графика</b> и <b>Игровая графика (D3D)</b> берутся <b>нативно</b>
              из Windows командой <code>winsat dwm</code> — прямая сопоставимость
              с тем, что показывает сам Windows (стадия требует прав администратора).</p>
              <p>• <b>Процессор, Память, Диск</b> — <b>собственные микробенчмарки</b>
              ApexCore, а не устаревшие тесты Winsat: ЦП через AES-256 + SHA-1
              (аппаратный AES-NI, гармоническое среднее), память — пропускная
              способность чтения DRAM, диск — последовательное + случайное чтение
              (минимум, как в Winsat).</p>
              <p>• Сырые МБ/с переводятся в шкалу 1.0–9.9 по <b>обновлённым порогам</b>
              под современные CPU/SSD/DDR5 — там, где оригинальный Winsat
              (пороги ~2009 г.) давно упирается в потолок.</p>
              <p>Итоговый <b>WinSPR — минимум</b> по подскорам: система настолько
              быстра, насколько медленно её слабейшее звено. Для практической
              оценки см. <a href="#cpu-advanced">Расш. тест процессора</a> и
              <a href="#general">Общую оценку системы</a>.</p>
            </div>
          </div>
          <button class="btn sm" id="btn-winsat-toggle">
            Посмотреть пример результата →
          </button>
        </div>
      </div>
    </div>
  `;
  document.getElementById('btn-winsat-toggle')?.addEventListener('click', () => {
    mockMode = true;
    realResult = null;
    realError = null;
    view = 'done';
    renderHost(host);
  });
  document.getElementById('btn-winsat-start')?.addEventListener('click', () => {
    mockMode = false;
    void onStart(host);
  });
}

// ─── RUNNING (Windows) ───────────────────────────────────────────────

function renderRunning(host) {
  host.innerHTML = `
    <div class="winsat-screen">
      ${renderHeader('running', 'идёт прогон Winsat…')}

      <div class="card">
        <div class="card__title">Текущая стадия <span class="card__title-tag" id="winsat-stage-tag">…</span></div>
        <div class="progress" style="margin-top: var(--gap);">
          <div class="progress__fill" id="winsat-progress-fill" style="width: 5%;"></div>
        </div>
        <div class="rows" style="margin-top: var(--gap);">
          <div class="row"><span class="k">стадия</span><span class="v" id="winsat-stage-name">подготовка</span></div>
          <div class="row"><span class="k">прогресс</span><span class="v" id="winsat-stage-idx">0 / 5</span></div>
        </div>
      </div>

      <div style="margin-top: var(--gap-lg); text-align: center; color: var(--muted); font-size: 12px;">
        Прогон занимает ~1 минуту (5 CPU/RAM/Disk-стадий + native winsat DWM для GPU).
      </div>
    </div>
  `;
}

function updateRunningProgress(s) {
  const p = s.progress || {};
  const idx = p.idx || 0;
  const total = p.total || 5;
  const stage = p.stage || 'подготовка';
  const tag = document.getElementById('winsat-stage-tag');
  const name = document.getElementById('winsat-stage-name');
  const idxEl = document.getElementById('winsat-stage-idx');
  const fill = document.getElementById('winsat-progress-fill');
  if (tag) tag.textContent = `${idx} / ${total}`;
  if (name) name.textContent = stage;
  if (idxEl) idxEl.textContent = `${idx} / ${total}`;
  if (fill) {
    const pct = total > 0 ? Math.max(5, Math.round((idx / total) * 100)) : 5;
    fill.style.width = `${pct}%`;
  }
}

function renderCategoryCard(label, key, metric, hint) {
  return `<div class="winsat-category-card">
    <div class="winsat-category-card__label">${escapeHtml(label)}</div>
    <div class="winsat-category-card__metric">${escapeHtml(SUBSCORE_METRIC_RU[metric] || metric)}</div>
    <div class="winsat-category-card__hint">${escapeHtml(hint)}</div>
  </div>`;
}

// ─── DONE (Windows) ──────────────────────────────────────────────────

// Блок «Как устроена оценка» — методика раздела. Показывается и на стартовом
// экране, и в результате (как в CLI-отчёте). Один источник правды.
function methodologyCard() {
  return `
    <div class="card">
      <div class="card__title">Как устроена оценка</div>
      <div class="winsat-explainer">
        <p>Раздел воспроизводит привычную шкалу Windows (<b>WinSPR, 1.0–9.9</b>),
        но обновляет измерение под современное железо — оценка <b>гибридная</b>:</p>
        <p>• <b>Графика</b> и <b>Игровая графика (D3D)</b> берутся <b>нативно</b>
        из Windows командой <code>winsat dwm</code> — прямая сопоставимость с тем,
        что показывает сам Windows (стадия требует прав администратора).</p>
        <p>• <b>Процессор, Память, Диск</b> — <b>собственные микробенчмарки</b>
        ApexCore, а не устаревшие тесты Winsat: ЦП через AES-256 + SHA-1
        (аппаратный AES-NI, гармоническое среднее), память — пропускная
        способность чтения DRAM, диск — последовательное + случайное чтение
        (минимум, как в Winsat).</p>
        <p>• Сырые МБ/с переводятся в шкалу 1.0–9.9 по <b>обновлённым порогам</b>
        под современные CPU/SSD/DDR5 — там, где оригинальный Winsat
        (пороги ~2009 г.) давно упирается в потолок.</p>
        <p>Итоговый <b>WinSPR — минимум</b> по подскорам: система настолько быстра,
        насколько медленно её слабейшее звено. Для практической оценки см.
        <a href="#cpu-advanced">Расш. тест процессора</a> и
        <a href="#general">Общую оценку системы</a>.</p>
      </div>
    </div>`;
}

// Нормализация real WinsatReport → ту же форму что MOCK (массив subscores).
function realToView(r) {
  if (!r) return null;
  return {
    timestamp: r.ended_at || r.started_at,
    winspr_level: r.winspr_level,
    cancelled: !!r.cancelled,
    subscores: ['cpu_score', 'memory_score', 'disk_score', 'graphics_score', 'd3d_score']
      .map(k => r[k])
      .filter(Boolean),
  };
}

function renderDone(host) {
  if (realError && !mockMode) {
    return renderDoneError(host);
  }
  const w = (!mockMode && realResult) ? realToView(realResult) : MOCK;
  const lvl = w.winspr_level;
  const tone = lvl >= 8.0 ? 'cool' : lvl >= 5.0 ? 'warm' : 'hot';
  const verdict = lvl >= 8.0 ? 'Высокая производительность' : lvl >= 5.0 ? 'Средняя' : 'Низкая';
  // Bottleneck = самая слабая PASS-категория
  const pass = w.subscores.filter(s => s.status === 'PASS');
  const bottleneck = pass.length > 0
    ? pass.reduce((min, s) => s.score < min.score ? s : min, pass[0])
    : w.subscores[0];

  // Графика/D3D приходят из native `winsat dwm` и требуют прав администратора.
  // В web их стадия сама поднимается через UAC; если подскор не PASS — значит
  // UAC отклонён или elevation не удалось → подсказываем перезапуск от админа.
  const gpuNotPass = !mockMode && w.subscores.some((s) => {
    const cat = (s.category || '').toLowerCase();
    return (cat === 'graphics' || cat === 'd3d')
      && (s.status || '').toUpperCase() !== 'PASS';
  });
  const gpuWarn = gpuNotPass
    ? `<div class="banner warn" style="margin-bottom: var(--gap);">
        <b>Графика / Игровая графика (D3D) не посчитаны.</b> Этим стадиям нужны
        права администратора (native <code>winsat dwm</code>). При запросе UAC
        во время прогона подтвердите его — или запустите ApexCore
        <b>от имени администратора</b> и повторите Winsat.
      </div>`
    : '';

  host.innerHTML = `
    <div class="winsat-screen">
      ${renderHeader('done', `прогон от ${fmtDate(w.timestamp)}`, true)}
      ${gpuWarn}

      <div class="card winsat-level-banner">
        <div class="card__title">Уровень системы (WinSPR)</div>
        <div class="winsat-level-row">
          <div class="winsat-level-row__main">
            <span class="winsat-level-value ${tone}">${fmtNum(lvl, 1)}</span>
            <div class="winsat-level-meta">
              <div class="winsat-level-scale">из 9.9</div>
              <div class="winsat-level-hint">На оценку влияют: CPU · Память · Диск · Графика · D3D</div>
              <span class="chip ${tone}">${escapeHtml(verdict)}</span>
            </div>
          </div>
          <div class="winsat-level-row__sep"></div>
          <div class="winsat-level-row__bottleneck">
            <div class="winsat-bottleneck-label">Самая слабая категория</div>
            <div class="winsat-bottleneck-value">
              <span class="winsat-bottleneck-cat">${escapeHtml(SUBSCORE_LABELS[bottleneck.category])}</span>
              · ${fmtNum(bottleneck.score, 1)}
            </div>
            <div class="winsat-bottleneck-explain">
              Winsat определяет общий уровень как минимум из 5 подскоров.
              Остальные 4 категории выше ${fmtNum(bottleneck.score, 1)}, но
              не поднимают общий уровень.
            </div>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card__title">Подскоры</div>
        <div class="winsat-subscores">
          ${w.subscores.map(renderSubscoreTile).join('')}
        </div>
      </div>

      ${methodologyCard()}
    </div>
  `;
  document.getElementById('btn-winsat-back')?.addEventListener('click', () => {
    view = 'idle';
    mockMode = false;
    realResult = null;
    renderHost(host);
  });
}

function renderSubscoreTile(s) {
  // Нормализация статуса: backend отдаёт lowercase enum-value (pass/na/error/...).
  const stKey = (s.status || '').toUpperCase();
  const statusToTone = {
    PASS: 'cool',
    NA: 'muted',
    ERROR: 'hot',
    NOT_SUPPORTED_ON_OS: 'warm',
  };
  const statusLabel = {
    PASS: 'PASS',
    NA: 'нет данных',
    ERROR: 'ошибка',
    NOT_SUPPORTED_ON_OS: 'не поддерживается',
  };
  const tone = statusToTone[stKey] || 'muted';
  // metric_value: 0.0 для NA/ERROR — backend всегда отдаёт число, скрываем при NA/ERROR.
  const showValue = stKey === 'PASS' && s.metric_value != null && s.metric_value > 0;
  return `<div class="winsat-subscore-tile ${stKey === 'PASS' ? 'pass' : 'other'}">
    <div class="winsat-subscore-tile__head">
      <span class="winsat-subscore-tile__cat">${escapeHtml(SUBSCORE_LABELS[s.category] || s.category)}</span>
      <span class="chip ${tone}">${escapeHtml(statusLabel[stKey] || s.status)}</span>
    </div>
    <div class="winsat-subscore-tile__score-row">
      <span class="winsat-subscore-tile__score ${tone}">${fmtNum(s.score, 1)}</span>
      <span class="winsat-subscore-tile__max">/ 9.9</span>
    </div>
    <div class="winsat-subscore-tile__metric">${escapeHtml(humanMetricName(s.metric_name))}</div>
    ${showValue ? `<div class="winsat-subscore-tile__value">
      ${fmtNum(s.metric_value, s.metric_value > 100 ? 0 : 1)}
      <span class="winsat-subscore-tile__unit">${escapeHtml(s.metric_unit || '')}</span>
    </div>` : ''}
    ${s.note ? `<div class="winsat-subscore-tile__note">${escapeHtml(s.note)}</div>` : ''}
  </div>`;
}

// Done-view при ошибке: backend вернул status="failed" (например, на Linux,
// либо если Winsat-сервис упал внутри).
function renderDoneError(host) {
  host.innerHTML = `
    <div class="winsat-screen">
      ${renderHeader('done', 'ошибка', true)}
      <div class="banner danger">
        <b>Прогон Winsat упал:</b> ${escapeHtml(realError || 'неизвестная ошибка')}
      </div>
    </div>
  `;
  document.getElementById('btn-winsat-back')?.addEventListener('click', () => {
    realError = null;
    view = 'idle';
    renderHost(host);
  });
}

// ─── LINUX platform-restriction ──────────────────────────────────────

function renderLinuxRestriction(host) {
  host.innerHTML = `
    <div class="winsat-screen">
      ${renderHeader('linux', 'недоступен на этой ОС')}
      <div class="winsat-linux-card">
        <div class="winsat-linux-card__tag">// PLATFORM RESTRICTED</div>
        <div class="winsat-linux-card__title">Winsat доступен только на Windows.</div>
        <div class="winsat-linux-card__body">
          Winsat (Windows System Assessment Tool) — Windows-специфичный API.
          Linux-эквивалента нет.<br><br>
          Для оценки производительности этой машины используйте:
        </div>
        <div class="winsat-linux-card__actions">
          <a class="btn primary" href="#cpu-advanced">▶ Расш. тест процессора</a>
          <a class="btn" href="#general">▶ Общая оценка системы</a>
        </div>
      </div>
    </div>
  `;
}

// ─── header ──────────────────────────────────────────────────────────

function renderHeader(state, subtitle, isDone = false) {
  const showState = state === 'done' || state === 'linux';
  const stateLabel = state === 'done' ? 'результат' : state === 'linux' ? 'недоступен на Linux' : '';
  const stateColor = state === 'done' ? 'cool' : state === 'linux' ? 'hot' : '';
  const action = isDone
    ? `<button class="btn sm" id="btn-winsat-back">← Назад</button>
       <button class="btn sm" disabled title="Экспорт появится в следующей версии">Экспорт</button>
       <button class="btn sm primary" disabled title="Появится в следующей версии">Запустить снова</button>`
    : (state === 'linux' ? '' : `<button class="btn sm" disabled title="История появится в следующей версии">История</button>`);
  return `
    <div class="screen__header">
      <div class="screen__header__title">
        <h1>Наследие Winsat</h1>
        <span class="screen__header__index">07</span>
        ${showState ? `<span class="screen__header__state ${stateColor}">· ${escapeHtml(stateLabel)}${subtitle ? ' · ' + escapeHtml(subtitle) : ''}</span>` : ''}
      </div>
      <div class="screen__header__chips">
        <span class="screen__header__goal screen__header__goal--amber">сверка с native Windows</span>
        <span class="screen__header__desc">шкала 1.0 – 9.9 · 5 подскоров · Windows-only</span>
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
