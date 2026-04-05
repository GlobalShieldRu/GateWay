#!/usr/bin/env python3
"""
GSG App Discovery Test
Создаёт соединения к 30 разным сервисам чтобы проверить отображение иконок приложений
в routeViz и device chips. Запускать с Mac'а через GSG-роутер.

Запуск: python3 test_apps_traffic.py
Стоп:   Ctrl+C
"""
import asyncio
import time
import sys

try:
    import aiohttp
except ImportError:
    print("Устанавливаю aiohttp...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "aiohttp", "-q"])
    import aiohttp

APPS = [
    # Имя             URL
    ("YouTube",       "https://www.youtube.com/"),
    ("YouTube CDN",   "https://i.ytimg.com/"),
    ("TikTok",        "https://www.tiktok.com/"),
    ("TikTok CDN",    "https://p16-sign.tiktokcdn.com/"),
    ("Telegram",      "https://telegram.org/"),
    ("Instagram",     "https://www.instagram.com/"),
    ("VK",            "https://vk.com/"),
    ("VK Video",      "https://vkvideo.ru/"),
    ("OK.ru",         "https://ok.ru/"),
    ("Kinopoisk",     "https://www.kinopoisk.ru/"),
    ("Yandex",        "https://yandex.ru/"),
    ("Yandex Music",  "https://music.yandex.ru/"),
    ("Discord",       "https://discord.com/"),
    ("Spotify",       "https://open.spotify.com/"),
    ("Twitch",        "https://www.twitch.tv/"),
    ("Netflix",       "https://www.netflix.com/"),
    ("ChatGPT",       "https://chatgpt.com/"),
    ("Claude",        "https://claude.ai/"),
    ("Gemini",        "https://gemini.google.com/"),
    ("Megogo",        "https://megogo.net/"),
    ("Apple",         "https://www.apple.com/"),
    ("iCloud",        "https://www.icloud.com/"),
    ("Skype",         "https://www.skype.com/"),
    ("Microsoft",     "https://www.microsoft.com/"),
    ("Reddit",        "https://www.reddit.com/"),
    ("Twitter/X",     "https://x.com/"),
    ("GitHub",        "https://github.com/"),
    ("Notion",        "https://www.notion.so/"),
    ("Figma",         "https://www.figma.com/"),
    ("Canva",         "https://www.canva.com/"),
]

INTERVAL   = 3.0   # секунд между запросами к одному сервису
TIMEOUT    = 8     # секунд таймаут на запрос
DURATION   = 180   # секунд работы (3 минуты)

ok_count   = 0
err_count  = 0
start_time = time.time()

async def fetch_loop(session: aiohttp.ClientSession, name: str, url: str):
    global ok_count, err_count
    while time.time() - start_time < DURATION:
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=TIMEOUT),
                allow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) GSG-Test/1.0"},
            ) as resp:
                await resp.read()
                ok_count += 1
                print(f"  ✓ {name:<16} {resp.status}  [{ok_count} ok / {err_count} err]", end="\r")
        except Exception as e:
            err_count += 1
        await asyncio.sleep(INTERVAL)

async def main():
    print(f"GSG App Discovery Test — {len(APPS)} сервисов, {DURATION}s")
    print("Соединения должны появиться в routeViz через ~5 секунд\n")

    connector = aiohttp.TCPConnector(limit=60, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [fetch_loop(session, name, url) for name, url in APPS]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass

    elapsed = time.time() - start_time
    print(f"\n\nГотово: {ok_count} успешных, {err_count} ошибок за {elapsed:.0f}s")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        elapsed = time.time() - start_time
        print(f"\n\nОстановлено: {ok_count} успешных, {err_count} ошибок за {elapsed:.0f}s")
