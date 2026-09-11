# TagPhonk V3

Telegram-бот для редактирования ID3-тегов MP3 без перекодирования аудио.

## V3

- карточка трека с реальной встроенной обложкой;
- основные ID3-теги;
- расширенные теги: BPM, publisher, copyright, ISRC, website, lyrics;
- пошаговое редактирование всех основных полей;
- undo до 10 последних изменений;
- подтверждение перед полным удалением тегов;
- очистка служебных ID3-фреймов;
- шаблоны имени готового MP3;
- авто-поиск метаданных через MusicBrainz;
- поиск обложки через Cover Art Archive;
- пакетный режим до 10 MP3;
- массовые Artist / Album / Genre / Year / Cover;
- экспорт пакетной обработки в ZIP;
- временные файлы автоматически очищаются;
- webhook защищён `X-Telegram-Bot-Api-Secret-Token`.

## Telegram limits

При использовании официального Telegram Bot API бот может скачать файл размером до 20 MB.
Обычный Bot API может отправлять документы до 50 MB, поэтому пакет ограничен примерно 45 MB.

## Render

Build:

`pip install -r requirements.txt`

Start:

`uvicorn app:api --host 0.0.0.0 --port $PORT`

Health:

`/health`

Environment:

- `BOT_TOKEN`
- `PUBLIC_URL=https://tagphonkbot.onrender.com`
- `WEBHOOK_SECRET`
- `PYTHON_VERSION=3.14.3`

## External metadata

MusicBrainz используется только для текстового поиска метаданных.
MP3-файл в MusicBrainz или Cover Art Archive не загружается.
