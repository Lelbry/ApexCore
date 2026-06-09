// Diagnose — per-subsystem sensor health sweep через GET /api/doctor (§9.8).
//
// Аналог CLI-команды `apexcore doctor`. Показывает статус каждого
// backend'а чтения сенсоров (LHM, HWiNFO/CoreTemp/AIDA64 SHM, NVML,
// smartctl, psutil, WMI, hwmon) + общий statusbar (driver_active,
// cpu_temp_source, gpu_temp_source) + сводный список DegradedReason
// с инструкциями.

import { api } from '../api.js';
import { fmtInt } from '../format.js';

let cache = null;

export async function render(host) {
  host.innerHTML = `
    <div class="screen">
      <div class="screen__header">
        <div class="screen__header__title">
          <h1>Diagnose</h1>
          <span class="screen__header__index">09</span>
        </div>
        <div class="screen__header__sub">
          Проверяет, какие источники датчиков работают на этой машине и
          читается ли температура CPU / GPU. Ниже — список бэкендов:
          <b>OK</b> — источник работает, <b>UNAVAILABLE</b> — не установлен
          (обычно это нормально, нужен лишь один рабочий). «Запустить
          диагностику» — перепроверить сейчас. «Переустановить драйверы» —
          переставить PawnIO + сервис, если температура CPU не читается.
        </div>
      </div>

      <div class="diagnose-controls">
        <button class="btn primary" id="btn-diagnose-run">▶ Запустить диагностику</button>
        <button class="btn" id="btn-diagnose-repair" title="Только Windows. Откроет UAC-окно для переустановки PawnIO и apexcore_sensord.">⚙ Переустановить драйверы (UAC)</button>
        <span class="field__hint" id="diagnose-status">не запущена</span>
      </div>

      <div id="diagnose-body"></div>
    </div>
  `;

  document.getElementById('btn-diagnose-run').addEventListener('click', () => { void load(); });
  const repairBtn = document.getElementById('btn-diagnose-repair');
  if (repairBtn) {
    repairBtn.addEventListener('click', async () => {
      const status = document.getElementById('diagnose-status');
      if (status) status.textContent = 'спавню UAC-окно repair-drivers…';
      repairBtn.disabled = true;
      try {
        await api.repairDrivers();
        if (status) {
          status.textContent = 'окно repair-drivers открыто — подтверди UAC и дождись завершения, затем «Запустить диагностику» ещё раз';
        }
      } catch (err) {
        if (status) status.textContent = `ошибка: ${err.message}`;
      } finally {
        repairBtn.disabled = false;
      }
    });
  }
  // Автостарт при первом открытии — пользователь сразу видит результат.
  await load();
}

async function load() {
  const status = document.getElementById('diagnose-status');
  const body = document.getElementById('diagnose-body');
  const btn = document.getElementById('btn-diagnose-run');
  if (status) status.textContent = '⟳ выполняется…';
  if (btn) btn.disabled = true;
  if (body) { body.style.transition = 'opacity 0.15s ease'; body.style.opacity = '0.45'; }
  const t0 = Date.now();
  try {
    cache = await api.getDoctor();
  } catch (err) {
    cache = { error: err.message };
  }
  // /api/doctor отвечает за ~100 мс — держим индикатор минимум ~450 мс,
  // иначе клик визуально «ничего не делает» (экран уже показан с autoload).
  const elapsed = Date.now() - t0;
  if (elapsed < 450) await new Promise((r) => setTimeout(r, 450 - elapsed));
  if (btn) btn.disabled = false;
  if (body) body.style.opacity = '1';
  renderDoctor();
}

function renderDoctor() {
  const body = document.getElementById('diagnose-body');
  const status = document.getElementById('diagnose-status');
  if (!body) return;
  if (!cache) return;
  if (cache.error) {
    if (status) status.textContent = 'ошибка';
    body.innerHTML = `<div class="banner danger"><b>Ошибка диагностики:</b> ${escapeHtml(cache.error)}</div>`;
    return;
  }
  if (status) status.textContent = `✓ обновлено · ${new Date().toLocaleTimeString('ru-RU')}`;

  body.innerHTML = `
    ${renderOverview()}
    ${renderDegradedReasons()}
    ${renderBackends()}
    ${renderAdvice()}
  `;
}

