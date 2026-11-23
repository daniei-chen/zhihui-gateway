'use client';

import {Cloud, type LucideIcon} from 'lucide-react';
import {motion} from 'motion/react';

import {usePlatform, type Platform} from '@/lib/platform-context';
import {useT} from '@/lib/i18n/provider';
import {cn} from '@/lib/utils';

/**
 * 平台切换。
 *
 * **2026-11-23：只剩 WorkBuddy 一个平台，切换器实际上不再需要。**
 * 这里仍保留组件与渲染逻辑，是因为它还被多处引用；当选项只有一个时
 * 直接返回 `null` —— 一个只有一项的「切换器」对用户是纯噪音。
 *
 * 历史沿革：`qoder` 于 2026-11-19 停止送积分、11-22 从网关移除；`trae` 于
 * 2026-11-23 主动下线。两者的 `Platform` 类型值仍保留（历史数据与死分支），
 * 但 UI 不再产出这些选项。
 *
 * 注意：这个 `options` 数组是**独立维护**的，不读 `lib/platform-store.ts` 的
 * `PLATFORMS`。改那边不会自动影响这里 —— 两处都要改。
 */
export function PlatformToggle({compact = false}: {compact?: boolean}) {
  const {platform, setPlatform} = usePlatform();
  const t = useT();
  const options: {id: Platform; label: string; title: string; icon: LucideIcon}[] = [
    {
      id: 'workbuddy',
      label: 'WorkBuddy',
      title: t('platform.workbuddyHint'),
      icon: Cloud,
    },
  ];

  // 只有一个平台时不渲染切换器：没有可切换的东西。
  if (options.length <= 1) return null;

  return (
    <div
      className="inline-flex items-center gap-0.5 rounded-full border border-border/60 bg-muted/60 p-0.5"
      role="tablist"
      aria-label={t('platform.switch')}
    >
      {options.map(({id, label, title, icon: Icon}) => {
        const active = platform === id;
        return (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={active}
            title={title}
            onClick={() => setPlatform(id)}
            className={cn(
              'relative flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-medium transition-colors',
              active ? 'text-foreground' : 'text-muted-foreground hover:text-foreground',
            )}
          >
            {active && (
              <motion.span
                layoutId="platform-pill"
                className="absolute inset-0 rounded-full bg-background shadow-sm"
                transition={{type: 'spring', stiffness: 400, damping: 32}}
              />
            )}
            <Icon className="relative h-3 w-3 shrink-0" />
            {!compact && (
              <>
                {/* 窄屏只留图标：三个 pill 带文字合计约 210–230px，320px 视口
                    （iPhone SE 一代等）仍会横向滚动，所以保留收起逻辑。
                    断点用 420px 而不是 sm(640)，
                    因为 375/390 这些常见机型放得下文字，没必要提前收起来。
                    收起时用 sr-only 保留可访问名，不能只剩一个裸图标。 */}
                <span className="relative hidden min-[420px]:inline">{label}</span>
                <span className="sr-only min-[420px]:hidden">{label}</span>
              </>
            )}
            {compact && <span className="sr-only">{label}</span>}
          </button>
        );
      })}
    </div>
  );
}
