import Link from 'next/link';
import {useId} from 'react';
import {PRODUCT_NAME} from '@/lib/product';

export function BrandHome() {
  const gradient = useId();
  return (
    <Link href="/dashboard" className="zh-console-brand" data-slot="console-brand">
      <svg aria-hidden="true" className="zh-console-brand-mark" viewBox="0 0 40 40" fill="none">
        <defs><linearGradient id={gradient} x1="6" y1="5" x2="34" y2="35" gradientUnits="userSpaceOnUse">
          <stop stopColor="#7391FA" /><stop offset="1" stopColor="#4D6CEB" />
        </linearGradient></defs>
        <path d="M8.5 10.5H31.5L8.5 29.5H31.5" stroke={`url(#${gradient})`} strokeWidth="5.6" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      <span className="zh-console-brand-copy">
        <span className="zh-console-brand-name">{PRODUCT_NAME}</span>
        <span className="zh-console-brand-caption" aria-hidden="true">ZHIHUI GATEWAY</span>
      </span>
    </Link>
  );
}
