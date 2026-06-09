// История тестов — список из таблицы runs.
//
// Фильтры:
//   - тип теста: 5 человеческих категорий, маппинг через profileToCategory()
//     (timed_stress/infinite_stress/cpu_heavy → «Стресс-тест», micro_* →
//     «Расш. тест CPU», ram_cache → «Ram & Cache», winsat → «Наследие Winsat»,
//     general_benchmark → «Общая оценка системы»);
//   - лимит: 5 / 10 / 20 / 50.
// Export / Delete — заглушки до реализации (кнопки disabled).

import { api } from '../api.js';
import { fmtInt, fmtDate, fmtDuration, shortUuid } from '../format.js';

let allRuns = [];        // последний полный набор (до клиентской фильтрации)
let limit = 20;
let categoryFilter = ''; // '' = все

// Маппинг profile_name (в БД) → ключ категории и человеческое имя.
const CATEGORIES = {
  stress:        'Стресс-тест',
  cpu_advanced:  'Расш. тест CPU',
  ramcache:      'Ram & Cache',
  winsat:        'Наследие Winsat',
  general:       'Общая оценка системы',
  other:         'Другое',
};

function profileToCategory(profile) {
  const p = (profile || '').toLowerCase();
  if (!p) return 'other';
  // Stress: всё что обозначено как stress-профиль или комбинированный CPU+RAM-стресс
  if (p.includes('stress') || ['cpu_heavy', 'mixed_quick', 'diagnostic',
       'cpu_int', 'cpu_fp', 'ram_bw', 'ram_lat'].includes(p)) return 'stress';
  // Расш. тест CPU: scoring v2 micro + точечные CPU-бенчмарки
  if (p === 'micro' || p === 'cpu_advanced' || p.startsWith('micro_')
      || p.startsWith('single_multi') || ['flops_sp', 'flops_dp',
       'int_iops_64', 'int_iops_32', 'int_iops_24', 'aes_256', 'sha1',
       'julia_sp', 'mandelbrot_dp', 'memory_read', 'memory_write',
       'memory_copy'].includes(p)) return 'cpu_advanced';
  if (p === 'ram_cache' || p === 'ramcache' || p.startsWith('ram_cache_')) return 'ramcache';
  if (p === 'winsat' || p.startsWith('winsat_')) return 'winsat';
  if (p === 'general' || p === 'general_benchmark' || p.startsWith('general_')) return 'general';
  return 'other';
}

export async function render(host) {
  host.innerHTML = `
    <div class="screen">
      <div class="screen__header">
        <div class="screen__header__title">
          <h1>История</h1>
          <span class="screen__header__index">08</span>
        </div>
        <div class="screen__header__sub">
          Список последних тестов. Фильтр по типу и количеству — справа.
        </div>
      </div>

      <div class="card">
        <div class="card__title">
          <span>Последние тесты</span>
          <span class="card__title-tag" id="history-count">—</span>
        </div>

        <div class="history-controls">
          <div class="field">
            <label class="field__label">Тип теста</label>
            <select id="filter-category" style="min-width: 220px;">
              <option value="">Все типы</option>
            </select>
          </div>

          <div class="field">
            <label class="field__label">Показать</label>
            <div class="radio-group" id="limit-group">
              <label><input type="radio" name="limit" value="5"  ${limit===5  ? 'checked' : ''}/><span>5</span></label>
              <label><input type="radio" name="limit" value="10" ${limit===10 ? 'checked' : ''}/><span>10</span></label>
              <label><input type="radio" name="limit" value="20" ${limit===20 ? 'checked' : ''}/><span>20</span></label>
              <label><input type="radio" name="limit" value="50" ${limit===50 ? 'checked' : ''}/><span>50</span></label>
            </div>
          </div>

          <button class="btn sm" id="btn-history-refresh" style="margin-left: auto; align-self: end;">↻ Обновить</button>
        </div>

        <div id="history-table"></div>
      </div>
    </div>
  `;

  document.getElementById('btn-history-refresh').addEventListener('click', () => { void load(); });
  document.getElementById('filter-category').addEventListener('change', (e) => {
    categoryFilter = e.target.value;
    renderRows();
  });
  for (const r of document.querySelectorAll('input[name="limit"]')) {
    r.addEventListener('change', (e) => {
      limit = parseInt(e.target.value, 10);
      void load();
    });
  }

  await load();
}

