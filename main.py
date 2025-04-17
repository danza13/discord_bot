# ------------ main.py ------------
import os
import discord
from discord.ext import commands
from aiohttp import web

# 1. Зчитуємо токен зі змінної середовища
TOKEN = os.getenv('DISCORD_TOKEN')
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN не встановлений у середовищі!")

# 2. Інтенси для команд і повідомлень
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

# 3. Простий HTTP‑сервер для Render
async def start_http_server():
    app = web.Application()
    app.add_routes([web.get('/', lambda req: web.Response(text="OK"))])
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get('PORT', 8000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

# 4. Створюємо клас бота на основі commands.Bot
class MusicBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='#', intents=intents)

    async def setup_hook(self):
        # а) запускаємо HTTP‑сервер паралельно
        self.loop.create_task(start_http_server())
        # б) підключаємо Cog з музикою
        from music_rus import Music
        await self.add_cog(Music(self))
        # в) синхронізуємо слеш‑команди
        await self.tree.sync()

    async def on_ready(self):
        print(f'✅ Logged in as {self.user} (ID: {self.user.id})')

# 5. Інстансуємо й запускаємо
bot = MusicBot()
bot.run(TOKEN)
