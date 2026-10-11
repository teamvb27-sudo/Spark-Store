# Spark Store – Setup (Android friendly)

1. BotFather me naya bot banao -> token = MAIN_BOT_TOKEN. (/setprivacy nahi chahiye)
2. Turso me NAYA database banao (purana Spark Live wala nahi). URL + token lo.
3. ENCRYPT_KEY aur WEBHOOK_SECRET: koi bhi lamba random text (40+ chars). Kisi ko mat batao.
4. OWNER_ID = tumhara Telegram numeric id (@userinfobot se).
5. GitHub par PRIVATE repo banao, saari files upload karo (.env kabhi nahi).
6. Render -> New Web Service -> repo chuno. Build: pip install -r requirements.txt
   Start: uvicorn app:app --host 0.0.0.0 --port $PORT
   Environment me ye daalo: MAIN_BOT_TOKEN, OWNER_ID, TURSO_DATABASE_URL, TURSO_AUTH_TOKEN,
   ENCRYPT_KEY, WEBHOOK_SECRET
   Optional: STORAGE_CHANNEL_ID (private channel, bot admin ho) = backup, LOG_CHANNEL_ID, FORCE_JOIN_CHANNEL
7. UptimeRobot -> HTTP monitor -> https://<tumhara-app>.onrender.com/health (5 min)
8. Bot me /start bhejo. Owner panel tumhe milega.
