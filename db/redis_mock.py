import time
import threading
import logging

logger = logging.getLogger(__name__)

class MockRedis:
    """
    A thread-safe in-memory mock of the Redis client.
    Implements all commands and pipeline patterns used by the Hermes-ADK scheduler.
    """
    _store = {}
    _expiries = {}
    _lock = threading.Lock()

    def __init__(self, *args, **kwargs):
        logger.info("Initializing in-memory MockRedis instance.")

    def get(self, key: str) -> str:
        with self._lock:
            # Check expiration
            if key in self._expiries:
                if time.time() > self._expiries[key]:
                    self._store.pop(key, None)
                    self._expiries.pop(key, None)
                    return None
            val = self._store.get(key)
            return str(val) if val is not None else None

    def set(self, key: str, value: str, nx: bool = False, ex: int = None) -> bool:
        with self._lock:
            # Check expiration to clear stale keys
            if key in self._expiries:
                if time.time() > self._expiries[key]:
                    self._store.pop(key, None)
                    self._expiries.pop(key, None)
            
            # Check NX condition
            if nx and (key in self._store):
                return False
                
            self._store[key] = str(value)
            if ex is not None:
                self._expiries[key] = time.time() + ex
            else:
                self._expiries.pop(key, None)
            return True

    def delete(self, key: str) -> int:
        with self._lock:
            self._expiries.pop(key, None)
            if key in self._store:
                self._store.pop(key)
                return 1
            return 0

    def eval(self, script: str, numkeys: int, key: str, arg: str) -> int:
        """
        Mock implementation of redis eval command.
        Targeted specifically to mimic the atomic unlock script.
        """
        # Unlock logic: if get(KEYS[1]) == ARGV[1] then delete KEYS[1]
        with self._lock:
            current = self.get(key)
            if current == str(arg):
                self.delete(key)
                return 1
            return 0

    class MockPipeline:
        def __init__(self, outer):
            self.outer = outer
            
        def watch(self, *args, **kwargs):
            pass
            
        def multi(self):
            pass
            
        def delete(self, key: str):
            self.outer.delete(key)
            
        def execute(self):
            return [1]

    def pipeline(self) -> MockPipeline:
        return self.MockPipeline(self)
