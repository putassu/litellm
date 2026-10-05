# Собственный LiteLLM Proxy

Конфигурация LiteLLM для запуска через Docker Compose с PostgreSQL и Redis.
Модели хранятся в базе данных и управляются через API или UI; скрипт
`sync_models.py` создаёт и обновляет deployments из `models.json`.

В Compose закреплён образ `ghcr.io/berriai/litellm:v1.103.2`. Отдельный
Dockerfile не требуется. Локальные AI-серверы и исходящий HTTP-прокси
разворачиваются отдельно.

## Файлы проекта

| Файл | Назначение |
| --- | --- |
| `docker-compose.yml` | LiteLLM, PostgreSQL, Redis и проверки готовности |
| `.env.example` | Шаблон переменных без ключей, паролей и личных адресов |
| `litellm_config.yaml` | Политика маршрутизации, fallback, cooldown и логирование |
| `models.json` | Пример моделей и лимитов для синхронизации |
| `sync_models.py` | Создание и обновление deployments через API LiteLLM |
| `nginx/litellm.conf.example` | Пример reverse proxy с префиксом `/litellm` |

## Подготовка

Нужны Docker Engine с Docker Compose и доступ контейнеров к исходящему
HTTP-прокси и локальному AI-серверу. Python 3 нужен на хосте только для
локального предварительного просмотра; синхронизация выполняется в контейнере.

Создайте локальный `.env` из шаблона.

Linux / macOS:

```sh
cp .env.example .env
chmod 600 .env
```

PowerShell:

```powershell
Copy-Item .env.example .env
```

Заполните следующие переменные:

| Переменная | Значение |
| --- | --- |
| `LOCAL_AI_IP` | IP-адрес или имя AI-сервера, доступное из контейнера |
| `OUTBOUND_PROXY_URL` | URL исходящего HTTP-прокси; при необходимости с учётными данными |
| `POSTGRES_PASSWORD` | Случайный пароль PostgreSQL из URL-безопасных символов |
| `LITELLM_MASTER_KEY` | Случайный административный ключ с префиксом `sk-` |
| `LITELLM_SALT_KEY` | Постоянный случайный ключ шифрования данных в БД |
| `UI_PASSWORD` | Отдельный случайный пароль UI |
| `GEMINI_KEY_PROJ_1`, `GEMINI_KEY_PROJ_2`, `GEMINI_KEY_PROJ_3` | API-ключи трёх Google-проектов |

Текущий Compose требует заполнения исходящего прокси и всех трёх Google-ключей.
Скрипт создаёт по три deployments для каждой облачной модели.

Для генерации одного пароля или salt можно выполнить:

```sh
python -c "import secrets; print(secrets.token_hex(32))"
```

Для master key:

```sh
python -c "import secrets; print('sk-' + secrets.token_hex(32))"
```

Каждому секрету задайте отдельное значение. `DATABASE_URL` собирается Compose
из `POSTGRES_USER`, `POSTGRES_PASSWORD` и `POSTGRES_DB`, поэтому для пароля
используйте символы `A-Z a-z 0-9 _ - . ~`.

`LITELLM_BIND_IP=127.0.0.1` оставляет порт доступным на самом сервере;
это подходит для Nginx на том же хосте. По умолчанию порт — `4000`,
`UI_USERNAME=admin`, а `SERVER_ROOT_PATH=/litellm`.
Если меняете порт или префикс, согласуйте их с конфигурацией Nginx.

