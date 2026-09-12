import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('WEBUI_SECRET_KEY', 'test-secret-key')

from open_webui.utils.mcp.client import MCPClient  # noqa: E402


class HangingSession:
    def __init__(self):
        self.started = asyncio.Event()

    async def _hang(self):
        self.started.set()
        await asyncio.Event().wait()

    async def list_tools(self):
        await self._hang()

    async def call_tool(self, function_name, function_args):
        await self._hang()

    async def list_resources(self, cursor=None):
        await self._hang()

    async def read_resource(self, uri):
        await self._hang()


class DumpResult:
    def __init__(self, payload, *, is_error=False):
        self.payload = payload
        self.isError = is_error

    def model_dump(self, **kwargs):
        return self.payload


class ImmediateSession:
    async def list_tools(self):
        tool = SimpleNamespace(
            name='example',
            description='Example tool',
            inputSchema={'type': 'object'},
            outputSchema=None,
        )
        return SimpleNamespace(tools=[tool])

    async def call_tool(self, function_name, function_args):
        return DumpResult({'content': [{'type': 'text', 'text': 'ok'}]})

    async def list_resources(self, cursor=None):
        return DumpResult({'resources': [{'uri': 'resource://example'}]})

    async def read_resource(self, uri):
        return DumpResult({'contents': [{'uri': uri, 'text': 'ok'}]})


class RecordingExitStack:
    def __init__(self, *, error=None, block=False):
        self.error = error
        self.block = block
        self.started = asyncio.Event()
        self.task = None

    async def aclose(self):
        self.task = asyncio.current_task()
        self.started.set()
        if self.block:
            await asyncio.Event().wait()
        if self.error is not None:
            raise self.error


class MCPClientOperationTests(unittest.IsolatedAsyncioTestCase):
    async def test_operations_use_server_timeout(self):
        operations = {
            'list_tool_specs': lambda client: client.list_tool_specs(),
            'call_tool': lambda client: client.call_tool('example', {}),
            'list_resources': lambda client: client.list_resources(),
            'read_resource': lambda client: client.read_resource('resource://example'),
        }

        with patch('open_webui.utils.mcp.client.AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER', 0.01):
            for name, operation in operations.items():
                with self.subTest(operation=name):
                    client = MCPClient()
                    client.session = HangingSession()

                    with self.assertRaises(TimeoutError):
                        await operation(client)

                    self.assertTrue(client.session.started.is_set())

    async def test_operations_return_results_with_timeout_scope(self):
        client = MCPClient()
        client.session = ImmediateSession()

        with patch('open_webui.utils.mcp.client.AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER', 1):
            self.assertEqual(
                await client.list_tool_specs(),
                [
                    {
                        'name': 'example',
                        'description': 'Example tool',
                        'parameters': {'type': 'object'},
                    }
                ],
            )
            self.assertEqual(
                await client.call_tool('example', {}),
                [{'type': 'text', 'text': 'ok'}],
            )
            self.assertEqual(
                await client.list_resources(),
                [{'uri': 'resource://example'}],
            )
            self.assertEqual(
                await client.read_resource('resource://example'),
                {'contents': [{'uri': 'resource://example', 'text': 'ok'}]},
            )


class MCPClientDisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_closes_exit_stack_in_calling_task(self):
        client = MCPClient()
        exit_stack = RecordingExitStack()
        client.exit_stack = exit_stack
        client.session = object()
        calling_task = asyncio.current_task()

        await client.disconnect()

        self.assertIs(exit_stack.task, calling_task)
        self.assertIsNone(client.exit_stack)
        self.assertIsNone(client.session)

    async def test_disconnect_suppresses_close_errors(self):
        for error in (RuntimeError('close failed'), ValueError('close failed')):
            with self.subTest(error=type(error).__name__):
                client = MCPClient()
                client.exit_stack = RecordingExitStack(error=error)
                client.session = object()

                await client.disconnect()

                self.assertIsNone(client.exit_stack)
                self.assertIsNone(client.session)

    async def test_disconnect_suppresses_internal_cancellation(self):
        client = MCPClient()
        client.exit_stack = RecordingExitStack(error=asyncio.CancelledError())
        client.session = object()

        await client.disconnect()

        self.assertIsNone(client.exit_stack)
        self.assertIsNone(client.session)

    async def test_disconnect_propagates_external_cancellation_in_same_task(self):
        client = MCPClient()
        exit_stack = RecordingExitStack(block=True)
        client.exit_stack = exit_stack
        client.session = object()

        disconnect_task = asyncio.create_task(client.disconnect())
        await exit_stack.started.wait()
        disconnect_task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await disconnect_task

        self.assertIs(exit_stack.task, disconnect_task)
        self.assertIsNone(client.exit_stack)
        self.assertIsNone(client.session)


if __name__ == '__main__':
    unittest.main()
