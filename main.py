# -*- coding: utf-8 -*-
import os, io, re, sqlite3, traceback
import datetime as dt
import discord
from discord.ext import commands
from discord import app_commands
from discord.errors import NotFound

# ===== 환경 =====
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # 비우면 봇이 들어간 모든 서버에 적용
DB_PATH = os.getenv("DB_PATH", "ticketbot.db")

def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

COLOR = discord.Color.from_rgb(0, 0, 0)

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ===== 안전 응답 헬퍼 =====
async def safe_reply(inter: discord.Interaction, content=None, embed=None, ephemeral=True):
    try:
        if not inter.response.is_done():
            return await inter.response.send_message(content=content, embed=embed, ephemeral=ephemeral)
        else:
            return await inter.followup.send(content=content, embed=embed, ephemeral=ephemeral)
    except NotFound:
        try:
            return await inter.followup.send(content=content or "처리가 완료됐어.", embed=embed, ephemeral=ephemeral)
        except Exception as e:
            print("safe_reply followup error:", e)
    except Exception as e:
        print("safe_reply error:", e)
        if not inter.response.is_done():
            try:
                await inter.response.send_message("처리 중 오류가 발생했어.", ephemeral=True)
            except:
                pass

# ===== 공용/DB =====
def db(): return sqlite3.connect(DB_PATH)

def make_embed(title: str, desc: str = "", fields: list[tuple[str,str,bool]]|None=None):
    e = discord.Embed(title=title, description=desc, color=COLOR)
    if fields:
        for n,v,i in fields:
            e.add_field(name=n, value=v, inline=i)
    e.timestamp = now_utc(); return e

