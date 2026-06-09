// Dashboard — главный экран.
//
// 4 карточки:
//   1) Последний прогон   (GET /api/runs?limit=1)
//   2) Здоровье системы   (live из /ws/metrics)
//   3) Быстрый запуск     (3 кнопки → переход в Stress / General-stub / CPU-stub)
//   4) Тренд баллов       (GET /api/trend)

import { api } from '../api.js';
import { state, subscribe } from '../store.js';
import { fmtNum, fmtInt, fmtDate, fmtDuration, colorForCpuTemp, colorForGpuTemp } from '../format.js';
import { renderSparkline, renderLineChart } from '../components/sparkline.js';

let unsubscribe = null;
let lastRunCache = null;
let trendCache = null;

export async function render(host) {
  host.innerHTML = `
    <div class="screen">
      <div class="screen__header">
        <div class="screen__header__title">
          <h1>Dashboard</h1>
          <span class="screen__header__index">01</span>
        </div>
        <div class="screen__header__sub">
          Общий обзор: последний прогон, здоровье системы, быстрый запуск, тренд баллов.
        </div>
      </div>
      <div class="dashboard-grid">
        <div class="card" id="card-last-run">
          <div class="card__title">Последний прогон</div>
          <div id="last-run-body"><div class="last-run__empty">загрузка…</div></div>
        </div>
        <div class="card" id="card-health">
          <div class="card__title">Здоровье системы <span class="card__title-tag">live</span></div>
          <div class="kpi-grid">
            <div class="kpi">
              <div class="kpi__label">CPU</div>
              <div class="kpi__value" id="dh-cpu">—</div>
              <div class="kpi__sub" id="dh-cpu-sub">загрузка</div>
              <div class="kpi__spark" id="spark-dh-cpu"></div>
            </div>
            <div class="kpi">
              <div class="kpi__label">RAM</div>
              <div class="kpi__value" id="dh-ram">—</div>
              <div class="kpi__sub" id="dh-ram-sub">использовано</div>
              <div class="kpi__spark" id="spark-dh-ram"></div>
            </div>
            <div class="kpi">
              <div class="kpi__label">T° CPU</div>
              <div class="kpi__value" id="dh-tcpu">—</div>
              <div class="kpi__sub">°C</div>
              <div class="kpi__spark" id="spark-dh-tcpu"></div>
            </div>
            <div class="kpi">
              <div class="kpi__label">T° GPU</div>
              <div class="kpi__value" id="dh-tgpu">—</div>
              <div class="kpi__sub">°C</div>
              <div class="kpi__spark" id="spark-dh-tgpu"></div>
            </div>
          </div>
        </div>
        <div class="card" id="card-quick">
          <div class="card__title">Быстрый запуск</div>
          <div class="quick-runs">
            <button class="quick-run-btn" data-target="#stress"><b>Стресс-тест</b>
              <span>длительная нагрузка ЦП+RAM, термоконтроль · 10-60 мин</span></button>
            <button class="quick-run-btn" data-target="#general"><b>Общая оценка</b>
              <span>быстрый CPU+RAM+disk, без термоконтроля · ~1.5 мин</span></button>
            <button class="quick-run-btn" data-target="#cpu-advanced"><b>Расш. тест CPU</b>
              <span>Single/Multi + рейтинг · CPU-only</span></button>
            <button class="quick-run-btn" data-target="#ramcache"><b>Ram & Cache</b>
              <span>таблица 4×4 · диагностика памяти</span></button>
          </div>
        </div>
        <div class="card" id="card-trend">
          <div class="card__title">Тренд баллов</div>
          <div class="trend-chart" id="trend-chart"></div>
        </div>
      </div>
    </div>
  `;

  // Quick-run кнопки.
  for (const btn of host.querySelectorAll('.quick-run-btn')) {
    btn.addEventListener('click', () => {
      window.location.hash = btn.dataset.target;
    });
  }

  // Подписка на live телеметрию.
  unsubscribe = subscribe((ev) => {
    if (ev.type === 'snapshot') updateHealth();
  });
  updateHealth();

  // Параллельно тянем последний прогон и тренд.
  void loadLastRun();
  void loadTrend();
}

export function dispose() {
  if (unsubscribe) { unsubscribe(); unsubscribe = null; }
}

