// Тест GPU — два режима: Roofline-бенчмарк + стресс-тест (термостабильность).
//
// Режим «Бенчмарк»: idle (выбор устройства + описание фаз + Roofline-нота)
// → running (прогресс-бар из status.progress + фаза + Стоп) → done (карточка
// результата: балл X / 10000, таблица измерено vs пик, notes). Backend §9.5
// (GET /api/gpu/devices, POST /api/gpu/start, GET /api/gpu/status,
// POST /api/gpu/stop). last_result держится прямо в status().
//
// Режим «Стресс-тест»: длительная FP32-нагрузка + посекундная телеметрия +
// вердикт PASS/WARN/FAIL/UNKNOWN. Backend §9.5 (POST /api/gpu/stress/start
// body {device_index, duration_sec}, GET /api/gpu/stress/status с
// progress={elapsed_sec,duration_sec}, POST /api/gpu/stress/stop). last_result
// (GpuStressReport.model_dump) — из status(). done-вью: большой бейдж вердикта
// + сводка (темп/мощность/частота/загрузка/тепловой лимит) + throttle_reasons/
// notes + спарклайн температуры из samples.
//
// Оба режима делят список устройств (/api/gpu/devices) и device-picker. Без
// GPU: { available:false } → «OpenCL/GPU не обнаружен», запуск заблокирован.

import { api } from '../api.js';
import { fmtNum, fmtDuration } from '../format.js';
import { renderSparkline } from '../components/sparkline.js';

const SCORE_MAX = 10000;

// Порядок и человекочитаемые подписи фаз оркестратора (gpu_benchmark.py).
const PHASE_LABEL = {
  fp32:          'FP32 · вычисления одинарной точности',
  fp64:          'FP64 · вычисления двойной точности',
  mem_bandwidth: 'VRAM · пропускная способность памяти',
  pcie_h2d:      'PCIe · копирование host → device',
  pcie_d2h:      'PCIe · копирование device → host',
};

// Человекочитаемые подписи вердикта GPU-стресса (GpuStressVerdict).
const VERDICT_META = {
  pass:    { label: 'PASS',    sub: 'стабильно',            tone: 'ok',     chip: 'ok' },
  warn:    { label: 'WARN',    sub: 'есть просадки',        tone: 'warm',   chip: 'warn' },
  fail:    { label: 'FAIL',    sub: 'троттлинг',            tone: 'hot',    chip: 'danger' },
  unknown: { label: 'UNKNOWN', sub: 'нет телеметрии',      tone: '',       chip: 'idle' },
};

// Пресеты длительности стресса (секунды). 60 с — быстрая проверка, дольше —
// ближе к «прогретому» установившемуся режиму.
const STRESS_DURATIONS = [
  { sec: 60,  label: '1 мин' },
  { sec: 180, label: '3 мин' },
  { sec: 300, label: '5 мин' },
  { sec: 600, label: '10 мин' },
];

let mode = 'bench';        // bench | stress — активная вкладка

// ── Бенчмарк-состояние ──
let view = 'idle';         // idle | running | done
let pollHandle = null;
let lastResult = null;     // GpuBenchmarkReport (model_dump) из status.last_result

// ── Стресс-состояние ──
let stressView = 'idle';   // idle | running | done
let stressPollHandle = null;
let stressLastResult = null; // GpuStressReport (model_dump) из status.last_result
let stressDuration = 60;   // выбранная длительность, сек

// ── Общее ──
let devices = null;        // [GpuDeviceInfo] из /api/gpu/devices
let available = null;      // bool: доступен ли OpenCL/GPU
let selectedIndex = 0;     // выбранный device_index
let loadError = null;      // ошибка загрузки списка устройств
let hostEl = null;         // текущий контейнер экрана

export function render(host) {
  hostEl = host;
  renderHost(host);
  void bootstrap(host);
}

export function dispose() {
  if (pollHandle) { clearInterval(pollHandle); pollHandle = null; }
  if (stressPollHandle) { clearInterval(stressPollHandle); stressPollHandle = null; }
}

async function bootstrap(host) {
  await loadDevices();
  renderHost(host);
  // При входе синхронизируем статус активного режима (F5 во время прогона).
  if (mode === 'stress') void syncStressStatus(host);
  else void syncStatus(host);
}

async function loadDevices() {
  if (devices !== null) return;
  try {
    const resp = await api.gpuDevices();
    available = !!resp.available;
    devices = Array.isArray(resp.devices) ? resp.devices : [];
    if (devices.length) {
      // По умолчанию — первое устройство (дискретное идёт первым в списке).
      selectedIndex = devices[0].index ?? 0;
    }
  } catch (err) {
    loadError = err.message;
    available = false;
    devices = [];
  }
}

// ─── mode switch ─────────────────────────────────────────────────────

function switchMode(next, host) {
  if (next === mode) return;
  // Останавливаем polling неактивного режима (сам прогон на backend'е
  // продолжится — при возврате syncStatus подхватит его снова).
  if (pollHandle) { clearInterval(pollHandle); pollHandle = null; }
  if (stressPollHandle) { clearInterval(stressPollHandle); stressPollHandle = null; }
  mode = next;
  renderHost(host);
  if (mode === 'stress') void syncStressStatus(host);
  else void syncStatus(host);
}

