// Topbar: лого + KPI-тайлы (CPU / RAM / GPU / RUN / WS) с детальными
// значениями из MetricSnapshot — приближённо к эталону handoff bundle.
//
// CPU-тайл показывает: T° · V · W · % · GHz (пять значений в одну строку).
// GPU-тайл: T° · V · W · % · MHz.
// RAM-тайл: T° · % · ГБ.
// RUN: имя движка / job id или idle.
// WS: live / down.

import { state, subscribe } from '../store.js';
import { fmtNum, colorForCpuTemp, colorForGpuTemp } from '../format.js';
import { getTheme, toggleTheme } from '../theme.js';

export function renderTopbar(host) {
  host.innerHTML = `
    <div class="topbar__brand">
      <img src="/static/assets/apex-logo.png" alt="ApexCore" />
      <div class="topbar__brand-text">
        <span class="topbar__brand-name">ApexCore</span>
        <span class="topbar__version" id="topbar-version">загрузка…</span>
      </div>
    </div>
    <div class="topbar__tiles" id="topbar-tiles">
      <div class="topbar__tile" id="tile-cpu">
        <span class="topbar__tile-label" id="tile-cpu-label">CPU</span>
        <span class="topbar__tile-value" id="tile-cpu-value">—</span>
      </div>
      <div class="topbar__tile" id="tile-ram">
        <span class="topbar__tile-label">RAM</span>
        <span class="topbar__tile-value" id="tile-ram-value">—</span>
      </div>
      <div class="topbar__tile" id="tile-gpu">
        <span class="topbar__tile-label" id="tile-gpu-label">GPU</span>
        <span class="topbar__tile-value" id="tile-gpu-value">—</span>
      </div>
      <button class="topbar__tile topbar__theme-toggle" id="tile-theme" type="button"
              title="Переключить тему (Тёмная ↔ Светлая)">
        <span class="topbar__tile-label">ТЕМА</span>
        <span class="topbar__tile-value" id="tile-theme-value">${themeLabel()}</span>
      </button>
      <div class="topbar__tile" id="tile-ws">
        <span class="topbar__tile-label">WS</span>
        <span class="topbar__tile-value dim" id="tile-ws-value">…</span>
      </div>
    </div>
    <div class="topbar__sys" id="topbar-sys">подключение…</div>
  `;
  refreshAll();
  let labelsBootstrapped = false;
  subscribe((ev) => {
    if (ev.type === 'system' || ev.type === 'config') refreshLabels();
    if (ev.type === 'snapshot') {
      refreshTiles();
      // Первый snapshot — пересчитать лейблы, чтобы GPU-fallback по WS-ключам
      // подхватился, когда sys.gpu_list пуст (Windows WMI без admin).
      if (!labelsBootstrapped) { refreshLabels(); labelsBootstrapped = true; }
    }
    // bench/stress live-статус виден в statusbar (sb-chip + sb-active);
    // отдельный RUN-тайл в topbar больше не нужен — место отдано переключателю темы.
  });

  // Бейдж WS отражает состояние СОЕДИНЕНИЯ (open → live, down → down), а не
  // наличие снимка: иначе при живом WS до первого тика семплера висит «down»,
  // а после mid-session разрыва — ложно остаётся «live» (снимок-то в state
  // остался). Событие шлёт app.js из open/down-хендлеров MetricsSocket.
  window.addEventListener('ws:status', (e) => setWs(!!e.detail?.connected));

  // Theme-toggle bindings.
  document.getElementById('tile-theme')?.addEventListener('click', () => toggleTheme());
  window.addEventListener('theme:change', () => {
    const el = document.getElementById('tile-theme-value');
    if (el) el.textContent = themeLabel();
  });
}

// «☀ light» в тёмной теме = «кликни чтобы переключиться на light», «☾ dark» —
// наоборот. Стандарт UX (видишь куда кликаешь, не текущее состояние). Те же
// строки используются в installer wizard (см. bootstrapper components.js:74).
function themeLabel() {
  return getTheme() === 'dark' ? '☀ light' : '☾ dark';
}

