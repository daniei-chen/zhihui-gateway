import Link from 'next/link';
import {PRODUCT_NAME} from '@/lib/product';

export function BrandHome() {
  return (
    <Link href="/dashboard" className="zh-console-brand" data-slot="console-brand">
      <svg aria-hidden="true" className="zh-console-brand-mark" viewBox="0 0 40 40" fill="none">
        <path d="M8.5 10.5H31.5L8.5 29.5H31.5" stroke="currentColor" strokeWidth="4.8" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      <span className="zh-console-brand-copy">
        <span className="zh-console-brand-name">{PRODUCT_NAME}</span>
        <span className="zh-console-brand-caption" aria-hidden="true">ZHIHUI GATEWAY</span>
      </span>
    </Link>
  );
}
