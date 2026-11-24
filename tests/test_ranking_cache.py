import unittest
import asyncio
from unittest.mock import AsyncMock
from ranking_cache import RankingCache

class CacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_requests_share_one_read(self):
        load=AsyncMock(return_value={'42':{'exp':100}});cache=RankingCache()
        rows=await asyncio.gather(*[cache.get('fall',load) for _ in range(20)])
        load.assert_awaited_once();rows[0]['42']['exp']=999
        self.assertEqual((await cache.get('fall',load))['42']['exp'],100)
    async def test_expiration_and_season_change_force_refresh(self):
        tick=[0];cache=RankingCache(clock=lambda:tick[0]);load=AsyncMock(return_value={})
        await cache.get('regular',load);tick[0]=30;await cache.get('regular',load);await cache.get('preseason',load)
        self.assertEqual(load.await_count,3)
    async def test_failed_read_is_not_cached_as_empty(self):
        cache=RankingCache();load=AsyncMock(side_effect=[RuntimeError(),{'42':{}}])
        with self.assertRaises(RuntimeError):await cache.get('season',load)
        self.assertEqual(await cache.get('season',load),{'42':{}})
