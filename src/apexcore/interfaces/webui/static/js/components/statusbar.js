// Statusbar: live статус + температура + версия.

import { state, subscribe } from '../store.js';
import { fmtNum } from '../format.js';

export function renderStatusbar(host) {
  host.innerHTML = `
    <span class="statusbar__chip" id="sb-chip">idle</span>
    <span id="sb-active">—</span>
    <span class="statusbar__spacer"></span>
    <span id="sb-cpu">CPU —</span>
    <span id="sb-temp">T° —</span>
    <span class="statusbar__spacer"></span>
    <span id="sb-version">v—</span>
  `;
  refreshAll();
  subscribe((ev) => {
    if (ev.type === 'snapshot') refreshLive();
    if (ev.type === 'bench' || ev.type === 'stress') refreshStatus();
    if (ev.type === 'config') refreshVersion();
  });
}

function refreshAll() {
  refreshLive();
  refreshStatus();
  refreshVersion();
}

function refreshLive() {
  const snap = state.lastSnap;
  if (!snap) {
    setText('sb-cpu', 'CPU —');
    setText('sb-temp', 'T° —');
    return;
  }
  setText('sb-cpu', `CPU ${fmtNum(snap.cpu_percent)}%`);
  const temps = snap.temperatures || {};
  const vals = Object.values(temps).filter(v => typeof v === 'number');
  const tmax = vals.length ? Math.max(...vals) : null;
  setText('sb-temp', tmax != null ? `T° ${fmtNum(tmax)}°C` : 'T° —');
}

function refreshStatus() {
  const bench = state.benchStatus;
  const stress = state.stressStatus;
  const chip = document.getElementById('sb-chip');
  const active = document.getElementById('sb-active');
  if (!chip || !active) return;

  if (bench?.running) {
    chip.className = 'statusbar__chip running';
    chip.textContent = 'bench';
    active.textContent = `${bench.job_id ? bench.job_id.slice(0, 8) : ''}…`;
  } else if (stress?.running) {
    chip.className = 'statusbar__chip running';
    chip.textContent = 'stress';
    active.textContent = stress.engine || '';
  } else {
    chip.className = 'statusbar__chip';
    chip.textContent = 'idle';
    active.textContent = '—';
  }
}

function refreshVersion() {
  const v = state.config?.version;
  if (v) setText('sb-version', `ApexCore v${v}`);
}

function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}
