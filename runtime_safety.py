"""Small bounded-state helpers for daily activity and command failures."""
import copy
import logging
import uuid


def daily_mission(raw, today):
    record = copy.deepcopy(raw) if isinstance(raw, dict) and raw.get('date') == today else {'date': today}
    for key, field in (('text', 'count'), ('repeat_vc', 'minutes')):
        if not isinstance(record.get(key), dict):
            record[key] = {}
        try:
            record[key][field] = max(0, int(record[key].get(field, 0)))
        except (TypeError, ValueError):
            record[key][field] = 0
    record['text']['completed'] = bool(record['text'].get('completed', False))
    return record


def mark_operation(interaction, stage, outcome=None):
    interaction.extras['operation_stage'] = stage
    if outcome is not None:
        interaction.extras['operation_outcome'] = outcome


def failure_message(interaction, error):
    code = uuid.uuid4().hex[:8].upper()
    command = getattr(getattr(interaction, 'command', None), 'name', 'unknown')
    stage = interaction.extras.get('operation_stage', '처리')
    outcome = interaction.extras.get('operation_outcome')
    logging.error('[error:%s] command=%s stage=%s outcome=%s user=%s',
                  code, command, stage, outcome, getattr(interaction.user, 'id', None),
                  exc_info=(type(error), error, error.__traceback__))
    if outcome == 'saved':
        message = '기록은 저장됐지만 이후 안내 또는 화면 처리에 실패했어요.'
        if command == '출석':
            message += ' `/출석`으로 다시 확인해주세요. 출석 보상은 중복 지급되지 않습니다.'
        else:
            message += ' 같은 작업을 반복하기 전에 현재 기록을 확인해주세요.'
    elif outcome == 'saving':
        message = '저장 완료 여부를 확인하지 못했어요. 현재 기록을 확인한 뒤 다시 시도해주세요.'
    elif outcome == 'read_only':
        message = '기록 조회 또는 화면 표시에 실패했어요. 이 명령은 기록을 변경하지 않습니다.'
    elif outcome == 'not_saved':
        message = '저장 전에 처리가 중단됐어요. 잠시 후 다시 시도해주세요.'
    else:
        message = '명령어를 완료하지 못했어요. 관리자에게 아래 오류 번호를 알려주세요.'
    return f'❌ {message}\n오류 번호: `{code}` · 단계: {stage}'
