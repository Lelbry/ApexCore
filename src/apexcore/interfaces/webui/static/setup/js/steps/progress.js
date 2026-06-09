// 05 · Progress
import { el, sectionTag } from '../components.js';

const DEFAULT_LOG = [
  { mark: '·', text: 'Ожидание старта…',                       state: 'pending' },
  { mark: '·', text: 'Создание целевого каталога',             state: 'pending' },
  { mark: '·', text: 'Распаковка Python runtime',              state: 'pending' },
  { mark: '·', text: 'Установка пакета apexcore',              state: 'pending' },
  { mark: '·', text: 'Регистрация драйверов / сервисов',       state: 'pending' },
  { mark: '·', text: 'Создание ярлыков и записей в реестре',   state: 'pending' },
];

export function renderProgress({ state }) {
  const root = el('div', { class: 'step step-progress' });
  root.appendChild(sectionTag('// 05 · УСТАНОВКА'));
  root.appendChild(el('h2', { class: 'h2', text: 'Идёт установка…' }));
  root.appendChild(el('p', {
    class: 'p',
    text: 'Не закрывайте окно. Если требуется UAC — подтвердите запрос Windows / pkexec на Linux.',
  }));

  // Progress bar
  const barHead = el('div', { class: 'step-progress__bar-head' }, [
    el('span', { class: 'step-progress__bar-head-label', text: '// PROGRESS' }),
    el('span', { class: 'step-progress__bar-head-value' }, [
      el('span', { id: 'progress-pct-num', text: '0' }),
      el('span', { class: 'pct', text: '%' }),
    ]),
  ]);
  root.appendChild(barHead);
  const bar = el('div', { class: 'step-progress__bar' });
  const fill = el('div', { class: 'step-progress__bar-fill' });
  fill.id = 'progress-fill';
  bar.appendChild(fill);
  for (const p of [25, 50, 75]) {
    const tick = el('div', { class: 'step-progress__bar-tick' });
    tick.style.left = `${p}%`;
    bar.appendChild(tick);
  }
  root.appendChild(bar);

  // Cards
  const cards = el('div', { class: 'step-progress__cards' });
  const stepCard = el('div', { class: 'step-progress__card' }, [
    el('div', { class: 'step-progress__card-label', text: '// ТЕКУЩИЙ ШАГ' }),
    el('div', {
      class: 'step-progress__card-value step-progress__card-value--accent',
      id: 'progress-step-value',
      text: 'Ожидание…',
    }),
    el('div', {
      class: 'step-progress__card-meta',
      id: 'progress-step-meta',
      text: 'шаг 0 из 6',
    }),
  ]);
  const timeCard = el('div', { class: 'step-progress__card' }, [
    el('div', { class: 'step-progress__card-label', text: '// ВРЕМЯ' }),
    el('div', {
      class: 'step-progress__card-value step-progress__card-value--mono',
      id: 'progress-time-value',
      text: '00:00',
    }),
    el('div', { class: 'step-progress__card-meta', text: 'elapsed · remaining' }),
  ]);
  cards.appendChild(stepCard);
  cards.appendChild(timeCard);
  root.appendChild(cards);

  // Log
  const log = el('div', { class: 'step-progress__log', id: 'progress-log' });
  const lines = state.progressLog || DEFAULT_LOG;
  for (const line of lines) {
    log.appendChild(makeLogRow(line));
  }
  root.appendChild(log);

  // Subscribe to bridge progress
  let startTs = 0;
  let stepIndex = 0;
  let lastStepName = null;
  let lastPct = 0;
  // Самотикающий elapsed: UI должен показывать живое время даже если bridge
  // молчит (например пока tail ждёт Inno-лог).
  startTs = Date.now();
  const tickInterval = setInterval(() => {
    if (state.installComplete) return;
    const elapsed = (Date.now() - startTs) / 1000;
    const remaining = lastPct > 5 ? elapsed * (100 - lastPct) / Math.max(lastPct, 1) : null;
    const el = document.getElementById('progress-time-value');
    if (el) {
      el.innerHTML = `${fmtTime(elapsed)}` +
        (remaining != null ? `<span class="step-progress__card-meta" style="margin-left:4px">· ещё ~${fmtTime(remaining, true)}</span>` : '');
    }
  }, 500);
  const onProgress = (ev) => {
    // Percent — apply только если событие реально его передало (heartbeat
    // событие шлёт только elapsed, без percent — не сбрасываем бар на 0).
    if (typeof ev.percent === 'number') {
      const pct = Math.max(0, Math.min(100, ev.percent));
      lastPct = pct;
      fill.style.width = `${pct}%`;
      document.getElementById('progress-pct-num').textContent = String(Math.round(pct));
    }
    const pct = lastPct;

    // Current step
    if (ev.step) {
      document.getElementById('progress-step-value').textContent = ev.step;
      if (ev.step !== lastStepName) {
        lastStepName = ev.step;
        stepIndex = Math.min(6, stepIndex + 1);
      }
      document.getElementById('progress-step-meta').textContent = `шаг ${stepIndex} из 6`;
    }

    // Time
    const elapsed = ev.elapsed_sec ?? ((Date.now() - startTs) / 1000);
    const remaining = ev.remaining_sec ?? (pct > 5 ? elapsed * (100 - pct) / Math.max(pct, 1) : null);
    document.getElementById('progress-time-value').innerHTML =
      `${fmtTime(elapsed)}` +
      (remaining != null ? `<span class="step-progress__card-meta" style="margin-left:4px">· ещё ~${fmtTime(remaining, true)}</span>` : '');

    // Log line
    if (ev.log_line) {
      // current → done, append new running
      const rows = log.children;
      // mark current running as done
      for (const row of rows) {
        if (row.classList.contains('step-progress__log-row--running')) {
          row.classList.remove('step-progress__log-row--running');
          row.classList.add('step-progress__log-row--done');
          row.querySelector('.mark').textContent = '✓';
        }
      }
      const newRow = makeLogRow({ mark: '▸', text: ev.log_line, state: 'running' });
      log.appendChild(newRow);
      // Trim to last 7 rows
      while (log.children.length > 7) log.removeChild(log.children[0]);
    }

    // Completion
    if (ev.state === 'done' || pct >= 100) {
      state.installComplete = true;
      clearInterval(tickInterval);
      if (state.advance) state.advance('done');
    }
  };
  state.bridge?.onProgress?.(onProgress);

  // Start install автоматически при входе на шаг (один раз)
  if (!state.installStarted) {
    state.installStarted = true;
    Promise.resolve(state.bridge?.startInstall?.({
      path: state.installPath,
      // addtopath — обязателен, иначе `apexcore menu` из новой PowerShell
      // даёт CommandNotFoundException. Inno [Tasks] чекнутые по дефолту
      // только если их имя в /TASKS=, поэтому передаём явно.
      tasks: ['addtopath', 'pawnio', 'sensord', 'smartmontools'],
      acceptLicense: state.licenseAccepted ?? true,
    })).catch((e) => {
      console.error('install failed', e);
      document.getElementById('progress-step-value').textContent = 'Ошибка установки';
      document.getElementById('progress-step-value').style.color = 'var(--close-hover)';
    });
  }

  return { node: root, hasBackground: false };
}

function makeLogRow({ mark, text, state }) {
  const row = el('div', { class: `step-progress__log-row step-progress__log-row--${state}` });
  row.appendChild(el('span', { class: 'mark', text: mark }));
  row.appendChild(el('span', { class: 'text', text }));
  return row;
}

function fmtTime(sec, short = false) {
  if (sec == null || !isFinite(sec)) return '--:--';
  sec = Math.max(0, sec);
  if (short && sec < 60) return `${Math.round(sec)} c`;
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}
