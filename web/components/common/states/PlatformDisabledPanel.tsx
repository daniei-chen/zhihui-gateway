'use client';

import {PowerOff} from 'lucide-react';
import {useEffect, useState} from 'react';

import {platformApi} from '@/lib/api';
import {useT} from '@/lib/i18n/provider';
import {usePlatform} from '@/lib/platform-context';
import {cn} from '@/lib/utils';

/**
 * 「当前平台未启用」的**页面级**空状态。
 *
 * 与布局层那条 `PlatformDisabledNotice` 黄色横幅的分工：
 *   · 横幅：常驻提示，不改变页面内容
 *   · 本组件：**顶掉页面主体**，因为主体里每一个数字此刻都是不可信的
 *
 * ## 为什么需要它（2026-11-19）
 *
 * `doubaowork` 的引擎默认 `off`（上游 doubaowork2api 未部署时它就是 off）。
 * 此时 dashboard 的四份数据**必然全部失败**，`isInitialFailed` 为真，页面
 * 显示「数据加载失败 · 请检查网络连接或后端服务是否正常」。
 *
 * 那句话对用户是误导：网络是好的、网关是好的，只是这个平台没开。用户会去
 * 点重试（永远失败）、去查服务状态（查不出问题）。真正该说的是「这个平台
 * 现在没有后端，原因如下，开关在哪」。
 */
export function PlatformDisabledPanel({
  note,
  className,
}: {
  /** 后端给的具体原因（含开关名），没有则用通用文案 */
  note?: string;
  className?: string;
}) {
  const t = useT();
  return (
    <div
      role="status"
      className={cn(
        'flex flex-col items-center justify-center gap-3 rounded-[20px] bg-muted px-6 py-16 text-center',
        className,
      )}
    >
      <div className="grid h-12 w-12 place-items-center rounded-full bg-muted-foreground/10">
        <PowerOff className="h-5 w-5 text-muted-foreground" />
      </div>
      <div className="text-sm font-medium">{t('platform.notEnabledTitle')}</div>
      <p className="max-w-md text-xs leading-5 text-muted-foreground">
        {note || t('platform.notEnabledHint')}
      </p>
      <p className="max-w-md text-[11px] leading-4 text-muted-foreground/80">
        {t('platform.notEnabledAction')}
      </p>
    </div>
  );
}

export interface PlatformEnabledState {
  /** `false` = 明确未启用；`true` = 已启用；`null` = 还没拿到 / 拿不到 */
  enabled: boolean | null;
  /** 后端给的原因文案 */
  note: string;
}

/**
 * 读当前平台是否启用。
 *
 * 拿不到能力矩阵时返回 `enabled: null`（**不是** `false`）——调用方据此
 * 保持原行为，避免升级顺序不一致时把正常页面误判成「未启用」。
 */
export function usePlatformEnabled(): PlatformEnabledState {
  const {platform} = usePlatform();
  const [state, setState] = useState<PlatformEnabledState>({enabled: null, note: ''});

  useEffect(() => {
    let alive = true;
    setState({enabled: null, note: ''});
    void (async () => {
      try {
        const data = await platformApi.list();
        if (!alive) return;
        const row = data.platforms.find(p => p.id === platform);
        if (!row) {
          setState({enabled: null, note: ''});
          return;
        }
        // 字段缺失（旧后端）按「已启用」处理，理由同上。
        setState({
          enabled: row.enabled === false ? false : true,
          note: row.note || '',
        });
      } catch {
        if (alive) setState({enabled: null, note: ''});
      }
    })();
    return () => {
      alive = false;
    };
  }, [platform]);

  return state;
}
