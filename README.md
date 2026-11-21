# Диадок SDK для Python

Синхронный и асинхронный клиенты Контур.Диадока для Python 3.10 и новее. Каждый включает 131 HTTP-операцию для работы с организациями, контрагентами, документами, сообщениями, событиями, доверенностями, печатными формами и полкой документов.

## Установка

```sh
python -m pip install .
```

## Подключение

```python
import os

from diadoc import DiadocClient

with DiadocClient(os.environ["DIADOC_ACCESS_TOKEN"]) as api:
    result = api.get_my_organizations()
    for organization in result["Organizations"]:
        print(organization["OrgId"], organization["Boxes"])
```

Для тестовой площадки передайте `base_url=STAGING_URL` и токен со скоупом `STAGING_SCOPE`. Ключевые слова в Python используют `snake_case`, а поля JSON и Protobuf сохраняют имена из документации Диадока. Имена без суффикса используют новейшую из включенных версий метода. Например, `get_documents` соответствует `get_documents_v4`, а `get_documents_v3` сохраняет интерфейс предыдущей версии.

## Авторизация через Контур ID

Для Refresh Token Flow выберите `AuthorizationCode` в настройке `clientMode` в Кабинете интегратора. В разделе «Выпуск токенов» выберите `offline_access` и `Diadoc.PublicAPI` (или `Diadoc.PublicAPI.Staging` для тестовой площадки), подтвердите доступ от имени нужной учетной записи и получите refresh-токен. В `DIADOC_CLIENT_SECRET` передайте API-ключ приложения. [Настройка Refresh Token Flow](https://developer.kontur.ru/doc/diadoc-api/authentication#refresh_flow)

```python
import os

from diadoc import DiadocClient, OidcClient, RefreshTokenAuth

with OidcClient(os.environ["DIADOC_CLIENT_ID"], os.environ["DIADOC_CLIENT_SECRET"]) as identity:
    auth = RefreshTokenAuth(identity, os.environ["DIADOC_REFRESH_TOKEN"])
    with DiadocClient(auth=auth) as api:
        organizations = api.get_my_organizations()
```

`RefreshTokenAuth` получает `access_token` при первом запросе и обновляет его заранее по `expires_in` при следующих вызовах. Используйте один объект `auth` для запросов с одной учетной записью. Если Контур вернет новый `refresh_token`, он будет доступен в `auth.refresh_token` и использован при следующем обмене. Чтобы пережить перезапуск, передайте `save_refresh_token=...` с функцией записи токена в ваше хранилище секретов и при запуске загружайте сохраненное значение. Ошибка записи прерывает запрос к Диадоку. При следующем запросе SDK повторит запись того же токена.

Обмен происходит при вызовах API. Если refresh-токен из Кабинета интегратора не использовать 30 дней, срок его действия истечет. [Срок действия токенов](https://developer.kontur.ru/doc/diadoc-api/authentication#refresh_flow)

Для разового обмена вызовите `identity.refresh(refresh_token)` и передайте `tokens["access_token"]` в `DiadocClient`. Если ответ содержит новый `refresh_token`, сохраните его для следующего обмена.

Для Authorization Code Flow используйте `authorization_url(redirect_uri, state=...)`, проверьте `state` в обработчике перенаправления и вызовите `exchange_code(code, redirect_uri)`. Для Device Authorization Flow вызовите `start_device_authorization()`, покажите пользователю `verification_uri` и `user_code`, затем опрашивайте `poll_device_token(device_code)` с учетом `interval` и `expires_in`. Причина отказа, например `authorization_pending` или `slow_down`, доступна в `OidcError.error`. Для интеграций со старым токеном клиент принимает пару `developer_key` и `legacy_token` и предоставляет `authenticate_v3` и `authenticate_confirm_v3`. [Документация авторизации](https://developer.kontur.ru/doc/diadoc-api/authentication)

## Асинхронные клиенты

`AsyncDiadocClient` и `AsyncOidcClient` используют `httpx.AsyncClient`. Сетевые методы сохраняют имена, параметры и типы результатов синхронных методов. Вызывайте их через `await`. Метод `authorization_url()` только формирует URL и вызывается синхронно.

```python
import asyncio
import os

from diadoc import AsyncDiadocClient, AsyncOidcClient, AsyncRefreshTokenAuth


async def main():
    async with AsyncOidcClient(os.environ["DIADOC_CLIENT_ID"], os.environ["DIADOC_CLIENT_SECRET"]) as identity:
        auth = AsyncRefreshTokenAuth(identity, os.environ["DIADOC_REFRESH_TOKEN"])
        async with AsyncDiadocClient(auth=auth, timeout=15.0) as api:
            organizations = await api.get_my_organizations()
            print(organizations["Organizations"])


asyncio.run(main())
```

В `AsyncRefreshTokenAuth` передавайте асинхронную функцию `save_refresh_token`. `async with` закрывает соединения при выходе. При ручном управлении клиентом вызовите `await client.aclose()`. Передайте `timeout` в секундах или объект `httpx.Timeout`. Таймаут поднимает `DiadocTransportError` с исходным исключением HTTPX в `__cause__`. При отмене задачи `asyncio` вызов прерывается с `asyncio.CancelledError`.

## Документы и файлы

По умолчанию структурированные ответы возвращаются в виде словарей. Для структурированного запроса можно передать словарь или Protobuf-сообщение. Значения `bytes` внутри JSON-запроса автоматически кодируются в Base64. В параметрах для файлов, XML и другого двоичного содержимого передавайте исходные байты. Локальную электронную подпись формирует ваш криптопровайдер. Готовую подпись передайте в поле `Signature`. [Форматы данных API](https://developer.kontur.ru/doc/diadoc-api/techinfo)

```python
import os
from pathlib import Path
from uuid import uuid4

from diadoc import DiadocClient, STAGING_URL

# Пример тестовой отправки между двумя тестовыми ящиками.
file = Path("contract.pdf")
with DiadocClient(os.environ["DIADOC_ACCESS_TOKEN"], base_url=STAGING_URL) as api:
    message = api.post_message(
        {
            "FromBoxId": os.environ["DIADOC_FROM_BOX_ID"],
            "ToBoxId": os.environ["DIADOC_TO_BOX_ID"],
            "DocumentAttachments": [{
                "TypeNamedId": "nonformalized",
                "Metadata": [{"Key": "FileName", "Value": file.name}],
                "SignedContent": {"Content": file.read_bytes(), "SignWithTestSignature": True},
            }],
        },
        operation_id=str(uuid4()),
    )
    print(message["MessageId"])
```

Для работы с типами Protobuf задайте `response_format="protobuf"`. Типы с уникальными именами доступны в `diadoc.proto`. Для совпадающих имен используйте полное имя, например `proto.get_message("Diadoc.Api.Proto.Departments.Department")`. Пакет содержит скомпилированные схемы и устанавливается без `protoc`.

```python
import os

from diadoc import DiadocClient, proto

with DiadocClient(os.environ["DIADOC_ACCESS_TOKEN"], response_format="protobuf") as api:
    query = proto.GetDocumentsV4Request(DocumentCategory="Inbound", Count=20)
    result = api.get_documents(os.environ["DIADOC_BOX_ID"], query)
    for document in result.Documents:
        print(document.MessageId, document.EntityId)
```

Методы для PDF, XML и бинарных вложений возвращают `bytes`. Для доступа к имени файла и другим заголовкам передайте `raw=True` и используйте свойства `httpx.Response`. Ответы `204` и пустые ответы с `Retry-After` возвращают `None` при обычном вызове. Для маршрутов вне встроенного списка доступен `request(method, path, params=..., headers=..., content=...)`.

При ошибке HTTP клиент поднимает `DiadocHTTPError` со статусом, кодом Диадока, `Retry-After` и исходным ответом. Ошибки сети и декодирования доступны как `DiadocTransportError` и `DiadocProtocolError`. Повтор запроса инициирует приложение.

## Разработка

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
```

Синхронные и асинхронные методы Диадока генерируются из общего описания операций OpenAPI с дополнениями из каталога API. Protobuf-дескриптор собирается из схем C# SDK. Маршруты и параметры также сверены с SDK для Java. [OpenAPI](https://developer.kontur.ru/doc/diadoc-api/api/diadoc-api.json) · [каталог API](https://developer.kontur.ru/doc/diadoc-api/methods) · [схемы C# SDK](https://github.com/diadoc/diadocsdk-csharp/tree/e3a701f992cbf7f59fd73e9a2ac32907d8e9e5e6/proto) · [Java SDK](https://github.com/diadoc/diadocsdk-java)

Для повторной генерации используйте OpenAPI-выгрузку и ZIP-архив C# SDK. Версии источников записаны в начале `src/diadoc/_methods.py`. Флаг `--check` сверяет результат с файлами в пакете.

```sh
python tools/generate_sdk.py diadoc-api.json diadocsdk-csharp.zip --protoc protoc
```
