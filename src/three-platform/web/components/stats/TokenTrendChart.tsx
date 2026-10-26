/**
 * 统计页的 token 趋势图（K37：单独成 chunk，首屏后异步加载）。
 *
 * 与 `components/dashboard/DashboardCharts.tsx` 同样的理由：recharts 的 import
 * 必须搬出页面文件，否则它跟页面 chunk 一起进首屏。这里只画图，范围选择器、
 * 粒度判定（`chartSpecFor`）与数据组装留在页面。
 */
'use client';

import {
  Area,
  Bar,
  CartesianGrid,
  Cell,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import {fmtCompact, fmtNumber} from '@/lib/format';
import type {ChartPoint, ChartShape} from '@/lib/usage-chart';

export function TokenTrendChart({
  data,
  shape,
  tickEvery,
  tokensLabel,
  requestsLabel,
  failedLabel,
}: {
  data: ChartPoint[];
  shape: ChartShape;
  tickEvery: number;
  tokensLabel: string;
  requestsLabel: string;
  failedLabel: string;
}) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      {/* 用 ComposedChart 而不是 BarChart：BarChart 会忽略非 Bar 子组件，
          失败曲线（Line）根本不会被渲染 —— 实测踩过，图上一条线都没有。 */}
      <ComposedChart data={data} margin={{top: 4, right: 8, bottom: 0, left: -8}}
                     barCategoryGap="20%">
        <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
        <XAxis
          dataKey="label"
          tickLine={false}
          axisLine={false}
          fontSize={11}
          stroke="var(--muted-foreground)"
          /* 24 个小时（或 90 天）标签全画出来会糊成一片，按 spec 抽稀。 */
          interval={Math.max(0, tickEvery - 1)}
        />
        <YAxis tickLine={false} axisLine={false} fontSize={11}
               stroke="var(--muted-foreground)"
               tickFormatter={(v) => fmtCompact(Number(v))} />
        <Tooltip
          cursor={{fill: 'var(--accent)'}}
          contentStyle={{
            background: 'var(--popover)',
            border: '1px solid var(--border)',
            borderRadius: 12,
            fontSize: 12,
          }}
          formatter={(value, name) => {
            const label = String(name);
            if (label === 'tokens') return [fmtNumber(Number(value)), tokensLabel];
            if (label === 'failed') return [fmtNumber(Number(value)), failedLabel];
            return [fmtNumber(Number(value)), requestsLabel];
          }}
        />
        {shape === 'bar' ? (
          <Bar
            dataKey="tokens"
            name="tokens"
            fill="var(--chart-1)"
            radius={[4, 4, 0, 0]}
            /* 限制柱宽：只有一两天数据时，柱子不会被拉伸占满整个图表 */
            maxBarSize={48}
          >
            {/* 当前小时单独上色：一眼看出「现在走到哪一格」。 */}
            {data.map((p) => (
              <Cell key={p.full} fill={p.isNow ? 'var(--chart-2)' : 'var(--chart-1)'} />
            ))}
          </Bar>
        ) : (
          /* 30 / 90 天用面积：柱子细得看不清起伏，面积图的形状对总量趋势更直观。 */
          <Area
            type="monotone"
            dataKey="tokens"
            name="tokens"
            stroke="var(--chart-1)"
            fill="var(--chart-1)"
            fillOpacity={0.18}
            strokeWidth={2}
          />
        )}
        {/* 失败数用一条线叠在同一张图上：它与 token 柱不同量级，做成柱子会把
            柱形压扁。线只作「那天出过事」的信号，具体数值看悬停。 */}
        <Line
          type="monotone"
          dataKey="failed"
          name="failed"
          stroke="var(--destructive)"
          strokeWidth={1.5}
          strokeDasharray="4 3"
          dot={false}
        />
      </ComposedChart>
    </ResponsiveContainer>
  );
}
