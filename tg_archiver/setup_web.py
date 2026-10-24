"""Local web workflows sharing the running application's collector and database."""
import asyncio
from pathlib import Path
import secrets
import time

from aiohttp import web
from telethon import errors

from .db import Setting
from .importer import import_export


class Setup:
    def __init__(self, db, config, collector):
        self.db, self.config, self.collector = db, config, collector
        self.csrf = secrets.token_urlsafe(32)
        self.task = None
        self.expiry = None
        self.closing = False
        self.paused = False
        self.step = "phone"
        self.phone = self.code_hash = None
        self.path = None
        self.preview = None
        self.result = None
        self.notice = self.error = ""
        self.cooldown = 0

    @property
    def busy(self):
        return self.task is not None and not self.task.done()

    async def page(self, request):
        from .web import render_page
        return await render_page(request, "Settings", "settings.html", "settings", setup=self)

    async def pause(self):
        if not self.paused:
            await self.collector.stop()
            self.paused = True

    async def resume(self):
        if self.paused and not self.closing:
            await self.collector.start()
            self.paused = False

    def clear_login(self):
        self.phone = self.code_hash = None
        self.step = "phone"
        if self.expiry:
            self.expiry.cancel()
            self.expiry = None

    def expire_login(self):
        if self.expiry:
            self.expiry.cancel()
        self.expiry = asyncio.get_running_loop().call_later(300, self.expired)

    def expired(self):
        if self.busy:
            self.expiry = asyncio.get_running_loop().call_later(30, self.expired)
            return
        self.task = asyncio.create_task(self.cancel_login(expired=True))

    async def cancel_login(self, expired=False):
        self.clear_login()
        await self.resume()
        self.notice = "Sign-in expired. Please try again." if expired else "Sign-in cancelled."

    async def post(self, request):
        data = await request.post()
        token = data.get("csrf", "")
        if not isinstance(token, str) or not secrets.compare_digest(token, self.csrf):
            raise web.HTTPForbidden(text="Refresh Settings and try again.")
        if request.headers.get("Origin") not in (None, f"{request.scheme}://{request.host}"):
            raise web.HTTPForbidden(text="Cross-origin requests are not allowed.")
        if self.busy:
            raise web.HTTPConflict(text="Another operation is in progress.")
        action = data.get("action")
        if action not in {"phone", "code", "password", "cancel", "preview", "import"}:
            raise web.HTTPBadRequest(text="Unknown settings action")
        if action != "cancel" and time.monotonic() < self.cooldown:
            raise web.HTTPTooManyRequests(text="Telegram requested a wait. Please try again later.")
        if action in {"preview", "import"} and self.paused:
            raise web.HTTPConflict(text="Finish or cancel sign-in before importing.")
        if action in {"code", "password"} and self.step != action:
            raise web.HTTPBadRequest(text="This sign-in step has expired. Refresh Settings.")
        if action == "phone" and self.step != "phone":
            raise web.HTTPConflict(text="Cancel the current sign-in first.")
        value = data.get({"phone": "phone", "code": "code", "password": "password", "preview": "path"}.get(action, ""), "")
        if not isinstance(value, str) or len(value) > 4096:
            raise web.HTTPBadRequest(text="Invalid form value")
        if action in {"phone", "code", "password", "preview"} and not value.strip():
            raise web.HTTPBadRequest(text="A value is required")
        if action == "import" and (self.path is None or self.preview is None):
            raise web.HTTPBadRequest(text="Preview an export before importing.")
        self.error = self.notice = ""
        self.task = asyncio.create_task(self.perform(action, value))
        raise web.HTTPSeeOther("/settings")

    async def perform(self, action, value):
        try:
            if action == "cancel":
                await self.cancel_login()
            elif action in {"preview", "import"}:
                await self.pause()
                try:
                    if action == "preview":
                        self.path = None
                        self.preview = None
                        path = Path(value.strip()).expanduser().resolve()
                        self.preview = await import_export(self.config, path, dry_run=True)
                        self.path = path
                        self.result = None
                        self.notice = "Preview ready. Review the counts, then import."
                    else:
                        self.result = await import_export(self.config, self.path, db=self.db)
                        self.preview = None
                        self.path = None
                        self.notice = "Import complete. Collection has resumed."
                finally:
                    await self.resume()
            else:
                await self.login(action, value)
        except errors.SessionPasswordNeededError:
            self.step = "password"
            self.expire_login()
        except errors.FloodWaitError as error:
            self.cooldown = time.monotonic() + error.seconds
            self.error = f"Telegram requested a wait of {error.seconds} seconds."
        except (errors.PhoneCodeInvalidError, errors.PasswordHashInvalidError):
            self.error = "Incorrect code or password. Please try again."
        except errors.PhoneCodeExpiredError:
            self.clear_login()
            await self.resume()
            self.error = "The code expired. Request a new code."
        except (ValueError, FileNotFoundError, IsADirectoryError) as error:
            self.error = str(error) if isinstance(error, ValueError) else "Export JSON file not found or unreadable."
            if action in {"phone", "code", "password"}:
                self.clear_login()
                await self.resume()
        except Exception as error:
            # Telegram exceptions can contain credentials; never render or log their text.
            self.error = f"Operation failed ({type(error).__name__}). Please try again."
            if action in {"phone", "code", "password"}:
                self.clear_login()
                await self.resume()
        finally:
            if action in {"phone", "code", "password"} and self.paused:
                self.expire_login()

    async def login(self, action, value):
        client = self.collector.client
        if action == "phone":
            await self.pause()
            await client.connect()
            if await client.is_user_authorized():
                await self.authorized()
                return
            self.phone = value.strip()
            sent = await client.send_code_request(self.phone)
            self.code_hash = sent.phone_code_hash
            self.step = "code"
            self.notice = "Enter the code sent by Telegram."
        elif action == "code":
            await client.sign_in(phone=self.phone, code=value.strip(), phone_code_hash=self.code_hash)
            await self.authorized()
        elif action == "password":
            await client.sign_in(password=value)
            await self.authorized()

    async def authorized(self):
        me = await self.collector.client.get_me()
        if not me or me.bot:
            raise ValueError("A personal Telegram account is required")
        async with self.db.write() as session:
            owner = await session.get(Setting, "account_id")
            if owner and owner.value != str(me.id):
                raise ValueError("Data directory belongs to a different Telegram account")
            if not owner:
                session.add(Setting(key="account_id", value=str(me.id)))
        self.clear_login()
        await self.resume()
        self.notice = "Signed in. Collection has resumed."

    async def cleanup(self, app):
        self.closing = True
        if self.expiry:
            self.expiry.cancel()
        # Let an active batch finish before the app closes its shared database.
        if self.task:
            await asyncio.gather(self.task, return_exceptions=True)
        self.clear_login()
