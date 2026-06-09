// Стресс-тест системы — длительная нагрузка ЦП+RAM с термоконтролем.
//
// Воссоздан pixel-close по console-stress.jsx (handoff bundle) — idle/running/done.
// Дополнительно добавлено: custom duration input + «Бесконечный стресс-тест»
// (большой `duration_sec` через POST /api/bench/start, см. PROJECT_CONTEXT §5.11).

import { api } from '../api.js';
import { state, subscribe } from '../store.js';
import {
  fmtNum, fmtInt, fmtDuration,
  colorForCpuTemp, colorForStressScore,
} from '../format.js';
import { renderSparkline } from '../components/sparkline.js';

let unsubscribe = null;
let pollHandle = null;
let lastResultId = null;
let lastResultCache = null;
let selectedDuration = 10;   // в минутах (общее время прогона, не per-engine)
let customDuration = null;   // отдельно, если пользователь ввёл руками
let infinityMode = false;
let cancelRequested = false;

// "Бесконечный" — backend не имеет нативного infinite-флага, поэтому посылаем
// очень большой duration (24 часа = 86400с) и явно сообщаем пользователю что
// прервать можно только закрытием webui (см. NOTES блок и note "cancel").
const INFINITY_DURATION_SEC = 86400;

// Backend (ParallelStressRunner) грузит CPU+RAM ОДНОВРЕМЕННО на всё указанное
// время — пользователь вводит длительность прогона, и она целиком уходит в
// duration_sec (раньше web гонял движки последовательно и делил время на 2).

const PRESETS = [
  { min: 5,  label: '5 мин',  hint: 'короткий замер' },
  { min: 10, label: '10 мин', hint: 'стандартный soak' },
  { min: 30, label: '30 мин', hint: 'тепловое равновесие' },
  { min: 60, label: '60 мин', hint: 'долгая стабильность' },
];

