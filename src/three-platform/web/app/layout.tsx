import type {Metadata} from 'next';
import {Inter} from 'next/font/google';
import {Toaster} from '@/components/ui/sonner';
import {ThemeProvider} from '@/components/common/layout/ThemeProvider';
import {AuthProvider} from '@/lib/auth-context';
import {I18nProvider} from '@/lib/i18n/provider';
import {withBasePath} from '@/lib/base-path';
import './globals.css';

const inter = Inter({
  variable: '--font-inter',
  subsets: ['latin'],
  display: 'swap',
});

/**
 * 中文字体走**系统字体栈**，不再下载 CJK webfont（K37 首屏）。
 *
 * 为什么删掉 `next/font/google` 的 Noto Sans SC：CJK 的 woff2 在 Next 里按
 * unicode-range 切成几十个小文件，浏览器按页面实际出现的汉字去挑 —— 实测
 * 首屏因此下载了 **12 个 woff2 / 680 KB（占首屏 28%）**。中文界面几乎每个
 * 字都可能命中不同子集，所以「只带必要子集」在这种切分方式下无法通过配置
 * 实现：要么全带，要么不带。项目又有离线构建约束（next/font/google 的字体
 * 是构建期下载后自托管的，离线环境不可复现），因此选择**不带 CJK webfont**。
 *
 * 视觉影响（需要在 evidence 里对比截图确认）：中文字形从 Noto Sans SC 变成
 * 各平台系统中文（Windows 微软雅黑 / macOS 苹方 / Linux Noto CJK）。字重、
 * 字号、行高、布局都不变 —— 变的是字形本身；CJK 系统字体在 14/16px 的 UI 上
 * 通常比 Noto Sans SC 更清晰（hinting 更贴合系统）。
 *
 * 变量名 `--font-noto-sans-sc` 必须保留：`app/globals.css` 的
 * `--font-sans: var(--font-inter), var(--font-noto-sans-sc), ...` 引用了它，
 * 而 globals.css 由并行的可访问性任务（R2-B）维护，这里不改它。把变量定义成
 * 系统 CJK 栈即可让那条 font-family 继续成立，且**不会**因为 var() 未定义而
 * 让整条 font-family 声明失效（那会掉到浏览器默认字体，排版全变）。
 */
const CJK_FONT_STACK = [
  '"Microsoft YaHei UI"',
  '"Microsoft YaHei"',
  '"PingFang SC"',
  '"Hiragino Sans GB"',
  '"Source Han Sans SC"',
  '"Noto Sans CJK SC"',
  'system-ui',
  '-apple-system',
  '"Segoe UI"',
  'sans-serif',
].join(', ');

export const metadata: Metadata = {
  title: {
    template: '%s - 智惠网关',
    default: '智惠网关',
  },
  /**
   * 站点描述用英文：它是构建期写进静态 HTML 的元数据，无法跟随运行时语言切换
   * （管理端本身不做语言路由，见 lib/i18n/config.ts 的说明）。选英文是因为
   * 分享卡片 / 搜索引擎抓取时它面向的受众最广；界面内的文案全部走 i18n。
   */
  description: '智惠网关 - Qoder / Trae / WorkBuddy 三平台账号池与网关控制台',
  // 图标 / 清单的 URL 也要带部署前缀（PR #60 评审补漏）：Next 只给 `_next`
  // 静态资源与 next/link 跳转补 basePath，**不会**改写 metadata 里的这些字符串
  // ——实测子路径部署下产物里仍是 `href="/favicon/..."`，会被请求到域名根（通常是
  // 另一个站点）而 404，表现为「站点图标不见了、PWA 清单取不到」。根路径部署时
  // withBasePath 原样返回，行为不变。
  manifest: withBasePath('/favicon/site.webmanifest'),
  icons: {
    icon: [
      {url: withBasePath('/favicon/favicon-32x32.png'), sizes: '32x32', type: 'image/png'},
      {url: withBasePath('/favicon/favicon-16x16.png'), sizes: '16x16', type: 'image/png'},
    ],
    shortcut: withBasePath('/favicon/favicon.ico'),
    apple: withBasePath('/favicon/apple-touch-icon.png'),
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  // 内联在 <html> 上：`:root { --font-noto-sans-sc: ... }` 属于 globals.css（R2-B 维护），
  // 这里用同优先级的 inline custom property 覆盖它 —— inline style 优先于 :root 规则，
  // 且 `--font-sans` 的 var() 在使用处解析，所以不需要改 globals.css。
  const fontVars = {'--font-noto-sans-sc': CJK_FONT_STACK} as unknown as React.CSSProperties;
  return (
    <html
      lang="zh-CN"
      style={fontVars}
      className={`${inter.variable} hide-scrollbar font-sans`}
      suppressHydrationWarning
    >
      <body
        className={`${inter.variable} hide-scrollbar font-sans antialiased`}
      >
        <ThemeProvider
          attribute="class"
          defaultTheme="system"
          enableSystem
          disableTransitionOnChange
        >
          <I18nProvider>
            <AuthProvider>
              {children}
              <Toaster />
            </AuthProvider>
          </I18nProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