function renderHost(host) {
  if (mode === 'stress') {
    if (stressView === 'running') return renderStressRunning(host);
    if (stressView === 'done')    return renderStressDone(host);
    return renderStressIdle(host);
  }
  if (view === 'running') return renderRunning(host);
  if (view === 'done')    return renderDone(host);
  return renderIdle(host);
}

// Переключатель режимов (две кнопки; активная — primary). Общий для всех вью.
function renderModeToggle() {
  return `
    <div class="gpu-mode-toggle" role="tablist" aria-label="Режим теста GPU">
      <button class="btn sm ${mode === 'bench' ? 'primary' : ''}" id="btn-gpu-mode-bench"
              role="tab" aria-selected="${mode === 'bench'}">Бенчмарк</button>
      <button class="btn sm ${mode === 'stress' ? 'primary' : ''}" id="btn-gpu-mode-stress"
              role="tab" aria-selected="${mode === 'stress'}">Стресс-тест</button>
    </div>`;
}

function bindModeToggle(host) {
  document.getElementById('btn-gpu-mode-bench')?.addEventListener('click', () => switchMode('bench', host));
  document.getElementById('btn-gpu-mode-stress')?.addEventListener('click', () => switchMode('stress', host));
}

// ═══════════════════════ БЕНЧМАРК ═══════════════════════════════════

async function syncStatus(host) {
  try {
    const s = await api.gpuStatus();
    if (s.running) {
      view = 'running';
      renderHost(host);
      if (!pollHandle) {
        pollHandle = setInterval(() => { void syncStatus(host); }, 1500);
      }
      updateRunningProgress(s);
    } else if ((s.status === 'completed' || s.status === 'cancelled') && s.last_result) {
      if (pollHandle) { clearInterval(pollHandle); pollHandle = null; }
      lastResult = s.last_result;
      view = 'done';
      renderHost(host);
    } else if (s.status === 'failed') {
      if (pollHandle) { clearInterval(pollHandle); pollHandle = null; }
      lastResult = { error: s.error || 'Прогон упал' };
      view = 'done';
      renderHost(host);
    }
  } catch (err) {
    // Тихо — следующая итерация попробует снова.
  }
}

async function onStart(host) {
  try {
    await api.gpuStart({ device_index: selectedIndex });
    view = 'running';
    lastResult = null;
    renderHost(host);
    if (!pollHandle) pollHandle = setInterval(() => { void syncStatus(host); }, 1500);
  } catch (err) {
    alert('Не удалось запустить: ' + err.message);
  }
}

async function onStop() {
  try { await api.gpuStop(); }
  catch (err) { alert('Не удалось остановить: ' + err.message); }
}

function renderIdle(host) {
  const noGpu = available === false || (devices !== null && devices.length === 0);

  host.innerHTML = `
    <div class="general-screen">
      ${renderHeader('idle', null)}

      <div class="general-layout">
        <div class="card general-main">
          <div class="card__title">Тест видеокарты</div>

          <div class="general-intro">
            Пять коротких фаз — итоговый балл вычислителя за ~30 секунд. Измеряем
            <b>реальную производительность</b> GPU и сравниваем с теоретическим
            пиком архитектуры (методика Roofline).
          </div>

          <div class="general-phases">
            ${renderPhase('FP32',  'вычисления одинарной точности (GFLOPS)', 'входит в балл')}
            ${renderPhase('VRAM',  'пропускная способность памяти (STREAM-triad)', 'входит в балл')}
            ${renderPhase('FP64',  'вычисления двойной точности', 'информационно')}
            ${renderPhase('PCIe',  'копирование host ↔ device', 'информационно')}
          </div>

          ${noGpu ? renderNoGpuBanner() : renderDevicePicker()}

          <div class="general-launch">
            <button class="btn primary lg general-launch__btn" id="btn-gpu-start" ${noGpu ? 'disabled' : ''}>
              ▶ &nbsp; Запустить тест
            </button>
            <div class="general-launch__hint">
              ${noGpu
                ? 'Запуск недоступен — OpenCL-устройство не обнаружено.'
                : 'Один клик — прогонит FP32, VRAM, FP64 и PCIe за ~30 секунд и построит итоговый балл.'}
            </div>
          </div>
        </div>

        <div class="general-side">
          <div class="card">
            <div class="card__title">Что означает балл</div>
            <div class="general-explainer">
              <p><b>Roofline</b>: измеренная производительность делится на
              <b>архитектурный пик</b> устройства (число блоков × частота ×
              операций за такт). Балл — доля от потолка в шкале ×10 000.</p>
              <p>Headline-балл строится по <b>FP32</b> и <b>пропускной способности
              VRAM</b> — это то, что определяет большинство GPU-нагрузок.</p>
              <p><b>FP64</b> и <b>PCIe</b> измеряются, но в балл не входят: на
              потребительских и встроенных GPU FP64 намеренно урезан и не должен
              занижать общую оценку.</p>
            </div>
          </div>

          <div class="card">
            <div class="card__title">Шкала</div>
            <div class="general-scale">
              <div class="general-scale-row">
                <span class="general-scale-row__chip cool">≥ 6000</span>
                <span>отличный результат</span>
              </div>
              <div class="general-scale-row">
                <span class="general-scale-row__chip warm">3000-6000</span>
                <span>средний результат</span>
              </div>
              <div class="general-scale-row">
                <span class="general-scale-row__chip hot">&lt; 3000</span>
                <span>низкий результат</span>
              </div>
              <div class="general-scale-note">
                Балл ×10 000 = среднее геометрическое r_fp32 и r_mem
                (отношений к теоретическому пику архитектуры).
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  `;
  bindModeToggle(host);
  document.getElementById('btn-gpu-start')?.addEventListener('click', () => { void onStart(host); });
  const sel = document.getElementById('gpu-device-select');
  sel?.addEventListener('change', (e) => { selectedIndex = Number(e.target.value); });
}

