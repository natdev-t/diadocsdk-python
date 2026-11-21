import asyncio
import inspect
import json
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx

from diadoc import (
    STAGING_SCOPE,
    STAGING_URL,
    AsyncDiadocClient,
    AsyncOidcClient,
    AsyncRefreshTokenAuth,
    DiadocClient,
    DiadocHTTPError,
    DiadocProtocolError,
    DiadocTransportError,
    OidcClient,
    OidcError,
    RefreshTokenAuth,
    proto,
)


async def resolve(result):
    return await result if inspect.isawaitable(result) else result


@asynccontextmanager
async def open_client(client):
    if isinstance(client, (AsyncDiadocClient, AsyncOidcClient)):
        async with client:
            yield client
    else:
        with client:
            yield client


class SdkTests(unittest.IsolatedAsyncioTestCase):
    diadoc_client = DiadocClient
    oidc_client = OidcClient
    refresh_auth = RefreshTokenAuth

    async def test_latest_routes_and_query_encoding(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(handle)
        async with open_client(self.diadoc_client("token", base_url=STAGING_URL, transport=transport)) as api:
            await resolve(api.get_new_events("box", after_index_key="a+b=/&", message_type=["Letter", "Draft"], limit=10))
            await resolve(api.get_security_events("box"))
            await resolve(api.get_documents_v3("box", "Any.Inbound", exclude_subdepartments=False))
            await resolve(api.get_documents("box", {"DocumentCategory": "Inbound"}))
        events, security, documents_v3, documents_v4 = requests
        self.assertEqual(events.url.host, "diadoc-api-staging.kontur.ru")
        self.assertEqual(events.headers["Authorization"], "Bearer token")
        self.assertEqual(events.url.path, "/V8/GetNewEvents")
        self.assertEqual(dict(events.url.params), {
            "boxId": "box",
            "afterIndexKey": "a+b=/&",
            "messageType": "Letter,Draft",
            "limit": "10",
        })
        self.assertEqual(security.url.path, "/V1/GetSecurityEvents")
        self.assertEqual(documents_v3.method, "GET")
        self.assertEqual(documents_v3.url.path, "/V3/GetDocuments")
        self.assertNotIn("excludeSubdepartments", documents_v3.url.params)
        self.assertEqual(documents_v4.method, "POST")
        self.assertEqual(documents_v4.url.path, "/V4/GetDocuments")
        self.assertEqual(json.loads(documents_v4.content), {"DocumentCategory": "Inbound"})

    async def test_json_post_message_encodes_nested_binary_fields(self):
        def handle(request):
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.url.path, "/V3/PostMessage")
            self.assertEqual(request.url.params["operationId"], "op")
            self.assertEqual(request.headers["Content-Type"], "application/json; charset=utf-8")
            self.assertEqual(request.headers["Accept"], "application/json")
            payload = json.loads(request.content)
            attachment = payload["DocumentAttachments"][0]
            self.assertEqual(attachment["Comment"], "Отправка")
            self.assertEqual(attachment["SignedContent"], {"Content": "AAE=", "Signature": "AgM="})
            return httpx.Response(200, json={"MessageId": "id"})

        async with open_client(self.diadoc_client("token", transport=httpx.MockTransport(handle))) as api:
            result = await resolve(api.post_message({
                "FromBoxId": "sender",
                "ToBoxId": "recipient",
                "DocumentAttachments": [{
                    "TypeNamedId": "nonformalized",
                    "Comment": "Отправка",
                    "SignedContent": {"Content": b"\x00\x01", "Signature": b"\x02\x03"},
                }],
            }, operation_id="op"))
        self.assertEqual(result, {"MessageId": "id"})

    async def test_protobuf_round_trip(self):
        message = proto.MessageToPost(FromBoxId="sender", ToBoxId="recipient")
        response_model = proto.Message(
            MessageId="msg", FromBoxId="sender", ToBoxId="recipient", FromTitle="Sender",
            TimestampTicks=1, LastPatchTimestampTicks=1, LockMode=0, MessageType=0,
        )
        response = response_model.SerializeToString()

        def handle(request):
            self.assertEqual(request.headers["Content-Type"], "application/octet-stream")
            self.assertEqual(request.headers["Accept"], "application/octet-stream")
            self.assertEqual(proto.MessageToPost.FromString(request.content), message)
            return httpx.Response(200, content=response, headers={"Content-Type": "application/octet-stream"})

        async with open_client(self.diadoc_client(
            "token", response_format="protobuf", transport=httpx.MockTransport(handle),
        )) as api:
            self.assertEqual(await resolve(api.post_message(message)), response_model)
            with self.assertRaises(TypeError):
                await resolve(api.post_message(proto.LoginPassword(Login="login", Password="password")))
        self.assertEqual(proto.get_enum("PrintOperationStatus").Value("Success"), 2)
        with self.assertRaisesRegex(KeyError, "Ambiguous"):
            proto.get_message("Department")
        self.assertEqual(proto.get_message("Diadoc.Api.Proto.Departments.Department").DESCRIPTOR.name, "Department")

    async def test_binary_response_flags_and_pending_metadata(self):
        def handle(request):
            if request.url.path == "/GenerateTitleXml":
                self.assertEqual(request.content, b"<UserDataXml/>")
                self.assertIn(b"&disableValidation", request.url.query)
                self.assertNotIn(b"disableValidation=", request.url.query)
                return httpx.Response(200, content=b"<Title/>")
            if request.url.path == "/V2/GetLastEvent":
                return httpx.Response(204)
            return httpx.Response(200, headers={"Retry-After": "4"})

        async with open_client(self.diadoc_client("token", transport=httpx.MockTransport(handle))) as api:
            self.assertEqual(
                await resolve(api.generate_title_xml("box", "UniversalTransferDocument", "СЧФДОП", "version", 0,
                                                     b"<UserDataXml/>", disable_validation=True)),
                b"<Title/>",
            )
            self.assertIsNone(await resolve(api.get_last_event("box")))
            self.assertIsNone(await resolve(api.generate_print_form("box", "message", "doc")))
            response = await resolve(api.generate_print_form("box", "message", "doc", raw=True))
            self.assertEqual(response.headers["Retry-After"], "4")

    async def test_legacy_auth_uses_only_developer_key_for_login(self):
        def handle(request):
            if request.url.path == "/V3/Authenticate":
                self.assertEqual(request.headers["Authorization"], "DiadocAuth ddauth_api_client_id=key")
                self.assertEqual(request.url.params["type"], "certificate")
                self.assertEqual(request.content, b"certificate")
                return httpx.Response(200, content=b"\x80\xff")
            self.assertEqual(request.headers["Authorization"], "DiadocAuth ddauth_api_client_id=key,ddauth_token=legacy")
            return httpx.Response(200, json={"Organizations": []})

        async with open_client(self.diadoc_client(
            developer_key="key", legacy_token="legacy", transport=httpx.MockTransport(handle),
        )) as api:
            self.assertEqual(await resolve(api.authenticate("certificate", body=b"certificate")), b"\x80\xff")
            self.assertEqual(await resolve(api.get_my_organizations()), {"Organizations": []})
            with self.assertRaises(ValueError):
                await resolve(api.request("GET", "//other.example/endpoint"))

    async def test_errors_preserve_response_and_do_not_retry(self):
        calls = []

        def handle(request):
            calls.append(request.url.path)
            if request.url.path == "/unavailable":
                raise httpx.ReadTimeout("timed out", request=request)
            if request.url.path == "/GetMyOrganizations":
                return httpx.Response(200, content=b"invalid JSON")
            return httpx.Response(429, text="limit", headers={"Retry-After": "2", "X-Diadoc-ErrorCode": "RateLimit"})

        async with open_client(self.diadoc_client("token", transport=httpx.MockTransport(handle))) as api:
            with self.assertRaises(DiadocHTTPError) as caught:
                await resolve(api.post_message({"FromBoxId": "box"}))
            self.assertEqual(caught.exception.status_code, 429)
            self.assertEqual(caught.exception.retry_after, "2")
            self.assertEqual(caught.exception.error_code, "RateLimit")
            self.assertEqual(caught.exception.response.text, "limit")
            with self.assertRaises(DiadocProtocolError):
                await resolve(api.get_my_organizations())
            with self.assertRaises(DiadocTransportError):
                await resolve(api.request("GET", "/unavailable"))
        self.assertEqual(calls, ["/V3/PostMessage", "/GetMyOrganizations", "/unavailable"])


    async def test_authorization_code_device_flow_and_refresh_rotation(self):
        forms = []
        device = {"device_code": "device", "user_code": "user", "verification_uri": "https://example.org", "expires_in": 60}
        tokens = {"access_token": "access", "token_type": "Bearer", "expires_in": 3600, "refresh_token": "new"}

        def handle(request):
            self.assertEqual(request.url.host, "identity.kontur.ru")
            self.assertNotIn("Authorization", request.headers)
            form = parse_qs(request.content.decode("ascii"))
            forms.append(form)
            self.assertEqual(form["client_secret"], ["secret&="])
            if request.url.path == "/connect/deviceauthorization":
                return httpx.Response(200, json=device)
            if "device_code" in form:
                return httpx.Response(400, json={"error": "authorization_pending"})
            return httpx.Response(200, json=tokens)

        async with open_client(self.oidc_client(
            "client", "secret&=", scopes=("openid", STAGING_SCOPE), transport=httpx.MockTransport(handle),
        )) as identity:
            redirect_uri = "https://localhost/callback"
            url = httpx.URL(identity.authorization_url(redirect_uri, state="session", nonce="nonce"))
            self.assertEqual(url.path, "/connect/authorize")
            self.assertEqual(url.params["state"], "session")
            self.assertEqual(url.params["redirect_uri"], redirect_uri)
            self.assertNotIn("client_secret", url.params)
            self.assertEqual(await resolve(identity.exchange_code("code", redirect_uri)), tokens)
            self.assertEqual((await resolve(identity.refresh("old&=")))["refresh_token"], "new")
            self.assertEqual(await resolve(identity.start_device_authorization()), device)
            with self.assertRaises(OidcError) as caught:
                await resolve(identity.poll_device_token(device["device_code"]))
            self.assertEqual(caught.exception.error, "authorization_pending")
        self.assertEqual(forms[0]["grant_type"], ["authorization_code"])
        self.assertEqual(forms[1]["refresh_token"], ["old&="])
        self.assertEqual(forms[2]["scope"], [f"openid {STAGING_SCOPE}"])
        self.assertEqual(forms[3]["grant_type"], ["urn:ietf:params:oauth:grant-type:device_code"])

    async def test_refresh_token_auth_caches_access_and_preserves_rotated_token(self):
        exchanged = []
        saved = []
        authorization = []
        now = 0.0

        def identity_response(request):
            self.assertEqual(request.url.path, "/connect/token")
            form = parse_qs(request.content.decode("ascii"))
            exchanged.append(form.pop("refresh_token")[0])
            self.assertEqual(form, {
                "grant_type": ["refresh_token"], "client_id": ["client"], "client_secret": ["secret&="],
            })
            tokens = {"access_token": f"access-{len(exchanged)}", "expires_in": 120, "token_type": "Bearer"}
            if len(exchanged) == 1:
                tokens["refresh_token"] = "rotated"
            return httpx.Response(200, json=tokens)

        def api_response(request):
            self.assertEqual(saved, ["rotated"])
            authorization.append(request.headers["Authorization"])
            return httpx.Response(401 if request.method == "POST" else 200, json={})

        async def save_async(token):
            saved.append(token)

        save = save_async if self.refresh_auth is AsyncRefreshTokenAuth else saved.append
        async with open_client(self.oidc_client(
            "client", "secret&=", transport=httpx.MockTransport(identity_response),
        )) as identity:
            auth = self.refresh_auth(identity, "initial+", save_refresh_token=save)
            async with open_client(self.diadoc_client(auth=auth, transport=httpx.MockTransport(api_response))) as api:
                with patch("diadoc.oidc.monotonic", side_effect=lambda: now):
                    self.assertEqual(exchanged, [])
                    await resolve(api.get_my_organizations())
                    await resolve(api.request("GET", "/custom", headers={"authorization": "Bearer override"}))
                    await resolve(api.request("GET", "/custom"))
                    now = 61.0
                    with self.assertRaises(DiadocHTTPError) as caught:
                        await resolve(api.post_message({"FromBoxId": "box"}))
                    self.assertEqual(caught.exception.status_code, 401)
                    now = 122.0
                    await resolve(api.get_my_organizations())

        self.assertEqual(auth.refresh_token, "rotated")
        self.assertEqual(exchanged, ["initial+", "rotated", "rotated"])
        self.assertEqual(authorization, [
            "Bearer access-1", "Bearer override", "Bearer access-1", "Bearer access-2", "Bearer access-3",
        ])


