// Keep a page's selections in the query string so links can be shared and
// reloads restore the same view. replaceState avoids flooding browser history.
const UrlState = {
  get(key) {
    return new URLSearchParams(window.location.search).get(key);
  },

  // Merge params into the URL; null/undefined/'' removes a key.
  set(params) {
    const url = new URL(window.location.href);
    Object.entries(params).forEach(([key, value]) => {
      if (value === null || value === undefined || value === '') url.searchParams.delete(key);
      else url.searchParams.set(key, value);
    });
    window.history.replaceState(null, '', url);
  },

  // Set a <select>/<input> from the URL if the value is valid; returns true if applied.
  apply(elementId, key) {
    const el = document.getElementById(elementId);
    const value = this.get(key);
    if (!el || value === null) return false;
    if (el.tagName === 'SELECT' && ![...el.options].some(o => o.value === value)) return false;
    el.value = value;
    return true;
  },

  async playerNames(ids) {
    if (!ids.length) return [];
    try {
      return await SiteData.playersById(ids);
    } catch (e) {
      console.error(e);
      return [];
    }
  },
};
