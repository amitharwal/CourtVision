// Site data: JSON files under /data/, saved by `flask build` for the static site and
// served live by the Flask dev server. A missing file means that data isn't on the site.
const SiteData = {
  MISSING: "This isn't on the site yet. Try the current season, or check back after the next update.",

  // Parsed JSON for a path under /data/; throws an Error with a readable message.
  // fresh: revalidate instead of using the browser's cached copy (live scores).
  async get(path, { fresh = false } = {}) {
    let res;
    try {
      res = await fetch(`/data/${path}`, fresh ? { cache: 'no-cache' } : {});
    } catch (e) {
      throw new Error("Couldn't reach the site. Check your connection and try again.");
    }
    if (res.status === 404) throw new Error(this.MISSING);
    let data;
    try {
      data = await res.json();
    } catch (e) {
      throw new Error('Something went wrong loading this data.');
    }
    if (!data.success) throw new Error(data.error || 'Something went wrong loading this data.');
    return data;
  },

  // ---- Player search over data/player-index.json ----
  _index: null,

  playerIndex() {
    if (!this._index) {
      this._index = this.get('player-index.json').then(d => d.players)
        .catch(e => { this._index = null; throw e; });
    }
    return this._index;
  },

  // Same as normalize_name() in app.py: "P.J. Dončić-Smith" -> "pj doncic smith".
  normalize(text) {
    return (text || '').normalize('NFKD').replace(/\p{M}/gu, '').toLowerCase()
      .replace(/[.'’]/g, '').replace(/[^a-z0-9]+/g, ' ').trim();
  },

  // Up to `limit` players whose name contains the query. Names (or a first/last
  // name) starting with it rank first, then active players, then alphabetically.
  async searchPlayers(query, limit = 10) {
    const q = this.normalize(query);
    if (!q) return [];
    const ranked = (await this.playerIndex())
      .filter(p => p.key.includes(q))
      .map(p => ({ p, rank: [!` ${p.key}`.includes(` ${q}`), !p.is_active, p.key] }));
    ranked.sort((a, b) => {
      for (let i = 0; i < 3; i++) {
        if (a.rank[i] < b.rank[i]) return -1;
        if (a.rank[i] > b.rank[i]) return 1;
      }
      return 0;
    });
    return ranked.slice(0, limit).map(({ p }) => p);
  },

  // Players by id, in the order given, skipping unknown ids (restores shared links).
  async playersById(ids) {
    const byId = new Map((await this.playerIndex()).map(p => [p.id, p]));
    return ids.map(id => byId.get(Number(id))).filter(Boolean);
  },
};
