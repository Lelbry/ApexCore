// Bridge — абстракция нативного API инсталлера.
//
// Цель: один и тот же UI кода ездит и в WebView2 (Windows bootstrapper),
// и в обычном браузере (Astra first-run wizard через FastAPI).
//
// Конкретная реализация выбирается в index.html по `window.IS_WEBVIEW2`
// или наличию `window.chrome.webview` объекта.

/**
 * @typedef {Object} ProgressEvent
 * @property {number} percent       — 0..100
 * @property {string} step          — короткое имя шага («Распаковка PawnIO драйвера…»)
 * @property {string} [log_line]    — последняя строка для лога
 * @property {string} [state]       — 'running' | 'done' | 'error'
 * @property {number} [elapsed_sec] — секунды с начала установки
 * @property {number} [remaining_sec]
 */

/**
 * @typedef {Object} GpuProbeResult
 * @property {string|null} nvidia — модель NVIDIA или null
 * @property {string|null} amd    — модель AMD или null
 * @property {string|null} intel  — Intel iGPU (опционально)
 * @property {string|null} reason — пояснение если GPU не найдена
 */

/**
 * @typedef {Object} BridgeAPI
 * @property {(opts: object) => Promise<void>} startInstall
 * @property {(cb: (e: ProgressEvent) => void) => void} onProgress
 * @property {(defaultPath: string) => Promise<string|null>} browse
 * @property {() => Promise<GpuProbeResult>} probeGpu
 * @property {() => Promise<object>} probeEnvironment
 * @property {(opts: object) => Promise<void>} finish
 * @property {(action: 'minimize'|'maximize'|'close') => void} windowAction
 * @property {(theme: 'dark'|'light') => void} [persistTheme]
 * @property {string} platform — 'windows' | 'linux'
 * @property {string} version  — версия apexcore
 */

let progressListeners = [];

function dispatchProgress(ev) {
  for (const fn of progressListeners) {
    try { fn(ev); } catch (e) { console.warn('progress listener error', e); }
  }
}

/* ─── WebView2 implementation (Windows bootstrapper) ────────────────────── */

function makeWebView2Bridge() {
  const replyMap = new Map();
  let replyId = 0;

  const post = (action, payload = {}) => {
    const id = ++replyId;
    return new Promise((resolve, reject) => {
      replyMap.set(id, { resolve, reject });
      try {
        window.chrome.webview.postMessage(JSON.stringify({ id, action, ...payload }));
      } catch (e) {
        replyMap.delete(id);
        reject(e);
      }
    });
  };

  window.chrome.webview.addEventListener('message', (ev) => {
    let msg;
    try { msg = typeof ev.data === 'string' ? JSON.parse(ev.data) : ev.data; }
    catch { return; }

    if (msg.event === 'progress') {
      dispatchProgress(msg);
      return;
    }
    // Bridge.cs шлёт ответ как { reply: id, data, error } (не { id }).
    const replyId = msg.reply ?? msg.id;
    if (replyId != null && replyMap.has(replyId)) {
      const slot = replyMap.get(replyId);
      replyMap.delete(replyId);
      if (msg.error) slot.reject(new Error(msg.error));
      else slot.resolve(msg.data ?? null);
    }
  });

  return {
    platform: 'windows',
    version: window.__APEXCORE_VERSION__ || '0.0.0',
    startInstall: (opts) => post('startInstall', { options: opts }),
    onProgress: (cb) => { progressListeners.push(cb); },
    browse: (defaultPath) => post('browse', { default: defaultPath }),
    probeGpu: () => post('probeGpu'),
    probeEnvironment: () => post('probeEnvironment'),
    probeDisk: (path) => post('probeDisk', { default: path }),
    finish: (opts) => post('finish', { options: opts }),
    windowAction: (action) => {
      window.chrome.webview.postMessage(JSON.stringify({ action: 'windowAction', value: action }));
    },
    persistTheme: (theme) => {
      window.chrome.webview.postMessage(JSON.stringify({ action: 'persistTheme', value: theme }));
    },
  };
}

/* ─── FastAPI implementation (Astra browser) ────────────────────────────── */

