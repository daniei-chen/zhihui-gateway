'use client';

import {ErrorFallback} from '@/components/common/states/ErrorFallback';

/**
 * 根级兜底：`(main)/error.tsx` 兜不到的那一层。
 *
 * React 的错误边界只能接住**子树**的异常——`(main)/error.tsx` 接不住
 * `(main)/layout.tsx` 自己抛的错。而 `(main)/layout.tsx` 恰恰挂着底栏、版本
 * 切换与 RealmProvider，是「整页白屏」最难受的一处。`(auth)/login` 与公开的
 * `/claim` 也不在 `(main)` 之下，同样需要这一层。
 *
 * 这里已经在 `app/layout.tsx` 之内，所以 ThemeProvider / I18nProvider 都还在，
 * 语言与主题照常生效；不在 `(main)` 之下则意味着没有底栏，因此提示里必须留一句
 * 「回首页」以外的出路——`reset` 之外再给一次整页刷新由用户自己决定。
 *
 * 具体文案分支（旧构建失配 / 普通渲染异常）在 `ErrorFallback` 里，与
 * `(main)/error.tsx` 共用一份，避免两处文案与行为漂移。
 */
export default function RootError({
  error,
  reset,
}: {
  error: Error & {digest?: string};
  reset: () => void;
}) {
  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-xl">
        <ErrorFallback error={error} reset={reset} />
      </div>
    </div>
  );
}