function refreshAll() {
  refreshLabels();
  refreshTiles();
}

function refreshLabels() {
  const version = state.config?.version;
  if (version) setText('topbar-version', `v${version}`);

  const sys = state.system;
  if (sys) {
    // Шапка справа — оставляем для общего паспорта системы.
    const cores = sys.cpu_cores;
    const coreStr = (cores?.p_cores != null && cores?.e_cores != null)
      ? `${cores.p_cores}P + ${cores.e_cores}E / ${cores.logical}T`
      : `${cores?.physical ?? '?'} / ${cores?.logical ?? '?'} ядер`;
    const el = document.getElementById('topbar-sys');
    if (el) el.innerHTML = `<b>${escapeHtml(sys.cpu_model || '—')}</b><br>${escapeHtml(sys.os_name || '')} · ${coreStr} · ${sys.ram_total_gb?.toFixed(1) ?? '?'} ГБ`;
    // Лейблы тайлов — короткие имена устройств.
    setText('tile-cpu-label', shortCpuLabel(sys.cpu_model));
    // Приоритет имени GPU:
    //   1) discrete (NVIDIA RTX/GTX, AMD RX, Intel Arc) — самая полезная
    //      информация для пользователя;
    //   2) первое не-виртуальное (не "Virtual Desktop Monitor" / Basic);
    //   3) NVML fallback (если sys.gpu_list пуст совсем);
    //   4) "GPU".
    // На гибридных системах WMI часто отдаёт [iGPU, discrete] — берём
    // discrete как более значимый для бенчмарков.
    const gpus = sys.gpu_list || [];
    const isVirtual = g => /virtual|microsoft basic|generic non-pnp/i.test(g || '');
    const isDiscrete = g => /(RTX|GTX|RX\s+\d|Arc\s+[A-Z]|Radeon|GeForce)/i.test(g || '');
    const isIntegrated = g => /\bUHD Graphics|\bHD Graphics|\bIris|AMD Radeon\(TM\) Graphics/i.test(g || '');
    const real = gpus.filter(g => g && !isVirtual(g));
    const discrete = real.find(g => isDiscrete(g) && !isIntegrated(g));
    const chosen = discrete || real.find(g => !isIntegrated(g)) || real[0];
    const gpuName = shortGpuLabel(chosen) || gpuLabelFromSnapshot(state.lastSnap) || 'GPU';
    setText('tile-gpu-label', gpuName);
  }
}

function shortCpuLabel(model) {
  if (!model) return 'CPU';
  // "12th Gen Intel(R) Core(TM) i9-12900K" → "i9-12900K"
  const m = model.match(/i[3579]-\w+|Ryzen\s+\w+\s*\w*|Xeon\s+\S+/i);
  return m ? m[0] : 'CPU';
}

function shortGpuLabel(model) {
  if (!model) return null;  // fallback резолвится по WS-ключам в refreshTiles
  // 1) lspci-формат с квадратными скобками (Linux):
  //    "Advanced Micro Devices, Inc. [AMD/ATI] Rembrandt [Radeon 680M] (rev 02)"
  //    → берём ПОСЛЕДНЮЮ скобку с цифрами или известным brand-name,
  //    игнорируя технические вроде "[AMD/ATI]" / "[Rembrandt]".
  const brackets = model.match(/\[([^\]]+?)\]/g);
  if (brackets) {
    for (let i = brackets.length - 1; i >= 0; i--) {
      const inner = brackets[i].slice(1, -1).trim();
      if (/\d/.test(inner) || /Radeon|GeForce|Iris|UHD|Xe|Arc|RTX|GTX/i.test(inner)) {
        return inner;
      }
    }
  }
  // 2) Discrete vendor patterns (Windows WMI / clean strings):
  //    "NVIDIA GeForce RTX 4080 SUPER" → "RTX 4080 SUPER"
  //    "AMD Radeon RX 7900 XTX" → "Radeon RX 7900 XTX"
  const m = model.match(/(RTX|GTX|RX|Arc|Radeon|GeForce|Iris)\s+[\w\s]+?(?=\s*\(|$)/i);
  return m ? m[0].trim() : (model.length > 22 ? model.slice(0, 22) + '…' : model);
}

