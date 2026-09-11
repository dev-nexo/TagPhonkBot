# MP3 Tag Editor Bot

Telegram-бот для редактирования ID3-тегов MP3.

Возможности:
- название;
- исполнитель;
- альбом;
- автор альбома;
- год;
- жанр;
- номер трека;
- номер диска;
- композитор;
- комментарий;
- установка и удаление обложки;
- очистка всех тегов;
- возврат готового MP3.

Аудио не перекодируется.

## Render

Build:
`pip install -r requirements.txt`

Start:
`uvicorn app:api --host 0.0.0.0 --port $PORT`

Environment:
- BOT_TOKEN
- PUBLIC_URL=https://<service>.onrender.com
- WEBHOOK_SECRET=<random secret>

Health check:
`/health`
