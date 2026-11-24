"""Task-local season policy. Manual changes never use automatic multipliers."""
from contextvars import ContextVar
from datetime import datetime, timedelta
from season_transition import KST, next_start

STATE=ContextVar('season_policy',default={})

def bind(state):
    STATE.set(dict(state));return state

def is_preseason(state=None):
    s=STATE.get() if state is None else state
    return bool(s.get('first_season_started') and s.get('settled') and s.get('status')=='preseason')

def multiplier(state=None, now=None):
    s=STATE.get() if state is None else state
    if not s.get('first_season_started') or s.get('status')=='locked':return 0
    cal=s.get('calendar') or {}
    if cal.get('season_id') and s.get('current_season_id')!=cal['season_id']:return 0
    if s.get('status')=='regular':return 1
    if is_preseason(s):
        if cal.get('preseason_end') and (now or datetime.now(KST))>=next_start(cal)-timedelta(hours=1):return 0
        return 10
    return 0

def reward(amount):return int(amount)*multiplier()
def cap():return float('inf') if is_preseason() else 100

def recovery_season(state):
    # Existing regular-season recovery offers remain compatible. Settlement
    # invalidates them even though the calendar season ID is unchanged.
    sid = state.get('current_season_id')
    return f'{sid}:preseason' if state.get('settled') else sid
