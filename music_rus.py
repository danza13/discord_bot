import asyncio
import functools
import itertools
import random

import discord
import yt_dlp as youtube_dl
from async_timeout import timeout
from discord import app_commands
from discord.ext import commands

# Приглушуємо повідомлення про баги в yt-dlp
youtube_dl.utils.bug_reports_message = lambda: ''

# Власні виключення
class VoiceError(Exception):
    pass

class YTDLError(Exception):
    pass

class YTDLSource(discord.PCMVolumeTransformer):
    YTDL_OPTIONS = {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'auto',
        'source_address': '0.0.0.0',
        # якщо потрібно підтримати cookies-файл (для відео з капчею):
        # 'cookiefile': 'cookies.txt',
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
        date = data.get('upload_date', '')
        if len(date) == 8:
            self.upload_date = f"{date[6:8]}.{date[4:6]}.{date[0:4]}"
        else:
            self.upload_date = "Unknown"

        self.title = data.get('title')
        self.thumbnail = data.get('thumbnail')
        self.description = data.get('description')
        self.duration = self.parse_duration(int(data.get('duration', 0)))
        self.tags = data.get('tags')
        self.url = data.get('webpage_url')
        self.views = data.get('view_count')
        self.likes = data.get('like_count')
        self.dislikes = data.get('dislike_count')

    def __str__(self):
        return f"**{self.title}** від **{self.uploader}**"

    @classmethod
    async def create_source(cls, ctx: commands.Context, search: str, *, loop: asyncio.BaseEventLoop = None):
        loop = loop or asyncio.get_event_loop()
        # Завантажуємо інформацію (оброблена) одразу
        data = await loop.run_in_executor(
            None,
            lambda: cls.ytdl.extract_info(search, download=False)
        )
        if data is None:
            raise YTDLError(f"Не знайдено результатів за запитом `{search}`")

        # Якщо це плейлист/пошук — беремо перший релевантний відео-результат
        if 'entries' in data:
            entries = [e for e in data['entries'] if e]
            if not entries:
                raise YTDLError(f"Не знайдено результатів за запитом `{search}`")
            data = entries[0]

        # Дістаємо безпосередній URL аудіопотоку
        url = data.get('url')
        if not url:
            # іноді треба повторно викликати на посилання на сторінку
            webpage_url = data.get('webpage_url')
            processed = await loop.run_in_executor(
                None,
                lambda: cls.ytdl.extract_info(webpage_url, download=False)
            )
            if 'entries' in processed:
                processed = processed['entries'][0]
            url = processed.get('url')
            if not url:
                raise YTDLError("Не вдалося отримати пряме посилання на аудіо.")

        # Створюємо PCM-джерело через FFmpeg
        ffmpeg_source = discord.FFmpegPCMAudio(url, **cls.FFMPEG_OPTIONS)
        return cls(ctx, ffmpeg_source, data=data)

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
        return ', '.join(parts) or "0 сек."

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
        if self.source.thumbnail:
            embed.set_thumbnail(url=self.source.thumbnail)
        return embed

class SongQueue(asyncio.Queue):
    def __getitem__(self, item):
        if isinstance(item, slice):
            return list(itertools.islice(self._queue, item.start or 0, item.stop))
        return self._queue[item]
    def __len__(self):
        return self.qsize()
    def clear(self):
        self._queue.clear()
    def shuffle(self):
        random.shuffle(self._queue)
    def remove(self, index: int):
        del self._queue[index]

class VoiceState:
    def __init__(self, bot: commands.Bot, ctx: commands.Context):
        self.bot = bot
        self._ctx = ctx
        self.voice = None
        self.current = None
        self.next = asyncio.Event()
        self.songs = SongQueue()
        self.loop = False
        self.volume = 0.5
        self.skip_votes = set()
        self.audio_player = bot.loop.create_task(self.player_loop())

    def __del__(self):
        self.audio_player.cancel()

    @property
    def is_playing(self):
        return self.voice and self.current

    async def player_loop(self):
        while True:
            self.next.clear()

            # Якщо не зациклюємо, чекаємо нової пісні
            if not self.loop:
                try:
                    async with timeout(180.0):
                        self.current = await self.songs.get()
                except asyncio.TimeoutError:
                    return await self.stop()

            # Відтворюємо
            self.current.source.volume = self.volume
            self.voice.play(self.current.source, after=lambda e: self.next.set())
            await self.current.source.channel.send(embed=self.current.create_embed())

            await self.next.wait()

    async def stop(self):
        self.songs.clear()
        if self.voice:
            await self.voice.disconnect()
            self.voice = None

class Music(commands.Cog):
    """Cog для відтворення музики через slash-команди"""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voice_states = {}

    def get_voice_state(self, interaction: discord.Interaction) -> VoiceState:
        gid = interaction.guild.id if interaction.guild else None
        state = self.voice_states.get(gid)
        if not state:
            state = VoiceState(self.bot, interaction)
            self.voice_states[gid] = state
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
        await interaction.response.send_message(f"Підключено до **{channel.name}**")

    @app_commands.command(name="play", description="Відтворити або поставити в чергу трек")
    @app_commands.describe(query="URL або пошуковий запит")
    async def play(self, interaction: discord.Interaction, query: str):
        state = self.get_voice_state(interaction)
        if not state.voice:
            await self.join(interaction)

        await interaction.response.defer()
        try:
            source = await YTDLSource.create_source(interaction, query)
            song = Song(source)
            await state.songs.put(song)
            await interaction.followup.send(f"Додано до черги: **{source.title}**")
        except YTDLError as e:
            await interaction.followup.send(f"❌ Помилка: {e}")

    @app_commands.command(name="skip", description="Пропустити поточний трек")
    async def skip(self, interaction: discord.Interaction):
        state = self.get_voice_state(interaction)
        if not state.is_playing:
            await interaction.response.send_message("Нічого не відтворюється.", ephemeral=True)
            return
        state.voice.stop()
        await interaction.response.send_message("⏭️ Трек пропущено.")

    @app_commands.command(name="stop", description="Зупинити відтворення та очистити чергу")
    async def stop(self, interaction: discord.Interaction):
        state = self.get_voice_state(interaction)
        await state.stop()
        await interaction.response.send_message("⏹️ Відтворення зупинено, черга очищена.")

    @app_commands.command(name="queue", description="Показати поточну чергу треків")
    async def queue(self, interaction: discord.Interaction):
        state = self.get_voice_state(interaction)
        if len(state.songs) == 0:
            await interaction.response.send_message("Черга порожня.", ephemeral=True)
            return
        entries = list(state.songs._queue)
        text = "\n".join(f"{i+1}. {s.source.title}" for i, s in enumerate(entries[:10]))
        await interaction.response.send_message(f"**Черга ({len(entries)}):**\n{text}")

    @app_commands.command(name="volume", description="Змінити гучність від 0 до 100")
    @app_commands.describe(level="Рівень гучності")
    async def volume(self, interaction: discord.Interaction, level: int):
        state = self.get_voice_state(interaction)
        if not state.is_playing:
            await interaction.response.send_message("Нічого не відтворюється.", ephemeral=True)
            return
        state.volume = max(0.0, min(level / 100.0, 1.0))
        await interaction.response.send_message(f"🔊 Гучність встановлено на {level}%")

    @app_commands.command(name="now", description="Показати поточний трек")
    async def now(self, interaction: discord.Interaction):
        state = self.get_voice_state(interaction)
        if not state.is_playing:
            await interaction.response.send_message("Нічого не відтворюється.", ephemeral=True)
            return
        embed = state.current.create_embed()
        await interaction.response.send_message(embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
