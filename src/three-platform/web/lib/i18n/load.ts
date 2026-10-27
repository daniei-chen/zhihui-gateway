'use client';

/**
 * 语言包按需加载（K37）。
 *
 * `lib/i18n/index.ts` 只静态带**源语言**（简体中文，缺键时的回退）；其余四种
 * 语言在这里用**字面量动态 import** 加载——必须是字面量，打包器才能为每种语言
 * 单独切一个 chunk；写成 `import(\`./locales/${locale}.json\`)` 会把整个目录
 * 打进同一个 chunk，等于没拆。
 *
 * 失败语义：加载失败（离线、旧缓存里 chunk 已不存在）**不抛**，返回 false；
 * 调用方保持源语言界面。这比整页报错好——语言包是增强，不是主流程。
 *
 * 旧 chunk 404 的体验由 `lib/stale-build.ts` 与错误边界处理（K33）。
 */
import {dictRegistry} from './dicts';
import {DEFAULT_LOCALE, type Locale} from './config';

/** 该语言的字典是否已经在内存里（源语言恒为 true） */
export function isLocaleLoaded(locale: Locale): boolean {
  return dictRegistry.has(locale);
}

/**
 * 确保某个语言的字典已注册。
 *
 * @returns 是否可用（源语言恒为 true；动态加载失败为 false）
 */
export async function ensureLocaleDict(locale: Locale): Promise<boolean> {
  if (locale === DEFAULT_LOCALE || dictRegistry.has(locale)) return true;
  try {
    // 注意：每个 case 都是字面量路径，webpack 会各切一个 chunk
    let mod: {default: unknown};
    switch (locale) {
      case 'zh-TW':
        mod = await import('./locales/zh-TW.json');
        break;
      case 'en':
        mod = await import('./locales/en.json');
        break;
      case 'ja':
        mod = await import('./locales/ja.json');
        break;
      case 'ko':
        mod = await import('./locales/ko.json');
        break;
      default:
        return true;
    }
    dictRegistry.register(locale, mod.default as Record<string, unknown>);
    return true;
  } catch {
    return false;
  }
}
