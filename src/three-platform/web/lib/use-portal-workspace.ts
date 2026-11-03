'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import {PortalError, portalFetch, type PortalOverview} from './portal-api';
import {useHeartbeat} from './use-heartbeat';

type Identity = {id: string; username: string};
type Workspace = {me: Identity | null; data: PortalOverview | null; anonymous: boolean; pending: boolean; error: string};
const EMPTY: Workspace = {me: null, data: null, anonymous: false, pending: true, error: ''};

export function usePortalWorkspace() {
  const [state, setState] = useState<Workspace>(EMPTY);
  const sequence = useRef(0);
  const active = useRef<{controller: AbortController; promise: Promise<boolean>} | null>(null);
  const invalidate = useCallback(() => {
    sequence.current++;
    active.current?.controller.abort();
    active.current = null;
  }, []);
  const reset = useCallback(() => {
    invalidate();
    setState({...EMPTY, anonymous: true, pending: false});
  }, [invalidate]);

  const run = useCallback((background: boolean): Promise<boolean> => {
    if (background && active.current) return active.current.promise;
    invalidate();
    const generation = sequence.current;
    const controller = new AbortController();
    const current = () => !controller.signal.aborted && generation === sequence.current;
    setState(previous => ({...previous, pending: true}));
    const promise = Promise.resolve().then(async () => {
      if (!current()) return false;
      try {
        const me = await portalFetch<Identity>('portal/me', {signal: controller.signal});
        if (!current()) return false;
        setState(previous => ({...previous, me, anonymous: false, data: previous.me?.id === me.id ? previous.data : null}));
        const data = await portalFetch<PortalOverview>('portal/overview', {signal: controller.signal});
        if (!current()) return false;
        setState({me, data, anonymous: false, pending: false, error: ''});
        return true;
      } catch (err) {
        if (!current()) return false;
        if (err instanceof PortalError && (err.status === 401 || err.status === 403)) {
          setState({...EMPTY, anonymous: true, pending: false,
            error: err.status === 403 ? err.message : ''});
        } else {
          setState(previous => ({...previous, pending: false,
            error: err instanceof Error ? err.message : '读取失败，请重新连接。'}));
        }
        return false;
      } finally {
        if (current()) active.current = null;
      }
    });
    active.current = {controller, promise};
    return promise;
  }, [invalidate]);

  const reload = useCallback(() => run(false), [run]);
  const connect = useCallback(() => {
    setState(EMPTY);
    return run(false);
  }, [run]);
  const refresh = useCallback(() => run(true), [run]);
  useEffect(() => {
    void reload();
    return invalidate;
  }, [reload, invalidate]);
  useHeartbeat(refresh, 20000);
  return {...state, reload, connect, reset};
}