function renderNoGpuBanner() {
  return `
    <div class="banner info" style="margin-top: var(--gap-lg);">
      <b>OpenCL/GPU не обнаружен.</b> ICD-loader не загрузился или в системе нет
      OpenCL-совместимого устройства. Установите драйверы GPU (NVIDIA/AMD/Intel)
      с поддержкой OpenCL и обновите страницу.
      ${loadError ? `<div style="margin-top: 6px; color: var(--muted);">Детали: ${escapeHtml(loadError)}</div>` : ''}
    </div>`;
}

function renderDevicePicker() {
  const opts = (devices || []).map((d) => {
    const label = `${d.name}${d.device_type ? ' · ' + humanDeviceType(d.device_type) : ''}`;
    const sel = d.index === selectedIndex ? 'selected' : '';
    return `<option value="${d.index}" ${sel}>${escapeHtml(label)}</option>`;
  }).join('');
  const dev = (devices || []).find((d) => d.index === selectedIndex) || (devices || [])[0];

  return `
    <div class="general-section-label">Устройство</div>
    <div class="field" style="margin-bottom: var(--gap-lg);">
      <div class="field__label">GPU для теста</div>
      <select id="gpu-device-select" style="min-width: 320px;">${opts}</select>
      ${dev ? `<div class="field__hint">${escapeHtml(deviceHint(dev))}</div>` : ''}
    </div>`;
}

function deviceHint(d) {
  const parts = [];
  if (d.vendor) parts.push(d.vendor);
  if (d.compute_units) parts.push(`${d.compute_units} блоков`);
  if (d.max_clock_mhz) parts.push(`${fmtNum(d.max_clock_mhz, 0)} МГц`);
  if (d.global_mem_mb) parts.push(`${fmtNum(d.global_mem_mb / 1024, 1)} ГБ VRAM`);
  parts.push(d.fp64_supported ? 'FP64 есть' : 'FP64 нет');
  return parts.join(' · ');
}

function renderRunning(host) {
  const devName = (devices || []).find((d) => d.index === selectedIndex)?.name || 'GPU';
  host.innerHTML = `
    <div class="general-screen">
      ${renderHeader('running', 'идёт тест видеокарты…')}

      <div class="card">
        <div class="card__title">
          ${escapeHtml(devName)} <span class="card__title-tag" id="gpu-phase-tag">…</span>
        </div>
        <div class="progress" style="margin-top: var(--gap);">
          <div class="progress__fill" id="gpu-progress-fill" style="width: 0%;"></div>
        </div>
        <div class="rows" style="margin-top: var(--gap);">
          <div class="row"><span class="k">фаза</span><span class="v" id="gpu-phase">подготовка</span></div>
          <div class="row"><span class="k">шаг</span><span class="v" id="gpu-step">—</span></div>
        </div>
        <div style="margin-top: var(--gap-lg); text-align: right;">
          <button class="btn sm" id="btn-gpu-stop">■ Остановить</button>
        </div>
      </div>

      <div style="margin-top: var(--gap-lg); text-align: center; color: var(--muted); font-size: 12px;">
        Полный прогон занимает примерно <b style="color: var(--text);">30 секунд</b>.
        Не закрывайте вкладку — результат сохранится автоматически.
      </div>
    </div>
  `;
  document.getElementById('btn-gpu-stop')?.addEventListener('click', () => { void onStop(); });
}

function updateRunningProgress(status) {
  const phase = status.progress?.phase || '';
  const idx = status.progress?.idx;
  const total = status.progress?.total;
  const tag = document.getElementById('gpu-phase-tag');
  const phaseEl = document.getElementById('gpu-phase');
  const stepEl = document.getElementById('gpu-step');
  const human = phase ? humanPhase(phase) : 'подготовка';
  if (tag) tag.textContent = human;
  if (phaseEl) phaseEl.textContent = human;
  if (stepEl) {
    stepEl.textContent = (typeof idx === 'number' && typeof total === 'number' && total > 0)
      ? `${idx} из ${total}` : '—';
  }
  // Прогресс по середине текущей фазы, чтобы бар не прыгал на 0 в начале.
  const fillEl = document.getElementById('gpu-progress-fill');
  if (fillEl) {
    const pct = (typeof idx === 'number' && typeof total === 'number' && total > 0)
      ? ((idx - 0.5) / total) * 100
      : 5;
    fillEl.style.width = `${Math.max(5, Math.min(100, pct)).toFixed(1)}%`;
  }
}

