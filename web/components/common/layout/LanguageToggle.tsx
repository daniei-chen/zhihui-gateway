'use client';

import {Languages} from 'lucide-react';

import {isLocale, LOCALE_LABELS} from '@/lib/i18n/config';
import {useI18n} from '@/lib/i18n/provider';
import {cn} from '@/lib/utils';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

/**
 * 语言切换。
 *
 * 放在版本切换旁边（页面右上角）：与 RealmToggle 一样不在浮动底栏上加控件，
 * 底栏保持 LDC 原样。选项用各语言的母语写法，选完即写入 localStorage。
 */
export function LanguageToggle({className}: {className?: string}) {
  const {locale, setLocale, t, locales} = useI18n();

  return (
    <Select value={locale} onValueChange={(value) => {
      if (isLocale(value)) setLocale(value);
    }}>
      <SelectTrigger
        data-slot="language-toggle"
        data-locale={locale}
        aria-label={t('language.switch')}
        title={t('language.switch')}
        size="sm"
        className={cn(
          'max-w-full gap-1.5 rounded-full bg-muted/60 px-3 text-xs font-medium data-[size=sm]:h-10 sm:data-[size=sm]:h-8',
          className,
        )}
      >
        <Languages aria-hidden="true" className="size-3.5 shrink-0 opacity-70" />
        <SelectValue><span lang={locale}>{LOCALE_LABELS[locale]}</span></SelectValue>
      </SelectTrigger>
      <SelectContent align="end" className="rounded-2xl border-border/60 p-1 shadow-lg">
        {locales.map((id) => (
          <SelectItem key={id} value={id} className="min-h-10 rounded-lg text-xs sm:min-h-8">
            <span lang={id}>{LOCALE_LABELS[id]}</span>
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
