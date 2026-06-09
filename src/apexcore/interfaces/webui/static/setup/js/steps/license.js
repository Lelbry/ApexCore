// 02 · Лицензия — пользовательское соглашение ApexCore (Вариант A: MIT + EULA).
//
// ВАЖНО (синхронизация при bump версии):
// - Версия в шапке EULA берётся из state.bridge.version (через meta-тег
//   apexcore-version в index.html, который патчится build_windows.ps1 во время
//   сборки). Менять текст соглашения вручную для версии НЕ нужно — она
//   подставляется автоматически.
// - При смене лицензии (например, MIT → Apache 2.0): меняй EULA_TEXT здесь
//   И сам файл LICENSE в корне репо.
// - Список компонентов третьих лиц в п.5 — синхронизировать с NOTICE.md
//   при добавлении/удалении зависимостей.
//
// ИСТОЧНИК (правда): src/apexcore/interfaces/webui/static/setup/js/steps/license.js
// (этот файл). При build_windows.ps1 содержимое каталога setup/ копируется в
// packaging/windows/bootstrapper/Resources/wwwroot/ — НЕ редактируй файл в
// destination, изменения снесутся при следующей сборке.

import { el, renderCheckbox, sectionTag } from '../components.js';

function buildEulaText(version) {
  return `ПОЛЬЗОВАТЕЛЬСКОЕ СОГЛАШЕНИЕ ApexCore v${version}

1. НАЗНАЧЕНИЕ
   ApexCore — программное обеспечение для оценки производительности
   компьютерных систем. Распространяется бесплатно по лицензии MIT
   (см. файл LICENSE в каталоге установки).

2. ИСПОЛЬЗОВАНИЕ
   Разрешено свободное использование в личных, исследовательских,
   образовательных и коммерческих целях с сохранением copyright-
   уведомления автора в производных работах.

3. ОТКАЗ ОТ ОТВЕТСТВЕННОСТИ
   ПО предоставляется «КАК ЕСТЬ», без каких-либо гарантий, явных или
   подразумеваемых. Стресс-тесты нагружают CPU / оперативную память /
   накопители на 100 % мощности — автор НЕ НЕСЁТ ОТВЕТСТВЕННОСТИ
   за перегрев, износ или повреждение оборудования, потерю данных
   и любые косвенные убытки. Использование на критически важных
   системах (медицинских, авиационных, серверах в production)
   НЕ РЕКОМЕНДУЕТСЯ.

4. ДРАЙВЕРЫ И СЛУЖБЫ
   Установка опционально включает:
   • kernel-драйвер PawnIO (от namazso, WHQL-подпись Microsoft) —
     для чтения CPU-температуры и напряжений через MSR / PCI / SuperIO;
   • службу apexcore_sensord (LocalSystem, autostart) — для UAC-free
     доступа к сенсорам при каждом запуске.
   Оба компонента удаляются штатным деинсталлятором.

5. КОМПОНЕНТЫ ТРЕТЬИХ ЛИЦ
   В состав дистрибутива включены:
   • LibreHardwareMonitor (MPL-2.0)
   • .NET 9 Runtime (MIT, Microsoft)
   • PawnIO (MIT)
   • smartmontools (GPL-2.0+, внешний исполняемый файл)
   • stress-ng (GPL-2.0+, опционально, внешний исполняемый файл)
   Полный список с текстами лицензий — файл NOTICE.md в каталоге
   установки.

6. ПЕРСОНАЛЬНЫЕ ДАННЫЕ
   ApexCore не собирает, не передаёт и не отправляет какие-либо
   данные на внешние серверы. Все результаты замеров хранятся
   локально (%APPDATA%\\apexcore\\apexcore.sqlite3).

Установка ПО означает согласие с условиями данного соглашения.`;
}

export function renderLicense({ state }) {
  const root = el('div', { class: 'step step-license' });
  root.appendChild(sectionTag('// 02 · ЛИЦЕНЗИЯ'));

  root.appendChild(el('h2', { class: 'h2', text: 'Лицензионное соглашение.' }));
  root.appendChild(el('p', {
    class: 'p',
    text: 'ApexCore распространяется бесплатно по лицензии MIT. Полный текст лицензии — в файле LICENSE в каталоге установки. Прочитайте соглашение и подтвердите согласие для продолжения.',
  }));

  const version = state.bridge?.version || '0.0.0';
  const textBox = el('div', { class: 'step-license__text' });
  textBox.appendChild(document.createTextNode(buildEulaText(version)));
  textBox.style.whiteSpace = 'pre-wrap';
  root.appendChild(textBox);

  const acceptCb = renderCheckbox({
    on: state.licenseAccepted ?? false,
    label: 'Я принимаю условия лицензионного соглашения',
    sub: 'Без принятия соглашения установка невозможна.',
    onChange: (v) => {
      state.licenseAccepted = v;
      state.updateFooter?.();
    },
  });
  const acceptWrap = el('div', { class: 'step-license__accept' }, [acceptCb]);
  root.appendChild(acceptWrap);

  return { node: root, hasBackground: false };
}
