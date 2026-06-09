// Общая оценка системы — pixel-close по console-general.jsx + UX-правки.
//
// UX-приоритет (по фидбэку): главное действие — «Запустить общую оценку»
// всей системы за один клик. Отдельные CPU/RAM/Disk прогоны — не главная
// фича этого экрана (для них есть «Расш. тест CPU» и Ram & Cache).
//
// Backend §9.4 — реализован (POST /api/general/start + GET /api/general/status
// + GET /api/general/runs/{id}). Кнопка запускает реальный прогон.

import { api } from '../api.js';
import { fmtNum, fmtDate, fmtDuration } from '../format.js';

let view = 'idle';     // idle | running | done
let pollHandle = null;
let lastResult = null; // GeneralBenchmarkReport
let mockMode = false;  // переключатель «посмотреть пример» — рисует MOCK
let hwInfo = null;     // реальное железо из /api/hardware (boot-диск + DRAM)

const MOCK = {
  id: '7c4f3a91-2b8e-4d5a-9f1c-8e6b2a1c4d5e',
  started_at: '2026-05-19T14:30:15+00:00',
  duration_sec: 92.4,
  score: 6840,
  dgemm_gflops: 487.3,
  stream_gb_s: 38.4,
  disk_seq_read_mb_s: 6420,
  disk_random_read_mb_s: 1240,
  disk_seq_write_mb_s: 5180,
  r_dgemm: 0.612,
  r_stream: 0.534,
  r_disk: 0.728,
  disk_media_label: 'NVMe',
  disk_model: 'Samsung 990 PRO 2TB',
  boot_drive_path: 'C:',
  disk_bus_type: 'PCIe 4.0',
};

export function render(host) {
  // Стартовое поведение: сразу проверяем status — может прогон уже идёт.
  renderHost(host);
  void syncStatus(host);
  void loadHardware();
}

export function dispose() {
  if (pollHandle) { clearInterval(pollHandle); pollHandle = null; }
}

// Реальный boot-диск из /api/hardware. В idle-превью раньше был mock
// (Samsung 990 PRO) — теперь подтягиваем фактическую модель/тип/букву.
function formatBootDisk() {
  if (mockMode) return `${MOCK.boot_drive_path} · ${MOCK.disk_model}`;
  const d = hwInfo && hwInfo.boot_disk;
  if (!d) return 'определяется…';
  const mount = d.mount || '';
  const model = d.model || 'Накопитель';
  const type = d.display_type ? ` · ${d.display_type}` : '';
  const size = d.size_gb ? ` · ${fmtNum(d.size_gb / 1024, 2)} ТБ` : '';
  return `${mount} · ${model}${type}${size}`.replace(/^ · /, '');
}

async function loadHardware() {
  if (hwInfo || mockMode) { updateBootDiskDom(); return; }
  try {
    hwInfo = await api.getHardware();
  } catch { /* оставим «определяется…» */ }
  updateBootDiskDom();
}

function updateBootDiskDom() {
  const el = document.getElementById('general-boot-disk');
  if (el) el.textContent = formatBootDisk();
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
    const s = await api.generalStatus();
    if (s.running) {
      view = 'running';
      renderHost(host);
      // Запускаем polling если ещё не запустили.
      if (!pollHandle) {
        pollHandle = setInterval(() => { void syncStatus(host); }, 1500);
      }
      updateRunningProgress(s);
    } else if (s.status === 'completed' && s.result_id) {
      // Прогон только что закончился — подгружаем детали и переходим в done.
      if (pollHandle) { clearInterval(pollHandle); pollHandle = null; }
      try { lastResult = await api.generalRun(s.result_id); }
      catch (err) { lastResult = { error: err.message }; }
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
    await api.generalStart();
    view = 'running';
    renderHost(host);
    if (!pollHandle) pollHandle = setInterval(() => { void syncStatus(host); }, 1500);
  } catch (err) {
    alert('Не удалось запустить: ' + err.message);
  }
}

// ─── IDLE ────────────────────────────────────────────────────────────

