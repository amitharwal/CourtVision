// Player headshots and team logos from the NBA's image CDN.
// Avatars render the player's initials underneath the photo, so if the image
// fails to load (onerror removes it) the initials show instead.
const NbaMedia = {
  headshotUrl(playerId) {
    return `https://cdn.nba.com/headshots/nba/latest/260x190/${encodeURIComponent(playerId)}.png`;
  },

  teamLogoUrl(teamId) {
    return `https://cdn.nba.com/logos/nba/${encodeURIComponent(teamId)}/global/L/logo.svg`;
  },

  initials(name) {
    return (name || '?').split(/\s+/).filter(Boolean).map(part => part[0]).join('').slice(0, 3).toUpperCase();
  },

  // <span class="avatar"> with initials and the headshot layered on top.
  avatar(playerId, name, size = 48) {
    const span = document.createElement('span');
    span.className = 'avatar';
    span.style.setProperty('--avatar-size', `${size}px`);
    span.textContent = this.initials(name);
    if (playerId) {
      const img = document.createElement('img');
      img.src = this.headshotUrl(playerId);
      img.alt = '';
      img.loading = 'lazy';
      img.addEventListener('error', () => img.remove());
      span.appendChild(img);
    }
    return span;
  },

  // Same avatar as an HTML string, for templates built with innerHTML.
  avatarHtml(playerId, name, size = 48) {
    return this.avatar(playerId, name, size).outerHTML
      .replace('<img ', '<img onerror="this.remove()" ');
  },

  teamLogo(teamId, size = 24) {
    const img = document.createElement('img');
    img.className = 'team-logo';
    img.src = this.teamLogoUrl(teamId);
    img.alt = '';
    img.width = size;
    img.height = size;
    img.loading = 'lazy';
    img.addEventListener('error', () => img.remove());
    return img;
  },
};
