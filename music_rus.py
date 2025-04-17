import asyncio
import functools
import itertools
import math
import random
from discord import Activity, ActivityType, app_commands
import discord
import youtube_dl
from async_timeout import timeout
from discord.ext import commands

# Silence youtube_dl bug reports
youtube_dl.utils.bug_reports_message = lambda: ''

# Custom exceptions
class VoiceError(Exception):
    pass

class YTDLError(Exception):
    pass

class YTDLSource(discord.PCMVolumeTransformer):
    YTDL_OPTIONS = {
        'format': 'bestaudio/best',
        'extractaudio': True,
        'audioformat': 'mp3',
        'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
        'restrictfilenames': True,
        'noplaylist': True,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'auto',
        'source_address': '0.0.0.0',
    }
    FFMPEG_OPTIONS = {
        'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
        'options': '-vn',
    }
    ytdl = youtube_dl.YoutubeDL(YTDL_OPTIONS)

    def __init__(self, ctx: commands.Context, source: discord.FFmpegPCMAudio, *, data: dict, volume: float = 0.5):
        super().__init__(source, volume)
        self.requester = ctx.author
        self.channel = ctx.channel
        self.data = data
        self.uploader = data.get('uploader')
        self.uploader_url = data.get('uploader_url')
        date = data.get('upload_date')
        self.upload_date = date[6:8] + '.' + date[4:6] + '.' + date[0:4]
        self.title = data.get('title')
        self.thumbnail = data.get('thumbnail')
        self.description = data.get('description')
        self.duration = self.parse_duration(int(data.get('duration')))
        self.tags = data.get('tags')
        self.url = data.get('webpage_url')
        self.views = data.get('view_count')
        self.likes = data.get('like_count')
        self.dislikes = data.get('dislike_count')
        self.stream_url = data.get('url')

    def __str__(self):
        return f"**{self.title}** від **{self.uploader}**"

    @classmethod
    async def create_source(cls, ctx: commands.Context, search: str, *, loop: asyncio.BaseEventLoop = None):
        loop = loop or asyncio.get_event_loop()
        partial = functools.partial(cls.ytdl.extract_info, search, download=False, process=False)
        data = await loop.run_in_executor(None, partial)
        if data is None:
            raise YTDLError(f"Не знайдено результатів за запитом `{search}`")
        if 'entries' in data:
            data = next((e for e in data['entries'] or [] if e), None)
            if data is None:
                raise YTDLError(f"Не знайдено результатів за запитом `{search}`")
        webpage_url = data['webpage_url']
        partial = functools.partial(cls.ytdl.extract_info, webpage_url, download=False)
        processed = await loop.run_in_executor(None, partial)
        info = processed['entries'][0] if 'entries' in processed else processed
        return cls(ctx, discord.FFmpegPCMAudio(info['url'], **cls.FFMPEG_OPTIONS), data=info)

    @staticmethod
    def parse_duration(duration: int):
        minutes, seconds = divmod(duration, 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)
        parts = []
        if days: parts.append(f"{days} дн.")
        if hours: parts.append(f"{hours} год.")
        if minutes: parts.append(f"{minutes} хв.")
        if seconds: parts.append(f"{seconds} сек.")
        return ', '.join(parts)

class Song:
    __slots__ = ('source', 'requester')
    def __init__(self, source: YTDLSource):
        self.source = source
        self.requester = source.requester

    def create_embed(self):
        embed = discord.Embed(
            title="Відтворюється:",
            description=f"**{self.source.title}**",
            color=discord.Color.blurple()
        )
        embed.add_field(name="Тривалість", value=self.source.duration)
        embed.add_field(name="Запитав", value=self.requester.mention)
        embed.set_thumbnail(url=self.source.thumbnail)
        return embed

class SongQueue(asyncio.Queue):
    def __getitem__(self, item):
        return list(itertools.islice(self._queue, item.start, item.stop)) if isinstance(item, slice) else self._queue[item]
    def __len__(self): return self.qsize()
    def clear(self): self._queue.clear()
    def shuffle(self): random.shuffle(self._queue)
    def remove(self, index: int): del self._queue[index]

