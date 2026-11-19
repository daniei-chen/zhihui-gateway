'use client';

import {AlertTriangle} from 'lucide-react';
import {useEffect, useState} from 'react';

import {platformApi} from '@/lib/api';
import {useT} from '@/lib/i18n/provider';
import {usePlatform} from '@/lib/platform-context';
import {cn} from '@/lib/utils';

/**
 * 「当前平台未启用」横幅。
 *
 * 背景：平台切换器的四个 pill 是**无条件显示**的（既有设计，平台选择是纯客户端
 * 状态），而 `QTG_DOUBAOWORK_ENGINE` 默认是 `off`。没有这条提示，用户切到豆包工作
 * 只会看到一片空账号池，第一反应是「账号全丢了」——而事实是「引擎没挂」。
 *
 * 判据来自 `/api/platforms` 的 `enabled`（机器可读的布尔值），不是去解析中文
 * `note`：后者既脆弱又不可翻译。
 */
export function PlatformDisabledNotice({className}: {className?: string}) {
  const {platform} = usePlatform();
  const t = useT();
  const [note, setNote] = useState('');

  useEffect(() => {
    let alive = true;
    setNote('');
    void (async () => {
      try {
        const data = await platformApi.list();
        if (!alive) return;
        const row = data.platforms.find(p => p.id === platform);
        // `enabled === false` 才提示；字段缺失（旧后端）按「已启用」处理，
        // 免得升级顺序不一致时凭空弹出警告。
        setNote(row && row.enabled === false ? (row.note || t('platform.disabledNotice')) : '');
      } catch {
        // 拿不到能力矩阵就不提示：当前页自己的请求失败会把问题暴露出来，
        // 这里再叠一条只会让用户看到两个错误。
      }
    })();
    return () => {
      alive = false;
    };
  }, [platform, t]);

  if (!note) return null;
  return (
    <div
      role="status"
      className={cn(
        'flex items-start gap-2 rounded-[16px] border border-amber-500/30 bg-amber-500/10 px-3.5 py-2.5 text-[11px] leading-5 text-amber-700 dark:text-amber-300',
        className,
      )}
    >
      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <span>{note}</span>
    </div>
  );
}