function renderIdle(host) {
  host.innerHTML = `
    <div class="general-screen">
      ${renderHeader('idle', null)}

      <div class="general-layout">
        <div class="card general-main">
          <div class="card__title">Общая оценка</div>

          <div class="general-intro">
            Один тест — три фазы — итоговый балл всей системы за ~1.5 минуты.
            Без термоконтроля и без длительной нагрузки: показывает на что
            железо <b>способно при нормальных условиях</b>.
          </div>

          <div class="general-phases">
            ${renderPhase('CPU',  'fp64 матричное умножение', '~30 сек')}
            ${renderPhase('RAM',  'пропускная способность (memory copy)', '~30 сек')}
            ${renderPhase('Диск', 'последовательное + случайное чтение/запись', '~30 сек')}
          </div>

          <div class="general-section-label">Параметры</div>
          <div class="general-params">
            <div class="param-field">
              <div class="param-field__label">Длительность фазы</div>
              <div class="param-field__value">30 сек</div>
            </div>
            <div class="param-field">
              <div class="param-field__label">Загрузочный диск</div>
              <div class="param-field__value" id="general-boot-disk">${escapeHtml(formatBootDisk())}</div>
            </div>
          </div>

          <div class="general-launch">
            <button class="btn primary lg general-launch__btn" id="btn-general-start">
              ▶ &nbsp; Запустить общую оценку
            </button>
            <div class="general-launch__hint">
              Один клик — оценит CPU, RAM и загрузочный диск за ~1.5 минуты.
              Не выключит компьютер, не нагрузит надолго.
            </div>
          </div>
        </div>

        <div class="general-side">
          <div class="card">
            <div class="card__title">Чем отличается от других тестов</div>
            <div class="general-explainer">
              <p><b>Общая оценка</b> — быстрая прикидка пиковой производительности всей системы.</p>
              <p>Если нужно <b>отдельно прогнать CPU</b> в частности
              (Single/Multi, точечные микробенчи) —
              <a href="#cpu-advanced">Расш. тест CPU</a>.</p>
              <p>Если нужно <b>протестировать отдельно память</b>
              (CPU L1/L2/L3 и DRAM) —
              <a href="#ramcache">Ram & Cache</a>.</p>
              <p>Если нужна <b>устойчивость под длительной нагрузкой</b> с
              термоконтролем (влияет непосредственно на итоговый балл) —
              <a href="#stress">Стресс-тест системы</a>.</p>
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
                Балл ×10 000 = среднее геометрическое трёх отношений
                к теоретическому пику архитектуры.
              </div>
            </div>
          </div>

          <button class="btn sm" id="btn-general-mock">
            Посмотреть пример результата →
          </button>
        </div>
      </div>
    </div>
  `;
  document.getElementById('btn-general-start')?.addEventListener('click', () => { void onStart(host); });
  document.getElementById('btn-general-mock')?.addEventListener('click', () => {
    mockMode = true;
    lastResult = { ...MOCK };
    view = 'done';
    renderHost(host);
  });
}

// ─── RUNNING ─────────────────────────────────────────────────────────

function renderRunning(host) {
  host.innerHTML = `
    <div class="general-screen">
      ${renderHeader('running', 'идёт оценка системы…')}

      <div class="card">
        <div class="card__title">Текущая фаза <span class="card__title-tag" id="general-phase-tag">…</span></div>
        <div class="progress" style="margin-top: var(--gap);">
          <div class="progress__fill" id="general-progress-fill" style="width: 0%;"></div>
        </div>
        <div class="rows" style="margin-top: var(--gap);" id="general-progress-rows">
          <div class="row"><span class="k">фаза</span><span class="v" id="general-phase">подготовка</span></div>
        </div>
      </div>

      <div style="margin-top: var(--gap-lg); text-align: center; color: var(--muted); font-size: 12px;">
        Полный прогон занимает примерно <b style="color: var(--text);">1.5 минуты</b>.
        Не закрывайте вкладку — результат сохранится автоматически.
      </div>
    </div>
  `;
}

function updateRunningProgress(status) {
  const phase = status.progress?.phase || 'подготовка';
  const idx = status.progress?.idx;
  const total = status.progress?.total;
  const tag = document.getElementById('general-phase-tag');
  const phaseEl = document.getElementById('general-phase');
  if (tag) tag.textContent = humanPhase(phase);
  if (phaseEl) phaseEl.textContent = humanPhase(phase);
  // idx/total приходят из оркестратора (general_benchmark.py: «фаза N из 5»).
  // Прогресс по середине текущей фазы, чтобы бар не прыгал на 0 в начале.
  const fillEl = document.getElementById('general-progress-fill');
  if (fillEl) {
    const pct = (typeof idx === 'number' && typeof total === 'number' && total > 0)
      ? ((idx - 0.5) / total) * 100
      : 5;
    fillEl.style.width = `${Math.max(5, Math.min(100, pct)).toFixed(1)}%`;
  }
}