function humanPhase(p) {
  return PHASE_LABEL[p] || p;
}

function renderDone(host) {
  const r = lastResult;
  if (!r || r.error) {
    host.innerHTML = `
      <div class="general-screen">
        ${renderHeader('done', 'ошибка прогона', true)}
        <div class="banner danger"><b>Не удалось завершить прогон:</b> ${escapeHtml(r?.error || 'неизвестная ошибка')}</div>
      </div>
    `;
    document.getElementById('btn-gpu-back')?.addEventListener('click', () => {
      view = 'idle'; lastResult = null; renderHost(host);
    });
    return;
  }

  const dev = r.device || {};
  const hasScore = r.score != null;
  const tone = colorTone(r.score);
  const verdict = !hasScore
    ? 'Балл недоступен'
    : r.score >= 6000 ? 'Отличный результат' : r.score >= 3000 ? 'Средний результат' : 'Низкий результат';
  const dur = (r.ended_at && r.started_at)
    ? (new Date(r.ended_at) - new Date(r.started_at)) / 1000 : null;
  const cancelledTag = r.cancelled ? 'прервано · ' : '';

  host.innerHTML = `
    <div class="general-screen">
      ${renderHeader('done', `${cancelledTag}${escapeHtml(dev.name || 'GPU')}${dur != null ? ' · ' + fmtDuration(dur) : ''}`, true)}

      <div class="general-result-banner card">
        <div class="general-result-banner__left">
          <div class="general-result-banner__value ${tone}">${formatScoreOutOfMax(r.score)}</div>
          <div class="general-result-banner__meta">
            <div class="general-result-banner__label">БАЛЛ GPU</div>
            <div class="general-result-banner__hint">${escapeHtml(dev.name || 'видеокарта')}${dev.device_type ? ' · ' + humanDeviceType(dev.device_type) : ''}</div>
            <span class="chip ${chipTone(r.score)}">${escapeHtml(verdict)}</span>
          </div>
        </div>
        <div class="general-result-banner__sep"></div>
        <div class="general-result-banner__right">
          <div class="general-section-label">Больше — лучше · % от теоретического пика</div>
          <div class="general-subscores">
            ${renderSubscoreTile('FP32', r.r_fp32)}
            ${renderSubscoreTile('VRAM', r.r_mem)}
            ${renderSubscoreTile('FP64', r.r_fp64, true)}
          </div>
          <div class="general-result-banner__legend">
            Балл ×10 000 = среднее геометрическое r_fp32 и r_mem. FP64 —
            информационный (в балл не входит)${r.peak_source ? ` · пик: ${escapeHtml(r.peak_source)}` : ''}.
          </div>
        </div>
      </div>

      <div class="general-grid-two">
        <div class="card">
          <div class="card__title">Измерено против пика</div>
          <div class="rows">
            <div class="row"><span class="k">FP32</span><span class="v">${r.fp32_gflops != null ? fmtNum(r.fp32_gflops, 0) + ' GFLOPS' : '—'}</span></div>
            <div class="row"><span class="k">↳ пик архитектуры</span><span class="v">${r.fp32_peak_gflops != null ? fmtNum(r.fp32_peak_gflops, 0) + ' GFLOPS' : '—'}</span></div>
            <div class="row"><span class="k">↳ % от пика (r_fp32)</span><span class="v ${toneForRatio(r.r_fp32)}">${formatPercent(r.r_fp32)}</span></div>
            <div class="row"><span class="k">VRAM bandwidth</span><span class="v">${r.mem_bandwidth_gb_s != null ? fmtNum(r.mem_bandwidth_gb_s, 1) + ' ГБ/с' : '—'}</span></div>
            <div class="row"><span class="k">↳ пик архитектуры</span><span class="v">${r.mem_bandwidth_peak_gb_s != null ? fmtNum(r.mem_bandwidth_peak_gb_s, 1) + ' ГБ/с' : '—'}</span></div>
            <div class="row"><span class="k">↳ % от пика (r_mem)</span><span class="v ${toneForRatio(r.r_mem)}">${formatPercent(r.r_mem)}</span></div>
          </div>
        </div>

        <div class="card">
          <div class="card__title">Информационно (вне балла)</div>
          <div class="rows">
            <div class="row"><span class="k">FP64</span><span class="v">${r.fp64_gflops != null ? fmtNum(r.fp64_gflops, 0) + ' GFLOPS' : (dev.fp64_supported ? '—' : 'не поддерж.')}</span></div>
            <div class="row"><span class="k">↳ % от пика (r_fp64)</span><span class="v">${formatPercent(r.r_fp64)}</span></div>
            <div class="row"><span class="k">PCIe host → device</span><span class="v">${r.pcie_h2d_gb_s != null ? fmtNum(r.pcie_h2d_gb_s, 1) + ' ГБ/с' : '—'}</span></div>
            <div class="row"><span class="k">PCIe device → host</span><span class="v">${r.pcie_d2h_gb_s != null ? fmtNum(r.pcie_d2h_gb_s, 1) + ' ГБ/с' : '—'}</span></div>
            ${dev.compute_units ? `<div class="row"><span class="k">Вычислительных блоков</span><span class="v">${fmtNum(dev.compute_units, 0)}</span></div>` : ''}
            ${dev.global_mem_mb ? `<div class="row"><span class="k">Объём VRAM</span><span class="v">${fmtNum(dev.global_mem_mb / 1024, 1)} ГБ</span></div>` : ''}
          </div>
        </div>
      </div>

      ${renderNotes(r.notes)}
    </div>
  `;
  document.getElementById('btn-gpu-back')?.addEventListener('click', () => {
    view = 'idle'; lastResult = null; renderHost(host);
  });
  document.getElementById('btn-gpu-rerun')?.addEventListener('click', () => { void onStart(host); });
}

