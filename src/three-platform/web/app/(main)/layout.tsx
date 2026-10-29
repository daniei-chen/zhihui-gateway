'use client';

import {memo, useEffect, useState} from 'react';
import {usePathname, useRouter} from 'next/navigation';
import {ManagementBar} from '@/components/common/layout/ManagementBar';
import {CommandPalette} from '@/components/common/layout/CommandPalette';
import {RealmToggle} from '@/components/common/layout/RealmToggle';
import {PlatformToggle} from '@/components/common/layout/PlatformToggle';
import {LanguageToggle} from '@/components/common/layout/LanguageToggle';
import {Button} from '@/components/ui/button';
import {BrandHome} from '@/components/common/layout/BrandHome';
import {ConsoleAtmosphere} from '@/components/common/layout/ConsoleAtmosphere';
import {RealmProvider} from '@/lib/realm-context';
import {PlatformProvider, PlatformScope} from '@/lib/platform-context';
import {useAuth} from '@/lib/auth-context';
import {useT} from '@/lib/i18n/provider';
import {errText} from '@/lib/api';
import {notify} from '@/lib/toast';

const MemoizedManagementBar = memo(ManagementBar);

export default function MainLayout({
  children,
}: {
  children: React.ReactNode
}) {
  const {me, loading, logout} = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const atIntake = pathname.replace(/\/+$/, '') === '/accounts';
  const limited = me?.role === 'account_admin';
  const t = useT();
  const [loggingOut, setLoggingOut] = useState(false);

  async function signOut() {
    if (loggingOut) return;
    setLoggingOut(true);
    try { await logout(); }
    catch (error) { notify.err(errText(error)); }
    finally { setLoggingOut(false); }
  }

  // Do not mount protected pages until the authoritative role is known.
  useEffect(() => {
    if (!loading && !me) router.replace('/login');
    else if (!loading && limited && !atIntake) router.replace('/accounts');
  }, [loading, me, router, limited, atIntake]);

  return (
    <RealmProvider>
      <PlatformProvider>
      <div className="min-h-screen flex flex-col">
        <ConsoleAtmosphere />
        {/*
          键盘用户的「跳转到主内容」链接（K38）。
          底栏有十几个入口，每次切页都要 Tab 过一整排才能到内容区；这个链接平时
          移出视口（`.skip-link` 见 globals.css），一按 Tab 就出现，回车直接跳到
          `#main-content`（`<main tabIndex={-1}>`，否则无 focusable 目标）。
        */}
        <a href="#main-content" className="skip-link">
          {t('common.skipToContent')}
        </a>
        {!loading && me && !limited && <MemoizedManagementBar />}
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
                {/* Shared brand stays visible; narrow screens wrap the control rail below it. */}
                <div className="zh-console-header">
                  <BrandHome />
                  <div className="zh-console-controls">
                    {!loading && me && !limited && <CommandPalette />}
                    {/* 平台（WorkBuddy / Qoder / Trae）是大维度，版本是它下面的细分 */}
                    <PlatformToggle />
                    <RealmToggle />
                    <LanguageToggle />
                    {limited && <Button variant="outline" className="rounded-full text-xs" disabled={loggingOut} onClick={signOut}>{t('profile.logout')}</Button>}
                  </div>
                </div>
                {/*
                  平台隔离的关键：切换平台时把内容子树整体重挂载。
                  少了这一层，页面组件不会重建，`useAsyncAll` 的 deps 里又没有 platform，
                  于是设置页仍显示上一个平台的配置、列表仍是上一个平台的数据。

                  挂载用的是 **epoch** 而不是 platform：见 lib/platform-context.tsx 的
                  说明——水合到持久化平台不算「切换」，否则首屏数据会被请求两遍。
                */}
                <PlatformScope>{!loading && me && (!limited || atIntake) ? children : null}</PlatformScope>
              </main>
            </div>
          </div>
        </div>
      </div>
      </PlatformProvider>
    </RealmProvider>
  );
}
