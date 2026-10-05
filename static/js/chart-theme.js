// One Chart.js theme for every chart on the site, built from the CSS tokens in
// base.css: site fonts, hairline solid grid, capped bars with rounded data-ends,
// 2px lines, and a tooltip styled like the rest of the UI. Legends are HTML
// (CourtCharts.legend) so they use the site's type and stay readable.
const CourtCharts = {
  token(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  },

  colors() {
    return {
      ink: this.token('--primary'),
      surface: this.token('--secondary'),
      muted: this.token('--muted'),
      grid: this.token('--line'),
      accent: this.token('--accent'),
      win: this.token('--chart-win'),
      loss: this.token('--chart-loss'),
    };
  },

  applyDefaults() {
    if (typeof Chart === 'undefined' || this.applied) return;
    this.applied = true;
    const c = this.colors();
    const d = Chart.defaults;

    d.font.family = getComputedStyle(document.body).fontFamily;
    d.font.size = 12;
    d.color = c.muted;
    d.borderColor = c.grid;
    d.maintainAspectRatio = false;
    d.animation.duration = 300;

    d.scale.grid.color = c.grid;
    d.scale.border.color = c.grid;
    d.scale.ticks.color = c.muted;

    d.elements.bar.borderRadius = 4;        // rounded data-end...
    d.elements.bar.borderSkipped = 'start'; // ...square at the baseline
    d.elements.bar.borderWidth = 0;
    d.datasets.bar.maxBarThickness = 24;

    d.elements.line.borderWidth = 2;
    d.elements.line.borderCapStyle = 'round';
    d.elements.line.borderJoinStyle = 'round';
    d.elements.point.radius = 0;
    d.elements.point.hoverRadius = 5;
    d.elements.point.hitRadius = 12;
    d.elements.point.borderWidth = 2;
    d.elements.point.hoverBorderColor = c.surface;  // surface ring around the marker

    d.plugins.legend.display = false;
    Object.assign(d.plugins.tooltip, {
      backgroundColor: c.surface,
      titleColor: c.ink,
      bodyColor: c.ink,
      borderColor: c.ink,
      borderWidth: 2,
      cornerRadius: 0,
      caretSize: 0,
      padding: 10,
      boxWidth: 12,
      boxHeight: 3,          // a short line key rather than a filled box
      boxPadding: 6,
      titleFont: { weight: 'bold' },
      bodyFont: { weight: '500' },
    });
  },

  // HTML legend: items = [{ label, color, kind: 'bar' | 'line' }]
  legend(container, items) {
    container.replaceChildren(...items.map(({ label, color, kind }) => {
      const item = document.createElement('span');
      item.className = 'legend-entry';
      const key = document.createElement('span');
      key.className = `legend-key legend-key-${kind || 'bar'}`;
      key.style.backgroundColor = color;
      item.append(key, document.createTextNode(label));
      return item;
    }));
  },
};