class VoiceState:
    def __init__(self, bot: commands.Bot, ctx: commands.Context):
        self.bot = bot
        self._ctx = ctx
        self.current = None
        self.voice = None
        self.next = asyncio.Event()
        self.songs = SongQueue()
        self.loop = False
        self.volume = 0.5
        self.skip_votes = set()
        self.audio_player = bot.loop.create_task(self.audio_player_task())
    def __del__(self): self.audio_player.cancel()
    @property
    def is_playing(self): return self.voice and self.current
    async def audio_player_task(self):
        while True:
            self.next.clear()
            if not self.loop:
                try:
                    async with timeout(180): self.current = await self.songs.get()
                except asyncio.TimeoutError:
                    await self.stop()
                    return
            self.current.source.volume = self.volume
            self.voice.play(self.current.source, after=lambda e: self.next.set())
            await self.current.source.channel.send(embed=self.current.create_embed())
            await self.next.wait()
    async def stop(self):
        self.songs.clear()
        if self.voice: await self.voice.disconnect(); self.voice = None

class Music(commands.Cog):
    """Cog для відтворення музики через slash-команди"""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voice_states = {}

    def get_voice_state(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id if interaction.guild else None
        state = self.voice_states.get(guild_id)
        if not state:
            state = VoiceState(self.bot, interaction)
            self.voice_states[guild_id] = state
        return state

    @app_commands.command(name="join", description="Приєднати бота до вашого голосового каналу")
    async def join(self, interaction: discord.Interaction):
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("Спочатку підключіться до голосового каналу.", ephemeral=True)
            return
        channel = interaction.user.voice.channel
        state = self.get_voice_state(interaction)
        if state.voice:
            await state.voice.move_to(channel)
        else:
            state.voice = await channel.connect()
        await interaction.response.send_message(f"Підключено до {channel.name}")

    @app_commands.command(name="play", description="Відтворити або поставити в чергу трек")
    @app_commands.describe(query="URL або пошуковий запит")
    async def play(self, interaction: discord.Interaction, query: str):
        state = self.get_voice_state(interaction)
        if not state.voice:
            await self.join(interaction)
        await interaction.response.defer()
        try:
            source = await YTDLSource.create_source(interaction, query, loop=self.bot.loop)

            song = Song(source)
            await state.songs.put(song)
            await interaction.followup.send(f"Додано до черги: **{source.title}**")
        except YTDLError as e:
            await interaction.followup.send(f"Помилка: {e}")

    @app_commands.command(name="skip", description="Пропустити поточний трек")
    async def skip(self, interaction: discord.Interaction):
        state = self.get_voice_state(interaction)
        if not state.is_playing:
            await interaction.response.send_message("Нічого не відтворюється.", ephemeral=True)
            return
        state.voice.stop()
        await interaction.response.send_message("Трек пропущено.")

    @app_commands.command(name="stop", description="Зупинити відтворення та очистити чергу")
    async def stop(self, interaction: discord.Interaction):
        state = self.get_voice_state(interaction)
        await state.stop()
        await interaction.response.send_message("Відтворення зупинено, черга очищена.")

    @app_commands.command(name="queue", description="Показати поточну чергу треків")
    async def queue(self, interaction: discord.Interaction):
        state = self.get_voice_state(interaction)
        if len(state.songs) == 0:
            await interaction.response.send_message("Черга порожня.", ephemeral=True)
            return
        entries = list(state.songs._queue)
        text = "\n".join([f"{i+1}. {s.source.title}" for i, s in enumerate(entries[:10])])
        await interaction.response.send_message(f"**Черга ({len(entries)}):**\n{text}")

    @app_commands.command(name="volume", description="Змінити гучність від 0 до 100")
    @app_commands.describe(level="Рівень гучності")
    async def volume(self, interaction: discord.Interaction, level: int):
        state = self.get_voice_state(interaction)
        if not state.is_playing:
            await interaction.response.send_message("Нічого не відтворюється.", ephemeral=True)
            return
        state.volume = max(0, min(level / 100, 1))
        await interaction.response.send_message(f"Гучність встановлено на {level}%")

    @app_commands.command(name="now", description="Показати поточний трек")
    async def now(self, interaction: discord.Interaction):
        state = self.get_voice_state(interaction)
        if not state.is_playing:
            await interaction.response.send_message("Нічого не відтворюється.", ephemeral=True)
            return
        embed = state.current.create_embed()
        await interaction.response.send_message(embed=embed)

# У цьому модулі лише клас Cog, без запуску бота
