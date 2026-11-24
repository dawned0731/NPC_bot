import copy
import unittest
from datetime import datetime
from unittest.mock import patch
from season_transition import KST
from season_runtime import close_if_due, open_regular
from test_season_transition import fixture,CAL

class Ref:
    def __init__(self,db,path):self.db,self.path=db,path
    def get(self):return copy.deepcopy(self.db.root.get(self.path))
    def transaction(self,callback):
        self.db.calls+=1
        self.db.root=callback(copy.deepcopy(self.db.root))
        return copy.deepcopy(self.db.root)
class DB:
    def __init__(self,root):self.root=root;self.calls=0
    def reference(self,path=None):return Ref(self,path)
class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_offline_recovery_closes_then_opens_once(self):
        root=fixture();root['season_state'].pop('preseason_cleared_for')
        db=DB(root)
        dates={'preseason_end':'2026-11-30'}
        g={'db':db,'_season_dates_for_type':lambda y,k:dates}
        class Clock:
            @staticmethod
            def now(tz):return datetime(2026,12,1,0,10,tzinfo=KST)
        with patch('season_runtime.datetime',Clock):
            self.assertTrue(await close_if_due(g))
            self.assertEqual(db.root['exp_data']['42']['exp'],0)
            await open_regular(g,db.root['season_state'],CAL)
            db.root['exp_data']['42']['exp']=50
            self.assertFalse(await close_if_due({'db':db,'_season_dates_for_type':lambda y,k:{'preseason_end':'2027-02-28'}}))
            self.assertEqual(db.root['exp_data']['42']['exp'],50)
            self.assertEqual(db.root['season_state']['current_season_id'],'2026_winter')
            self.assertEqual(db.calls,2)
    async def test_unprepared_cutoff_does_not_clear(self):
        root=fixture();root['season_state'].pop('preseason_cleared_for');root['season_state']['next_ready']=False;db=DB(root)
        class Clock:
            @staticmethod
            def now(tz):return datetime(2026,11,30,23,0,tzinfo=KST)
        with patch('season_runtime.datetime',Clock):
            await close_if_due({'db':db,'_season_dates_for_type':lambda y,k:{'preseason_end':'2026-11-30'}})
        self.assertEqual(db.root['exp_data']['42']['exp'],1381600)
        self.assertEqual(db.root['season_state']['status'],'locked')
