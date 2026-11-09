/** Opens an isolated provider window. The main page only reads its own API. */
export function openAuthorizationWindow(url: string): boolean {
  if (typeof window === 'undefined') return false;
  const width = Math.min(520, window.screen.availWidth);
  const height = Math.min(760, window.screen.availHeight);
  const left = Math.max(0, Math.round(window.screenX + (window.outerWidth - width) / 2));
  const top = Math.max(0, Math.round(window.screenY + (window.outerHeight - height) / 2));
  const child = window.open('about:blank', '_blank',
    `popup=yes,width=${width},height=${height},left=${left},top=${top}`);
  if (!child) return false;
  try {
    child.opener = null;
    child.location.replace(url);
    child.focus();
    return true;
  } catch {
    child.close();
    return false;
  }
}
