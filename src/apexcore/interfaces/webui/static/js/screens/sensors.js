// Sensors Live — §9.2 полный dashboard.
//
// Подключается к WS /ws/sensors, получает SensorSnapshot с readings
// группированными по device. Рендерит 6 карточек (CPU/GPU/Memory/MB/Fans/
// Storage) с per-card SOURCE badge (LHM/HWiNFO/CoreTemp/AIDA64/NVML/smartctl/
// psutil) + ThrottleState banner сверху.

import { api } from '../api.js';
import { MetricsSocket } from '../ws.js';
import { fmtNum } from '../format.js';
import { renderSparkline } from '../components/sparkline.js';

let socket = null;
let lastSnap = null;
// Storage inventory приходит только в REST /api/sensors/snapshot (статичный
// список физических дисков с буквами). WS-сообщения его не содержат — кэшируем
// отдельно, чтобы при WS-обновлениях не потерять.
let storageInventory = [];
// История температур каждого reading'а — для inline sparkline. Ключ
// `${group}/${device}/${sensor}`, значение — кольцевой буфер до HISTORY_CAP
// последних значений. Заполняется в pushHistory() на каждом WS-тике.
const tempHistory = new Map();
const HISTORY_CAP = 60;  // ~60s при rate=1s / ~30s при 0.5s

function readingKey(r) { return `${r.group}/${r.device}/${r.sensor}`; }

function pushHistory(snap) {
  for (const r of (snap.readings || [])) {
    if (r.kind !== 'temperature') continue;
    const key = readingKey(r);
    let buf = tempHistory.get(key);
    if (!buf) { buf = []; tempHistory.set(key, buf); }
    buf.push(typeof r.value === 'number' ? r.value : null);
    if (buf.length > HISTORY_CAP) buf.shift();
  }
}

// `memory` группа схлапывается в `motherboard` (см. mergeGroup ниже) — на
// материнке физически и живут DIMM-датчики, отдельная карточка для 2-3
// показаний оверкилл. См. также CLI render_sensors._build_group_panel.
const GROUP_ORDER = ['cpu', 'gpu', 'motherboard', 'fans', 'storage'];
const GROUP_LABELS = {
  cpu:         'CPU',
  gpu:         'GPU',
  motherboard: 'Материнская плата',
  fans:        'Вентиляторы',
  storage:     'Накопители',
};
const GROUP_ICONS = {
  cpu:         '◧',
  gpu:         '◨',
  motherboard: '▦',
  fans:        '✦',
  storage:     '◇',
};

// memory → motherboard. Возвращает целевую группу для reading'а.
function mergeGroup(group) {
  return group === 'memory' ? 'motherboard' : group;
}
const KIND_ORDER = {
  temperature: 0,
  fan_rpm:     1,
  voltage:     2,
  power:       3,
  frequency:   4,
  load:        5,
  usage_bytes: 6,
};

export async function render(host) {
  host.innerHTML = `
    <div class="sensors-screen">
      <div class="screen__header">
        <div class="screen__header__title">
          <h1>Sensors Live</h1>
          <span class="screen__header__index">02</span>
        </div>
        <div class="screen__header__sub">
          Структурированные показания по группам устройств. Badge источника
          показывает откуда пришло значение (LHM / HWiNFO / CoreTemp / AIDA64 /
          NVML / smartctl / psutil). Throttle-баннер сверху появляется
          при активном термальном тротлинге.
        </div>
      </div>

      <div id="sensors-throttle"></div>
      <div class="sensors-grid" id="sensors-grid">
        <div style="color: var(--muted); padding: var(--gap-lg); grid-column: 1 / -1;">подключение к сенсорам…</div>
      </div>
    </div>
  `;

  // 1) Сразу запрашиваем snapshot через REST, чтобы не ждать первый WS-тик.
  //    Заодно достаём storage_devices inventory — он есть только в REST-ответе.
  try {
    const initial = await api.getSensorsSnapshot();
    storageInventory = initial.storage_devices || [];
    lastSnap = initial;
    renderSnapshot();
  } catch (err) {
    // если REST отдал 503 (семплер ещё не успел) — WS-тик подъедет
    console.warn('sensors snapshot REST failed', err);
  }

  // 2) Подключаемся к WS для live-обновлений.
  socket = new MetricsSocket('/ws/sensors');
  socket.addEventListener('message', (e) => {
    lastSnap = e.detail;
    renderSnapshot();
  });
  socket.addEventListener('down', () => {
    // overlay про разрыв уже обрабатывается app.js на /ws/metrics —
    // здесь молчим, чтобы не дублировать.
  });
  socket.connect();
}

