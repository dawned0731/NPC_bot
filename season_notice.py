"""Season cards rendered locally; missing fonts fall back to the text embed."""
from io import BytesIO
from pathlib import Path
import asyncio
import math
import discord
from PIL import Image, ImageDraw, ImageFont

COLORS={'spring':('#143D32','#B5D99C'),'summer':('#103A55','#88DAD0'),
        'fall':('#492E28','#E6BB86'),'winter':('#25354D','#C1D3EC')}
FONT=Path(__file__).parent/'assets/fonts/Donoun Medium.ttf'


def render(state, dates, reward, kind='regular', font_path=FONT):
    if not Path(font_path).is_file():
        raise FileNotFoundError('Korean season font unavailable')
    base,accent=COLORS[state['current_season_type']]
    image=Image.new('RGB',(1200,440),base);draw=ImageDraw.Draw(image)
    # Subtle seasonal motifs remain behind text and require no external assets.
    motif = Image.new('RGBA', image.size)
    md = ImageDraw.Draw(motif)
    ink = accent + '30'
    season = state['current_season_type']
    for cx, cy in ((1030, 80), (1130, 140), (1090, 370)):
        if season == 'spring':
            for n in range(5):
                angle = n * math.tau / 5
                x, y = cx + 20 * math.cos(angle), cy + 20 * math.sin(angle)
                md.ellipse((x-17,y-17,x+17,y+17), fill=ink)
        elif season == 'summer':
            md.ellipse((cx-25,cy-25,cx+25,cy+25), fill=ink)
            for n in range(8):
                a = n * math.tau / 8
                md.line((cx+32*math.cos(a),cy+32*math.sin(a),cx+45*math.cos(a),cy+45*math.sin(a)),fill=ink,width=4)
        elif season == 'fall':
            md.polygon([(cx,cy-40),(cx+12,cy-10),(cx+35,cy-20),(cx+23,cy+4),(cx+38,cy+16),(cx+6,cy+26),(cx,cy+43),(cx-6,cy+26),(cx-38,cy+16),(cx-23,cy+4),(cx-35,cy-20),(cx-12,cy-10)],fill=ink)
        else:
            for n in range(6):
                a = n * math.tau / 6
                md.line((cx,cy,cx+40*math.cos(a),cy+40*math.sin(a)),fill=ink,width=4)
                for side in (-1,1):
                    x,y = cx+25*math.cos(a),cy+25*math.sin(a)
                    md.line((x,y,x+14*math.cos(a+side*.8),y+14*math.sin(a+side*.8)),fill=ink,width=3)
    image = Image.alpha_composite(image.convert('RGBA'), motif).convert('RGB')
    draw = ImageDraw.Draw(image)
    def font(size):return ImageFont.truetype(str(font_path),size)
    def fit(text,size,width):
        while size>16 and draw.textlength(text,font=font(size))>width:size-=1
        if draw.textlength(text,font=font(size))>width:
            while text and draw.textlength(text+'…',font=font(size))>width:text=text[:-1]
            text+='…'
        return text,font(size)
    pre=kind=='preseason';label={'regular':'정규 시즌 시작','preseason':'프리시즌 · 버닝 타임','settled':'시즌 정산 완료'}[kind]
    draw.rectangle((0,0,12,440),fill=accent)
    draw.text((48,34),'사계절, 그 사이  /  '+label,font=font(24),fill=accent)
    name=str(state.get('current_season_name') or state['current_season_id'])
    text,titlefont=fit(name,58,1090);draw.text((46,88),text,font=titlefont,fill='white')
    prefix='preseason' if pre else 'regular'
    draw.text((48,176),f"{dates[prefix+'_start']} — {dates[prefix+'_end']}  KST",font=font(26),fill='white')
    draw.line((48,232,1152,232),fill=accent,width=1)
    draw.text((48,258),'경험치 10배 · 레벨 제한 없음' if pre else 'Lv.1 — Lv.100 · 레벨당 1,100 XP',font=font(30),fill=accent)
    detail='마감일 23:00 경험치 종료 · 기록 보관 없이 초기화' if pre else 'Lv.100 보상  '+str(reward.get('title_name') or '미설정')
    text,detailfont=fit(detail,25,1090);draw.text((48,326),text,font=detailfont,fill='white')
    out=BytesIO();image.save(out,'PNG');out.seek(0);return out


async def send(channel, embed, state, dates, reward, kind='regular', renderer=render):
    attachment=None
    try:
        try:
            buffer=await asyncio.to_thread(renderer,state,dates,reward,kind)
            attachment=discord.File(buffer,filename='season-notice.png')
        except Exception:
            return await channel.send(embed=embed,allowed_mentions=discord.AllowedMentions.none())
        copy=embed.copy();copy.set_image(url='attachment://season-notice.png')
        try:
            return await channel.send(embed=copy,file=attachment,allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as error:
            if error.status not in (400,403,413):
                raise  # An uncertain send must not be immediately duplicated.
            return await channel.send(embed=embed,allowed_mentions=discord.AllowedMentions.none())
    finally:
        if attachment is not None:attachment.close()
