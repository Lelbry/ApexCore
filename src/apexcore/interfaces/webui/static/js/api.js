// REST-клиент ApexCore. Все запросы — same-origin, без CORS.
//
// Источник правды для эндпойнтов — PROJECT_CONTEXT.md §5. Endpoint'ы,
// помеченные как pending в DESIGN_BRIEF.md §9.*, в этом клиенте не вызываются —
// соответствующие экраны рендерятся через stub.

const API = (path) => `${window.location.origin}${path}`;

async function request(path, options = {}) {
  const response = await fetch(API(path), options);
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      detail = body.detail || detail;
    } catch { /* пустое тело — норма для 204/500 */ }
    throw new Error(detail);
  }
  if (response.status === 204) return null;
  return response.json();
}

// ─── Endpoints — только то, что реально работает на backend ───────────

export const api = {
  // §5.1
  getSystem: () => request('/api/system'),
  // Реальная конфигурация железа (boot-диск + DRAM) для idle-превью.
  getHardware: () => request('/api/hardware'),
  // §5 — текущая версия Web API + порт/хост + платформа
  getConfig: () => request('/api/config'),
  // POST body { port?: int, host?: string }
  updateConfig: (body) => request('/api/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }),
  // §5.2 — список последних прогонов из таблицы runs (legacy)
  listRuns: (limit = 20, profile = null) => {
    const q = new URLSearchParams({ limit: String(limit) });
    if (profile) q.set('profile', profile);
    return request(`/api/runs?${q.toString()}`);
  },
  // §5.3 — полный BenchmarkResult по UUID или префиксу
  getRun: (runId) => request(`/api/runs/${encodeURIComponent(runId)}`),
  // §5.4 — тренд балла
  getTrend: (metric = 'final_score', { profile = null, last = 30, window = 5 } = {}) => {
    const q = new URLSearchParams({ metric, last: String(last), window: String(window) });
    if (profile) q.set('profile', profile);
    return request(`/api/trend?${q.toString()}`);
  },
  // §5.5 — список стресс-движков (для отладки; в Stress не используется)
  listStressEngines: () => request('/api/stress/list'),
  // §5.6 — статус стресс-контроллера
  stressStatus: () => request('/api/stress/status'),
  // §5.7
  stressStart: (body) => request('/api/stress/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }),
  // §5.8
  stressStop: () => request('/api/stress/stop', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  }),
  // §5.9
  benchProfiles: () => request('/api/bench/profiles'),
  // §5.10
  benchStatus: () => request('/api/bench/status'),
  // §5.11 — body { profile, duration_sec, rate_sec, threads }
  benchStart: (body) => request('/api/bench/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }),
  // §5.12 — отмена текущего bench-прогона. Не мгновенная, реальный stop
  // через ≤ 1-2 сек на ближайшем cancel-token tick стресс-движка.
  // По завершении: status → "cancelled" в /api/bench/status.
  benchStop: () => request('/api/bench/cancel', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  }),
  // §9.8 — sensor health sweep (аналог apexcore doctor)
  getDoctor: () => request('/api/doctor'),
  // POST — спавнит `apexcore repair-drivers` в отдельном UAC-окне
  repairDrivers: () => request('/api/repair-drivers', { method: 'POST' }),
  // §9.2 — snapshot структурированных датчиков (SensorSnapshot)
  getSensorsSnapshot: () => request('/api/sensors/snapshot'),
  // §9.4 — общая оценка системы (CPU + RAM + boot-disk)
  generalStart:  () => request('/api/general/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }),
  generalStatus: () => request('/api/general/status'),
  generalRun:    (runId) => request(`/api/general/runs/${encodeURIComponent(runId)}`),
  // §9.3 — расш. тест CPU: Single/Multi сравнение + Полный прогон
  microStartSingleMulti: (body = {}) => request('/api/micro/start-single-multi', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }),
  // Полный прогон всех 12 микробенчей (scoring v2 standard = 3 прогона).
  // Пресет фиксирован — web не даёт выбирать точность, для accurate идти в CLI.
  microStartFullRun: (body = {}) => request('/api/micro/start-full-run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }),
  microStatus: () => request('/api/micro/status'),
  microStop:   () => request('/api/micro/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }),
  // §9.7 — Наследие Winsat (Windows-only).
  winsatStart: (body = {}) => request('/api/winsat/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }),
  winsatStatus: () => request('/api/winsat/status'),
  winsatStop:   () => request('/api/winsat/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }),
  winsatList:   (limit = 20) => request(`/api/winsat/runs?limit=${limit}`),
  winsatRun:    (runId) => request(`/api/winsat/runs/${encodeURIComponent(runId)}`),
  // §9.6 — Ram & Cache (in-memory, без БД).
  ramCacheStart: (body = {}) => request('/api/ram-cache/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }),
  ramCacheStatus: () => request('/api/ram-cache/status'),
  ramCacheStop:   () => request('/api/ram-cache/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }),
  // §9.9 — экспорт + удаление прогона (любой тип). Экспорт триггерит
  // download через Content-Disposition заголовок — реализуем как window.open
  // вместо fetch (чтобы браузер сам обработал скачивание).
  exportRunUrl: (runId, format = 'json') =>
    `${window.location.origin}/api/runs/${encodeURIComponent(runId)}/export?format=${encodeURIComponent(format)}`,
  exportAllUrl: (format = 'json') =>
    `${window.location.origin}/api/export/all?format=${encodeURIComponent(format)}`,
  deleteRun: (runId) => request(`/api/runs/${encodeURIComponent(runId)}`, { method: 'DELETE' }),
};