function updateHealth() {
  const snap = state.lastSnap;
  if (!snap) return;

  // CPU%
  setText('dh-cpu', `${fmtNum(snap.cpu_percent)}%`);
  setText('dh-cpu-sub', `ядер: ${snap.cpu_per_core_percent?.length || 0}`);
  renderSparkline(byId('spark-dh-cpu'), state.live.cpu, { color: 'var(--accent)' });

  // RAM
  setText('dh-ram', `${fmtNum(snap.ram_percent)}%`);
  setText('dh-ram-sub', `${fmtNum(snap.ram_used_gb, 2)} ГБ`);
  renderSparkline(byId('spark-dh-ram'), state.live.ram, { color: 'var(--warn)' });

  // T° CPU
  const tcpuLast = state.live.tcpu[state.live.tcpu.length - 1];
  const tcpuEl = byId('dh-tcpu');
  tcpuEl.textContent = fmtNum(tcpuLast);
  setColorClass(tcpuEl, colorForCpuTemp(tcpuLast));
  renderSparkline(byId('spark-dh-tcpu'), state.live.tcpu, { color: 'var(--hot)' });

  // T° GPU
  const tgpuLast = state.live.tgpu[state.live.tgpu.length - 1];
  const tgpuEl = byId('dh-tgpu');
  tgpuEl.textContent = fmtNum(tgpuLast);
  setColorClass(tgpuEl, colorForGpuTemp(tgpuLast));
  renderSparkline(byId('spark-dh-tgpu'), state.live.tgpu, { color: 'var(--ok)' });
}

async function loadLastRun() {
  try {
    const runs = await api.listRuns(1);
    lastRunCache = runs?.[0] || null;
  } catch (err) {
    lastRunCache = { error: err.message };
  }
  renderLastRun();
}

function renderLastRun() {
  const body = byId('last-run-body');
  if (!body) return;
  if (!lastRunCache) {
    renderEmptyState(body);
    return;
  }
  if (lastRunCache.error) {
    body.innerHTML = `<div class="last-run__empty" style="color:var(--danger)">Ошибка: ${lastRunCache.error}</div>`;
    return;
  }
  const run = lastRunCache;
  const dur = (new Date(run.end_time) - new Date(run.start_time)) / 1000;
  // Маппинг raw profile_name → человеческое имя теста (как в Истории).
  const PROFILE_LABEL = {
    timed_stress:    'Стресс-тест',
    infinite_stress: 'Стресс-тест (бесконечный)',
    cpu_heavy:       'Стресс-тест',
    mixed_quick:     'Стресс-тест',
  };
  const humanProfile = PROFILE_LABEL[run.profile_name] || 'Стресс-тест';
  const STATUS_RU = {
    completed: 'завершён',
    cancelled: 'отменён',
    failed:    'упал',
    running:   'идёт',
  };
  const statusLabel = STATUS_RU[run.status] || run.status;
  body.innerHTML = `
    <div class="last-run__main">
      <div class="chip ${run.status === 'completed' ? 'ok' : 'warn'}">${escapeHtml(statusLabel)}</div>
      <span style="color: var(--muted); font-size: 12px;">${escapeHtml(humanProfile)}</span>
    </div>
    <div class="rows last-run__rows">
      <div class="row"><span class="k">дата</span><span class="v">${fmtDate(run.start_time)}</span></div>
      <div class="row"><span class="k">длительность</span><span class="v">${fmtDuration(dur)}</span></div>
      <div class="row"><span class="k">отсчётов телеметрии</span><span class="v">${fmtInt(run.samples)}</span></div>
      <div class="row"><span class="k">идентификатор</span><span class="v" style="font-size:11px;color:var(--muted)">${escapeHtml((run.id || '').slice(0, 8))}…</span></div>
    </div>
  `;
}

