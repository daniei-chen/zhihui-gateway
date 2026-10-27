'use client';

import {useEffect} from 'react';

import {LoadError} from '@/components/common/states/LoadError';
import {useT} from '@/lib/i18n/provider';
import {
  createReloadGuard,
  isStaleBundleError,
  reloadCurrentPage,
  sessionStorageOrNull,
} from '@/lib/stale-build';

/**
 * 错误边界的统一落点（K33 前端侧 + 原有 P0-3 兜底）。
 *
 * 错误分两类，**处理方式必须不同**：
 *
 *  1. 旧构建/旧资源失配（`ChunkLoadError`、RSC payload 取不到、旧 chunk 404）：
 *     页面代码本身没坏，是标签页还停在旧版本上。正确动作是**刷新到当前版本**，
 *     所以先尝试自动刷新一次；被 `lib/stale-build.ts` 的守卫拦下时（本标签页
 *     刚自动刷过、或存储不可用）只显示手动「刷新页面」，绝不再自动刷——否则
 *     就是「白屏 → 刷新 → 白屏」的死循环。
 *  2. 普通渲染异常：重试大概率还是错，用户该做的是把控制台报错交出来（原行为）。
 *
 * 两处错误边界（`app/error.tsx` 与 `app/(main)/error.tsx`）共用本组件，避免两边
 * 逻辑各写一份、以后只改了一处。
 */
export function ErrorFallback({
  error,
  reset,
}: {
  error: Error & {digest?: string};
  reset: () => void;
}) {
  const t = useT();
  const stale = isStaleBundleError(error);

  useEffect(() => {
    if (!stale) return;
    // 守卫：10 分钟内只自动刷新一次；存储不可写则完全交给手动按钮
    const guard = createReloadGuard(sessionStorageOrNull());
    if (!guard.shouldAutoReload()) return;
    guard.markAutoReload();
    reloadCurrentPage();
  }, [stale]);

  if (stale) {
    return (
      <LoadError
        variant="page"
        message={t('error.staleBuildTitle')}
        hint={t('error.staleBuildHint')}
        retryLabel={t('error.reloadPage')}
        onRetry={reloadCurrentPage}
      />
    );
  }

  return (
    <LoadError
      variant="page"
      message={t('error.crashTitle')}
      hint={t('error.crashHint')}
      onRetry={reset}
    />
  );
}
