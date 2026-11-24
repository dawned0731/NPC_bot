"""Conservative inactivity eligibility; missing evidence never authorizes removal."""
import math


def inactive_candidate(last_activity, threshold, *, recent_activity=0, joined_at=0, in_voice=False):
    if in_voice:
        return False
    try:
        stamps = [float(last_activity), float(recent_activity), float(joined_at)]
        cutoff = float(threshold)
    except (TypeError, ValueError, OverflowError):
        return False
    if not all(math.isfinite(value) for value in stamps + [cutoff]):
        return False
    return stamps[0] > 0 and max(stamps) < cutoff