// Empty state по образцу console-dashboard.jsx → ConsoleDashboardEmpty.
// Появляется когда у пользователя нет ни одного прогона в БД.
function renderEmptyState(host) {
  host.innerHTML = `
    <div class="empty-hero">
      <img src="/static/assets/apex-logo.png" alt="" class="empty-hero__icon" />
      <div class="empty-hero__text">
        <div class="empty-hero__tag">// ЕЩЁ НЕТ ПРОГОНОВ</div>
        <div class="empty-hero__title">запустите ваш первый тест.</div>
        <div class="empty-hero__body">
          Живая телеметрия уже идёт — все аппаратные источники в норме.
          Выберите тип теста и нажмите запуск — история сохранится
          автоматически.
        </div>
      </div>
    </div>
    <div class="empty-actions">
      <a class="empty-action" href="#stress">
        <div class="empty-action__head">
          <span class="empty-action__label">▶ Стресс-тест</span>
          <span class="empty-action__hint">10 мин · CPU + RAM с термоконтролем</span>
        </div>
        <span class="empty-action__cmd">→ перейти в раздел</span>
      </a>
      <a class="empty-action primary" href="#general">
        <div class="empty-action__head">
          <span class="empty-action__label">▶ Общая оценка системы</span>
          <span class="empty-action__hint">~1.5 мин · CPU + RAM + диск</span>
        </div>
        <span class="empty-action__cmd">→ перейти в раздел</span>
      </a>
      <a class="empty-action" href="#cpu-advanced">
        <div class="empty-action__head">
          <span class="empty-action__label">▶ Расш. тест CPU</span>
          <span class="empty-action__hint">CPU-only · Single/Multi + рейтинг</span>
        </div>
        <span class="empty-action__cmd">→ перейти в раздел</span>
      </a>
    </div>
  `;
}

async function loadTrend() {
  try {
    trendCache = await api.getTrend('final_score', { last: 30, window: 5 });
  } catch (err) {
    trendCache = { error: err.message };
  }
  renderTrend();
}

function renderTrend() {
  const host = byId('trend-chart');
  if (!host) return;
  if (!trendCache || trendCache.error) {
    host.innerHTML = `<div class="trend-empty">
      <div class="trend-empty__icon">📊</div>
      <div class="trend-empty__title">Нет данных тренда</div>
      <div class="trend-empty__body">
        ${trendCache?.error ? 'Ошибка: ' + escapeHtml(trendCache.error) : 'История прогонов пуста.'}
      </div>
    </div>`;
    return;
  }
  // Все значения = 0.0 → это legacy final_score (всегда 0 в scoring v2).
  // Реальный балл живёт в micro_runs.overall (§9.3 pending).
  const vals = (trendCache.values || []).filter(v => typeof v === 'number');
  const allZero = vals.length > 0 && vals.every(v => v === 0);
  const allSame = vals.length > 1 && vals.every(v => v === vals[0]);
  if (allZero || allSame) {
    host.innerHTML = `<div class="trend-empty">
      <div class="trend-empty__icon">📊</div>
      <div class="trend-empty__title">${allZero ? 'нет накопленной истории баллов' : 'нет вариаций'}</div>
      <div class="trend-empty__body">
        Тренд считается по прогонам из раздела
        <a href="#cpu-advanced">Расш. тест процессора</a>. Когда там накопится
        несколько результатов, график покажет динамику изменения балла.
      </div>
      <div class="trend-empty__count">${vals.length} ${pluralRu(vals.length, ['прогон', 'прогона', 'прогонов'])} в выборке</div>
    </div>`;
    return;
  }
  const labels = (trendCache.timestamps || []).map(t => new Date(t).toLocaleDateString('ru-RU'));
  renderLineChart(host, [
    { values: trendCache.values || [], color: 'var(--accent)', label: 'балл' },
    { values: trendCache.rolling_mean || [], color: 'var(--warn)', label: 'rolling μ' },
    { values: trendCache.rolling_p95 || [], color: 'var(--danger)', label: 'rolling p95' },
  ], { xLabels: labels });
}

function pluralRu(n, forms) {
  const r = Math.abs(n) % 100;
  if (r >= 11 && r <= 14) return forms[2];
  const r1 = r % 10;
  if (r1 === 1) return forms[0];
  if (r1 >= 2 && r1 <= 4) return forms[1];
  return forms[2];
}

function setColorClass(el, color) {
  el.classList.remove('hot', 'warm', 'cool');
  if (color === 'ok')    el.classList.add('cool');
  if (color === 'warn')  el.classList.add('warm');
  if (color === 'danger') el.classList.add('hot');
}

function byId(id) { return document.getElementById(id); }
function setText(id, text) { const el = byId(id); if (el) el.textContent = text; }
function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[c]);
}