// ═══════════════════════ СТРЕСС-ТЕСТ ════════════════════════════════

async function syncStressStatus(host) {
  try {
    const s = await api.gpuStressStatus();
    if (s.running) {
      stressView = 'running';
      renderHost(host);
      if (!stressPollHandle) {
        stressPollHandle = setInterval(() => { void syncStressStatus(host); }, 1500);
      }
      updateStressProgress(s);
    } else if ((s.status === 'completed' || s.status === 'cancelled') && s.last_result) {
      if (stressPollHandle) { clearInterval(stressPollHandle); stressPollHandle = null; }
      stressLastResult = s.last_result;
      stressView = 'done';
      renderHost(host);
    } else if (s.status === 'failed') {
      if (stressPollHandle) { clearInterval(stressPollHandle); stressPollHandle = null; }
      stressLastResult = { error: s.error || 'Прогон упал' };
      stressView = 'done';
      renderHost(host);
    }
  } catch (err) {
    // Тихо — следующая итерация попробует снова.
  }
}

async function onStressStart(host) {
  try {
    await api.gpuStressStart({ device_index: selectedIndex, duration_sec: stressDuration });
    stressView = 'running';
    stressLastResult = null;
    renderHost(host);
    if (!stressPollHandle) stressPollHandle = setInterval(() => { void syncStressStatus(host); }, 1500);
  } catch (err) {
    alert('Не удалось запустить стресс: ' + err.message);
  }
}

async function onStressStop() {
  try { await api.gpuStressStop(); }
  catch (err) { alert('Не удалось остановить: ' + err.message); }
}

function renderStressIdle(host) {
  const noGpu = available === false || (devices !== null && devices.length === 0);
  const durOpts = STRESS_DURATIONS.map((d) =>
    `<option value="${d.sec}" ${d.sec === stressDuration ? 'selected' : ''}>${d.label}</option>`
  ).join('');

  host.innerHTML = `
    <div class="general-screen">
      ${renderHeader('idle', null)}

      <div class="general-layout">
        <div class="card general-main">
          <div class="card__title">Стресс-тест видеокарты</div>

          <div class="general-intro">
            Длительная максимальная <b>FP32-нагрузка</b> («power virus») с
            посекундной телеметрией. Проверяет <b>термостабильность</b> и
            троттлинг под sustained-нагрузкой — GPU-аналог полного стресс-теста
            CPU, headless и без внешних утилит (FurMark и т.п.).
          </div>

          <div class="general-phases">
            ${renderPhase('Нагрузка', 'максимальный ALU-кернел FP32', 'весь прогон')}
            ${renderPhase('Телеметрия', 'температура · мощность · частота · загрузка', 'раз в ~1 с')}
            ${renderPhase('Троттлинг', 'обвал частоты / тепловой лимит / просадка', 'детекция')}
            ${renderPhase('Вердикт', 'PASS · WARN · FAIL · UNKNOWN', 'итог')}
          </div>

          ${noGpu ? renderNoGpuBanner() : renderStressControls(durOpts)}

          <div class="general-launch">
            <button class="btn primary lg general-launch__btn" id="btn-gpu-stress-start" ${noGpu ? 'disabled' : ''}>
              ▶ &nbsp; Запустить стресс
            </button>
            <div class="general-launch__hint">
              ${noGpu
                ? 'Запуск недоступен — OpenCL-устройство не обнаружено.'
                : 'Тест максимально греет GPU. Длительность ограничена сверху — можно остановить в любой момент.'}
            </div>
          </div>
        </div>

        <div class="general-side">
          <div class="card">
            <div class="card__title">Что означает вердикт</div>
            <div class="general-explainer">
              <p><b>PASS</b> — частоты держатся, температура в норме, троттлинга нет.</p>
              <p><b>WARN</b> — есть признаки просадки (штатный boost-settle,
              температура у порога или прогон прерван) — не критично.</p>
              <p><b>FAIL</b> — тепловой троттлинг: устройство не держит
              sustained-режим (крупный обвал частоты или достигнут тепловой лимит).</p>
              <p><b>UNKNOWN</b> — нет NVML/hwmon-телеметрии GPU: нагрузка
              выполнена, но судить о стабильности не по чему.</p>
            </div>
          </div>

          <div class="card">
            <div class="card__title">Как читать</div>
            <div class="general-scale">
              <div class="general-scale-row">
                <span class="general-scale-row__chip cool">PASS</span>
                <span>стабильно под нагрузкой</span>
              </div>
              <div class="general-scale-row">
                <span class="general-scale-row__chip warm">WARN</span>
                <span>небольшие просадки</span>
              </div>
              <div class="general-scale-row">
                <span class="general-scale-row__chip hot">FAIL</span>
                <span>троттлинг / перегрев</span>
              </div>
              <div class="general-scale-note">
                Частота сравнивается между началом и концом прогона (first-third
                vs last-third) — начальный буст не считается троттлингом.
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  `;
  bindModeToggle(host);
  document.getElementById('btn-gpu-stress-start')?.addEventListener('click', () => { void onStressStart(host); });
  const sel = document.getElementById('gpu-device-select');
  sel?.addEventListener('change', (e) => { selectedIndex = Number(e.target.value); });
  const durSel = document.getElementById('gpu-stress-duration');
  durSel?.addEventListener('change', (e) => { stressDuration = Number(e.target.value); });
}