// Если sys.gpu_list пуст (на Windows WMI Win32_VideoController может не дать
// имя без admin), определяем вендора по ключам в MetricSnapshot. NVML →
// «NVIDIA GPU», gpuamd/ → «AMD GPU», и т.д. Это даёт хотя бы вендор вместо
// безликого «GPU».
function gpuLabelFromSnapshot(snap) {
  if (!snap) return null;
  const allKeys = [
    ...Object.keys(snap.temperatures || {}),
    ...Object.keys(snap.frequencies || {}),
    ...Object.keys(snap.voltages || {}),
  ].map(k => k.toLowerCase());
  if (allKeys.some(k => k.startsWith('nvml/') || k.startsWith('gpunvidia/'))) return 'NVIDIA GPU';
  if (allKeys.some(k => k.startsWith('gpuamd/'))) return 'AMD GPU';
  if (allKeys.some(k => k.startsWith('gpuintel/'))) return 'Intel GPU';
  return null;
}

function refreshTiles() {
  const snap = state.lastSnap;
  // Бейдж WS управляется событием 'ws:status' (см. renderTopbar), не наличием
  // снимка — тут только значения тайлов.
  if (!snap) return;

  // ─── CPU: T° · V · W · % · GHz ─────────────────────────────────────
  const cpuTemp = maxBy(snap.temperatures, isCpuTempKey);
  const cpuVoltage = pickFirst(snap.voltages, ['cpu/cpu_core', 'cpu/vcore', 'cpu/core_vid']);
  const cpuPower = snap.power_w
    ?? pickFirst(snap.temperatures, []) // placeholder
    ?? null;
  // Лучше брать cpu_power/package из temperatures? Нет — это температура.
  // Power_w на корне MetricSnapshot, или нет — отдадим null.
  const cpuFreq = snap.frequencies?.cpu_avg
    ?? snap.frequencies?.cpu_max
    ?? null;
  const cpuLoad = snap.cpu_percent;
  setTile('tile-cpu-value', composeMultiValue([
    cpuTemp != null   ? `${fmtNum(cpuTemp)}°C` : null,
    cpuVoltage != null ? `${fmtNum(cpuVoltage, 2)} В` : null,
    snap.power_w != null ? `${fmtNum(snap.power_w)} Вт` : null,
    cpuLoad != null  ? `${fmtNum(cpuLoad)}%` : null,
    cpuFreq != null  ? `${fmtNum(cpuFreq / 1000, 2)} GHz` : null,
  ]), colorForCpuTemp(cpuTemp));

  // ─── RAM: T° · used/total ГБ · % ───────────────────────────────────
  const ramTemp = maxBy(snap.temperatures, k => k.toLowerCase().startsWith('memory/'));
  const ramTotal = state.system?.ram_total_gb;
  const ramBytesStr = snap.ram_used_gb != null
    ? (ramTotal != null
        ? `${fmtNum(snap.ram_used_gb, 1)} / ${fmtNum(ramTotal, 0)} ГБ`
        : `${fmtNum(snap.ram_used_gb, 1)} ГБ`)
    : null;
  setTile('tile-ram-value', composeMultiValue([
    ramTemp != null ? `${fmtNum(ramTemp)}°C` : null,
    ramBytesStr,
    snap.ram_percent != null ? `${fmtNum(snap.ram_percent)}%` : null,
  ]), '');

  // ─── GPU: T° · V · W · % · MHz ─────────────────────────────────────
  // Ключи в порядке предпочтения: сперва NVML/LHM (Windows), потом
  // hwmon Linux (gpuamd/vddgfx, gpuamd/power_average, gpuamd/sclk).
  const gpuTemp = maxBy(snap.temperatures, isGpuKey);
  const gpuVoltage = pickFirst(snap.voltages, [
    'gpunvidia/gpu_core', 'gpuamd/gpu_core', 'gpuintel/gpu_core',  // LHM-стиль (Windows)
    'gpuamd/vddgfx', 'gpuamd/gpu_core_voltage',                    // Linux hwmon (amdgpu)
  ]);
  let gpuPowerVal = pickFromAny(snap, [
    'nvml/0/power_w', 'gpunvidia/gpu_power', 'gpunvidia/gpu_package_power',  // NVML/LHM
    'gpuamd/power_average', 'gpuamd/ppt',                                     // Linux hwmon
  ]);
  const gpuUtil = pickFromAny(snap, ['nvml/0/util_gpu']);
  const gpuClock = snap.frequencies?.['nvml/0/clock_graphics']
    ?? snap.frequencies?.['gpunvidia/gpu_core_clock']
    ?? snap.frequencies?.['gpuamd/sclk']        // Linux hwmon (amdgpu shader clock)
    ?? snap.frequencies?.['gpuamd/gpu_clock']
    ?? null;
  setTile('tile-gpu-value', composeMultiValue([
    gpuTemp != null     ? `${fmtNum(gpuTemp)}°C` : null,
    gpuVoltage != null  ? `${fmtNum(gpuVoltage, 2)} В` : null,
    gpuPowerVal != null ? `${fmtNum(gpuPowerVal)} Вт` : null,
    gpuUtil != null     ? `${fmtNum(gpuUtil)}%` : null,
    gpuClock != null    ? `${fmtNum(gpuClock, 0)} MHz` : null,
  ]), colorForGpuTemp(gpuTemp));
}

