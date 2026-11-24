"""Boundary operations. Transactions only run when a cutoff/start is due."""
import asyncio
from datetime import datetime
from season_transition import close_preseason, transition_regular, closing_due, KST

_blocked_checks = {}

async def close_if_due(g):
    state=await asyncio.to_thread(lambda:g['db'].reference('season_state').get()) or {}
    sid=state.get('current_season_id')
    if not sid or not state.get('first_season_started'):return False
    year,kind=sid.split('_',1)
    dates=g['_season_dates_for_type'](int(year),kind)
    now=datetime.now(KST)
    if not closing_due(dates,now) or state.get('preseason_cleared_for')==sid:return False
    # If preparation is incomplete, reread only its small records. Never retry a
    # full database transaction every 30 seconds for an unchanged blocked state.
    reward = await asyncio.to_thread(lambda:g['db'].reference(f"season_rewards/{state.get('next_season_id')}").get()) or {}
    job = await asyncio.to_thread(lambda:g['db'].reference('season_settlement').get()) or {}
    signature = repr((state.get('settled'), state.get('next_ready'), state.get('next_season_id'),
                      state.get('next_season_name'), reward.get('title_name'), job.get('core_done')))
    key = (id(g['db']), sid)
    if state.get('preseason_clear_blocked') and _blocked_checks.get(key) == signature:
        return False
    result = await asyncio.to_thread(lambda:g['db'].reference().transaction(
        lambda root:close_preseason(root,expected_id=sid,calendar=dates,now=datetime.now(KST))))
    if (result.get('season_state') or {}).get('preseason_clear_blocked'):
        _blocked_checks[key] = signature
    else:
        _blocked_checks.pop(key, None)
    return True

async def open_regular(g,state,calendar):
    expected=state['current_season_id']
    return await asyncio.to_thread(lambda:g['db'].reference().transaction(
        lambda root:transition_regular(root,expected_id=expected,calendar=calendar,stamp=datetime.now(KST).isoformat())))