function renderStressControls(durOpts) {
  const opts = (devices || []).map((d) => {
    const label = `${d.name}${d.device_type ? ' · ' + humanDeviceType(d.device_type) : ''}`;
    const sel = d.index === selectedIndex ? 'selected' : '';
    return `<option value="${d.index}" ${sel}>${escapeHtml(label)}</option>`;
  }).join('');
  const dev = (devices || []).find((d) => d.index === selectedIndex) || (devices || [])[0];

  return `
    <div class="general-section-label">Параметры</div>
    <div class="gpu-stress-controls" style="margin-bottom: var(--gap-lg);">
      <div class="field">
        <div class="field__label">GPU для теста</div>
        <select id="gpu-device-select" style="min-width: 320px;">${opts}</select>
        ${dev ? `<div class="field__hint">${escapeHtml(deviceHint(dev))}</div>` : ''}
      </div>
      <div class="field">
        <div class="field__label">Длительность</div>
        <select id="gpu-stress-duration" style="min-width: 140px;">${durOpts}</select>
        <div class="field__hint">дольше — ближе к установившемуся режиму</div>
      </div>
    </div>`;
}

function renderStressRunning(host) {
  const devName = (devices || []).find((d) => d.index === selectedIndex)?.name || 'GPU';
  host.innerHTML = `
    <div class="general-screen">
      ${renderHeader('running', 'идёт стресс-тест GPU…')}

      <div class="card">
        <div class="card__title">
          ${escapeHtml(devName)} <span class="card__title-tag" id="gpu-stress-tag">нагрузка</span>
        </div>
        <div class="progress" style="margin-top: var(--gap);">
          <div class="progress__fill" id="gpu-stress-fill" style="width: 0%;"></div>
        </div>
        <div class="rows" style="margin-top: var(--gap);">
          <div class="row"><span class="k">прошло</span><span class="v" id="gpu-stress-elapsed">0 с</span></div>
          <div class="row"><span class="k">всего</span><span class="v" id="gpu-stress-total">${fmtDuration(stressDuration)}</span></div>
        </div>
        <div style="margin-top: var(--gap-lg); text-align: right;">
          <button class="btn sm" id="btn-gpu-stress-stop">■ Остановить</button>
        </div>
      </div>

      <div style="margin-top: var(--gap-lg); text-align: center; color: var(--muted); font-size: 12px;">
        GPU под максимальной FP32-нагрузкой. Не закрывайте вкладку — результат
        и вердикт сохранятся автоматически.
      </div>
    </div>
  `;
  document.getElementById('btn-gpu-stress-stop')?.addEventListener('click', () => { void onStressStop(); });
}

function updateStressProgress(status) {
  const elapsed = status.progress?.elapsed_sec;
  const total = status.progress?.duration_sec || stressDuration;
  const elapsedEl = document.getElementById('gpu-stress-elapsed');
  const totalEl = document.getElementById('gpu-stress-total');
  const fillEl = document.getElementById('gpu-stress-fill');
  if (elapsedEl && typeof elapsed === 'number') elapsedEl.textContent = fmtDuration(elapsed);
  if (totalEl && typeof total === 'number' && total > 0) totalEl.textContent = fmtDuration(total);
  if (fillEl) {
    const pct = (typeof elapsed === 'number' && typeof total === 'number' && total > 0)
      ? (elapsed / total) * 100 : 3;
    fillEl.style.width = `${Math.max(3, Math.min(100, pct)).toFixed(1)}%`;
  }
}

