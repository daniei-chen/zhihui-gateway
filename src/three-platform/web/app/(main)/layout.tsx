'use client';

import {memo, useEffect} from 'react';
import {useRouter} from 'next/navigation';
import {ManagementBar} from '@/components/common/layout/ManagementBar';
import {CommandPalette} from '@/components/common/layout/CommandPalette';
import {RealmToggle} from '@/components/common/layout/RealmToggle';
import {PlatformToggle} from '@/components/common/layout/PlatformToggle';
import {RealmProvider} from '@/lib/realm-context';
import {PlatformProvider, PlatformScope} from '@/lib/platform-context';
import {useAuth} from '@/lib/auth-context';
import {useT} from '@/lib/i18n/provider';

const MemoizedManagementBar = memo(ManagementBar);

export default function MainLayout({
  children,
}: {
  children: React.ReactNode
}) {
  const {me, loading} = useAuth();
  const router = useRouter();
  const t = useT();

  // 仅在确认未登录时跳转；不阻塞内容渲染，避免每次切页闪一下
  useEffect(() => {
    if (!loading && !me) router.replace('/login');
  }, [loading, me, router]);

  return (
    <RealmProvider>
      <PlatformProvider>
      <div className="min-h-screen flex flex-col">
        {/*
          键盘用户的「跳转到主内容」链接（K38）。
          底栏有十几个入口，每次切页都要 Tab 过一整排才能到内容区；这个链接平时
          移出视口（`.skip-link` 见 globals.css），一按 Tab 就出现，回车直接跳到
          `#main-content`（`<main tabIndex={-1}>`，否则无 focusable 目标）。
        */}
        <a href="#main-content" className="skip-link">
          {t('common.skipToContent')}
        </a>
        <MemoizedManagementBar />
        <div className="flex flex-1 flex-col">
          <div className="@container/main flex flex-1 flex-col gap-2">
            <div className="flex min-h-0 flex-1 flex-col px-4 pt-12 py-8 sm:px-6 md:px-8 lg:px-12">
              {/*
                用 <main> 而不是 <div>：读屏软件靠 landmark 在页面区域间跳转，
                原来整页只有一堆 div，没有「主内容」这个地标。
              */}
              <main
                id="main-content"
                tabIndex={-1}
                className="mx-auto flex min-h-0 w-full max-w-7xl flex-1 flex-col gap-4 pb-24 outline-none md:gap-6"
              >
                {/*
                  版本切换固定在右上角：底栏是照 LDC 原样保留的，控件不往那里加。
                  移动端只显示图标（compact），避免窄屏被它占掉一行。

                  命令面板（⌘K）挂在同一行，且放在最左：它是**入口**，语言与版本是
                  **状态**，入口排在状态前面才符合扫读顺序。放在布局里而不是某一页里，
                  是因为它必须在每一页都能按得出来（快捷键监听也挂在它自己身上）。
                */}
                <div className="flex flex-wrap items-center justify-end gap-2">
                  <CommandPalette />
                  {/* 平台（WorkBuddy / Qoder / Trae）是大维度，版本是它下面的细分 */}
                  <PlatformToggle />
                  <RealmToggle />
                </div>
                {/*
                  平台隔离的关键：切换平台时把内容子树整体重挂载。
                  少了这一层，页面组件不会重建，`useAsyncAll` 的 deps 里又没有 platform，
                  于是设置页仍显示上一个平台的配置、列表仍是上一个平台的数据。

                  挂载用的是 **epoch** 而不是 platform：见 lib/platform-context.tsx 的
                  说明——水合到持久化平台不算「切换」，否则首屏数据会被请求两遍。
                */}
                <PlatformScope>{children}</PlatformScope>
              </main>
            </div>
          </div>
        </div>
      </div>
      </PlatformProvider>
    </RealmProvider>
  );
}
