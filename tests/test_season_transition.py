import copy
import unittest
from season_transition import transition_regular, TransitionBlocked, level_for, automatic_reward, close_preseason, closing_due

CAL = dict(season_id='2026_winter', status='regular', season_type='winter', season_label='겨울', next_season_id='2027_spring')

def fixture():
    return dict(season_state=dict(current_season_id='2026_fall', current_season_name='가을', first_season_started=True, settled=True, next_ready=True, next_season_id='2026_winter', next_season_name='겨울', status='preseason',preseason_cleared_for='2026_fall'), season_rewards={'2026_winter': {'title_name': '겨울 보상'}}, exp_data={'42': dict(exp=1381600, level=1257, last_activity=12345)}, mission_data={'42': {'count': 29}}, attendance={'42': {'total_days': 400}}, user_titles={'42': {'owned': {'old': 'title'}}})

def apply(root):
    return transition_regular(root, expected_id='2026_fall', calendar=CAL, stamp='2026-12-01T00:00:00+09:00')

class TransitionTests(unittest.TestCase):
    def test_regular_cap_and_unlimited_preseason(self):
        self.assertEqual(level_for(1381600), 100)
        self.assertEqual(level_for(1381600, True), 1257)
        self.assertEqual(level_for(0, True), 1)
    def test_automatic_only_multiplier(self):
        state=fixture()['season_state']
        for amount in (1,30,150,300,1200):
            self.assertEqual(automatic_reward(amount,state),amount*10)
        state['settled']=False
        self.assertEqual(automatic_reward(1200,state),0)
    def test_archives_resets_and_preserves_permanent_fields(self):
        old=fixture();old['exp_data']['42'].update(exp=0,level=1);old.pop('mission_data');snapshot=copy.deepcopy(old);new=apply(old)
        self.assertEqual(old,snapshot)
        self.assertNotIn('preseason_archives',new)
        self.assertEqual(new['exp_data']['42']['exp'],0)
        self.assertEqual(new['exp_data']['42']['last_activity'],12345)
        self.assertEqual(new['attendance'],old['attendance'])
        self.assertEqual(new['user_titles'],old['user_titles'])
        self.assertNotIn('mission_data',new)
        self.assertEqual(new['season_state']['current_season_name'],'겨울')
    def test_retry_does_not_reset_new_season_earnings(self):
        new=apply(fixture());new['exp_data']['42']['exp']=555
        self.assertEqual(apply(new),new)
    def test_unprepared_state_is_preserved(self):
        for key,value in [('settled',False),('next_ready',False),('next_season_id','wrong'),('next_season_name','')]:
            old=fixture();old['season_state'][key]=value;before=copy.deepcopy(old)
            with self.assertRaises(TransitionBlocked):apply(old)
            self.assertEqual(old,before)
    def test_missing_reward_and_unfinished_settlement_block(self):
        for change in ({'season_rewards':{}},{'season_settlement':{'season_id':'2026_fall','core_done':False}}):
            old=fixture();old.update(change)
            with self.assertRaises(TransitionBlocked):apply(old)
    def test_midnight_requires_completed_cutoff(self):
        old=fixture();old['season_state'].pop('preseason_cleared_for')
        with self.assertRaises(TransitionBlocked):apply(old)
    def test_23_cutoff_clears_without_archive_preserving_permanent_data(self):
        from datetime import datetime
        old=fixture();old['season_state'].pop('preseason_cleared_for')
        cal={'preseason_end':'2026-11-30'}
        now=datetime.fromisoformat('2026-11-30T23:00:00+09:00')
        new=close_preseason(old,expected_id='2026_fall',calendar=cal,now=now)
        self.assertEqual(new['exp_data']['42']['exp'],0)
        self.assertEqual(new['season_state']['status'],'locked')
        self.assertEqual(new['attendance'],old['attendance'])
        self.assertEqual(new['user_titles'],old['user_titles'])
        self.assertEqual(new['exp_data']['42']['last_activity'],12345)
        self.assertNotIn('mission_data',new);self.assertNotIn('preseason_archives',new)
        self.assertEqual(close_preseason(new,expected_id='2026_fall',calendar=cal,now=now),new)
    def test_unprepared_cutoff_locks_without_deleting(self):
        from datetime import datetime
        old=fixture();old['season_state'].pop('preseason_cleared_for');old['season_state']['next_ready']=False
        new=close_preseason(old,expected_id='2026_fall',calendar={'preseason_end':'2026-11-30'},now=datetime.fromisoformat('2026-11-30T23:00:00+09:00'))
        self.assertEqual(new['exp_data'],old['exp_data']);self.assertEqual(new['mission_data'],old['mission_data'])
        self.assertEqual(new['season_state']['status'],'locked')
    def test_cutoff_boundary_and_leap_day(self):
        from datetime import datetime
        for end in ('2026-11-30','2027-02-28','2028-02-29'):
            self.assertFalse(closing_due({'preseason_end':end},datetime.fromisoformat(end+'T22:59:59+09:00')))
            self.assertTrue(closing_due({'preseason_end':end},datetime.fromisoformat(end+'T23:00:00+09:00')))