function renderStressDone(host) {
  const r = stressLastResult;
  if (!r || r.error) {
    host.innerHTML = `
      <div class="general-screen">
        ${renderHeader('done', 'ошибка прогона', true)}
        <div class="banner danger"><b>Не удалось завершить стресс-тест:</b> ${escapeHtml(r?.error || 'неизвестная ошибка')}</div>
      </div>
    `;
    document.getElementById('btn-gpu-back')?.addEventListener('click', () => {
      stressView = 'idle'; stressLastResult = null; renderHost(host);
    });
    return;
  }

  const dev = r.device || {};
  const verdict = (r.verdict || 'unknown').toLowerCase();
  const meta = VERDICT_META[verdict] || VERDICT_META.unknown;
  const dur = r.duration_sec != null ? r.duration_sec
    : (r.ended_at && r.started_at ? (new Date(r.ended_at) - new Date(r.started_at)) / 1000 : null);
  const cancelledTag = r.cancelled ? 'прервано · ' : '';
  // Спарклайн строим по температуре (fallback — частота/мощность/загрузка).
  const samples = Array.isArray(r.samples) ? r.samples : [];
  const sparkField = ['temp_c', 'clock_mhz', 'power_w', 'util_pct']
    .find((f) => samples.some((s) => typeof s[f] === 'number'));

  host.innerHTML = `
    <div class="general-screen">
      ${renderHeader('done', `${cancelledTag}${escapeHtml(dev.name || 'GPU')}${dur != null ? ' · ' + fmtDuration(dur) : ''}`, true)}

      <div class="general-result-banner card">
        <div class="general-result-banner__left">
          <div class="general-result-banner__value ${meta.tone}">${meta.label}</div>
          <div class="general-result-banner__meta">
            <div class="general-result-banner__label">ВЕРДИКТ GPU-СТРЕССА</div>
            <div class="general-result-banner__hint">${escapeHtml(dev.name || 'видеокарта')}${dev.device_type ? ' · ' + humanDeviceType(dev.device_type) : ''}</div>
            <span class="chip ${meta.chip}">${escapeHtml(meta.sub)}</span>
          </div>
        </div>
        <div class="general-result-banner__sep"></div>
        <div class="general-result-banner__right">
          <div class="general-section-label">Телеметрия под нагрузкой</div>
          <div class="rows">
            <div class="row"><span class="k">температура · пик / средн.</span><span class="v ${toneForTemp(r.max_temp_c, r.thermal_limit_c)}">${fmtOpt(r.max_temp_c, '°C', 0)} / ${fmtOpt(r.avg_temp_c, '°C', 0)}</span></div>
            <div class="row"><span class="k">мощность · пик / средн.</span><span class="v">${fmtOpt(r.max_power_w, ' Вт', 0)} / ${fmtOpt(r.avg_power_w, ' Вт', 0)}</span></div>
            <div class="row"><span class="k">частота · средн. / мин.</span><span class="v">${fmtOpt(r.avg_clock_mhz, ' МГц', 0)} / ${fmtOpt(r.min_clock_mhz, ' МГц', 0)}</span></div>
            <div class="row"><span class="k">загрузка · средн.</span><span class="v">${fmtOpt(r.avg_util_pct, '%', 0)}</span></div>
            <div class="row"><span class="k">тепловой лимит</span><span class="v">${fmtOpt(r.thermal_limit_c, '°C', 0)}</span></div>
          </div>
        </div>
      </div>

      ${sparkField ? renderStressSparkline(samples, sparkField) : ''}
      ${renderThrottle(r.throttle_reasons)}
      ${renderNotes(r.notes)}
    </div>
  `;
  // Спарклайн рисуем после вставки в DOM (нужны реальные размеры контейнера).
  if (sparkField) {
    const cont = document.getElementById('gpu-stress-spark');
    if (cont) {
      const vals = samples.map((s) => (typeof s[sparkField] === 'number' ? s[sparkField] : null));
      renderSparkline(cont, vals, { color: sparkField === 'temp_c' ? 'var(--warn)' : 'var(--accent)', height: 56 });
    }
  }
  document.getElementById('btn-gpu-back')?.addEventListener('click', () => {
    stressView = 'idle'; stressLastResult = null; renderHost(host);
  });
  document.getElementById('btn-gpu-rerun')?.addEventListener('click', () => { void onStressStart(host); });
}

const SPARK_LABEL = {
  temp_c:    'Температура ядра, °C',
  clock_mhz: 'Частота ядра, МГц',
  power_w:   'Мощность, Вт',
  util_pct:  'Загрузка GPU, %',
};

function renderStressSparkline(samples, field) {
  const label = SPARK_LABEL[field] || field;
  return `
    <div class="card" style="margin-top: var(--gap-lg);">
      <div class="card__title">
        <span>${escapeHtml(label)}</span>
        <span class="card__title-tag">${samples.length} отсчётов</span>
      </div>
      <div id="gpu-stress-spark" style="margin-top: var(--gap);"></div>
    </div>`;
}

function renderThrottle(reasons) {
  if (!Array.isArray(reasons) || reasons.length === 0) return '';
  return `
    <div class="card" style="margin-top: var(--gap-lg);">
      <div class="card__title">Признаки троттлинга</div>
      <div class="general-explainer">
        <ul style="margin: 0; padding-left: 18px;">
          ${reasons.map((n) => `<li>${escapeHtml(n)}</li>`).join('')}
        </ul>
      </div>
    </div>`;
}

// ═══════════════════════ ОБЩИЕ БЛОКИ ════════════════════════════════