export function dispose() {
  if (socket) { socket.close(); socket = null; }
}

// open-state collapsible-блоков (data-collapse-key) сохраняется между WS-тиками,
// потому что renderCards() пересоздаёт innerHTML и без этого `<details>` будет
// сбрасываться в closed на каждой смене snapshot'а.
const collapseOpenState = new Map();

function renderSnapshot() {
  if (!lastSnap) return;
  // Снимаем текущее состояние перед перерисовкой.
  for (const el of document.querySelectorAll('[data-collapse-key]')) {
    collapseOpenState.set(el.dataset.collapseKey, el.open);
  }
  pushHistory(lastSnap);
  renderThrottle();
  renderCards();
  renderSparklines();
}

// Заполняет все мини-canvas'ы в строках, которые ожидают sparkline.
// Делается после renderCards (когда DOM существует) и на каждом тике.
function renderSparklines() {
  for (const host of document.querySelectorAll('[data-spark-key]')) {
    const key = host.dataset.sparkKey;
    const color = host.dataset.sparkColor || 'var(--accent)';
    const buf = tempHistory.get(key) || [];
    if (buf.length < 2) continue;
    renderSparkline(host, buf, { color, height: 14, minSpread: 2 });
  }
}

function renderThrottle() {
  const host = document.getElementById('sensors-throttle');
  if (!host) return;
  const t = lastSnap.throttle || { cause: 'none', detail: '' };
  const cause = (t.cause || 'none').toLowerCase();
  if (cause === 'none' || !cause) {
    host.innerHTML = '';
    return;
  }
  const causeLabels = {
    thermal:    'тепловой троттлинг',
    power:      'power-лимит',
    current:    'токовый лимит',
    vr_thermal: 'VR-thermal',
    other:      'другая причина',
  };
  host.innerHTML = `<div class="banner danger sensors-throttle">
    <b>⚠ ТРОТТЛИНГ АКТИВЕН · ${escapeHtml(causeLabels[cause] || cause)}</b>
    ${t.detail ? `<div style="margin-top: 4px; font-size: 11px; color: var(--text-dim);">${escapeHtml(t.detail)}</div>` : ''}
  </div>`;
}

function renderCards() {
  const host = document.getElementById('sensors-grid');
  if (!host) return;

  // Группируем readings: group → device → list of readings.
  // Для storage — обогащаем device буквами/типом из storage_devices inventory.
  // Если inventory непуст, а reading не сматчился ни с одним диском (типичный
  // случай: LHM публикует storage/composite_temperature без указания
  // конкретного NVMe-устройства) — **пропускаем такой reading**. CLI в этом
  // месте показывает sub-block «Прочие источники T° дисков», но в Web UI
  // пользователь предпочёл чистый список без агрегатов-фоллбэков.
  const hasInventory = (storageInventory || []).length > 0;
  const grouped = {};
  for (const r of (lastSnap.readings || [])) {
    let device = r.device;
    if (r.group === 'storage') {
      const enriched = enrichStorageDevice(device, storageInventory);
      if (hasInventory && !enriched.matched) {
        // Fallback на single-device системы (типичный laptop = 1 NVMe).
        // На Linux hwmon-storage публикуется с generic device="Накопитель"
        // (см. application/sensor_keys._parse_storage), substring-match с
        // моделью «Phison CFES...» из inventory не срабатывает. Если в
        // inventory ровно одно устройство — считаем что orphan-reading
        // относится к нему и обогащаем именем из inventory вручную.
        // Этот fallback покрывает кейс «smartctl scan не работает без
        // capability, но lsblk видит диск» — без него Storage-карточки
        // на Astra нет вообще.
        if (storageInventory.length === 1) {
          const sd = storageInventory[0];
          const parts = [sd.model || 'Накопитель'];
          if (sd.display_type) parts.push(sd.display_type);
          if (sd.letters && sd.letters.length > 0) parts.push(sd.letters.join(' '));
          device = parts.join(' · ');
        } else {
          continue;
        }
      } else {
        device = enriched.device;
      }
    }
    const targetGroup = mergeGroup(r.group);
    if (!grouped[targetGroup]) grouped[targetGroup] = {};
    if (!grouped[targetGroup][device]) grouped[targetGroup][device] = [];
    grouped[targetGroup][device].push(r);
  }

  const cards = GROUP_ORDER
    .filter(g => grouped[g])
    .map(g => renderGroupCard(g, grouped[g]))
    .join('');

  if (!cards) {
    host.innerHTML = `<div style="color: var(--muted); padding: var(--gap-lg); grid-column: 1 / -1;">нет данных от сенсоров</div>`;
    return;
  }
  host.innerHTML = cards;
}