async function load() {
  const host = document.getElementById('history-table');
  if (!host) return;
  host.innerHTML = `<div style="color: var(--muted); padding: var(--gap-lg); text-align: center;">загрузка…</div>`;
  try {
    // /api/history объединяет все 4 таблицы (stress / general / cpu_advanced /
    // winsat) с уже посчитанными score + готовым score_label. См. server.py.
    const resp = await fetch('/api/history?limit=50');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    allRuns = await resp.json();
  } catch (err) {
    host.innerHTML = `<div style="color: var(--danger); padding: var(--gap-lg);">Ошибка: ${escapeHtml(err.message)}</div>`;
    allRuns = [];
    return;
  }
  refreshCategoryDropdown();
  renderRows();
}

function refreshCategoryDropdown() {
  const sel = document.getElementById('filter-category');
  if (!sel) return;
  // Категории берём из item.type (сервер уже их классифицировал).
  const presentCats = new Set(allRuns.map(r => r.type));
  // Порядок отображения — по списку CATEGORIES.
  const order = ['stress', 'cpu_advanced', 'ramcache', 'winsat', 'general', 'other'];
  const visible = order.filter(c => presentCats.has(c));
  const current = sel.value;
  sel.innerHTML = `<option value="">Все типы</option>` +
    visible.map(c => `<option value="${c}">${CATEGORIES[c]}</option>`).join('');
  if (visible.includes(current)) sel.value = current;
  else { sel.value = ''; categoryFilter = ''; }
}

function renderRows() {
  const host = document.getElementById('history-table');
  const countEl = document.getElementById('history-count');
  if (!host) return;

  const filtered = categoryFilter
    ? allRuns.filter(r => r.type === categoryFilter)
    : allRuns;
  const shown = filtered.slice(0, limit);

  if (countEl) {
    const totalLabel = categoryFilter
      ? `${shown.length} из ${filtered.length} (${CATEGORIES[categoryFilter]})`
      : `${shown.length} из ${allRuns.length}`;
    countEl.textContent = `// показано: ${totalLabel}`;
  }

  if (shown.length === 0) {
    host.innerHTML = `<div style="color: var(--muted); padding: var(--gap-lg); text-align: center;">
      ${allRuns.length === 0
        ? `Нет сохранённых тестов. Запустите <a href="#stress" style="color: var(--accent);">стресс-тест</a> для первого.`
        : `Нет тестов под фильтр «${escapeHtml(CATEGORIES[categoryFilter] || categoryFilter)}».`}
    </div>`;
    return;
  }

  host.innerHTML = `<table>
    <thead><tr>
      <th>ID</th><th>Тип теста</th><th>Старт</th><th>Длит.</th><th>Балл</th><th>Статус</th><th>Семплов</th><th></th>
    </tr></thead>
    <tbody>
      ${shown.map(renderRow).join('')}
    </tbody>
  </table>`;
  bindRowActions();
}

const STATUS_RU = {
  completed: 'завершён',
  cancelled: 'отменён',
  failed:    'упал',
  running:   'идёт',
};

