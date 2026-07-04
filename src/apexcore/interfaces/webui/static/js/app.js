// ApexCore Web UI — точка входа.
//
// Hash-based SPA router. На каждом изменении hash:
// - находит экран в маршруте,
// - вызывает dispose() предыдущего (если был),
// - вызывает render(contentHost) нового.
//
// WebSocket /ws/metrics стартует один раз и кормит store.pushSnapshot.
// При разрыве — показывает полноэкранный overlay (см. ws.js + chrome.css).

import { api } from './api.js';
import { state, setSystem, setConfig, setBenchStatus, setStressStatus, pushSnapshot } from './store.js';
import { MetricsSocket } from './ws.js';
import { initTheme } from './theme.js';

import { renderTopbar } from './components/topbar.js';
import { renderSidebar } from './components/sidebar.js';
import { renderStatusbar } from './components/statusbar.js';

import * as dashboard from './screens/dashboard.js';
import * as sensors from './screens/sensors.js';
import * as stress from './screens/stress.js';
import * as general from './screens/general.js';
import * as gpu from './screens/gpu.js';
import * as cpuAdvanced from './screens/cpu_advanced.js';
import * as ramcache from './screens/ramcache.js';
import * as winsat from './screens/winsat.js';
import * as history from './screens/history.js';
import * as diagnose from './screens/diagnose.js';
import * as settings from './screens/settings.js';

const ROUTES = {
  '#dashboard':    dashboard,
  '#sensors':      sensors,
  '#stress':       stress,
  '#general':      general,
  '#gpu':          gpu,
  '#cpu-advanced': cpuAdvanced,
  '#ramcache':     ramcache,
  '#winsat':       winsat,
  '#history':      history,
  '#diagnose':     diagnose,
  '#settings':     settings,
};

let currentScreen = null;

async function mountChrome() {
  renderTopbar(document.querySelector('.topbar'));
  renderSidebar(document.querySelector('.sidebar'));
  renderStatusbar(document.querySelector('.statusbar'));
}

async function navigate() {
  const hash = window.location.hash || '#dashboard';
  const route = ROUTES[hash] || ROUTES['#dashboard'];
  const host = document.querySelector('.content');
  if (!host) return;

  if (currentScreen?.dispose) {
    try { currentScreen.dispose(); } catch (e) { console.warn('dispose error', e); }
  }
  currentScreen = route;
  host.innerHTML = '';
  try {
    await route.render(host);
  } catch (err) {
    host.innerHTML = `<div class="stub">
      <div class="stub__icon">⚠</div>
      <div class="stub__title">Ошибка рендера экрана</div>
      <div class="stub__body">${escapeHtml(err.message)}</div>
    </div>`;
    console.error('screen render error', err);
  }
}

// ─── WebSocket disconnect overlay ────────────────────────────────────

function showDisconnect(detail) {
  let el = document.getElementById('ws-disconnect');
  if (!el) {
    el = document.createElement('div');
    el.id = 'ws-disconnect';
    el.className = 'ws-disconnect';
    document.body.appendChild(el);
  }
  const isCold = detail.reason === 'cold-start';
  const lastConn = detail.lastConnectedAt
    ? new Date(detail.lastConnectedAt).toLocaleTimeString('ru-RU')
    : '—';
  el.innerHTML = `
    <div class="ws-disconnect__card">
      <div class="ws-disconnect__title">${isCold ? 'ApexCore service is not running' : 'ApexCore перестал отвечать'}</div>
      <div class="ws-disconnect__body">
        ${isCold
          ? 'Не удалось подключиться к локальному сервису. Запустите его в терминале:'
          : 'Соединение с локальным сервисом оборвалось. Скорее всего процесс закрыт. Запустите его снова:'}
        <div class="ws-disconnect__cmd">apexcore webui</div>
        После запуска нажмите кнопку ниже — UI попробует подключиться заново.
        ${detail.lastConnectedAt ? `<div class="ws-disconnect__hint">Последнее соединение: ${lastConn}</div>` : ''}
      </div>
      <button class="btn primary" id="btn-ws-retry">Retry connection</button>
    </div>
  `;
  el.style.display = 'flex';
  document.getElementById('btn-ws-retry').addEventListener('click', () => {
    el.style.display = 'none';
    bootstrap();
  });
}

function hideDisconnect() {
  const el = document.getElementById('ws-disconnect');
  if (el) el.style.display = 'none';
}

// ─── Bootstrap ──────────────────────────────────────────────────────

let socket = null;
let wsSoftRetryTried = false;

async function bootstrap() {
  try {
    const [cfg, system] = await Promise.allSettled([api.getConfig(), api.getSystem()]);
    if (cfg.status === 'fulfilled') setConfig(cfg.value);
    if (system.status === 'fulfilled') setSystem(system.value);
  } catch (err) {
    console.error('bootstrap REST failed', err);
  }

  // Periodic background status (для statusbar chip + dashboard).
  setInterval(async () => {
    try {
      const [b, s] = await Promise.allSettled([api.benchStatus(), api.stressStatus()]);
      if (b.status === 'fulfilled') setBenchStatus(b.value);
      if (s.status === 'fulfilled') setStressStatus(s.value);
    } catch { /* ignore */ }
  }, 3000);

  // WS.
  if (socket) socket.close();
  wsSoftRetryTried = false;
  socket = new MetricsSocket();
  socket.addEventListener('open', () => {
    wsSoftRetryTried = false;  // успешный connect → сброс флага
    hideDisconnect();
    // Бейдж WS = состояние соединения (не наличие данных): «live» сразу на
    // connect, не дожидаясь первого снимка. См. topbar.js setWs.
    window.dispatchEvent(new CustomEvent('ws:status', { detail: { connected: true } }));
  });
  socket.addEventListener('message', (e) => pushSnapshot(e.detail));
  socket.addEventListener('down', (e) => {
    window.dispatchEvent(new CustomEvent('ws:status', { detail: { connected: false } }));
    // Soft retry: при первом 'down' (особенно после F5 страницы — браузер
    // закрыл старый WS, новый ещё не успел соединиться, появляется ложный
    // cold-start) делаем ровно одну попытку через 2 сек до показа overlay.
    // Архитектурный комментарий ws.js (no auto-reconnect в low-level
    // wrapper) сохраняется — retry-логика **здесь, в bootstrap-layer**.
    if (!wsSoftRetryTried) {
      wsSoftRetryTried = true;
      setTimeout(() => {
        if (socket && socket.ws && socket.ws.readyState !== WebSocket.OPEN) {
          socket.connect();
        }
      }, 2000);
      return;
    }
    showDisconnect(e.detail);
  });
  socket.connect();
}

function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[c]);
}

// ─── Go ─────────────────────────────────────────────────────────────

window.addEventListener('hashchange', navigate);
window.addEventListener('DOMContentLoaded', async () => {
  // Inline-script в index.html уже выставил data-theme из localStorage
  // (anti-FOUC). initTheme повторно нормализует значение и нотифицирует
  // подписчиков theme:change — это нужно topbar-кнопке для корректной
  // подсветки иконки солнца/луны.
  initTheme('dark');
  await mountChrome();
  await navigate();
  await bootstrap();
});
