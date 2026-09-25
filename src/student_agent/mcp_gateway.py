from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .contracts import Contracts


class EvidenceGateway:
    def __init__(self, endpoint: str, team_api_key: str, contracts: Contracts) -> None:
        self._endpoint = endpoint
        self._team_api_key = team_api_key
        self._contracts = contracts
        self._session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None
        self._lock = asyncio.Lock()

    async def _connect(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:
                pass
            self._stack = None
            self._session = None

        stack = AsyncExitStack()
        try:
            headers = {"Authorization": f"Bearer {self._team_api_key}"}
            timeout = httpx2.Timeout(30.0, connect=15.0, write=20.0, pool=20.0)
            http_client = await stack.enter_async_context(
                httpx2.AsyncClient(headers=headers, timeout=timeout)
            )
            read_stream, write_stream = await stack.enter_async_context(
                streamable_http_client(self._endpoint, http_client=http_client)
            )
            session = await stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await session.initialize()
            self._session = session
            self._stack = stack
        except Exception:
            await stack.aclose()
            raise

    async def close(self) -> None:
        async with self._lock:
            if self._stack is not None:
                await self._stack.aclose()
                self._stack = None
                self._session = None

    async def list_tools(self) -> list[str]:
        async with self._lock:
            if self._session is None:
                await self._connect()
            assert self._session is not None
            response = await self._session.list_tools()
            return sorted(tool.name for tool in response.tools)

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        payload = {"case_id": case_id, **arguments}
        last_error: Exception | None = None

        for attempt in range(3):
            try:
                async with self._lock:
                    if self._session is None:
                        await self._connect()
                    assert self._session is not None
                    result = await asyncio.wait_for(
                        self._session.call_tool(tool_name, arguments=payload),
                        timeout=25.0,
                    )

                is_err = getattr(result, "is_error", None)
                if is_err is None:
                    is_err = getattr(result, "isError", False)
                if is_err:
                    message = " ".join(
                        block.text for block in result.content if getattr(block, "text", None)
                    )
                    raise RuntimeError(f"MCP tool {tool_name} failed: {message or 'unknown error'}")

                evidence = getattr(result, "structuredContent", None)
                if evidence is None:
                    evidence = getattr(result, "structured_content", None)
                if evidence is None:
                    text_blocks = [block.text for block in result.content if getattr(block, "text", None)]
                    if len(text_blocks) != 1:
                        raise ValueError(f"MCP tool {tool_name} did not return one evidence object")
                    evidence = json.loads(text_blocks[0])

                self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
                return evidence

            except RuntimeError as r_err:
                # If the tool explicitly reported failure from the server, do not loop-retry indefinitely
                if "MCP tool" in str(r_err) and "failed:" in str(r_err):
                    raise
                last_error = r_err
            except Exception as exc:
                last_error = exc

            # Transient network/socket/timeout issue: force reconnect
            async with self._lock:
                if self._stack is not None:
                    try:
                        await self._stack.aclose()
                    except Exception:
                        pass
                    self._stack = None
                    self._session = None
            await asyncio.sleep(1.0 * (attempt + 1))

        raise RuntimeError(f"MCP tool {tool_name} failed after 3 attempts: {last_error}")


@asynccontextmanager
async def connect_gateway(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[EvidenceGateway]:
    gateway = EvidenceGateway(endpoint, team_api_key, contracts)
    try:
        await gateway._connect()
        yield gateway
    finally:
        await gateway.close()
