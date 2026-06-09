// Background decoration: grid + 5 sparklines + crosshair + score block + bar histogram.
//
// Порт из design_handoff_installer/installer-shared.jsx:
//   - makeSpark/sparkPath — детерминированный псевдо-LCG генератор (тот же seed → тот же путь)
//   - InstallerBackground — SVG + score block
//
// Грид — через CSS background-image (см. background.css). Sparks и crosshair — SVG.

const SCORE_DEFAULT = 9412;
const SCORE_LABEL = '// ОБЩАЯ ОЦЕНКА';
const SCORE_META_HTML = '×10 000 · <span class="pass">PASS</span> · топ 12%';
const BAR_HEIGHTS = [10, 14, 13, 18, 15, 19, 22, 17, 23, 26, 22, 24];

/**
 * Псевдо-случайный sparkline-генератор. Детерминированный (LCG + sin),
 * один и тот же seed даёт один и тот же путь.
 */
export function makeSpark(seed, n = 30, base = 50, amp = 18, drift = 0) {
  const out = [];
  let s = seed;
  for (let i = 0; i < n; i++) {
    s = (s * 9301 + 49297) % 233280;
    const r = s / 233280 - 0.5;
    out.push(base + drift * (i / n) + Math.sin(i / 4 + seed) * amp * 0.5 + r * amp * 0.7);
  }
  return out;
}

/** Превращает массив значений в SVG path для sparkline'а размером w×h. */
export function sparkPath(data, w, h) {
  if (!data || !data.length) return '';
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const pad = 2;
  return data.map((v, i) => {
    const x = pad + (i / (data.length - 1)) * (w - pad * 2);
    const y = pad + (h - pad * 2) - ((v - min) / range) * (h - pad * 2);
    return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
}

const SPARK_SPECS = [
  { x:  60, y: 200, w: 180, h: 56, seed: 53, drift:  6, color: 'var(--spark-a)' },
  { x: 700, y: 220, w: 220, h: 56, seed: 67, drift:  3, color: 'var(--spark-b)' },
  { x: 420, y:  60, w: 240, h: 48, seed: 79, drift:  9, color: 'var(--spark-a)' },
  { x: 100, y: 340, w: 200, h: 50, seed: 11, drift:  6, color: 'var(--spark-c)' },
  { x: 600, y: 360, w: 200, h: 50, seed: 23, drift:  4, color: 'var(--spark-a)' },
];

const SCATTER_DOTS = [
  [80, 280], [180, 260], [310, 130], [540, 110], [760, 130], [820, 240],
];

/**
 * Создаёт DOM-узел декоративного фона. Параметр score — большая цифра в правом-верхнем углу.
 */
export function createBackground({ score = SCORE_DEFAULT } = {}) {
  const root = document.createElement('div');
  root.className = 'bg-deco';
  root.setAttribute('aria-hidden', 'true');

  // Сетка
  const grid = document.createElement('div');
  grid.className = 'bg-deco__grid';
  root.appendChild(grid);

  // SVG
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('class', 'bg-deco__svg');
  svg.setAttribute('viewBox', '0 0 960 680');
  svg.setAttribute('preserveAspectRatio', 'none');

  for (const s of SPARK_SPECS) {
    const g = document.createElementNS(SVG_NS, 'g');
    g.setAttribute('transform', `translate(${s.x}, ${s.y})`);
    g.setAttribute('opacity', '0.75');
    const path = document.createElementNS(SVG_NS, 'path');
    path.setAttribute('d', sparkPath(makeSpark(s.seed, 30, 50, 18, s.drift), s.w, s.h));
    path.setAttribute('fill', 'none');
    path.setAttribute('stroke', s.color);
    path.setAttribute('stroke-width', '1');
    path.setAttribute('stroke-linejoin', 'round');
    path.setAttribute('stroke-linecap', 'round');
    g.appendChild(path);
    const baseline = document.createElementNS(SVG_NS, 'line');
    baseline.setAttribute('x1', '0');
    baseline.setAttribute('y1', String(s.h / 2));
    baseline.setAttribute('x2', String(s.w));
    baseline.setAttribute('y2', String(s.h / 2));
    baseline.setAttribute('stroke', 'var(--faint)');
    baseline.setAttribute('stroke-opacity', '0.18');
    baseline.setAttribute('stroke-dasharray', '2 4');
    baseline.setAttribute('stroke-width', '1');
    g.appendChild(baseline);
    svg.appendChild(g);
  }

  for (const [x, y] of SCATTER_DOTS) {
    const c = document.createElementNS(SVG_NS, 'circle');
    c.setAttribute('cx', String(x));
    c.setAttribute('cy', String(y));
    c.setAttribute('r', '2.5');
    c.setAttribute('fill', 'var(--accent)');
    c.setAttribute('opacity', '0.35');
    svg.appendChild(c);
  }

  // Crosshair (60,60)
  const cross = document.createElementNS(SVG_NS, 'g');
  cross.setAttribute('transform', 'translate(60, 60)');
  cross.setAttribute('opacity', '0.4');
  const cl1 = document.createElementNS(SVG_NS, 'line');
  cl1.setAttribute('x1', '0'); cl1.setAttribute('y1', '6');
  cl1.setAttribute('x2', '12'); cl1.setAttribute('y2', '6');
  cl1.setAttribute('stroke', 'var(--accent)');
  cl1.setAttribute('stroke-width', '1');
  cross.appendChild(cl1);
  const cl2 = document.createElementNS(SVG_NS, 'line');
  cl2.setAttribute('x1', '6'); cl2.setAttribute('y1', '0');
  cl2.setAttribute('x2', '6'); cl2.setAttribute('y2', '12');
  cl2.setAttribute('stroke', 'var(--accent)');
  cl2.setAttribute('stroke-width', '1');
  cross.appendChild(cl2);
  const ct = document.createElementNS(SVG_NS, 'text');
  ct.setAttribute('x', '18'); ct.setAttribute('y', '10');
  ct.setAttribute('fill', 'var(--faint)');
  ct.setAttribute('font-size', '9');
  ct.setAttribute('font-family', 'JetBrains Mono, Consolas, monospace');
  ct.setAttribute('letter-spacing', '0.06em');
  ct.textContent = '+0.42σ';
  cross.appendChild(ct);
  svg.appendChild(cross);

  root.appendChild(svg);

  // Score block
  const scoreBox = document.createElement('div');
  scoreBox.className = 'bg-deco__score';
  scoreBox.innerHTML = `
    <div class="bg-deco__score-label">${SCORE_LABEL}</div>
    <div class="bg-deco__score-value">${formatScore(score)}</div>
    <div class="bg-deco__score-meta">${SCORE_META_HTML}</div>
    <div class="bg-deco__bars"></div>
  `;
  const bars = scoreBox.querySelector('.bg-deco__bars');
  BAR_HEIGHTS.forEach((h, i) => {
    const b = document.createElement('div');
    b.className = 'bg-deco__bar';
    b.style.height = `${h}px`;
    b.style.background = i >= 8 ? 'var(--score)' : 'var(--accent)';
    b.style.opacity = String(0.55 + (i / 28));
    bars.appendChild(b);
  });
  root.appendChild(scoreBox);

  return root;
}

function formatScore(n) {
  // ru-RU: «9 412» (узкий неразрывный пробел в группе тысяч)
  return n.toLocaleString('ru-RU').replace(/\s/g, ' ');
}