def slugify(s: str) -> str:
    s = s.lower().strip().replace(" ", "-")
    s = re.sub(r"[^a-z0-9\-]", "", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "ticket"

def pemoji(s: str|None):
    if not s: return None
    try: return discord.PartialEmoji.from_str(s.strip())
    except: return None

def init_db():
    conn=db(); cur=conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS guild_settings(
        guild_id INTEGER PRIMARY KEY,
        category_id INTEGER,
        support_role_id INTEGER,
        log_channel_id INTEGER,
        channel_name_fmt TEXT DEFAULT 'ticket-{type}-{user}',
        open_msg TEXT DEFAULT '티켓이 열렸습니다.',
        guide_msg TEXT DEFAULT '상담 내용을 구체적으로 남겨주세요. 스태프가 곧 도와드려요.',
        close_msg TEXT DEFAULT '티켓이 종료되었습니다.',
        modal_title TEXT DEFAULT '티켓 사유 입력',
        reason_label TEXT DEFAULT '간단한 문의/구매 사유',
        reason_placeholder TEXT DEFAULT '예) 로벅스 10,000 구매 문의'
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS ticket_types(
        guild_id INTEGER,
        value TEXT,
        label TEXT,
        description TEXT,
        emoji TEXT,
        ord INTEGER DEFAULT 0,
        PRIMARY KEY(guild_id, value)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS tickets(
        ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER,
        channel_id INTEGER,
        opener_id INTEGER,
        type_value TEXT,
        opened_at TEXT,
        last_activity_at TEXT,
        status TEXT,   -- open/closed
        claimed_by INTEGER,
        reason TEXT
    )""")
    conn.commit(); conn.close()

def get_settings(gid:int):
    conn=db(); cur=conn.cursor()
    cur.execute("""SELECT category_id,support_role_id,log_channel_id,
                          channel_name_fmt,open_msg,guide_msg,close_msg,
                          modal_title,reason_label,reason_placeholder
                   FROM guild_settings WHERE guild_id=?""",(gid,))
    r=cur.fetchone(); conn.close()
    if not r:
        return (None, None, None,
                'ticket-{type}-{user}',
                '티켓이 열렸습니다.',
                '상담 내용을 구체적으로 남겨주세요. 스태프가 곧 도와드려요.',
                '티켓이 종료되었습니다.',
                '티켓 사유 입력','간단한 문의/구매 사유','예) 로벅스 10,000 구매 문의')
    return r

def upsert_settings(gid:int, **kwargs):
    cur_vals = list(get_settings(gid))
    keys = ["category_id","support_role_id","log_channel_id",
            "channel_name_fmt","open_msg","guide_msg","close_msg",
            "modal_title","reason_label","reason_placeholder"]
    defaults = [None,None,None,'ticket-{type}-{user}',
                '티켓이 열렸습니다.','상담 내용을 구체적으로 남겨주세요. 스태프가 곧 도와드려요.',
                '티켓이 종료되었습니다.',
                '티켓 사유 입력','간단한 문의/구매 사유','예) 로벅스 10,000 구매 문의']
    if not cur_vals or len(cur_vals)!=len(keys): cur_vals=defaults[:]
    for i,k in enumerate(keys):
        if k in kwargs and kwargs[k] is not None:
            cur_vals[i]=kwargs[k]
    conn=db(); cur=conn.cursor()
    cur.execute("""INSERT INTO guild_settings(
        guild_id,category_id,support_role_id,log_channel_id,
        channel_name_fmt,open_msg,guide_msg,close_msg,
        modal_title,reason_label,reason_placeholder
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(guild_id) DO UPDATE SET
        category_id=excluded.category_id,
        support_role_id=excluded.support_role_id,
        log_channel_id=excluded.log_channel_id,
        channel_name_fmt=excluded.channel_name_fmt,
        open_msg=excluded.open_msg,
        guide_msg=excluded.guide_msg,
        close_msg=excluded.close_msg,
        modal_title=excluded.modal_title,
        reason_label=excluded.reason_label,
        reason_placeholder=excluded.reason_placeholder
    """,(gid,*cur_vals))
    conn.commit(); conn.close()

def add_type(gid,value,label,desc,emoji,ord_):
    conn=db(); cur=conn.cursor()
    cur.execute("""INSERT OR REPLACE INTO ticket_types(guild_id,value,label,description,emoji,ord)
                   VALUES(?,?,?,?,?,?)""",(gid,value,label,desc,emoji,ord_))
    conn.commit(); conn.close()

def list_types(gid):
    conn=db(); cur=conn.cursor()
    cur.execute("""SELECT value,label,description,emoji,ord
                   FROM ticket_types WHERE guild_id=?
                   ORDER BY ord,label""",(gid,))
    rows=cur.fetchall(); conn.close(); return rows

def ticket_from_channel(chid):
    conn=db(); cur=conn.cursor()
    cur.execute("""SELECT ticket_id,guild_id,channel_id,opener_id,type_value,
                          opened_at,last_activity_at,status,claimed_by,reason
                   FROM tickets WHERE channel_id=?""",(chid,))
    r=cur.fetchone(); conn.close(); return r

def update_ticket_activity(chid):
    conn=db(); cur=conn.cursor()
    cur.execute("UPDATE tickets SET last_activity_at=? WHERE channel_id=?",
                (now_utc().isoformat(), chid))
    conn.commit(); conn.close()

def close_ticket_record(chid):
    conn=db(); cur=conn.cursor()
    cur.execute("UPDATE tickets SET status='closed', last_activity_at=? WHERE channel_id=?",
                (now_utc().isoformat(), chid))
    conn.commit(); conn.close()

# ===== 모달(동적 문구) =====
class ReasonModal(discord.ui.Modal):
    def __init__(self, title: str, label: str, placeholder: str, parent_callback):
        super().__init__(title=title, timeout=120)
        self.parent_callback = parent_callback
        self.reason = discord.ui.TextInput(
            label=label, placeholder=placeholder,
            max_length=120, required=False
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await self.parent_callback(interaction, str(self.reason).strip())
        except Exception as e:
            print("ReasonModal error:", e); print(traceback.format_exc())
            await safe_reply(interaction, "처리 중 오류가 발생했어.", ephemeral=True)

# ===== 버튼 뷰 =====
class TicketOpsView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="티켓 닫기", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close_btn(self, inter, btn): await handle_close(inter)
    @discord.ui.button(label="담당하기", style=discord.ButtonStyle.primary, custom_id="ticket_claim")
    async def claim_btn(self, inter, btn): await handle_claim(inter)

async def handle_claim(inter: discord.Interaction):
    try:
        ch=inter.channel
        if not isinstance(ch, discord.TextChannel):
            return await safe_reply(inter, "텍스트 채널에서만 가능해.", ephemeral=True)
        row=ticket_from_channel(ch.id)
        if not row: return await safe_reply(inter, "티켓 정보가 없네. 관리자에게 문의!", ephemeral=True)
        _, gid, _, _, _, _, _, status, claimed_by, _ = row
        if status!="open": return await safe_reply(inter, "닫힌 티켓은 담당 불가.", ephemeral=True)
        _, support_role_id, *_ = get_settings(gid)
        role_ok=False
        if support_role_id:
            role=inter.guild.get_role(int(support_role_id))
            if role and role in getattr(inter.user,"roles",[]): role_ok=True
        if not role_ok: return await safe_reply(inter, "스태프만 담당 가능.", ephemeral=True)
        if claimed_by and claimed_by!=inter.user.id:
            return await safe_reply(inter, "이미 다른 스태프가 담당 중.", ephemeral=True)
        conn=db(); cur=conn.cursor()
        cur.execute("UPDATE tickets SET claimed_by=?, last_activity_at=? WHERE channel_id=?",
                    (inter.user.id, now_utc().isoformat(), ch.id))
        conn.commit(); conn.close()
        await safe_reply(inter, embed=make_embed("담당자 지정", f"{inter.user.mention} 님이 담당합니다."), ephemeral=False)
    except Exception as e:
        print("handle_claim:", e); print(traceback.format_exc())
        await safe_reply(inter, "처리 중 오류가 발생했어.", ephemeral=True)

async def handle_close(inter: discord.Interaction):
    try:
        ch=inter.channel
        if not isinstance(ch, discord.TextChannel):
            return await safe_reply(inter, "텍스트 채널에서만 가능해.", ephemeral=True)
        row=ticket_from_channel(ch.id)
        if not row: return await safe_reply(inter, "티켓 정보가 없네. 관리자에게 문의!", ephemeral=True)
        ticket_id, gid, _, opener_id, *_ = row
        _, support_role_id, log_channel_id, *_ = get_settings(gid)

        is_staff=False
        if support_role_id:
            role=inter.guild.get_role(int(support_role_id))
            if role and role in getattr(inter.user,"roles",[]): is_staff=True
        if inter.user.id!=opener_id and not is_staff:
            return await safe_reply(inter, "개설자 또는 스태프만 닫을 수 있어.", ephemeral=True)

        await inter.response.defer(ephemeral=True)  # 무거운 처리 전 예약

        # 트랜스크립트
        lines=[]
        async for m in ch.history(limit=2000, oldest_first=True):
            ts=m.created_at.strftime("%Y-%m-%d %H:%M")
            author=f"{m.author}({m.author.id})"
            content=m.content or ""
            if m.attachments: content += " " + " ".join(a.url for a in m.attachments)
            lines.append(f"[{ts}] {author}: {content}")
        transcript="\n".join(lines) if lines else "내용 없음"
        file=discord.File(io.BytesIO(transcript.encode("utf-8")), filename=f"{ch.name}_transcript.txt")

        # 로그 채널
        if log_channel_id:
            log_ch=inter.guild.get_channel(int(log_channel_id))
            if isinstance(log_ch, discord.TextChannel):
                try:
                    await log_ch.send(embed=make_embed("티켓 종료", f"#{ch.name} (ID: {ticket_id})",
                                                       [("종료자", inter.user.mention, True)]), file=file)
                except Exception as e: print("log send:", e)

        try:
            await ch.send(embed=make_embed("티켓 종료", get_settings(gid)[6]))
        except: pass

        close_ticket_record(ch.id)
        try:
            await ch.edit(name=f"closed-{ch.name}", reason="티켓 닫힘")
            await ch.set_permissions(inter.guild.default_role, view_channel=False, send_messages=False)
        except: pass
        try:
            await ch.delete(reason="티켓 닫기")
        except: pass

        await inter.followup.send("티켓을 종료했어.", ephemeral=True)
    except Exception as e:
        print("handle_close:", e); print(traceback.format_exc())
        await safe_reply(inter, "종료 처리 중 오류.", ephemeral=True)

# ===== 드롭다운 → 모달 → 채널 생성 =====
class TicketTypeSelect(discord.ui.Select):
    def __init__(self, rows):
        opts=[]
        for v,label,desc,emoji,_ in rows:
            opts.append(discord.SelectOption(label=label[:100], description=(desc or "")[:100],
                                             emoji=pemoji(emoji), value=v))
        super().__init__(placeholder="선택하기", min_values=1, max_values=1, options=opts, custom_id="ticket_type_select")
        self.map = {v:(label,desc,emoji) for v,label,desc,emoji,_ in rows}

    async def callback(self, inter: discord.Interaction):
        try:
            gid=inter.guild_id
            (category_id, support_role_id, log_channel_id,
             name_fmt, open_msg, guide_msg, close_msg,
             modal_title, reason_label, reason_placeholder) = get_settings(gid)
            guild=inter.guild
            category=guild.get_channel(int(category_id)) if category_id else None
            if not isinstance(category, discord.CategoryChannel):
                return await safe_reply(inter, "카테고리가 설정되지 않았어. /티켓설정 카테고리 먼저!", ephemeral=True)

            v=self.values[0]
            label,desc,_=self.map.get(v, ("기타 문의","",None))

            async def after_reason(inter2: discord.Interaction, reason_text: str):
                try:
                    # 3초 넘을 수 있으니 예약
                    if not inter2.response.is_done():
                        await inter2.response.defer(ephemeral=True)

                    safe_user = re.sub(r'[^a-z0-9\-]', '', inter.user.name.lower().replace(' ','-')) or str(inter.user.id)
                    slug_type = slugify(label)
                    temp = name_fmt.replace("{type}", slug_type).replace("{user}", safe_user).replace("{id}","x")
                    ch_name = slugify(temp)[:90]

                    overwrites={
                        guild.default_role: discord.PermissionOverwrite(view_channel=False),
                        inter.user: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                                read_message_history=True, attach_files=True),
                    }
                    if support_role_id:
                        role=guild.get_role(int(support_role_id))
                        if role:
                            overwrites[role]=discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                                        read_message_history=True, manage_messages=True)

                    # 같은 유저가 이미 연 티켓 방지(같은 카테고리 내)
                    for ch in category.text_channels:
                        if ch.topic and ch.topic.startswith(f"opener:{inter.user.id}|"):
                            return await inter2.followup.send(f"이미 열린 티켓 있어: {ch.mention}", ephemeral=True)

                    channel = await guild.create_text_channel(
                        ch_name, category=category, overwrites=overwrites,
                        topic=f"opener:{inter.user.id}|type:{v}",
                        reason=f"티켓 생성: {label}"
                    )

                    # DB 기록
                    conn=db(); cur=conn.cursor()
                    now=now_utc().isoformat()
                    cur.execute("""INSERT INTO tickets(guild_id,channel_id,opener_id,type_value,opened_at,last_activity_at,status,claimed_by,reason)
                                   VALUES(?,?,?,?,?,?,?,?,?)""",
                                (gid, channel.id, inter.user.id, v, now, now, "open", None, reason_text))
                    conn.commit(); ticket_id=cur.lastrowid; conn.close()

                    # {id} 치환
                    if "{id}" in name_fmt:
                        new_name = name_fmt.replace("{type}",slug_type).replace("{user}",safe_user).replace("{id}",str(ticket_id))
                        new_name = slugify(new_name)[:90]
                        try: await channel.edit(name=new_name)
                        except: pass

                    # 안내 임베드
                    fields=[("유형",label,True), ("개설자",inter.user.mention,True)]
                    if reason_text: fields.append(("사유", reason_text, False))
                    fields.append(("안내", guide_msg, False))
                    ping = guild.get_role(int(support_role_id)).mention if support_role_id and guild.get_role(int(support_role_id)) else None
                    await channel.send(content=ping, embed=make_embed(open_msg, desc or "", fields), view=TicketOpsView())

                    await inter2.followup.send(f"티켓 채널이 생성됐어: {channel.mention}", ephemeral=True)
                except Exception as e:
                    print("after_reason error:", e); print(traceback.format_exc())
                    await safe_reply(inter2, "티켓 생성 중 오류.", ephemeral=True)

            # 모달 오픈(가벼우므로 바로)
            await inter.response.send_modal(ReasonModal(modal_title, reason_label, reason_placeholder, after_reason))
        except Exception as e:
            print("Select callback:", e); print(traceback.format_exc())
            await safe_reply(inter, "티켓 생성 중 오류.", ephemeral=True)

class TicketPanelView(discord.ui.View):
    def __init__(self, rows): super().__init__(timeout=None); self.add_item(TicketTypeSelect(rows))

# ===== 슬래시 그룹(핵심) =====
티켓설정 = app_commands.Group(name="티켓설정", description="티켓 설정(관리자)")
티켓유형 = app_commands.Group(name="티켓유형", description="티켓 드롭다운 항목(관리자)")

@티켓설정.command(name="카테고리", description="티켓 생성 카테고리 설정")
async def set_cat(inter: discord.Interaction, 카테고리: discord.CategoryChannel):
    if not inter.user.guild_permissions.manage_guild:
        return await safe_reply(inter, "서버 관리 권한이 필요해.", ephemeral=True)
    upsert_settings(inter.guild_id, category_id=카테고리.id)
    await safe_reply(inter, embed=make_embed("카테고리 설정 완료", f"{카테고리.mention}"), ephemeral=True)

@티켓설정.command(name="역할", description="스태프 역할 설정")
async def set_role(inter: discord.Interaction, 역할: discord.Role):
    if not inter.user.guild_permissions.manage_guild:
        return await safe_reply(inter, "서버 관리 권한이 필요해.", ephemeral=True)
    upsert_settings(inter.guild_id, support_role_id=역할.id)
    await safe_reply(inter, embed=make_embed("스태프 역할 설정 완료", f"{역할.mention}"), ephemeral=True)

@티켓설정.command(name="로그채널", description="로그/트랜스크립트 채널 설정(선택)")
async def set_log(inter: discord.Interaction, 채널: discord.TextChannel):
    if not inter.user.guild_permissions.manage_guild:
        return await safe_reply(inter, "서버 관리 권한이 필요해.", ephemeral=True)
    upsert_settings(inter.guild_id, log_channel_id=채널.id)
    await safe_reply(inter, embed=make_embed("로그 채널 설정 완료", f"{채널.mention}"), ephemeral=True)

@티켓설정.command(name="모달", description="모달 문구 설정(제목/라벨/힌트)")
@app_commands.describe(제목="모달 제목", 라벨="입력창 라벨", 힌트="입력창 플레이스홀더")
async def set_modal(inter: discord.Interaction, 제목: str = None, 라벨: str = None, 힌트: str = None):
    if not inter.user.guild_permissions.manage_guild:
        return await safe_reply(inter, "서버 관리 권한이 필요해.", ephemeral=True)
    kwargs={}
    if 제목 is not None: kwargs["modal_title"]=제목[:50]
    if 라벨 is not None: kwargs["reason_label"]=라벨[:50]
    if 힌트 is not None: kwargs["reason_placeholder"]=힌트[:100]
    if not kwargs:
        return await safe_reply(inter, "제목/라벨/힌트 중 하나 이상 입력해줘.", ephemeral=True)
    upsert_settings(inter.guild_id, **kwargs)
    await safe_reply(inter, embed=make_embed("모달 문구 설정", "\n".join([
        f"제목: {kwargs.get('modal_title','(변경 없음)')}",
        f"라벨: {kwargs.get('reason_label','(변경 없음)')}",
        f"힌트: {kwargs.get('reason_placeholder','(변경 없음)')}",
    ])), ephemeral=True)

@티켓유형.command(name="프리셋", description="로블록스 5종 프리셋 등록")
async def type_preset(inter: discord.Interaction):
    if not inter.user.guild_permissions.manage_guild:
        return await safe_reply(inter, "서버 관리 권한이 필요해.", ephemeral=True)
    presets=[
        ("item","로블록스 아이템 구매","아이템 구매 문의","<:emoji_10:1411978370635399288>",1),
        ("robux","로블록스 로벅스 구매","로벅스 구매 문의","<:emoji_11:1411978635480399963>",2),
        ("partner","파트너 / 상단 문의","파트너 문의","<a:emoji_13:1411978711544238140>",3),
        ("event","이벤트 관련","이벤트 문의","<a:emoji_12:1411978680653185055>",4),
        ("other","기타 문의","기타 문의","<:emoji_14:1411978741504282685>",5),
    ]
    for v,l,d,e,o in presets: add_type(inter.guild_id, v,l,d,e,o)
    await safe_reply(inter, embed=make_embed("프리셋 등록 완료","5종 항목이 등록됐어."), ephemeral=True)

@티켓유형.command(name="추가", description="유형 추가/수정")
async def type_add(inter: discord.Interaction, 라벨:str, 설명:str="", 이모지:str="", 값:str="", 순서:int=0):
    if not inter.user.guild_permissions.manage_guild:
        return await safe_reply(inter, "서버 관리 권한이 필요해.", ephemeral=True)
    if not 값: 값=slugify(라벨)
    if not re.fullmatch(r"[a-z0-9-]{1,50}", 값):
        return await safe_reply(inter, "값은 영소문자/숫자/하이픈 1~50자.", ephemeral=True)
    add_type(inter.guild_id, 값, 라벨[:100], 설명[:100], (이모지 or None), int(순서))
    await safe_reply(inter, embed=make_embed("유형 저장", f"{라벨} (값: {값})"), ephemeral=True)

@티켓유형.command(name="목록", description="유형 목록 보기")
async def type_list(inter: discord.Interaction):
    rows=list_types(inter.guild_id)
    if not rows:
        return await safe_reply(inter, "등록된 유형이 없어요. /티켓유형 프리셋 또는 /티켓유형 추가 먼저!", ephemeral=True)
    desc=""
    for v,label,d,e,o in rows[:25]:
        desc += f"- [{o}] {e+' ' if e else ''}{label} (값: {v}) - {d or ''}\n"
    await safe_reply(inter, embed=make_embed("유형 목록", desc or "(없음)"), ephemeral=True)

# ---- 패널/운영 ----
@bot.tree.command(name="티켓패널", description="티켓 패널 게시(관리자)")
async def ticket_panel(inter: discord.Interaction):
    try:
        await inter.response.defer(ephemeral=True)  # 예약
        rows=list_types(inter.guild_id)
        if not rows:
            return await inter.followup.send("유형이 없어요. /티켓유형 프리셋 또는 /티켓유형 추가 먼저!", ephemeral=True)
        cat,*_=get_settings(inter.guild_id)
        if not cat:
            return await inter.followup.send("카테고리 미설정. /티켓설정 카테고리 먼저!", ephemeral=True)
        await inter.followup.send(embed=make_embed("구매 & 문의","아래 드롭다운에서 항목을 선택해줘."),
                                  view=TicketPanelView(rows), ephemeral=True)
    except Exception as e:
        print("ticket_panel:", e); print(traceback.format_exc())
        await safe_reply(inter, "패널 게시 중 오류.", ephemeral=True)

@bot.tree.command(name="티켓", description="티켓 채널 운영(이름변경/우선순위)")
@app_commands.describe(액션="이름변경/우선순위", 값="새이름 또는 우선순위(low|normal|high)")
async def ticket_ops(inter: discord.Interaction, 액션:str, 값:str=""):
    ch=inter.channel
    if not isinstance(ch, discord.TextChannel):
        return await safe_reply(inter, "티켓 채널에서만 써줘.", ephemeral=True)
    row=ticket_from_channel(ch.id)
    if not row: return await safe_reply(inter, "티켓 채널이 아니야.", ephemeral=True)
    if 액션 == "이름변경":
        if not 값: return await safe_reply(inter, "새 이름을 입력해줘.", ephemeral=True)
        new_name=slugify(값)[:90]
        try:
            await ch.edit(name=new_name, reason="티켓 이름변경")
            await safe_reply(inter, embed=make_embed("이름 변경", f"#{new_name}"), ephemeral=False)
        except: await safe_reply(inter, "이름 변경 실패.", ephemeral=True)
    elif 액션 == "우선순위":
        pr=(값 or "normal").lower()
        if pr not in ("low","normal","high"):
            return await safe_reply(inter, "low/normal/high 중 하나.", ephemeral=True)
        try:
            name=ch.name
            if name.startswith(("low-","normal-","high-")):
                name = pr + "-" + name.split("-",1)[1]
            else:
                name = pr + "-" + name
            await ch.edit(name=name, reason="우선순위 변경")
            await safe_reply(inter, embed=make_embed("우선순위", pr), ephemeral=False)
        except: await safe_reply(inter, "우선순위 변경 실패.", ephemeral=True)
    else:
        await safe_reply(inter, "액션은 이름변경/우선순위 중 하나.", ephemeral=True)

# ===== 활동 시간 갱신(필요 최소) =====
@bot.event
async def on_message(message: discord.Message):
    try:
        await bot.process_commands(message)
        if message.guild and isinstance(message.channel, discord.TextChannel):
            row=ticket_from_channel(message.channel.id)
            if row and row[7]=="open":
                update_ticket_activity(message.channel.id)
    except Exception as e:
        print("on_message:", e)

# ===== 새 서버 참여: 길드 전용 설치 =====
@bot.event
async def on_guild_join(guild: discord.Guild):
    try:
        bot.tree.copy_global_to(guild=discord.Object(id=guild.id))
        synced = await bot.tree.sync(guild=discord.Object(id=guild.id))
        print(f"[AUTO SYNC] {guild.name}({guild.id}): {len(synced)}개")
    except Exception as e:
        print("[AUTO SYNC] 실패:", e)

# ===== 부트스트랩: 글로벌 비움 → 길드 전용 싱크 =====
@bot.event
async def on_ready():
    init_db()

    for grp in (티켓설정, 티켓유형):
        try: bot.tree.add_command(grp)
        except Exception as e: print(f"[TREE] 그룹 추가 스킵: {getattr(grp,'name','?')} ({e})")

    try: bot.add_view(TicketOpsView())
    except Exception as e: print("[VIEW] 등록 실패:", e)

    # 글로벌 비우고(중복 방지) → 길드만 싱크
    try:
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()  # 글로벌을 빈 상태로 동기화(삭제)
    except Exception as e:
        print("[SYNC][GLOBAL-PURGE] 예외:", e)

    try:
        if GUILD_ID:
            gid=int(GUILD_ID)
            bot.tree.copy_global_to(guild=discord.Object(id=gid))
            synced = await bot.tree.sync(guild=discord.Object(id=gid))
            print(f"[SYNC][GUILD] {gid}: {len(synced)}개")
        else:
            total=0
            for g in bot.guilds:
                bot.tree.copy_global_to(guild=discord.Object(id=g.id))
                synced = await bot.tree.sync(guild=discord.Object(id=g.id))
                print(f"[SYNC][GUILD] {g.name}({g.id}): {len(synced)}개")
                total += len(synced)
            print(f"[SYNC] 길드 합계: {total}개")
    except Exception as e:
        print("[SYNC][GUILD] 예외:", e)

    print(f"[READY] 로그인: {bot.user}")

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("환경변수 DISCORD_TOKEN 이 설정되지 않았습니다.")
    bot.run(TOKEN)
