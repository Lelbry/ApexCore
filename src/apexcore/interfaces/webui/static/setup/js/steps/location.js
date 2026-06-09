// 03 · Location
import { el, sectionTag, secondaryBtn } from '../components.js';

export function renderLocation({ state }) {
  const root = el('div', { class: 'step step-location' });
  root.appendChild(sectionTag('// 03 · ПАПКА УСТАНОВКИ'));

  const platform = state.bridge?.platform === 'linux' ? 'linux' : 'windows';
  const defaultPath = state.installPath
    || (platform === 'linux' ? '/opt/apexcore' : 'C:\\Program Files\\ApexCore');
  state.installPath = defaultPath;

  root.appendChild(el('h2', { class: 'h2', text: 'Выберите место установки.' }));
  const p = el('p', { class: 'p' });
  p.textContent = platform === 'linux'
    ? 'ApexCore установлен через .deb в /opt/apexcore/. Это путь чтения; перенос требует переустановки пакета.'
    : 'ApexCore не использует системные пути. Все файлы будут в выбранной папке — можно удалить вручную.';
  root.appendChild(p);

  // Path label + input + browse
  root.appendChild(el('div', {
    class: 'step-location__path-label',
    text: '// ПУТЬ УСТАНОВКИ',
  }));

  const input = el('input', {
    class: 'text-input',
    type: 'text',
    value: defaultPath,
  });
  // На Linux путь read-only (deb уже распакован)
  if (platform === 'linux') input.readOnly = true;
  input.addEventListener('input', (ev) => { state.installPath = ev.target.value; });

  const browseBtn = secondaryBtn('Обзор…', async () => {
    if (!state.bridge?.browse) return;
    const next = await state.bridge.browse(state.installPath);
    if (next) {
      input.value = next;
      state.installPath = next;
      // input.value = ... программно не фаярит 'change'; зовём руками.
      refreshDiskStats(next);
    }
  });
  if (platform === 'linux') browseBtn.disabled = true;

  root.appendChild(el('div', { class: 'step-location__path-row' }, [input, browseBtn]));

  // Stats grid 2×2 — initial placeholder + live probe.
  const REQUIRED_MB = 140;
  const initialStats = state.location_stats || {
    available: '…',
    required: `~${REQUIRED_MB} MB`,
    after: '…',
    disk: platform === 'linux' ? '/dev/nvme0n1' : 'C:',
  };
  const tiles = [
    { key: 'available', label: 'Доступно на диске', cls: '' },
    { key: 'required',  label: 'Требуется',         cls: 'step-location__stat-value--score' },
    { key: 'after',     label: 'После установки',   cls: 'step-location__stat-value--dim' },
    { key: 'disk',      label: 'Диск',              cls: 'step-location__stat-value--dim' },
  ];
  const grid = el('div', { class: 'step-location__stats' });
  const valueNodes = {};
  for (const t of tiles) {
    const valueNode = el('div', { class: `step-location__stat-value ${t.cls}`.trim(), text: initialStats[t.key] });
    valueNodes[t.key] = valueNode;
    grid.appendChild(el('div', { class: 'step-location__stat' }, [
      el('div', { class: 'step-location__stat-label', text: t.label }),
      valueNode,
    ]));
  }
  root.appendChild(grid);

  async function refreshDiskStats(path) {
    if (!state.bridge?.probeDisk) return;
    try {
      const info = await state.bridge.probeDisk(path);
      if (!info || typeof info.available_gb !== 'number') return;
      const availGb = info.available_gb;
      const totalGb = info.total_gb;
      const afterGb = Math.max(availGb - REQUIRED_MB / 1024, 0);
      const fmt = (gb) => gb >= 100 ? `${Math.round(gb)} GB` : `${gb.toFixed(1)} GB`;
      state.location_stats = {
        available: `${fmt(availGb)} / ${fmt(totalGb)}`,
        required: `~${REQUIRED_MB} MB`,
        after: fmt(afterGb),
        disk: info.root ? info.root.replace(/\\$/, '') + (info.fs ? ` (${info.fs})` : '') : 'C:',
      };
      for (const k of Object.keys(valueNodes)) {
        valueNodes[k].textContent = state.location_stats[k];
      }
    } catch (e) {
      console.warn('[probeDisk]', e);
    }
  }
  refreshDiskStats(defaultPath);
  input.addEventListener('change', (ev) => refreshDiskStats(ev.target.value));

  // Warning callout
  const warningText = platform === 'linux'
    ? 'На Astra Linux несколько шагов настройки (sensors-detect, setcap для smartctl/dmidecode) требуют прав root — wizard вызовет pkexec при подтверждении.'
    : 'Папка должна быть доступна на запись. Для системных путей нужны права администратора — инсталлятор перезапустится с UAC-запросом.';
  root.appendChild(el('div', { class: 'callout step-location__warning' }, [
    el('div', { class: 'callout__title', text: '// ВНИМАНИЕ' }),
    el('div', { class: 'callout__body', text: warningText }),
  ]));

  return { node: root, hasBackground: false };
}
