"""Own the startup password audit through application shutdown."""
import logging
import threading

from .. import security
from .asyncwork import joined_thread

log=logging.getLogger(__name__)


class WeakAdminCheck:
    def __init__(self):
        self.stop=threading.Event()
        self.thread=None

    def start(self):
        if self.thread is not None or self.stop.is_set():
            raise RuntimeError('startup password audit cannot be restarted')
        thread=threading.Thread(target=self._run,daemon=True,name='wb-weak-pwd-check')
        thread.start()
        self.thread=thread

    def _run(self):
        try:
            weak=security.weak_admin_passwords(stop=self.stop)
            if weak and not self.stop.is_set():
                log.error('管理员仍在使用常见弱口令：%s。请在设置中修改。','、'.join(weak))
        except Exception:
            log.exception('weak admin password check failed')

    async def close(self):
        self.stop.set()
        if self.thread is not None:
            await joined_thread(self.thread.join)
