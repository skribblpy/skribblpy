"""Minimal Engine.IO 4 / Socket.IO default-namespace WebSocket transport."""

from typing import Optional
from json import dumps, loads
from asyncio import Lock, timeout
from dataclasses import dataclass
from urllib.parse import urlsplit
from skribblpy.errors import Disconnected, ProtocolError, MatchmakingError
from skribblpy.constants import MAX_SOCKET_PAYLOAD, MAX_MATCHMAKER_RESPONSE
from aiohttp import WSMsgType, ClientError, ClientSession, ClientWebSocketResponse


@dataclass(frozen=True, slots=True)
class Shard:
    origin: str
    path: str
    websocket: str

    @classmethod
    def parse(cls, value: str):
        url = urlsplit(value.strip())
        hostname = url.hostname
        if (
            url.scheme not in ('http', 'https')
            or not hostname
            or not url.port
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in ('', '/')
        ):
            raise ProtocolError('Expected a shard URL with an HTTP(S) host and port')
        host = f'[{hostname}]' if ':' in hostname else hostname
        path = f'/{url.port}/'
        scheme = 'wss' if url.scheme == 'https' else 'ws'
        return cls(
            f'{url.scheme}://{host}', path, f'{scheme}://{host}{path}?EIO=4&transport=websocket'
        )


class Socket:
    def __init__(self, session: ClientSession, *, base_url: str, connect_timeout: float):
        self.session = session
        self.base_url = base_url.rstrip('/')
        self.connect_timeout = connect_timeout
        self.websocket: Optional[ClientWebSocketResponse] = None
        self.max_payload = MAX_SOCKET_PAYLOAD
        self.heartbeat_timeout = connect_timeout
        self._write_lock = Lock()

    async def connect(self, language: str, lobby_id: str):
        async with timeout(self.connect_timeout):
            form = {'id': lobby_id} if lobby_id else {'lang': language}
            async with self.session.post(f'{self.base_url}/api/play', data=form) as response:
                if not 200 <= response.status < 300:
                    raise MatchmakingError(response.status)
                body = bytearray()
                async for chunk in response.content.iter_chunked(MAX_MATCHMAKER_RESPONSE + 1):
                    body.extend(chunk)
                    if len(body) > MAX_MATCHMAKER_RESPONSE:
                        raise ProtocolError('Matchmaker response exceeds 4096 bytes')
                shard = Shard.parse(body.decode())
            self.websocket = await self.session.ws_connect(
                shard.websocket, origin=self.base_url, max_msg_size=self.max_payload
            )
            frame = await self._frame()
            if not frame.startswith('0'):
                raise ProtocolError('Expected Engine.IO open')
            try:
                policy = loads(frame[1:])
                interval, expiry = policy['pingInterval'], policy['pingTimeout']
                payload = policy.get('maxPayload', self.max_payload)
                if any(
                    type(value) is not int or value <= 0 for value in (interval, expiry, payload)
                ):
                    raise ValueError('Policy values must be positive integers')
            except (ValueError, TypeError, KeyError) as error:
                raise ProtocolError(f'Invalid Engine.IO policy: {error}') from error
            self.heartbeat_timeout = (interval + expiry) / 1000
            self.max_payload = min(self.max_payload, payload)
            await self._write('40')
            while True:
                frame = await self._frame()
                if frame == '2':
                    await self._write('3')
                elif frame == '40' or frame.startswith('40{'):
                    return
                else:
                    raise ProtocolError(f'Unexpected Socket.IO connect frame: {frame[:80]}')

    async def emit(self, name: str, data):
        frame = '42' + dumps(
            [name, data], ensure_ascii=False, separators=(',', ':'), allow_nan=False
        )
        if len(frame.encode()) > self.max_payload:
            raise ProtocolError('Outgoing event exceeds the server payload limit')
        await self._write(frame)

    async def read(self):
        while True:
            frame = await self._frame()
            if frame == '2':
                await self._write('3')
            elif frame.startswith('42'):
                try:
                    values = loads(frame[2:])
                    if (
                        not isinstance(values, list)
                        or len(values) != 2
                        or not isinstance(values[0], str)
                    ):
                        raise ValueError('Expected an event name and argument')
                    return values[0], values[1]
                except ValueError as error:
                    raise ProtocolError(f'Invalid Socket.IO event: {error}') from error
            elif frame in ('1', '41'):
                raise Disconnected('Server disconnected')
            elif frame.startswith('44'):
                raise ProtocolError(f'Socket.IO connection error: {frame[2:]}')

    async def close(self):
        if self.websocket is not None:
            await self.websocket.close()

    async def _frame(self) -> str:
        if self.websocket is None:
            raise Disconnected('Socket is not connected')
        try:
            async with timeout(self.heartbeat_timeout):
                message = await self.websocket.receive()
        except (ClientError, OSError) as error:
            raise Disconnected(f'WebSocket receive failed: {error}') from error
        if message.type != WSMsgType.TEXT:
            raise Disconnected(f'WebSocket ended: {message.type.name}')
        return message.data

    async def _write(self, frame: str):
        async with self._write_lock:
            if self.websocket is None or self.websocket.closed:
                raise Disconnected('Socket is closed')
            try:
                await self.websocket.send_str(frame)
            except (ClientError, OSError) as error:
                raise Disconnected(f'WebSocket send failed: {error}') from error
