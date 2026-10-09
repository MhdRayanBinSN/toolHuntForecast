import asyncio, time

class DomainLimiter:
    def __init__(self, interval=0.75): self.interval=interval; self.last={}; self.lock=asyncio.Lock()
    async def wait(self, key):
        async with self.lock:
            delay=max(0,self.interval-(time.monotonic()-self.last.get(key,0)))
            if delay: await asyncio.sleep(delay)
            self.last[key]=time.monotonic()

limiter=DomainLimiter()
