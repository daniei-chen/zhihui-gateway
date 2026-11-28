'use client';

import {AlertTriangle} from 'lucide-react';

import {useAuth} from '@/lib/auth-context';
import {useT} from '@/lib/i18n/provider';
import {needsOriginNotice} from '@/lib/origin-check';
import {cn} from '@/lib/utils';

/**
 * D-41 横幅：**在填表之前**说明「这个地址提交会被拒」以及怎么修。
 *
 * 判据是 `/api/me` 的 `origin_check.writes_allowed`（服务端算出来的机器可读
 * 字段），不是前端去猜 `location.hostname`：只有服务端知道这次请求的 Host 是
 * 不是点分域名、有没有可信反代送来的 `X-Forwarded-Host`。
 *
 * 字段缺失（旧后端 / 升级顺序不一致）一律不提示 —— 与 `PlatformDisabledNotice`
 * 同一取向：凭空弹一条警告比晚一条提示更糟。
 */
export function OriginAnchorNotice({className}: {className?: string}) {
  const {me, loading} = useAuth();
  const t = useT();
  if (loading || !needsOriginNotice(me?.origin_check)) return null;
  return (
    <div
      role="alert"
      className={cn(
        'flex items-start gap-2 rounded-[16px] border border-amber-500/30 bg-amber-500/10 px-3.5 py-2.5 text-[11px] leading-5 text-amber-700 dark:text-amber-300',
        className,
      )}
    >
      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <div className="space-y-0.5">
        <div className="font-medium">{t('origin.noticeTitle')}</div>
        <div>{t('origin.noticeBody')}</div>
      </div>
    </div>
  );
}
