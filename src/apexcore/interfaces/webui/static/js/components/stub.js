// Generic заглушка для разделов, где функционал ещё не реализован.
// Применяется для Winsat (Linux platform-restriction) и других mock-карточек.

export function renderStub(host, opts) {
  const {
    icon = '🔌',
    title = 'Раздел появится в следующей версии',
    cli = 'apexcore ...',
    body = '',
  } = opts || {};

  host.innerHTML = `
    <div class="stub">
      <div class="stub__icon">${icon}</div>
      <div class="stub__title">${escapeHtml(title)}</div>
      <div class="stub__body">
        ${body || 'Этот раздел появится в следующей версии web-интерфейса.'}
        ${cli ? `<div class="stub__cli-block">Пока доступно через CLI:<br>> ${escapeHtml(cli)}</div>` : ''}
      </div>
    </div>
  `;
}

export function renderPlatformRestriction(host, opts) {
  const { feature = 'Этот раздел', platform = 'Windows' } = opts || {};
  host.innerHTML = `
    <div class="stub">
      <div class="stub__icon">🚫</div>
      <div class="stub__title">${escapeHtml(feature)} доступен только на ${escapeHtml(platform)}</div>
      <div class="stub__body">
        Этот раздел использует ${escapeHtml(platform)}-специфичный API и работает
        только на этой платформе. На текущей системе он скрыт автоматически.
      </div>
    </div>
  `;
}

function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[c]);
}