export async function render(host) {
  host.innerHTML = `
    <div class="screen stress-screen">
      <div class="screen__header">
        <div class="screen__header__title">
          <h1>Стресс-тест системы</h1>
          <span class="screen__header__index">03</span>
          <span class="screen__header__state" id="stress-state-inline"></span>
        </div>
        <div class="screen__header__chips">
          <span class="screen__header__goal screen__header__goal--amber">цель · сколько выдержит</span>
          <span class="screen__header__desc">длительная нагрузка ЦП + RAM с термоконтролем · 5 мин – бесконечно</span>
        </div>
      </div>

      <div class="stress-layout">
        <!-- LEFT panel: hero + controls + launch -->
        <div class="card stress-main">
          <div class="card__title">
            <span>СТРЕСС-ТЕСТ СИСТЕМЫ</span>
            <span class="card__title-tag" id="stress-endpoint">профиль · CPU heavy</span>
          </div>

          <div class="stress-hero">
            <img src="/static/assets/apex-logo.png" alt="" class="stress-hero__icon" />
            <div>
              <div class="stress-hero__tag">// ГОТОВ К ЗАПУСКУ</div>
              <div class="stress-hero__title">запустите стресс-тест системы.</div>
              <div class="stress-hero__body">
                Комбинированная нагрузка <b>CPU + RAM параллельно</b> с термальным
                контролем (TJmax) и расчётом стабильности частот. На выходе —
                композитный балл <b>«Оценка под нагрузкой»</b> и подскоры
                по CPU, RAM, стабильности и тепловому запасу.
              </div>
            </div>
          </div>

          <div class="stress-section">
            <div class="stress-section__head">
              <span class="stress-section__label">// ДЛИТЕЛЬНОСТЬ (общее время прогона)</span>
              <span class="stress-section__hint">по умолчанию — 10 мин</span>
            </div>
            <div class="stress-presets" id="stress-presets">
              ${PRESETS.map(p => renderPreset(p)).join('')}
            </div>
            <div class="stress-custom" id="stress-custom-row">
              <label class="stress-custom__label">или введите свою длительность:</label>
              <input type="number" id="stress-custom" min="0.1" step="0.1" max="1440" placeholder="напр. 15" class="stress-custom__input" />
              <span class="stress-section__hint">мин (0.1-1440)</span>
              <button class="btn sm" id="btn-stress-infinity">∞ Бесконечный</button>
            </div>
          </div>

          <div class="stress-section">
            <div class="stress-section__head">
              <span class="stress-section__label">// ПОТОКИ</span>
              <span class="stress-section__hint" id="stress-threads-hint">0 — авто (все логические)</span>
            </div>
            <div class="stress-threads">
              <input type="number" id="stress-threads" min="0" max="256" value="0" />
              <span style="color: var(--muted); font-size: 11px;">
                По умолчанию — все потоки CPU. Ограничьте, чтобы сравнить с другой топологией.
              </span>
            </div>
          </div>

          <button class="stress-launch" id="btn-stress-start">
            <span class="stress-launch__icon">▶</span>
            <div class="stress-launch__body">
              <span class="stress-launch__main" id="btn-stress-start-main">Запустить стресс-тест · 10 мин</span>
            </div>
          </button>

          <div class="stress-live" id="stress-live"></div>
        </div>

        <!-- RIGHT column: last + system + notes -->
        <div class="stress-side">
          <div class="card">
            <div class="card__title">
              <span>LAST STRESS</span>
              <span class="card__title-tag" id="last-stress-tag">// нет прогонов</span>
            </div>
            <div id="last-stress-body">
              <div style="color: var(--muted); font-style: italic; padding: var(--gap) 0;">
                ещё не было запусков
              </div>
            </div>
          </div>

          <div class="card">
            <div class="card__title">
              <span>СИСТЕМА</span>
            </div>
            <div id="system-preview" class="rows"></div>
          </div>

          <div class="card stress-notes">
            <div class="card__title">// ЗАМЕТКИ</div>
            <div class="stress-note">
              <span class="stress-note__tag">тип нагрузки</span>
              <span><b>CPU + RAM одновременно</b> весь прогон:
              <b>CPU</b> — большое матричное умножение (DGEMM, FMA/AVX + кэш),
              <b>RAM</b> — потоковая пропускная способность (STREAM).
              Параллельно, как в консольном «Стресс-тесте» — отсюда реальный
              прогрев.</span>
            </div>
            <div class="stress-note">
              <span class="stress-note__tag warn">отмена</span>
              <span>Кнопка «Отменить тест» появляется в прогресс-карте после
              запуска. Реальный stop через ≤ 1-2 сек на ближайшем cancel-tick
              стресс-движка. Результат сохраняется со статусом «cancelled».</span>
            </div>
            <div class="stress-note">
              <span class="stress-note__tag">тепловой запас</span>
              <span>Если температура CPU недоступна — балл «Оценка под нагрузкой»
              не считается. Стабильность частот и пропускная способность
              при этом остаются валидными.</span>
            </div>
            <div class="stress-note">
              <span class="stress-note__tag">сторож по TJmax</span>
              <span>При достижении предельной температуры (TJmax) тест мягко
              завершается за 60 с, результат сохраняется как «частичный».</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  `;

  // ─── Bind: preset cards ─────────────────────────────────────────────
  for (const btn of host.querySelectorAll('.stress-preset')) {
    btn.addEventListener('click', () => {
      const min = parseInt(btn.dataset.min, 10);
      selectedDuration = min;
      customDuration = null;
      infinityMode = false;
      document.getElementById('stress-custom').value = '';
      refreshSelected();
    });
  }

  // ─── Bind: custom input ─────────────────────────────────────────────
  // Принимаем дробные минуты (0.1-1440) — позволяет короткие dev-тесты
  // вроде 0.5 мин (30 сек). Бэкенд /api/bench/start уже умеет float.
  document.getElementById('stress-custom').addEventListener('input', (e) => {
    const v = parseFloat(e.target.value);
    if (Number.isFinite(v) && v >= 0.1 && v <= 1440) {
      customDuration = v;
      infinityMode = false;
      selectedDuration = null;
    } else {
      customDuration = null;
    }
    refreshSelected();
  });

  // ─── Bind: ∞ Бесконечный ────────────────────────────────────────────
  document.getElementById('btn-stress-infinity').addEventListener('click', () => {
    infinityMode = !infinityMode;
    if (infinityMode) {
      selectedDuration = null;
      customDuration = null;
      document.getElementById('stress-custom').value = '';
    } else {
      selectedDuration = 10;
    }
    refreshSelected();
  });

  // ─── Bind: launch ───────────────────────────────────────────────────
  document.getElementById('btn-stress-start').addEventListener('click', onStart);

  refreshSelected();

  // ─── Live subs ───────────────────────────────────────────────────────
  unsubscribe = subscribe((ev) => {
    if (ev.type === 'system') renderSystemPreview();
    if (ev.type === 'bench') refreshBenchStatus();
  });
  renderSystemPreview();

  pollHandle = setInterval(() => { void pollStatus(); }, 2000);
  void pollStatus();
}

