// ApexCore Installer — главный controller.
//
// Берёт bridge (WebView2 / FastAPI / mock), монтирует shell, рулит
// переходами между 6 шагами.

import { createBridge } from './bridge.js';
import { initTheme, getTheme } from './theme.js';
import {
  el,
  renderWindowChrome,
  renderStepRail,
  renderFooterBar,
  INSTALLER_STEPS,
} from './components.js';
import { createBackground } from './background.js';

import { renderWelcome } from './steps/welcome.js';
import { renderLicense } from './steps/license.js';
import { renderLocation } from './steps/location.js';
import { renderComponents } from './steps/components.js';
import { renderProgress } from './steps/progress.js';
import { renderDone } from './steps/done.js';

const RENDERERS = {
  welcome:    renderWelcome,
  license:    renderLicense,
  location:   renderLocation,
  components: renderComponents,
  progress:   renderProgress,
  done:       renderDone,
};

/** Глобальное состояние мастера. */
const state = {
  step: 'welcome',
  bridge: null,
  installPath: null,
  licenseAccepted: true,
  finishChoices: null,
  installStarted: false,
  installComplete: false,
  progressLog: null,
};

let host = null;     // root для всего инсталлера
let bodyHost = null; // body = step-rail + content
let canvas = null;   // step canvas (область рендера шагов)
let footerHost = null;

// ─── Bootstrap ─────────────────────────────────────────────────────────────

function bootstrap() {
  initTheme('dark');
  state.bridge = createBridge();

  host = document.body;
  // shell
  const shell = el('div', { class: 'installer-shell installer-shell--full' });
  shell.appendChild(renderWindowChrome({
    title: `ApexCore ${state.bridge.version} Setup`,
    onWindowAction: (action) => state.bridge.windowAction(action),
  }));
  bodyHost = el('div', { class: 'installer-body' });
  shell.appendChild(bodyHost);

  host.appendChild(shell);

  state.advance = advance;
  state.updateFooter = renderFooter;
  state.goBack = goBack;

  // Когда FastAPI bridge определит реальную platform (через probe-env),
  // welcome/location/components могут показать другую OS-инфо. Перерисуем
  // текущий шаг — это безопасно даже на welcome (нет state'а ввода кроме
  // licenseAccepted, который мы не теряем).
  window.addEventListener('bridge:platform-changed', () => {
    // Только если мы на welcome/license/location/components (где textы
    // зависят от platform). Progress/done — не зависят.
    if (['welcome', 'license', 'location', 'components'].includes(state.step)) {
      renderStep();
    }
  });

  renderAll();
}

function renderAll() {
  // Полная пересборка: rail + content (canvas) + footer
  bodyHost.innerHTML = '';
  bodyHost.appendChild(renderStepRail({ currentStep: state.step, version: state.bridge.version, platform: state.bridge?.platform }));
  const content = el('div', { class: 'installer-content' });
  canvas = el('div', { class: 'step-canvas' });
  content.appendChild(canvas);
  footerHost = el('div', { class: 'footer-host' });
  content.appendChild(footerHost);
  bodyHost.appendChild(content);

  renderStep();
  renderFooter();
}

function renderStep() {
  canvas.innerHTML = '';
  const r = RENDERERS[state.step];
  if (!r) {
    canvas.appendChild(el('div', { text: `Unknown step: ${state.step}` }));
    return;
  }
  const { node, hasBackground } = r({ state });
  if (hasBackground) {
    canvas.appendChild(createBackground({ score: 9412 }));
  }
  canvas.appendChild(node);
}

