/** Match Python len(str) without changing the password sent to the server. */
export const passwordCharacters = (value: string) => Array.from(value).length;

// 校验函数返回的是**文案键**而不是文案本身：原先直接返回中文，成员管理页就
// 跟着整块变成中文，海外管理员看不到自己语言的提示。调用方用 t() 取词。
export function portalPasswordLengthProblem(value: string): string | null {
  const length = passwordCharacters(value);
  if (length < 8) return 'portal.members.errPasswordShort';
  if (length > 128) return 'portal.members.errPasswordLong';
  return null;
}

export function portalUsernameProblem(value: string): string | null {
  return /^[a-zA-Z0-9_-]{3,40}$/.test(value) ? null : 'portal.members.errUsername';
}

export const PORTAL_PASSWORD_HINT_KEY = 'portal.members.passwordHint';

export function filterPortalMembers<T extends {username: string; enabled: number}>(
  members: T[], query: string, status: string,
): T[] {
  const search = query.trim().toLocaleLowerCase();
  return members.filter(member => (!search || member.username.toLocaleLowerCase().includes(search))
    && (status === 'all' || (status === 'enabled' ? !!member.enabled : !member.enabled)));
}
