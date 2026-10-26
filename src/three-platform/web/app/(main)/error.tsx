'use client';

import {ErrorFallback} from '@/components/common/states/ErrorFallback';

/**
 * 页面渲染期异常的兜底（P0-3）。
 *
 * 为什么必须有：在此之前 `app/` 下只有 `not-found.tsx`，任何一处渲染抛错
 * （最常见的是对还没到位的取数结果直接取属性）都会让整页白屏——用户看到的
 * 是一片空白，既没有解释、也没有出路，只能自己猜是不是网络断了。
 *
 * 为什么复用 `LoadError`/`ErrorFallback` 而不是新做一个页面：两者说的是同一件事
 * ——「这份内容没出来」——所以视觉语言该一致。区别在来源：取数失败重试往往就好；
 * 这里是**代码抛错**，重试大概率还是错，用户真正该做的是把控制台的报错交出来。
 * 而 **旧构建失配**（K33：旧 chunk / RSC payload 已不在当前部署里）又是第三种：
 * 刷新到新版本就能解决，所以交给 `ErrorFallback` 自动判断。
 *
 * 为什么放在 `(main)` 而不是 `app/` 根：这样它渲染在 `(main)/layout.tsx` 内部，
 * 底栏还在，用户可以直接切去别的页面，而不是被困在一个孤岛页上。代价是它
 * 兜不住 `(main)/layout.tsx` 自身的异常——那由 `app/error.tsx` 兜。
 */
export default function MainError({
  error,
  reset,
}: {
  /** 交给 ErrorFallback 判断是否属于「旧构建失配」；普通异常只用于控制台堆栈 */
  error: Error & {digest?: string};
  reset: () => void;
}) {
  return <ErrorFallback error={error} reset={reset} />;
}
