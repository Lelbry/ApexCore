// Утилиты форматирования и цветовой логики.
//
// Источник правды для цветовых порогов — PROJECT_CONTEXT.md §8.
// UI никогда не пересчитывает scoring сам — только классифицирует значение.

const ruNum = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 });
const ruNum2 = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 });
const ruNum0 = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });
const ruDate = new Intl.DateTimeFormat('ru-RU', {
  day: '2-digit', month: '2-digit', year: 'numeric',
  hour: '2-digit', minute: '2-digit',
});

export function fmtNum(value, digits = 1) {
  if (value == null || Number.isNaN(value)) return '—';
  const formatter = digits === 0 ? ruNum0 : (digits === 2 ? ruNum2 : ruNum);
  return formatter.format(Number(value));
}

export function fmtInt(value) {
  if (value == null || Number.isNaN(value)) return '—';
  return ruNum0.format(Number(value));
}

export function fmtDate(value) {
  if (value == null) return '—';
  try {
    return ruDate.format(new Date(value));
  } catch {
    return String(value);
  }
}

export function fmtTime(value) {
  if (value == null) return '—';
  try {
    return new Date(value).toLocaleTimeString('ru-RU');
  } catch {
    return String(value);
  }
}

export function fmtDuration(seconds) {
  if (seconds == null) return '—';
  const s = Math.floor(seconds);
  if (s < 60) return `${s} с`;
  const m = Math.floor(s / 60);
  const sec = s % 60;
  if (m < 60) return `${m} мин ${sec.toString().padStart(2, '0')} с`;
  const h = Math.floor(m / 60);
  return `${h} ч ${(m % 60).toString().padStart(2, '0')} мин`;
}

export function fmtBytes(mb) {
  if (mb == null) return '—';
  if (mb < 1024) return `${fmtNum(mb)} МБ`;
  return `${fmtNum(mb / 1024, 2)} ГБ`;
}

// ─── Цветовая классификация по PROJECT_CONTEXT §8 ──────────────────
// Возвращает 'ok' | 'warn' | 'danger' | '' (если значение неприменимо)

export function colorForCpuTemp(c) {
  if (c == null) return '';
  if (c >= 85) return 'danger';
  if (c >= 75) return 'warn';
  return 'ok';
}

export function colorForGpuTemp(c) {
  if (c == null) return '';
  if (c >= 90) return 'danger';
  if (c >= 80) return 'warn';
  return 'ok';
}

export function colorForGeneralScore(score) {
  if (score == null) return '';
  if (score >= 6000) return 'ok';
  if (score >= 3000) return 'warn';
  return 'danger';
}

export function colorForStressScore(score) {
  if (score == null) return '';
  if (score >= 5000) return 'ok';
  if (score >= 2500) return 'warn';
  return 'danger';
}

export function colorForScoringV2(score) {
  if (score == null) return '';
  if (score >= 600) return 'ok';
  if (score >= 200) return 'warn';
  return 'danger';
}

export function colorForWinsat(score) {
  if (score == null) return '';
  if (score >= 8.0) return 'ok';
  if (score >= 5.0) return 'warn';
  return 'danger';
}

export function colorForFrsPct(pct) {
  if (pct == null) return '';
  if (pct >= 97) return 'ok';
  if (pct >= 90) return 'warn';
  return 'danger';
}

// ─── Классификация сенсорных ключей (для группировки в Sensors) ────
// Возвращает 'cpu' | 'gpu' | 'mb' | 'mem' | 'storage' | 'fan' | 'other'.
export function sensorGroup(key) {
  const k = (key || '').toLowerCase();
  if (k.startsWith('cpu_power/') || k.startsWith('cpu/')) return 'cpu';
  if (k.startsWith('gpunvidia/') || k.startsWith('gpuamd/') || k.startsWith('gpuintel/')
      || k.startsWith('nvml/')) return 'gpu';
  if (k.startsWith('motherboard/')) return 'mb';
  if (k.startsWith('memory/')) return 'mem';
  if (k.startsWith('storage/')) return 'storage';
  if (k.startsWith('fan/')) return 'fan';
  // legacy ключи без prefix
  if (k.includes('gpu') || k.includes('videocard')) return 'gpu';
  if (k.includes('cpu') || k.includes('core') || k.includes('package') ||
      k.includes('tctl') || k.includes('tdie') || k.includes('processor')) return 'cpu';
  return 'other';
}

export function shortUuid(uuid) {
  if (typeof uuid !== 'string' || uuid.length < 8) return uuid || '—';
  return uuid.slice(0, 8);
}

// Перевод имени группы сенсоров → человеческое
export const SENSOR_GROUP_LABEL = {
  cpu:    'CPU',
  gpu:    'GPU',
  mb:     'Материнская плата',
  mem:    'Память',
  storage: 'Накопители',
  fan:    'Вентиляторы',
  other:  'Прочее',
};
