// 04 · Components
import { el, sectionTag, renderCheckbox } from '../components.js';

const COMPONENTS_WINDOWS = [
  { label: 'ApexCore core · CLI + Web UI',                sub: 'Python-пакет apexcore, веб-интерфейс на 127.0.0.1:8765 · 92 MB' },
  { label: 'LibreHardwareMonitorLib · DLL',               sub: 'Прямое чтение CPU/GPU/материнки через LHM · 18 MB' },
  { label: 'PawnIO kernel driver',                        sub: 'MSR-доступ без admin-прав каждый запуск · 4 MB · UAC при установке' },
  { label: 'apexcore_sensord (Windows service)',          sub: 'Фоновый сервис для multi-process SHM · 12 MB' },
  { label: 'Контекстное меню «Запустить бенчмарк»',       sub: 'Правый клик по ярлыку ApexCore в проводнике · 0.2 MB' },
];

const COMPONENTS_LINUX = [
  { label: 'ApexCore core · CLI + Web UI',                sub: '/opt/apexcore/.venv с numpy/scipy/pydantic · 92 MB' },
  { label: 'stress-ng + sysbench (apt-зависимости)',      sub: 'Эталонные стресс-движки для CPU/RAM · ~10 MB' },
  { label: 'lm-sensors + smartmontools',                  sub: 'CPU/материнка + T° дисков NVMe/SATA · ~5 MB' },
  { label: 'capability'+'’'+'ы для smartctl и dmidecode', sub: 'setcap cap_sys_rawio+ep — pkexec на Progress-шаге' },
  { label: 'Иконка в меню приложений',                    sub: '/usr/share/applications/apexcore.desktop · 0.1 MB' },
];

export function renderComponents({ state }) {
  const root = el('div', { class: 'step step-components' });
  root.appendChild(sectionTag('// 04 · КОМПОНЕНТЫ'));

  const isLinux = state.bridge?.platform === 'linux';
  const list = isLinux ? COMPONENTS_LINUX : COMPONENTS_WINDOWS;
  const total = isLinux ? '~107 MB' : '126.2 MB';

  root.appendChild(el('h2', { class: 'h2', text: 'Состав установки.' }));
  root.appendChild(el('p', {
    class: 'p',
    text: 'Все компоненты обязательны для корректной работы ApexCore — без них часть функций (silicon-quality сенсоры, MSR-доступ, фоновый сервис) станет недоступной. Состав зафиксирован и не настраивается пользователем.',
  }));

  const cont = el('div', { class: 'step-components__list' });
  for (let i = 0; i < list.length; i++) {
    if (i > 0) cont.appendChild(el('div', { class: 'step-components__divider' }));
    cont.appendChild(renderCheckbox({
      on: true,
      locked: true,
      label: list[i].label,
      sub: list[i].sub,
    }));
  }
  root.appendChild(cont);

  // Summary callout (score-styled)
  const summary = el('div', { class: 'step-components__summary' });
  summary.appendChild(el('div', { class: 'step-components__summary-left' }, [
    el('div', { class: 'step-components__summary-left-title', text: '// СУММАРНО' }),
    el('div', { class: 'step-components__summary-left-text', text: `${list.length} компонентов · полный набор` }),
  ]));
  summary.appendChild(el('div', { class: 'step-components__summary-right' }, [
    el('div', { class: 'step-components__summary-value', text: total }),
    el('div', { class: 'step-components__summary-meta', text: 'на диске' }),
  ]));
  root.appendChild(summary);

  return { node: root, hasBackground: false };
}
