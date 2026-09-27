# dakera — плагин памяти для Hermes (self-hosted Dakera)

> 🧠 Добавляет **сервер долговременной памяти Dakera** в любую установку Hermes:
> полностью локальная персистентная память между сессиями, гибридный recall,
> per-turn retain и инструменты `dakera_recall` / `dakera_store` /
> `dakera_search` / `dakera_forget`, которые агент вызывает напрямую.

---

## ✨ Возможности

### 1. Долговременная память между сессиями
- Каждый ход диалога сохраняется парой `[user]/[assistant]` на self-hosted
  сервере Dakera.
- В конце сессии последние 6 сообщений суммаризируются и сохраняются как
  `[session summary]` (importance 0.7) — будущие сессии их вспомнят.
- Единица изоляции — `agent_id`: один сервер, много агентов, ноль протечек.
  Сменил `agent_id` в конфиге → отдельная вселенная памяти.

### 2. Гибридный recall (vector + BM25)
- Режимы `recall_routing`: `auto` | `vector` | `bm25` | `hybrid` (по умолчанию
  `auto` — сервер сам выбирает стратегию под запрос).
- Векторный поиск ловит смысл («починить шину» вспоминает «replace wheel»),
  BM25 — точные идентификаторы (`err_code_XYZ`, имена функций).
- Top-K настраивается (`top_k`, по умолчанию 5).
- Блок воспоминаний инъецируется в контекст перед каждым ходом в тегах
  `<dakera-memory>` с оценками релевантности, например:
  `<dakera-memory>` `1. Факт (score 0.92)` `</dakera-memory>`

### 3. Четыре инструмента памяти для агента
Агент не просто пассивно помнит — он активно управляет своей памятью:

| Инструмент | Что делает |
|---|---|
| `dakera_recall` | семантический/гибридный поиск по долговременной памяти, возвращает `{id, content, score}` |
| `dakera_store` | явное сохранение: content + `importance` (0.0–1.0) + `memory_type` (`episodic`/`semantic`/`procedural`/`working`), теги `hermes`+`explicit` |
| `dakera_search` | лексический просмотр памяти по ключевым словам (BM25) — когда recall не нашёл или нужна инвентаризация |
| `dakera_forget` | удаление по точным `memory_ids` из результатов recall/search (GDPR-style) |

### 4. Per-turn retain (режим пар)
- `sync_turn` сохраняет пару `[user] …` + `[assistant] …` за ход с тегами
  `user`/`assistant` и метаданными `hermes_session` для трассировки.
- Шумовой фильтр: ходы с суммарным контентом <16 символов пропускаются —
  мусор вроде «ок», «угу» не засоряет память.

### 5. Неблокирующий prefetch с кэшем на один слот
- Recall выполняется в фоновом потоке (`queue_prefetch`); следующий ход
  забирает результат из кэша на один слот — сверка по точному запросу,
  при несовпадении — синхронный recall.
- Результат: инъекция памяти добавляет ~0 мс в горячий путь.

### 6. Fail-open везде
- Сервер лежит / таймаут / ошибка SDK → warning в лог, агент продолжает
  работать с пустым recall. **Память никогда не должна убивать агента.**
- Ошибки записи тоже просто логируются — диалог продолжается.

### 7. Безопасность записи по контексту агента
- Запись (auto-retain + инструменты) **отключена** для контекстов `cron`,
  `flush`, `subagent` — в долговременную память пишет только `primary` агент.
- Никакого спама памятью от фоновых задач.

### 8. Экономия ресурсов по дизайну
- `rerank` никогда не отправляется: серверный реранк стоит ~2 с на кандидата
  на небольших хостах. Гибридный роутинг даёт качество без этой задержки.
- `consolidate` никогда не вызывается — серверная компакция остаётся ручной.
- Клиентский таймаут recall 10 с (бюджет быстрого пути); store 30 с.

### 9. Секреты — по-взрослому
- API-ключ читается из secret scope (`DAKERA_API_KEY`) → env →
  `$HERMES_HOME/dakera/config.json`; конфиг пишется с правами `0600`.
- `is_available()` не ходит в сеть — только проверка конфига/креденшелов.

---

## 🚀 Установка (LLM-friendly)

**Для агентов:** выполняй шаги сверху вниз, ничего не пропускай. Все команды
безопасны для неинтерактивного запуска. В конце — проверки.

### Шаг 0 — Preflight
```bash
hermes --version                     # Hermes должен быть установлен
python3 --version                    # >= 3.10
```
Запроси у пользователя `DAKERA_API_URL` (например, `http://your-dakera-host:3003`)
и `DAKERA_API_KEY`, если они не даны. **Не угадывай домен и ключ.**

