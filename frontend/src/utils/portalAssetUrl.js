export function portalAssetUrl(value) {
  if (!value) return null;

  const raw = String(value).trim();
  if (!raw) return null;

  // Leave already absolute and browser-local URLs untouched.
  if (/^(?:[a-z][a-z0-9+.-]*:|\/\/)/i.test(raw)) {
    return raw;
  }

  const basePath = (import.meta.env.BASE_URL || '/').replace(/\/+$/, '');
  const assetPath = raw.startsWith('/') ? raw : `/${raw}`;

  if (!basePath || basePath === '/') {
    return `${window.location.origin}${assetPath}`;
  }

  // Avoid adding /game twice when the backend already returned a prefixed path.
  if (assetPath === basePath || assetPath.startsWith(`${basePath}/`)) {
    return `${window.location.origin}${assetPath}`;
  }

  return `${window.location.origin}${basePath}${assetPath}`;
}

export default portalAssetUrl;
