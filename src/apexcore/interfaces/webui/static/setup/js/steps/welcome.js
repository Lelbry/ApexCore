// 01 · Welcome
import { el, sectionTag } from '../components.js';

const INCLUDED = [
  'Веб-интерфейс на 127.0.0.1:8765 — графики, история, экспорт',
  'CLI apexcore + TUI меню — для скриптов и автоматизации',
  'Драйверы LHM / PawnIO для прямого чтения сенсоров',
];

export function renderWelcome({ state }) {
  const root = el('div', { class: 'step step-welcome' });
  root.appendChild(sectionTag('// 01 · ДОБРО ПОЖАЛОВАТЬ'));

  root.appendChild(el('h1', { class: 'h1', text: 'Установка ApexCore.' }));

  const platform = state.bridge?.platform === 'linux' ? 'Astra Linux' : 'Windows 11';
  const p = el('p', { class: 'p p--large' });
  p.innerHTML = `Локальный инструмент бенчмаркинга и thermal-стресса с прозрачной системой оценки на базе ` +
    `<span class="p--accent">Roofline-модели</span>. Целевая ОС: <span class="p--accent">${platform}</span>. ` +
    `Установка займёт около <span class="p--accent">30 секунд</span>.`;
  root.appendChild(p);

  const included = el('div', { class: 'step-welcome__included' });
  included.appendChild(el('span', { class: 'step-welcome__included-title', text: '// ЧТО ВКЛЮЧЕНО' }));
  for (const text of INCLUDED) {
    included.appendChild(el('div', { class: 'step-welcome__included-row' }, [
      el('span', { class: 'check', text: '✓' }),
      el('span', { text }),
    ]));
  }
  root.appendChild(included);

  root.appendChild(el('div', {
    class: 'step-welcome__footnote',
    text: 'работает офлайн · single-user',
  }));

  return { node: root, hasBackground: true };
}