function renderRow(run) {
  // /api/history даёт уже готовые duration_sec, type_label, score_label.
  const dur = run.duration_sec != null
    ? run.duration_sec
    : (new Date(run.end_time) - new Date(run.start_time)) / 1000;
  const statusCls = run.status === 'completed' ? 'ok'
    : run.status === 'cancelled' ? 'warn'
    : run.status === 'failed' ? 'danger' : '';
  const catLabel = run.type_label || CATEGORIES[run.type] || 'Прогон';
  const statusLabel = STATUS_RU[run.status] || run.status;
  // score_label уже отформатирован сервером с учётом шкалы (×10 000 для
  // stress/general/cpu_advanced, "WinSPR X.X" для winsat, "—" для null).
  // Для winsat дополнительно есть score_breakdown с 5 подскорами — рендерим
  // как hover-tooltip + ⓘ icon чтобы пользователь увидел что есть детали.
  let scoreCell;
  if (run.score_label === '—' || !run.score_label) {
    scoreCell = '<span class="dim">—</span>';
  } else if (Array.isArray(run.score_breakdown) && run.score_breakdown.length > 0) {
    const rows = run.score_breakdown.map(b => {
      const status = (b.status || '').toLowerCase();
      const isPass = status === 'pass';
      const isNotSupported = status.includes('not_supported') || status === 'na';
      const cls = isPass ? 'ok' : isNotSupported ? 'dim' : 'warn';
      // Для PASS — показываем metric (FPS / MB/s); для FAILED/NA — note (причину).
      // Если note нет — показываем "—" чтобы было видно что данных нет.
      let aux;
      if (isPass && b.metric_value) {
        aux = `<span class="score-tooltip__metric">${escapeHtml(b.metric_value.toFixed(1))} ${escapeHtml(b.metric_unit || '')}</span>`;
      } else if (b.note) {
        aux = `<span class="score-tooltip__note">${escapeHtml(b.note)}</span>`;
      } else {
        aux = `<span class="score-tooltip__note">недоступно</span>`;
      }
      return `<div class="score-tooltip__row">
          <span class="score-tooltip__dot ${cls}"></span>
          <span class="score-tooltip__label">${escapeHtml(b.label)}</span>
          <span class="score-tooltip__value">${isPass ? b.score.toFixed(1) : '—'}</span>
          ${aux}
        </div>`;
    }).join('');
    scoreCell = `<span class="score-hint" tabindex="0">
        <span class="score-hint__value">${escapeHtml(run.score_label)}</span><span class="score-hint__icon" aria-hidden="true">ⓘ</span>
        <div class="score-tooltip" role="tooltip">
          <div class="score-tooltip__title">// ПОДСКОРЫ WINSAT</div>
          ${rows}
        </div>
      </span>`;
  } else {
    scoreCell = escapeHtml(run.score_label);
  }
  return `<tr>
    <td title="${escapeHtml(run.id)}">${shortUuid(run.id)}…</td>
    <td>
      <div>${escapeHtml(catLabel)}</div>
    </td>
    <td>${fmtDate(run.start_time)}</td>
    <td>${fmtDuration(dur)}</td>
    <td>${scoreCell}</td>
    <td><span class="chip ${statusCls}">${escapeHtml(statusLabel)}</span></td>
    <td>${fmtInt(run.samples)}</td>
    <td class="history-row__actions">
      <div class="history-export">
        <button class="btn sm" data-export="${escapeHtml(run.id)}">Экспорт ▾</button>
        <div class="history-export__menu" data-menu="${escapeHtml(run.id)}">
          <a href="${escapeHtml(api.exportRunUrl(run.id, 'html'))}" target="_blank">HTML для печати</a>
          <a href="${escapeHtml(api.exportRunUrl(run.id, 'json'))}" download>JSON</a>
          <a href="${escapeHtml(api.exportRunUrl(run.id, 'csv'))}" download>CSV</a>
        </div>
      </div>
      <button class="btn sm danger" data-delete="${escapeHtml(run.id)}" title="Удалить прогон">×</button>
    </td>
  </tr>`;
}

// Один глобальный handler на document — закрывает любые открытые dropdown
// (из истории И из settings) когда клик вне `.history-export`. Регистрируется
// один раз, флаг защищает от дублирования при перерисовке таблицы.
let _outsideClickBound = false;
function ensureOutsideClickHandler() {
  if (_outsideClickBound) return;
  _outsideClickBound = true;
  document.addEventListener('click', (ev) => {
    if (ev.target.closest('.history-export')) return;
    for (const m of document.querySelectorAll('.history-export__menu.open')) {
      m.classList.remove('open');
      m.classList.remove('history-export__menu--up');
    }
  });
}

// Тогглит dropdown с автоматическим флипом вверх если не помещается вниз.
// Используется и в history (на каждой строке), и в settings («Экспорт всей истории»).
export function toggleExportMenu(menu) {
  ensureOutsideClickHandler();
  if (!menu) return;
  if (menu.classList.contains('open')) {
    menu.classList.remove('open');
    menu.classList.remove('history-export__menu--up');
    return;
  }
  // Закрыть остальные открытые меню перед тем как открыть новое.
  for (const m of document.querySelectorAll('.history-export__menu.open')) {
    if (m !== menu) {
      m.classList.remove('open');
      m.classList.remove('history-export__menu--up');
    }
  }
  // Открыть и проверить помещается ли вниз.
  menu.classList.remove('history-export__menu--up');
  menu.classList.add('open');
  const rect = menu.getBoundingClientRect();
  const vh = window.innerHeight || document.documentElement.clientHeight;
  if (rect.bottom > vh - 8) {
    menu.classList.add('history-export__menu--up');
  }
}

function bindRowActions() {
  ensureOutsideClickHandler();
  const host = document.getElementById('history-table');
  if (!host) return;
  host.addEventListener('click', async (ev) => {
    const exportBtn = ev.target.closest('button[data-export]');
    const deleteBtn = ev.target.closest('button[data-delete]');
    if (exportBtn) {
      ev.preventDefault();
      ev.stopPropagation();
      const runId = exportBtn.dataset.export;
      const menu = host.querySelector(`.history-export__menu[data-menu="${runId}"]`);
      toggleExportMenu(menu);
      return;
    }
    if (deleteBtn) {
      ev.preventDefault();
      const runId = deleteBtn.dataset.delete;
      if (!window.confirm(`Удалить прогон ${runId.slice(0, 8)}…? Действие необратимо.`)) return;
      try {
        await api.deleteRun(runId);
        await load();
      } catch (err) {
        alert('Не удалось удалить: ' + err.message);
      }
    }
  });
}

function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[c]);
}
