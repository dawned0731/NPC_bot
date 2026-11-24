"""Conditional XP commits and idempotent settlement over the existing schema."""
import copy
from season_policy import multiplier
from season_transition import TransitionBlocked, epoch


def identity(state):
    return epoch(state)


def apply_patch(root, values):
    result=copy.deepcopy(root or {})
    for path,value in values.items():
        parts=path.strip('/').split('/');node=result
        for part in parts[:-1]:node=node.setdefault(part,{})
        if value is None:node.pop(parts[-1],None)
        else:node[parts[-1]]=copy.deepcopy(value)
    return result


def protected(values):
    return any((p.startswith('exp_data/') and not p.endswith('/last_activity')) or p.startswith('mission_data/') for p in values)


def commit_xp(root, values, expected, calendar):
    state=(root or {}).get('season_state') or {}
    if identity(state)!=identity(expected):
        raise TransitionBlocked('시즌이 변경되어 이전 경험치 저장을 취소했습니다. 다시 실행해주세요.')
    effective=dict(state,calendar=calendar)
    adjusted = dict(values)
    for uid in user_ids(values):
        old = copy.deepcopy(((root or {}).get('exp_data') or {}).get(uid) or {})
        if '_xp_epoch' not in old:
            old['_mission'] = copy.deepcopy(((root or {}).get('mission_data') or {}).get(uid) or {})
        record = updated_record(old, values, uid, effective)
        for path in list(adjusted):
            if path == f'exp_data/{uid}' or path.startswith(f'exp_data/{uid}/') or path == f'mission_data/{uid}':
                del adjusted[path]
        adjusted[f'exp_data/{uid}'] = record
        adjusted[f'mission_data/{uid}'] = None
    return apply_patch(root, adjusted)


def user_ids(values):
    return {p.split('/')[1] for p in values if p.startswith(('exp_data/', 'mission_data/'))}


def updated_record(old, values, uid, state):
    record = copy.deepcopy(values.get(f'exp_data/{uid}', old)) or {}
    if '_mission' not in record and old.get('_mission'):
        record['_mission'] = copy.deepcopy(old['_mission'])
    for path, value in values.items():
        if path.startswith(f'exp_data/{uid}/'):
            field = path.split('/', 2)[2]
            if '/' in field:
                raise ValueError('Unsupported XP field path')
            record[field] = copy.deepcopy(value)
    mission = f'mission_data/{uid}'
    if multiplier(state) == 0:
        if mission in values or any(record.get(field, 0) != old.get(field, 0)
                                   for field in ('exp', 'voice_minutes', 'last_text_xp_at')):
            raise TransitionBlocked('시즌 정비 중에는 경험치와 미션을 변경할 수 없습니다.')
    if mission in values:
        record['_mission'] = copy.deepcopy(values[mission])
    # Activity can be written independently; never replace a newer observation.
    record['last_activity'] = max(float(old.get('last_activity') or 0), float(record.get('last_activity') or 0))
    record['_xp_epoch'] = epoch(state)
    return record


class InitializeRecord(Exception):
    pass


def commit_updates(db, values, expected, calendar_provider):
    """Normal XP + missions share one small per-user transaction.

    Legacy/new records are initialized once with a root CAS. Rare attendance
    commits also use a root CAS to keep attendance and its award atomic. A reset
    stamps every user in the same transaction, fencing all delayed old writes.
    """
    ids = user_ids(values)
    if len(ids) == 1 and all(p.startswith(('exp_data/', 'mission_data/')) for p in values):
        uid = next(iter(ids))
        def update(old):
            if not isinstance(old, dict) or '_xp_epoch' not in old:
                raise InitializeRecord()
            if old['_xp_epoch'] != epoch(expected):
                raise TransitionBlocked('시즌이 변경되어 이전 저장을 취소했습니다. 다시 실행해주세요.')
            return updated_record(old, values, uid, dict(expected, calendar=calendar_provider()))
        try:
            return db.reference(f'exp_data/{uid}').transaction(update)
        except InitializeRecord:
            pass
    return db.reference().transaction(lambda root: commit_xp(root, values, expected, calendar_provider()))
