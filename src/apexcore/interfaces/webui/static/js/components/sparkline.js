// SVG sparkline + line chart. Без зависимостей (Chart.js не используется).
//
// renderSparkline(container, values, opts) — мини-график в KPI-карточках.
// renderLineChart(container, series, opts) — большой график.
//
// Цвета можно передавать как hex (#7cd0ff) или как CSS-переменную
// ('var(--accent)') — оба варианта применяются через inline style на path,
// потому что SVG-атрибут `stroke="var(...)"` НЕ резолвится в большинстве
// браузеров (это работает только в CSS). Текст/сетка/легенда используют
// CSS-классы (.spark__empty-text / .spark__grid-line / .spark__legend-text),
// которые подхватывают переменные из tokens.css для текущей темы.

const DEFAULT_COLOR = 'var(--accent)';

export function renderSparkline(container, values, opts = {}) {
  const {
    color = DEFAULT_COLOR,
    height = 38,
    minSpread = 1,    // если разница max-min < minSpread, рисуем плоско по центру
  } = opts;

  const w = container.clientWidth || 200;
  const h = height;

  const pts = (values || []).filter(v => typeof v === 'number' && !Number.isNaN(v));
  if (pts.length < 2) {
    container.innerHTML = `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"></svg>`;
    return;
  }

  let min = Math.min(...pts);
  let max = Math.max(...pts);
  if (max - min < minSpread) {
    const mid = (min + max) / 2;
    min = mid - minSpread / 2;
    max = mid + minSpread / 2;
  }

  const n = values.length;
  const dx = w / Math.max(1, n - 1);
  const range = max - min || 1;
  const pad = 2;

  const points = values.map((v, i) => {
    if (typeof v !== 'number' || Number.isNaN(v)) return null;
    const x = i * dx;
    const y = h - pad - ((v - min) / range) * (h - 2 * pad);
    return [x, y];
  });

  // Линия: пропускаем null-значения (разрывы).
  let linePath = '';
  let move = true;
  for (const p of points) {
    if (p == null) { move = true; continue; }
    linePath += `${move ? 'M' : 'L'}${p[0].toFixed(1)},${p[1].toFixed(1)} `;
    move = false;
  }

  // Заливка под линией.
  const firstReal = points.findIndex(p => p != null);
  const lastReal = points.length - 1 - [...points].reverse().findIndex(p => p != null);
  let areaPath = '';
  if (firstReal >= 0 && lastReal >= 0) {
    areaPath = linePath + `L${(lastReal * dx).toFixed(1)},${h} L${(firstReal * dx).toFixed(1)},${h} Z`;
  }

  container.innerHTML = `
    <svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
      <path class="spark__area" d="${areaPath}" style="fill: ${color}" />
      <path class="spark__line" d="${linePath}" style="stroke: ${color}" />
    </svg>
  `;
}

