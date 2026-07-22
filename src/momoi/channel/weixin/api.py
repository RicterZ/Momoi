import asyncio
import base64
import json
import secrets
import time
from typing import Any
from urllib.parse import quote, urlparse

import aiohttp
import qrcode

from .config import DEFAULT_BASE_URL, MOMOI_VERSION, WeixinConfig, WeixinState


class WeixinHTTPError(RuntimeError):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"Weixin API returned HTTP {status}")


class WeixinAPI:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str = DEFAULT_BASE_URL,
        token: str = "",
        timeout: float = 20,
    ) -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    @staticmethod
    def base_info() -> dict[str, str]:
        return {
            "channel_version": MOMOI_VERSION,
            "bot_agent": f"Momoi/{MOMOI_VERSION}",
        }

    @staticmethod
    def common_headers() -> dict[str, str]:
        parts = [
            int(part) if part.isdigit() else 0 for part in MOMOI_VERSION.split(".")[:3]
        ]
        parts += [0] * (3 - len(parts))
        client_version = (
            ((parts[0] & 255) << 16) | ((parts[1] & 255) << 8) | (parts[2] & 255)
        )
        return {
            "iLink-App-Id": "bot",
            "iLink-App-ClientVersion": str(client_version),
        }

    def headers(self, token: str | None = None) -> dict[str, str]:
        actual = self.token if token is None else token
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": base64.b64encode(
                str(secrets.randbits(32)).encode("ascii")
            ).decode("ascii"),
            **self.common_headers(),
        }
        if actual.strip():
            headers["Authorization"] = f"Bearer {actual.strip()}"
        return headers

    async def post(
        self,
        endpoint: str,
        body: dict[str, Any],
        *,
        timeout: float | None = None,
        base_url: str | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        url = f"{(base_url or self.base_url).rstrip('/')}/{endpoint.lstrip('/')}"
        async with self.session.post(
            url,
            json=body,
            headers=self.headers(token),
            timeout=aiohttp.ClientTimeout(total=timeout or self.timeout),
        ) as response:
            if response.status >= 400:
                await response.read()
                raise WeixinHTTPError(response.status)
            try:
                value = await response.json(content_type=None)
            except (json.JSONDecodeError, aiohttp.ContentTypeError) as error:
                raise RuntimeError("Weixin API returned invalid JSON") from error
        if not isinstance(value, dict):
            raise RuntimeError("Weixin API returned a non-object response")
        return value

    async def get(
        self, endpoint: str, *, timeout: float = 35, base_url: str | None = None
    ) -> dict[str, Any]:
        url = f"{(base_url or self.base_url).rstrip('/')}/{endpoint.lstrip('/')}"
        async with self.session.get(
            url,
            headers=self.common_headers(),
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as response:
            if response.status >= 400:
                await response.read()
                raise WeixinHTTPError(response.status)
            value = await response.json(content_type=None)
        if not isinstance(value, dict):
            raise RuntimeError("Weixin API returned a non-object response")
        return value


async def login(config: WeixinConfig) -> None:
    previous = WeixinState.load(config.state_path)
    timeout = aiohttp.ClientTimeout(total=None, connect=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        api = WeixinAPI(session)
        deadline = time.monotonic() + 480
        refreshes = 0
        code = ""
        poll_base = DEFAULT_BASE_URL

        async def new_qr() -> str:
            response = await api.post(
                "ilink/bot/get_bot_qrcode?bot_type=3",
                {"local_token_list": [previous.token] if previous else []},
                base_url=DEFAULT_BASE_URL,
                token="",
            )
            token = str(response.get("qrcode") or "")
            url = str(response.get("qrcode_img_content") or "")
            if not token or not url:
                raise ValueError("Weixin did not return a QR code")
            print("请用手机微信扫描以下二维码：")
            qr = qrcode.QRCode(border=1)
            qr.add_data(url)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
            print(f"二维码无法显示时，请打开：{url}")
            return token

        qr_token = await new_qr()
        while time.monotonic() < deadline:
            endpoint = f"ilink/bot/get_qrcode_status?qrcode={quote(qr_token, safe='')}"
            if code:
                endpoint += f"&verify_code={quote(code, safe='')}"
            try:
                response = await api.get(endpoint, timeout=35, base_url=poll_base)
            except (aiohttp.ClientError, asyncio.TimeoutError, WeixinHTTPError):
                await asyncio.sleep(1)
                continue
            status = str(response.get("status") or "wait")
            if status == "wait":
                await asyncio.sleep(1)
                continue
            if status == "scaned":
                code = ""
                print("已扫码，正在确认…")
            elif status == "need_verifycode":
                code = (
                    await asyncio.to_thread(input, "请输入手机微信显示的数字：")
                ).strip()
                continue
            elif status in {"expired", "verify_code_blocked"}:
                refreshes += 1
                if refreshes > 3:
                    raise ValueError("Weixin QR code expired too many times")
                code = ""
                print("二维码已失效，正在刷新…")
                qr_token = await new_qr()
                poll_base = DEFAULT_BASE_URL
                continue
            elif status == "scaned_but_redirect":
                host = str(response.get("redirect_host") or "").strip()
                if host and "/" not in host and ":" not in host:
                    poll_base = f"https://{host}"
            elif status == "binded_redirect":
                if previous is None:
                    raise ValueError(
                        "Weixin reports an existing binding, but local state is missing"
                    )
                print("微信已绑定，保留现有登录状态。")
                return
            elif status == "confirmed":
                account_id = str(response.get("ilink_bot_id") or "").strip()
                user_id = str(response.get("ilink_user_id") or "").strip()
                token = str(response.get("bot_token") or "").strip()
                base_url = str(response.get("baseurl") or DEFAULT_BASE_URL).rstrip("/")
                if not account_id or not user_id or not token:
                    raise ValueError("Weixin login response is missing credentials")
                if urlparse(base_url).scheme not in {"http", "https"}:
                    raise ValueError("Weixin login response has an invalid base URL")
                same_account = (
                    previous is not None and previous.account_id == account_id
                )
                WeixinState(
                    account_id=account_id,
                    user_id=user_id,
                    token=token,
                    base_url=base_url,
                    get_updates_buf=previous.get_updates_buf if same_account else "",
                    context_token=previous.context_token if same_account else "",
                ).save(config.state_path)
                print("微信登录成功。")
                return
            await asyncio.sleep(1)
    raise ValueError("Weixin login timed out")
