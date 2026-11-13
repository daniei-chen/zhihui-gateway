export const MANAGED_USER_MIN_PASSWORD_LENGTH = 8;
export const ROOT_USER_MIN_PASSWORD_LENGTH = 12;
export const PANEL_USER_MIN_PASSWORD_LENGTH = 12;

/** Login passwords only. Do not normalize input or apply this to API keys. */
export function isManagedUserPassword(value: unknown): value is string {
  return typeof value === 'string' && Array.from(value).length >= MANAGED_USER_MIN_PASSWORD_LENGTH;
}

/** Management-panel identities use the root-grade minimum. */
export function isPanelUserPassword(value: unknown): value is string {
  return typeof value === 'string' && Array.from(value).length >= PANEL_USER_MIN_PASSWORD_LENGTH;
}

export function canResetUserPassword(username: string, password: string): boolean {
  return Array.from(password).length >= PANEL_USER_MIN_PASSWORD_LENGTH;
}
