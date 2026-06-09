// Settings — 5 групп, одна страница со скроллом.
// Изменения порта применяются мгновенно (POST /api/config), но требуют
// перезапуска apexcore webui — об этом explicit alert.

import { api } from '../api.js';
import { state } from '../store.js';
import { toggleExportMenu } from './history.js';
import { getTheme, setTheme } from '../theme.js';

export async function render(host) {
  host.innerHTML = `
    <div class="screen">
      <div class="screen__header">
        <div class="screen__header__title">
          <h1>Settings</h1>
          <span class="screen__header__index">10</span>
        </div>
        <div class="screen__header__sub">
          Локальное приложение, single-user. Изменения сохраняются в
          <code>menu_settings.yaml</code> в папке данных ApexCore и применяются
          при следующем запуске <code>apexcore webui</code>.
        </div>
      </div>

      <div class="settings-stack">

        <div class="settings-card">
          <div class="settings-card__header">
            <h3 class="settings-card__title">Подключение</h3>
            <div class="settings-card__hint">Порт, на котором запускается локальный веб-сервер.</div>
          </div>
          <div class="settings-card__body">
            <div class="settings-row">
              <div>
                <div class="settings-row__label">Текущий URL</div>
                <div class="settings-row__hint">Same-origin для всех REST/WS запросов.</div>
              </div>
              <div class="settings-row__control">
                <input type="text" id="cfg-url" readonly value="${escapeHtml(window.location.origin)}" style="flex:1; max-width: 320px;"/>
                <button class="btn sm" id="btn-copy-url">Копировать</button>
              </div>
            </div>
            <div class="settings-row">
              <div>
                <div class="settings-row__label">Сохранённый порт</div>
                <div class="settings-row__hint">Диапазон 1024-65535. По умолчанию 8765. Применяется при следующем запуске <code>apexcore webui</code>.</div>
              </div>
              <div class="settings-row__control">
                <input type="number" id="cfg-port" min="1024" max="65535" value="8765" style="width: 120px;"/>
                <button class="btn primary" id="btn-save-port">Сохранить порт</button>
              </div>
            </div>
            <div class="settings-row">
              <div>
                <div class="settings-row__label">Хост</div>
                <div class="settings-row__hint">Только локальные адреса (single-user).</div>
              </div>
              <div class="settings-row__control">
                <select id="cfg-host" style="min-width: 200px;">
                  <option value="127.0.0.1">127.0.0.1 (по умолчанию)</option>
                  <option value="localhost">localhost</option>
                  <option value="0.0.0.0">0.0.0.0 (LAN, осторожно)</option>
                </select>
              </div>
            </div>
          </div>
        </div>

        <div class="settings-card">
          <div class="settings-card__header">
            <h3 class="settings-card__title">Внешний вид</h3>
            <div class="settings-card__hint">Тема интерфейса. Сохраняется в браузере (localStorage), применяется мгновенно без перезапуска.</div>
          </div>
          <div class="settings-card__body">
            <div class="settings-row">
              <div class="settings-row__label">Тема</div>
              <div class="settings-row__control">
                <div class="radio-group" id="theme-radio-group">
                  <label><input type="radio" name="theme" value="dark"  ${getTheme() === 'dark'  ? 'checked' : ''}/><span>Тёмная</span></label>
                  <label><input type="radio" name="theme" value="light" ${getTheme() === 'light' ? 'checked' : ''}/><span>Светлая</span></label>
                </div>
                <span class="field__hint">светлая — для скриншотов на печать и яркого окружения</span>
              </div>
            </div>
          </div>
        </div>

        <div class="settings-card">
          <div class="settings-card__header">
            <h3 class="settings-card__title">Сенсоры и диагностика</h3>
            <div class="settings-card__hint">Источник температуры CPU выбирается автоматически из доступных программ-поставщиков (HWiNFO, CoreTemp, AIDA64, Ryzen Master, LibreHardwareMonitor, psutil, WMI). Активный источник можно увидеть в Diagnose.</div>
          </div>
          <div class="settings-card__body">
            <div class="settings-row">
              <div class="settings-row__label">Открыть Diagnose</div>
              <div class="settings-row__control">
                <button class="btn sm" id="btn-open-diagnose" title="Per-subsystem sensor health sweep">Открыть Diagnose</button>
                <span class="field__hint">подробная карта 12 backend-ов температурных сенсоров</span>
              </div>
            </div>
          </div>
        </div>

        <div class="settings-card" id="settings-card-sensord">
          <div class="settings-card__header">
            <h3 class="settings-card__title">Сервис ApexCore</h3>
            <div class="settings-card__hint">Только Windows: фоновая служба для чтения сенсоров без UAC.</div>
          </div>
          <div class="settings-card__body" id="settings-sensord-body">
            <div class="settings-row">
              <div class="settings-row__label">Статус сервиса</div>
              <div class="settings-row__control">
                <span class="field__hint">появится в следующей версии</span>
              </div>
            </div>
          </div>
        </div>

        <div class="settings-card">
          <div class="settings-card__header">
            <h3 class="settings-card__title">Хранилище и данные</h3>
            <div class="settings-card__hint">База данных в папке настроек пользователя.</div>
          </div>
          <div class="settings-card__body">
            <div class="settings-row">
              <div class="settings-row__label">Версия схемы БД</div>
              <div class="settings-row__control"><span class="field__hint">v4</span></div>
            </div>
            <div class="settings-row">
              <div class="settings-row__label">Действия</div>
              <div class="settings-row__control">
                <button class="btn sm" disabled title="Появится в следующей версии">Открыть папку настроек</button>
                <div class="history-export">
                  <button class="btn sm" id="btn-export-all">Экспорт всей истории ▾</button>
                  <div class="history-export__menu" id="menu-export-all">
                    <a href="${api.exportAllUrl('json')}" download>JSON · один файл</a>
                    <a href="${api.exportAllUrl('csv')}" download>CSV · zip-архив</a>
                  </div>
                </div>
                <button class="btn sm danger" disabled title="Появится в следующей версии">Очистить историю</button>
              </div>
            </div>
          </div>
        </div>

        <div class="settings-card">
          <div class="settings-card__header">
            <h3 class="settings-card__title">О программе</h3>
          </div>
          <div class="settings-card__body">
            <div class="settings-row">
              <div class="settings-row__label">Версия</div>
              <div class="settings-row__control"><span class="field__hint" id="about-version">—</span></div>
            </div>
            <div class="settings-row">
              <div class="settings-row__label">Разработчик</div>
              <div class="settings-row__control"><span class="field__hint">Дудкин Александр Владимирович</span></div>
            </div>
            <div class="settings-row">
              <div class="settings-row__label">Лицензия</div>
              <div class="settings-row__control"><span class="field__hint">MIT</span></div>
            </div>
            <div class="settings-row">
              <div class="settings-row__label">Документация</div>
              <div class="settings-row__control">
                <a href="/docs" target="_blank" rel="noopener" class="btn sm">OpenAPI · /docs</a>
              </div>
            </div>
          </div>
        </div>

      </div>
    </div>
  `;

  // Bind handlers.
  bindHandlers();
  await loadConfig();
}

