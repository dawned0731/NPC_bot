import unittest
from datetime import datetime
import season_policy as policy

class PolicyTests(unittest.TestCase):
    def tearDown(self):policy.bind({})
    def test_preseason_rewards_and_cutoff(self):
        state={'first_season_started':True,'settled':True,'status':'preseason','current_season_id':'2026_fall','calendar':{'season_id':'2026_fall','preseason_end':'2026-11-30'}}
        self.assertEqual(policy.multiplier(state,datetime.fromisoformat('2026-11-30T22:59:59+09:00')),10)
        self.assertEqual(policy.multiplier(state,datetime.fromisoformat('2026-11-30T23:00:00+09:00')),0)
        state['settled']=False;self.assertEqual(policy.multiplier(state),0)
    def test_regular_and_locked(self):
        policy.bind({'first_season_started':True,'status':'regular'})
        for amount in (1,30,150,300,1200):self.assertEqual(policy.reward(amount),amount)
        policy.bind({'first_season_started':True,'status':'locked'})
        self.assertEqual(policy.reward(1200),0)
    def test_old_season_state_does_not_award(self):
        self.assertEqual(policy.multiplier({'first_season_started':True,'status':'regular','current_season_id':'2026_fall','calendar':{'season_id':'2026_winter'}}),0)
    def test_runtime_level_over_100(self):
        import ast
        from pathlib import Path
        nodes=[n for n in ast.parse(Path('main.py').read_text(encoding='utf-8')).body if isinstance(n,ast.FunctionDef) and n.name in ('calculate_level','get_level_progress')]
        env={'season_policy':policy,'SEASON_MAX_LEVEL':100,'SEASON_XP_PER_LEVEL':1100,'_clamp01':lambda n:max(0,min(1,n))}
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'main.py','exec'),env)
        policy.bind({'first_season_started':True,'status':'preseason','settled':True})
        self.assertEqual(env['calculate_level'](1381600),1257)
        self.assertEqual(env['get_level_progress'](1381600),(1257,0,1100,0))
        policy.bind({'first_season_started':True,'status':'regular'})
        self.assertEqual(env['calculate_level'](1381600),100)