function renderGroupCard(group, devices) {
  const deviceList = Object.entries(devices);

  // Сортируем readings внутри каждого устройства: temp → fan → volt → power → freq → load
  for (const [, readings] of deviceList) {
    readings.sort((a, b) => {
      const k = (KIND_ORDER[a.kind] ?? 99) - (KIND_ORDER[b.kind] ?? 99);
      return k !== 0 ? k : a.label.localeCompare(b.label, 'ru', { numeric: true });
    });
  }

  // Storage: сортируем устройства по букве диска (как Проводник: C, D, E, F).
  // Без буквы — в конец через ключ '~'. Аналог CLI render_sensors._sort_key.
  if (group === 'storage') {
    deviceList.sort(([a], [b]) => extractLetter(a).localeCompare(extractLetter(b), 'ru'));
  }

  // Считаем агрегаты для подписи карточки.
  const allReadings = deviceList.flatMap(([, rs]) => rs);
  const sources = new Set(allReadings.map(r => r.source).filter(Boolean));
  const sourceBadges = Array.from(sources)
    .map(s => `<span class="src-badge src-${s}">${s.toUpperCase()}</span>`)
    .join(' ');

  return `<div class="sensor-card">
    <div class="sensor-card__head">
      <div class="sensor-card__title">
        <span class="sensor-card__icon">${GROUP_ICONS[group] || '●'}</span>
        <span>${GROUP_LABELS[group] || group}</span>
        <span class="sensor-card__count">${allReadings.length}</span>
      </div>
      <div class="sensor-card__sources">${sourceBadges}</div>
    </div>
    <div class="sensor-card__body">
      ${deviceList.map(([device, readings]) => renderDeviceBlock(device, readings)).join('')}
    </div>
  </div>`;
}

// Достаём первую букву диска для сортировки: "Kingston · SSD NVME · E:" → "E".
// Без буквы / orphan → "~" (сортируется в конец).
function extractLetter(device) {
  const m = String(device || '').match(/·\s*([A-Z]):/i);
  return m ? m[1].toUpperCase() : '~';
}

function renderDeviceBlock(device, readings) {
  // VID per-core voltages (VID P1..P8 / VID E1..E8) — мало кому интересно
  // в реальном времени, занимают много места. Прячем в свёрнутый <details>.
  const isVid = (r) => /^VID\b/i.test(r.label || '');
  const mainReadings = readings.filter(r => !isVid(r));
  const vidReadings  = readings.filter(isVid);

  // Уникальный ключ для сохранения open-state между WS-тиками.
  const vidKey = `vid:${device}`;
  const vidOpen = collapseOpenState.get(vidKey) ? ' open' : '';
  const vidBlock = vidReadings.length > 0 ? `<details class="sensor-vid" data-collapse-key="${escapeAttr(vidKey)}"${vidOpen}>
    <summary class="sensor-vid__summary">
      VID per-core
      <span class="sensor-vid__count">${vidReadings.length}</span>
    </summary>
    <div class="sensor-device__rows sensor-vid__rows">
      ${vidReadings.map(renderReadingRow).join('')}
    </div>
  </details>` : '';

  return `<div class="sensor-device">
    <div class="sensor-device__name">${escapeHtml(device)}</div>
    <div class="sensor-device__rows">
      ${mainReadings.map(renderReadingRow).join('')}
    </div>
    ${vidBlock}
  </div>`;
}

