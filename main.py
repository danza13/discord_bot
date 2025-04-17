# ------------ main.py ------------
import os
import discord
from aiohttp import web

# 1. Зчитуємо токен зі змінної середовища
TOKEN = os.getenv('DISCORD_TOKEN')
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN не встановлений у середовищі!")

# 2. Налаштовуємо Intents
intents = discord.Intents.default()
intents.message_content = True   # щоб працювало on_message
intents.guilds = True            # щоб бот бачив сервери

# 3. HTTP‑сервер для Render
async def start_http_server():
    app = web.Application()
    app.add_routes([web.get('/', lambda req: web.Response(text="OK"))])
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get('PORT', 8000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

# 4. Визначаємо клієнта Discord
class MyClient(discord.Client):
    async def setup_hook(self):
        # Запускаємо HTTP‑сервер паралельно з ботом
        self.loop.create_task(start_http_server())

    async def on_ready(self):
        print(f'Logged on as {self.user}!')

    async def on_message(self, message):
        # Щоб бот не реагував на власні повідомлення
        if message.author == self.user:
            return
        print(f'Message from {message.author}: {message.content}')

# 5. Створюємо та запускаємо клієнта
client = MyClient(intents=intents)
client.run(TOKEN)
