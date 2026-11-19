/**
 * K32 测试：平台状态双来源收敛（lib/platform-store.ts）。
 *
 * 跑法：`cd src/three-platform/web && node --test lib/*.test.mjs`
 *
 * 这里钉的是**不变量**，不是实现细节：
 *   1. 请求上下文（`get`/`explicit`）与界面（`peek`）最终是同一个值；
 *   2. 水合**不放 epoch**（水合若放 epoch，`PlatformScope` 会重挂载 → 首屏数据
 *      请求两遍）；
 *   3. 用户切换与跨标签页变化**必须**放 epoch（否则页面内容留在旧平台数据上）；
 *   4. 存储被禁/无痕/脏值：任何一条路径都不抛，且退化成默认值；
 *   5. 从未显式选择过平台时 `explicit()` 为 null（请求不带平台头，与原行为一致）。
 */
import {
  DEFAULT_PLATFORM,
  PLATFORMS,
  PLATFORM_STORAGE_KEY,
  createPlatformStore,
  isPlatform,
} from './platform-store.ts';

let failed = 0;
let passed = 0;

function check(name, got, want) {
  const g = JSON.stringify(got);
  const w = JSON.stringify(want);
  if (g === w) {
    passed += 1;
    return;
  }
  failed += 1;
  console.log(`  FAIL ${name}\n       got  ${g}\n       want ${w}`);
}

/** 可控假存储：可读、可写、可模拟抛错 */
function fakeStorage(initial = {}) {
  const data = {...initial};
  let getThrows = false;
  let setThrows = false;
  return {
    data,
    failGet(v) {
      getThrows = v;
    },
    failSet(v) {
      setThrows = v;
    },
    getItem(k) {
      if (getThrows) throw new Error('storage disabled');
      return k in data ? data[k] : null;
    },
    setItem(k, v) {
      if (setThrows) throw new Error('quota exceeded');
      data[k] = v;
    },
  };
}

console.log('默认值与显式选择：');
{
  const store = createPlatformStore({storage: fakeStorage()});
  check('peek defaults before hydration', store.peek(), DEFAULT_PLATFORM);
  check('get defaults before hydration', store.get(), DEFAULT_PLATFORM);
  check('never-chosen platform is not explicit', store.explicit(), null);
  // 2026-11-19：`qoder` 已从 PLATFORMS 摘除，所以它现在是**未知值**。
  // `'Qoder'` 则一直是未知值（大小写敏感）。两者都必须被拒。
  check('isPlatform rejects unknown values',
    [isPlatform('Qoder'), isPlatform('qoder'), isPlatform(null)],
    [false, false, false]);
  check('isPlatform accepts the live enum',
    PLATFORMS.map((p) => isPlatform(p)),
    PLATFORMS.map(() => true));
}
{
  // 首次水合：把默认值落盘（原 Provider 行为），且**不放 epoch**
  const storage = fakeStorage();
  const store = createPlatformStore({storage});
  store.hydrate();
  check('first visit persists default', storage.data[PLATFORM_STORAGE_KEY], DEFAULT_PLATFORM);
  check('hydration does not bump epoch', store.getEpoch(), 0);
  check('hydrated flag set', store.isHydrated(), true);
  check('default is now explicit', store.explicit(), DEFAULT_PLATFORM);
}

console.log('水合：不重复请求的关键：');
{
  // 持久化平台 ≠ 首帧默认：水合后界面值要跟着变，但 epoch 不能动
  const storage = fakeStorage({[PLATFORM_STORAGE_KEY]: 'doubaowork'});
  const store = createPlatformStore({storage});
  check('request context sees persisted value before hydration', store.get(), 'doubaowork');
  check('explicit before hydration reads through', store.explicit(), 'doubaowork');
  store.hydrate();
  check('peek follows persisted value after hydration', store.peek(), 'doubaowork');
  check('hydration does not remount subtree (epoch stays 0)', store.getEpoch(), 0);
  check('request context and UI agree after hydration', [store.get(), store.peek()], ['doubaowork', 'doubaowork']);
}
{
  // 脏值：修正为默认值（并落盘），不能让非法值一路发到后端
  const storage = fakeStorage({[PLATFORM_STORAGE_KEY]: 'DoubaoWork-Legacy'});
  const store = createPlatformStore({storage});
  check('invalid stored value ignored for requests', store.explicit(), null);
  store.hydrate();
  check('invalid stored value heals to default', storage.data[PLATFORM_STORAGE_KEY], DEFAULT_PLATFORM);
  check('invalid value falls back to default', store.peek(), DEFAULT_PLATFORM);
}

console.log('用户切换：');
{
  const storage = fakeStorage();
  const store = createPlatformStore({storage});
  store.hydrate();
  let notifications = 0;
  const unsubscribe = store.subscribe(() => {
    notifications += 1;
  });
  store.set('trae');
  check('set updates memory', store.peek(), 'trae');
  check('set writes persistence', storage.data[PLATFORM_STORAGE_KEY], 'trae');
  check('set bumps epoch (subtree must remount)', store.getEpoch(), 1);
  check('subscriber notified', notifications, 1);
  check('request context follows set', store.get(), 'trae');

  // 点同一个平台：不重挂载、不通知（避免无谓请求），但仍保证存储里是它
  store.set('trae');
  check('same value does not bump epoch', store.getEpoch(), 1);
  check('same value does not notify', notifications, 1);

  unsubscribe();
  store.set('workbuddy');
  check('unsubscribe stops notifications', notifications, 1);
  check('set still works after unsubscribe', store.peek(), 'workbuddy');
}

