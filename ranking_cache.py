"""One small, bounded, read-only snapshot for ranking requests."""
import asyncio
import copy
import time

class RankingCache:
    def __init__(self, ttl=30, clock=time.monotonic):
        self.ttl,self.clock=ttl,clock
        self.lock=asyncio.Lock()
        self.key=None
        self.value=None
        self.until=0
    async def get(self, key, loader):
        async with self.lock:
            if self.key!=key or self.value is None or self.clock()>=self.until:
                value=await loader()
                self.value=copy.deepcopy(value)
                self.key=key
                self.until=self.clock()+self.ttl
            return copy.deepcopy(self.value)