function renderFooter() {
  footerHost.innerHTML = '';

  const idx = INSTALLER_STEPS.findIndex((s) => s.key === state.step);
  const isFirst = idx === 0;
  const isProgress = state.step === 'progress';
  const isDone = state.step === 'done';

  let nextLabel = 'Далее';
  if (state.step === 'components') nextLabel = 'Установить';
  if (isProgress) nextLabel = 'Подождите…';
  if (isDone) nextLabel = 'Завершить';

  const nextDisabled = isProgress
    || (state.step === 'license' && state.licenseAccepted !== true);

  const footer = renderFooterBar({
    onCancel: () => {
      if (confirm('Прервать установку?')) {
        state.bridge?.windowAction?.('close');
      }
    },
    onBack: goBack,
    onNext: isDone ? handleFinish : advanceNext,
    nextLabel,
    backDisabled: isFirst || isProgress || isDone,
    nextDisabled,
    footnote: isProgress ? 'установка не прерывается · отмена недоступна' : null,
    nextPrimary: !isProgress,
  });
  footerHost.appendChild(footer);
}

// ─── Navigation ────────────────────────────────────────────────────────────

function advance(targetKey) {
  if (!RENDERERS[targetKey]) return;
  state.step = targetKey;
  renderAll();
}

function advanceNext() {
  const idx = INSTALLER_STEPS.findIndex((s) => s.key === state.step);
  if (idx < 0 || idx >= INSTALLER_STEPS.length - 1) return;
  const next = INSTALLER_STEPS[idx + 1];
  advance(next.key);
}

function goBack() {
  const idx = INSTALLER_STEPS.findIndex((s) => s.key === state.step);
  if (idx <= 0) return;
  const prev = INSTALLER_STEPS[idx - 1];
  advance(prev.key);
}

async function handleFinish() {
  // WebView2 (Windows-bootstrapper) сам закрывает окно по finish. В обычном
  // браузере (Astra first-run wizard) окна-хоста нет: без явного экрана клик
  // «Завершить» выглядит как немой тупик («кнопка не реагирует»). Поэтому в
  // браузере после finish показываем подтверждение + ссылку на основной UI.
  const isWebView2 = !!(window.chrome && window.chrome.webview);
  try {
    await state.bridge.finish({
      launch_webui: state.finishChoices?.launchWebUI ?? true,
      launch_cli: state.finishChoices?.launchCLI ?? false,
      desktop_shortcut: state.finishChoices?.desktopShortcut ?? false,
      open_readme: state.finishChoices?.openReadme ?? false,
    });
    if (!isWebView2) renderFinished(null);
  } catch (e) {
    console.error('finish failed', e);
    // Даже при ошибке не оставляем немой экран — показываем фидбэк.
    if (!isWebView2) renderFinished(String((e && e.message) || e));
  }
}

// Финальный экран браузерного (Linux) flow: wizard сам не закрывается, а
// основной web UI живёт на том же порту по «/». Даём явное подтверждение и
// кнопку перехода.
function renderFinished(errorMsg) {
  bodyHost.innerHTML = '';
  const wrap = el('div', { class: 'installer-content' });
  const card = el('div', { class: 'step-canvas' });
  if (errorMsg) {
    card.appendChild(el('h1', { text: 'Почти готово' }));
    card.appendChild(el('p', {
      text: 'Настройка сохранена, но финальный шаг вернул ошибку: ' + errorMsg,
    }));
  } else {
    card.appendChild(el('h1', { text: '✓ ApexCore настроен' }));
    card.appendChild(el('p', {
      text: 'Первичная настройка завершена. Можно закрыть эту вкладку.',
    }));
  }
  card.appendChild(el('a', {
    class: 'btn btn-primary',
    href: '/',
    style: { display: 'inline-block', marginTop: '16px', textDecoration: 'none' },
    text: 'Открыть ApexCore',
  }));
  card.appendChild(el('p', {
    style: { marginTop: '20px', opacity: '0.7', fontSize: '13px' },
    text: 'Запустить позже: команда «apexcore» (меню) или «apexcore webui» (этот веб-интерфейс).',
  }));
  wrap.appendChild(card);
  bodyHost.appendChild(wrap);
}

// ─── Entry ─────────────────────────────────────────────────────────────────

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', bootstrap);
} else {
  bootstrap();
}
