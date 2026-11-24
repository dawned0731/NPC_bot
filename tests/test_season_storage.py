"""Offline RTDB CAS model; no Firebase credentials or network are used."""
import copy
import unittest
from datetime import datetime
from unittest.mock import patch
from season_storage import commit_updates, commit_xp, apply_patch
from season_transition import close_preseason, transition_regular, epoch, TransitionBlocked, KST
from season_safety import settlement_payload
from test_season_transition import fixture, CAL


class MemoryDB:
    def __init__(self, root):
        self.root = copy.deepcopy(root)
        self.paths = []
        self.before_commit = None

    def reference(self, path=None):
        db = self
        path = (path or '').strip('/')
        class Ref:
            def child(self, child):
                return db.reference('/'.join(filter(None, (path, str(child)))))
            def get(self):
                val = db.root
                for key in path.split('/') if path else []:
                    val = (val or {}).get(key)
                return copy.deepcopy(val)
            def set(self, val):
                if path:
                    db.root = apply_patch(db.root, {path: val})
                else:
                    db.root = copy.deepcopy(val)
            def update(self, val):
                db.root = apply_patch(db.root, {'/'.join(filter(None, (path, k))): v for k, v in val.items()})
            def transaction(self, callback):
                db.paths.append(path)
                value = callback(self.get())
                if db.before_commit:
                    hook, db.before_commit = db.before_commit, None
                    hook()
                    # Firebase retries after an ETag conflict, with fresh data.
                    value = callback(self.get())
                self.set(value)
                return copy.deepcopy(value)
        return Ref()


class Clock:
    now_value = datetime(2026, 11, 30, 22, 59, 59, tzinfo=KST)
    @classmethod
    def now(cls, tz):
        return cls.now_value


PRE = dict(season_id='2026_fall', status='preseason', preseason_end='2026-11-30')


class StorageTests(unittest.TestCase):
    def setUp(self):
        root = fixture()
        root['season_state'].pop('preseason_cleared_for')
        self.db = MemoryDB(root)
        self.expected = copy.deepcopy(root['season_state'])
        self.calendar = PRE
        Clock.now_value = datetime(2026, 11, 30, 22, 59, 59, tzinfo=KST)
        self.clock = patch('season_policy.datetime', Clock)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def write(self, values, expected=None):
        return commit_updates(self.db, values, expected or self.expected, lambda: self.calendar)

    def close(self):
        Clock.now_value = datetime(2026, 11, 30, 23, 0, tzinfo=KST)
        self.db.root = close_preseason(self.db.root, expected_id='2026_fall', calendar=PRE, now=Clock.now_value)

    def test_legacy_mission_migrates_once_and_normal_chat_uses_small_transaction(self):
        self.write({'exp_data/42': {'exp': 100}})
        self.assertEqual(self.db.root['exp_data']['42']['_mission'], {'count': 29})
        self.assertNotIn('42', self.db.root.get('mission_data', {}))
        self.db.paths.clear()
        self.write({'exp_data/42': {'exp': 200}, 'mission_data/42': {'count': 30}})
        self.assertEqual(self.db.paths, ['exp_data/42'])
        self.assertEqual(self.db.root['exp_data']['42']['_mission']['count'], 30)

    def test_delayed_existing_writer_conflicts_with_23_reset(self):
        self.write({'exp_data/42': {'exp': 100}})
        self.db.before_commit = self.close
        with self.assertRaises(TransitionBlocked):
            self.write({'exp_data/42': {'exp': 99999}, 'mission_data/42': {'count': 30}})
        self.assertEqual(self.db.root['exp_data']['42']['exp'], 0)
        self.assertNotIn('_mission', self.db.root['exp_data']['42'])

    def test_delayed_absent_user_cannot_recreate_preseason_after_midnight(self):
        self.close()
        self.db.root = transition_regular(self.db.root, expected_id='2026_fall', calendar=CAL, stamp='now')
        self.calendar = CAL
        with self.assertRaises(TransitionBlocked):
            self.write({'exp_data/99': {'exp': 99999}})
        self.assertNotIn('99', self.db.root['exp_data'])

    def test_cutoff_blocks_writes_even_before_reset_worker_runs(self):
        self.write({'exp_data/42': {'exp': 100}})
        Clock.now_value = datetime(2026, 11, 30, 23, 0, tzinfo=KST)
        with self.assertRaises(TransitionBlocked):
            self.write({'exp_data/42/exp': 500})
        self.assertEqual(self.db.root['exp_data']['42']['exp'], 100)

    def test_midnight_new_reward_survives_repeated_open_and_old_writer(self):
        self.close()
        self.db.root = transition_regular(self.db.root, expected_id='2026_fall', calendar=CAL, stamp='now')
        self.calendar = CAL
        fresh = copy.deepcopy(self.db.root['season_state'])
        self.write({'exp_data/42': {'exp': 30}}, fresh)
        with self.assertRaises(TransitionBlocked):
            self.write({'exp_data/42': {'exp': 50000}})
        self.db.root = transition_regular(self.db.root, expected_id='2026_fall', calendar=CAL, stamp='again')
        self.assertEqual(self.db.root['exp_data']['42']['exp'], 30)
        self.assertEqual(self.db.root['attendance']['42']['total_days'], 400)

    def test_regular_settlement_fences_old_writers_and_keeps_official_record(self):
        state = dict(self.expected, settled=False)
        self.db.root['season_state'] = state
        self.db.root['exp_data']['42']['_xp_epoch'] = epoch(state)
        payload = settlement_payload(state, {'title_name': '가을'}, self.db.root['exp_data'], {}, 1, 1, lambda x: min(100, x // 1100 + 1))
        self.db.root = apply_patch(self.db.root, payload)
        with self.assertRaises(TransitionBlocked):
            self.write({'exp_data/42': {'exp': 50000}}, state)
        self.assertEqual(self.db.root['exp_data']['42']['exp'], 0)
        self.assertEqual(self.db.root['season_records']['2026_fall']['42']['final_exp'], 1381600)
        self.assertEqual(self.db.root['season_backup']['exp_data']['42']['exp'], 1381600)

    def test_mixed_attendance_commit_is_atomic_on_transition_conflict(self):
        self.db.before_commit = self.close
        with self.assertRaises(TransitionBlocked):
            self.write({'exp_data/42': {'exp': 12000}, 'attendance/42': {'total_days': 401}})
        self.assertEqual(self.db.root['attendance']['42']['total_days'], 400)

    def test_unprepared_and_wrong_target_never_erase_records(self):
        self.db.root['season_state']['next_season_id'] = '2027_spring'
        self.close()
        self.assertEqual(self.db.root['exp_data']['42']['exp'], 1381600)
        self.assertEqual(self.db.root['season_state']['status'], 'locked')

    def test_root_guard_rejects_leaf_xp_write_during_lock(self):
        self.close()
        with self.assertRaises(TransitionBlocked):
            commit_xp(self.db.root, {'exp_data/42/exp': 500}, self.db.root['season_state'], PRE)
