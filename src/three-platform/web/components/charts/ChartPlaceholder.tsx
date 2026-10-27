/**
 * 图表占位（K37 懒加载期间）。
 *
 * 高度必须与真图一致（调用方传 h），否则图表在首屏后出现时会把下面的内容
 * 顶下去 —— 那就是拿「减少首屏字节」换来 CLS 变差，不划算。
 */
'use client';

export function ChartPlaceholder({h, label}: {h: number; label?: string}) {
  return (
    <div
      className="grid w-full place-items-center rounded-lg bg-background/40 text-[11px] text-muted-foreground"
      style={{height: h}}
      aria-busy="true"
      data-testid="chart-placeholder"
    >
      {label ?? ''}
    </div>
  );
}
