"""Pure Firebase transaction callbacks. Never connect to the operating database here."""
import copy


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
    archive = {'season_id': expected_id, 'finished_at': stamp,
               'season_name': state.get('current_season_name'),
               'exp_data': copy.deepcopy(data),
               'mission_data': copy.deepcopy(root.get('mission_data') or {})}
    archives = root.setdefault('preseason_archives', {})
    if expected_id in archives:
        raise TransitionBlocked('Archive exists without its transition journal')
    archives[expected_id] = archive
    for record in data.values():
        if isinstance(record, dict):
            record.update(exp=0, level=1, voice_minutes=0, last_text_xp_at=0)
    root['exp_data'] = data
    root.pop('mission_data', None)
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
    root.setdefault('season_transitions', {})[token] = {'committed_at': stamp, 'target': target}
    return root