// Большой график с осями. Один или несколько series.
//   series: [{ values: [...], color: '#7cd0ff' | 'var(--accent)', label: 'CPU %', yMin?, yMax? }]
export function renderLineChart(container, series, opts = {}) {
  const {
    height = 200,
    yLabel = '',
    xLabels = null,    // массив подписей x, та же длина что values
  } = opts;

  const w = container.clientWidth || 600;
  const h = height;
  const padLeft = 36, padRight = 8, padTop = 8, padBottom = 22;
  const plotW = w - padLeft - padRight;
  const plotH = h - padTop - padBottom;

  if (!series || series.length === 0 || !series[0].values?.length) {
    container.innerHTML = `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
      <text class="spark__empty-text" x="${w/2}" y="${h/2}" font-size="11" text-anchor="middle" font-family="monospace">нет данных</text>
    </svg>`;
    return;
  }

  // Общий диапазон по всем series (или per-series yMin/yMax).
  let yMin = Infinity, yMax = -Infinity;
  for (const s of series) {
    const real = s.values.filter(v => typeof v === 'number' && !Number.isNaN(v));
    if (real.length) {
      yMin = Math.min(yMin, s.yMin ?? Math.min(...real));
      yMax = Math.max(yMax, s.yMax ?? Math.max(...real));
    }
  }
  // Если все значения одинаковые (например, legacy `final_score = 0.0` у всех
  // прогонов) — рисуем заглушку. Иначе SVG получит плоскую линию у самого низа,
  // что выглядит как «график-карандашик» и сбивает с толку.
  if (!Number.isFinite(yMin) || yMax === yMin) {
    const constVal = Number.isFinite(yMin) ? yMin : null;
    const text = constVal != null
      ? `все значения = ${formatTick(constVal)} (нет вариаций)`
      : 'нет данных';
    container.innerHTML = `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
      <text class="spark__empty-text" x="${w/2}" y="${h/2}" font-size="11" text-anchor="middle" font-family="monospace">${text}</text>
    </svg>`;
    return;
  }
  const yRange = yMax - yMin;
  const n = series[0].values.length;
  const dx = plotW / Math.max(1, n - 1);

  const yScale = (v) => padTop + plotH - ((v - yMin) / yRange) * plotH;

  // Подписи оси Y (4 деления).
  const yTicks = [];
  for (let i = 0; i <= 4; i++) {
    const v = yMin + (yRange * i / 4);
    yTicks.push({ v, y: yScale(v) });
  }

  // Подписи оси X.
  let xTicks = [];
  if (xLabels && xLabels.length) {
    const step = Math.max(1, Math.floor(xLabels.length / 6));
    for (let i = 0; i < xLabels.length; i += step) {
      xTicks.push({ label: xLabels[i], x: padLeft + i * dx });
    }
  }

  const lines = series.map((s) => {
    let path = '';
    let move = true;
    s.values.forEach((v, i) => {
      if (typeof v !== 'number' || Number.isNaN(v)) { move = true; return; }
      const x = padLeft + i * dx;
      const y = yScale(v);
      path += `${move ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)} `;
      move = false;
    });
    const stroke = s.color || DEFAULT_COLOR;
    return `<path d="${path}" style="stroke: ${stroke}" stroke-width="1.6" fill="none" />`;
  }).join('');

  const yGrid = yTicks.map(t => `
    <line class="spark__grid-line" x1="${padLeft}" x2="${w - padRight}" y1="${t.y}" y2="${t.y}" stroke-width="0.5" />
    <text class="spark__empty-text" x="${padLeft - 4}" y="${t.y + 3}" font-size="9" text-anchor="end" font-family="monospace">${formatTick(t.v)}</text>
  `).join('');

  const xGrid = xTicks.map(t => `
    <text class="spark__empty-text" x="${t.x}" y="${h - 6}" font-size="9" text-anchor="middle" font-family="monospace">${t.label}</text>
  `).join('');

  const legend = series
    .filter(s => s.label)
    .map((s, i) => {
      const x = padLeft + i * 80;
      const fill = s.color || DEFAULT_COLOR;
      return `<g transform="translate(${x}, ${padTop + 6})">
        <rect width="10" height="2" style="fill: ${fill}" />
        <text class="spark__legend-text" x="14" y="3" font-size="10" font-family="monospace">${s.label}</text>
      </g>`;
    }).join('');

  container.innerHTML = `
    <svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
      ${yGrid}
      ${xGrid}
      ${lines}
      ${legend}
      ${yLabel ? `<text class="spark__empty-text" x="4" y="${padTop + 8}" font-size="10" font-family="monospace">${yLabel}</text>` : ''}
    </svg>
  `;
}

function formatTick(v) {
  if (Math.abs(v) >= 1000) return `${(v/1000).toFixed(1)}k`;
  if (Math.abs(v) >= 100) return v.toFixed(0);
  if (Math.abs(v) >= 10) return v.toFixed(0);
  return v.toFixed(1);
}