function humanPhase(p) {
  return ({
    dgemm:            'CPU compute (fp64 matmul)',
    stream:           'RAM bandwidth (memory copy)',
    disk_seq_read:    'Диск · последовательное чтение',
    disk_random_read: 'Диск · случайное чтение',
    disk_seq_write:   'Диск · последовательная запись',
    cooldown:         'остывание между фазами',
  }[p]) || p;
}

// ─── DONE ────────────────────────────────────────────────────────────

function renderDone(host) {
  const r = lastResult;
  if (!r || r.error) {
    host.innerHTML = `
      <div class="general-screen">
        ${renderHeader('done', 'ошибка прогона', true)}
        <div class="banner danger"><b>Не удалось завершить прогон:</b> ${escapeHtml(r?.error || 'неизвестная ошибка')}</div>
      </div>
    `;
    document.getElementById('btn-general-back')?.addEventListener('click', () => {
      view = 'idle'; mockMode = false; lastResult = null; renderHost(host);
    });
    return;
  }
  const tone = colorTone(r.score);
  const verdict = r.score >= 6000 ? 'Отличный результат' : r.score >= 3000 ? 'Средний результат' : 'Низкий результат';
  const dur = r.duration_sec || (r.ended_at && r.started_at
    ? (new Date(r.ended_at) - new Date(r.started_at)) / 1000 : 90);
  const isMock = mockMode;

  host.innerHTML = `
    <div class="general-screen">
      ${renderHeader('done', `${isMock ? 'пример · ' : ''}прогон от ${fmtDate(r.started_at)} · ${fmtDuration(dur)}`, true)}

      <div class="general-result-banner card">
        <div class="general-result-banner__left">
          <div class="general-result-banner__value ${tone}">${formatScore(r.score)}</div>
          <div class="general-result-banner__meta">
            <div class="general-result-banner__label">ОБЩАЯ ОЦЕНКА</div>
            <div class="general-result-banner__hint">CPU · RAM · загрузочный диск</div>
            <span class="chip ${tone}">${escapeHtml(verdict)}</span>
          </div>
        </div>
        <div class="general-result-banner__sep"></div>
        <div class="general-result-banner__right">
          <div class="general-section-label">Больше — лучше · % от теоретического пика</div>
          <div class="general-subscores">
            ${renderSubscoreTile('CPU',  r.r_dgemm)}
            ${renderSubscoreTile('RAM',  r.r_stream)}
            ${renderSubscoreTile('Диск', r.r_disk)}
          </div>
          <div class="general-result-banner__legend">
            Шкала ×10 000 = среднее геометрическое трёх отношений к теоретическому
            пику архитектуры.
          </div>
        </div>
      </div>

      <div class="general-grid-two">
        <div class="card">
          <div class="card__title">CPU + RAM</div>
          <div class="rows">
            <div class="row"><span class="k">CPU compute</span><span class="v">${r.dgemm_gflops != null ? fmtNum(r.dgemm_gflops) + ' GFLOPS' : '—'}</span></div>
            <div class="row"><span class="k">↳ % от пика</span><span class="v">${formatPercent(r.r_dgemm)}</span></div>
            <div class="row"><span class="k">RAM bandwidth</span><span class="v">${r.stream_gb_s != null ? fmtNum(r.stream_gb_s) + ' ГБ/с' : '—'}</span></div>
            <div class="row"><span class="k">↳ % от пика</span><span class="v">${formatPercent(r.r_stream)}</span></div>
          </div>
        </div>

        <div class="card">
          <div class="card__title">Диск${r.disk_media_label ? ` ${escapeHtml(r.disk_media_label)}` : ''}${r.boot_drive_path ? ` · ${escapeHtml(r.boot_drive_path)}` : ''}</div>
          <div class="rows">
            ${r.disk_model ? `<div class="row"><span class="k">Модель</span><span class="v">${escapeHtml(r.disk_model)}</span></div>` : ''}
            ${r.disk_bus_type ? `<div class="row"><span class="k">Интерфейс</span><span class="v">${escapeHtml(r.disk_bus_type)}</span></div>` : ''}
            ${r.disk_seq_read_mb_s != null ? `<div class="row"><span class="k">Чтение (последовательное)</span><span class="v">${fmtNum(r.disk_seq_read_mb_s, 0)} МБ/с</span></div>` : ''}
            ${r.disk_random_read_mb_s != null ? `<div class="row"><span class="k">Чтение (случайное)</span><span class="v">${fmtNum(r.disk_random_read_mb_s, 0)} МБ/с</span></div>` : ''}
            ${r.disk_seq_write_mb_s != null ? `<div class="row"><span class="k">Запись (последовательная)</span><span class="v">${fmtNum(r.disk_seq_write_mb_s, 0)} МБ/с</span></div>` : ''}
            <div class="row"><span class="k">↳ % от пика</span><span class="v">${formatPercent(r.r_disk)}</span></div>
          </div>
        </div>
      </div>
    </div>
  `;
  document.getElementById('btn-general-back')?.addEventListener('click', () => {
    view = 'idle'; mockMode = false; lastResult = null; renderHost(host);
  });
  document.getElementById('btn-general-rerun')?.addEventListener('click', () => {
    if (mockMode) {
      mockMode = false; lastResult = null; view = 'idle'; renderHost(host);
      return;
    }
    void onStart(host);
  });
}