console.log('跨标签页（storage 事件）：');
{
  const storage = fakeStorage({[PLATFORM_STORAGE_KEY]: 'workbuddy'});
  const store = createPlatformStore({storage});
  store.hydrate();
  let notifications = 0;
  store.subscribe(() => {
    notifications += 1;
  });

  // 另一个标签页切到 doubaowork
  storage.data[PLATFORM_STORAGE_KEY] = 'doubaowork';
  store.syncFromStorage();
  check('external change updates UI value', store.peek(), 'doubaowork');
  check('external change bumps epoch (must refetch)', store.getEpoch(), 1);
  check('external change notifies', notifications, 1);

  // 存储事件重复到达（同值）：不重复重挂载
  store.syncFromStorage();
  check('same external value does not bump epoch again', store.getEpoch(), 1);

  // 另一个标签页清空存储：回到默认值，同样要重挂载
  delete storage.data[PLATFORM_STORAGE_KEY];
  store.syncFromStorage();
  check('cleared storage falls back to default', store.peek(), DEFAULT_PLATFORM);
  check('cleared storage bumps epoch', store.getEpoch(), 2);

  // 非法值从 storage 事件进来：忽略值本身，按默认处理，但不能抛
  store.setFromStorageValue('not-a-platform');
  check('invalid storage event value -> default', store.peek(), DEFAULT_PLATFORM);
}

console.log('存储被禁 / 无痕：');
{
  const storage = fakeStorage();
  storage.failGet(true);
  storage.failSet(true);
  const store = createPlatformStore({storage});
  check('throwing getItem does not throw on get', store.get(), DEFAULT_PLATFORM);
  check('throwing getItem does not throw on explicit', store.explicit(), null);
  store.hydrate(); // 不能抛
  check('hydration survives throwing storage', store.peek(), DEFAULT_PLATFORM);
  store.set('doubaowork'); // 写不进去也不能抛
  check('set works in memory when storage is read-only', store.peek(), 'doubaowork');
  check('set still bumps epoch without persistence', store.getEpoch(), 1);
  store.syncFromStorage(); // getItem 抛 → 回退默认值
  check('sync falls back to default when storage throws', store.peek(), DEFAULT_PLATFORM);
}
{
  // 无存储实例（SSR / 被策略禁用）：全部退化成内存
  const store = createPlatformStore({storage: null});
  check('null storage: default', store.get(), DEFAULT_PLATFORM);
  store.set('trae');
  check('null storage: memory only', store.peek(), 'trae');
  // 水合不能把已经做出的选择改回默认值（只有持久化里真有值时才覆盖）
  check('null storage: hydrate keeps explicit choice', (store.hydrate(), store.peek()), 'trae');
}

console.log('第四平台（doubaowork）：');
{
  // 枚举扩容本身不能改变默认值：新平台进入 PLATFORMS 不等于它成为默认，
  // 也不等于它被默认授权（授权是后端 DEFAULT_PLATFORMS 的事）。
  check('PLATFORMS 含 doubaowork', PLATFORMS.includes('doubaowork'), true);
  check('isPlatform 接受 doubaowork', isPlatform('doubaowork'), true);
  check('DEFAULT_PLATFORM 仍是 workbuddy', DEFAULT_PLATFORM, 'workbuddy');

  const store = createPlatformStore({storage: fakeStorage()});
  store.hydrate();
  check('切换前默认', store.peek(), DEFAULT_PLATFORM);
  store.set('doubaowork');
  check('切到 doubaowork 生效', store.peek(), 'doubaowork');
  check('切到 doubaowork 放 epoch', store.getEpoch(), 1);
  check('切换后请求上下文一致', store.get(), 'doubaowork');
  store.set('doubaowork');
  check('重复设置同一平台不再放 epoch', store.getEpoch(), 1);

  // 持久化里是第四平台时能水合回来，且水合不放 epoch
  const persisted = fakeStorage();
  persisted.setItem(PLATFORM_STORAGE_KEY, 'doubaowork');
  const hydrating = createPlatformStore({storage: persisted});
  check('水合出 doubaowork', hydrating.hydrate(), 'doubaowork');
  check('水合不放 epoch', hydrating.getEpoch(), 0);
}

console.log('隔离性：');
{
  // 两个 store 实例互不影响（测试之间不共享模块级状态）
  const a = createPlatformStore({storage: fakeStorage()});
  const b = createPlatformStore({storage: fakeStorage()});
  a.set('doubaowork');
  check('instances are independent', [a.peek(), b.peek()], ['doubaowork', DEFAULT_PLATFORM]);
}

console.log(failed ? `\n${failed} failed / ${passed} passed` : `\nall passed（${passed} 条）`);
process.exit(failed ? 1 : 0);