function makeFastApiBridge() {
  const wsUrl = new URL(window.location.href);
  wsUrl.protocol = wsUrl.protocol === 'https:' ? 'wss:' : 'ws:';
  wsUrl.pathname = '/ws/setup';
  wsUrl.search = '';
  wsUrl.hash = '';

  let socket = null;
  const replyMap = new Map();
  let replyId = 0;
  let reconnectTimer = null;

  function connect() {
    socket = new WebSocket(wsUrl.toString());
    socket.addEventListener('open', () => {
      console.log('[bridge] /ws/setup connected');
      if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    });
    socket.addEventListener('message', (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }

      if (msg.event === 'progress') {
        dispatchProgress(msg);
        return;
      }
      if (msg.reply != null && replyMap.has(msg.reply)) {
        const slot = replyMap.get(msg.reply);
        replyMap.delete(msg.reply);
        if (msg.error) slot.reject(new Error(msg.error));
        else slot.resolve(msg.data ?? null);
      }
    });
    socket.addEventListener('close', () => {
      console.warn('[bridge] /ws/setup closed, reconnecting in 2s');
      reconnectTimer = setTimeout(connect, 2000);
    });
    socket.addEventListener('error', (e) => {
      console.error('[bridge] /ws/setup error', e);
    });
  }
  connect();

  const post = (action, payload = {}) => {
    const id = ++replyId;
    return new Promise((resolve, reject) => {
      replyMap.set(id, { resolve, reject });
      if (!socket || socket.readyState !== WebSocket.OPEN) {
        // ждём подключения 2с
        const waitOpen = setInterval(() => {
          if (socket && socket.readyState === WebSocket.OPEN) {
            clearInterval(waitOpen);
            socket.send(JSON.stringify({ id, action, ...payload }));
          }
        }, 100);
        setTimeout(() => {
          clearInterval(waitOpen);
          if (replyMap.has(id)) {
            replyMap.delete(id);
            reject(new Error('WebSocket not ready'));
          }
        }, 5000);
      } else {
        socket.send(JSON.stringify({ id, action, ...payload }));
      }
    });
  };

  // Platform detection — eager fetch /api/setup/probe-env при создании моста.
  // До получения ответа platform остаётся 'linux' (дефолт для .deb-сценария);
  // когда придёт ответ — обновится на 'windows' / 'linux' / 'other'. UI
  // обращается через геттер bridge.platform, так что после await fetch
  // welcome/location/components шаги отображают правильную ОС.
  let _platform = 'linux';
  fetch('/api/setup/probe-env')
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error('probe-env failed'))))
    .then((env) => {
      if (env && env.platform) {
        _platform = String(env.platform).toLowerCase();
        // Сигналим UI что platform определился — шаги могут перерисоваться.
        window.dispatchEvent(new CustomEvent('bridge:platform-changed', {
          detail: { platform: _platform },
        }));
      }
    })
    .catch((err) => console.warn('[bridge] platform probe failed:', err));

  return {
    // Getter — UI читает свежее значение после probe-env ответа.
    get platform() { return _platform; },
    version: document.querySelector('meta[name="apexcore-version"]')?.content || '0.0.0',
    startInstall: (opts) => post('startInstall', { options: opts }),
    onProgress: (cb) => { progressListeners.push(cb); },
    browse: async (defaultPath) => defaultPath, // browser sandbox: нет file dialog
    probeGpu: () => post('probeGpu'),
    probeEnvironment: () => post('probeEnvironment'),
    probeDisk: () => Promise.resolve(null),
    finish: (opts) => post('finish', { options: opts }),
    windowAction: (action) => {
      // в браузере close = window.close(), maximize/minimize игнорируем
      if (action === 'close') {
        post('finish', { options: { cancel: true } }).catch(() => {});
        setTimeout(() => window.close(), 300);
      }
    },
  };
}

/* ─── Mock implementation (для standalone preview через python -m http.server) ─ */

function makeMockBridge() {
  console.info('[bridge] mock mode — нет нативного host\'а, действия логируются в console');
  return {
    platform: 'mock',
    version: '1.0.0-dev',
    startInstall: async (opts) => {
      console.log('[bridge] startInstall (mock)', opts);
      // имитируем 6 шагов прогресса
      const steps = [
        { percent: 5,  step: 'Создание каталога', log_line: 'mkdir C:\\Program Files\\ApexCore' },
        { percent: 20, step: 'Распаковка Python 3.10.13 runtime', log_line: '38 MB' },
        { percent: 45, step: 'Установка пакета apexcore-1.0.0', log_line: '54 MB' },
        { percent: 65, step: 'Установка LibreHardwareMonitorLib.dll', log_line: '18 MB' },
        { percent: 80, step: 'Распаковка PawnIO драйвера', log_line: 'PawnIO.msi' },
        { percent: 95, step: 'Регистрация контекстных меню' },
        { percent: 100, step: 'Готово', state: 'done' },
      ];
      for (const s of steps) {
        await new Promise((r) => setTimeout(r, 500));
        dispatchProgress(s);
      }
    },
    onProgress: (cb) => { progressListeners.push(cb); },
    browse: async (defaultPath) => prompt('Mock browse — путь:', defaultPath) || defaultPath,
    probeDisk: async () => ({ root: 'C:\\', fs: 'NTFS', total_gb: 931.5, available_gb: 412.8 }),
    probeGpu: async () => ({ nvidia: 'NVIDIA RTX 4090 (mock)', amd: null, intel: null, reason: null }),
    probeEnvironment: async () => ({
      python: '3.11.9', blas: 'OpenBLAS', sensors: true, smartctl: true, capabilities: true,
    }),
    finish: async (opts) => { console.log('[bridge] finish (mock)', opts); },
    windowAction: (action) => { console.log('[bridge] windowAction (mock)', action); },
  };
}

/* ─── Factory ───────────────────────────────────────────────────────────── */

export function createBridge() {
  if (window.chrome?.webview) return makeWebView2Bridge();
  if (location.protocol === 'http:' || location.protocol === 'https:') {
    // если хостимся под FastAPI — есть /ws/setup
    if (location.pathname.startsWith('/setup') || location.pathname === '/') {
      return makeFastApiBridge();
    }
  }
  return makeMockBridge();
}