export function dispose() {
  if (unsubscribe) { unsubscribe(); unsubscribe = null; }
  if (pollHandle) { clearInterval(pollHandle); pollHandle = null; }
}

function renderPreset(p) {
  return `<div class="stress-preset" data-min="${p.min}" data-default="${p.min === 10 ? '1' : '0'}">
    <div class="stress-preset__sel">SEL</div>
    <div class="stress-preset__label">${p.label}</div>
    <div class="stress-preset__hint">${p.hint}</div>
  </div>`;
}

function refreshSelected() {
  // SEL ribbon на выбранной карточке.
  for (const card of document.querySelectorAll('.stress-preset')) {
    const min = parseInt(card.dataset.min, 10);
    card.classList.toggle('active', min === selectedDuration && !infinityMode && customDuration == null);
  }
  // ∞-toggle visual.
  const infBtn = document.getElementById('btn-stress-infinity');
  if (infBtn) {
    infBtn.classList.toggle('active', infinityMode);
    infBtn.textContent = infinityMode ? '∞ Бесконечный · ON' : '∞ Бесконечный';
  }

  // Текст launch-кнопки.
  const { sec, label } = computeDuration();
  const mainEl = document.getElementById('btn-stress-start-main');
  if (mainEl) mainEl.textContent = `Запустить стресс-тест · ${label}`;
}

function computeDuration() {
  if (infinityMode) {
    return { sec: INFINITY_DURATION_SEC, label: '∞ бесконечно' };
  }
  // CPU+RAM грузятся ОДНОВРЕМЕННО на всё указанное время — делить на движки
  // больше не нужно (parallel-нагрузка, паритет с CLI «Стресс-тест»).
  const totalMin = customDuration ?? selectedDuration ?? 10;
  const sec = totalMin * 60;
  const fmtMin = (m) => Number.isInteger(m) ? String(m) : m.toFixed(1).replace(/\.0$/, '');
  return { sec, label: `${fmtMin(totalMin)} мин` };
}

function renderSystemPreview() {
  const sys = state.system;
  const host = document.getElementById('system-preview');
  if (!host || !sys) return;
  const cpu = sys.cpu_cores;
  const coreStr = (cpu?.p_cores != null && cpu?.e_cores != null)
    ? `${cpu.p_cores}P + ${cpu.e_cores}E / ${cpu.logical}T` : `${cpu?.physical}/${cpu?.logical}`;
  host.innerHTML = [
    ['процессор', sys.cpu_model],
    ['ядра / потоки', coreStr],
    ['базовая частота', sys.cpu_base_mhz ? fmtNum(sys.cpu_base_mhz / 1000, 2) + ' ГГц' : '—'],
    ['ОЗУ', `${fmtNum(sys.ram_total_gb, 1)} ГБ`],
    ['видеокарта', (sys.gpu_list && sys.gpu_list[0]) || '—'],
    ['ОС', `${sys.os_name} ${sys.os_version || ''}`],
    ['имя хоста', sys.hostname || '—'],
  ].map(([k, v]) => `<div class="row"><span class="k">${k}</span><span class="v">${escapeHtml(v)}</span></div>`).join('');

  const hint = document.getElementById('stress-threads-hint');
  if (hint) hint.textContent = `0 — авто (всего ${cpu?.logical ?? '—'} потоков)`;
}

async function onStart() {
  const btn = document.getElementById('btn-stress-start');
  btn.disabled = true;
  cancelRequested = false;     // новый прогон — сбросить cancel-флаг
  const { sec } = computeDuration();
  const threads = parseInt(document.getElementById('stress-threads').value, 10) || 0;
  try {
    await api.benchStart({
      profile: 'cpu_heavy',
      duration_sec: sec,
      rate_sec: 0.5,
      threads,
    });
    await pollStatus();
  } catch (err) {
    alert('Не удалось запустить: ' + err.message);
    btn.disabled = false;
  }
}

