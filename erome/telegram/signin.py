"""First-run sign-in of the userbot, in the plain console before the bot starts: QR code, cloud password, wrong account."""
from __future__ import annotations

import asyncio
import contextlib
import getpass
import io
from datetime import datetime, timezone
from typing import Callable

from telethon import TelegramClient, errors

from erome.telegram.faults import WrongAccount
from erome.telegram.names import display_name
from erome.telegram.settings import TelegramSettings
from erome.telegram.transport import new_client


async def _qr_login(client: TelegramClient, say: Callable[[str], None], attempts: int) -> None:
    import qrcode

    for attempt in range(1, attempts + 1):
        qr = await client.qr_login()
        timeout = max(0.0, (qr.expires - datetime.now(timezone.utc)).total_seconds())
        waiting = asyncio.create_task(qr.wait(timeout=timeout))
        try:
            await asyncio.sleep(0)  # let Telethon register its login handler before the code is shown
            code = qrcode.QRCode(border=2)
            code.add_data(qr.url)
            code.make(fit=True)
            buffer = io.StringIO()
            code.print_ascii(out=buffer, invert=True)
            say("")
            say("Вход в Telegram: Настройки → Устройства → Подключить устройство.")
            say("Отсканируйте QR-код аккаунтом, с которого пойдёт отправка (Ctrl+C — пропустить). Резервная ссылка:")
            say(qr.url)
            say("")
            for row in buffer.getvalue().splitlines():
                say(row)
            await waiting
            client.session.save()
            return
        except (asyncio.TimeoutError, TimeoutError):
            say(f"Срок действия QR-кода истёк, создаю новый ({attempt}/{attempts}).")
        except errors.SessionPasswordNeededError:
            password = await asyncio.to_thread(getpass.getpass, "Облачный пароль Telegram (ввод скрыт): ")
            await client.sign_in(password=password)
            client.session.save()
            return
        except errors.FloodWaitError as exc:
            raise RuntimeError(f"Telegram ограничил вход. Подождите {exc.seconds} с.") from exc
        finally:
            if not waiting.done():
                waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
    raise RuntimeError("QR-код истёк, попытки закончились.")


async def _preflight(settings: TelegramSettings, say: Callable[[str], None], attempts: int) -> str:
    client = new_client(settings, connection_retries=1, receive_updates=True)
    try:
        await asyncio.wait_for(client.connect(), 20)
        if not await asyncio.wait_for(client.is_user_authorized(), 20):
            await _qr_login(client, say, attempts)
        me = await asyncio.wait_for(client.get_me(), 20)
        if settings.account_id and int(me.id) != settings.account_id:
            say(f"Вошли под другим аккаунтом: {display_name(me)}. Нужен аккаунт с ID {settings.account_id}.")
            say("Выхожу из этой сессии, чтобы вы могли войти нужным аккаунтом.")
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.log_out(), 20)
            raise WrongAccount("вход выполнен не тем аккаунтом")
        premium = " · Premium" if getattr(me, "premium", False) else " · без Premium"
        return f"{display_name(me)}{premium}"
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()


def sign_in_if_needed(
    settings: TelegramSettings,
    say: Callable[[str], None] = print,
    *,
    attempts: int = 10,
) -> tuple[bool, str]:
    """Make sure the session is authorized. Returns (signed_in, message); never raises for network or skipped sign-in."""
    try:
        name = asyncio.run(_preflight(settings, say, attempts))
    except KeyboardInterrupt:
        return False, "вход в Telegram пропущен"
    except WrongAccount as exc:
        return False, str(exc)
    except (OSError, ConnectionError, asyncio.TimeoutError, TimeoutError) as exc:
        return False, f"нет связи с Telegram ({type(exc).__name__})"
    except Exception as exc:
        return False, f"вход в Telegram не выполнен: {exc}"
    return True, name
