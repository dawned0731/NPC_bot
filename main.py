import discord
from discord.ext import commands, tasks
from discord import app_commands


import os
import sys
import json
import time
import random
import re
import asyncio
import logging
import copy
import functools
import pytz
import aiohttp

from threading import Thread
from datetime import time as dtime
from datetime import datetime, date, timedelta
from collections import defaultdict
from typing import Optional

from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, db

from io import BytesIO
from PIL import Image, ImageDraw, ImageFont

import logging
logging.basicConfig(level=logging.INFO)


# =========================
# Rank card rendering (Pillow)
# =========================

_ASSET_DIR = os.path.join(os.path.dirname(__file__), "assets")
_BG_PATH = os.path.join(_ASSET_DIR, "rank_bg.png")
_FONT_PATH = os.path.join(_ASSET_DIR, "fonts", "Donoun Medium.ttf")  # 네가 넣은 폰트명에 맞춤

_BG_TEMPLATE = None  # type: Optional[Image.Image]
_FONT_CACHE = {}     # size -> ImageFont.FreeTypeFont

_QUEST_BG_PATH = os.path.join(_ASSET_DIR, "quest_banner_bg.png")
_QUEST_BG_TEMPLATE = None  # type: Optional[Image.Image]

def _get_quest_bg_template() -> Image.Image:
    global _QUEST_BG_TEMPLATE
    if _QUEST_BG_TEMPLATE is None:
        try:
            bg = Image.open(_QUEST_BG_PATH).convert("RGBA")
        except Exception:
            # 파일 없으면 rank_bg로 폴백
            bg = _get_bg_template()
        _QUEST_BG_TEMPLATE = bg
    return _QUEST_BG_TEMPLATE


def _get_bg_template() -> Image.Image:
    global _BG_TEMPLATE
    if _BG_TEMPLATE is None:
        bg = Image.open(_BG_PATH).convert("RGBA")
        _BG_TEMPLATE = bg
    return _BG_TEMPLATE


def _get_font(size: int) -> ImageFont.FreeTypeFont:
    font = _FONT_CACHE.get(size)
    if font is None:
        font = ImageFont.truetype(_FONT_PATH, size)
        _FONT_CACHE[size] = font
    return font


def _format_int(n: int) -> str:
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


