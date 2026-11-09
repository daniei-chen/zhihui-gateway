/** Match Python len(str) without changing the password sent to the server. */
export const passwordCharacters = (value: string) => Array.from(value).length;

export function portalPasswordLengthProblem(value: string): string | null {
  const length = passwordCharacters(value);
  if (length < 8) return '用户登录密码至少 8 位';
  if (length > 128) return '用户登录密码最多 128 位';
  return null;
}

export function portalUsernameProblem(value: string): string | null {
  return /^[a-zA-Z0-9_-]{3,40}$/.test(value) ? null : '用户名需为3至40位字母、数字、下划线或短横线';
}

export const PORTAL_PASSWORD_HINT = '8–128个字符，支持数字。不要使用常见弱密码、单一重复字符、用户名或用户名加数字。';

export function filterPortalMembers<T extends {username: string; enabled: number}>(
  members: T[], query: string, status: string,
): T[] {
  const search = query.trim().toLocaleLowerCase();
  return members.filter(member => (!search || member.username.toLocaleLowerCase().includes(search))
    && (status === 'all' || (status === 'enabled' ? !!member.enabled : !member.enabled)));
}
