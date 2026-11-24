"""Pure Firebase transaction callbacks. Never connect to the operating database here."""
import copy
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))


class TransitionBlocked(ValueError):
    pass


def preseason_active(state):
    return bool(state.get('first_season_started') and state.get('settled')
                and state.get('status') == 'preseason')


def level_for(exp, preseason=False):
    level = max(0, int(exp)) // 1100 + 1
    return level if preseason else min(level, 100)


def automatic_reward(amount, state):
    if preseason_active(state):
        return int(amount) * 10
    if state.get('first_season_started') and state.get('status') == 'regular':
        return int(amount)
    return 0


def transition_regular(root, *, expected_id, calendar, stamp):
    """Invoke through reference('/').transaction; retries recompute from fresh data.

    A repeated committed transition is a no-op. Missing preparation protects all
    data. Dates must come from the existing KST calendar, never from UI input.
    """
    root = copy.deepcopy(root or {})
    state = root.get('season_state') or {}
    target = calendar['season_id']
    journal = root.get('season_transitions') or {}
    token = expected_id + '__' + target
    if token in journal:
        if state.get('current_season_id') != target:
            raise TransitionBlocked('Transition journal and active season disagree')
        return root
    if state.get('current_season_id') != expected_id or expected_id == target:
        raise TransitionBlocked('Active season changed; reload state')
    if calendar.get('status') != 'regular':
        raise TransitionBlocked('The next regular season has not started')
    if not state.get('first_season_started') or not state.get('settled'):
        raise TransitionBlocked('Regular season settlement is required')
    if not state.get('next_ready') or state.get('next_season_id') != target:
        raise TransitionBlocked('Next season is not ready')
    if not str(state.get('next_season_name') or '').strip():
        raise TransitionBlocked('Next season name is missing')
    reward = (root.get('season_rewards') or {}).get(target) or {}
    if not reward.get('title_name'):
        raise TransitionBlocked('Next season reward is missing')
    job = root.get('season_settlement') or {}
    if job and job.get('season_id') == expected_id and not job.get('core_done'):
        raise TransitionBlocked('Settlement rewards are not complete')
    data = root.get('exp_data') or {}
    if state.get('preseason_cleared_for') != expected_id:
        raise TransitionBlocked('The 23:00 preseason reset must complete first')
    state.update(current_season_id=target, current_season_name=state['next_season_name'],
                 current_season_type=calendar['season_type'],
                 current_season_label=calendar['season_label'], status='regular',
                 settled=False, next_ready=False, next_season_name='',
                 next_season_id=calendar['next_season_id'], started_at=stamp,
                 season_start_processed_at=stamp,
                 season_start_postprocess_pending_for=target,
                 season_start_notice_pending_for=target,
                 season_start_notice_last_error='')
    root['season_state'] = state
    root['season_transitions'] = {token: {'committed_at': stamp, 'target': target}}
    return root


def next_start(calendar):
    # Existing calendar supplies the final preseason date, including leap years.
    end = datetime.fromisoformat(str(calendar['preseason_end']))
    return (end.replace(tzinfo=KST, hour=0, minute=0, second=0, microsecond=0)
            + timedelta(days=1))


def closing_due(calendar, now):
    if now.tzinfo is None:
        raise ValueError('Timezone-aware time is required')
    return now >= next_start(calendar) - timedelta(hours=1)


def close_preseason(root, *, expected_id, calendar, now):
    """23:00 lock and clear; no user-level preseason archive is created.

    Missing preparation locks XP without erasing records. The runtime must call
    this through a Firebase transaction and independently reject stale writers.
    """
    root = copy.deepcopy(root or {})
    state = root.get('season_state') or {}
    if state.get('current_season_id') != expected_id:
        raise TransitionBlocked('Active season changed')
    if not closing_due(calendar, now):
        raise TransitionBlocked('Preseason cutoff has not arrived')
    if not state.get('first_season_started'):
        raise TransitionBlocked('The first season has not started')
    state['status'] = 'locked'
    root['season_state'] = state
    if state.get('preseason_cleared_for') == expected_id:
        return root
    target = state.get('next_season_id')
    reward = (root.get('season_rewards') or {}).get(target) or {}
    job = root.get('season_settlement') or {}
    ready = (state.get('settled') and state.get('next_ready')
             and str(state.get('next_season_name') or '').strip()
             and reward.get('title_name')
             and (not job or job.get('season_id') != expected_id or job.get('core_done')))
    if not ready:
        state['preseason_clear_blocked'] = '정산·다음 시즌 이름·보상·후처리 확인 필요'
        return root
    for record in (root.get('exp_data') or {}).values():
        if isinstance(record, dict):
            record.update(exp=0, level=1, voice_minutes=0, last_text_xp_at=0)
    root.pop('mission_data', None)
    state.update(preseason_cleared_for=expected_id,
                 preseason_cleared_at=now.astimezone(KST).isoformat(),
                 preseason_clear_blocked='',
                 xp_generation=int(state.get('xp_generation', 0)) + 1)
    return root
