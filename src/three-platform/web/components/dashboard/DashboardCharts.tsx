/**
 * 仪表盘的两张图（K37：单独成 chunk，首屏后异步加载）。
 *
 * 为什么把 JSX 搬出来而不是在页面里 dynamic 包一层：`next/dynamic` 只能按
 * **模块** 拆包，recharts 的 import 留在页面文件里就仍会进首屏 bundle。搬到这里
 * 之后，页面只 import 本文件（动态），recharts 连同它的 d3 依赖落在独立 chunk。
 *
 * 这里只负责画图；数据获取、时段选择、失败态文案都留在页面（它们属于页面语义，
 * 搬进来会让这个 chunk 反向依赖页面的 state）。
 */
'use client';

import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

export type TrendPoint = {
  day: string;
  requests: number;
  tokens: number;
  failed: number;
};

export type FlowPoint = {
  day: string;
  income: number;
  expense: number;
  net?: number;
};

/** 近 N 天请求趋势（实线=请求数，虚线=失败数，两条来自不同数据源）。 */
export function TrendChart({
  data,
  requestsLabel,
  failedLabel,
}: {
  data: TrendPoint[];
  requestsLabel: string;
  failedLabel: string;
}) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <AreaChart data={data} margin={{top: 4, right: 8, bottom: 0, left: 0}}>
        <defs>
          <linearGradient id="gReq" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--chart-1)" stopOpacity={0.35} />
            <stop offset="100%" stopColor="var(--chart-1)" stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
        <XAxis dataKey="day" tickLine={false} axisLine={false} fontSize={11}
               stroke="var(--muted-foreground)" />
        <YAxis tickLine={false} axisLine={false} fontSize={11}
               stroke="var(--muted-foreground)" />
        <Tooltip
          contentStyle={{
            background: 'var(--popover)',
            border: '1px solid var(--border)',
            borderRadius: 12,
            fontSize: 12,
          }}
        />
        <Area
          type="monotone"
          dataKey="requests"
          name={requestsLabel}
          stroke="var(--chart-1)"
          fill="url(#gReq)"
          strokeWidth={2}
        />
        {/* 失败曲线单独画（虚线、不发散填充）：它来自另一份数据源，
            与请求数不是「同一条曲线的两个部分」，不该堆叠。 */}
        <Area
          type="monotone"
          dataKey="failed"
          name={failedLabel}
          stroke="var(--destructive)"
          fill="none"
          strokeWidth={1.5}
          strokeDasharray="4 3"
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

/** 积分收支（双 Y 轴：收入常是几千、支出常是几十，同一根轴会把支出压平）。 */
export function FlowChart({
  data,
  incomeLabel,
  expenseLabel,
}: {
  data: FlowPoint[];
  incomeLabel: string;
  expenseLabel: string;
}) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      {/* 左边距不能给负数：轴宽 48 减去它才是刻度的可用宽度，
          4 位数的刻度会越出图表左边缘被裁掉（issue #184 的遮挡就是这么来的）。 */}
      <LineChart data={data} margin={{top: 4, right: 4, bottom: 0, left: 0}}>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
        <XAxis dataKey="day" tickLine={false} axisLine={false} fontSize={11}
               stroke="var(--muted-foreground)" />
        <YAxis yAxisId="inc" tickLine={false} axisLine={false} fontSize={11}
               stroke="var(--chart-2)" width={52} />
        <YAxis yAxisId="exp" orientation="right" tickLine={false} axisLine={false}
               fontSize={11} stroke="var(--destructive)" width={48} />
        <Tooltip
          contentStyle={{
            background: 'var(--popover)',
            border: '1px solid var(--border)',
            borderRadius: 12,
            fontSize: 12,
          }}
        />
        <Legend wrapperStyle={{fontSize: 11}} />
        <Line yAxisId="inc" type="monotone" dataKey="income"
              name={incomeLabel} stroke="var(--chart-2)"
              strokeWidth={2} dot={false} />
        <Line yAxisId="exp" type="monotone" dataKey="expense"
              name={expenseLabel} stroke="var(--destructive)"
              strokeWidth={2} dot={false} />
      </LineChart>
    </ResponsiveContainer>
  );
}