async function pollStatus() {
  try {
    const s = await api.benchStatus();
    state.benchStatus = s;
    refreshBenchStatus();
    if (s.status === 'completed' && s.result_id && s.result_id !== lastResultId) {
      lastResultId = s.result_id;
      try { lastResultCache = await api.getRun(s.result_id); }
      catch (err) { lastResultCache = { error: err.message }; }
      renderLastStress();
    } else if (!s.running && !lastResultId && lastResultCache == null) {
      // Fallback: BenchController хранит result_id только in-memory, после
      // рестарта apexcore webui он null. Чтобы пользователь после рестарта
      // видел свой прошлый прогон, подтягиваем последний завершённый
      // stress-прогон из таблицы `runs` (там лежат все cpu_heavy-прогоны).
      try {
        const runs = await api.listRuns(1);
        if (runs && runs.length > 0 && runs[0].status === 'completed') {
          lastResultId = runs[0].id;
          lastResultCache = await api.getRun(lastResultId);
          renderLastStress();
        }
      } catch { /* history fetch failed — ok, просто не подтянем */ }
    }
  } catch { /* poll silently retries */ }
}

function refreshBenchStatus() {
  const s = state.benchStatus;
  const btn = document.getElementById('btn-stress-start');
  const live = document.getElementById('stress-live');
  const stateEl = document.getElementById('stress-state-inline');
  if (!btn || !live) return;

  if (s?.running) {
    btn.disabled = true;
    btn.classList.add('running');
    if (stateEl) {
      stateEl.textContent = cancelRequested
        ? '· отмена запрошена…'
        : '· в работе · soak in progress';
      stateEl.className = 'screen__header__state ' + (cancelRequested ? 'warm' : 'accent');
    }
    // Math.max(0, …) — защита от clock-skew между сервером и браузером
    // (webui на машине с RTC в local-time → started_at в UTC опережает
    // Date.now() браузера на TZ-offset, давая отрицательный elapsed).
    // started_at приходит как UTC ISO (+00:00), new Date парсит корректно;
    // guard страхует от рассинхрона часов.
    const elapsed = s.started_at ? Math.max(0, (Date.now() - new Date(s.started_at).getTime()) / 1000) : 0;
    const cancelHint = cancelRequested
      ? '⏹ отмена запрошена, ждём ближайший cancel-token tick (≤ 1-2 сек)'
      : 'кнопка ниже шлёт cancel-сигнал, реальный stop через ≤ 1-2 сек';
    const cancelBtnAttrs = cancelRequested
      ? 'disabled style="opacity:0.6;cursor:not-allowed;"'
      : '';
    const cancelBtnText = cancelRequested
      ? '⏹ отмена запрошена…'
      : '⏹ Отменить тест';
    live.innerHTML = `
      <div class="card" style="margin-top: var(--gap-lg); background: var(--panel-2);">
        <div class="card__title">ПРОГРЕСС</div>
        <div class="progress"><div class="progress__fill" style="width: 8%;"></div></div>
        <div class="rows" style="margin-top: var(--gap-sm);">
          <div class="row"><span class="k">прошло</span><span class="v">${fmtDuration(elapsed)}</span></div>
          <div class="row"><span class="k">отмена</span><span class="v" style="color: var(--muted)">${cancelHint}</span></div>
        </div>
        <button class="btn ghost" id="btn-stress-cancel" ${cancelBtnAttrs}
                style="margin-top: var(--gap); width: 100%; color: var(--warn); border-color: var(--warn);">
          ${cancelBtnText}
        </button>
      </div>
    `;
    btn.disabled = true;
    // Bind cancel-кнопки (live-card перерисовывается каждый poll, listener
    // надо привязывать заново после innerHTML).
    const cancelBtn = document.getElementById('btn-stress-cancel');
    if (cancelBtn && !cancelRequested) {
      cancelBtn.addEventListener('click', onCancel);
    }
  } else if (s?.status === 'failed') {
    if (stateEl) {
      stateEl.textContent = '· прогон упал';
      stateEl.className = 'screen__header__state hot';
    }
    btn.disabled = false;
    btn.classList.remove('running');
    live.innerHTML = `<div class="banner danger" style="margin-top: var(--gap-lg);">
      <b>Прогон упал.</b> ${escapeHtml(s.error || 'неизвестная ошибка')}</div>`;
  } else {
    if (stateEl) {
      if (s?.status === 'cancelled') {
        stateEl.textContent = '· отменён';
        stateEl.className = 'screen__header__state warm';
      } else if (s?.status === 'completed') {
        stateEl.textContent = '· завершён';
        stateEl.className = 'screen__header__state cool';
      } else {
        stateEl.textContent = '';
        stateEl.className = 'screen__header__state';
      }
    }
    btn.disabled = false;
    btn.classList.remove('running');
    live.innerHTML = '';
    // Прогон завершился (любым путём — completed/failed/cancelled) →
    // сбрасываем cancel-флаг, чтобы следующий запуск стартовал без
    // pre-set "отмена запрошена".
    cancelRequested = false;
  }
}

