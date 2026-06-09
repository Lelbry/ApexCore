// Минимальный observable store + localStorage persistence.
//
// state.live      — последние ~120 тиков MetricSnapshot (для KPI sparkline)
// state.lastSnap  — последний тик целиком
// state.system    — кеш ответа /api/system (запрашивается при бутстрапе)
// state.config    — кеш ответа /api/config
// state.benchStatus / state.stressStatus — кеш статусов

const MAX_LIVE_POINTS = 120;

export const state = {
  live: {
    labels: [],
    cpu: [],
    ram: [],
    tcpu: [],
    tgpu: [],
    tmax: [],
    freq: [],
    powerW: [],
  },
  lastSnap: null,
  system: null,
  config: null,
  benchStatus: null,
  stressStatus: null,
};

const listeners = new Set();

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function emit(event) {
  for (const fn of listeners) {
    try { fn(event); } catch (err) { console.error('store listener error', err); }
  }
}

export function pushSnapshot(snap) {
  state.lastSnap = snap;
  const ts = new Date(snap.timestamp).toLocaleTimeString();
  pushCapped(state.live.labels, ts);
  pushCapped(state.live.cpu, snap.cpu_percent);
  pushCapped(state.live.ram, snap.ram_percent);
  pushCapped(state.live.freq, snap.frequencies?.cpu_avg ?? null);
  pushCapped(state.live.powerW, snap.power_w ?? null);

  const temps = snap.temperatures || {};
  const vals = Object.values(temps).filter(v => typeof v === 'number');
  const tmax = vals.length ? Math.max(...vals) : null;
  pushCapped(state.live.tmax, tmax);

  let tcpu = null, tgpu = null;
  for (const [k, v] of Object.entries(temps)) {
    const lk = k.toLowerCase();
    const isCpu = lk.startsWith('cpu/') || lk.includes('cpu_package')
                  || (lk.includes('cpu') && !lk.includes('gpu'));
    const isGpu = lk.startsWith('gpunvidia/') || lk.startsWith('nvml/')
                  || lk.startsWith('gpuamd/') || lk.startsWith('gpuintel/')
                  || lk.includes('gpu');
    if (isCpu && (tcpu == null || v > tcpu)) tcpu = v;
    if (isGpu && (tgpu == null || v > tgpu)) tgpu = v;
  }
  pushCapped(state.live.tcpu, tcpu);
  pushCapped(state.live.tgpu, tgpu);

  emit({ type: 'snapshot' });
}

export function setSystem(system) {
  state.system = system;
  emit({ type: 'system' });
}

export function setConfig(config) {
  state.config = config;
  emit({ type: 'config' });
}

export function setBenchStatus(s) {
  state.benchStatus = s;
  emit({ type: 'bench' });
}

export function setStressStatus(s) {
  state.stressStatus = s;
  emit({ type: 'stress' });
}

// ─── helpers ────────────────────────────────────────────────────────

function pushCapped(arr, val) {
  arr.push(val);
  if (arr.length > MAX_LIVE_POINTS) arr.shift();
}

export { MAX_LIVE_POINTS };