function renderNotes(notes) {
  if (!Array.isArray(notes) || notes.length === 0) return '';
  return `
    <div class="card" style="margin-top: var(--gap-lg);">
      <div class="card__title">Примечания</div>
      <div class="general-explainer">
        <ul style="margin: 0; padding-left: 18px;">
          ${notes.map((n) => `<li>${escapeHtml(n)}</li>`).join('')}
        </ul>
      </div>
    </div>`;
}

function renderHeader(state, subtitle, isDone = false) {
  const showState = state === 'running' || state === 'done';
  const stateLabel = state === 'running' ? 'в работе' : state === 'done' ? 'результат' : '';
  const stateColor = state === 'done' ? 'cool' : state === 'running' ? 'accent' : '';
  const isStress = mode === 'stress';
  // В done-вью — «Назад / Запустить снова»; в idle/running — переключатель
  // режимов (одни и те же id кнопок для обоих режимов; обработчики навешивает
  // соответствующий renderDone/renderStressDone).
  const action = isDone
    ? `<button class="btn sm" id="btn-gpu-back">← Назад</button>
       <button class="btn sm primary" id="btn-gpu-rerun">↻ Запустить снова</button>`
    : renderModeToggle();
  const goal = isStress ? 'цель · проверить стабильность GPU' : 'цель · оценить видеокарту';
  const desc = isStress
    ? 'FP32-нагрузка + телеметрия · вердикт PASS/WARN/FAIL · термостабильность'
    : 'FP32 + VRAM + FP64 + PCIe · ~30 сек · методика Roofline';
  return `
    <div class="screen__header">
      <div class="screen__header__title">
        <h1>Тест GPU</h1>
        <span class="screen__header__index">05</span>
        ${showState ? `<span class="screen__header__state ${stateColor}">· ${escapeHtml(stateLabel)}${subtitle ? ' · ' + escapeHtml(subtitle) : ''}</span>` : ''}
      </div>
      <div class="screen__header__chips">
        <span class="screen__header__goal">${goal}</span>
        <span class="screen__header__desc">${desc}</span>
      </div>
      <div class="screen__header__actions">${action}</div>
    </div>
  `;
}

function renderPhase(name, sub, dur) {
  return `<div class="general-phase">
    <div class="general-phase__name">${escapeHtml(name)}</div>
    <div class="general-phase__sub">${escapeHtml(sub)}</div>
    <div class="general-phase__desc">${escapeHtml(dur)}</div>
  </div>`;
}

function renderSubscoreTile(label, ratio, informational = false) {
  if (ratio == null) {
    return `<div class="subscore-tile">
      <div class="subscore-tile__label">${escapeHtml(label)}${informational ? ' ·' : ''}</div>
      <div class="subscore-tile__value" style="color: var(--muted);">—</div>
    </div>`;
  }
  const pct = ratio * 100;
  const tone = ratio >= 0.6 ? 'cool' : ratio >= 0.3 ? 'warm' : '';
  return `<div class="subscore-tile">
    <div class="subscore-tile__label">${escapeHtml(label)}${informational ? ' ·' : ''}</div>
    <div class="subscore-tile__value ${tone}">${fmtNum(pct, 1)}%</div>
    <div class="subscore-tile__bar">
      <div class="subscore-tile__bar-fill ${tone}" style="width: ${Math.min(100, pct).toFixed(1)}%;"></div>
    </div>
  </div>`;
}

function humanDeviceType(t) {
  return ({
    discrete:   'дискретная',
    integrated: 'встроенная',
    virtual:    'виртуальная',
    unknown:    'неизвестный тип',
  }[t]) || t;
}

function colorTone(score) {
  // Без агрессивного красного: низкий/недоступный балл — нейтральный.
  if (score == null) return '';
  if (score >= 3000) return 'cool';
  return '';
}

function chipTone(score) {
  if (score == null) return 'idle';
  if (score >= 6000) return 'ok';
  if (score >= 3000) return 'warn';
  return 'idle';
}

function toneForRatio(ratio) {
  if (ratio == null) return '';
  if (ratio >= 0.6) return 'cool';
  if (ratio >= 0.3) return 'warm';
  return '';
}

// Тон температуры относительно теплового лимита (или абсолютных порогов).
function toneForTemp(maxTemp, limit) {
  if (maxTemp == null) return '';
  if (limit != null && limit > 0) {
    if (maxTemp >= limit) return 'hot';
    if (maxTemp >= limit - 3) return 'warm';
    return 'cool';
  }
  if (maxTemp >= 90) return 'hot';
  if (maxTemp >= 84) return 'warm';
  return 'cool';
}

function formatScoreOutOfMax(score) {
  // Главный показатель: «6 708 / 10 000». null → «недоступно».
  if (score == null) return 'недоступно';
  return `${Math.round(score).toLocaleString('ru-RU')} / ${SCORE_MAX.toLocaleString('ru-RU')}`;
}

function formatPercent(ratio) {
  return ratio != null ? `${fmtNum(ratio * 100, 1)}%` : '—';
}

// Опциональное число с единицей, null → «—».
function fmtOpt(value, unit = '', digits = 1) {
  return value != null ? `${fmtNum(value, digits)}${unit}` : '—';
}

function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[c]);
}
