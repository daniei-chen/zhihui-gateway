export const MANAGED_USER_MIN_PASSWORD_LENGTH = 8;
export const ROOT_USER_MIN_PASSWORD_LENGTH = 12;

/** Login passwords only. Do not normalize input or apply this to API keys. */
export function isManagedUserPassword(value: unknown): value is string {
  return typeof value === 'string' && Array.from(value).length >= MANAGED_USER_MIN_PASSWORD_LENGTH;
}

export function canResetUserPassword(username: string, password: string): boolean {
  return username === 'admin'
    ? password.length >= ROOT_USER_MIN_PASSWORD_LENGTH
    : isManagedUserPassword(password);
}