### Шаг 1 — Установка плагина
Предпочтительно (Git URL или шорткат `owner/repo` — Hermes копирует плагин в
`$HERMES_HOME/plugins/` и ставит `pip_dependencies` из манифеста):
```bash
hermes plugins install Xyzjesus/hermes-dakera   # или полный Git URL
hermes plugins list                           # dakera должен быть в списке, enabled
```
Ручная альтернатива (drop-in):
```bash
mkdir -p ~/.hermes/plugins
cp -R /path/to/hermes-dakera/plugin/dakera ~/.hermes/plugins/
# или скачать два файла напрямую:
curl -fsSLo ~/.hermes/plugins/dakera/__init__.py https://raw.githubusercontent.com/Xyzjesus/hermes-dakera/main/plugin/dakera/__init__.py
curl -fsSLo ~/.hermes/plugins/dakera/plugin.yaml https://raw.githubusercontent.com/Xyzjesus/hermes-dakera/main/plugin/dakera/plugin.yaml
hermes plugins doctor   # валидирует раскладку и манифест
```
SDK `dakera` (`dakera>=0.12.11,<0.13`) ставится автоматически через
`pip_dependencies` манифеста — ручной pip не нужен.

### Шаг 2 — Настройка креденшелов
```bash
mkdir -p ~/.hermes/dakera
umask 077
cat > ~/.hermes/dakera/config.json <<'EOF'
{"api_url": "http://YOUR-DAKERA-HOST:3000", "api_key": "YOUR-TOKEN", "agent_id": "hermes", "top_k": 5, "recall_routing": "auto"}
EOF
chmod 600 ~/.hermes/dakera/config.json
# затем впиши два плейсхолдера: api_url и api_key
```
Или задай их из переменных окружения вместо ручного редактирования:
```bash
python3 - <<'PY'
import json, pathlib, os
p = pathlib.Path.home() / ".hermes/dakera/config.json"
cfg = json.loads(p.read_text())
cfg["api_url"] = os.environ["DAKERA_API_URL"]
cfg["api_key"] = os.environ["DAKERA_API_KEY"]
p.chmod(0o600)
p.write_text(json.dumps(cfg))
PY
```
Для интерактивной установки удобнее `hermes memory setup` — он пишет тот же
файл через `get_config_schema`/`save_config` плагина.

### Шаг 3 — Активация
Создай или отредактируй `~/.hermes/config.yaml` так, чтобы он содержал:
```yaml
memory:
  provider: dakera
```
Это правка двух строк YAML — устанавливающий агент должен сделать её своими
файловыми инструментами (не `echo >>` в конфиг): добавить блок `memory:`,
если его нет, иначе поменять существующую строку `provider:`.

### Шаг 4 — Проверка
```bash
hermes memory status        # provider: dakera, active: true
hermes -z "call the dakera_store tool with content 'install test' and then dakera_recall 'install test'"
```
Ожидание: round-trip инструмента возвращает сохранённую память со score.
Проверь логи на `dakera provider init failed` — отсутствие записи значит,
что SDK подключился.

Опциональная проверка сервера без Hermes — recall должен вернуть 200:
```bash
curl -fsS -o /dev/null -w '%{http_code}\n' -X POST \
  -H "Authorization: Bearer $DAKERA_API_KEY" -H 'Content-Type: application/json' \
  -d '{"query":"ping","top_k":1,"agent_id":"hermes"}' \
  "$DAKERA_API_URL/v1/memory/recall"
```

### Удаление
```bash
hermes plugins remove dakera || rm -rf ~/.hermes/plugins/dakera
# и убери `memory.provider: dakera` из ~/.hermes/config.yaml
```
Воспоминания остаются на сервере Dakera (в скоупе `agent_id`) — удаление
плагина данные не трогает.

---

## ⚙️ Конфигурация

| Ключ | Env | По умолчанию | Описание |
|---|---|---|---|
| `api_url` | `DAKERA_API_URL` | `http://localhost:3000` | URL сервера Dakera |
| `api_key` | `DAKERA_API_KEY` | — | root- или скоуп-ключ (сначала secret scope) |
| `agent_id` | — | `hermes` | единица изоляции |
| `top_k` | — | `5` | количество результатов recall |
| `recall_routing` | — | `auto` | `auto`/`vector`/`bm25`/`hybrid` |

Порядок разрешения api_url/api_key: **env → `$HERMES_HOME/dakera/config.json` → дефолт**;
для api_key дополнительно: **сначала secret scope**, затем env, затем файл.

---

## 🧪 Разработка

```bash
pytest tests/ -q
```

Раскладка: `plugin/dakera/__init__.py` (провайдер + инструменты),
`plugin/dakera/plugin.yaml` (манифест), `tests/test_dakera_provider.py`,
`research/ru/dakera-memory-plugin.md` (дизайн-спека с цитатами источников).
