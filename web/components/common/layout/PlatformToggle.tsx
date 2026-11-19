'use client';

import {Bot, Brain, Cloud, Sparkles} from 'lucide-react';
import {motion} from 'motion/react';

import {usePlatform, type Platform} from '@/lib/platform-context';
import {useT} from '@/lib/i18n/provider';
import {cn} from '@/lib/utils';

/**
 * 平台切换（WorkBuddy / Qoder / Trae / 豆包工作）。WorkBuddy 排第一：它是当前主力平台。
 *
 * 与右侧的版本切换（国内 / 国际）**并存**，两者是不同维度：
 * 平台决定连哪个上游网关，版本决定该平台下的账号属于国内站还是国际站。
 *
 * 豆包工作没有「国内 / 国际」之分，切到它时版本切换器不产生实际影响。
 *
 * 样式与 RealmToggle 保持一致（同一套 pill + layoutId 弹簧滑动）。
 */
export function PlatformToggle({compact = false}: {compact?: boolean}) {
  const {platform, setPlatform} = usePlatform();
  const t = useT();
  const options: {id: Platform; label: string; title: string; icon: typeof Bot}[] = [
    {
      id: 'workbuddy',
      label: 'WorkBuddy',
      title: t('platform.workbuddyHint'),
      icon: Cloud,
    },
    {id: 'qoder', label: 'Qoder', title: t('platform.qoderHint'), icon: Sparkles},
    {id: 'trae', label: 'Trae', title: t('platform.traeHint'), icon: Bot},
    {
      id: 'doubaowork',
      // 走 i18n：前三个平台名是英文专名（WorkBuddy/Qoder/Trae），中文产品名
      // 若在这里写死，en/ja/ko 界面会突然出现中文。
      label: t('platform.doubaoworkShort'),
      title: t('platform.doubaoworkHint'),
      icon: Brain,
    },
  ];

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
                {/* 窄屏只留图标：四个 pill 带文字合计约 280–300px，320px 视口
                    （iPhone SE 一代等）会横向滚动。断点用 420px 而不是 sm(640)，
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