def _clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def _ellipsize(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    if not text:
        return ""
    if draw.textlength(text, font=font) <= max_width:
        return text

    ell = "…"
    lo, hi = 0, len(text)
    # 이진 탐색으로 최대 길이 찾기
    while lo < hi:
        mid = (lo + hi) // 2
        cand = text[:mid] + ell
        if draw.textlength(cand, font=font) <= max_width:
            lo = mid + 1
        else:
            hi = mid
    cut = max(0, lo - 1)
    return text[:cut] + ell


def _circle_crop(im: Image.Image, size: int) -> Image.Image:
    # 정사각으로 맞춘 뒤 원형 마스크
    im = im.convert("RGBA")
    w, h = im.size
    s = min(w, h)
    left = (w - s) // 2
    top = (h - s) // 2
    im = im.crop((left, top, left + s, top + s))

    resample = getattr(Image, "Resampling", None)
    if resample is not None:
        im = im.resize((size, size), resample=resample.LANCZOS)
    else:
        im = im.resize((size, size), resample=Image.LANCZOS)

    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    md.ellipse((0, 0, size - 1, size - 1), fill=255)

    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    return out


def render_rank_card(
    *,
    display_name: str,
    level: int,
    total_xp: int,
    cur_xp: int,
    need_xp: int,
    pct: float,
    avatar_bytes: Optional[bytes] = None,
) -> BytesIO:
    """
    디스코드/DB와 무관한 순수 렌더러.
    - 입력: 가공된 수치 + 아바타 이미지 bytes
    - 출력: PNG(BytesIO)
    """
    bg = _get_bg_template()
    img = bg.copy()
    draw = ImageDraw.Draw(img)

    # ===== 레이아웃 (600x240 기준) =====
    AVATAR_SIZE = 96
    AVATAR_X, AVATAR_Y = 36, 72

    TEXT_X = 155
    NAME_Y = 60
    STAT_Y = 102
    XP_Y = 130

    BAR_X, BAR_Y = 150, 180
    BAR_W, BAR_H = 300, 22
    BAR_RADIUS = 11  # BAR_H//2

    # ===== 아바타 =====
    if avatar_bytes:
        try:
            av = Image.open(BytesIO(avatar_bytes))
            av = _circle_crop(av, AVATAR_SIZE)
            img.paste(av, (AVATAR_X, AVATAR_Y), av)
        except Exception:
            # 아바타 실패 시 회색 원으로 대체
            fallback = Image.new("RGBA", (AVATAR_SIZE, AVATAR_SIZE), (0, 0, 0, 0))
            fd = ImageDraw.Draw(fallback)
            fd.ellipse((0, 0, AVATAR_SIZE - 1, AVATAR_SIZE - 1), fill=(120, 120, 120, 255))
            img.paste(fallback, (AVATAR_X, AVATAR_Y), fallback)

    # ===== 폰트 =====
    font_name = _get_font(28)
    font_stat = _get_font(22)
    font_small = _get_font(18)

    # ===== 닉네임 =====
    name_max_w = 600 - TEXT_X - 30
    safe_name = _ellipsize(draw, display_name, font_name, name_max_w)
    draw.text((TEXT_X, NAME_Y), safe_name, font=font_name, fill=(0x05, 0x44, 0x6B, 255))

    # ===== 레벨 / XP =====
    draw.text((TEXT_X, STAT_Y), f"Lv. {int(level)}", font=font_stat, fill=(0xFF, 0xFF, 0xFF, 255))
    draw.text((TEXT_X, XP_Y), f"XP  {_format_int(total_xp)}", font=font_stat, fill=(0x9E, 0x9E, 0x9E, 255))

    # ===== 진행도 바 =====
    pct = _clamp01(float(pct))
    # 바 배경
    draw.rounded_rectangle(
        (BAR_X, BAR_Y, BAR_X + BAR_W, BAR_Y + BAR_H),
        radius=BAR_RADIUS,
        fill=(0xED, 0xF8, 0xFC, 255),
    )
    # 바 채움
    fill_w = int(BAR_W * pct)
    if fill_w > 0:
        draw.rounded_rectangle(
            (BAR_X, BAR_Y, BAR_X + fill_w, BAR_Y + BAR_H),
            radius=BAR_RADIUS,
            fill=(0x05, 0x44, 0x6B, 255),
        )

    # 진행도 텍스트
    # 예: "123 / 456 (27%)"
    pct_int = int(round(pct * 100))
    prog_text = f"{_format_int(cur_xp)} / {_format_int(need_xp)} ({pct_int}%)"
    draw.text((BAR_X, BAR_Y - 22), prog_text, font=font_small, fill=(60, 60, 60, 255))

    # ===== PNG 출력 =====
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf

def render_daily_quest_banner(
    *,
    display_name: str,
    pct_int: int,
    height: int = 70,
    reward_pct: int = 1,
) -> BytesIO:
    """
    채팅 한 줄 체감용 초슬림 배너 (아이콘 없음, 단일 행)
    레이아웃:
    [일일 퀘스트 성공!  경험치 1% 지급   |   서버 닉네임 님의   |   현재 경험치 37%]
    """
    bg = _get_quest_bg_template()
    w = bg.size[0]
    h = int(height)

    base = bg.crop((0, 0, w, min(h, bg.size[1]))).copy()
    if base.size[1] != h:
        img = Image.new("RGBA", (w, h), (245, 245, 245, 255))
        img.paste(base, (0, 0))
    else:
        img = base

    draw = ImageDraw.Draw(img)
    font = _get_font(16)

    x = 18
    max_w = w - (x * 2)

    title = "일일 퀘스트 성공!"
    reward = f"경험치 {reward_pct}% 지급"
    nick = f"{display_name} 님의"
    prog = f"현재 경험치 {max(0, min(100, int(pct_int)))}%"

    sep = "   |   "
    line = f"{title}  {reward}{sep}{nick}{sep}{prog}"

    safe_line = _ellipsize(draw, line, font, max_w)
    
    bbox = draw.textbbox((0, 0), safe_line, font=font)
    text_h = bbox[3] - bbox[1]
    y = (h - text_h) // 2 - bbox[1]
    
    draw.text((x, y), safe_line, font=font, fill=(0, 0, 0, 255))

    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


# =======================================================================

KST = pytz.timezone("Asia/Seoul")  # ← 추가

# =========================
# Attendance (출석) 메시지 설정
# =========================
ATTEND_MILESTONE_STREAKS = {3, 7, 14, 30, 50, 100}

ATTEND_MSG_ALREADY = [
    "📺 (속보) {mention} 오늘 출석, 이미 처리 완료",
    "🧾 (기록 확인) {mention} 오늘 도장: 찍힘",
    "🕰️ (리마인드) {mention} 오늘 건은 수령 완료 상태",
    "🚫 (제한 안내) {mention} 1일 1회 규정 적용 중",
    "🔒 (봉인됨) {mention} 오늘 출석 슬롯: 닫힘",
    "📌 (체크 완료) {mention} 오늘 항목: 완료 표시",
    "🎛️ (시스템) {mention} 중복 요청 감지: 처리 생략",
    "📷 (현장) {mention} 이미 찍힌 도장 화면 확보",
    "🗂️ (로그) {mention} 오늘자 출석 로그 존재",
    "📦 (수령 내역) {mention} 오늘 보상: 수령됨",
    "🧯 (과열 방지) {mention} 연타 방지 모드 작동",
    "🧊 (쿨다운) {mention} 오늘은 여기까지",
    "📎 (첨부) {mention} 오늘 출석 확인서 발급 완료",
    "🔔 (알림) {mention} 오늘 출석은 이미 끝난 이야기",
    "🧷 (고정) {mention} 오늘 체크는 더 이상 갱신되지 않음",
]

ATTEND_MSG_SUCCESS = [
    "🎉 (자막) {mention} 오늘도 무사 통과",
    "🥁 (효과음) {mention} 도장 “딱”",
    "🏁 (완료) {mention} 오늘 구간 클리어",
    "📌 (확정) {mention} 출석 처리 완료",
    "📈 (상승) {mention} 연속 기록 유지 중",
    "🔥 (유지력) {mention} 루틴이 꺼지지 않는다",
    "🧭 (정상 항로) {mention} 오늘도 경로 이탈 없음",
    "🧱 (적립) {mention} 한 칸 추가 적립",
    "🎬 (엔딩) {mention} 오늘의 출석, 깔끔한 마무리",
    "📣 (공지) {mention} 출석 완료 처리되었습니다",
    "🗃️ (저장) {mention} 오늘 기록 저장 완료",
    "🧲 (흡착) {mention} 습관이 또 붙었다",
    "🎯 (명중) {mention} 출석 타이밍 적중",
    "🛎️ (완료음) {mention} 처리 완료 신호",
    "📡 (송출) {mention} 출석 성공 신호 수신",
    "🧨 (기세) {mention} 연속 흐름 계속 간다",
    "🧽 (깔끔) {mention} 오늘도 정리정돈 완료",
    "🪪 (인증) {mention} 오늘 출석 인증 통과",
    "🔧 (정상 작동) {mention} 출석 모듈 이상 없음",
    "🎊 (장면 전환) {mention} 다음 출석은 내일로 넘어갑니다",
]

ATTEND_MSG_FIRST = [
    "🆕 (감지) {mention} 새로운 출석 기록 생성",
    "🎬 (오프닝) {mention} 1일차 장면 시작",
    "📍 (첫 체크) {mention} 오늘이 첫 도장입니다",
    "🗂️ (신규 등록) {mention} 출석 카드 발급 완료",
    "🚦 (출발) {mention} 이제부터 누적이 쌓입니다",
    "🧩 (첫 조각) {mention} 퍼즐 1칸 채움",
    "🏗️ (기초 공사) {mention} 기록의 바닥을 다졌습니다",
    "🎟️ (입장) {mention} 출석 루틴에 입장했습니다",
    "🧾 (초안 작성) {mention} 오늘부터 로그가 남습니다",
    "🔰 (스타트) {mention} 시작 마크 확인",
]

ATTEND_MSG_RESET = [
    "🧊 (알림) {mention} 연속 기록이 1일로 재설정됩니다",
    "📉 (변동) {mention} 연속 흐름이 끊겼습니다",
    "🪓 (컷) {mention} 콤보 종료, 오늘부터 다시 시작",
    "🧯 (진화) {mention} 불은 꺼졌고, 다시 붙이면 됩니다",
    "🕳️ (이탈) {mention} 연속 구간에서 벗어났습니다",
    "🧽 (리셋) {mention} 연속 수치 초기화 처리",
    "🔄 (재정렬) {mention} 연속 기록 1일차로 정렬",
    "📎 (참고) {mention} 누적은 유지, 연속만 리셋",
    "🪫 (방전) {mention} 연속 배터리 0%, 오늘부터 충전",
    "⛔ (중단) {mention} 연속 기록 중단 확인",
    "🧱 (재시작) {mention} 다시 한 칸부터 쌓습니다",
    "📌 (확정) {mention} 연속 끊김 상태로 출석 처리",
]



ATTEND_MSG_MILESTONE = [
    "🏅 (기록) {mention} 연속 {streak}일 달성",
    "🎖️ (인증) {mention} 연속 {streak}일 구간 진입",
    "📣 (특보) {mention} 연속 {streak}일, 축하 출력 송출",
    "🧱 (누적) {mention} 연속 {streak}일, 기반이 단단해졌습니다",
    "🎇 (이벤트) {mention} 연속 {streak}일 체크포인트 통과",
    "🏁 (분기점) {mention} 연속 {streak}일 구간 완료",
    "📌 (배지) {mention} 연속 {streak}일 표식 부착",
    "🥇 (랭크업) {mention} 연속 {streak}일, 컨디션 최상",
    "📈 (상향) {mention} 연속 {streak}일, 상승세 유지",
    "🎬 (하이라이트) {mention} 연속 {streak}일 장면 저장",
]

def _safe_int(v, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default

def normalize_attendance_record(ud: dict | None) -> dict:
    if not isinstance(ud, dict):
        ud = {}
    ud.setdefault("last_date", "")
    ud["total_days"] = max(0, _safe_int(ud.get("total_days", 0), 0))
    ud["streak"] = max(0, _safe_int(ud.get("streak", 0), 0))
    if not isinstance(ud.get("weekly"), dict):
        ud["weekly"] = {}
    if not isinstance(ud.get("monthly"), dict):
        ud["monthly"] = {}
    return ud

def _until_next_attendance(now_kst: datetime) -> tuple[int, int]:
    until = (now_kst.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)) - now_kst
    h, m = divmod(int(until.total_seconds() // 60), 60)
    return h, m

def _build_attendance_stats_line(total_days: int, streak: int, gain: int | None = None) -> str:
    if gain is None:
        return f"누적 {total_days}일 · 연속 {streak}일"
    return f"누적 {total_days}일 · 연속 {streak}일 · +{gain} XP"

SAFEGUARD_DISABLE_EXTERNAL_IO = os.getenv("SAFEGUARD_DISABLE_EXTERNAL_IO", "1") == "1"
SAFEGUARD_MIN_INTERVAL_GLOBAL = float(os.getenv("SAFEGUARD_MIN_INTERVAL_GLOBAL", "1.0"))  # 전역 처리 간 최소 간격(초)
SAFEGUARD_MIN_INTERVAL_PER_CHANNEL = float(os.getenv("SAFEGUARD_MIN_INTERVAL_PER_CHANNEL", "2.0"))  # 채널별
SAFEGUARD_MIN_INTERVAL_PER_USER = float(os.getenv("SAFEGUARD_MIN_INTERVAL_PER_USER", "2.0"))  # 유저별

# 외부 HTTP 동시성 제한 (필요 시 사용)
SAFEGUARD_EXTERNAL_IO_SEMAPHORE = asyncio.Semaphore(int(os.getenv("SAFEGUARD_EXTERNAL_IO_MAX_CONCURRENCY", "3")))

_last_user_ts = defaultdict(float)     # user_id -> ts

load_dotenv()
firebase_key_json = os.getenv("FIREBASE_KEY_JSON")

# === fail-fast: Firebase 키 없으면 즉시 종료 ===
if not firebase_key_json:
    raise RuntimeError("FIREBASE_KEY_JSON 환경변수가 설정되어 있지 않습니다.")

# 1차 파싱: 환경변수 값이 (a) 원본 JSON 이거나 (b) JSON 문자열(tojson 결과)일 수 있음
try:
    v = json.loads(firebase_key_json)
except json.JSONDecodeError:
    raise RuntimeError("FIREBASE_KEY_JSON 값이 올바른 JSON 형식이 아닙니다.")

# 2차 처리: tojson로 넣은 경우(str)면 한 번 더 파싱해서 dict로 만든다
if isinstance(v, str):
    try:
        firebase_key_dict = json.loads(v)  # 최종 dict
    except json.JSONDecodeError:
        raise RuntimeError("FIREBASE_KEY_JSON 내부 문자열이 올바른 JSON이 아닙니다.")
elif isinstance(v, dict):
    firebase_key_dict = v
else:
    raise RuntimeError("FIREBASE_KEY_JSON는 JSON 객체여야 합니다.")

# Firebase Admin 초기화 (중복 방지)
# 이미 초기화되어 있으면 재사용, 없으면 한 번만 초기화
FIREBASE_DB_URL = os.getenv("FIREBASE_DB_URL", "https://npc-bot-add0a-default-rtdb.firebaseio.com")
try:
    firebase_admin.get_app()  # 기본 앱 존재 여부 확인
except ValueError:
    cred = credentials.Certificate(firebase_key_dict)
    firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL})


# ---- 설정 영역 ----
EXEMPT_ROLE_IDS = [
    1391063915655331942,  # 예외 역할 : 관리자
    1410180795938771066,  # 예외 역할 : 추방 방지
]
# Discord 봇 토큰 및 슬래시 커맨드 동기화를 위한 길드 ID
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0"))
# === fail-fast: 토큰 없으면 즉시 종료 ===
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN 환경변수가 설정되어 있지 않습니다.")

# ---- 역할별 인원수를 음성 채널 이름으로 실시간 반영 ----

SEASON_ROLE_CHANNEL_MAP = {
    "봄": (1386685631551246426, 1401854813356036196),
    "여름": (1386685631551246425, 1401854844628893718),
    "가을": (1386685631551246424, 1401854913117687889),
    "겨울": (1386685631551246423, 1401854945547915316),
}

async def update_season_voice_channels(_bot: commands.Bot):
    """계절 역할 인원수를 설정된 음성 채널 이름에 반영합니다."""
    for guild in _bot.guilds:
        try:
            cfg = await aget_guild_config(guild.id)
        except Exception as e:
            logging.warning(f"[season-voice] config load failed guild={guild.id}: {e!r}")
            cfg = {}

        if not _cfg_get(cfg, "features", "season_voice_enabled", default=True):
            continue

        configured_map = cfg.get("season_map", {}) if isinstance(cfg, dict) else {}
        for season, fallback in SEASON_ROLE_CHANNEL_MAP.items():
            role_id, channel_id = fallback
            configured = configured_map.get(season) if isinstance(configured_map, dict) else None
            if isinstance(configured, dict):
                try:
                    role_id = int(configured.get("role_id") or role_id)
                    channel_id = int(configured.get("channel_id") or channel_id)
                except (TypeError, ValueError):
                    role_id, channel_id = fallback

            role = guild.get_role(role_id)
            channel = guild.get_channel(channel_id)
            if not role or not channel:
                continue

            count = sum(1 for member in role.members if not member.bot)
            new_name = f"[{season}], 그 사이의 {count}명"
            if channel.name == new_name:
                continue

            try:
                await channel.edit(name=new_name, reason="계절 역할 인원수 자동 반영")
            except Exception as e:
                logging.warning(f"[season-voice] channel rename failed season={season}: {e!r}")


# 로컬 데이터 디렉토리 생성
os.makedirs("data", exist_ok=True)

# 파일 및 채널, 쿨다운 등 상수 정의
EXP_PATH = "data/exp.json"
MISSION_PATH = "data/mission.json"
LOG_CHANNEL_ID = 1386685633136820248
INACTIVE_LOG_CHANNEL_ID = 1386685633136820247
DISCONNECT_LOG_CHANNEL_ID = 1506202471058509904
INACTIVE_KICK_DAYS = 30  # 원하는 기준일로
INACTIVE_AUTO_KICK_ENABLED = os.getenv("INACTIVE_AUTO_KICK_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
LEVELUP_ANNOUNCE_CHANNEL = 1386685634462093332
TARGET_TEXT_CHANNEL_ID = 1386685633413775416
THREAD_ROLE_CHANNEL_ID = 1386685633413775416
THREAD_ROLE_ID = 1386685631580733541
COOLDOWN_SECONDS = 5
VOICE_COOLDOWN = 60
VOICE_MIN_XP = 10
VOICE_MAX_XP = 50
AFK_CHANNEL_IDS = [1386685633820495994]
MISSION_EXP_REWARD = 100
MISSION_REQUIRED_MESSAGES = 30
REPEAT_VC_EXP_REWARD = 100
REPEAT_VC_REQUIRED_MINUTES = 15
REPEAT_VC_MIN_PEOPLE = 5
SPECIAL_VC_CATEGORY_IDS = [1386685633820495991]
ATTENDANCE_DB_KEY = "attendance_data"

# =========================
# Season Pass 설정
# =========================
CURRENT_SEASON_NAME = "첫 번째 계절"  # 최초 season_state가 없을 때 사용할 기본 시즌명
SEASON_MAX_LEVEL = 100

# 기준:
# 음성 경험치 평균 30XP/분
# 하루 2시간 × 30일 = 3,600분
# 3,600분 × 30XP = 108,000XP
# Lv.1 → Lv.100 = 99구간
# 1구간 약 1,100XP로 설정
SEASON_XP_PER_LEVEL = 1100
SEASON_TOTAL_XP_TO_MAX = SEASON_XP_PER_LEVEL * (SEASON_MAX_LEVEL - 1)

# 출석 고정 경험치
ATTENDANCE_EXP_REWARD = 200

# 시즌 공지 채널
SEASON_NOTICE_CHANNEL_ID = 1506509476297969766

SEASON_STATUS_REGULAR = "regular"
SEASON_STATUS_PRESEASON = "preseason"
SEASON_STATUS_LOCKED = "locked"

SEASON_TYPE_LABELS = {
    "spring": "봄",
    "summer": "여름",
    "fall": "가을",
    "winter": "겨울",
}

SEASON_TYPE_ORDER = ["spring", "summer", "fall", "winter"]

DEFAULT_SEASON_NAMES = {
    "spring": "봄의 계절",
    "summer": "여름의 계절",
    "fall": "가을의 계절",
    "winter": "겨울의 계절",
}

# KST 타임존 객체
KST = pytz.timezone("Asia/Seoul")


# ---- Firebase 핸들링 함수 ----


# ---- Firebase 비동기 래퍼 (블로킹 방지) ----
import asyncio

async def aload_exp_data():
    return await asyncio.to_thread(load_exp_data)

async def asave_exp_data(data):
    return await asyncio.to_thread(save_exp_data, data)

async def asave_user_exp(user_id, user_data):
    return await asyncio.to_thread(save_user_exp, user_id, user_data)

async def aload_mission_data():
    return await asyncio.to_thread(load_mission_data)

async def asave_mission_data(data):
    return await asyncio.to_thread(save_mission_data, data)

async def asave_user_mission(user_id, user_mission):
    return await asyncio.to_thread(save_user_mission, user_id, user_mission)

async def aget_attendance_data():
    return await asyncio.to_thread(get_attendance_data)

async def aset_attendance_data(user_id, data):
    return await asyncio.to_thread(set_attendance_data, user_id, data)

async def aget_attendance_user(uid: str) -> dict:
    return await asyncio.to_thread(get_attendance_user, uid)

async def aset_attendance_user(uid: str, data: dict):
    return await asyncio.to_thread(set_attendance_user, uid, data)

async def abulk_update_attendance(updates: dict):
    return await asyncio.to_thread(bulk_update_attendance, updates)


# 같은 유저에게 여러 보상 루프가 동시에 접근할 때 발생하는 덮어쓰기를 막습니다.
_USER_STATE_LOCKS: dict[str, asyncio.Lock] = {}
_LEVEL100_AWARD_CACHE: set[tuple[str, str]] = set()
_SEASON_OPERATION_LOCKS: dict[int, asyncio.Lock] = {}


def get_season_operation_lock(guild_id: int) -> asyncio.Lock:
    key = int(guild_id)
    lock = _SEASON_OPERATION_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _SEASON_OPERATION_LOCKS[key] = lock
    return lock


def season_operation_serialized():
    """파괴적 시즌 관리 명령어가 같은 서버에서 동시에 실행되지 않게 합니다."""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(interaction: discord.Interaction, *args, **kwargs):
            guild = interaction.guild
            if guild is None:
                return await func(interaction, *args, **kwargs)

            lock = get_season_operation_lock(guild.id)
            if lock.locked():
                message = "❌ 다른 시즌 관리 작업이 진행 중입니다. 완료된 뒤 다시 시도해주세요."
                try:
                    if interaction.response.is_done():
                        return await interaction.followup.send(message, ephemeral=True)
                    return await interaction.response.send_message(message, ephemeral=True)
                except Exception:
                    return None

            async with lock:
                return await func(interaction, *args, **kwargs)
        return wrapper
    return decorator


def guard_background_task(name: str):
    """한 번의 외부 서비스 오류로 반복 태스크 전체가 종료되지 않게 합니다."""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("[background-task:%s] iteration failed; next iteration will continue", name)
                return None
        return wrapper
    return decorator


def get_user_state_lock(uid: str | int) -> asyncio.Lock:
    key = str(uid)
    lock = _USER_STATE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _USER_STATE_LOCKS[key] = lock
    return lock


async def aupdate_user_exp_fields(uid: str, fields: dict):
    """EXP 전체 레코드를 덮어쓰지 않고 필요한 필드만 부분 갱신합니다."""
    if not isinstance(fields, dict) or not fields:
        return
    await asyncio.to_thread(lambda: db.reference("exp_data").child(str(uid)).update(fields))

async def aget_user_exp(uid: str):
    def _get():
        raw = db.reference("exp_data").child(uid).get()

        # 1) 레코드 자체가 없으면 기본값
        if not isinstance(raw, dict):
            return {"exp": 0, "level": 1, "voice_minutes": 0}

        # 2) exp 보정 (없거나 타입 이상하면 0)
        exp = raw.get("exp", 0)
        try:
            exp = int(exp)
        except Exception:
            exp = 0
        if exp < 0:
            exp = 0

        # 3) 나머지 키도 기본값 보장
        vm = raw.get("voice_minutes", 0)
        try:
            vm = int(vm)
        except Exception:
            vm = 0
        if vm < 0:
            vm = 0

        lvl = raw.get("level", 1)
        try:
            lvl = int(lvl)
        except Exception:
            lvl = 1

        # 4) 반환값은 “항상 완전한 스키마”
        raw["exp"] = exp
        raw["voice_minutes"] = vm
        raw["level"] = lvl
        return raw

    return await asyncio.to_thread(_get)


async def aget_user_mission(uid: str, today: str):
    def _get():
        base = {"date": today, "text": {"count": 0, "completed": False}, "repeat_vc": {"minutes": 0}}
        val = db.reference("mission_data").child(uid).get()
        return val or base
    return await asyncio.to_thread(_get)

# =========================
# Guild (server) config IO
# =========================

_GUILD_CONFIG_CACHE = {}          # guild_id(str) -> dict
_GUILD_CONFIG_CACHE_TS = {}       # guild_id(str) -> float
_GUILD_CONFIG_TTL = 30.0          # seconds

def _guild_cfg_ref(guild_id: int):
    return db.reference("guild_config").child(str(guild_id))

def _default_guild_config() -> dict:
    # 최소 스키마. 없으면 dict 합치기 쉬움.
    return {
        "channels": {},
        "roles": {},
        "voice": {
            "afk_channel_ids": [],
            "special_vc_category_ids": [],
        },
        "season_map": {},  # "봄": {"role_id":..., "channel_id":...}
        "features": {
            "season_voice_enabled": True,
        }
    }
    
_COUNT_SUFFIX_RE = re.compile(r"(\d+)명$")

def _replace_count_suffix(name: str, count: int):
    m = _COUNT_SUFFIX_RE.search(name or "")
    if not m:
        return None
    return name[:m.start(1)] + f"{count}명"


async def aget_guild_config(guild_id: int) -> dict:
    now = time.time()
    gid = str(guild_id)
    ts = _GUILD_CONFIG_CACHE_TS.get(gid, 0.0)
    if gid in _GUILD_CONFIG_CACHE and (now - ts) < _GUILD_CONFIG_TTL:
        return _GUILD_CONFIG_CACHE[gid]

    def _get():
        val = _guild_cfg_ref(guild_id).get() or {}
        base = _default_guild_config()
        # 얕은 병합(필요 키 보장)
        for k, v in base.items():
            if k not in val or not isinstance(val.get(k), type(v)):
                val[k] = v
        return val

    cfg = await asyncio.to_thread(_get)
    _GUILD_CONFIG_CACHE[gid] = cfg
    _GUILD_CONFIG_CACHE_TS[gid] = now
    return cfg

async def aset_guild_config_field(guild_id: int, path: str, value):
    # path 예: "channels/log_channel_id"
    def _set():
        ref = _guild_cfg_ref(guild_id)
        parts = [p for p in path.split("/") if p]
        node = ref
        for p in parts[:-1]:
            node = node.child(p)
        node.child(parts[-1]).set(value)

    await asyncio.to_thread(_set)
    # 캐시 무효화
    gid = str(guild_id)
    _GUILD_CONFIG_CACHE.pop(gid, None)
    _GUILD_CONFIG_CACHE_TS.pop(gid, None)

def _cfg_get(cfg: dict, *keys, default=None):
    cur = cfg
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur

async def get_channel_from_cfg(guild: discord.Guild, cfg: dict, key: str, fallback_id: int | None):
    """메시지 전송이 가능한 설정 채널만 반환합니다."""
    ch_id = _cfg_get(cfg, "channels", key, default=None)
    if isinstance(ch_id, str) and ch_id.isdigit():
        ch_id = int(ch_id)
    if not isinstance(ch_id, int):
        ch_id = fallback_id
    if not ch_id:
        return None
    channel = guild.get_channel(int(ch_id))
    return channel if channel and hasattr(channel, "send") else None

async def get_role_from_cfg(guild: discord.Guild, cfg: dict, key: str, fallback_id: int | None):
    role_id = _cfg_get(cfg, "roles", key, default=None)
    if isinstance(role_id, int):
        return guild.get_role(role_id)
    if isinstance(role_id, str) and role_id.isdigit():
        return guild.get_role(int(role_id))
    if fallback_id:
        return guild.get_role(fallback_id)
    return None


def load_exp_data():
    """사용자 경험치 데이터를 Realtime DB에서 가져옵니다."""
    return db.reference("exp_data").get() or {}


def save_exp_data(data):
    """전체 경험치 데이터를 저장하며 실패를 호출부에 전달합니다."""
    db.reference("exp_data").set(data)

def save_user_exp(user_id, user_data):
    """특정 사용자 경험치 데이터를 저장하며 실패를 호출부에 전달합니다."""
    db.reference("exp_data").child(str(user_id)).set(user_data)

def load_mission_data():
    """일일 미션 데이터 로드"""
    return db.reference("mission_data").get() or {}


def save_mission_data(data):
    """전체 미션 데이터를 저장하며 실패를 호출부에 전달합니다."""
    db.reference("mission_data").set(data)

def save_user_mission(user_id, user_mission):
    """특정 사용자 미션 데이터를 저장하며 실패를 호출부에 전달합니다."""
    db.reference("mission_data").child(str(user_id)).set(user_mission)

def get_attendance_data():
    """출석 데이터를 불러옵니다."""
    return db.reference(ATTENDANCE_DB_KEY).get() or {}


def set_attendance_data(user_id, data):
    """출석 데이터를 저장하며 실패를 호출부에 전달합니다."""
    db.reference(ATTENDANCE_DB_KEY).child(str(user_id)).set(data)

def get_attendance_user(user_id: str) -> dict:
    """특정 유저 출석 데이터만 불러옵니다."""
    raw = db.reference(ATTENDANCE_DB_KEY).child(user_id).get()
    return raw if isinstance(raw, dict) else {}

def set_attendance_user(user_id: str, data: dict):
    """특정 유저 출석 데이터를 저장하며 실패를 호출부에 전달합니다."""
    db.reference(ATTENDANCE_DB_KEY).child(str(user_id)).set(data)

def bulk_update_attendance(updates: dict):
    """attendance_data 루트에 대해 update(부분 갱신)"""
    try:
        db.reference(ATTENDANCE_DB_KEY).update(updates)
    except Exception as e:
        print(f"❌ bulk_update_attendance 실패: {e}")


def save_exp_data_strict(data: dict):
    """실패를 숨기지 않는 전체 경험치 저장 함수."""
    db.reference("exp_data").set(data)


def save_mission_data_strict(data: dict):
    """실패를 숨기지 않는 전체 미션 저장 함수."""
    db.reference("mission_data").set(data)


def firebase_root_update_strict(updates: dict):
    """Realtime Database 다중 경로를 원자적으로 갱신합니다."""
    if not isinstance(updates, dict) or not updates:
        raise ValueError("Firebase update payload가 비어 있습니다.")
    db.reference().update(updates)


async def asave_exp_data_strict(data: dict):
    await asyncio.to_thread(save_exp_data_strict, data)


async def asave_mission_data_strict(data: dict):
    await asyncio.to_thread(save_mission_data_strict, data)


async def afirebase_root_update_strict(updates: dict):
    await asyncio.to_thread(firebase_root_update_strict, updates)

def load_json(path):
    """로컬 JSON 파일 로드 (없으면 빈 dict)"""
    if not os.path.exists(path):
        return {}
    with open(path, 'r') as f:
        return json.load(f)


def save_json(path, data):
    """로컬 JSON 파일 저장"""
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)


# ---- 유틸 함수 ----
# === 시즌패스 레벨 계산: 1~100 동일 간격 ===

def calculate_level(exp: int) -> int:
    """
    시즌패스 레벨 계산.
    - Lv.1은 0XP부터 시작
    - Lv.2부터는 SEASON_XP_PER_LEVEL 단위로 상승
    - 최대 Lv.100
    """
    try:
        exp = int(exp)
    except Exception:
        exp = 0

    exp = max(0, exp)
    level = (exp // SEASON_XP_PER_LEVEL) + 1
    return max(1, min(int(level), SEASON_MAX_LEVEL))


def get_level_progress(exp: int) -> tuple[int, int, int, float]:
    """
    현재 시즌패스 진행도 계산.
    반환값:
    - level: 현재 레벨
    - cur_xp: 현재 레벨 구간 내 경험치
    - need_xp: 다음 레벨까지 필요한 기준 경험치
    - pct: 현재 레벨 구간 진행률
    """
    try:
        exp = int(exp)
    except Exception:
        exp = 0

    exp = max(0, exp)
    level = calculate_level(exp)

    if level >= SEASON_MAX_LEVEL:
        return SEASON_MAX_LEVEL, SEASON_XP_PER_LEVEL, SEASON_XP_PER_LEVEL, 1.0

    current_level_start = (level - 1) * SEASON_XP_PER_LEVEL
    cur_xp = max(0, exp - current_level_start)
    need_xp = SEASON_XP_PER_LEVEL
    pct = cur_xp / need_xp if need_xp > 0 else 0.0

    return level, cur_xp, need_xp, _clamp01(pct)


def get_level_progress_percent(exp: int) -> int:
    """현재 레벨 구간 진행률을 0~100 정수로 반환."""
    _, _, _, pct = get_level_progress(exp)
    return int(round(_clamp01(pct) * 100))


_TITLE_SUFFIX_RE = re.compile(r"\s*\[[^\[\]]+\]\s*$")

def strip_title_suffix(name: str) -> str:
    """
    닉네임 뒤에 붙은 칭호/진행도 태그를 제거합니다.
    예:
    - 홍길동 [ Lv. 17 : 한여름의 항해 ] → 홍길동
    - 홍길동 [ 봄의 개척자 ] → 홍길동
    """
    if not name:
        return ""
    return _TITLE_SUFFIX_RE.sub("", name).strip()


def generate_nickname(base: str, level: int) -> str:
    """
    시즌패스 진행도 칭호를 닉네임 뒤에 붙입니다.
    형식: 닉네임 [ Lv. n : 시즌이름 ]
    """
    clean = strip_title_suffix(base)
    if not clean:
        clean = base.strip() if base else "Unknown"

    tag = f" [ Lv. {int(level)} : {CURRENT_SEASON_NAME} ]"

    # Discord 닉네임 32자 제한 대응
    max_base_len = 32 - len(tag)
    if max_base_len < 1:
        # 시즌명이 너무 긴 경우에도 최소한 닉네임 변경 실패를 줄이기 위한 보호
        short_tag = f" [ Lv. {int(level)} ]"
        max_base_len = max(1, 32 - len(short_tag))
        return (clean[:max_base_len] + short_tag)[:32]

    return (clean[:max_base_len] + tag)[:32]
    
# =========================
# Legacy Level 보존 유틸
# =========================
from bisect import bisect_right

LEGACY_LEVEL_MAX = 99

LEGACY_STAGES = [
    (1,   5,  200,   1.040, 1.00),
    (6,  10,  None,  1.045, 1.10),
    (11, 15,  None,  1.050, 1.11),
    (16, 20,  None,  1.056, 1.12),
    (21, 25,  None,  1.063, 1.12),
    (26, 30,  None,  1.071, 1.13),
    (31, 35,  None,  1.080, 1.14),
    (36, 40,  None,  1.090, 1.15),
    (41, 45,  None,  1.101, 1.16),
    (46, 50,  None,  1.113, 1.17),
    (51, 55,  None,  1.126, 1.18),
    (56, 60,  None,  1.140, 1.19),
    (61, 65,  None,  1.155, 1.20),
    (66, 70,  None,  1.171, 1.21),
    (71, 75,  None,  1.196, 1.22),
    (76, 80,  None,  1.213, 1.23),
    (81, 90,  None,  1.241, 1.24),
    (91, 99,  None,  1.270, 1.25),
]


def _build_legacy_level_deltas(stages, level_max: int) -> list[int]:
    deltas = []
    prev_d = None

    for start_level, end_level, start_delta, ratio, jump in stages:
        if start_delta is None:
            start_delta = int(round(prev_d * jump))

        for level in range(start_level, end_level + 1):
            if level == start_level:
                d = int(start_delta)
            else:
                d = int(round(d * ratio))

            if prev_d is not None and d <= prev_d:
                d = prev_d + 1

            deltas.append(d)
            prev_d = d

    if len(deltas) < level_max:
        deltas += [deltas[-1]] * (level_max - len(deltas))

    return deltas[:level_max]


LEGACY_DELTAS = _build_legacy_level_deltas(LEGACY_STAGES, LEGACY_LEVEL_MAX)

LEGACY_THRESHOLDS = [0]
for _legacy_delta in LEGACY_DELTAS:
    LEGACY_THRESHOLDS.append(LEGACY_THRESHOLDS[-1] + _legacy_delta)


def calculate_legacy_level_from_exp(exp: int) -> int:
    """
    기존 레벨제 기준으로 경험치를 레벨로 환산합니다.
    새 시즌패스 레벨 계산과 섞이지 않도록 별도 함수로 유지합니다.
    """
    try:
        exp = int(exp)
    except Exception:
        exp = 0

    exp = max(0, exp)
    idx = bisect_right(LEGACY_THRESHOLDS, exp) - 1
    return max(1, min(idx + 1, LEGACY_LEVEL_MAX))


def get_legacy_level_from_user_record(user_data: dict) -> int:
    """
    기존 유저 레코드에서 보존할 레벨을 가져옵니다.
    1순위: 기존 경험치 곡선으로 exp 재계산
    2순위: exp가 없거나 손상된 경우 DB에 저장된 기존 level 값
    """
    if not isinstance(user_data, dict):
        return 1

    if "exp" in user_data:
        try:
            legacy_exp = int(user_data.get("exp", 0))
            if legacy_exp >= 0:
                return calculate_legacy_level_from_exp(legacy_exp)
        except Exception:
            pass

    try:
        raw_level = int(user_data.get("level", 1))
    except Exception:
        raw_level = 1

    return max(1, min(raw_level, LEGACY_LEVEL_MAX))

# 기존 레벨 역할 ID 목록
# 시즌패스 전환 시 서버원에게서 제거할 대상입니다.
LEGACY_LEVEL_ROLE_IDS = [
    1386685631627006000,
    1386685631627005999,
    1386685631627005998,
    1386685631627005997,
    1386685631627005996,
]

LEGACY_FORGOTTEN_TITLE_ID = "legacy_forgotten_memory"

async def remove_legacy_level_roles(member: discord.Member) -> tuple[int, int]:
    """
    기존 레벨 역할을 서버원에게서 제거합니다.
    반환값:
    - removed_count: 제거 성공 또는 제거 시도한 역할 수
    - failed_count: 제거 실패 수
    """
    if not member or not isinstance(member, discord.Member):
        return 0, 0

    roles_to_remove = [
        role for role in member.roles
        if role.id in LEGACY_LEVEL_ROLE_IDS
    ]

    if not roles_to_remove:
        return 0, 0

    try:
        await member.remove_roles(
            *roles_to_remove,
            reason="시즌패스 전환에 따른 기존 레벨 역할 제거"
        )
        return len(roles_to_remove), 0
    except Exception as e:
        logging.warning(
            f"[legacy-role] remove failed uid={getattr(member, 'id', '?')}: {e!r}"
        )
        return 0, len(roles_to_remove)

# =========================
# Season Pass 상태/칭호 유틸
# =========================

def _date_to_str(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def _season_dates_for_type(season_year: int, season_type: str) -> dict:
    if season_type == "spring":
        return {
            "regular_start": date(season_year, 3, 1),
            "regular_end": date(season_year, 5, 24),
            "preseason_start": date(season_year, 5, 25),
            "preseason_end": date(season_year, 5, 31),
        }
    if season_type == "summer":
        return {
            "regular_start": date(season_year, 6, 1),
            "regular_end": date(season_year, 8, 24),
            "preseason_start": date(season_year, 8, 25),
            "preseason_end": date(season_year, 8, 31),
        }
    if season_type == "fall":
        return {
            "regular_start": date(season_year, 9, 1),
            "regular_end": date(season_year, 11, 23),
            "preseason_start": date(season_year, 11, 24),
            "preseason_end": date(season_year, 11, 30),
        }

    # winter: season_year년 12월 ~ season_year+1년 2월
    feb_year = season_year + 1
    march_first = date(feb_year, 3, 1)
    feb_last = march_first - timedelta(days=1)
    preseason_start = feb_last - timedelta(days=6)
    return {
        "regular_start": date(season_year, 12, 1),
        "regular_end": preseason_start - timedelta(days=1),
        "preseason_start": preseason_start,
        "preseason_end": feb_last,
    }


def _next_season_id_after(season_id: str) -> str:
    try:
        year_str, season_type = season_id.split("_", 1)
        year = int(year_str)
    except Exception:
        now = datetime.now(KST)
        cal = get_calendar_season_info(now)
        year = int(cal["season_year"])
        season_type = cal["season_type"]

    idx = SEASON_TYPE_ORDER.index(season_type)
    if idx == len(SEASON_TYPE_ORDER) - 1:
        return f"{year + 1}_spring"
    return f"{year}_{SEASON_TYPE_ORDER[idx + 1]}"


def get_calendar_season_info(now_kst: datetime | None = None) -> dict:
    now_kst = now_kst or datetime.now(KST)
    today = now_kst.date()
    m = today.month

    if 3 <= m <= 5:
        season_year, season_type = today.year, "spring"
    elif 6 <= m <= 8:
        season_year, season_type = today.year, "summer"
    elif 9 <= m <= 11:
        season_year, season_type = today.year, "fall"
    elif m == 12:
        season_year, season_type = today.year, "winter"
    else:
        season_year, season_type = today.year - 1, "winter"

    dates = _season_dates_for_type(season_year, season_type)
    if dates["preseason_start"] <= today <= dates["preseason_end"]:
        status = SEASON_STATUS_PRESEASON
    else:
        status = SEASON_STATUS_REGULAR

    season_id = f"{season_year}_{season_type}"
    return {
        "season_id": season_id,
        "season_year": season_year,
        "season_type": season_type,
        "season_label": SEASON_TYPE_LABELS.get(season_type, season_type),
        "status": status,
        "regular_start": _date_to_str(dates["regular_start"]),
        "regular_end": _date_to_str(dates["regular_end"]),
        "preseason_start": _date_to_str(dates["preseason_start"]),
        "preseason_end": _date_to_str(dates["preseason_end"]),
        "next_season_id": _next_season_id_after(season_id),
    }


def _default_season_state_from_calendar(cal: dict) -> dict:
    return {
        "current_season_id": cal["season_id"],
        "current_season_name": "시즌패스 준비 중",
        "current_season_type": cal["season_type"],
        "current_season_label": cal["season_label"],
        "status": SEASON_STATUS_LOCKED,
        "settled": False,
        "next_ready": False,
        "first_season_started": False,
        "first_season_started_at": "",
        "first_season_started_by": "",
        "next_season_id": cal["next_season_id"],
        "next_season_name": "",
        "season_notice_channel_id": SEASON_NOTICE_CHANNEL_ID,
        "created_at": datetime.now(KST).isoformat(),
    }


def _season_state_ref():
    return db.reference("season_state")


def _season_rewards_ref(season_id: str):
    return db.reference("season_rewards").child(season_id)


def _user_titles_ref(uid: str):
    return db.reference("user_titles").child(str(uid))


def _season_completion_ref(season_id: str, uid: str):
    return db.reference("season_completion").child(season_id).child(str(uid))


def _season_records_ref(season_id: str):
    return db.reference("season_records").child(season_id)

def _legacy_migration_ref(season_id: str):
    return db.reference("legacy_migration_records").child(season_id)

async def aget_legacy_migration_record(season_id: str) -> dict:
    def _get():
        raw = _legacy_migration_ref(season_id).get()
        return raw if isinstance(raw, dict) else {}
    return await asyncio.to_thread(_get)


async def aset_legacy_migration_record(season_id: str, data: dict):
    await asyncio.to_thread(lambda: _legacy_migration_ref(season_id).set(data))


async def aupdate_legacy_migration_record(season_id: str, data: dict):
    await asyncio.to_thread(lambda: _legacy_migration_ref(season_id).update(data))

async def aget_effective_season_state() -> dict:
    """달력 기준 상태를 계산하고 변경된 필드만 Firebase에 반영합니다."""
    def _get_and_fix():
        cal = get_calendar_season_info(datetime.now(KST))
        ref = _season_state_ref()
        state = ref.get()

        if not isinstance(state, dict):
            persisted = _default_season_state_from_calendar(cal)
            ref.set(persisted)
            return {**persisted, "calendar": cal}

        patch: dict[str, object] = {}
        defaults = _default_season_state_from_calendar(cal)
        for key, value in defaults.items():
            if key not in state:
                patch[key] = value

        effective = {**state, **patch}
        if not effective.get("first_season_started"):
            desired = {
                "current_season_id": cal["season_id"],
                "current_season_type": cal["season_type"],
                "current_season_label": cal["season_label"],
                "next_season_id": cal["next_season_id"],
                "status": SEASON_STATUS_LOCKED,
            }
        elif effective.get("current_season_id") != cal["season_id"]:
            desired = {
                "status": SEASON_STATUS_LOCKED,
                "next_season_id": cal["season_id"],
            }
        else:
            desired = {
                "status": cal["status"],
                "current_season_type": cal["season_type"],
                "current_season_label": cal["season_label"],
                "next_season_id": cal["next_season_id"],
            }

        for key, value in desired.items():
            if effective.get(key) != value:
                patch[key] = value
                effective[key] = value

        if patch:
            ref.update(patch)

        effective["calendar"] = cal
        return effective

    return await asyncio.to_thread(_get_and_fix)


async def aseason_xp_enabled() -> bool:
    state = await aget_effective_season_state()
    return state.get("status") == SEASON_STATUS_REGULAR


def season_status_label(status: str) -> str:
    return {
        SEASON_STATUS_REGULAR: "정규 시즌",
        SEASON_STATUS_PRESEASON: "프리시즌",
        SEASON_STATUS_LOCKED: "시즌 잠금",
    }.get(status, status or "알 수 없음")


def days_left_until(date_str: str) -> int:
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
        today = datetime.now(KST).date()
        return max(0, (target - today).days)
    except Exception:
        return 0


def make_title_id(season_id: str) -> str:
    return f"{season_id}_lv100"


def generate_nickname_with_title(base: str, title_text: str) -> str:
    clean = strip_title_suffix(base)
    if not clean:
        clean = base.strip() if base else "Unknown"
    tag = f" [ {title_text} ]"
    max_base_len = 32 - len(tag)
    if max_base_len < 1:
        short_tag = " [ 칭호 ]"
        max_base_len = max(1, 32 - len(short_tag))
        return (clean[:max_base_len] + short_tag)[:32]
    return (clean[:max_base_len] + tag)[:32]


def progress_title_text(level: int, state: dict) -> str:
    if not state.get("first_season_started"):
        return "??? : 시즌 준비 중"
    if state.get("status") == SEASON_STATUS_REGULAR:
        return f"Lv. {int(level)} : {state.get('current_season_name', CURRENT_SEASON_NAME)}"
    return "??? : 프리시즌"


async def aget_user_titles(uid: str) -> dict:
    def _get():
        raw = _user_titles_ref(str(uid)).get()
        if not isinstance(raw, dict):
            raw = {}
        if not isinstance(raw.get("owned"), dict):
            raw["owned"] = {}
        if not isinstance(raw.get("equipped"), dict):
            raw["equipped"] = {"type": "progress"}
        return raw
    return await asyncio.to_thread(_get)


async def aset_user_equipped_title(uid: str, equipped: dict):
    """보유 칭호 목록을 건드리지 않고 착용 정보만 갱신합니다."""
    await asyncio.to_thread(
        lambda: _user_titles_ref(str(uid)).child("equipped").set(equipped)
    )


async def aget_equipped_title_text(uid: str, level: int) -> str:
    state = await aget_effective_season_state()
    titles = await aget_user_titles(uid)
    equipped = titles.get("equipped") or {"type": "progress"}

    if equipped.get("type") == "title":
        title_id = equipped.get("title_id")
        owned = titles.get("owned", {})
        title = owned.get(title_id) if isinstance(owned, dict) else None
        if isinstance(title, dict) and title.get("title_name"):
            return str(title["title_name"])

    return progress_title_text(level, state)


async def apply_member_title(member: discord.Member, level: int) -> bool:
    """닉네임 칭호 반영 성공 여부를 반환합니다."""
    if not member or member.id == getattr(member.guild, "owner_id", None):
        return False
    try:
        title_text = await aget_equipped_title_text(str(member.id), level)
        new_nick = generate_nickname_with_title(member.display_name, title_text)
        if member.display_name == new_nick:
            return True
        await member.edit(nick=new_nick, reason="시즌패스 칭호 갱신")
        return True
    except Exception as e:
        logging.warning(f"[title] nickname update failed uid={getattr(member, 'id', '?')}: {e!r}")
        return False


async def maybe_award_level100(member: discord.Member, level: int, *, reason: str = "levelup") -> dict:
    if not member or level < SEASON_MAX_LEVEL:
        return {"awarded": False, "reason": "not_max_level"}

    state = await aget_effective_season_state()
    settlement_reasons = {"season_settlement", "reward_set_retroactive"}
    if state.get("status") != SEASON_STATUS_REGULAR and reason not in settlement_reasons:
        return {"awarded": False, "reason": "not_regular_season"}

    season_id = state.get("current_season_id")
    if not season_id:
        return {"awarded": False, "reason": "no_season_id"}

    uid = str(member.id)
    cache_key = (str(season_id), uid)
    if cache_key in _LEVEL100_AWARD_CACHE:
        return {"awarded": False, "reason": "already_owned_cached"}

    title_id = make_title_id(season_id)

    def _award_sync():
        now = datetime.now(KST).isoformat()
        reward = _season_rewards_ref(season_id).get() or {}
        title_name = reward.get("title_name")
        description = reward.get("description", "")
        completion_ref = _season_completion_ref(season_id, uid)
        completion = completion_ref.get() or {}

        if not title_name:
            completion.update({
                "reached_at": completion.get("reached_at") or now,
                "last_checked_at": now,
                "level": int(level),
                "reason": reason,
                "reward_given": False,
                "reward_pending": True,
            })
            completion_ref.set(completion)
            return {"awarded": False, "reason": "reward_not_configured", "title_name": None}

        titles_ref = _user_titles_ref(uid)
        titles = titles_ref.get() or {}
        owned = titles.get("owned") if isinstance(titles.get("owned"), dict) else {}
        existing = owned.get(title_id) if isinstance(owned.get(title_id), dict) else {}
        already_owned = bool(existing)
        metadata_same = (
            existing.get("title_name") == title_name
            and existing.get("description", "") == description
            and existing.get("source_season_id") == season_id
        )

        if completion.get("reward_given") and already_owned and metadata_same:
            return {
                "awarded": False,
                "reason": "already_owned",
                "title_name": title_name,
                "reward_given": True,
            }

        title_record = {
            **existing,
            "title_name": title_name,
            "source_season_id": season_id,
            "acquired_at": existing.get("acquired_at") or now,
            "description": description,
        }
        completion.update({
            "reached_at": completion.get("reached_at") or now,
            "last_checked_at": now,
            "level": int(level),
            "reason": reason,
            "reward_given": True,
            "reward_pending": False,
            "title_id": title_id,
            "title_name": title_name,
        })
        award_updates: dict[str, object] = {
            f"user_titles/{uid}/owned/{title_id}": title_record,
            f"season_completion/{season_id}/{uid}": completion,
        }
        if not isinstance(titles.get("equipped"), dict):
            award_updates[f"user_titles/{uid}/equipped"] = {"type": "progress"}
        firebase_root_update_strict(award_updates)
        return {
            "awarded": not already_owned,
            "reason": "ok" if not already_owned else "metadata_updated",
            "title_name": title_name,
            "reward_given": True,
        }

    async with get_user_state_lock(uid):
        # 잠금을 기다리는 사이 다른 루틴이 지급을 끝냈을 수 있습니다.
        if cache_key in _LEVEL100_AWARD_CACHE:
            return {"awarded": False, "reason": "already_owned_cached"}
        result = await asyncio.to_thread(_award_sync)
        if result.get("reward_given"):
            _LEVEL100_AWARD_CACHE.add(cache_key)

    if result.get("awarded"):
        dm_sent = False
        try:
            await member.send(
                f"🎉 축하합니다! `{state.get('current_season_name', '현재 시즌')}` 시즌패스 Lv.100 달성 보상으로 "
                f"칭호 `[ {result.get('title_name')} ]` 을 획득했습니다.\n"
                "서버에서 `/칭호관리` 명령어로 착용할 수 있습니다."
            )
            dm_sent = True
        except Exception:
            pass

        await asyncio.to_thread(
            lambda: _season_completion_ref(season_id, uid).update({
                "dm_sent": dm_sent,
                "dm_checked_at": datetime.now(KST).isoformat(),
            })
        )

        try:
            log = member.guild.get_channel(LOG_CHANNEL_ID)
            if log:
                await log.send(
                    f"[🏆 시즌 보상] {member.display_name} 님이 Lv.100 보상 "
                    f"`[ {result.get('title_name')} ]` 을 획득했습니다. "
                    f"DM: {'성공' if dm_sent else '실패'}",
                    allowed_mentions=ALLOW_NO_PING,
                )
        except Exception:
            pass

    return result



async def update_existing_season_title_metadata(season_id: str, title_name: str, description: str) -> int:
    """이미 지급된 동일 시즌 칭호의 이름과 설명을 일괄 동기화합니다."""
    title_id = make_title_id(season_id)

    def _sync() -> int:
        all_titles = db.reference("user_titles").get() or {}
        completions = db.reference("season_completion").child(season_id).get() or {}
        updates: dict[str, object] = {}
        updated = 0

        if isinstance(all_titles, dict):
            for uid, record in all_titles.items():
                if not isinstance(record, dict):
                    continue
                owned = record.get("owned")
                if not isinstance(owned, dict) or not isinstance(owned.get(title_id), dict):
                    continue
                updates[f"user_titles/{uid}/owned/{title_id}/title_name"] = title_name
                updates[f"user_titles/{uid}/owned/{title_id}/description"] = description
                updates[f"user_titles/{uid}/owned/{title_id}/source_season_id"] = season_id
                updated += 1

        if isinstance(completions, dict):
            for uid, completion in completions.items():
                if isinstance(completion, dict) and completion.get("reward_given"):
                    updates[f"season_completion/{season_id}/{uid}/title_name"] = title_name

        if updates:
            firebase_root_update_strict(updates)
        return updated

    updated_count = await asyncio.to_thread(_sync)
    for key in list(_LEVEL100_AWARD_CACHE):
        if key[0] == str(season_id):
            _LEVEL100_AWARD_CACHE.discard(key)
    return updated_count


async def reset_progress_title_members(guild: discord.Guild, *, level: int = 1) -> dict:
    updated = 0
    failed = 0
    for member in guild.members:
        if member.bot or member.id == guild.owner_id:
            continue
        try:
            titles = await aget_user_titles(str(member.id))
            equipped = titles.get("equipped") or {"type": "progress"}
            if equipped.get("type") != "title":
                if await apply_member_title(member, level):
                    updated += 1
                else:
                    failed += 1
                await asyncio.sleep(0.15)
        except Exception:
            failed += 1
    return {"updated": updated, "failed": failed}


def get_week_key_kst(dt: datetime) -> str:
    """
    주 단위 키를 ISO 형식으로 반환합니다. 
    예: 2025년 7월 22일 → '2025-W29'
    """
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"

def get_month_key_kst(dt: datetime) -> str:
    """
    월 단위 키를 'YYYY-M' 형식으로 반환합니다.
    예: 2025년 7월 → '2025-7'
    """
    # 한 자리 월에는 앞에 ‘0’을 붙이지 않음
    return f"{dt.year}-{dt.month}"


async def update_role_and_nick(member: discord.Member, new_level: int) -> bool:
    """레벨 변화 시 현재 칭호를 즉시 반영합니다."""
    if not member or member.id == getattr(member.guild, "owner_id", None):
        return False

    # 첫 시즌 시작 전에는 기존 서버 닉네임을 변경하지 않습니다.
    state = await aget_effective_season_state()
    if not state.get("first_season_started"):
        return False

    return await apply_member_title(member, new_level)
# ────────────────────────────────────────────────────────────
# === [SAFEGUARD UTILS] ===
def _is_bot_message(message) -> bool:
    # 봇/웹훅은 무시
    if getattr(message.author, "bot", False):
        return True
    if getattr(message, "webhook_id", None):
        return True
    return False

def _is_low_value_context(message) -> bool:
    # DM, 스레드 등 필요 시 필터링
    try:
        if isinstance(message.channel, discord.DMChannel):
            return True
        # 스레드 필터링이 필요하면 아래 주석 해제
        # if isinstance(message.channel, discord.Thread):
        #     return True
    except Exception:
        pass
    return False

def _hit_cooldowns(message):
    """동일 유저의 과도한 연속 이벤트만 제한합니다."""
    now = time.time()
    user_id = getattr(message.author, "id", None)
    if user_id is None:
        return None

    if now - _last_user_ts[user_id] < SAFEGUARD_MIN_INTERVAL_PER_USER:
        return "user_cooldown"

    _last_user_ts[user_id] = now
    return None

# ---- Discord Bot 초기화 (슬래시 전용) ---
intents = discord.Intents.all()

# --- AllowedMentions 공통 설정 (핑 방지용) ---
ALLOW_NO_PING = discord.AllowedMentions(
    everyone=False,     # @everyone 금지
    users=False,        # 유저 멘션 금지
    roles=False,        # 역할 멘션 금지
    replied_user=False  # 답장 대상 멘션 금지
)
# --- /END AllowedMentions 설정 ---

bot = commands.Bot(
    command_prefix=commands.when_mentioned,     # 프리픽스 명령어 비활성화
    help_command=None,      # 기본 도움말 명령어 비활성화
    intents=intents
)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    original = getattr(error, "original", error)
    error_name = type(original).__name__
    if error_name == "MissingPermissions":
        message = "❌ 이 명령어는 관리자만 사용할 수 있습니다."
    elif error_name == "NoPrivateMessage":
        message = "❌ 이 명령어는 서버에서만 사용할 수 있습니다."
    elif error_name == "CommandOnCooldown":
        retry_after = _safe_float(getattr(original, "retry_after", 0), 0)
        message = f"❌ 잠시 후 다시 시도해주세요. ({retry_after:.1f}초)"
    else:
        logging.error(
            "[app-command] unhandled error command=%s error=%r",
            getattr(getattr(interaction, "command", None), "name", "unknown"),
            original,
            exc_info=(type(original), original, original.__traceback__),
        )
        message = "❌ 명령어 처리 중 오류가 발생했습니다. 관리자 로그를 확인해주세요."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except Exception:
        pass


# ---- on_ready ----
@bot.event
async def on_ready():

    # 2) 시즌 보이스 채널 업데이트 (예외 로깅)
    try:
        await update_season_voice_channels(bot)
    except Exception as e:
        print(f"[on_ready] update_season_voice_channels error: {e!r}")


    print(f"✅ {bot.user} 온라인")
    logging.info(f"[ready] logged in as {bot.user} (id={bot.user.id})")
    await bot.change_presence(activity=discord.Game("제가 오프라인이라면, 서버장에게 말해주세요!"))
    
    # 3) 슬래시 커맨드 동기화: 최초 1회만
    if not getattr(bot, "_synced", False):
        try:
            synced = await bot.tree.sync()  # 전역 등록
            bot._synced = True
            print(f"🌐 전역 슬래시 커맨드 {len(synced)}개 동기화 완료")
        except Exception as e:
            print(f"❌ 슬래시 커맨드 동기화 실패: {e!r}")

    # 4) 백그라운드 태스크 안전 시작(중복 방지)
    for task in (voice_xp_task, reset_daily_missions, repeat_vc_mission_task, inactive_user_log_task, voice_count_channel_task, season_transition_task):
        try:
            if not task.is_running():
                task.start()
        except Exception as e:
            print(f"[on_ready] task start error: {e!r}")
            

# ---- on_member_update: 환영 메시지 및 역할 동기화 ----
@bot.event
async def on_member_update(before, after):
    before_roles = {role.id for role in before.roles}
    after_roles = {role.id for role in after.roles}
    added = after_roles - before_roles

    if before_roles != after_roles:
        await update_season_voice_channels(bot)

    try:
        cfg = await aget_guild_config(after.guild.id)
    except Exception:
        cfg = {}
    welcome_role_id = _safe_int(
        _cfg_get(cfg, "roles", "thread_role_id", default=THREAD_ROLE_ID),
        THREAD_ROLE_ID,
    )
    welcome_channel_id = _safe_int(
        _cfg_get(cfg, "channels", "thread_role_channel_id", default=TARGET_TEXT_CHANNEL_ID),
        TARGET_TEXT_CHANNEL_ID,
    )

    if welcome_role_id not in added:
        return

    channel = after.guild.get_channel(welcome_channel_id)
    if channel and hasattr(channel, "send"):
        try:
            await channel.send(
                f"환영합니다 {after.mention} 님! '사계절, 그 사이' 서버입니다.\n"
                "프로필 우클릭 → 편집으로 닉네임을 변경할 수 있어요!\n"
                "닉네임은 한글만 사용 가능합니다!"
            )
        except Exception as e:
            logging.warning(f"[on_member_update] welcome send failed: {e!r}")

    uid = str(after.id)
    try:
        async with get_user_state_lock(uid):
            user_data = await aget_user_exp(uid)
            await asave_user_exp(uid, user_data)
        await update_role_and_nick(after, calculate_level(user_data.get("exp", 0)))
    except Exception as e:
        logging.exception(f"[on_member_update] initialization failed uid={uid}: {e}")


# ---- 백그라운드 태스크 정의 ----
@tasks.loop(time=dtime(hour=3, minute=0, tzinfo=pytz.FixedOffset(540)))
@guard_background_task("inactive_user_log")
async def inactive_user_log_task():
    """매일 03:00(KST)에 장기 미접속 사용자 추방과 결과 로그를 처리합니다."""
    if not INACTIVE_AUTO_KICK_ENABLED:
        logging.info("[inactive] automatic kick is disabled by INACTIVE_AUTO_KICK_ENABLED")
        return

    threshold = datetime.now(KST) - timedelta(days=INACTIVE_KICK_DAYS)

    for guild in bot.guilds:
        try:
            cfg = await aget_guild_config(guild.id)
        except Exception as e:
            logging.exception(f"[inactive] config load failed guild={guild.id}: {e}")
            continue
        log_channel = await get_channel_from_cfg(
            guild, cfg, "inactive_log_channel_id", INACTIVE_LOG_CHANNEL_ID
        )
        if not log_channel:
            continue

        kicked: list[str] = []
        for member in guild.members:
            if member.bot or member.id == guild.owner_id:
                continue
            if any(role.id in EXEMPT_ROLE_IDS for role in member.roles):
                continue

            try:
                user = await aget_user_exp(str(member.id))
                last_ts = _safe_float(user.get("last_activity"), 0)
                if last_ts <= 0:
                    continue
                last_active = datetime.fromtimestamp(last_ts, KST)
                if last_active >= threshold:
                    continue

                try:
                    embed = discord.Embed(
                        title="📢 사계절, 그 사이 서버 안내",
                        description=(
                            "안녕하세요, '사계절, 그 사이' 서버 서버장입니다!\n\n"
                            f"최근 {INACTIVE_KICK_DAYS}일간 서버에 기록된 활동 내역이 없어,\n"
                            "공지해둔 규칙 사항에 따라 서버에서 추방 처리가 진행됩니다.\n\n"
                            "아래 링크를 통해 언제든 다시 서버에 입장하실 수 있습니다.\n\n"
                            "👉 https://discord.gg/Npuxrkf38G\n\n"
                            "- '사계절, 그 사이' 서버장 새벽녘 -"
                        ),
                        color=0x3498DB,
                    )
                    await member.send(embed=embed)
                except Exception:
                    await log_channel.send(f"❌ {member.display_name} 님에게 DM 전송 실패")

                await member.kick(reason=f"{INACTIVE_KICK_DAYS}일 미접속 자동 추방")
                await log_channel.send(
                    f"👢 {member.display_name} 님이 {INACTIVE_KICK_DAYS}일간 미접속으로 추방되었습니다."
                )
                kicked.append(member.display_name)
            except Exception as e:
                logging.exception(f"[inactive] uid={member.id} error: {e}")
                try:
                    await log_channel.send(f"❌ {member.display_name} 님 미접속 처리 실패: {type(e).__name__}")
                except Exception:
                    pass

        if not kicked:
            await log_channel.send(
                f"✅ 현재 {INACTIVE_KICK_DAYS}일 이상 미접속 중인 사용자가 없습니다."
            )
        
@tasks.loop(time=dtime(hour=0, minute=0, tzinfo=pytz.FixedOffset(540)))
@guard_background_task("reset_daily_missions")
async def reset_daily_missions():
    """매일 자정(KST)에 일일 미션 데이터를 초기화합니다."""
    try:
        await asave_mission_data({})
        save_json(MISSION_PATH, {})
        print("🔁 일일 미션 초기화 완료")
    except Exception as e:
        logging.exception(f"[daily-mission-reset] failed: {e}")

@tasks.loop(seconds=VOICE_COOLDOWN)
@guard_background_task("voice_xp")
async def voice_xp_task():
    """음성 채널 경험치 태스크."""
    if not await aseason_xp_enabled():
        return

    now_ts = time.time()
    for guild in bot.guilds:
        try:
            cfg = await aget_guild_config(guild.id)
        except Exception as e:
            logging.exception(f"[voice_xp_task] config load failed guild={guild.id}: {e}")
            continue
        afk_ids = _cfg_get(cfg, "voice", "afk_channel_ids", default=AFK_CHANNEL_IDS) or []
        sp_cat_ids = _cfg_get(cfg, "voice", "special_vc_category_ids", default=SPECIAL_VC_CATEGORY_IDS) or []
        afk_ids = [int(x) for x in afk_ids if str(x).isdigit()]
        sp_cat_ids = [int(x) for x in sp_cat_ids if str(x).isdigit()]

        try:
            voice_like_channels = list(guild.voice_channels) + list(getattr(guild, "stage_channels", []))
        except Exception:
            voice_like_channels = list(guild.voice_channels)

        for vc in voice_like_channels:
            if vc.id in afk_ids:
                continue
            is_special = bool(vc.category and vc.category.id in sp_cat_ids)

            for member in vc.members:
                if member.bot:
                    continue
                try:
                    uid = str(member.id)
                    gain = random.randint(VOICE_MIN_XP, VOICE_MAX_XP)
                    if is_special:
                        gain = max(1, int(gain * 0.2))

                    async with get_user_state_lock(uid):
                        user_data = await aget_user_exp(uid)
                        prev_level = calculate_level(user_data.get("exp", 0))
                        user_data["exp"] = max(0, _safe_int(user_data.get("exp", 0), 0)) + gain
                        if not is_special:
                            user_data["voice_minutes"] = max(0, _safe_int(user_data.get("voice_minutes", 0), 0)) + 1
                        user_data["last_activity"] = now_ts
                        new_level = calculate_level(user_data["exp"])
                        user_data["level"] = new_level
                        await asave_user_exp(uid, user_data)

                    if new_level != prev_level:
                        await update_role_and_nick(member, new_level)
                        announce = await get_channel_from_cfg(
                            guild, cfg, "levelup_channel_id", LEVELUP_ANNOUNCE_CHANNEL
                        )
                        if announce:
                            await announce.send(
                                f"🎉 {member.display_name} 님이 시즌 Lv.{new_level} 에 도달했습니다! 🎊",
                                allowed_mentions=ALLOW_NO_PING,
                            )

                    if new_level >= SEASON_MAX_LEVEL:
                        await maybe_award_level100(member, new_level, reason="voice_xp")
                except Exception as e:
                    logging.exception(f"[voice_xp_task] uid={getattr(member, 'id', '?')} error: {e}")

@voice_xp_task.error
async def voice_xp_task_error(error):
    logging.exception(f"[voice_xp_task] crashed: {error}")
    try:
        # 예외로 루프가 중지됐으면 재시작 시도
        if not voice_xp_task.is_running():
            voice_xp_task.start()
    except Exception as e2:
        logging.exception(f"[voice_xp_task] restart failed: {e2}")
        
@tasks.loop(seconds=60)
@guard_background_task("repeat_vc_mission")
async def repeat_vc_mission_task():
    """5인 이상 음성방 반복 미션을 유저 단위로 안전하게 누적합니다."""
    if not await aseason_xp_enabled():
        return

    today = datetime.now(KST).strftime("%Y-%m-%d")
    for guild in bot.guilds:
        try:
            cfg = await aget_guild_config(guild.id)
        except Exception as e:
            logging.exception(f"[repeat_vc_mission] config load failed guild={guild.id}: {e}")
            continue
        afk_ids = _cfg_get(cfg, "voice", "afk_channel_ids", default=AFK_CHANNEL_IDS) or []
        afk_ids = [int(x) for x in afk_ids if str(x).isdigit()]
        voice_like_channels = list(guild.voice_channels) + list(getattr(guild, "stage_channels", []))

        for vc in voice_like_channels:
            if vc.id in afk_ids:
                continue
            humans = [member for member in vc.members if not member.bot]
            if len(humans) < REPEAT_VC_MIN_PEOPLE:
                continue

            for member in humans:
                uid = str(member.id)
                reward_due = False
                new_level = prev_level = 1
                try:
                    async with get_user_state_lock(uid):
                        user_m = await aget_user_mission(uid, today)
                        if not isinstance(user_m, dict) or user_m.get("date") != today:
                            user_m = {
                                "date": today,
                                "text": {"count": 0, "completed": False},
                                "repeat_vc": {"minutes": 0},
                            }
                        if not isinstance(user_m.get("text"), dict):
                            user_m["text"] = {"count": 0, "completed": False}
                        if not isinstance(user_m.get("repeat_vc"), dict):
                            user_m["repeat_vc"] = {"minutes": 0}

                        minutes = max(0, _safe_int(user_m["repeat_vc"].get("minutes", 0), 0)) + 1
                        user_m["repeat_vc"]["minutes"] = minutes
                        reward_due = minutes % REPEAT_VC_REQUIRED_MINUTES == 0

                        commit_updates: dict[str, object] = {
                            f"mission_data/{uid}": user_m,
                        }
                        if reward_due:
                            uexp = await aget_user_exp(uid)
                            prev_level = calculate_level(uexp.get("exp", 0))
                            uexp["exp"] = max(0, _safe_int(uexp.get("exp", 0), 0)) + REPEAT_VC_EXP_REWARD
                            uexp["level"] = calculate_level(uexp["exp"])
                            uexp["last_activity"] = time.time()
                            new_level = uexp["level"]
                            commit_updates[f"exp_data/{uid}"] = uexp

                        await afirebase_root_update_strict(commit_updates)

                    if reward_due:
                        if new_level != prev_level:
                            await update_role_and_nick(member, new_level)
                        if new_level >= SEASON_MAX_LEVEL:
                            await maybe_award_level100(member, new_level, reason="repeat_vc_mission")

                        log = await get_channel_from_cfg(guild, cfg, "log_channel_id", LOG_CHANNEL_ID)
                        if log:
                            await log.send(
                                f"[🧾 로그] {member.display_name} 님이 반복 VC 미션 완료! "
                                f"+{REPEAT_VC_EXP_REWARD}XP",
                                allowed_mentions=ALLOW_NO_PING,
                            )
                except Exception as e:
                    logging.exception(f"[repeat_vc_mission] uid={uid} error: {e}")

    # 로컬 파일은 DB의 최신 상태를 읽어 백업만 하며 DB로 다시 덮어쓰지 않습니다.
    try:
        latest = await aload_mission_data()
        save_json(MISSION_PATH, latest if isinstance(latest, dict) else {})
    except Exception as e:
        logging.warning(f"[repeat_vc_mission] local backup failed: {e!r}")

@tasks.loop(seconds=60)
@guard_background_task("voice_count_channel")
async def voice_count_channel_task():
    for guild in bot.guilds:
        try:
            cfg = await aget_guild_config(guild.id)
            items = cfg.get("voice_count_channels", [])
            if not isinstance(items, list):
                continue

            for item in items:
                try:
                    if not isinstance(item, dict):
                        continue
                    role_id = _safe_int(item.get("role_id"), 0)
                    channel_id = _safe_int(item.get("channel_id"), 0)
                    role = guild.get_role(role_id)
                    channel = guild.get_channel(channel_id)
                    if not role or not channel:
                        continue
                    count = sum(1 for member in role.members if not member.bot)
                    new_name = _replace_count_suffix(channel.name, count)
                    if new_name and new_name != channel.name:
                        await channel.edit(name=new_name, reason="역할 인원수 자동 반영")
                except Exception as e:
                    logging.warning(f"[voice-count] item update failed guild={guild.id}: {e!r}")
        except Exception as e:
            logging.exception(f"[voice-count] guild={guild.id} error: {e}")

@tasks.loop(minutes=5)
@guard_background_task("season_transition")
async def season_transition_task():
    """
    시즌 시작 자동 처리 태스크.
    정산 완료 + 다음 시즌 준비 완료 + 날짜상 다음 시즌 진입 시
    시즌 시작 공지와 진행도 칭호 갱신을 자동 처리합니다.
    """
    try:
        for guild in bot.guilds:
            await process_season_start_if_needed(guild)
    except Exception as e:
        logging.exception(f"[season_transition_task] error: {e}")

@bot.event
async def on_message(message):
    try:
        if _is_bot_message(message) or _is_low_value_context(message):
            return
        if _hit_cooldowns(message) is not None:
            return
    except Exception as e:
        logging.warning(f"[on_message] safeguard error: {e!r}")
        return

    try:
        text = (message.content or "").strip()
        if not text or not message.guild:
            return

        cfg = await aget_guild_config(message.guild.id)
        thread_ch_id = _safe_int(
            _cfg_get(cfg, "channels", "thread_role_channel_id", default=THREAD_ROLE_CHANNEL_ID),
            THREAD_ROLE_CHANNEL_ID,
        )
        thread_role_id = _safe_int(
            _cfg_get(cfg, "roles", "thread_role_id", default=THREAD_ROLE_ID),
            THREAD_ROLE_ID,
        )

        if getattr(message.channel, "id", None) == thread_ch_id:
            role = message.guild.get_role(thread_role_id) if thread_role_id else None
            member = message.author
            if role and isinstance(member, discord.Member) and role not in member.roles:
                try:
                    await member.add_roles(role, reason="thread activity auto-assign")
                except discord.Forbidden:
                    logging.warning("[role] lacking permissions to add role")
                except Exception as e:
                    logging.exception(f"[role] add_roles error: {e}")

        if not await aseason_xp_enabled():
            return

        uid = str(message.author.id)
        now_ts = time.time()
        level_changed = False
        quest_completed_now = False
        reward_xp = 0
        pct_int = 0
        final_level = 1

        async with get_user_state_lock(uid):
            user_data = await aget_user_exp(uid)
            prev_level = calculate_level(user_data.get("exp", 0))
            last_text_xp_at = float(user_data.get("last_text_xp_at", 0) or 0)

            if now_ts - last_text_xp_at >= COOLDOWN_SECONDS:
                user_data["exp"] = max(0, _safe_int(user_data.get("exp", 0), 0)) + random.randint(1, 30)
                user_data["last_text_xp_at"] = now_ts
            user_data["last_activity"] = now_ts

            today = datetime.now(KST).strftime("%Y-%m-%d")
            user_m = await aget_user_mission(uid, today)
            if not isinstance(user_m, dict) or user_m.get("date") != today:
                user_m = {
                    "date": today,
                    "text": {"count": 0, "completed": False},
                    "repeat_vc": {"minutes": 0},
                }
            if not isinstance(user_m.get("text"), dict):
                user_m["text"] = {"count": 0, "completed": False}
            if not isinstance(user_m.get("repeat_vc"), dict):
                user_m["repeat_vc"] = {"minutes": 0}

            if not bool(user_m["text"].get("completed")):
                user_m["text"]["count"] = max(0, _safe_int(user_m["text"].get("count", 0), 0)) + 1
                if user_m["text"]["count"] >= MISSION_REQUIRED_MESSAGES:
                    reward_xp = max(10, min(int(round(SEASON_XP_PER_LEVEL * 0.01)), 5000))
                    user_data["exp"] += reward_xp
                    user_m["text"]["completed"] = True
                    quest_completed_now = True

            final_level = calculate_level(user_data.get("exp", 0))
            user_data["level"] = final_level
            level_changed = final_level != prev_level
            pct_int = get_level_progress_percent(user_data.get("exp", 0))

            await afirebase_root_update_strict({
                f"mission_data/{uid}": user_m,
                f"exp_data/{uid}": user_data,
            })

        if level_changed:
            await update_role_and_nick(message.author, final_level)
            announce = await get_channel_from_cfg(
                message.guild, cfg, "levelup_channel_id", LEVELUP_ANNOUNCE_CHANNEL
            )
            if announce:
                try:
                    await announce.send(
                        f"🎉 {message.author.display_name} 님이 시즌 Lv.{final_level} 에 도달했습니다! 🎊",
                        allowed_mentions=ALLOW_NO_PING,
                    )
                except Exception:
                    pass

        if quest_completed_now:
            log_ch = await get_channel_from_cfg(message.guild, cfg, "log_channel_id", LOG_CHANNEL_ID)
            if log_ch:
                await log_ch.send(
                    f"[🧾 로그] {message.author.display_name} 님 텍스트 일일 퀘스트 완료! "
                    f"+{reward_xp}XP (1%)",
                    allowed_mentions=ALLOW_NO_PING,
                )
            try:
                buf = await asyncio.wait_for(
                    asyncio.to_thread(
                        render_daily_quest_banner,
                        display_name=message.author.display_name,
                        pct_int=pct_int,
                        height=40,
                        reward_pct=1,
                    ),
                    timeout=6,
                )
                await message.channel.send(file=discord.File(fp=buf, filename="daily_quest.png"))
            except Exception:
                await message.channel.send(
                    f"🎯 {message.author.mention} 일일 퀘스트 완료! "
                    f"경험치 1% 지급 (현재 {pct_int}%)"
                )

        if final_level >= SEASON_MAX_LEVEL:
            await maybe_award_level100(message.author, final_level, reason="text_activity")
    except Exception as e:
        logging.exception(f"[on_message] processing error: {e}")

# ---- 기타 슬래시 커맨드 핸들러 (/정보, /퀘스트, /랭킹, /출석, /출석랭킹) ----

# 건의함 기능 설정
SUGGEST_ANON_CHANNEL_ID = 1410186330083954689  # 익명 건의함 채널 ID
SUGGEST_REAL_CHANNEL_ID = 1410186411310710847  # 실명 건의함 채널 ID
OWNER_ID = 792661958549045249                  # 서버 오너(본인) ID

from discord import Embed

# =========================
# /설정 commands (admin only)
# =========================

@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.guild_only()
@bot.tree.command(name="설정", description="서버별 봇 설정을 변경/조회합니다.")
@app_commands.describe(
    작업="view/set_channel/set_role/add_afk/remove_afk/toggle_season/set_season_map",
    종류="설정 종류(예: log, levelup, inactive_log, suggest_anon, suggest_real, thread_role_channel, thread_role)",
    채널="지정할 채널(해당 시)",
    역할="지정할 역할(해당 시)",
    계절="봄/여름/가을/겨울",
    음성채널="시즌 음성 채널",
    onoff="true/false"
)
@app_commands.choices(
    작업=[
        app_commands.Choice(name="보기", value="view"),
        app_commands.Choice(name="채널지정", value="set_channel"),
        app_commands.Choice(name="역할지정", value="set_role"),
        app_commands.Choice(name="AFK채널추가", value="add_afk"),
        app_commands.Choice(name="AFK채널제거", value="remove_afk"),
        app_commands.Choice(name="시즌기능ONOFF", value="toggle_season"),
        app_commands.Choice(name="시즌매핑지정", value="set_season_map"),
    ]
)
async def config_cmd(
    interaction: discord.Interaction,
    작업: str,
    종류: str = None,
    채널: discord.TextChannel | discord.VoiceChannel | discord.StageChannel = None,
    역할: discord.Role = None,
    계절: str = None,
    음성채널: discord.VoiceChannel | discord.StageChannel = None,
    onoff: str = None,
):
    if not interaction.guild:
        return await interaction.response.send_message("DM에서는 사용할 수 없습니다.", ephemeral=True)

    gid = interaction.guild.id

    # 1) 보기
    if 작업 == "view":
        cfg = await aget_guild_config(gid)

        def fmt_id(v):
            return str(v) if v else "미설정"

        channels = cfg.get("channels", {})
        roles = cfg.get("roles", {})
        voice = cfg.get("voice", {})
        features = cfg.get("features", {})
        season_map = cfg.get("season_map", {})

        embed = discord.Embed(title="⚙️ 서버 설정", color=discord.Color.blurple())
        embed.add_field(name="채널", value=(
            f"퀘스트 로그 채널 지정: {fmt_id(channels.get('log_channel_id'))}\n"
            f"레벨업 공지 채널 지정: {fmt_id(channels.get('levelup_channel_id'))}\n"
            f"미접속 로그 채널 지정: {fmt_id(channels.get('inactive_log_channel_id'))}\n"
            f"건의함(익명) 채널 지정: {fmt_id(channels.get('suggest_anon_channel_id'))}\n"
            f"건의함(실명) 채널 지정: {fmt_id(channels.get('suggest_real_channel_id'))}\n"
            f"입장 첫 역할 채널 지정: {fmt_id(channels.get('thread_role_channel_id'))}"
        ), inline=False)

        embed.add_field(name="역할", value=(
            f"첫 채팅 시 자동부여 역할: {fmt_id(roles.get('thread_role_id'))}"
        ), inline=False)

        embed.add_field(name="음성", value=(
            f"잠수 채널 지정: {voice.get('afk_channel_ids', [])}\n"
            f"특수 채널 지정: {voice.get('special_vc_category_ids', [])}"
        ), inline=False)

        embed.add_field(name="기능", value=(
            f"역할 별 인원 표시방 지정: {features.get('season_voice_enabled', True)}"
        ), inline=False)

        # 시즌 매핑은 길어질 수 있으니 간단히
        sm_lines = []
        for k in ["봄", "여름", "가을", "겨울"]:
            v = season_map.get(k) or {}
            sm_lines.append(f"{k}: role={v.get('role_id','미설정')}, channel={v.get('channel_id','미설정')}")
        embed.add_field(name="시즌 매핑", value="\n".join(sm_lines), inline=False)

        return await interaction.response.send_message(embed=embed, ephemeral=True)

    # 2) 채널 지정
    if 작업 == "set_channel":
        if not 종류 or not 채널:
            return await interaction.response.send_message("종류와 채널을 지정하세요.", ephemeral=True)
        if not isinstance(채널, discord.TextChannel):
            return await interaction.response.send_message(
                "❌ 해당 설정에는 텍스트 채널만 지정할 수 있습니다.",
                ephemeral=True,
            )

        key_map = {
            "log": "log_channel_id",
            "levelup": "levelup_channel_id",
            "inactive_log": "inactive_log_channel_id",
            "suggest_anon": "suggest_anon_channel_id",
            "suggest_real": "suggest_real_channel_id",
            "thread_role_channel": "thread_role_channel_id",
        }
        if 종류 not in key_map:
            return await interaction.response.send_message(f"알 수 없는 종류: {종류}", ephemeral=True)

        await aset_guild_config_field(gid, f"channels/{key_map[종류]}", int(채널.id))
        return await interaction.response.send_message(f"✅ 채널 설정 완료: {종류} = {채널.mention}", ephemeral=True)

    # 3) 역할 지정
    if 작업 == "set_role":
        if not 종류 or not 역할:
            return await interaction.response.send_message("종류와 역할을 지정하세요.", ephemeral=True)

        key_map = {
            "thread_role": "thread_role_id",
        }
        if 종류 not in key_map:
            return await interaction.response.send_message(f"알 수 없는 종류: {종류}", ephemeral=True)

        await aset_guild_config_field(gid, f"roles/{key_map[종류]}", int(역할.id))
        return await interaction.response.send_message(f"✅ 역할 설정 완료: {종류} = {역할.name}", ephemeral=True)

    # 4) AFK 채널 추가/제거
    if 작업 in ("add_afk", "remove_afk"):
        if not 채널:
            return await interaction.response.send_message("AFK로 지정할 음성/스테이지 채널을 선택하세요.", ephemeral=True)

        cfg = await aget_guild_config(gid)
        lst = cfg.get("voice", {}).get("afk_channel_ids", []) or []
        lst = [int(x) for x in lst if str(x).isdigit()]

        cid = int(채널.id)
        if 작업 == "add_afk" and cid not in lst:
            lst.append(cid)
        if 작업 == "remove_afk" and cid in lst:
            lst.remove(cid)

        await aset_guild_config_field(gid, "voice/afk_channel_ids", lst)
        return await interaction.response.send_message(f"✅ AFK 목록 업데이트: {lst}", ephemeral=True)

    # 5) 시즌 기능 ON/OFF
    if 작업 == "toggle_season":
        if onoff not in ("true", "false"):
            return await interaction.response.send_message("onoff는 true/false 중 하나여야 합니다.", ephemeral=True)
        val = (onoff == "true")
        await aset_guild_config_field(gid, "features/season_voice_enabled", val)
        return await interaction.response.send_message(f"✅ 시즌 음성 기능: {val}", ephemeral=True)

    # 6) 시즌 매핑 지정
    if 작업 == "set_season_map":
        if 계절 not in ("봄", "여름", "가을", "겨울") or not 역할 or not 음성채널:
            return await interaction.response.send_message("계절(봄/여름/가을/겨울), 역할, 음성채널을 모두 지정하세요.", ephemeral=True)

        await aset_guild_config_field(gid, f"season_map/{계절}", {"role_id": int(역할.id), "channel_id": int(음성채널.id)})
        return await interaction.response.send_message(f"✅ 시즌 매핑 설정: {계절}", ephemeral=True)

    return await interaction.response.send_message("알 수 없는 작업입니다.", ephemeral=True)



@app_commands.guild_only()
@bot.tree.command(name="건의함", description="건의사항을 관리자에게 전달합니다.")
@app_commands.describe(
    모드="익명 또는 실명 중 선택하세요.",
    내용="보낼 건의 내용을 작성하세요."
)
@app_commands.choices(
    모드=[
        app_commands.Choice(name="익명", value="익명"),
        app_commands.Choice(name="실명", value="실명"),
    ]
)
async def suggest(interaction: discord.Interaction, 모드: str, 내용: str):
    if len(내용) > 1000:
        return await interaction.response.send_message(
            "❌ 건의 내용은 **1000자 이내**로 작성해주세요.",
            ephemeral=True,
        )

    await interaction.response.defer(ephemeral=True)
    cfg = await aget_guild_config(interaction.guild.id)
    anon_ch = await get_channel_from_cfg(
        interaction.guild, cfg, "suggest_anon_channel_id", SUGGEST_ANON_CHANNEL_ID
    )
    real_ch = await get_channel_from_cfg(
        interaction.guild, cfg, "suggest_real_channel_id", SUGGEST_REAL_CHANNEL_ID
    )
    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M")

    if 모드 == "익명":
        if not anon_ch or not hasattr(anon_ch, "send"):
            return await interaction.followup.send(
                "❌ 익명 건의함 채널을 찾을 수 없습니다. 관리자에게 알려주세요.",
                ephemeral=True,
            )
        embed = Embed(
            title=f"📢 익명 건의 ({now_str})",
            description=f"알 수 없는 서버원 님이 아래와 같이 건의하셨습니다:\n\n{내용}",
            color=0x95A5A6,
        )
        if anon_ch:
            await anon_ch.send(embed=embed, allowed_mentions=ALLOW_NO_PING)

        owner = interaction.guild.get_member(OWNER_ID) or bot.get_user(OWNER_ID)
        if owner:
            user = await aget_user_exp(str(interaction.user.id))
            last_ts = user.get("last_activity")
            if last_ts:
                last_dt = datetime.fromtimestamp(last_ts, KST)
                days_ago = (datetime.now(KST) - last_dt).days
                last_seen = f"{days_ago}일 전 ({last_dt.strftime('%Y.%m.%d %H:%M')})"
            else:
                last_seen = "기록 없음"

            dm_embed = Embed(title=f"📢 익명 건의 (내부 기록) [{now_str}]", color=0xE74C3C)
            dm_embed.add_field(name="서버 닉네임", value=interaction.user.display_name, inline=False)
            dm_embed.add_field(name="계정 닉네임", value=str(interaction.user), inline=False)
            joined_at = getattr(interaction.user, "joined_at", None)
            dm_embed.add_field(
                name="서버 입장일",
                value=joined_at.strftime("%Y-%m-%d %H:%M") if joined_at else "확인 불가",
                inline=False,
            )
            dm_embed.add_field(name="최근 활동", value=last_seen, inline=False)
            dm_embed.add_field(name="건의 내용", value=내용, inline=False)
            try:
                await owner.send(embed=dm_embed)
            except Exception:
                pass

    elif 모드 == "실명":
        if not real_ch or not hasattr(real_ch, "send"):
            return await interaction.followup.send(
                "❌ 실명 건의함 채널을 찾을 수 없습니다. 관리자에게 알려주세요.",
                ephemeral=True,
            )
        embed = Embed(
            title=f"📢 실명 건의 ({now_str})",
            description=f"서버원 {interaction.user.display_name} 님이 아래와 같이 건의하셨습니다:\n\n{내용}",
            color=0x2ECC71,
        )
        if real_ch:
            await real_ch.send(embed=embed, allowed_mentions=ALLOW_NO_PING)
    else:
        return await interaction.followup.send("❌ 모드 값이 올바르지 않습니다.", ephemeral=True)

    await interaction.followup.send("✅ 건의가 정상적으로 전달되었습니다.", ephemeral=True)

@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.guild_only()
@bot.tree.command(name="정보분석", description="서버원의 경험치 및 마지막 활동일 분석")
@app_commands.describe(member="분석할 서버원")
async def analyze_info(interaction: discord.Interaction, member: discord.Member):
    uid = str(member.id)
    user = await aget_user_exp(uid)
    exp = max(0, _safe_int(user.get("exp", 0), 0))
    state = await aget_effective_season_state()
    if state.get("first_season_started"):
        level = calculate_level(exp)
        system_label = "시즌패스"
    else:
        level = calculate_legacy_level_from_exp(exp)
        system_label = "기존 레벨"

    last_ts = user.get("last_activity")
    if last_ts:
        last_dt = datetime.fromtimestamp(last_ts, KST)
        days_ago = (datetime.now(KST) - last_dt).days
        last_seen = last_dt.strftime("%Y. %m. %d %H:%M")
    else:
        last_seen = "기록 없음"
        days_ago = "-"

    embed = discord.Embed(
        title=f"📊 {member.display_name}님의 활동 분석",
        color=discord.Color.orange(),
    )
    embed.add_field(name=system_label, value=f"Lv. {level} ({exp:,} XP)", inline=False)
    embed.add_field(name="마지막 활동 시각", value=last_seen, inline=False)
    embed.add_field(
        name="경과일",
        value=f"{days_ago}일 경과" if isinstance(days_ago, int) else days_ago,
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.guild_only()
@bot.tree.command(name="경험치지급", description="유저에게 경험치를 지급합니다.")
async def grant_xp(interaction: discord.Interaction, member: discord.Member, amount: int):
    if amount <= 0:
        return await interaction.response.send_message("❌ 지급 경험치는 1 이상이어야 합니다.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    if not await aseason_xp_enabled():
        return await interaction.followup.send(
            "현재는 프리시즌/시즌 잠금 상태라 경험치를 지급할 수 없습니다.",
            ephemeral=True,
        )

    uid = str(member.id)
    async with get_user_state_lock(uid):
        user_data = await aget_user_exp(uid)
        prev_level = calculate_level(user_data.get("exp", 0))
        user_data["exp"] = max(0, _safe_int(user_data.get("exp", 0), 0)) + amount
        new_level = calculate_level(user_data["exp"])
        user_data["level"] = new_level
        await asave_user_exp(uid, user_data)

    if new_level > prev_level:
        await update_role_and_nick(member, new_level)
        cfg = await aget_guild_config(interaction.guild.id)
        announce = await get_channel_from_cfg(
            interaction.guild, cfg, "levelup_channel_id", LEVELUP_ANNOUNCE_CHANNEL
        )
        if announce:
            await announce.send(
                f"🎉 {member.display_name} 님이 시즌 Lv.{new_level} 에 도달했습니다! 🎊",
                allowed_mentions=ALLOW_NO_PING,
            )

    if new_level >= SEASON_MAX_LEVEL:
        await maybe_award_level100(member, new_level, reason="admin_grant")

    await interaction.followup.send(
        f"✅ {member.mention}에게 경험치 {amount}XP 지급 완료!",
        ephemeral=True,
    )

@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.guild_only()
@bot.tree.command(name="인원채널_생성", description="역할 인원수를 표시하는 음성채널 생성")
async def create_count_channel(
    interaction: discord.Interaction,
    역할: discord.Role,
    제목: str = "임시 제목"
):
    guild = interaction.guild
    if not guild:
        return

    ch = await guild.create_voice_channel(f"{제목} : 0명")

    cfg = await aget_guild_config(guild.id)
    items = cfg.get("voice_count_channels", [])
    if not isinstance(items, list):
        items = []
    items.append({"role_id": 역할.id, "channel_id": ch.id})

    await aset_guild_config_field(guild.id, "voice_count_channels", items)

    await interaction.response.send_message(
        f"완료: {ch.mention}\n채널명 끝의 `n명`만 자동 갱신됩니다.",
        ephemeral=True
    )


@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.guild_only()
@bot.tree.command(name="경험치차감", description="유저의 경험치를 차감합니다.")
async def deduct_xp(interaction: discord.Interaction, member: discord.Member, amount: int):
    if amount <= 0:
        return await interaction.response.send_message("❌ 차감 경험치는 1 이상이어야 합니다.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    uid = str(member.id)
    async with get_user_state_lock(uid):
        user_data = await aget_user_exp(uid)
        user_data["exp"] = max(0, _safe_int(user_data.get("exp", 0), 0) - amount)
        user_data["level"] = calculate_level(user_data["exp"])
        await asave_user_exp(uid, user_data)

    await update_role_and_nick(member, user_data["level"])
    await interaction.followup.send(
        f"✅ {member.mention}에게서 경험치 {amount}XP 차감 완료!",
        ephemeral=True,
    )
# ---- 기타 슬래시 커맨드 핸들러 (/정보, /퀘스트, /랭킹, /출석, /출석랭킹) ----
                                            
@app_commands.guild_only()
@bot.tree.command(name="정보", description="내 정보를 이미지 카드로 확인합니다")
async def info(interaction: discord.Interaction):
    # defer부터 안전하게
    try:
        await interaction.response.defer()
    except discord.NotFound:
        # 10062 Unknown interaction: 이미 만료됨
        return
    except Exception:
        # defer 실패는 일단 종료
        return

    try:
        logging.info("[/정보] start")

        user = interaction.user
        uid = str(user.id)
        state = await aget_effective_season_state()
        if not state.get("first_season_started"):
            await interaction.followup.send(
                "현재 시즌패스 준비 중입니다. 첫 시즌 시작 후 `/정보`를 이용해주세요."
            )
            return

        logging.info("[/정보] load user exp")
        exp_data = await aget_user_exp(uid)

        total_xp = int(exp_data.get("exp", 0))
        level, cur_xp, need_xp, pct = get_level_progress(total_xp)
        
        if exp_data.get("level") != level:
            await aupdate_user_exp_fields(uid, {"level": level})

        logging.info("[/정보] fetch avatar")
        avatar_bytes = None
        try:
            avatar_url = user.display_avatar.replace(size=256).url
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(avatar_url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                    logging.info(f"[/정보] avatar resp={resp.status}")
                    if resp.status == 200:
                        avatar_bytes = await resp.read()
        except Exception:
            logging.exception("[/정보] avatar fetch failed")
            avatar_bytes = None

        logging.info("[/정보] render image")
        buf = await asyncio.wait_for(
            asyncio.to_thread(
                render_rank_card,
                display_name=strip_title_suffix(user.display_name),
                level=level,
                total_xp=total_xp,
                cur_xp=cur_xp,
                need_xp=need_xp,
                pct=pct,
                avatar_bytes=avatar_bytes,
            ),
            timeout=8,
        )

        logging.info("[/정보] send file")
        await interaction.followup.send(file=discord.File(fp=buf, filename="rank.png"))
        logging.info("[/정보] done")

    except asyncio.TimeoutError:
        logging.exception("[/정보] timeout")
        try:
            await interaction.followup.send("응답이 지연되어 중단했습니다. (타임아웃)")
        except Exception:
            pass
    except Exception as e:
        logging.exception("[/정보] error")
        try:
            await interaction.followup.send(f"처리 중 오류: {type(e).__name__}")
        except Exception:
            pass


@app_commands.guild_only()
@bot.tree.command(name="퀘스트", description="일일 및 반복 VC 퀘스트 현황을 확인합니다.")
async def quest(interaction: discord.Interaction):
    await interaction.response.defer()
    uid = str(interaction.user.id)
    today = datetime.now(KST).strftime("%Y-%m-%d")
    um = await aget_user_mission(uid, today)
    if not isinstance(um, dict) or um.get("date") != today:
        um = {
            "date": today,
            "text": {"count": 0, "completed": False},
            "repeat_vc": {"minutes": 0},
        }
    if not isinstance(um.get("text"), dict):
        um["text"] = {"count": 0, "completed": False}
    if not isinstance(um.get("repeat_vc"), dict):
        um["repeat_vc"] = {"minutes": 0}

    text_count = max(0, _safe_int(um["text"].get("count", 0), 0))
    text_status = (
        f"진행도: {text_count} / {MISSION_REQUIRED_MESSAGES}\n"
        f"상태: {'✅ 완료' if bool(um['text'].get('completed')) else '❌ 미완료'}"
    )
    vc_minutes = max(0, _safe_int(um["repeat_vc"].get("minutes", 0), 0))
    vc_status = (
        f"누적 참여: {vc_minutes}분\n"
        f"보상 횟수: {vc_minutes // REPEAT_VC_REQUIRED_MINUTES}회 지급"
    )

    attendance = await aget_attendance_user(uid)
    attended = isinstance(attendance, dict) and attendance.get("last_date") == today
    attendance_status = f"상태: {'✅ 출석 완료' if attended else '❌ 출석 안됨'}"

    embed = discord.Embed(title="📜 퀘스트 현황", color=discord.Color.green())
    embed.add_field(name="🗨️ 텍스트 미션", value=text_status, inline=False)
    embed.add_field(name="📞 5인 이상 통화방 참여 미션", value=vc_status, inline=False)
    embed.add_field(name="🗓️ 출석", value=attendance_status, inline=False)
    await interaction.followup.send(embed=embed)

@app_commands.guild_only()
@bot.tree.command(name="랭킹", description="경험치 랭킹을 확인합니다.")
async def ranking(interaction: discord.Interaction):
    await interaction.response.defer()
    state = await aget_effective_season_state()
    if not state.get("first_season_started"):
        return await interaction.followup.send(
            "현재 시즌패스 준비 중입니다. 첫 시즌 시작 후 랭킹이 공개됩니다."
        )

    data = await aload_exp_data()
    if not isinstance(data, dict):
        data = {}

    current_member_ids = {
        str(member.id) for member in interaction.guild.members if not member.bot
    }
    normalized = [
        (str(uid), user_data)
        for uid, user_data in data.items()
        if str(uid) in current_member_ids and isinstance(user_data, dict)
    ]
    sorted_users = sorted(
        normalized,
        key=lambda item: _safe_int(item[1].get("exp", 0), 0),
        reverse=True,
    )

    desc_lines = []
    for idx, (uid, user_data) in enumerate(sorted_users[:10], start=1):
        member = interaction.guild.get_member(int(uid)) if uid.isdigit() else None
        if member is None and uid.isdigit():
            try:
                member = await interaction.guild.fetch_member(int(uid))
            except Exception:
                member = None
        name = member.display_name if member else "Unknown"
        exp = max(0, _safe_int(user_data.get("exp", 0), 0))
        desc_lines.append(
            f"{idx}위. {strip_title_suffix(name)} - Lv. {calculate_level(exp)} ({exp:,} XP)"
        )

    my_rank = None
    me = str(interaction.user.id)
    for idx, (uid, user_data) in enumerate(sorted_users, start=1):
        if uid == me:
            my_exp = max(0, _safe_int(user_data.get("exp", 0), 0))
            my_rank = (
                f"당신의 순위: {idx}위 - 시즌패스 "
                f"Lv. {calculate_level(my_exp)} ({my_exp:,} XP)"
            )
            break

    embed = discord.Embed(
        title=f"🏆 시즌패스 랭킹 - {state.get('current_season_name', CURRENT_SEASON_NAME)}",
        description="\n".join(desc_lines) if desc_lines else "랭킹 데이터가 없습니다.",
        color=discord.Color.gold(),
    )
    if my_rank:
        embed.add_field(name="📍 내 순위", value=my_rank, inline=False)
    await interaction.followup.send(embed=embed)

@app_commands.guild_only()
@bot.tree.command(name="출석", description="오늘의 출석을 기록합니다.")
async def attend(interaction: discord.Interaction):
    await interaction.response.defer()
    uid = str(interaction.user.id)
    now = datetime.now(KST)
    today_str = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    week = get_week_key_kst(now)
    month = get_month_key_kst(now)

    gain = ATTENDANCE_EXP_REWARD if await aseason_xp_enabled() else 0
    level_up = False
    final_level = 1

    async with get_user_state_lock(uid):
        ud = normalize_attendance_record(await aget_attendance_user(uid))
        prev_last = ud.get("last_date", "")

        if prev_last == today_str:
            h, m = _until_next_attendance(now)
            headline = random.choice(ATTEND_MSG_ALREADY).format(mention=interaction.user.mention)
            msg = "\n".join([
                headline,
                _build_attendance_stats_line(ud["total_days"], ud["streak"]),
                f"다음 출석까지 {h}시간 {m}분",
            ])
            return await interaction.followup.send(msg)

        natural_continue = prev_last == yesterday
        is_first = prev_last == ""
        prev_streak = _safe_int(ud.get("streak", 0), 0)
        if is_first:
            new_streak = 1
            headline = random.choice(ATTEND_MSG_FIRST).format(mention=interaction.user.mention)
        elif natural_continue:
            new_streak = prev_streak + 1
            headline = random.choice(ATTEND_MSG_SUCCESS).format(mention=interaction.user.mention)
        else:
            new_streak = 1
            headline = random.choice(ATTEND_MSG_RESET).format(mention=interaction.user.mention)

        ud["streak"] = new_streak
        ud["last_date"] = today_str
        ud["total_days"] = _safe_int(ud.get("total_days", 0), 0) + 1
        ud.setdefault("weekly", {})[week] = _safe_int(ud["weekly"].get(week, 0), 0) + 1
        ud.setdefault("monthly", {})[month] = _safe_int(ud["monthly"].get(month, 0), 0) + 1

        ue = await aget_user_exp(uid)
        prev_level = calculate_level(ue.get("exp", 0))
        final_level = prev_level
        attendance_updates: dict[str, object] = {
            f"{ATTENDANCE_DB_KEY}/{uid}": ud,
        }
        if gain > 0:
            ue["exp"] = max(0, _safe_int(ue.get("exp", 0), 0)) + gain
            final_level = calculate_level(ue["exp"])
            ue["level"] = final_level
            ue["last_activity"] = time.time()
            attendance_updates[f"exp_data/{uid}"] = ue
        else:
            # 프리시즌에는 EXP 전체 레코드를 덮어쓰지 않고 활동 시각만 갱신합니다.
            attendance_updates[f"exp_data/{uid}/last_activity"] = time.time()

        await afirebase_root_update_strict(attendance_updates)
        level_up = final_level > prev_level

    if level_up:
        cfg = await aget_guild_config(interaction.guild.id)
        announce = await get_channel_from_cfg(
            interaction.guild, cfg, "levelup_channel_id", LEVELUP_ANNOUNCE_CHANNEL
        )
        if announce:
            try:
                await announce.send(
                    f"🎉 {interaction.user.display_name} 님이 시즌 Lv.{final_level} 에 도달했습니다! 🎊",
                    allowed_mentions=ALLOW_NO_PING,
                )
            except Exception:
                pass

    if gain > 0 and final_level >= SEASON_MAX_LEVEL:
        await maybe_award_level100(interaction.user, final_level, reason="attendance")

    await update_role_and_nick(interaction.user, final_level)
    lines = [headline]
    if new_streak in ATTEND_MILESTONE_STREAKS:
        lines.append(
            random.choice(ATTEND_MSG_MILESTONE).format(
                mention=interaction.user.mention,
                streak=new_streak,
            )
        )
    lines.append(_build_attendance_stats_line(ud["total_days"], ud["streak"], gain))
    await interaction.followup.send("\n".join(lines))

@app_commands.guild_only()
@bot.tree.command(name="출석랭킹", description="출석 랭킹을 확인합니다.")
async def attend_ranking(interaction: discord.Interaction):
    await interaction.response.defer()
    data = await aget_attendance_data()
    if not isinstance(data, dict):
        data = {}
    current_member_ids = {
        str(member.id) for member in interaction.guild.members if not member.bot
    }
    ranked = sorted(
        [
            (str(uid), ud)
            for uid, ud in data.items()
            if str(uid) in current_member_ids and isinstance(ud, dict)
        ],
        key=lambda item: (
            -_safe_int(item[1].get("total_days", 0), 0),
            -_safe_int(item[1].get("streak", 0), 0),
        ),
    )

    lines = []
    for idx, (uid, ud) in enumerate(ranked[:10], start=1):
        member = interaction.guild.get_member(int(uid)) if uid.isdigit() else None
        if member is None and uid.isdigit():
            try:
                member = await interaction.guild.fetch_member(int(uid))
            except Exception:
                member = None
        name = member.display_name if member else "Unknown"
        lines.append(
            f"{idx}위. {strip_title_suffix(name)} - "
            f"누적 {_safe_int(ud.get('total_days', 0), 0)}일 / "
            f"연속 {_safe_int(ud.get('streak', 0), 0)}일"
        )

    my_rank = None
    for idx, (uid, _) in enumerate(ranked, start=1):
        if uid == str(interaction.user.id):
            my_rank = f"당신의 순위: {idx}위"
            break

    embed = discord.Embed(
        title="🏅 출석 랭킹",
        description="\n".join(lines) if lines else "출석 데이터가 없습니다.",
        color=discord.Color.blue(),
    )
    if my_rank:
        embed.add_field(name="📍 내 순위", value=my_rank, inline=False)
    await interaction.followup.send(embed=embed)
    
# =========================
# Attendance Admin Commands
# =========================

@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.guild_only()
@bot.tree.command(name="출석수정", description="유저의 누적/연속 출석을 수정합니다.")
@app_commands.describe(
    member="대상 유저",
    total_days="누적 출석일(0 이상)",
    streak="연속 출석일(0 이상)",
    last_date="마지막 출석일(선택, YYYY-MM-DD)"
)
async def attendance_edit(
    interaction: discord.Interaction,
    member: discord.Member,
    total_days: int,
    streak: int,
    last_date: str | None = None,
):
    uid = str(member.id)
    async with get_user_state_lock(uid):
        ud = normalize_attendance_record(await aget_attendance_user(uid))
        ud["total_days"] = max(0, _safe_int(total_days, 0))
        ud["streak"] = max(0, _safe_int(streak, 0))

        if last_date is not None:
            ld = last_date.strip()
            if ld == "":
                ud["last_date"] = ""
            else:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", ld):
                    return await interaction.response.send_message(
                        "❌ last_date 형식이 올바르지 않습니다. 예: 2026-02-24",
                        ephemeral=True,
                    )
                try:
                    datetime.strptime(ld, "%Y-%m-%d")
                except Exception:
                    return await interaction.response.send_message(
                        "❌ last_date 값이 유효한 날짜가 아닙니다.",
                        ephemeral=True,
                    )
                ud["last_date"] = ld

        await aset_attendance_user(uid, ud)

    await interaction.response.send_message(
        f"✅ {member.mention} 출석 수정 완료\n"
        f"- 누적: {ud['total_days']}일\n"
        f"- 연속: {ud['streak']}일\n"
        f"- 마지막 출석일: {ud.get('last_date', '') or '(없음)'}",
        ephemeral=True,
    )


# =========================
# Season Pass Commands
# =========================

async def _get_season_reward(season_id: str) -> dict:
    return await asyncio.to_thread(lambda: _season_rewards_ref(season_id).get() or {})


async def _set_season_reward(season_id: str, data: dict):
    await asyncio.to_thread(lambda: _season_rewards_ref(season_id).set(data))


async def _update_season_state(data: dict):
    """시즌 상태 중 지정된 필드만 부분 갱신합니다."""
    if not isinstance(data, dict) or not data:
        return
    await asyncio.to_thread(lambda: _season_state_ref().update(data))


async def ensure_guild_member_cache_complete(guild: discord.Guild) -> tuple[bool, str]:
    """첫 시즌 전환 전에 서버 멤버 캐시가 완전한지 확인합니다."""
    if not bot.intents.members:
        return False, "봇 코드의 Server Members Intent가 비활성화되어 있습니다."

    expected = guild.member_count
    cached = len(guild.members)
    if guild.chunked and (expected is None or cached >= expected):
        return True, ""

    try:
        await asyncio.wait_for(guild.chunk(cache=True), timeout=60)
    except asyncio.TimeoutError:
        return False, "서버원 목록 불러오기가 60초 안에 완료되지 않았습니다."
    except discord.PrivilegedIntentsRequired:
        return False, "Discord 개발자 포털에서 서버 멤버 인텐트를 활성화해야 합니다."
    except Exception as e:
        logging.exception("[first-season] guild member chunk failed")
        return False, f"서버원 목록을 불러오지 못했습니다: {type(e).__name__}"

    expected = guild.member_count
    cached = len(guild.members)
    if not guild.chunked:
        return False, f"서버원 캐시가 완성되지 않았습니다. 캐시 {cached}명 / 서버 표시 {expected or '확인 불가'}명"
    if expected is not None and cached < expected:
        return False, f"서버원 캐시 인원이 부족합니다. 캐시 {cached}명 / 서버 표시 {expected}명"
    return True, ""


def first_season_preflight_errors(guild: discord.Guild) -> list[str]:
    """첫 시즌 전환 전에 Discord 권한과 공지 채널을 점검합니다."""
    errors: list[str] = []
    me = guild.me
    if me is None:
        return ["서버에서 봇 멤버 정보를 확인할 수 없습니다."]
    if not bot.intents.members:
        errors.append("봇 코드의 `서버 멤버 인텐트`가 비활성화되어 있습니다.")

    if not me.guild_permissions.manage_roles:
        errors.append("봇에 `역할 관리` 권한이 없습니다.")
    if not me.guild_permissions.manage_nicknames:
        errors.append("봇에 `닉네임 관리` 권한이 없습니다.")

    notice = guild.get_channel(SEASON_NOTICE_CHANNEL_ID)
    if notice is None or not hasattr(notice, "send"):
        errors.append(f"시즌 공지 채널 `{SEASON_NOTICE_CHANNEL_ID}`을 찾을 수 없습니다.")
    else:
        try:
            perms = notice.permissions_for(me)
            if not perms.view_channel:
                errors.append("봇이 시즌 공지 채널을 볼 수 없습니다.")
            if not perms.send_messages:
                errors.append("봇이 시즌 공지 채널에 메시지를 보낼 수 없습니다.")
            if not perms.embed_links:
                errors.append("봇이 시즌 공지 채널에 임베드를 보낼 수 없습니다.")
        except Exception:
            errors.append("시즌 공지 채널 권한을 확인하지 못했습니다.")

    for role_id in LEGACY_LEVEL_ROLE_IDS:
        role = guild.get_role(role_id)
        if role and role >= me.top_role:
            errors.append(f"기존 레벨 역할 `{role.name}`이 봇 역할보다 높거나 같습니다.")

    return errors


def build_standard_season_start_embed(season_name: str, cal: dict, reward: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"🌿 {season_name} 시즌 시작",
        description=(
            f"`{season_name}` 시즌이 시작되었습니다.\n\n"
            "시즌패스 경험치 획득이 다시 활성화됩니다.\n"
            f"진행도 칭호를 착용 중인 서버원은 `[ Lv. 1 : {season_name} ]` 형식으로 갱신됩니다."
        ),
        color=discord.Color.green(),
    )
    embed.add_field(
        name="시즌 기간",
        value=(
            f"정규 시즌: {cal['regular_start']} ~ {cal['regular_end']}\n"
            f"프리시즌: {cal['preseason_start']} ~ {cal['preseason_end']}"
        ),
        inline=False,
    )
    embed.add_field(
        name="진행 기준",
        value=(
            f"최대 레벨: Lv.{SEASON_MAX_LEVEL}\n"
            f"1레벨 필요 경험치: {SEASON_XP_PER_LEVEL:,} XP\n"
            f"Lv.{SEASON_MAX_LEVEL} 필요 경험치: {SEASON_TOTAL_XP_TO_MAX:,} XP"
        ),
        inline=False,
    )
    embed.add_field(
        name="이번 시즌 Lv.100 보상",
        value=f"[ {reward.get('title_name', '아직 설정되지 않음')} ]",
        inline=False,
    )
    return embed


def build_first_season_start_embed(
    season_name: str,
    cal: dict,
    reward: dict,
    extra_notice: str = "",
) -> discord.Embed:
    embed = discord.Embed(
        title=f"🌿 {season_name} 시즌 시작",
        description=(
            f"`{season_name}` 시즌이 시작되었습니다.\n\n"
            "이번 시즌부터 기존 레벨 시스템은 시즌패스 시스템으로 전환됩니다.\n"
            "모든 서버원의 시즌패스 경험치는 `0 XP / Lv.1`부터 시작됩니다.\n\n"
            "기존 레벨은 `[ 잊혀진 기억 Lv. nn ]` 칭호로 보존되며,\n"
            "`/칭호관리` 명령어를 통해 착용할 수 있습니다."
        ),
        color=discord.Color.green(),
    )
    embed.add_field(
        name="시즌 기간",
        value=(
            f"정규 시즌: {cal['regular_start']} ~ {cal['regular_end']}\n"
            f"프리시즌: {cal['preseason_start']} ~ {cal['preseason_end']}"
        ),
        inline=False,
    )
    embed.add_field(
        name="진행 기준",
        value=(
            f"최대 레벨: Lv.{SEASON_MAX_LEVEL}\n"
            f"1레벨 필요 경험치: {SEASON_XP_PER_LEVEL:,} XP\n"
            f"Lv.{SEASON_MAX_LEVEL} 필요 경험치: {SEASON_TOTAL_XP_TO_MAX:,} XP"
        ),
        inline=False,
    )
    embed.add_field(
        name="이번 시즌 Lv.100 보상",
        value=f"[ {reward.get('title_name', '아직 설정되지 않음')} ]",
        inline=False,
    )
    if extra_notice:
        embed.add_field(name="추가 안내", value=extra_notice[:1024], inline=False)
    embed.set_footer(text="첫 시즌은 시즌패스 전환 후 진행되는 첫 운영 시즌입니다.")
    return embed


async def send_season_start_embed(
    guild: discord.Guild,
    *,
    embed: discord.Embed,
    channel_id: int,
) -> tuple[bool, str]:
    notice = guild.get_channel(int(channel_id))
    me = guild.me
    if notice is None or me is None or not hasattr(notice, "send"):
        return False, "notice_channel_unavailable"
    try:
        perms = notice.permissions_for(me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return False, "notice_permission_missing"
        await notice.send(embed=embed, allowed_mentions=ALLOW_NO_PING)
        return True, ""
    except Exception as e:
        logging.warning(f"[season_start] notice send failed: {e!r}")
        return False, repr(e)


async def send_standard_season_start_notice(
    guild: discord.Guild,
    *,
    season_name: str,
    cal: dict,
    reward: dict,
    channel_id: int,
) -> tuple[bool, str]:
    return await send_season_start_embed(
        guild,
        embed=build_standard_season_start_embed(season_name, cal, reward),
        channel_id=channel_id,
    )


async def process_season_start_if_needed(guild: discord.Guild) -> dict:
    """같은 서버의 다른 시즌 작업과 겹치지 않게 자동 시즌 전환을 직렬화합니다."""
    if not guild:
        return {"processed": False, "reason": "no_guild"}
    lock = get_season_operation_lock(guild.id)
    if lock.locked():
        return {"processed": False, "reason": "season_operation_busy"}
    async with lock:
        return await _process_season_start_if_needed_locked(guild)


async def _process_season_start_if_needed_locked(guild: discord.Guild) -> dict:
    """준비된 다음 시즌을 열고, 실패한 시작 공지는 이후 루프에서 재시도합니다."""
    if not guild:
        return {"processed": False, "reason": "no_guild"}

    def _load_state_and_calendar():
        cal = get_calendar_season_info(datetime.now(KST))
        ref = _season_state_ref()
        state = ref.get()
        if not isinstance(state, dict):
            state = _default_season_state_from_calendar(cal)
            ref.set(state)
        return state, cal

    state, cal = await asyncio.to_thread(_load_state_and_calendar)
    if not state.get("first_season_started"):
        await _update_season_state({
            "status": SEASON_STATUS_LOCKED,
            "current_season_id": cal.get("season_id"),
            "current_season_type": cal.get("season_type"),
            "current_season_label": cal.get("season_label"),
            "next_season_id": cal.get("next_season_id"),
        })
        return {"processed": False, "reason": "first_season_not_started"}

    current_id = state.get("current_season_id")
    calendar_id = cal.get("season_id")

    # 현재 시즌 정산 후처리가 중간에 끊겼다면 닉네임과 공지를 복구합니다.
    if current_id == calendar_id and state.get("settlement_postprocess_pending"):
        pending_season_id = state.get("settlement_postprocess_season_id")
        if pending_season_id == calendar_id:
            cache_ok, cache_error = await ensure_guild_member_cache_complete(guild)
            if not cache_ok:
                logging.warning(f"[season-settlement] postprocess recovery delayed: {cache_error}")
                return {"processed": False, "reason": "member_cache_incomplete", "error": cache_error}
            nick_result = await reset_progress_title_members(guild, level=1)
            notice_sent = state.get("settlement_notice_sent_for") == calendar_id
            if not notice_sent:
                notice = guild.get_channel(int(state.get("season_notice_channel_id") or SEASON_NOTICE_CHANNEL_ID))
                if notice and hasattr(notice, "send"):
                    try:
                        await notice.send(
                            f"📢 `{state.get('current_season_name', '현재 시즌')}` 시즌 정산이 완료되었습니다. "
                            "프리시즌 동안 시즌 경험치 획득이 중단됩니다.",
                            allowed_mentions=ALLOW_NO_PING,
                        )
                        notice_sent = True
                    except Exception as e:
                        logging.warning(f"[season-settlement] notice recovery failed: {e!r}")

            await _update_season_state({
                "settlement_postprocess_pending": False,
                "settlement_postprocess_completed_at": datetime.now(KST).isoformat(),
                "settlement_notice_sent_for": calendar_id if notice_sent else "",
                "settlement_notice_pending": not notice_sent,
            })
            return {
                "processed": True,
                "reason": "settlement_postprocess_recovered",
                "nick_updated": nick_result["updated"],
                "nick_failed": nick_result["failed"],
                "notice_sent": notice_sent,
            }

    # 정산 공지만 실패한 경우 후처리를 반복하지 않고 공지만 재전송합니다.
    if current_id == calendar_id and state.get("settlement_notice_pending"):
        notice = guild.get_channel(int(state.get("season_notice_channel_id") or SEASON_NOTICE_CHANNEL_ID))
        if notice and hasattr(notice, "send"):
            try:
                await notice.send(
                    f"📢 `{state.get('current_season_name', '현재 시즌')}` 시즌 정산이 완료되었습니다. "
                    "프리시즌 동안 시즌 경험치 획득이 중단됩니다.",
                    allowed_mentions=ALLOW_NO_PING,
                )
                await _update_season_state({
                    "settlement_notice_pending": False,
                    "settlement_notice_sent_for": calendar_id,
                    "settlement_notice_sent_at": datetime.now(KST).isoformat(),
                })
                return {"processed": True, "reason": "settlement_notice_retried"}
            except Exception as e:
                logging.warning(f"[season-settlement] notice retry failed: {e!r}")

    # 첫 시즌 DB 커밋 후 역할/닉네임 후처리가 중간에 끊겼다면 자동 복구합니다.
    if current_id == calendar_id:
        migration = await aget_legacy_migration_record(calendar_id)
        if (
            migration.get("type") == "first_season_start"
            and migration.get("status") == "db_committed"
        ):
            cache_ok, cache_error = await ensure_guild_member_cache_complete(guild)
            if not cache_ok:
                logging.warning(f"[first-season] postprocess recovery delayed: {cache_error}")
                return {"processed": False, "reason": "member_cache_incomplete", "error": cache_error}

            role_removed_count = role_failed_count = 0
            nick_updated_count = nick_failed_count = 0
            for member in guild.members:
                if member.bot:
                    continue
                removed, failed = await remove_legacy_level_roles(member)
                role_removed_count += removed
                role_failed_count += failed
                if member.id != guild.owner_id:
                    if await apply_member_title(member, 1):
                        nick_updated_count += 1
                    else:
                        nick_failed_count += 1
                await asyncio.sleep(0.15)

            await _update_season_state({
                "season_start_postprocess_pending_for": "",
                "season_start_postprocess_completed_at": datetime.now(KST).isoformat(),
            })

            reward = await _get_season_reward(calendar_id)
            channel_id = int(state.get("season_notice_channel_id") or SEASON_NOTICE_CHANNEL_ID)
            notice_sent = state.get("start_notice_sent_for") == calendar_id
            notice_error = ""
            if not notice_sent:
                notice_sent, notice_error = await send_season_start_embed(
                    guild,
                    embed=build_first_season_start_embed(
                        state.get("current_season_name") or migration.get("season_name") or "첫 시즌",
                        cal,
                        reward,
                        str(migration.get("notice_extra", "")),
                    ),
                    channel_id=channel_id,
                )
                if notice_sent:
                    await _update_season_state({
                        "start_notice_sent_for": calendar_id,
                        "season_start_notice_pending_for": "",
                        "season_start_notice_sent_at": datetime.now(KST).isoformat(),
                        "season_start_notice_last_error": "",
                    })
                else:
                    await _update_season_state({"season_start_notice_last_error": notice_error})

            final_status = "completed" if notice_sent else "completed_notice_failed"
            await aupdate_legacy_migration_record(calendar_id, {
                "status": final_status,
                "phase": "completed",
                "resumed_at": datetime.now(KST).isoformat(),
                "notice_error": notice_error,
                "result": {
                    "legacy_title_count": _safe_int(
                        (migration.get("result") or {}).get("legacy_title_target_count"), 0
                    ),
                    "exp_reset_count": _safe_int(
                        (migration.get("result") or {}).get("exp_reset_target_count"), 0
                    ),
                    "role_removed_count": role_removed_count,
                    "role_failed_count": role_failed_count,
                    "nick_updated_count": nick_updated_count,
                    "nick_failed_count": nick_failed_count,
                    "notice_sent": notice_sent,
                },
            })
            return {
                "processed": True,
                "reason": "first_season_postprocess_recovered",
                "notice_sent": notice_sent,
            }

    # 일반 시즌 개방 후 닉네임 후처리가 중간에 끊겼다면 자동 복구합니다.
    if (
        current_id == calendar_id
        and state.get("season_start_postprocess_pending_for") == calendar_id
    ):
        cache_ok, cache_error = await ensure_guild_member_cache_complete(guild)
        if not cache_ok:
            logging.warning(f"[season-start] nickname recovery delayed: {cache_error}")
            return {"processed": False, "reason": "member_cache_incomplete", "error": cache_error}
        nick_result = await reset_progress_title_members(guild, level=1)
        await _update_season_state({
            "season_start_postprocess_pending_for": "",
            "season_start_postprocess_completed_at": datetime.now(KST).isoformat(),
        })
        state["season_start_postprocess_pending_for"] = ""
        logging.info(
            "[season-start] recovered nickname postprocess season=%s updated=%s failed=%s",
            calendar_id, nick_result["updated"], nick_result["failed"],
        )

    # 이미 시즌 데이터는 열렸지만 공지만 실패한 경우 재전송합니다.
    if current_id == calendar_id:
        pending_for = state.get("season_start_notice_pending_for")
        if pending_for == calendar_id and state.get("start_notice_sent_for") != calendar_id:
            reward = await _get_season_reward(calendar_id)
            season_name = state.get("current_season_name") or DEFAULT_SEASON_NAMES.get(
                cal["season_type"], "새 시즌"
            )
            migration = await aget_legacy_migration_record(calendar_id)
            channel_id = int(state.get("season_notice_channel_id") or SEASON_NOTICE_CHANNEL_ID)
            if migration.get("type") == "first_season_start":
                success, error = await send_season_start_embed(
                    guild,
                    embed=build_first_season_start_embed(
                        season_name,
                        cal,
                        reward,
                        str(migration.get("notice_extra", "")),
                    ),
                    channel_id=channel_id,
                )
            else:
                success, error = await send_standard_season_start_notice(
                    guild,
                    season_name=season_name,
                    cal=cal,
                    reward=reward,
                    channel_id=channel_id,
                )
            if success:
                await _update_season_state({
                    "start_notice_sent_for": calendar_id,
                    "season_start_notice_pending_for": "",
                    "season_start_notice_sent_at": datetime.now(KST).isoformat(),
                    "season_start_notice_last_error": "",
                })
                try:
                    migration = await aget_legacy_migration_record(calendar_id)
                    if migration.get("type") == "first_season_start":
                        await aupdate_legacy_migration_record(calendar_id, {
                            "status": "completed",
                            "notice_retried_at": datetime.now(KST).isoformat(),
                            "notice_error": "",
                        })
                except Exception:
                    pass
                return {"processed": True, "reason": "notice_retried", "season_id": calendar_id}

            await _update_season_state({"season_start_notice_last_error": error})
            return {"processed": False, "reason": "notice_retry_failed", "error": error}
        return {"processed": False, "reason": "same_season"}

    if not state.get("settled") or not state.get("next_ready"):
        await _update_season_state({"status": SEASON_STATUS_LOCKED})
        return {"processed": False, "reason": "not_ready"}
    if state.get("next_season_id") != calendar_id:
        await _update_season_state({"status": SEASON_STATUS_LOCKED})
        return {"processed": False, "reason": "next_season_id_mismatch"}

    next_reward = await _get_season_reward(calendar_id)
    if not next_reward.get("title_name"):
        await _update_season_state({"status": SEASON_STATUS_LOCKED})
        return {"processed": False, "reason": "next_reward_not_configured"}

    notice_channel_id = int(state.get("season_notice_channel_id") or SEASON_NOTICE_CHANNEL_ID)
    notice = guild.get_channel(notice_channel_id)
    me = guild.me
    if notice is None or me is None or not hasattr(notice, "send"):
        await _update_season_state({"status": SEASON_STATUS_LOCKED})
        return {"processed": False, "reason": "notice_channel_unavailable"}
    perms = notice.permissions_for(me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        await _update_season_state({"status": SEASON_STATUS_LOCKED})
        return {"processed": False, "reason": "notice_permission_missing"}

    cache_ok, cache_error = await ensure_guild_member_cache_complete(guild)
    if not cache_ok:
        await _update_season_state({
            "status": SEASON_STATUS_LOCKED,
            "season_start_last_error": cache_error,
        })
        return {"processed": False, "reason": "member_cache_incomplete", "error": cache_error}

    season_name = state.get("next_season_name") or DEFAULT_SEASON_NAMES.get(cal["season_type"], "새 시즌")
    now_iso = datetime.now(KST).isoformat()
    await _update_season_state({
        "current_season_id": calendar_id,
        "current_season_name": season_name,
        "current_season_type": cal["season_type"],
        "current_season_label": cal["season_label"],
        "status": cal["status"],
        "settled": False,
        "next_ready": False,
        "next_season_id": cal["next_season_id"],
        "next_season_name": "",
        "started_at": now_iso,
        "season_start_processed_at": now_iso,
        "season_start_postprocess_pending_for": calendar_id,
        "season_start_notice_pending_for": calendar_id,
        "season_start_notice_last_error": "",
    })

    nick_result = await reset_progress_title_members(guild, level=1)
    await _update_season_state({
        "season_start_postprocess_pending_for": "",
        "season_start_postprocess_completed_at": datetime.now(KST).isoformat(),
    })
    success, error = await send_standard_season_start_notice(
        guild,
        season_name=season_name,
        cal=cal,
        reward=next_reward,
        channel_id=notice_channel_id,
    )
    if success:
        await _update_season_state({
            "start_notice_sent_for": calendar_id,
            "season_start_notice_pending_for": "",
            "season_start_notice_sent_at": datetime.now(KST).isoformat(),
        })
    else:
        await _update_season_state({"season_start_notice_last_error": error})

    log_channel = guild.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        try:
            await log_channel.send(
                f"[🌿 시즌 시작] `{season_name}` 시즌 자동 시작 처리 완료\n"
                f"- 시즌 ID: `{calendar_id}`\n"
                f"- 진행도 칭호 갱신: {nick_result['updated']}명 / 실패 {nick_result['failed']}명\n"
                f"- 시작 공지: {'성공' if success else '재시도 예정'}",
                allowed_mentions=ALLOW_NO_PING,
            )
        except Exception:
            pass

    return {
        "processed": True,
        "season_id": calendar_id,
        "season_name": season_name,
        "nick_updated": nick_result["updated"],
        "nick_failed": nick_result["failed"],
        "notice_sent": success,
    }

@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.guild_only()
@bot.tree.command(name="첫시즌시작", description="기존 레벨을 칭호로 보존하고 첫 시즌패스를 시작합니다.")
@app_commands.describe(시즌이름="첫 시즌 이름", 공지내용="시즌 시작 공지에 추가로 적을 내용(선택)")
@season_operation_serialized()
async def first_season_start(
    interaction: discord.Interaction,
    시즌이름: str,
    공지내용: str | None = None,
):
    if not interaction.guild:
        return await interaction.response.send_message("DM에서는 사용할 수 없습니다.", ephemeral=True)

    시즌이름 = (시즌이름 or "").strip()
    공지내용 = (공지내용 or "").strip() if 공지내용 else ""
    if not 시즌이름:
        return await interaction.response.send_message("❌ 첫 시즌 이름을 입력해주세요.", ephemeral=True)
    if len(시즌이름) > 20:
        return await interaction.response.send_message("❌ 시즌 이름은 20자 이내로 입력해주세요.", ephemeral=True)
    if len(공지내용) > 800:
        return await interaction.response.send_message("❌ 공지내용은 800자 이내로 입력해주세요.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    state = await aget_effective_season_state()
    cal = get_calendar_season_info(datetime.now(KST))
    season_id = cal["season_id"]

    if state.get("first_season_started"):
        return await interaction.followup.send("❌ 첫 시즌 시작 처리는 이미 완료되었습니다.", ephemeral=True)

    existing = await aget_legacy_migration_record(season_id)
    if existing:
        existing_status = str(existing.get("status", "completed"))
        if existing_status not in {"prepared", "failed_before_commit", "failed_preflight"}:
            return await interaction.followup.send(
                "❌ 이 시즌의 마이그레이션 기록이 이미 존재합니다.\n"
                f"상태: `{existing_status}` / 실행자: `{existing.get('executed_by_name', '알 수 없음')}`",
                ephemeral=True,
            )

    reward = await _get_season_reward(season_id)
    if not reward.get("title_name"):
        return await interaction.followup.send(
            "❌ 먼저 `/시즌보상설정`으로 첫 시즌 Lv.100 보상을 설정해주세요.",
            ephemeral=True,
        )

    errors = first_season_preflight_errors(interaction.guild)
    if errors:
        try:
            await aset_legacy_migration_record(season_id, {
                "type": "first_season_start",
                "status": "failed_preflight",
                "phase": "preflight",
                "season_id": season_id,
                "season_name": 시즌이름,
                "executed_by": str(interaction.user.id),
                "executed_by_name": interaction.user.display_name,
                "executed_at": datetime.now(KST).isoformat(),
                "errors": errors,
            })
        except Exception:
            pass
        return await interaction.followup.send(
            "❌ 첫 시즌 시작 전 점검 실패:\n- " + "\n- ".join(errors),
            ephemeral=True,
        )

    cache_ok, cache_error = await ensure_guild_member_cache_complete(interaction.guild)
    if not cache_ok:
        try:
            await aset_legacy_migration_record(season_id, {
                "type": "first_season_start",
                "status": "failed_preflight",
                "phase": "member_cache",
                "season_id": season_id,
                "season_name": 시즌이름,
                "executed_by": str(interaction.user.id),
                "executed_by_name": interaction.user.display_name,
                "executed_at": datetime.now(KST).isoformat(),
                "errors": [cache_error],
            })
        except Exception:
            pass
        return await interaction.followup.send(
            "❌ 첫 시즌 시작을 중단했습니다. 서버원 목록이 완전히 로드되지 않았습니다.\n"
            f"사유: {cache_error}\n"
            "Discord 개발자 포털의 `서버 멤버 인텐트`와 봇의 서버 재접속 상태를 확인해주세요.",
            ephemeral=True,
        )

    exp_data = await aload_exp_data()
    if not isinstance(exp_data, dict):
        exp_data = {}
    mission_backup = await aload_mission_data()
    if not isinstance(mission_backup, dict):
        mission_backup = {}

    now_iso = datetime.now(KST).isoformat()
    original_exp_data = copy.deepcopy(exp_data)
    reset_exp_data = copy.deepcopy(exp_data)
    user_snapshots: dict[str, dict] = {}
    updates: dict[str, object] = {}
    title_target_count = 0

    for uid, raw in list(exp_data.items()):
        uid = str(uid)
        user_data = raw if isinstance(raw, dict) else {}
        member = interaction.guild.get_member(int(uid)) if uid.isdigit() else None
        is_bot_record = bool(member and member.bot)
        legacy_level = get_legacy_level_from_user_record(user_data)
        legacy_title_name = f"잊혀진 기억 Lv. {legacy_level}"

        user_snapshots[uid] = {
            "original_record": copy.deepcopy(user_data),
            "legacy_level": legacy_level,
            "legacy_title_name": legacy_title_name,
            "known_bot": is_bot_record,
        }

        reset_record = copy.deepcopy(user_data)
        reset_record["exp"] = 0
        reset_record["level"] = 1
        reset_record["voice_minutes"] = 0
        reset_record["last_text_xp_at"] = 0
        reset_exp_data[uid] = reset_record

        if not is_bot_record:
            title_target_count += 1
            updates[f"user_titles/{uid}/owned/{LEGACY_FORGOTTEN_TITLE_ID}"] = {
                "title_name": legacy_title_name,
                "source": "legacy_level_system",
                "source_level": int(legacy_level),
                "acquired_at": now_iso,
                "description": "시즌패스 전환 전 기존 레벨을 보존한 칭호입니다.",
            }

    prepared_record = {
        "type": "first_season_start",
        "status": "prepared",
        "phase": "snapshot_saved",
        "season_id": season_id,
        "season_name": 시즌이름,
        "executed_by": str(interaction.user.id),
        "executed_by_name": interaction.user.display_name,
        "executed_at": now_iso,
        "calendar": {
            "regular_start": cal.get("regular_start"),
            "regular_end": cal.get("regular_end"),
            "preseason_start": cal.get("preseason_start"),
            "preseason_end": cal.get("preseason_end"),
        },
        "reward": {
            "title_name": reward.get("title_name"),
            "description": reward.get("description", ""),
        },
        "notice_extra": 공지내용,
        "backup": {"exp_data": original_exp_data, "mission_data": mission_backup},
        "users": user_snapshots,
        "result": {
            "legacy_title_target_count": title_target_count,
            "exp_reset_target_count": len(reset_exp_data),
        },
    }

    try:
        await aset_legacy_migration_record(season_id, prepared_record)
    except Exception as e:
        logging.exception(f"[first-season] snapshot save failed: {e}")
        return await interaction.followup.send(
            "❌ 기존 경험치 백업 저장에 실패했습니다. 데이터는 변경되지 않았습니다.",
            ephemeral=True,
        )

    state_updates = {
        "current_season_id": season_id,
        "current_season_name": 시즌이름,
        "current_season_type": cal["season_type"],
        "current_season_label": cal["season_label"],
        "status": cal["status"],
        "settled": False,
        "next_ready": False,
        "first_season_started": True,
        "first_season_started_at": now_iso,
        "first_season_started_by": str(interaction.user.id),
        "next_season_id": cal["next_season_id"],
        "next_season_name": "",
        "started_at": now_iso,
        "start_notice_sent_for": "",
        "season_start_postprocess_pending_for": season_id,
        "season_start_notice_pending_for": season_id,
        "season_start_notice_last_error": "",
        "season_start_processed_at": now_iso,
        "season_notice_channel_id": SEASON_NOTICE_CHANNEL_ID,
    }

    updates["exp_data"] = reset_exp_data if reset_exp_data else None
    updates["mission_data"] = None
    for key, value in state_updates.items():
        updates[f"season_state/{key}"] = value
    updates[f"legacy_migration_records/{season_id}/status"] = "db_committed"
    updates[f"legacy_migration_records/{season_id}/phase"] = "database_committed"
    updates[f"legacy_migration_records/{season_id}/db_committed_at"] = datetime.now(KST).isoformat()

    try:
        await afirebase_root_update_strict(updates)
    except Exception as e:
        logging.exception(f"[first-season] atomic database commit failed: {e}")
        committed = False
        try:
            verify_state = await asyncio.to_thread(lambda: _season_state_ref().get() or {})
            verify_record = await aget_legacy_migration_record(season_id)
            committed = (
                isinstance(verify_state, dict)
                and verify_state.get("first_season_started") is True
                and verify_state.get("current_season_id") == season_id
                and verify_record.get("status") == "db_committed"
            )
        except Exception:
            committed = False

        if not committed:
            try:
                await aupdate_legacy_migration_record(season_id, {
                    "status": "failed_before_commit",
                    "phase": "atomic_database_commit",
                    "error": repr(e),
                    "failed_at": datetime.now(KST).isoformat(),
                })
            except Exception:
                pass
            return await interaction.followup.send(
                "❌ Firebase 전환 처리에 실패했습니다. 기존 경험치와 시즌 상태는 변경되지 않았습니다.",
                ephemeral=True,
            )

        logging.warning("[first-season] commit response failed, but committed state was verified")

    try:
        save_json(MISSION_PATH, {})
    except Exception as e:
        logging.warning(f"[first-season] local mission cache reset failed: {e!r}")

    role_removed_count = role_failed_count = 0
    nick_updated_count = nick_failed_count = 0
    for member in interaction.guild.members:
        if member.bot:
            continue
        try:
            removed, failed = await remove_legacy_level_roles(member)
            role_removed_count += removed
            role_failed_count += failed
        except Exception as e:
            logging.warning(f"[first-season] legacy role cleanup failed uid={member.id}: {e!r}")
            role_failed_count += 1

        if member.id != interaction.guild.owner_id:
            if await apply_member_title(member, 1):
                nick_updated_count += 1
            else:
                nick_failed_count += 1
        await asyncio.sleep(0.15)

    await _update_season_state({
        "season_start_postprocess_pending_for": "",
        "season_start_postprocess_completed_at": datetime.now(KST).isoformat(),
    })

    notice_sent, notice_error = await send_season_start_embed(
        interaction.guild,
        embed=build_first_season_start_embed(시즌이름, cal, reward, 공지내용),
        channel_id=SEASON_NOTICE_CHANNEL_ID,
    )
    if notice_sent:
        await _update_season_state({
            "start_notice_sent_for": season_id,
            "season_start_notice_pending_for": "",
            "season_start_notice_sent_at": datetime.now(KST).isoformat(),
            "season_start_notice_last_error": "",
        })
    else:
        await _update_season_state({"season_start_notice_last_error": notice_error})

    if not notice_sent:
        try:
            latest_state = await asyncio.to_thread(lambda: _season_state_ref().get() or {})
            if isinstance(latest_state, dict) and latest_state.get("start_notice_sent_for") == season_id:
                notice_sent = True
                notice_error = ""
        except Exception:
            pass

    final_status = "completed" if notice_sent else "completed_notice_failed"
    final_result = {
        "legacy_title_count": title_target_count,
        "exp_reset_count": len(reset_exp_data),
        "role_removed_count": role_removed_count,
        "role_failed_count": role_failed_count,
        "nick_updated_count": nick_updated_count,
        "nick_failed_count": nick_failed_count,
        "notice_sent": notice_sent,
    }
    try:
        await aupdate_legacy_migration_record(season_id, {
            "status": final_status,
            "phase": "completed",
            "completed_at": datetime.now(KST).isoformat(),
            "result": final_result,
            "notice_error": notice_error,
        })
    except Exception as e:
        logging.warning(f"[first-season] final migration record update failed: {e!r}")

    log_channel = interaction.guild.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        try:
            log_embed = discord.Embed(title="🌿 첫 시즌 시작 처리 완료", color=discord.Color.blurple())
            log_embed.add_field(name="시즌명", value=시즌이름, inline=False)
            log_embed.add_field(name="시즌 ID", value=season_id, inline=False)
            log_embed.add_field(name="기존 레벨 칭호 지급", value=f"{title_target_count}명", inline=True)
            log_embed.add_field(name="EXP 초기화", value=f"{len(reset_exp_data)}명", inline=True)
            log_embed.add_field(name="기존 레벨 역할 제거", value=f"성공 {role_removed_count}개 / 실패 {role_failed_count}개", inline=False)
            log_embed.add_field(name="닉네임 갱신", value=f"성공 {nick_updated_count}명 / 실패 {nick_failed_count}명", inline=False)
            log_embed.add_field(name="시즌 공지", value="성공" if notice_sent else "실패", inline=False)
            await log_channel.send(embed=log_embed, allowed_mentions=ALLOW_NO_PING)
        except Exception:
            pass

    result_embed = discord.Embed(
        title="✅ 첫 시즌 시작 완료" if notice_sent else "⚠️ 첫 시즌 DB 전환 완료 · 공지 실패",
        description=f"`{시즌이름}` 시즌 데이터 전환이 완료되었습니다.",
        color=discord.Color.green() if notice_sent else discord.Color.orange(),
    )
    result_embed.add_field(name="기존 레벨 칭호 지급", value=f"{title_target_count}명", inline=True)
    result_embed.add_field(name="EXP 초기화", value=f"{len(reset_exp_data)}명", inline=True)
    result_embed.add_field(name="기존 레벨 역할 제거", value=f"성공 {role_removed_count}개 / 실패 {role_failed_count}개", inline=False)
    result_embed.add_field(name="닉네임 갱신", value=f"성공 {nick_updated_count}명 / 실패 {nick_failed_count}명", inline=False)
    result_embed.add_field(name="시즌 공지", value="성공" if notice_sent else f"실패: {notice_error[:500]}", inline=False)
    result_embed.add_field(
        name="현재 시즌 상태",
        value=f"{season_status_label(cal['status'])}\n정규 시즌 종료일: {cal['regular_end']}",
        inline=False,
    )
    await interaction.followup.send(embed=result_embed, ephemeral=True)

@app_commands.guild_only()
@bot.tree.command(name="시즌정보", description="현재 시즌패스 정보와 내 진행도를 확인합니다.")
async def season_info(interaction: discord.Interaction):
    await interaction.response.defer()
    state = await aget_effective_season_state()
    cal = state.get("calendar") or get_calendar_season_info(datetime.now(KST))
    current_season_id = state.get("current_season_id") or cal["season_id"]
    reward = await _get_season_reward(current_season_id)

    uid = str(interaction.user.id)
    user_data = await aget_user_exp(uid)
    if state.get("first_season_started"):
        total_xp = max(0, _safe_int(user_data.get("exp", 0), 0))
    else:
        # 기존 레벨 EXP는 첫 시즌 시작 전 시즌패스 진행도로 노출하지 않습니다.
        total_xp = 0
    level, cur_xp, need_xp, pct = get_level_progress(total_xp)
    pct_int = int(round(pct * 100))
    status = state.get("status", cal.get("status"))

    if status == SEASON_STATUS_REGULAR:
        remain_label = f"D-{days_left_until(cal['regular_end'])}"
        status_desc = "경험치 획득 가능"
    elif status == SEASON_STATUS_PRESEASON:
        remain_label = f"D-{days_left_until(cal['preseason_end'])}"
        if state.get("settled") and state.get("next_ready"):
            status_desc = "경험치 획득 중단 · 다음 시즌 준비 완료"
        elif state.get("settled"):
            status_desc = "경험치 획득 중단 · 정산 완료 · 다음 시즌 준비 필요"
        else:
            status_desc = "경험치 획득 중단 · 현재 시즌 정산 필요"
    else:
        remain_label = "-"
        status_desc = (
            "첫 시즌 시작 전이라 경험치 획득이 중단된 상태입니다."
            if not state.get("first_season_started")
            else "정산 또는 다음 시즌 준비가 완료되지 않아 경험치 획득이 중단된 상태입니다."
        )

    embed = discord.Embed(
        title=f"🌿 시즌 정보 - {state.get('current_season_name', CURRENT_SEASON_NAME)}",
        color=discord.Color.green() if status == SEASON_STATUS_REGULAR else discord.Color.orange(),
    )
    embed.add_field(
        name="상태",
        value=f"{season_status_label(status)}\n{status_desc}\n남은 기간: {remain_label}",
        inline=False,
    )
    embed.add_field(
        name="기간",
        value=(
            f"정규 시즌: {cal['regular_start']} ~ {cal['regular_end']}\n"
            f"프리시즌: {cal['preseason_start']} ~ {cal['preseason_end']}"
        ),
        inline=False,
    )
    embed.add_field(
        name="진행 기준",
        value=(
            f"최대 레벨: Lv.{SEASON_MAX_LEVEL}\n"
            f"1레벨 필요 경험치: {SEASON_XP_PER_LEVEL:,} XP\n"
            f"Lv.{SEASON_MAX_LEVEL} 필요 경험치: {SEASON_TOTAL_XP_TO_MAX:,} XP"
        ),
        inline=False,
    )
    embed.add_field(
        name="내 진행도",
        value=(
            f"현재 레벨: Lv.{level}\n"
            f"현재 경험치: {total_xp:,} XP\n"
            f"현재 구간: {cur_xp:,} / {need_xp:,} XP ({pct_int}%)"
        ),
        inline=False,
    )
    embed.add_field(
        name="현재 시즌 보상",
        value=f"[ {reward.get('title_name', '아직 설정되지 않음')} ]",
        inline=False,
    )

    if state.get("settled"):
        next_id = state.get("next_season_id") or cal.get("next_season_id")
        next_reward = await _get_season_reward(next_id) if next_id else {}
        embed.add_field(
            name="다음 시즌 보상",
            value=f"[ {next_reward.get('title_name', '아직 설정되지 않음')} ]",
            inline=False,
        )

    embed.add_field(
        name="정산/다음 시즌",
        value=(
            f"현재 시즌 정산: {'완료' if state.get('settled') else '미완료'}\n"
            f"다음 시즌 준비: {'완료' if state.get('next_ready') else '미완료'}\n"
            f"다음 시즌명: {state.get('next_season_name') or '미설정'}"
        ),
        inline=False,
    )

    if status == SEASON_STATUS_LOCKED:
        if not state.get("first_season_started"):
            embed.set_footer(text="첫 시즌 시작 전입니다. 관리자가 /시즌보상설정 후 /첫시즌시작 을 실행해야 시즌패스가 열립니다.")
        else:
            embed.set_footer(text="/현재시즌초기화, /다음시즌준비, 다음 시즌 보상 설정을 완료해야 시즌이 열립니다.")
    elif status == SEASON_STATUS_PRESEASON:
        if not state.get("settled"):
            embed.set_footer(text="프리시즌 기간입니다. 관리자가 /현재시즌초기화 로 현재 시즌을 정산해야 합니다.")
        elif not state.get("next_ready"):
            embed.set_footer(text="현재 시즌 정산이 완료되었습니다. 관리자가 /다음시즌준비 로 다음 시즌명을 등록해야 합니다.")
        else:
            embed.set_footer(text="다음 시즌 이름과 보상을 모두 설정하면 시작일에 자동으로 시즌이 열립니다.")

    await interaction.followup.send(embed=embed)

@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.guild_only()
@bot.tree.command(name="시즌보상설정", description="현재 또는 다음 시즌 Lv.100 보상 칭호를 설정합니다.")
@app_commands.describe(칭호명="Lv.100 달성자에게 지급할 칭호", 설명="보상 설명")
@season_operation_serialized()
async def season_reward_set(interaction: discord.Interaction, 칭호명: str, 설명: str = ""):
    칭호명 = (칭호명 or "").strip()
    설명 = (설명 or "").strip()
    if not 칭호명:
        return await interaction.response.send_message("❌ 칭호명을 입력해주세요.", ephemeral=True)
    if len(칭호명) > 24:
        return await interaction.response.send_message("❌ 칭호명은 24자 이내로 입력해주세요.", ephemeral=True)
    if len(설명) > 300:
        return await interaction.response.send_message("❌ 설명은 300자 이내로 입력해주세요.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    state = await aget_effective_season_state()
    cal = get_calendar_season_info(datetime.now(KST))

    if not state.get("first_season_started"):
        season_id = cal["season_id"]
        target_label = "첫 시즌"
    elif state.get("settled"):
        season_id = state.get("next_season_id") or cal.get("next_season_id")
        target_label = "다음 시즌"
    else:
        season_id = state.get("current_season_id") or cal["season_id"]
        target_label = "현재 시즌"

    if not season_id:
        return await interaction.followup.send(
            "❌ 보상을 저장할 시즌 ID를 확인하지 못했습니다.",
            ephemeral=True,
        )

    data = {
        "title_id": make_title_id(season_id),
        "title_name": 칭호명,
        "description": 설명,
        "updated_by": str(interaction.user.id),
        "updated_at": datetime.now(KST).isoformat(),
    }
    await _set_season_reward(season_id, data)
    existing_updated = await update_existing_season_title_metadata(season_id, 칭호명, 설명)

    awarded = checked = 0
    is_active_current = (
        state.get("first_season_started")
        and not state.get("settled")
        and state.get("status") == SEASON_STATUS_REGULAR
        and season_id == state.get("current_season_id")
    )

    if is_active_current:
        exp_data = await aload_exp_data()
        if isinstance(exp_data, dict):
            for uid, user_data in exp_data.items():
                if not isinstance(user_data, dict) or not str(uid).isdigit():
                    continue
                level = calculate_level(_safe_int(user_data.get("exp", 0), 0))
                if level < SEASON_MAX_LEVEL:
                    continue
                member = interaction.guild.get_member(int(uid))
                if member and not member.bot:
                    result = await maybe_award_level100(member, level, reason="reward_set_retroactive")
                    checked += 1
                    if result.get("awarded"):
                        awarded += 1

    if is_active_current:
        extra_line = f"Lv.100 대상 확인: {checked}명 / 신규 지급: {awarded}명"
    elif not state.get("first_season_started"):
        extra_line = "첫 시즌 시작 전이므로 기존 EXP를 대상으로 소급 지급하지 않았습니다."
    else:
        extra_line = "다음 시즌 보상이므로 현재 시즌 대상 소급 지급을 실행하지 않았습니다."

    await interaction.followup.send(
        f"✅ {target_label} 보상 칭호가 설정되었습니다.\n"
        f"시즌 ID: `{season_id}`\n"
        f"보상: `[ {칭호명} ]`\n"
        f"기존 보유자 정보 갱신: {existing_updated}명\n"
        f"{extra_line}",
        ephemeral=True,
    )

@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.guild_only()
@bot.tree.command(name="다음시즌준비", description="다음 시즌에 사용할 시즌 이름을 지정합니다.")
@app_commands.describe(시즌이름="다음 시즌 이름")
@season_operation_serialized()
async def next_season_prepare(interaction: discord.Interaction, 시즌이름: str):
    state = await aget_effective_season_state()

    if not state.get("first_season_started"):
        return await interaction.response.send_message(
            "❌ 첫 시즌 시작 전에는 `/다음시즌준비`를 사용할 수 없습니다.\n"
            "먼저 `/시즌보상설정` 후 `/첫시즌시작`으로 첫 시즌을 개방해주세요.",
            ephemeral=True
        )

    if not state.get("settled"):
        return await interaction.response.send_message(
            "❌ 현재 시즌 정산이 완료되지 않았습니다.\n"
            "먼저 프리시즌 기간에 `/현재시즌초기화`로 현재 시즌을 정산한 뒤 `/다음시즌준비`를 실행해주세요.",
            ephemeral=True
        )

    if state.get("status") == SEASON_STATUS_REGULAR:
        return await interaction.response.send_message(
            "❌ 정규 시즌 중에는 `/다음시즌준비`를 사용할 수 없습니다.\n"
            "프리시즌에 `/현재시즌초기화`로 정산을 완료한 뒤 다음 시즌을 준비해주세요.",
            ephemeral=True
        )

    시즌이름 = (시즌이름 or "").strip()
    if not 시즌이름:
        return await interaction.response.send_message("❌ 다음 시즌 이름을 입력해주세요.", ephemeral=True)
    if len(시즌이름) > 20:
        return await interaction.response.send_message("❌ 시즌 이름은 20자 이내로 입력해주세요.", ephemeral=True)

    next_id = state.get("next_season_id") or _next_season_id_after(state.get("current_season_id", get_calendar_season_info()["season_id"]))
    await _update_season_state({
        "next_season_id": next_id,
        "next_season_name": 시즌이름,
        "next_ready": True,
        "next_prepared_by": str(interaction.user.id),
        "next_prepared_at": datetime.now(KST).isoformat(),
    })

    # locked 상태에서 준비가 완료되면 즉시 개방 가능한지 갱신
    await interaction.response.send_message(
        f"✅ 다음 시즌 준비 완료\n"
        f"다음 시즌 ID: `{next_id}`\n"
        f"다음 시즌명: `{시즌이름}`\n"
        f"※ 실제 시즌 시작은 다음 시즌 시작일에 자동 처리됩니다.",
        ephemeral=True,
    )


@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.guild_only()
@bot.tree.command(name="현재시즌초기화", description="현재 시즌을 정산하고 경험치/레벨을 초기화합니다.")
@season_operation_serialized()
async def current_season_reset(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("DM에서는 사용할 수 없습니다.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    state = await aget_effective_season_state()

    if not state.get("first_season_started"):
        return await interaction.followup.send(
            "❌ 첫 시즌 시작 전에는 정산할 시즌이 없습니다.",
            ephemeral=True,
        )
    if state.get("status") == SEASON_STATUS_REGULAR:
        return await interaction.followup.send(
            "❌ 정규 시즌 중에는 초기화할 수 없습니다. 프리시즌 또는 시즌 잠금 상태에서 진행해주세요.",
            ephemeral=True,
        )
    if state.get("settled"):
        return await interaction.followup.send(
            "❌ 현재 시즌은 이미 정산 완료 상태입니다.",
            ephemeral=True,
        )

    season_id = state.get("current_season_id")
    reward = await _get_season_reward(season_id)
    if not reward.get("title_name"):
        return await interaction.followup.send(
            "❌ 현재 시즌 Lv.100 보상 칭호가 설정되어 있지 않습니다.",
            ephemeral=True,
        )

    cache_ok, cache_error = await ensure_guild_member_cache_complete(interaction.guild)
    if not cache_ok:
        return await interaction.followup.send(
            "❌ 시즌 정산을 중단했습니다. 서버원 목록이 완전히 로드되지 않았습니다.\n"
            f"사유: {cache_error}",
            ephemeral=True,
        )

    exp_data = await aload_exp_data()
    if not isinstance(exp_data, dict):
        exp_data = {}

    now_iso = datetime.now(KST).isoformat()
    reached: list[str] = []
    dm_success: list[str] = []
    dm_failed: list[str] = []
    reset_count = 0
    records: dict[str, dict] = {}
    reset_exp_data = copy.deepcopy(exp_data)

    for uid, raw in list(exp_data.items()):
        uid = str(uid)
        if not isinstance(raw, dict):
            continue
        exp = max(0, _safe_int(raw.get("exp", 0), 0))
        level = calculate_level(exp)
        reached_100 = level >= SEASON_MAX_LEVEL

        if reached_100:
            reached.append(uid)
            member = None
            try:
                member = interaction.guild.get_member(int(uid)) or await interaction.guild.fetch_member(int(uid))
            except Exception:
                pass
            if member and not member.bot:
                await maybe_award_level100(member, level, reason="season_settlement")
                completion = await asyncio.to_thread(
                    lambda sid=season_id, x=uid: _season_completion_ref(sid, x).get() or {}
                )
                (dm_success if completion.get("dm_sent") else dm_failed).append(uid)
            else:
                dm_failed.append(uid)

        records[uid] = {
            "final_exp": exp,
            "final_level": level,
            "reached_100": reached_100,
            "reward_title": reward.get("title_name", ""),
            "settled_at": now_iso,
        }

        reset_record = copy.deepcopy(raw)
        reset_record["exp"] = 0
        reset_record["level"] = 1
        reset_record["voice_minutes"] = 0
        reset_record["last_text_xp_at"] = 0
        reset_exp_data[uid] = reset_record
        reset_count += 1

    settlement_updates = {
        f"season_records/{season_id}": records,
        "exp_data": reset_exp_data if reset_exp_data else None,
        "mission_data": None,
        "season_state/settled": True,
        "season_state/status": SEASON_STATUS_PRESEASON,
        "season_state/next_ready": False,
        "season_state/settled_by": str(interaction.user.id),
        "season_state/settled_at": now_iso,
        "season_state/settlement_postprocess_pending": True,
        "season_state/settlement_postprocess_season_id": season_id,
        "season_state/settlement_notice_sent_for": "",
        "season_state/settlement_notice_pending": True,
        "season_state/settlement_log_sent_for": "",
    }
    try:
        await afirebase_root_update_strict(settlement_updates)
    except Exception as e:
        logging.exception(f"[season-settlement] atomic update failed: {e}")
        committed = False
        try:
            verify_state = await asyncio.to_thread(lambda: _season_state_ref().get() or {})
            verify_records = await asyncio.to_thread(lambda: _season_records_ref(season_id).get() or {})
            committed = (
                isinstance(verify_state, dict)
                and verify_state.get("settled") is True
                and isinstance(verify_records, dict)
            )
        except Exception:
            committed = False

        if not committed:
            return await interaction.followup.send(
                "❌ 시즌 정산 저장에 실패했습니다. 경험치와 시즌 상태는 변경되지 않았습니다.",
                ephemeral=True,
            )
        logging.warning("[season-settlement] update response failed, but committed state was verified")

    try:
        save_json(MISSION_PATH, {})
    except Exception as e:
        logging.warning(f"[season-settlement] local mission cache reset failed: {e!r}")

    nick_result = await reset_progress_title_members(interaction.guild, level=1)
    names = []
    for uid in reached[:20]:
        try:
            member = interaction.guild.get_member(int(uid)) or await interaction.guild.fetch_member(int(uid))
            names.append(member.display_name)
        except Exception:
            names.append(uid)
    reached_text = "없음" if not names else "\n".join(f"- {name}" for name in names)
    if len(reached) > 20:
        reached_text += f"\n...외 {len(reached) - 20}명"

    embed = discord.Embed(title="🧾 현재 시즌 정산 완료", color=discord.Color.blurple())
    embed.add_field(name="시즌", value=f"{state.get('current_season_name')} (`{season_id}`)", inline=False)
    embed.add_field(name="초기화", value=f"경험치/레벨/음성시간 초기화: {reset_count}명", inline=False)
    embed.add_field(name="Lv.100 대상자", value=f"총 {len(reached)}명\n{reached_text}", inline=False)
    embed.add_field(
        name="보상 DM 기록",
        value=f"성공 기록: {len(dm_success)}명 / 실패 또는 확인 불가: {len(dm_failed)}명",
        inline=False,
    )
    embed.add_field(
        name="닉네임 갱신",
        value=f"성공: {nick_result['updated']}명 / 실패: {nick_result['failed']}명",
        inline=False,
    )
    embed.set_footer(text=f"정산자: {interaction.user.display_name} · {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')}")

    log_sent = False
    log_channel = interaction.guild.get_channel(LOG_CHANNEL_ID)
    if log_channel and hasattr(log_channel, "send"):
        try:
            await log_channel.send(embed=embed, allowed_mentions=ALLOW_NO_PING)
            log_sent = True
        except Exception:
            pass

    notice_sent = False
    notice = interaction.guild.get_channel(SEASON_NOTICE_CHANNEL_ID)
    if notice and hasattr(notice, "send"):
        try:
            await notice.send(
                f"📢 `{state.get('current_season_name')}` 시즌 정산이 완료되었습니다. 프리시즌 동안 시즌 경험치 획득이 중단됩니다.",
                allowed_mentions=ALLOW_NO_PING,
            )
            notice_sent = True
        except Exception:
            pass

    await _update_season_state({
        "settlement_postprocess_pending": False,
        "settlement_postprocess_completed_at": datetime.now(KST).isoformat(),
        "settlement_notice_sent_for": season_id if notice_sent else "",
        "settlement_notice_pending": not notice_sent,
        "settlement_log_sent_for": season_id if log_sent else "",
    })

    await interaction.followup.send(embed=embed, ephemeral=True)


class TitleSelect(discord.ui.Select):
    def __init__(self, owner_id: int, options_data: list[dict]):
        self.owner_id = owner_id
        self.options_data = options_data
        options = []
        for idx, item in enumerate(options_data[:25], start=1):
            options.append(discord.SelectOption(
                label=f"{idx}. {item['label']}"[:100],
                value=item["value"],
                description=item.get("description", "")[:100],
            ))
        super().__init__(placeholder="착용할 칭호를 선택하세요", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("이 메뉴는 명령어를 실행한 본인만 사용할 수 있습니다.", ephemeral=True)

        state = await aget_effective_season_state()
        if not state.get("first_season_started"):
            return await interaction.response.edit_message(
                content="❌ 현재는 시즌패스 준비 중이라 칭호를 변경할 수 없습니다.",
                view=None,
            )

        selected = self.values[0]
        uid = str(interaction.user.id)
        titles = await aget_user_titles(uid)
        level = calculate_level((await aget_user_exp(uid)).get("exp", 0))

        if selected == "progress":
            await aset_user_equipped_title(uid, {"type": "progress"})
            await apply_member_title(interaction.user, level)
            title_text = await aget_equipped_title_text(uid, level)
            return await interaction.response.edit_message(content=f"✅ 진행도 칭호 `[ {title_text} ]` 를 착용했습니다.", view=None)

        owned = titles.get("owned", {})
        if selected not in owned:
            return await interaction.response.edit_message(content="❌ 보유하지 않은 칭호입니다.", view=None)

        await aset_user_equipped_title(uid, {"type": "title", "title_id": selected})
        await apply_member_title(interaction.user, level)
        title_name = owned[selected].get("title_name", selected)
        await interaction.response.edit_message(content=f"✅ 칭호 `[ {title_name} ]` 를 착용했습니다.", view=None)


class TitleManageView(discord.ui.View):
    def __init__(self, owner_id: int, options_data: list[dict]):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self.message: discord.Message | None = None
        self.add_item(TitleSelect(owner_id, options_data))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("이 메뉴는 명령어를 실행한 본인만 사용할 수 있습니다.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        try:
            for item in self.children:
                item.disabled = True
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass


@app_commands.guild_only()
@bot.tree.command(name="칭호관리", description="보유한 칭호를 확인하고 착용합니다.")
async def title_manage(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    state = await aget_effective_season_state()
    if not state.get("first_season_started"):
        return await interaction.followup.send(
            "현재는 시즌패스 준비 중이라 칭호를 변경할 수 없습니다. 첫 시즌 시작 후 이용해주세요.",
            ephemeral=True,
        )

    uid = str(interaction.user.id)
    user_data = await aget_user_exp(uid)
    level = calculate_level(user_data.get("exp", 0))
    titles = await aget_user_titles(uid)
    owned = titles.get("owned", {})

    options_data = [{
        "label": progress_title_text(level, state),
        "value": "progress",
        "description": "기본 진행도 칭호",
    }]
    for title_id, title in owned.items():
        if isinstance(title, dict) and title.get("title_name"):
            options_data.append({
                "label": title["title_name"],
                "value": title_id,
                "description": (
                    "기존 레벨 보존"
                    if title.get("source") == "legacy_level_system"
                    else str(title.get("source_season_id", "시즌 보상"))
                ),
            })

    desc_lines = []
    for idx, item in enumerate(options_data[:25], start=1):
        desc_lines.append(f"{idx}. [ {item['label']} ] - {item.get('description', '')}")
    if len(options_data) > 25:
        desc_lines.append(f"...표시 제한으로 {len(options_data) - 25}개 칭호는 표시되지 않습니다.")

    embed = discord.Embed(title="🏷️ 칭호 관리", description="\n".join(desc_lines), color=discord.Color.purple())
    embed.set_footer(text="60초 안에 선택해주세요. 이 메시지는 본인에게만 보입니다.")
    view = TitleManageView(interaction.user.id, options_data)
    try:
        view.message = await interaction.followup.send(
            embed=embed,
            view=view,
            ephemeral=True,
            wait=True,
        )
    except Exception:
        await interaction.followup.send(
            "❌ 칭호 관리 메뉴를 불러오지 못했습니다.",
            ephemeral=True,
        )

@app_commands.guild_only()
@bot.tree.command(name="연결끊기", description="현재 음성방에 있는 유저의 음성 연결을 끊습니다.")
@app_commands.describe(
    대상="연결을 끊을 대상",
    사유="연결을 끊는 사유"
)
async def disconnect_voice(
    interaction: discord.Interaction,
    대상: discord.Member,
    사유: str
):
    if not interaction.guild:
        return await interaction.response.send_message(
            "DM에서는 사용할 수 없습니다.",
            ephemeral=True
        )

    사유 = (사유 or "").strip()

    if not 사유:
        return await interaction.response.send_message(
            "❌ 연결 끊는 사유를 입력해주세요.",
            ephemeral=True
        )

    if len(사유) > 500:
        return await interaction.response.send_message(
            "❌ 사유는 500자 이내로 입력해주세요.",
            ephemeral=True
        )

    if not 대상.voice or not 대상.voice.channel:
        return await interaction.response.send_message(
            f"❌ {대상.display_name} 님은 현재 음성방에 참여 중이 아닙니다.",
            ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)

    executor = interaction.user
    voice_channel = 대상.voice.channel
    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")

    dm_sent = True
    try:
        dm_embed = discord.Embed(
            title="🔌 음성 연결이 종료되었습니다",
            description=(
                f"서버: {interaction.guild.name}\n"
                f"처리자: {executor.display_name}\n"
                f"사유: {사유}"
            ),
            color=discord.Color.orange()
        )
        dm_embed.set_footer(text=f"처리 시각: {now_str}")
        await 대상.send(embed=dm_embed)
    except Exception:
        dm_sent = False

    try:
        await 대상.move_to(
            None,
            reason=f"/연결끊기 사용자={executor}({executor.id}) 사유={사유[:300]}"
        )
    except discord.Forbidden:
        return await interaction.followup.send(
            "❌ 봇에게 대상자의 음성 연결을 끊을 권한이 없습니다. "
            "봇 역할에 `멤버 이동` 권한이 있는지 확인해주세요.",
            ephemeral=True
        )
    except discord.HTTPException as e:
        return await interaction.followup.send(
            f"❌ 연결 끊기 처리 중 Discord 오류가 발생했습니다: {e}",
            ephemeral=True
        )
    except Exception as e:
        return await interaction.followup.send(
            f"❌ 연결 끊기 처리 중 오류가 발생했습니다: {type(e).__name__}",
            ephemeral=True
        )

    log_channel = interaction.guild.get_channel(DISCONNECT_LOG_CHANNEL_ID)

    if log_channel:
        log_embed = discord.Embed(
            title="🔌 음성 연결 끊기 기록",
            color=discord.Color.red()
        )
        log_embed.add_field(
            name="처리자",
            value=f"{executor.display_name} (`{executor.id}`)",
            inline=False
        )
        log_embed.add_field(
            name="대상자",
            value=f"{대상.display_name} (`{대상.id}`)",
            inline=False
        )
        log_embed.add_field(
            name="대상 음성방",
            value=f"{voice_channel.name} (`{voice_channel.id}`)",
            inline=False
        )
        log_embed.add_field(
            name="사유",
            value=사유,
            inline=False
        )
        log_embed.add_field(
            name="DM 전송",
            value="성공" if dm_sent else "실패",
            inline=True
        )
        log_embed.set_footer(text=f"처리 시각: {now_str}")

        try:
            await log_channel.send(embed=log_embed, allowed_mentions=ALLOW_NO_PING)
        except Exception:
            pass

    await interaction.followup.send(
        f"✅ {대상.display_name} 님의 음성 연결을 끊었습니다.\n"
        f"사유: {사유}\n"
        f"DM 전송: {'성공' if dm_sent else '실패'}",
        ephemeral=True
    )

# ---- 실행 및 웹 서버 유지 ----
from aiohttp import web

# ---- 실행 및 웹 서버 유지 (aiohttp, same event loop) ----
async def health(_request):
    """프로세스와 웹 서버 생존 여부를 반환합니다."""
    return web.json_response({
        "process": "ok",
        "discord_ready": bool(bot.is_ready()),
        "guild_count": len(bot.guilds),
    })


async def readiness(_request):
    """Discord 로그인까지 완료됐는지 확인하는 준비 상태 엔드포인트입니다."""
    ready = bool(bot.is_ready())
    return web.json_response(
        {
            "ready": ready,
            "discord_user": str(bot.user) if bot.user else None,
            "guild_count": len(bot.guilds),
        },
        status=200 if ready else 503,
    )

_web_runner = None

async def start_web_app():
    global _web_runner
    try:
        app = web.Application()
        app.router.add_get("/", health)
        app.router.add_get("/ready", readiness)

        _web_runner = web.AppRunner(app)
        await _web_runner.setup()

        port = int(os.getenv("PORT", "10000"))
        site = web.TCPSite(_web_runner, host="0.0.0.0", port=port)
        await site.start()

        logging.info(f"[web] listening on 0.0.0.0:{port}")
    except Exception as e:
        logging.exception(f"[web] failed to start: {e}")
        # 웹이 죽어도 봇은 계속 켠다

def _http_retry_after_seconds(error: Exception) -> float | None:
    """Discord HTTP 오류에서 Retry-After 값을 가능한 범위에서 안전하게 추출합니다."""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        try:
            raw = headers.get("Retry-After")
            if raw is not None:
                value = float(raw)
                if value >= 0:
                    return value
        except (TypeError, ValueError):
            pass

    # discord.py 버전에 따라 오류 본문이 문자열/딕셔너리 형태일 수 있어 둘 다 대응합니다.
    body = getattr(error, "text", None)
    if isinstance(body, dict):
        raw = body.get("retry_after")
        try:
            value = float(raw)
            if value >= 0:
                return value
        except (TypeError, ValueError):
            pass
    elif isinstance(body, str) and body:
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict) and parsed.get("retry_after") is not None:
                value = float(parsed["retry_after"])
                if value >= 0:
                    return value
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    return None


def _login_backoff_seconds(failure_count: int, *, base: int, cap: int) -> int:
    """로그인 재시도 간격을 지수형으로 늘리되 상한을 둡니다."""
    failure_count = max(1, int(failure_count))
    exponent = min(failure_count - 1, 6)
    raw = min(base * (2 ** exponent), cap)
    # 서버가 요구한 대기시간보다 짧아지는 하향 지터는 사용하지 않습니다.
    return max(1, int(raw * random.uniform(1.0, 1.10)))


async def _safe_start():
    """
    Discord 시작을 두 단계로 분리합니다.

    1) 최초 HTTP 로그인만 제한적으로 재시도합니다.
       - 429: 10분 → 20분 → 30분(상한)
       - 5xx/네트워크 오류: 1분 → 2분 → 4분 → 5분(상한)
       - Retry-After가 있으면 그 값을 절대 밑돌지 않습니다.
    2) 로그인 성공 후 Gateway 연결은 discord.py의 reconnect=True에 맡깁니다.

    핵심: 로그인 실패 때 bot.close()를 호출하지 않습니다.
    닫힌 aiohttp 세션을 같은 Bot 인스턴스에서 반복 재사용해
    `RuntimeError: Session is closed`가 이어지는 경로를 차단합니다.
    """
    login_failures = 0

    while True:
        try:
            logging.info("[login] authenticating with Discord")
            await bot.login(TOKEN)
            logging.info("[login] authentication succeeded")
            break

        except discord.HTTPException as e:
            status = getattr(e, "status", None)
            login_failures += 1

            if status == 429:
                server_retry = _http_retry_after_seconds(e)
                wait = _login_backoff_seconds(
                    login_failures,
                    base=600,   # 10분
                    cap=1800,   # 30분
                )
                if server_retry is not None:
                    # Retry-After보다 최소 5초 여유를 둡니다.
                    wait = max(wait, int(server_retry) + 5)

                logging.warning(
                    "[login] HTTP 429 rate limited; retry in %ss (attempt=%s, server_retry=%s)",
                    wait,
                    login_failures,
                    server_retry,
                )
                await asyncio.sleep(wait)
                continue

            if isinstance(status, int) and 500 <= status <= 599:
                wait = _login_backoff_seconds(
                    login_failures,
                    base=60,
                    cap=300,
                )
                logging.warning(
                    "[login] Discord HTTP %s; retry in %ss (attempt=%s): %r",
                    status,
                    wait,
                    login_failures,
                    e,
                )
                await asyncio.sleep(wait)
                continue

            # 401/403 등 설정·토큰 계열 오류는 무한 재시도하지 않고 즉시 실패시킵니다.
            logging.exception("[login] non-retryable Discord HTTP error status=%s", status)
            raise

        except aiohttp.ClientError as e:
            login_failures += 1
            wait = _login_backoff_seconds(
                login_failures,
                base=60,
                cap=300,
            )
            logging.warning(
                "[login] network error; retry in %ss (attempt=%s): %r",
                wait,
                login_failures,
                e,
            )
            await asyncio.sleep(wait)
            continue

        except RuntimeError as e:
            # 과거 배포에서 닫힌 HTTP 세션 상태가 남았을 때 한 번 복구할 수 있도록 합니다.
            if "session is closed" in str(e).lower():
                login_failures += 1
                try:
                    bot.clear()
                except Exception:
                    logging.exception("[login] bot.clear() failed while recovering closed session")
                    raise

                wait = _login_backoff_seconds(
                    login_failures,
                    base=60,
                    cap=300,
                )
                logging.warning(
                    "[login] closed HTTP session detected; client state cleared; retry in %ss",
                    wait,
                )
                await asyncio.sleep(wait)
                continue
            raise

    # Client.start(token, reconnect=True)의 두 번째 단계와 동일하게 Gateway에 연결합니다.
    # 연결이 성립한 뒤의 일시적인 Gateway 장애는 discord.py 자체 재접속 로직이 담당합니다.
    logging.info("[gateway] connecting with reconnect=True")
    await bot.connect(reconnect=True)

    # 정상 운영 중에는 connect()가 임의로 반환하지 않습니다.
    # 여기까지 왔다면 프로세스를 정상 상태로 가장하지 말고 Render가 재시작할 수 있게 실패시킵니다.
    raise RuntimeError("Discord gateway loop returned unexpectedly")



# --- 강제 로깅 활성화 (INFO 이상 콘솔 출력)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
    force=True,
)

# discord 내부 로거 가시성 상승
logging.getLogger("discord").setLevel(logging.INFO)
logging.getLogger("discord.client").setLevel(logging.INFO)
logging.getLogger("discord.gateway").setLevel(logging.INFO)
logging.getLogger("discord.http").setLevel(logging.INFO)

# 프로그램 시작 시: 포트를 먼저 바인딩하고, 그 다음 디스코드 봇을 시작
async def _main():
    # 포트 바인딩(웹 서버) 먼저 시작 → Render의 포트 스캔 통과
    await start_web_app()
    # 이후 디스코드 로그인 루프 진입
    await _safe_start()

if __name__ == "__main__":
    asyncio.run(_main())