function renderOverview() {
  const d = cache;
  const cpuChip = d.has_cpu_temperature
    ? `<span class="chip ok">CPU temperature · доступна</span>`
    : `<span class="chip danger">CPU temperature · недоступна</span>`;
  const gpuChip = d.has_gpu_temperature
    ? `<span class="chip ok">GPU temperature · доступна</span>`
    : `<span class="chip warn">GPU temperature · недоступна</span>`;
  const driverChip = d.driver_active
    ? `<span class="chip ok">driver active</span>`
    : `<span class="chip danger">driver inactive</span>`;
  return `<div class="diagnose-overview">
    <div class="diagnose-overview__title">// SYSTEM SWEEP · ${escapeHtml(d.platform || '—')}</div>
    <div class="diagnose-overview__chips">${cpuChip} ${gpuChip} ${driverChip}</div>
    <div class="rows" style="margin-top: var(--gap);">
      <div class="row"><span class="k">cpu_temp_source</span><span class="v">${escapeHtml(d.cpu_temp_source || '—')}</span></div>
      <div class="row"><span class="k">gpu_temp_source</span><span class="v">${escapeHtml(d.gpu_temp_source || '—')}</span></div>
      <div class="row"><span class="k">backends_total</span><span class="v">${(d.backends || []).length}</span></div>
      <div class="row"><span class="k">backends_ok</span><span class="v cool">${(d.backends || []).filter(b => b.ok).length}</span></div>
    </div>
  </div>`;
}

function renderDegradedReasons() {
  const list = cache.degraded_reasons || [];
  if (list.length === 0) return '';
  return `<div class="card" style="margin-top: var(--gap-lg); border-color: var(--warn);">
    <div class="card__title" style="color: var(--warn);">⚠ Degraded · ${list.length}</div>
    <div class="rows">
      ${list.map(r => `<div class="row">
        <span class="k">${escapeHtml(r.value)}</span>
        <span class="v warm">${escapeHtml(r.short)}</span>
      </div>`).join('')}
    </div>
  </div>`;
}

function renderBackends() {
  const list = cache.backends || [];
  if (list.length === 0) return '';
  return `<div class="diagnose-backends">
    <div class="card__title" style="margin-top: var(--gap-lg);">// BACKENDS · ${list.length}</div>
    <div class="diagnose-backends__grid">
      ${list.map(renderBackend).join('')}
    </div>
  </div>`;
}

function renderBackend(b) {
  const chipCls = b.ok ? 'ok' : (b.reason ? 'warn' : 'danger');
  const chipText = b.ok
    ? `OK · ${fmtInt(b.sensor_count)} ${pluralRu(b.sensor_count, ['сенсор', 'сенсора', 'сенсоров'])}`
    : (b.reason ? 'DEGRADED' : 'UNAVAILABLE');
  const sampleEntries = Object.entries(b.sample || {}).slice(0, 5);
  return `<div class="diagnose-backend">
    <div class="diagnose-backend__head">
      <span class="diagnose-backend__name">${escapeHtml(b.name)}</span>
      <span class="chip ${chipCls}">${chipText}</span>
    </div>
    ${b.detail ? `<div class="diagnose-backend__detail">${escapeHtml(b.detail)}</div>` : ''}
    ${b.reason ? `<div class="diagnose-backend__reason">
      <span class="diagnose-backend__reason-code">${escapeHtml(b.reason)}</span>
      <span>${escapeHtml(b.reason_short || '')}</span>
    </div>` : ''}
    ${sampleEntries.length > 0 ? `<div class="diagnose-backend__sample">
      <div class="diagnose-backend__sample-title">// sample</div>
      ${sampleEntries.map(([k, v]) => `<div class="row">
        <span class="k">${escapeHtml(k)}</span>
        <span class="v">${typeof v === 'number' ? v.toFixed(1) : escapeHtml(String(v))}</span>
      </div>`).join('')}
    </div>` : ''}
  </div>`;
}

function renderAdvice() {
  const list = cache.advice || [];
  if (list.length === 0) return '';
  return `<div class="card" style="margin-top: var(--gap-lg);">
    <div class="card__title">// RECOMMENDATIONS · ${list.length}</div>
    <ul style="margin: 0; padding-left: var(--gap-lg); color: var(--text-dim); font-size: 12px; line-height: 1.7;">
      ${list.map(a => `<li>${escapeHtml(a)}</li>`).join('')}
    </ul>
  </div>`;
}

function pluralRu(n, forms) {
  const r = Math.abs(n) % 100;
  if (r >= 11 && r <= 14) return forms[2];
  const r1 = r % 10;
  if (r1 === 1) return forms[0];
  if (r1 >= 2 && r1 <= 4) return forms[1];
  return forms[2];
}

function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[c]);
}