class AsyncSdkTests(SdkTests):
    diadoc_client = AsyncDiadocClient
    oidc_client = AsyncOidcClient
    refresh_auth = AsyncRefreshTokenAuth


class AsyncLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def test_generated_methods_and_aliases_match(self):
        sync_methods = {name for name, value in vars(DiadocClient).items() if inspect.isfunction(value)}
        async_methods = {name for name, value in vars(AsyncDiadocClient).items() if inspect.iscoroutinefunction(value)}
        self.assertEqual(sync_methods, async_methods)
        for name in sync_methods:
            sync = getattr(DiadocClient, name)
            asynchronous = getattr(AsyncDiadocClient, name)
            self.assertEqual(inspect.signature(sync), inspect.signature(asynchronous))
            self.assertEqual(sync.__name__, asynchronous.__name__)

    async def test_cancelling_in_flight_request_leaves_client_usable(self):
        for factory in (AsyncDiadocClient, AsyncOidcClient):
            with self.subTest(client=factory.__name__):
                started = asyncio.Event()
                cancelled = asyncio.Event()

                async def handle(request):
                    if started.is_set():
                        return httpx.Response(200, json={"access_token": "token"})
                    started.set()
                    try:
                        await asyncio.Future()
                    finally:
                        cancelled.set()

                client, call = self.make_client(factory, httpx.MockTransport(handle))
                async with client:
                    task = asyncio.create_task(call())
                    try:
                        await asyncio.wait_for(started.wait(), timeout=2)
                    finally:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    self.assertTrue(cancelled.is_set())
                    self.assertEqual(await call(), {"access_token": "token"})

    async def test_concurrent_refresh_retries_saving_a_rotated_token(self):
        exchanges = []
        saves = []
        sent = []

        async def identity_response(request):
            exchanges.append(request)
            await asyncio.sleep(0)
            return httpx.Response(200, json={"access_token": "access", "expires_in": 120, "refresh_token": "new"})

        async def save(token):
            saves.append(token)
            if len(saves) == 1:
                raise OSError("secret store unavailable")

        def api_response(request):
            sent.append(request.headers["Authorization"])
            return httpx.Response(200, json={})

        async with AsyncOidcClient("client", "secret", transport=httpx.MockTransport(identity_response)) as identity:
            auth = AsyncRefreshTokenAuth(identity, "old", save_refresh_token=save)
            async with AsyncDiadocClient(auth=auth, transport=httpx.MockTransport(api_response)) as api:
                results = await asyncio.gather(
                    api.get_my_organizations(), api.get_my_organizations(), return_exceptions=True,
                )

        self.assertIsInstance(results[0], OSError)
        self.assertEqual(results[1], {})
        self.assertEqual(len(exchanges), 1)
        self.assertEqual(saves, ["new", "new"])
        self.assertEqual(sent, ["Bearer access"])

    async def test_timeout_configuration_and_error_cause(self):
        timeout = httpx.Timeout(5, connect=1, read=2, write=3, pool=4)

        async def handle(request):
            self.assertEqual(request.extensions["timeout"], timeout.as_dict())
            raise httpx.ReadTimeout("timed out", request=request)

        for factory in (AsyncDiadocClient, AsyncOidcClient):
            with self.subTest(client=factory.__name__):
                client, call = self.make_client(factory, httpx.MockTransport(handle), timeout=timeout)
                async with client:
                    with self.assertRaises(DiadocTransportError) as caught:
                        await call()
                    self.assertIsInstance(caught.exception.__cause__, httpx.ReadTimeout)

    async def test_close_releases_transport_on_context_exit_and_explicitly(self):
        class Transport(httpx.AsyncBaseTransport):
            entered = False
            closed = False

            async def __aenter__(self):
                self.entered = True
                return self

            async def handle_async_request(self, request):
                return httpx.Response(200, json={"access_token": "token"})

            async def aclose(self):
                self.closed = True

        for factory in (AsyncDiadocClient, AsyncOidcClient):
            for mode in ("context", "exception", "explicit"):
                with self.subTest(client=factory.__name__, mode=mode):
                    transport = Transport()
                    client, call = self.make_client(factory, transport)
                    if mode == "explicit":
                        await call()
                        await client.aclose()
                    elif mode == "exception":
                        with self.assertRaisesRegex(ValueError, "inside context"):
                            async with client:
                                await call()
                                raise ValueError("inside context")
                    else:
                        async with client:
                            await call()
                    self.assertEqual(transport.entered, mode != "explicit")
                    self.assertTrue(transport.closed)
                    with self.assertRaisesRegex(RuntimeError, "closed"):
                        await call()

    @staticmethod
    def make_client(factory, transport, **kwargs):
        if factory is AsyncDiadocClient:
            client = factory("access", transport=transport, **kwargs)
            return client, client.get_my_organizations
        client = factory("client", "secret", transport=transport, **kwargs)
        return client, lambda: client.refresh("refresh")


if __name__ == "__main__":
    unittest.main()