async function loadConfig() {
  try {
    const cfg = await api.getConfig();
    state.config = cfg;
    const port = document.getElementById('cfg-port');
    const host = document.getElementById('cfg-host');
    if (port) port.value = cfg.port;
    if (host) host.value = cfg.host;
    const v = document.getElementById('about-version');
    if (v) v.textContent = `ApexCore v${cfg.version}`;
    // Скрываем «Сервис ApexCore» на не-Windows.
    if (cfg.platform !== 'windows') {
      const card = document.getElementById('settings-card-sensord');
      if (card) card.style.display = 'none';
    }
  } catch (err) {
    console.warn('failed to load /api/config', err);
  }
}

function bindHandlers() {
  document.getElementById('btn-copy-url')?.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(window.location.origin);
    } catch { /* ignore */ }
  });

  // Theme radio: live-переключение, без перезапуска. Сохраняется в
  // localStorage внутри setTheme(). Подписка через delegation — оба radio
  // в одной группе, проще слушать change на контейнере.
  document.getElementById('theme-radio-group')?.addEventListener('change', (ev) => {
    if (ev.target?.name === 'theme' && (ev.target.value === 'dark' || ev.target.value === 'light')) {
      setTheme(ev.target.value);
    }
  });
  // Поддержка: если тему сменили из topbar-кнопки, синхронизировать
  // radio (на случай если пользователь обратно вернулся на Settings).
  window.addEventListener('theme:change', (ev) => {
    const radio = document.querySelector(`#theme-radio-group input[value="${ev.detail.theme}"]`);
    if (radio) radio.checked = true;
  });

  // Открыть Diagnose — простая навигация на hash-route. Backend не нужен,
  // экран #diagnose уже существует (см. js/screens/diagnose.js).
  document.getElementById('btn-open-diagnose')?.addEventListener('click', () => {
    window.location.hash = '#diagnose';
  });

  document.getElementById('btn-save-port')?.addEventListener('click', async () => {
    const portInput = document.getElementById('cfg-port');
    const hostInput = document.getElementById('cfg-host');
    const port = parseInt(portInput.value, 10);
    const host = hostInput.value;
    if (!Number.isInteger(port) || port < 1024 || port > 65535) {
      alert('Порт должен быть целым числом в диапазоне 1024-65535');
      return;
    }
    try {
      await api.updateConfig({ port, host });
      alert(`Сохранено: ${host}:${port}.\n\nПерезапустите ApexCore командой:\n  apexcore webui\n\nОн подхватит новый порт автоматически. Текущая сессия продолжит работать на старом порту до закрытия.`);
    } catch (err) {
      alert('Ошибка сохранения: ' + err.message);
    }
  });

  // Dropdown «Экспорт всей истории» — общий toggleExportMenu (auto-flip).
  // outside-click handler уже зарегистрирован один раз в history.js.
  const exportAllBtn = document.getElementById('btn-export-all');
  const exportAllMenu = document.getElementById('menu-export-all');
  exportAllBtn?.addEventListener('click', (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    toggleExportMenu(exportAllMenu);
  });
}

function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[c]);
}
