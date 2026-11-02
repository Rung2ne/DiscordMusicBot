import asyncio
import os
import random
import shutil
import time
from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands

from dotenv import load_dotenv
import yt_dlp

import re



load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FFMPEG_PATH = shutil.which("ffmpeg") or "ffmpeg"

intents = discord.Intents.default()
intents.message_content = True



bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

YTDL_OPTIONS = {
    'format': 'bestaudio[ext=webm]/bestaudio/best',
    'extract_flat': 'in_playlist',
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)


class Song:
    def __init__(self, title: str, webpage_url: str, duration: int, thumbnail: str, requester: discord.Member, stream_url: str = None):
        self.title = title
        self.webpage_url = webpage_url
        self.duration = duration
        self.thumbnail = thumbnail
        self.requester = requester
        self.stream_url = stream_url
        self.start_time = 0

    @property
    def duration_str(self) -> str:
        if not self.duration:
            return "라이브 / 알 수 없음"
        m, s = divmod(self.duration, 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"


def get_speed_filters(speed: float) -> list[str]:
    filters = []
    current = speed
    while current > 2.0:
        filters.append("atempo=2.0")
        current /= 2.0
    while current < 0.5:
        filters.append("atempo=0.5")
        current /= 0.5
    filters.append(f"atempo={current:.3f}")
    return filters


def build_ffmpeg_options(volume: float, speed: float, pitch: float, bassboost: bool, nightcore: bool, audio_8d: bool, seek_seconds: int = 0):
    filters = []

    vol_val = volume / 100.0
    filters.append(f"volume={vol_val:.2f}")

    rate_mult = 1.0
    tempo_mult = 1.0

    if nightcore:
        rate_mult *= 1.25

    if pitch != 0:
        p_factor = 2.0 ** (pitch / 12.0)
        rate_mult *= p_factor
        tempo_mult *= (1.0 / p_factor)

    if rate_mult != 1.0:
        target_rate = int(48000 * rate_mult)
        filters.append(f"asetrate={target_rate}")
        filters.append("aresample=48000")

    total_speed = speed * tempo_mult
    if total_speed != 1.0:
        filters.extend(get_speed_filters(total_speed))

    if bassboost:
        filters.append("bass=g=10:f=110")

    if audio_8d:
        filters.append("apulsator=hz=0.125")

    filter_str = ",".join(filters)

    before_opts = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
    if seek_seconds > 0:
        before_opts += f" -ss {seek_seconds}"

    opts = f'-vn -ar 48000 -ac 2 -b:a 192k -filter:a "{filter_str}"'
    return before_opts, opts


class GuildMusicState:
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.queue: list[Song] = []
        self.current_song: Optional[Song] = None
        self.last_song: Optional[Song] = None
        self.volume: float = 100.0
        self.speed: float = 1.0
        self.pitch: float = 0.0
        self.bassboost: bool = False
        self.nightcore: bool = False
        self.audio_8d: bool = False
        self.loop_mode: str = "off"
        self.autoplay: bool = False
        self.text_channel: Optional[discord.TextChannel] = None

        self.play_start_time: float = 0.0
        self.current_position: float = 0.0

        self.played_urls: set[str] = set()
        self.last_embed_message: Optional[discord.Message] = None

    def reset_settings(self):
        self.volume = 100.0
        self.speed = 1.0
        self.pitch = 0.0
        self.bassboost = False
        self.nightcore = False
        self.audio_8d = False
        self.loop_mode = "off"
        self.autoplay = False

    def clear_queue(self):
        self.queue.clear()
        self.current_song = None
        self.last_song = None
        self.play_start_time = 0.0
        self.current_position = 0.0
        self.played_urls.clear()

    def get_current_position(self) -> int:
        if not self.current_song or self.play_start_time == 0.0:
            return int(self.current_position)
        
        elapsed_real = time.time() - self.play_start_time
        return int(self.current_position + (elapsed_real * self.speed))

    def make_progress_bar(self, current_sec: int, total_sec: int, length: int = 15) -> str:
        if not total_sec or total_sec <= 0:
            return "🔘" + "─" * (length - 1) + " [라이브]"
        percent = min(1.0, max(0.0, current_sec / total_sec))
        filled = int(length * percent)
        bar = "─" * filled + "🔘" + "─" * (length - filled - 1)
        return bar

    def get_queue_embed(self, page: int = 1, per_page: int = 10) -> discord.Embed:
        embed = discord.Embed(title="📜 현재 대기열 목록", color=discord.Color.green())

        if self.current_song:
            curr_title = self.current_song.title[:50] + ("..." if len(self.current_song.title) > 50 else "")
            embed.add_field(
                name="▶️ 현재 재생 중",
                value=f"**[{curr_title}]({self.current_song.webpage_url})** ({self.current_song.duration_str}) | 신청자: {self.current_song.requester.mention}",
                inline=False
            )
        else:
            embed.add_field(name="▶️ 현재 재생 중", value="없음", inline=False)

        if self.queue:
            total_pages = max(1, (len(self.queue) + per_page - 1) // per_page)
            page = max(1, min(page, total_pages))

            start_idx = (page - 1) * per_page
            end_idx = start_idx + per_page
            page_queue = self.queue[start_idx:end_idx]

            queue_list = []
            for idx, song in enumerate(page_queue, start=start_idx + 1):
                song_title = song.title[:45] + ("..." if len(song.title) > 45 else "")
                queue_list.append(f"**{idx}.** [{song_title}]({song.webpage_url}) ({song.duration_str}) - {song.requester.mention}")

            queue_str = "\n".join(queue_list)
            
            if len(queue_str) > 1020:
                queue_str = queue_str[:1015] + "\n..."

            embed.add_field(name=f"🎵 다음 대기 곡 목록 (페이지 {page}/{total_pages})", value=queue_str, inline=False)
        else:
            embed.add_field(name="🎵 다음 대기 곡 목록", value="대기열이 비어 있습니다.", inline=False)

        loop_map = {"off": "꺼짐", "one": "한 곡 반복 🔂", "queue": "전체 반복 🔁"}
        embed.set_footer(text=f"반복: {loop_map[self.loop_mode]} | 자동추천: {'켜짐' if self.autoplay else '꺼짐'} | 총 {len(self.queue)}곡")
        return embed


class QueuePaginationView(discord.ui.View):
    def __init__(self, state: GuildMusicState, current_page: int = 1, per_page: int = 10):
        super().__init__(timeout=60)
        self.state = state
        self.current_page = current_page
        self.per_page = per_page
        self.total_pages = max(1, (len(self.state.queue) + per_page - 1) // per_page)
        self.update_buttons()

    def update_buttons(self):
        self.total_pages = max(1, (len(self.state.queue) + self.per_page - 1) // self.per_page)
        self.prev_button.disabled = (self.current_page <= 1)
        self.next_button.disabled = (self.current_page >= self.total_pages)

    @discord.ui.button(label="◀️ 이전", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 1:
            self.current_page -= 1
            self.update_buttons()
            embed = self.state.get_queue_embed(page=self.current_page, per_page=self.per_page)
            await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="▶️ 다음", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page < self.total_pages:
            self.current_page += 1
            self.update_buttons()
            embed = self.state.get_queue_embed(page=self.current_page, per_page=self.per_page)
            await interaction.response.edit_message(embed=embed, view=self)

music_states: dict[int, GuildMusicState] = {}


def get_state(guild_id: int) -> GuildMusicState:
    if guild_id not in music_states:
        music_states[guild_id] = GuildMusicState(guild_id)
    return music_states[guild_id]


async def apply_filter_and_restart(guild: discord.Guild):
    state = get_state(guild.id)
    vc = guild.voice_client

    if vc and (vc.is_playing() or vc.is_paused()) and state.current_song:
        current_pos = state.get_current_position()
        state.current_song.start_time = current_pos
        state.queue.insert(0, state.current_song)
        vc.stop()


class MusicControlView(discord.ui.View):
    def __init__(self, state: GuildMusicState):
        super().__init__(timeout=None)
        self.state = state

    @discord.ui.button(label="일시정지/재개", style=discord.ButtonStyle.primary, emoji="⏯️")
    async def pause_resume_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            return await interaction.response.send_message("봇이 음성 채널에 없습니다.", ephemeral=True)

        if vc.is_paused():
            self.state.play_start_time = time.time()
            vc.resume()
            await interaction.response.send_message("▶️ 재생을 재개합니다.", ephemeral=True)
        elif vc.is_playing():
            self.state.current_position = self.state.get_current_position()
            self.state.play_start_time = 0.0
            vc.pause()
            await interaction.response.send_message("⏸️ 재생을 일시정지합니다.", ephemeral=True)
        else:
            await interaction.response.send_message("현재 재생 중인 음원이 없습니다.", ephemeral=True)

    @discord.ui.button(label="스킵", style=discord.ButtonStyle.secondary, emoji="⏭️")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if not vc or not (vc.is_playing() or vc.is_paused()):
            return await interaction.response.send_message("스킵할 음원이 없습니다.", ephemeral=True)

        vc.stop()
        await interaction.response.send_message("⏭️ 곡을 스킵했습니다.", ephemeral=True)

    @discord.ui.button(label="셔플", style=discord.ButtonStyle.secondary, emoji="🔀")
    async def shuffle_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.state.queue:
            return await interaction.response.send_message("대기열이 비어있습니다.", ephemeral=True)

        random.shuffle(self.state.queue)
        await interaction.response.send_message("🔀 대기열 목록을 무작위로 섞었습니다.", ephemeral=True)

    @discord.ui.button(label="대기열", style=discord.ButtonStyle.secondary, emoji="📜")
    async def queue_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = self.state.get_queue_embed(page=1)
        view = QueuePaginationView(self.state, current_page=1)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="퇴장", style=discord.ButtonStyle.danger, emoji="⏹️")
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc and vc.is_connected():
            if self.state.last_embed_message:
                try:
                    await self.state.last_embed_message.delete()
                except Exception:
                    pass
                self.state.last_embed_message = None
            self.state.clear_queue()
            await vc.disconnect()
            await interaction.response.send_message("⏹️ 음성 채널에서 퇴장했습니다.", ephemeral=False)
        else:
            await interaction.response.send_message("봇이 음성 채널에 연결되어 있지 않습니다.", ephemeral=True)


async def fetch_stream_info(url: str):
    loop = asyncio.get_event_loop()
    ydl_single = yt_dlp.YoutubeDL({
        'format': 'bestaudio[ext=webm]/bestaudio/best',
        'quiet': True, 
        'no_warnings': True
    })
    info = await loop.run_in_executor(None, lambda: ydl_single.extract_info(url, download=False))
    return info.get('url'), info.get('title'), info.get('duration'), info.get('thumbnail')


def extract_youtube_id(url: str) -> Optional[str]:
    if not url:
        return None
    match = re.search(r"(?:v=|\/|be\/)([0-9A-Za-z_-]{11})", url)
    return match.group(1) if match else None


async def fetch_autoplay_song(state: GuildMusicState, last_song: Song, requester: discord.Member) -> Optional[Song]:
    loop = asyncio.get_event_loop()
    video_id = extract_youtube_id(last_song.webpage_url)
    
    entries = []
    
    if video_id:
        mix_url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
        try:
            data = await loop.run_in_executor(
                None, 
                lambda: ytdl.extract_info(mix_url, download=False)
            )
            if data and 'entries' in data:
                entries = [e for e in data['entries'] if e]
        except Exception as e:
            print(f"Mix 추출 실패, 키워드 검색으로 전환: {e}")

    if not entries:
        search_query = f"ytsearch10:{last_song.title} 추천"
        try:
            data = await loop.run_in_executor(
                None, 
                lambda: ytdl.extract_info(search_query, download=False)
            )
            if data and 'entries' in data:
                entries = [e for e in data['entries'] if e]
        except Exception as e:
            print(f"Autoplay 검색 실패: {e}")
            return None

    for entry in entries:
        url = entry.get('webpage_url') or f"https://www.youtube.com/watch?v={entry.get('id')}"
        title = entry.get('title', 'Unknown Title')
        
        if url in state.played_urls or url == last_song.webpage_url or title == last_song.title:
            continue

        duration = entry.get('duration', 0)
        thumbnail = entry.get('thumbnail', '')
        return Song(title=title, webpage_url=url, duration=duration, thumbnail=thumbnail, requester=requester)

    return None


async def play_next(guild: discord.Guild):
    state = get_state(guild.id)
    vc = guild.voice_client

    if not vc or not vc.is_connected():
        return

    if state.last_embed_message:
        try:
            await state.last_embed_message.delete()
        except Exception:
            pass
        state.last_embed_message = None

    if state.current_song:
        state.last_song = state.current_song
        if state.loop_mode == "one":
            state.queue.insert(0, state.current_song)
        elif state.loop_mode == "queue":
            state.queue.append(state.current_song)

    if not state.queue and state.autoplay and state.last_song:
        bot_member = guild.me
        auto_song = await fetch_autoplay_song(state, state.last_song, bot_member)
        if auto_song:
            state.queue.append(auto_song)
            if state.text_channel:
                await state.text_channel.send(f"📻 **[라디오 모드]** 다음 추천 곡이 추가되었습니다: **{auto_song.title}**", delete_after=8)

    if not state.queue:
        state.current_song = None
        state.play_start_time = 0.0
        state.current_position = 0.0
        return

    song = state.queue.pop(0)
    state.current_song = song
    state.played_urls.add(song.webpage_url)

    try:
        if not song.stream_url:
            stream_url, title, duration, thumbnail = await fetch_stream_info(song.webpage_url)
            song.stream_url = stream_url
            if title: song.title = title
            if duration: song.duration = duration
            if thumbnail: song.thumbnail = thumbnail

        before_opts, opts = build_ffmpeg_options(
            volume=state.volume,
            speed=state.speed,
            pitch=state.pitch,
            bassboost=state.bassboost,
            nightcore=state.nightcore,
            audio_8d=state.audio_8d,
            seek_seconds=song.start_time
        )
        
        state.current_position = song.start_time
        state.play_start_time = time.time()
        song.start_time = 0

        source = discord.FFmpegPCMAudio(
            song.stream_url,
            executable=FFMPEG_PATH,
            before_options=before_opts,
            options=opts
        )

        def after_playing(error):
            if error:
                print(f"재생 오류 ({guild.name}): {error}")
            coro = play_next(guild)
            asyncio.run_coroutine_threadsafe(coro, bot.loop)

        vc.play(source, after=after_playing)

        if state.text_channel:
            embed = discord.Embed(title="🎵 현재 재생 중", description=f"**[{song.title}]({song.webpage_url})**", color=discord.Color.blue())
            if song.thumbnail:
                embed.set_thumbnail(url=song.thumbnail)
            embed.add_field(name="재생 시간", value=song.duration_str, inline=True)
            embed.add_field(name="신청자", value=song.requester.mention, inline=True)

            status_text = f"볼륨: {state.volume:.0f}% | 배속: {state.speed:.1f}x | 피치: {state.pitch:+.1f} | 8D: {'ON' if state.audio_8d else 'OFF'}"
            embed.set_footer(text=status_text)

            view = MusicControlView(state)
            state.last_embed_message = await state.text_channel.send(embed=embed, view=view)

    except Exception as e:
        if state.text_channel:
            await state.text_channel.send(f"❌ 곡 재생 중 오류가 발생했습니다: `{e}`")
        await play_next(guild)


@bot.event
async def on_ready():
    print(f'{bot.user}, 켜졌어요!')
    print(f" FFmpeg 경로: {FFMPEG_PATH}")
    try:
        synced = await bot.tree.sync()
        print(f'{len(synced)}개의 슬래시 명령어가 동기화 되었을거에요!')
    except Exception as e:
        print(f"{e}개의 슬래시 명령어는 동기화에 실패햤어요...")



@bot.tree.command(name="help", description="디스코드 노래봇 명령어 및 안내를 확인합니다.")
async def help_slash(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎵 디스코드 노래봇 명령어 안내",
        description="모든 명령어는 슬래시(`/`)를 입력하여 실행할 수 있습니다.",
        color=discord.Color.blue()
    )

    embed.add_field(
        name="🎧 재생 & 음성 채널",
        value=(
            "`/play [검색어/URL]` - 음악 재생 및 재생목록 추가\n"
            "`/nowplaying` - 현재 재생 중인 음악 진행바 및 상세 정보\n"
            "`/seek [시간]` - 특정 재생 시간대로 이동 (`1:30` 또는 `90`)\n"
            "`/skip` - 현재 곡 스킵\n"
            "`/pause` / `/resume` - 음악 일시정지 / 다시 재생\n"
            "`/leave` - 음성 채널 퇴장"
        ),
        inline=False
    )

    embed.add_field(
        name="📜 대기열 & 반복 제어",
        value=(
            "`/queue` - 대기열 목록 조회\n"
            "`/loop [mode]` - 반복 재생 설정 (off / one / queue)\n"
            "`/autoplay` - 자동 추천 재생(라디오 모드) On/Off\n"
            "`/cleardupes` - 대기열 중복 곡 일괄 삭제\n"
            "`/move` / `/remove` / `/clear` / `/shuffle` - 대기열 제어"
        ),
        inline=False
    )

    embed.add_field(
        name="🎛️ 오디오 필터 및 설정 초기화",
        value=(
            "`/reset` - 모든 오디오 및 필터 설정을 기본값으로 초기화\n"
            "`/volume [0~500]` - 볼륨 조절\n"
            "`/speed [0.1~5.0]` - 재생 배속 조절\n"
            "`/pitch [-12~12]` - 음높이(키) 조절\n"
            "`/audio8d` - 8D 입체 음향 온/오프\n"
            "`/bassboost` / `/nightcore` - 이펙트 온/오프"
        ),
        inline=False
    )

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="reset", description="해당 서버의 모든 오디오 필터 및 설정을 기본값으로 리셋합니다.")
async def reset_slash(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    state.reset_settings()
    await interaction.response.send_message("🔄 서버의 모든 오디오 설정(볼륨 100%, 배속 1.0x, 피치 0, 이펙트 Off, 반복 Off)이 기본값으로 초기화되었습니다.")
    await apply_filter_and_restart(interaction.guild)


@bot.tree.command(name="play", description="유튜브 링크 또는 검색어로 음악을 재생합니다.")
@app_commands.describe(query="유튜브 URL 또는 검색하고 싶은 키워드")
async def play_slash(interaction: discord.Interaction, query: str):
    if not interaction.user.voice:
        return await interaction.response.send_message("❌ 먼저 음성 채널에 입장해 주세요.", ephemeral=True)

    await interaction.response.defer()

    voice_channel = interaction.user.voice.channel
    vc = interaction.guild.voice_client

    if not vc:
        vc = await voice_channel.connect()
    elif vc.channel != voice_channel:
        await vc.move_to(voice_channel)

    state = get_state(interaction.guild.id)
    state.text_channel = interaction.channel

    loop = asyncio.get_event_loop()
    is_url = query.startswith("http://") or query.startswith("https://")
    search_target = query if is_url else f"ytsearch1:{query}"

    try:
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(search_target, download=False))
    except Exception as e:
        return await interaction.followup.send(f"❌ 검색/추출 실패: `{e}`")

    if 'entries' in data and data['entries']:
        entries = data['entries']
        added_count = 0

        for entry in entries:
            if not entry:
                continue
            title = entry.get('title', 'Unknown Title')
            url = entry.get('url') or entry.get('webpage_url') or f"https://www.youtube.com/watch?v={entry.get('id')}"
            duration = entry.get('duration', 0)
            thumbnail = entry.get('thumbnail', '')

            song = Song(title=title, webpage_url=url, duration=duration, thumbnail=thumbnail, requester=interaction.user)
            state.queue.append(song)
            added_count += 1

        if is_url and len(entries) > 1:
            await interaction.followup.send(f"✅ 재생목록에서 **{added_count}**개 곡을 대기열에 추가했습니다.")
        else:
            first_song = state.queue[-added_count]
            await interaction.followup.send(f"✅ **[{first_song.title}]({first_song.webpage_url})** 곡을 대기열에 추가했습니다.")
    elif 'title' in data:
        title = data.get('title')
        url = data.get('webpage_url') or query
        duration = data.get('duration', 0)
        thumbnail = data.get('thumbnail', '')
        stream_url = data.get('url')

        song = Song(title=title, webpage_url=url, duration=duration, thumbnail=thumbnail, requester=interaction.user, stream_url=stream_url)
        state.queue.append(song)
        await interaction.followup.send(f"✅ **[{song.title}]({song.webpage_url})** 곡을 대기열에 추가했습니다.")
    else:
        return await interaction.followup.send("❌ 검색 결과를 찾을 수 없습니다.")

    if not vc.is_playing() and not vc.is_paused():
        await play_next(interaction.guild)


@bot.tree.command(name="nowplaying", description="현재 재생 중인 곡의 실시간 진행바와 정보를 확인합니다.")
async def nowplaying_slash(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    vc = interaction.guild.voice_client

    if not vc or not state.current_song:
        return await interaction.response.send_message("❌ 현재 재생 중인 음악이 없습니다.", ephemeral=True)

    song = state.current_song
    curr_sec = state.get_current_position()
    total_sec = song.duration

    m, s = divmod(curr_sec, 60)
    h, m = divmod(m, 60)
    curr_str = f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"

    progress_bar = state.make_progress_bar(curr_sec, total_sec)

    embed = discord.Embed(title="🎵 현재 재생 중인 음악", description=f"**[{song.title}]({song.webpage_url})**", color=discord.Color.blue())
    if song.thumbnail:
        embed.set_thumbnail(url=song.thumbnail)

    embed.add_field(name="진행 상황", value=f"`{curr_str}` {progress_bar} `{song.duration_str}`", inline=False)
    embed.add_field(name="신청자", value=song.requester.mention, inline=True)

    loop_map = {"off": "꺼짐", "one": "한 곡 반복 🔂", "queue": "전체 반복 🔁"}
    embed.set_footer(text=f"반복: {loop_map[state.loop_mode]} | 볼륨: {state.volume:.0f}% | 배속: {state.speed:.1f}x | 피치: {state.pitch:+.1f}")

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="loop", description="반복 재생 모드를 설정합니다.")
@app_commands.describe(mode="반복 모드 선택")
async def loop_slash(interaction: discord.Interaction, mode: Literal["off", "one", "queue"]):
    state = get_state(interaction.guild.id)
    state.loop_mode = mode

    loop_map = {
        "off": "❌ 반복 재생을 해제했습니다.",
        "one": "🔂 현재 곡을 **한 곡 반복**합니다.",
        "queue": "🔁 **전체 대기열**을 순환 반복합니다."
    }
    await interaction.response.send_message(loop_map[mode])


@bot.tree.command(name="autoplay", description="대기열이 비었을 때 연관 음악을 자동 추가하는 라디오 모드를 토글합니다.")
async def autoplay_slash(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    state.autoplay = not state.autoplay
    status = "켜짐(ON) 📻" if state.autoplay else "꺼짐(OFF)"
    await interaction.response.send_message(f"자동 추천 재생(라디오 모드)이 **{status}** 상태로 변경되었습니다.")


@bot.tree.command(name="cleardupes", description="대기열 내에 중복으로 들어간 노래들을 제거합니다.")
async def cleardupes_slash(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    if not state.queue:
        return await interaction.response.send_message("❌ 대기열이 비어 있습니다.", ephemeral=True)

    seen = set()
    new_queue = []
    for song in state.queue:
        if song.webpage_url not in seen:
            seen.add(song.webpage_url)
            new_queue.append(song)

    removed = len(state.queue) - len(new_queue)
    state.queue = new_queue
    await interaction.response.send_message(f"🧹 대기열에서 중복된 곡 **{removed}**개를 제거했습니다.")


@bot.tree.command(name="pitch", description="노래의 음높이(키)를 조절합니다. (-12 ~ +12 반음)")
@app_commands.describe(semitones="조절할 반음 수 (-12: 1옥타브 낮춤, +12: 1옥타브 높임)")
async def pitch_slash(interaction: discord.Interaction, semitones: float):
    if semitones < -12 or semitones > 12:
        return await interaction.response.send_message("❌ 음높이는 -12에서 +12 사이로 입력해 주세요.", ephemeral=True)

    state = get_state(interaction.guild.id)
    state.pitch = semitones
    await interaction.response.send_message(f"🎼 음높이(키)가 **{semitones:+.1f}** 반음으로 변경되었습니다.")
    await apply_filter_and_restart(interaction.guild)


@bot.tree.command(name="audio8d", description="8D 입체 음향 오디오 효과를 온/오프 합니다.")
async def audio8d_slash(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    state.audio_8d = not state.audio_8d
    status = "켜짐(ON) 🎧" if state.audio_8d else "꺼짐(OFF)"
    await interaction.response.send_message(f"🎧 8D 입체 음향 효과가 **{status}** 상태로 변경되었습니다.")
    await apply_filter_and_restart(interaction.guild)


@bot.tree.command(name="skip", description="현재 재생 중인 곡을 스킵합니다.")
async def skip_slash(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not (vc.is_playing() or vc.is_paused()):
        return await interaction.response.send_message("❌ 스킵할 곡이 없습니다.", ephemeral=True)
    vc.stop()
    await interaction.response.send_message("⏭️ 현재 곡을 스킵했습니다.")


@bot.tree.command(name="pause", description="재생 중인 음악을 일시정지합니다.")
async def pause_slash(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        state.current_position = state.get_current_position()
        state.play_start_time = 0.0
        vc.pause()
        await interaction.response.send_message("⏸️ 재생을 일시정지했습니다.")
    else:
        await interaction.response.send_message("❌ 일시정지할 곡이 없습니다.", ephemeral=True)


@bot.tree.command(name="resume", description="일시정지된 음악을 다시 재생합니다.")
async def resume_slash(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        state.play_start_time = time.time()
        vc.resume()
        await interaction.response.send_message("▶️ 재생을 다시 시작합니다.")
    else:
        await interaction.response.send_message("❌ 일시정지 상태가 아닙니다.", ephemeral=True)


@bot.tree.command(name="leave", description="음성 채널에서 퇴장하고 대기열을 초기화합니다.")
async def leave_slash(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    vc = interaction.guild.voice_client
    if vc and vc.is_connected():
        if state.last_embed_message:
            try:
                await state.last_embed_message.delete()
            except Exception:
                pass
            state.last_embed_message = None
        state.clear_queue()
        await vc.disconnect()
        await interaction.response.send_message("⏹️ 음성 채널에서 퇴장했습니다.")
    else:
        await interaction.response.send_message("❌ 음성 채널에 연결되어 있지 않습니다.", ephemeral=True)


@bot.tree.command(name="queue", description="현재 대기열 목록을 확인합니다.")
async def queue_slash(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    embed = state.get_queue_embed(page=1)
    view = QueuePaginationView(state, current_page=1)
    await interaction.response.send_message(embed=embed, view=view)


@bot.tree.command(name="move", description="대기열의 곡 순서를 변경합니다.")
@app_commands.describe(src="이동시킬 기존 곡 번호", dst="옮길 목표 위치 번호")
async def move_slash(interaction: discord.Interaction, src: int, dst: int):
    state = get_state(interaction.guild.id)
    if not state.queue:
        return await interaction.response.send_message("❌ 대기열이 비어 있습니다.", ephemeral=True)

    if src < 1 or src > len(state.queue) or dst < 1 or dst > len(state.queue):
        return await interaction.response.send_message(f"❌ 올바른 번호를 입력해 주세요. (1~{len(state.queue)})", ephemeral=True)

    song = state.queue.pop(src - 1)
    state.queue.insert(dst - 1, song)
    await interaction.response.send_message(f"🚚 **{song.title}** 곡을 대기열 {src}번에서 {dst}번으로 이동했습니다.")


@bot.tree.command(name="remove", description="대기열의 특정 번호 곡을 삭제합니다.")
@app_commands.describe(index="삭제할 곡 번호")
async def remove_slash(interaction: discord.Interaction, index: int):
    state = get_state(interaction.guild.id)
    if not state.queue:
        return await interaction.response.send_message("❌ 대기열이 비어 있습니다.", ephemeral=True)

    if index < 1 or index > len(state.queue):
        return await interaction.response.send_message(f"❌ 올바른 번호를 입력해 주세요. (1~{len(state.queue)})", ephemeral=True)

    removed = state.queue.pop(index - 1)
    await interaction.response.send_message(f"🗑️ 대기열 {index}번 곡 **{removed.title}**을(를) 삭제했습니다.")


@bot.tree.command(name="clear", description="대기열을 완전히 비웁니다.")
async def clear_slash(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    state.queue.clear()
    await interaction.response.send_message("🧹 대기열 목록을 모두 삭제했습니다.")


@bot.tree.command(name="shuffle", description="대기열 목록을 무작위로 섞습니다.")
async def shuffle_slash(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    if not state.queue:
        return await interaction.response.send_message("❌ 대기열이 비어 있습니다.", ephemeral=True)
    random.shuffle(state.queue)
    await interaction.response.send_message("🔀 대기열 목록을 무작위로 섞었습니다.")


@bot.tree.command(name="volume", description="재생 볼륨을 설정합니다 (0~500%).")
@app_commands.describe(vol="설정할 볼륨 크기 (기본: 100)")
async def volume_slash(interaction: discord.Interaction, vol: float):
    if vol < 0 or vol > 500:
        return await interaction.response.send_message("❌ 볼륨은 0%에서 500% 사이로 설정해 주세요.", ephemeral=True)

    state = get_state(interaction.guild.id)
    state.volume = vol
    await interaction.response.send_message(f"🔊 볼륨이 **{vol:.0f}%**로 변경되었습니다.")
    await apply_filter_and_restart(interaction.guild)


@bot.tree.command(name="speed", description="재생 배속을 설정합니다 (0.1~5.0배).")
@app_commands.describe(spd="설정할 재생 배속 (기본: 1.0)")
async def speed_slash(interaction: discord.Interaction, spd: float):
    if spd < 0.1 or spd > 5.0:
        return await interaction.response.send_message("❌ 배속은 0.1배에서 5.0배 사이로 설정해 주세요.", ephemeral=True)

    state = get_state(interaction.guild.id)
    state.speed = spd
    await interaction.response.send_message(f"⏩ 재생 배속이 **{spd:.1f}배**로 변경되었습니다.")
    await apply_filter_and_restart(interaction.guild)


@bot.tree.command(name="seek", description="현재 곡의 특정 시간으로 즉시 이동합니다.")
@app_commands.describe(time_str="이동할 시간 (예: 1:30 또는 90)")
async def seek_slash(interaction: discord.Interaction, time_str: str):
    state = get_state(interaction.guild.id)
    vc = interaction.guild.voice_client

    if not vc or not vc.is_playing():
        return await interaction.response.send_message("❌ 현재 재생 중인 곡이 없습니다.", ephemeral=True)

    seconds = None
    if time_str.isdigit():
        seconds = int(time_str)
    else:
        parts = time_str.split(":")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            seconds = int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit() and parts[2].isdigit():
            seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])

    if seconds is None:
        return await interaction.response.send_message("❌ 올바른 시간 형식을 입력해 주세요. (예: `1:30` 또는 `90`)", ephemeral=True)

    if state.current_song:
        state.current_song.start_time = seconds
        state.queue.insert(0, state.current_song)
        vc.stop()
        await interaction.response.send_message(f"⏩ `{time_str}` 위치로 이동합니다.")


@bot.tree.command(name="bassboost", description="베이스 부스트 필터를 온/오프 합니다.")
async def bassboost_slash(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    state.bassboost = not state.bassboost
    status = "켜짐(ON)" if state.bassboost else "꺼짐(OFF)"
    await interaction.response.send_message(f"🔊 베이스 부스트 필터가 **{status}** 상태로 변경되었습니다.")
    await apply_filter_and_restart(interaction.guild)


@bot.tree.command(name="nightcore", description="나이트코어 필터를 온/오프 합니다.")
async def nightcore_slash(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    state.nightcore = not state.nightcore
    status = "켜짐(ON)" if state.nightcore else "꺼짐(OFF)"
    await interaction.response.send_message(f"⚡ 나이트코어 필터가 **{status}** 상태로 변경되었습니다.")
    await apply_filter_and_restart(interaction.guild)


if __name__ == "__main__":
    if not TOKEN:
        print("오류: .env 파일에 DISCORD_TOKEN이 없거나 읽지 못했습니다.")
    else:
        bot.run(TOKEN)