При подключении существующей базы сохраните прежние `POSTGRES_USER`,
`POSTGRES_DB`, `POSTGRES_PASSWORD` и `LITELLM_SALT_KEY`. Изменение переменных
не меняет пароль уже созданной БД; смена salt может лишить доступа
к зашифрованным значениям ([документация LiteLLM](https://docs.litellm.ai/docs/proxy/deploy#core-configuration)).
После очистки проекта локальный `.env` также
содержит только шаблон: действующие значения нужно восстановить из своего
защищённого хранилища.

## Запуск

Сначала проверьте заполнение переменных и синтаксис Compose без вывода секретов:

```sh
docker compose config --quiet
docker compose pull
docker compose up -d
docker compose ps
```

После готовности LiteLLM синхронизируйте модели:

```sh
docker compose exec -T litellm python /opt/setup/sync_models.py
```

Для обновления одной модели:

```sh
docker compose exec -T litellm python /opt/setup/sync_models.py --model local-bge-m3
```

Скрипт обновляет совпадающие ID и создаёт отсутствующие deployments.
Другие модели сохраняются. Удаление модели из `models.json` само по себе
не удаляет её из базы.

Предварительный просмотр внутри контейнера использует настроенный адрес AI-сервера
и не обращается к API:

```sh
docker compose exec -T litellm python /opt/setup/sync_models.py --dry-run
```

До запуска контейнеров можно проверить структуру локально:

```sh
python sync_models.py --dry-run
```

При локальном запуске скрипт не читает `.env`: он использует переменные
окружения процесса. Без `LOCAL_AI_IP` предварительный просмотр подставляет
`127.0.0.1`; это пример адреса, а не адрес внешнего AI-сервера.

## Модели и маршрутизация

`models.json` содержит пример облачных Gemini/Gemma и локальных chat/embedding
моделей. Названия, доступность моделей и лимиты нужно проверить для своего
провайдера и аккаунта перед использованием. Числа в манифесте — настройки
этого примера, а не подтверждённые актуальные квоты Google.

Для локальных моделей задаются `port`, `provider_model` и при необходимости
`api_base_path`. По умолчанию используется путь `/v1`; для примера Infinity
он пустой. Все локальные модели используют общий `LOCAL_AI_IP`.
Значение `sk-dummy-key` в скрипте — заглушка для локальных API без авторизации.

Список облачных моделей согласуйте с `router_settings.fallbacks`
в `litellm_config.yaml`. Клиент выбирает `model_name` явно; начало цепочки
в текущем примере — `gemini-3.5-flash-lite`.

Маршрутизация задаёт порядок трёх проектов, затем переход к следующей
облачной модели. Cooldown облачных deployments настроен на 86 400 секунд,
локальных — на 30 секунд. RPD хранится в описании модели; суточную квоту
ограничивает провайдер. Кэш ответов отключён; Redis используется для состояния
маршрутизации.

## Nginx

Адаптируйте `nginx/litellm.conf.example`: замените `example.com` своим доменом,
настройте HTTPS и установите конфигурацию на сервере. Проверяйте её командой
`nginx -t` перед перезагрузкой Nginx. Пример рассчитан на Nginx на том же
хосте, где Compose публикует LiteLLM на `127.0.0.1:4000`.

В `proxy_pass` намеренно нет завершающего `/`: Nginx сохраняет префикс
`/litellm/`, совпадающий с `SERVER_ROOT_PATH`. Для потоковых ответов отключена
буферизация. Пример слушает HTTP на порту 80; TLS нужно настроить перед
внешним доступом.

После настройки HTTPS адрес UI будет `https://example.com/litellm/ui`,
а базовый адрес API — `https://example.com/litellm`. Путь UI — `/ui`
относительно базового адреса ([документация UI](https://docs.litellm.ai/docs/proxy/ui));
`SERVER_ROOT_PATH` задаёт префикс приложения
([переменные окружения](https://docs.litellm.ai/docs/proxy/config_settings)).

## Обслуживание и приватные данные

```sh
docker compose logs --tail=100 litellm
docker compose restart litellm
docker compose down
```

PostgreSQL хранит данные в `pg_data/`, Redis — в `redis_data/`.
Эти каталоги исключены из Git. Обычный `docker compose down` сохраняет их;
резервные копии базы храните отдельно в защищённом месте.

В текущей конфигурации включён `store_prompts_in_spend_logs`: содержимое
запросов может попадать в БД. Если это не требуется, установите `false`
в `litellm_config.yaml` и перезапустите LiteLLM. Не публикуйте дампы БД
и журналы: они могут содержать запросы, ключи и пользовательские данные.

`.gitignore` исключает `.env`, локальные конфигурации, секреты, сертификаты,
данные сервисов, резервные копии и журналы. Для локальной Nginx-конфигурации
можно использовать `nginx/litellm.conf`: этот путь также игнорируется.
В Git включён только очищенный `.env.example`.

Перед коммитом проверяйте состав изменений:

```sh
git status --short
git diff --cached
git check-ignore .env pg_data/ redis_data/
```

Не используйте `git add -f` для приватных файлов. Если ключ уже был опубликован
или передан посторонним, удаление из файлов не отзывает его: замените такой ключ
у провайдера. Не публикуйте вывод `docker compose config` без `--quiet`,
поскольку он содержит подставленные значения `.env`.