function setWs(connected) {
  setText('tile-ws-value', connected ? 'live' : 'down');
  setClass('tile-ws-value', 'topbar__tile-value ' + (connected ? 'cool' : 'hot'));
}

// ─── Helpers ────────────────────────────────────────────────────────

function composeMultiValue(parts) {
  return parts.filter(p => p != null && p !== '').join(' · ') || '—';
}

function maxBy(obj, predicate) {
  if (!obj) return null;
  let best = null;
  for (const [k, v] of Object.entries(obj)) {
    if (typeof v !== 'number' || Number.isNaN(v)) continue;
    if (!predicate(k)) continue;
    if (best == null || v > best) best = v;
  }
  return best;
}

function pickFirst(obj, keys) {
  if (!obj) return null;
  for (const k of keys) {
    if (typeof obj[k] === 'number' && !Number.isNaN(obj[k])) return obj[k];
  }
  return null;
}

// Достаёт значение из любого из словарей snap (temperatures/frequencies/voltages).
function pickFromAny(snap, keys) {
  for (const dict of [snap.temperatures, snap.frequencies, snap.voltages]) {
    if (!dict) continue;
    for (const k of keys) {
      if (typeof dict[k] === 'number' && !Number.isNaN(dict[k])) return dict[k];
    }
  }
  return null;
}

function isCpuTempKey(key) {
  const k = key.toLowerCase();
  if (k.startsWith('cpu/')) return true;
  if (k.includes('gpu')) return false;
  return k.includes('cpu') || k.includes('package') || k.includes('tdie') || k.includes('tctl');
}

function isGpuKey(key) {
  const k = key.toLowerCase();
  return k.startsWith('gpunvidia/') || k.startsWith('gpuamd/') || k.startsWith('gpuintel/')
      || k.startsWith('nvml/') || (k.includes('gpu') && !k.includes('cpu'));
}

function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

function setClass(id, cls) {
  const el = document.getElementById(id);
  if (el) el.className = cls;
}

function setTile(id, value, color = '') {
  setText(id, value);
  setClass(id, 'topbar__tile-value' + (color ? ' ' + color : ''));
}

function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[c]);
}