function renderReadingRow(r) {
  const colorCls = valueColorClass(r);
  const valueStr = formatReadingValue(r);
  // Sparkline только для температур — самая информативная метрика тренда.
  // renderSparklines() позже заполнит <span data-spark-key> через SVG.
  const sparkSlot = r.kind === 'temperature'
    ? `<span class="sensor-row__spark"
         data-spark-key="${escapeAttr(readingKey(r))}"
         data-spark-color="${sparkColorFor(colorCls)}"></span>`
    : '';
  return `<div class="sensor-row" title="${escapeHtml(r.sensor)} · ${escapeHtml(r.source || '')}">
    <span class="sensor-row__label">${escapeHtml(r.label)}</span>
    ${sparkSlot}
    <span class="sensor-row__value ${colorCls}">${valueStr}</span>
  </div>`;
}

function sparkColorFor(colorCls) {
  // Возвращаем CSS-переменную (а не хардкод-hex), чтобы цвета sparkline'ов
  // подхватывали смену темы. sparkline.js применяет через style=stroke/fill,
  // где var() работает (в SVG-attribute не работает — это известный сишник).
  if (colorCls === 'hot')  return 'var(--danger)';
  if (colorCls === 'warm') return 'var(--warn)';
  if (colorCls === 'cool') return 'var(--ok)';
  return 'var(--accent)';
}

function escapeAttr(s) {
  return String(s || '').replace(/["&]/g, c => c === '"' ? '&quot;' : '&amp;');
}

// Обогащаем device-имя storage-reading'а из inventory:
//   "ST2000NM0011 · Диск" + inventory[ST2000NM0011] → "ST2000NM0011 · HDD SATA · D:"
//   "Накопитель"          + inventory с одной NVMe записью → если совпадение
//                                                            возможно — приписать.
// Match по substring модели: model из inventory должна встречаться в device
// (case-insensitive). Возвращает { device, matched, letter } где matched=true
// если удалось сопоставить с физическим диском.
function enrichStorageDevice(device, inventory) {
  if (!inventory || inventory.length === 0) {
    return { device, matched: false, letter: '~' };
  }
  const dev = String(device || '');
  const lower = dev.toLowerCase();
  // Берём самое длинное совпадение чтобы "Samsung SSD 860 EVO 500GB" побеждало
  // короткий "Samsung".
  let bestMatch = null;
  for (const sd of inventory) {
    if (!sd.model) continue;
    if (lower.includes(sd.model.toLowerCase())) {
      if (!bestMatch || sd.model.length > bestMatch.model.length) bestMatch = sd;
    }
  }
  if (!bestMatch) {
    return { device, matched: false, letter: '~' };
  }
  const parts = [bestMatch.model];
  if (bestMatch.display_type) parts.push(bestMatch.display_type);
  if (bestMatch.letters && bestMatch.letters.length > 0) {
    parts.push(bestMatch.letters.join(' '));
  }
  const letter = (bestMatch.letters && bestMatch.letters[0])
    ? bestMatch.letters[0].toUpperCase()
    : '~';
  return { device: parts.join(' · '), matched: true, letter };
}

function formatReadingValue(r) {
  const v = r.value;
  const unit = r.unit || '';
  if (typeof v !== 'number' || Number.isNaN(v)) return '—';
  if (r.kind === 'frequency')   return `${fmtNum(v, 0)} ${unit}`;
  if (r.kind === 'fan_rpm')     return `${fmtNum(v, 0)} ${unit}`;
  if (r.kind === 'voltage')     return `${fmtNum(v, 2)} ${unit}`;
  if (r.kind === 'usage_bytes') return `${fmtNum(v, 2)} ${unit}`;
  return `${fmtNum(v, 1)} ${unit}`;
}

function valueColorClass(r) {
  if (r.kind !== 'temperature') return '';
  const v = r.value;
  if (typeof v !== 'number') return '';
  // Используем threshold_crit/warn если есть, иначе фиксированные пороги §8.
  const crit = r.threshold_crit ?? (r.group === 'gpu' ? 90 : 85);
  const warn = r.threshold_warn ?? (r.group === 'gpu' ? 80 : 75);
  if (v >= crit) return 'hot';
  if (v >= warn) return 'warm';
  return 'cool';
}

function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[c]);
}