async function onCancel() {
  cancelRequested = true;
  // Перерисуем live-card сразу чтобы пользователь увидел реакцию,
  // не дожидаясь следующего poll-тика (2 сек).
  refreshBenchStatus();
  try {
    await api.benchStop();
  } catch (err) {
    cancelRequested = false;
    refreshBenchStatus();
    alert('Не удалось отправить cancel-сигнал: ' + err.message);
  }
}

function renderLastStress() {
  const body = document.getElementById('last-stress-body');
  const tag = document.getElementById('last-stress-tag');
  if (!body || !lastResultCache) return;
  if (lastResultCache.error) {
    body.innerHTML = `<div style="color: var(--danger)">Ошибка: ${escapeHtml(lastResultCache.error)}</div>`;
    return;
  }
  const r = lastResultCache;
  const thermal = r.thermal || {};
  const dur = (new Date(r.end_time) - new Date(r.start_time)) / 1000;
  // `final_score` в payload — это stress_score × 10 000, посчитанный
  // в `GET /api/runs/{id}` через lazy compute из `compute_stress_score_context`
  // (см. server.py: ветка `if result.thermal is not None and result.stress_results`).
  // Если в БД остался legacy 0.0 и lazy compute не сработал (нет thermal /
  // нет stress_results / r_thermal=None потому что CPU temp недоступна) —
  // payload приедет с `final_score=0`, и мы покажем «—» с пояснением через
  // `tooShort` или общий «нет данных».
  const RELIABLE_DURATION_SEC = 90;
  const tooShort = dur < RELIABLE_DURATION_SEC;
  const cancelled = r.status === 'cancelled';
  const stressScore = r.final_score;
  const hasScore = typeof stressScore === 'number' && stressScore > 0;
  const scoreCls = hasScore ? colorForStressScore(stressScore) : 'dim';

  if (tag) tag.textContent = `прогон · ${(r.id || '').slice(0, 8)}`;
  // Warning-плашка для коротких / cancelled прогонов: объясняем почему
  // показано «—» вместо балла, чтобы пользователь не путался.
  let warnBanner = '';
  if (cancelled) {
    warnBanner = `<div class="banner warn" style="margin-bottom: var(--gap-sm); font-size: 11px;">
      Прогон <b>отменён</b> на ${fmtNum(dur, 1)} сек — стресс-балл не считается,
      thermal stability не успела собраться.
    </div>`;
  } else if (tooShort) {
    warnBanner = `<div class="banner warn" style="margin-bottom: var(--gap-sm); font-size: 11px;">
      Прогон <b>${fmtNum(dur, 1)} сек</b> — слишком короткий для надёжного
      балла «Оценка под нагрузкой» (минимум ${RELIABLE_DURATION_SEC} сек = 1.5 мин).
      Для оценки thermal stability рекомендуется ≥ 10 мин общего стресса.
    </div>`;
  }
  body.innerHTML = `
    ${warnBanner}
    <div class="last-stress__score ${scoreCls}">${hasScore ? fmtInt(stressScore) : '—'}</div>
    <div style="color: var(--muted); font-size: 11px; margin-top: 2px;">оценка под нагрузкой</div>
    <div style="display: flex; gap: 6px; margin: var(--gap-sm) 0; flex-wrap: wrap;">
      ${thermal.throttle_observed ? '<span class="chip warn">⚑ throttling замечен</span>' : ''}
      ${cancelled ? '<span class="chip warn">cancelled</span>' : ''}
    </div>
    <div class="rows">
      <div class="row"><span class="k">время тестирования</span><span class="v">${fmtDuration(dur)}</span></div>
      <div class="row"><span class="k">средняя температура</span>
        <span class="v ${rowClass(colorForCpuTemp(thermal.temp_avg_c))}">${thermal.temp_avg_c != null ? fmtNum(thermal.temp_avg_c) + ' °C' : '—'}</span></div>
      <div class="row"><span class="k">пиковая температура</span>
        <span class="v ${rowClass(colorForCpuTemp(thermal.temp_max_c))}">${thermal.temp_max_c != null ? fmtNum(thermal.temp_max_c) + ' °C' : '—'}</span></div>
    </div>
  `;
}

function rowClass(c) { return c === 'ok' ? 'cool' : c === 'warn' ? 'warm' : c === 'danger' ? 'hot' : ''; }

function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[c]);
}
