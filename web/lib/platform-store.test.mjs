/**
 * K32 测试：平台状态双来源收敛（lib/platform-store.ts）。
 *
 * 跑法：`cd web && node --test lib/*.test.mjs`
 *
 * 这里钉的是**不变量**，不是实现细节：
 *   1. 请求上下文（`get`/`explicit`）与界面（`peek`）最终是同一个值；
 *   2. 水合**不放 epoch**（水合若放 epoch，`PlatformScope` 会重挂载 → 首屏数据
 *      请求两遍）；
 *   3. 用户切换与跨标签页变化**必须**放 epoch（否则页面内容留在旧平台数据上）；
 *   4. 存储被禁/无痕/脏值：任何一条路径都不抛，且退化成默认值；
 *   5. 从未显式选择过平台时 `explicit()` 为 null（请求不带平台头，与原行为一致）。
 *
 * **2026-11-23 起 `PLATFORMS` 只剩 `workbuddy`**（Qoder 11-22 移除、Trae 11-23 下线）。
 * 于是本文件里 `trae` / `qoder` / `doubaowork` 的角色变了：它们不再是「另一个合法
 * 平台」，而是**历史残留值**。这反而让我们能钉两条更要紧的不变量：
 *
 *   · 已下线平台的值出现在存储里时，水合必须自愈成默认值（不能让死值发到后端）；
 *   · `set()` 收到已下线平台时必须**原样返回、不 bump epoch**（不能因为一个
 *     无效操作把整棵子树重挂载）。
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
  // 已下线平台与大小写错写都必须是未知值：放行它们等于让请求带着死平台头出去。
  check('isPlatform rejects retired and malformed values',
    [isPlatform('Qoder'), isPlatform('qoder'), isPlatform('trae'), isPlatform('doubaowork'), isPlatform(null)],
    [false, false, false, false, false]);
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
  // 持久化里的值与首帧默认不同 → 水合后界面值要跟着变，但 epoch 不能动
  const storage = fakeStorage({[PLATFORM_STORAGE_KEY]: DEFAULT_PLATFORM});
  const store = createPlatformStore({storage});
  check('request context sees persisted value before hydration', store.get(), DEFAULT_PLATFORM);
  check('explicit before hydration reads through', store.explicit(), DEFAULT_PLATFORM);
  store.hydrate();
  check('peek follows persisted value after hydration', store.peek(), DEFAULT_PLATFORM);
  check('hydration does not remount subtree (epoch stays 0)', store.getEpoch(), 0);
  check('request context and UI agree after hydration',
    [store.get(), store.peek()], [DEFAULT_PLATFORM, DEFAULT_PLATFORM]);
}
{
  // 脏值：修正为默认值（并落盘），不能让非法值一路发到后端
  // 用已下线平台的旧值当脏值：这正是线上会残留的真实形态
  // （用户 11-22 之前的标签页里存的可能是 trae 或 qoder）。
  for (const stale of ['trae', 'qoder', 'Qoder-Legacy', 'doubaowork']) {
    const storage = fakeStorage({[PLATFORM_STORAGE_KEY]: stale});
    const store = createPlatformStore({storage});
    check(`stale "${stale}" ignored for requests`, store.explicit(), null);
    store.hydrate();
    check(`stale "${stale}" heals to default on disk`, storage.data[PLATFORM_STORAGE_KEY], DEFAULT_PLATFORM);
    check(`stale "${stale}" falls back to default`, store.peek(), DEFAULT_PLATFORM);
  }
}

console.log('用户选择：');
{
  // 未水合时 set 默认平台：memory 从 null 变成具体值，这是「真的变了」
  const storage = fakeStorage();
  const store = createPlatformStore({storage});
  let notifications = 0;
  const unsubscribe = store.subscribe(() => {
    notifications += 1;
  });
  store.set(DEFAULT_PLATFORM);
  check('set updates memory', store.peek(), DEFAULT_PLATFORM);
  check('set writes persistence', storage.data[PLATFORM_STORAGE_KEY], DEFAULT_PLATFORM);
  check('set bumps epoch (subtree must remount)', store.getEpoch(), 1);
  check('subscriber notified', notifications, 1);
  check('request context follows set', store.get(), DEFAULT_PLATFORM);

  // 再设同一个值：不重挂载、不通知（避免无谓请求）
  store.set(DEFAULT_PLATFORM);
  check('same value does not bump epoch', store.getEpoch(), 1);
  check('same value does not notify', notifications, 1);

  unsubscribe();
  store.set(DEFAULT_PLATFORM);
  check('unsubscribe stops notifications', notifications, 1);
  check('set still works after unsubscribe', store.peek(), DEFAULT_PLATFORM);
}
{
  // 已下线平台：set 必须被静默拒绝，而且**不能 bump epoch**
  // （无效操作重挂载整棵子树 = 白掉一次首屏请求）
  const storage = fakeStorage({[PLATFORM_STORAGE_KEY]: DEFAULT_PLATFORM});
  const store = createPlatformStore({storage});
  store.hydrate();
  let notifications = 0;
  store.subscribe(() => {
    notifications += 1;
  });
  for (const retired of ['trae', 'qoder', 'doubaowork']) {
    store.set(retired);
    check(`set("${retired}") is ignored`, store.peek(), DEFAULT_PLATFORM);
  }
  check('retired set does not bump epoch', store.getEpoch(), 0);
  check('retired set does not notify', notifications, 0);
  check('retired set does not overwrite storage', storage.data[PLATFORM_STORAGE_KEY], DEFAULT_PLATFORM);
}

console.log('跨标签页（storage 事件）：');
{
  // 起点：内存里没有值，存储里也没有 → syncFromStorage 读到 null = 默认值
  const storage = fakeStorage();
  const store = createPlatformStore({storage});
  let notifications = 0;
  store.subscribe(() => {
    notifications += 1;
  });

  // 另一个标签页写入了合法值：本页必须跟着变并重挂载
  storage.data[PLATFORM_STORAGE_KEY] = DEFAULT_PLATFORM;
  store.syncFromStorage();
  check('external change updates UI value', store.peek(), DEFAULT_PLATFORM);
  check('external change bumps epoch (must refetch)', store.getEpoch(), 1);
  check('external change notifies', notifications, 1);

  // 存储事件重复到达（同值）：不重复重挂载
  store.syncFromStorage();
  check('same external value does not bump epoch again', store.getEpoch(), 1);

  // 另一个标签页清空存储：回到默认值（值没变 → 不重挂载）
  delete storage.data[PLATFORM_STORAGE_KEY];
  store.syncFromStorage();
  check('cleared storage falls back to default', store.peek(), DEFAULT_PLATFORM);
  check('cleared storage to same value does not bump epoch', store.getEpoch(), 1);

  // 非法值从 storage 事件进来：忽略值本身，按默认处理，但不能抛
  store.setFromStorageValue('not-a-platform');
  check('invalid storage event value -> default', store.peek(), DEFAULT_PLATFORM);

  // 已下线平台从别的标签页进来（老标签页还在用 trae）：自愈成默认值
  store.setFromStorageValue('trae');
  check('retired storage event value -> default', store.peek(), DEFAULT_PLATFORM);
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
  store.set(DEFAULT_PLATFORM); // 写不进去也不能抛
  check('set works in memory when storage is read-only', store.peek(), DEFAULT_PLATFORM);
  store.syncFromStorage(); // getItem 抛 → 回退默认值
  check('sync falls back to default when storage throws', store.peek(), DEFAULT_PLATFORM);
}
{
  // 无存储实例（SSR / 被策略禁用）：全部退化成内存
  const store = createPlatformStore({storage: null});
  check('null storage: default', store.get(), DEFAULT_PLATFORM);
  check('null storage: not explicit yet', store.explicit(), null);
  store.set(DEFAULT_PLATFORM);
  check('null storage: memory only', store.peek(), DEFAULT_PLATFORM);
  check('null storage: explicit after set', store.explicit(), DEFAULT_PLATFORM);
  // 水合不能把已经做出的选择改回默认值（只有持久化里真有值时才覆盖）
  check('null storage: hydrate keeps explicit choice', (store.hydrate(), store.peek()), DEFAULT_PLATFORM);
}

console.log('平台枚举：只剩 WorkBuddy：');
{
  // 2026-11-22：qoder 与 doubaowork 下线。
  // 2026-11-23：trae 下线，网关收敛到一条线。
  // 枚举收窄本身不能改变默认值：平台减少不等于默认平台换人。
  check('PLATFORMS 只剩一个平台', PLATFORMS.length, 1);
  check('PLATFORMS 含 workbuddy', PLATFORMS.includes('workbuddy'), true);
  check('PLATFORMS 不含 trae（已下线）', PLATFORMS.includes('trae'), false);
  check('PLATFORMS 不含 qoder（已下线）', PLATFORMS.includes('qoder'), false);
  check('PLATFORMS 不含 doubaowork（已下线）', PLATFORMS.includes('doubaowork'), false);
  check('isPlatform 拒绝 trae', isPlatform('trae'), false);
  check('isPlatform 拒绝 qoder', isPlatform('qoder'), false);
  check('isPlatform 拒绝 doubaowork', isPlatform('doubaowork'), false);
  check('DEFAULT_PLATFORM 仍是 workbuddy', DEFAULT_PLATFORM, 'workbuddy');

  // 持久化里是已下线平台时，水合必须自愈成默认值（不能让死值一直发到后端）
  for (const retired of ['trae', 'doubaowork']) {
    const persisted = fakeStorage();
    persisted.setItem(PLATFORM_STORAGE_KEY, retired);
    const hydrating = createPlatformStore({storage: persisted});
    check(`水合把已下线的 ${retired} 纠正为默认`, hydrating.hydrate(), DEFAULT_PLATFORM);
    check(`${retired} 纠正后的值已落盘`, persisted.data[PLATFORM_STORAGE_KEY], DEFAULT_PLATFORM);
    check(`${retired} 水合不放 epoch`, hydrating.getEpoch(), 0);
  }
}

console.log('隔离性：');
{
  // 两个 store 实例互不影响（测试之间不共享模块级状态）
  const a = createPlatformStore({storage: fakeStorage()});
  const b = createPlatformStore({storage: fakeStorage()});
  a.set(DEFAULT_PLATFORM);
  check('instances are independent',
    [a.explicit(), b.explicit()], [DEFAULT_PLATFORM, null]);
}

console.log(failed ? `\n${failed} failed / ${passed} passed` : `\nall passed（${passed} 条）`);
process.exit(failed ? 1 : 0);
