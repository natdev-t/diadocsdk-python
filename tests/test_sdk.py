import json
import unittest
from urllib.parse import parse_qs

import httpx

from diadoc import (
    STAGING_SCOPE,
    STAGING_URL,
    DiadocClient,
    DiadocHTTPError,
    DiadocProtocolError,
    DiadocTransportError,
    OidcClient,
    OidcError,
    proto,
)


class DiadocClientTests(unittest.TestCase):
    def test_latest_routes_and_query_encoding(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(handle)
        with DiadocClient("token", base_url=STAGING_URL, transport=transport) as api:
            api.get_new_events("box", after_index_key="a+b=/&", message_type=["Letter", "Draft"], limit=10)
            api.get_security_events("box")
            api.get_documents_v3("box", "Any.Inbound", exclude_subdepartments=False)
            api.get_documents("box", {"DocumentCategory": "Inbound"})
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

    def test_json_post_message_encodes_nested_binary_fields(self):
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

        with DiadocClient("token", transport=httpx.MockTransport(handle)) as api:
            result = api.post_message({
                "FromBoxId": "sender",
                "ToBoxId": "recipient",
                "DocumentAttachments": [{
                    "TypeNamedId": "nonformalized",
                    "Comment": "Отправка",
                    "SignedContent": {"Content": b"\x00\x01", "Signature": b"\x02\x03"},
                }],
            }, operation_id="op")
        self.assertEqual(result, {"MessageId": "id"})

    def test_protobuf_round_trip(self):
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

        with DiadocClient("token", response_format="protobuf", transport=httpx.MockTransport(handle)) as api:
            self.assertEqual(api.post_message(message), response_model)
            with self.assertRaises(TypeError):
                api.post_message(proto.LoginPassword(Login="login", Password="password"))
        self.assertEqual(proto.get_enum("PrintOperationStatus").Value("Success"), 2)
        with self.assertRaisesRegex(KeyError, "Ambiguous"):
            proto.get_message("Department")
        self.assertEqual(proto.get_message("Diadoc.Api.Proto.Departments.Department").DESCRIPTOR.name, "Department")

    def test_binary_response_flags_and_pending_metadata(self):
        def handle(request):
            if request.url.path == "/GenerateTitleXml":
                self.assertEqual(request.content, b"<UserDataXml/>")
                self.assertIn(b"&disableValidation", request.url.query)
                self.assertNotIn(b"disableValidation=", request.url.query)
                return httpx.Response(200, content=b"<Title/>")
            if request.url.path == "/V2/GetLastEvent":
                return httpx.Response(204)
            return httpx.Response(200, headers={"Retry-After": "4"})

        with DiadocClient("token", transport=httpx.MockTransport(handle)) as api:
            self.assertEqual(
                api.generate_title_xml("box", "UniversalTransferDocument", "СЧФДОП", "version", 0,
                                       b"<UserDataXml/>", disable_validation=True),
                b"<Title/>",
            )
            self.assertIsNone(api.get_last_event("box"))
            self.assertIsNone(api.generate_print_form("box", "message", "doc"))
            response = api.generate_print_form("box", "message", "doc", raw=True)
            self.assertEqual(response.headers["Retry-After"], "4")

    def test_legacy_auth_uses_only_developer_key_for_login(self):
        def handle(request):
            if request.url.path == "/V3/Authenticate":
                self.assertEqual(request.headers["Authorization"], "DiadocAuth ddauth_api_client_id=key")
                self.assertEqual(request.url.params["type"], "certificate")
                self.assertEqual(request.content, b"certificate")
                return httpx.Response(200, content=b"\x80\xff")
            self.assertEqual(request.headers["Authorization"], "DiadocAuth ddauth_api_client_id=key,ddauth_token=legacy")
            return httpx.Response(200, json={"Organizations": []})

        with DiadocClient(developer_key="key", legacy_token="legacy", transport=httpx.MockTransport(handle)) as api:
            self.assertEqual(api.authenticate("certificate", body=b"certificate"), b"\x80\xff")
            self.assertEqual(api.get_my_organizations(), {"Organizations": []})
            with self.assertRaises(ValueError):
                api.request("GET", "//other.example/endpoint")

    def test_errors_preserve_response_and_do_not_retry(self):
        calls = []

        def handle(request):
            calls.append(request.url.path)
            if request.url.path == "/unavailable":
                raise httpx.ReadTimeout("timed out", request=request)
            if request.url.path == "/GetMyOrganizations":
                return httpx.Response(200, content=b"invalid JSON")
            return httpx.Response(429, text="limit", headers={"Retry-After": "2", "X-Diadoc-ErrorCode": "RateLimit"})

        with DiadocClient("token", transport=httpx.MockTransport(handle)) as api:
            with self.assertRaises(DiadocHTTPError) as caught:
                api.post_message({"FromBoxId": "box"})
            self.assertEqual(caught.exception.status_code, 429)
            self.assertEqual(caught.exception.retry_after, "2")
            self.assertEqual(caught.exception.error_code, "RateLimit")
            self.assertEqual(caught.exception.response.text, "limit")
            with self.assertRaises(DiadocProtocolError):
                api.get_my_organizations()
            with self.assertRaises(DiadocTransportError):
                api.request("GET", "/unavailable")
        self.assertEqual(calls, ["/V3/PostMessage", "/GetMyOrganizations", "/unavailable"])


class OidcTests(unittest.TestCase):
    def test_authorization_code_device_flow_and_refresh_rotation(self):
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

        with OidcClient("client", "secret&=", scopes=("openid", STAGING_SCOPE),
                        transport=httpx.MockTransport(handle)) as identity:
            redirect_uri = "https://localhost/callback"
            url = httpx.URL(identity.authorization_url(redirect_uri, state="session", nonce="nonce"))
            self.assertEqual(url.path, "/connect/authorize")
            self.assertEqual(url.params["state"], "session")
            self.assertEqual(url.params["redirect_uri"], redirect_uri)
            self.assertNotIn("client_secret", url.params)
            self.assertEqual(identity.exchange_code("code", redirect_uri), tokens)
            self.assertEqual(identity.refresh("old&=")["refresh_token"], "new")
            self.assertEqual(identity.start_device_authorization(), device)
            with self.assertRaises(OidcError) as caught:
                identity.poll_device_token(device["device_code"])
            self.assertEqual(caught.exception.error, "authorization_pending")
        self.assertEqual(forms[0]["grant_type"], ["authorization_code"])
        self.assertEqual(forms[1]["refresh_token"], ["old&="])
        self.assertEqual(forms[2]["scope"], [f"openid {STAGING_SCOPE}"])
        self.assertEqual(forms[3]["grant_type"], ["urn:ietf:params:oauth:grant-type:device_code"])


if __name__ == "__main__":
    unittest.main()
