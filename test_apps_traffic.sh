#!/bin/bash
# GSG App Discovery Test
# Создаёт соединения к 30 сервисам — проверяет появление иконок в routeViz и device chips
# Запуск: bash test_apps_traffic.sh
# Стоп:   Ctrl+C

INTERVAL=4    # секунд между повторными запросами
DURATION=180  # секунд работы
CURL_OPTS="-s -o /dev/null --max-time 8 -L -A 'Mozilla/5.0 GSG-Test/1.0'"

URLS=(
    "https://www.youtube.com/"
    "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"
    "https://www.tiktok.com/"
    "https://www.tiktokcdn.com/"
    "https://telegram.org/"
    "https://t.me/"
    "https://www.instagram.com/"
    "https://vk.com/"
    "https://vkvideo.ru/"
    "https://ok.ru/"
    "https://www.kinopoisk.ru/"
    "https://yandex.ru/"
    "https://music.yandex.ru/"
    "https://discord.com/"
    "https://open.spotify.com/"
    "https://www.twitch.tv/"
    "https://www.netflix.com/"
    "https://chatgpt.com/"
    "https://claude.ai/"
    "https://gemini.google.com/"
    "https://megogo.net/"
    "https://www.apple.com/"
    "https://www.icloud.com/"
    "https://www.skype.com/"
    "https://www.microsoft.com/"
    "https://www.reddit.com/"
    "https://x.com/"
    "https://github.com/"
    "https://www.notion.so/"
    "https://www.figma.com/"
)

NAMES=(
    "YouTube"      "YouTube CDN"
    "TikTok"       "TikTok CDN"
    "Telegram"     "Telegram t.me"
    "Instagram"
    "VK"           "VK Video"
    "OK.ru"
    "Kinopoisk"
    "Yandex"       "Yandex Music"
    "Discord"
    "Spotify"
    "Twitch"
    "Netflix"
    "ChatGPT"
    "Claude"
    "Gemini"
    "Megogo"
    "Apple"        "iCloud"
    "Skype"
    "Microsoft"
    "Reddit"
    "Twitter/X"
    "GitHub"
    "Notion"
    "Figma"
)

echo "╔══════════════════════════════════════════════════════╗"
echo "║       GSG App Discovery Test — ${#URLS[@]} сервисов          ║"
echo "║  Иконки появятся в routeViz через ~5 секунд          ║"
echo "║  Ctrl+C чтобы остановить (авто-стоп через ${DURATION}s)     ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

END=$((SECONDS + DURATION))
OK=0
ERR=0

loop_url() {
    local url="$1"
    local name="$2"
    local end=$3
    while [ $SECONDS -lt $end ]; do
        if curl -s -o /dev/null --max-time 8 -L \
               -A "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) GSG-Test/1.0" \
               "$url" 2>/dev/null; then
            echo "  ✓ $name"
        fi
        sleep $INTERVAL
    done
}

# Запускаем все в фоне
PIDS=()
for i in "${!URLS[@]}"; do
    loop_url "${URLS[$i]}" "${NAMES[$i]:-${URLS[$i]}}" $END &
    PIDS+=($!)
    sleep 0.1   # небольшой сдвиг чтобы не спамить одновременно
done

echo "Запущено ${#PIDS[@]} потоков. Жду ${DURATION}s..."
echo ""

# Ждём окончания или Ctrl+C
trap 'echo ""; echo "Останавливаю..."; kill "${PIDS[@]}" 2>/dev/null; exit 0' INT TERM

wait
echo ""
echo "Готово — тест завершён через ${DURATION}s"
