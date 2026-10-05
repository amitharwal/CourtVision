// Small shared UI helpers: toasts (instead of alert()), loading skeletons for
// first loads, and a dimmed "refreshing" state that keeps the previous content
// in place while new data loads (no flash back to a skeleton).
const UI = {
  // Brief, non-blocking notice. kind: 'info' | 'error'. action: { label, href }.
  toast(message, { kind = 'info', action = null, duration = 4000 } = {}) {
    let region = document.getElementById('toast-region');
    if (!region) {
      region = document.createElement('div');
      region.id = 'toast-region';
      region.className = 'toast-region';
      region.setAttribute('role', 'status');
      region.setAttribute('aria-live', 'polite');
      document.body.appendChild(region);
    }
    const toast = document.createElement('div');
    toast.className = `toast toast-${kind}`;
    toast.append(document.createTextNode(message));
    if (action) {
      const link = document.createElement('a');
      link.href = action.href;
      link.textContent = action.label;
      toast.append(link);
    }
    region.appendChild(toast);
    setTimeout(() => toast.remove(), duration);
  },

  // Placeholder shapes while something loads for the first time.
  // kind: 'cards' (n boxes) | 'rows' (n text lines) | 'block' (one chart-sized box)
  skeleton(container, kind = 'rows', count = 5) {
    const wrap = document.createElement('div');
    wrap.className = `skeleton-group skeleton-${kind}`;
    wrap.setAttribute('aria-busy', 'true');
    wrap.setAttribute('aria-label', 'Loading');
    const n = kind === 'block' ? 1 : count;
    for (let i = 0; i < n; i++) {
      const item = document.createElement('div');
      item.className = 'skeleton';
      wrap.appendChild(item);
    }
    container.replaceChildren(wrap);
  },

  // Dim existing content during a refetch instead of replacing it.
  refreshing(el, on) {
    if (!el) return;
    el.classList.toggle('is-refreshing', on);
    el.setAttribute('aria-busy', on ? 'true' : 'false');
  },

  // A player search result as a keyboard-reachable <button> with headshot.
  playerOption(player, onSelect) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'button-reset search-option search-player';
    const name = document.createElement('span');
    name.textContent = player.name;
    btn.append(NbaMedia.avatar(player.id, player.name, 28), name);
    if (!player.is_active) {
      const note = document.createElement('span');
      note.className = 'search-note';
      note.textContent = 'Retired';
      btn.append(note);
    }
    btn.addEventListener('click', () => onSelect(player));
    return btn;
  },
};
