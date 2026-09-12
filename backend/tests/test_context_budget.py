import os
import unittest

os.environ.setdefault('WEBUI_SECRET_KEY', 'test-secret-key')

from open_webui.utils.context_budget import (  # noqa: E402
    enforce_final_context_budget,
    estimate_request_tokens,
)


class ContextBudgetTests(unittest.TestCase):
    def test_keeps_payload_when_limit_is_unknown(self):
        payload = {'messages': [{'role': 'user', 'content': 'hello'}]}
        metadata = {}
        result = enforce_final_context_budget(payload, {}, metadata)
        self.assertEqual(result['messages'], payload['messages'])
        self.assertNotIn('context_budget', metadata)

    def test_drops_oldest_complete_messages_and_keeps_system_and_latest_user(self):
        payload = {
            'messages': [
                {'role': 'system', 'content': 'policy'},
                {'role': 'user', 'content': 'old ' * 1000},
                {'role': 'assistant', 'content': 'old answer ' * 1000},
                {'role': 'user', 'content': 'current request'},
            ]
        }
        metadata = {'params': {'num_ctx': 1024}}
        result = enforce_final_context_budget(payload, {}, metadata)
        self.assertEqual(result['messages'][0]['role'], 'system')
        self.assertEqual(result['messages'][-1]['content'], 'current request')
        self.assertGreater(metadata['context_budget']['dropped_messages'], 0)

    def test_tool_schema_counts_toward_budget(self):
        base = {'messages': [{'role': 'user', 'content': 'hello'}]}
        with_tools = {**base, 'tools': [{'description': 'x' * 3000}]}
        self.assertGreater(estimate_request_tokens(with_tools), estimate_request_tokens(base))


if __name__ == '__main__':
    unittest.main()