// ─── building blocks ─────────────────────────────────────────────────

function renderHeader(state, subtitle, isDone = false) {
  // Универсальный header (как в Sensors): большой белый h1 + серый mono индекс.
  // Дублирующая «готов готов к запуску» убрана — state-слово показываем только
  // в running/done, в idle главное — h1.
  const showState = state === 'running' || state === 'done';
  const stateLabel = state === 'running' ? 'в работе' : state === 'done' ? 'результат' : '';
  const stateColor = state === 'done' ? 'cool' : state === 'running' ? 'accent' : '';
  const action = isDone
    ? `<button class="btn sm" id="btn-general-back">← Назад</button>
       <button class="btn sm primary" id="btn-general-rerun">${mockMode ? 'К экрану запуска' : '↻ Запустить снова'}</button>`
    : `<button class="btn sm" disabled title="История появится в следующей версии">История</button>`;
  return `
    <div class="screen__header">
      <div class="screen__header__title">
        <h1>Общая оценка системы</h1>
        <span class="screen__header__index">04</span>
        ${showState ? `<span class="screen__header__state ${stateColor}">· ${escapeHtml(stateLabel)}${subtitle ? ' · ' + escapeHtml(subtitle) : ''}</span>` : ''}
      </div>
      <div class="screen__header__chips">
        <span class="screen__header__goal">цель · оценить систему целиком</span>
        <span class="screen__header__desc">CPU + RAM + загрузочный диск · ~1.5 мин · без термоконтроля</span>
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

function renderSubscoreTile(label, ratio) {
  if (ratio == null) {
    return `<div class="subscore-tile">
      <div class="subscore-tile__label">${escapeHtml(label)}</div>
      <div class="subscore-tile__value" style="color: var(--muted);">—</div>
    </div>`;
  }
  const pct = ratio * 100;
  const tone = ratio >= 0.6 ? 'cool' : ratio >= 0.3 ? 'warm' : '';
  return `<div class="subscore-tile">
    <div class="subscore-tile__label">${escapeHtml(label)}</div>
    <div class="subscore-tile__value ${tone}">${fmtNum(pct, 1)}%</div>
    <div class="subscore-tile__bar">
      <div class="subscore-tile__bar-fill ${tone}" style="width: ${Math.min(100, pct).toFixed(1)}%;"></div>
    </div>
  </div>`;
}

function colorTone(score) {
  // Без агрессивного красного: низкий балл — нейтральный, средний/хороший —
  // зелёный. Пользователь со слабым ПК не должен видеть «тревожный» красный.
  if (score == null) return '';
  if (score >= 3000) return 'cool';
  return '';
}

function formatScore(score) {
  // Округляем до целого — «1 792», а не «1 792,584» (лишние знаки не нужны).
  return score != null ? Math.round(score).toLocaleString('ru-RU') : '—';
}

function formatPercent(ratio) {
  return ratio != null ? `${fmtNum(ratio * 100, 1)}%` : '—';
}

function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[c]);
}
