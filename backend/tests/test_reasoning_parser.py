import unittest

from open_webui.utils.middleware import DEFAULT_REASONING_TAGS
from open_webui.utils.reasoning_parser import (
    ReasoningTagParser,
    ReasoningTagState,
    TaggedOutputState,
    finalize_reasoning_items,
    finalize_stream_reasoning_items,
)


class ReasoningParserTests(unittest.TestCase):
    def test_every_default_tag_survives_all_chunk_boundaries(self):
        for start, end in DEFAULT_REASONING_TAGS:
            with self.subTest(start=start):
                source = f'answer {start}private reasoning{end} after'
                for split in range(1, len(source)):
                    parser = ReasoningTagParser([(start, end)])
                    events = parser.feed(source[:split]) + parser.feed(source[split:])
                    events += parser.finish()
                    self.assertEqual(
                        ''.join(event.get('text', '') for event in events if event['type'] == 'text'),
                        'answer  after',
                    )
                    self.assertEqual(
                        ''.join(event.get('text', '') for event in events if event['type'] == 'reasoning'),
                        'private reasoning',
                    )
                    self.assertNotIn(start, ''.join(event.get('text', '') for event in events))
                    self.assertNotIn(end, ''.join(event.get('text', '') for event in events))
                    self.assertEqual(parser.state, ReasoningTagState.COMPLETED)

    def test_incomplete_reasoning_never_becomes_plain_text(self):
        parser = ReasoningTagParser([('<think>', '</think>')])
        events = parser.feed('answer <think>partial') + parser.finish()
        self.assertEqual(
            ''.join(event.get('text', '') for event in events if event['type'] == 'text'),
            'answer ',
        )
        self.assertEqual(
            ''.join(event.get('text', '') for event in events if event['type'] == 'reasoning'),
            'partial',
        )
        self.assertEqual(events[-1]['type'], 'reasoning_incomplete')
        self.assertEqual(parser.state, ReasoningTagState.INTERRUPTED)

    def test_partial_open_or_close_markers_are_not_emitted_as_text(self):
        parser = ReasoningTagParser([('<think>', '</think>')])
        events = parser.feed('answer <thi') + parser.finish()
        self.assertEqual(
            ''.join(event.get('text', '') for event in events if event['type'] == 'text'),
            'answer ',
        )

        parser = ReasoningTagParser([('<think>', '</think>')])
        events = parser.feed('<think>partial</thi') + parser.finish()
        self.assertEqual(
            ''.join(event.get('text', '') for event in events if event['type'] == 'reasoning'),
            'partial',
        )
        self.assertEqual(events[-1]['type'], 'reasoning_incomplete')

    def test_finalize_marks_only_open_reasoning_items_incomplete(self):
        output = [
            {'type': 'reasoning', 'status': 'in_progress', 'started_at': 10},
            {'type': 'reasoning', 'status': 'completed', 'started_at': 1},
            {'type': 'message', 'status': 'in_progress'},
        ]
        changed = finalize_reasoning_items(output, now=lambda: 15)
        self.assertEqual(changed, 1)
        self.assertEqual(output[0]['status'], 'incomplete')
        self.assertEqual(output[0]['duration'], 5)
        self.assertEqual(output[1]['status'], 'completed')
        self.assertEqual(output[2]['status'], 'in_progress')

    def test_normal_stream_end_distinguishes_tagged_and_provider_reasoning(self):
        state = TaggedOutputState(output_id=lambda prefix: f'{prefix}_1', now=lambda: 10)
        output = [
            {
                'type': 'message',
                'id': 'initial',
                'status': 'in_progress',
                'role': 'assistant',
                'content': [{'type': 'output_text', 'text': '<think>missing close marker'}],
            }
        ]
        output, _ = state.process('reasoning', [('<think>', '</think>')], output)
        output.append(
            {
                'type': 'reasoning',
                'id': 'provider_1',
                'status': 'in_progress',
                'started_at': 12,
                'attributes': {'type': 'reasoning_content'},
                'content': [{'type': 'output_text', 'text': 'provider reasoning'}],
            }
        )

        changed = finalize_stream_reasoning_items(output, now=lambda: 15)

        self.assertEqual(changed, 2)
        self.assertEqual(output[0]['status'], 'incomplete')
        self.assertEqual(output[0]['duration'], 5)
        self.assertEqual(output[1]['status'], 'completed')
        self.assertEqual(output[1]['duration'], 3)

    def test_tagged_output_state_handles_split_reasoning_and_solution(self):
        ids = iter(['msg_1', 'r_1', 'msg_2', 'msg_3'])
        state = TaggedOutputState(output_id=lambda prefix: next(ids), now=lambda: 10)
        output = [
            {
                'type': 'message',
                'id': 'initial',
                'status': 'in_progress',
                'role': 'assistant',
                'content': [{'type': 'output_text', 'text': 'answer <thi'}],
            }
        ]
        output, end = state.process('reasoning', [('<think>', '</think>')], output)
        self.assertFalse(end)
        output[-1]['content'][-1]['text'] += 'nk>private'
        output, end = state.process('reasoning', [('<think>', '</think>')], output)
        self.assertFalse(end)
        output[-1]['content'][-1]['text'] += '</thi'
        output, end = state.process('reasoning', [('<think>', '</think>')], output)
        self.assertFalse(end)
        output[-1]['content'][-1]['text'] += 'nk> after'
        output, end = state.process('reasoning', [('<think>', '</think>')], output)
        self.assertTrue(end)
        self.assertEqual(output[0]['type'], 'message')
        self.assertEqual(output[0]['content'][0]['text'], 'answer ')
        self.assertEqual(output[1]['type'], 'reasoning')
        self.assertEqual(output[1]['content'][0]['text'], 'private')
        self.assertEqual(output[1]['status'], 'completed')
        self.assertEqual(output[2]['content'][0]['text'], 'after')

        output[-1]['content'][0]['text'] += ' <|begin_of_solution|>result'
        output, end = state.process(
            'solution',
            [('<|begin_of_solution|>', '<|end_of_solution|>')],
            output,
        )
        self.assertFalse(end)
        output[-1]['content'][0]['text'] += '<|end_of_solution|> tail'
        output, end = state.process(
            'solution',
            [('<|begin_of_solution|>', '<|end_of_solution|>')],
            output,
        )
        self.assertTrue(end)
        self.assertEqual(output[-2]['content'][0]['text'], 'result')
        self.assertEqual(output[-1]['content'][0]['text'], 'tail')

    def test_tagged_output_state_safe_deltas_cover_every_default_split(self):
        for start, end in DEFAULT_REASONING_TAGS:
            source = f'answer {start}private{end} after'
            for split in range(1, len(source)):
                with self.subTest(start=start, split=split):
                    next_id = 0

                    def output_id(prefix):
                        nonlocal next_id
                        next_id += 1
                        return f'{prefix}_{next_id}'

                    state = TaggedOutputState(output_id=output_id, now=lambda: 10)
                    output = []
                    deltas = []
                    for chunk in (source[:split], source[split:]):
                        if output and output[-1].get('type') == 'reasoning':
                            parts = output[-1].get('content', [])
                            if parts:
                                parts[-1]['text'] += chunk
                            else:
                                output[-1]['content'] = [{'type': 'output_text', 'text': chunk}]
                        else:
                            if not output or output[-1].get('type') != 'message':
                                output.append(
                                    {
                                        'type': 'message',
                                        'id': output_id('msg'),
                                        'status': 'in_progress',
                                        'role': 'assistant',
                                        'content': [{'type': 'output_text', 'text': ''}],
                                    }
                                )
                            output[-1]['content'][-1]['text'] += chunk
                        output, _ = state.process('reasoning', DEFAULT_REASONING_TAGS, output)
                        deltas.extend(state.take_safe_deltas(output))

                    ordinary = ''.join(
                        delta['delta'] for delta in deltas if delta['type'] == 'response.output_text.delta'
                    )
                    reasoning = ''.join(
                        delta['delta'] for delta in deltas if delta['type'] == 'response.reasoning_text.delta'
                    )
                    emitted = ordinary + reasoning
                    self.assertEqual(' '.join(ordinary.split()), 'answer after')
                    self.assertEqual(reasoning, 'private')
                    self.assertNotIn(start, emitted)
                    self.assertNotIn(end, emitted)

    def test_tagged_output_state_emits_only_sanitized_split_deltas(self):
        ids = iter(['r_1', 'msg_1'])
        state = TaggedOutputState(output_id=lambda prefix: next(ids), now=lambda: 10)
        output = [
            {
                'type': 'message',
                'id': 'initial',
                'status': 'in_progress',
                'role': 'assistant',
                'content': [{'type': 'output_text', 'text': 'answer <thi'}],
            }
        ]

        output, _ = state.process('reasoning', [('<think>', '</think>')], output)
        deltas = state.take_safe_deltas(output)
        self.assertEqual([delta['delta'] for delta in deltas], ['answer '])

        output[-1]['content'][-1]['text'] += 'nk>private</thi'
        output, _ = state.process('reasoning', [('<think>', '</think>')], output)
        deltas += state.take_safe_deltas(output)

        output[-1]['content'][-1]['text'] += 'nk> after'
        output, _ = state.process('reasoning', [('<think>', '</think>')], output)
        deltas += state.take_safe_deltas(output)

        emitted = ''.join(delta['delta'] for delta in deltas)
        self.assertEqual(emitted, 'answer privateafter')
        self.assertNotIn('<thi', emitted)
        self.assertNotIn('</thi', emitted)
        self.assertEqual(
            [delta['type'] for delta in deltas],
            [
                'response.output_text.delta',
                'response.reasoning_text.delta',
                'response.output_text.delta',
            ],
        )

    def test_tagged_output_state_releases_false_marker_prefix_as_text(self):
        state = TaggedOutputState(output_id=lambda prefix: f'{prefix}_1', now=lambda: 10)
        output = [
            {
                'type': 'message',
                'id': 'initial',
                'status': 'in_progress',
                'role': 'assistant',
                'content': [{'type': 'output_text', 'text': 'answer <thi'}],
            }
        ]

        output, _ = state.process('reasoning', [('<think>', '</think>')], output)
        first = state.take_safe_deltas(output)
        output[-1]['content'][-1]['text'] += 's is ordinary text'
        output, _ = state.process('reasoning', [('<think>', '</think>')], output)
        second = state.take_safe_deltas(output)

        self.assertEqual(
            ''.join(delta['delta'] for delta in first + second),
            'answer <this is ordinary text',
        )

    def test_tagged_output_state_extracts_code_interpreter_attributes(self):
        ids = iter(['ci_1', 'msg_1'])
        state = TaggedOutputState(output_id=lambda prefix: next(ids), now=lambda: 20)
        output = [
            {
                'type': 'message',
                'id': 'initial',
                'status': 'in_progress',
                'role': 'assistant',
                'content': [
                    {
                        'type': 'output_text',
                        'text': '<code_interpreter lang="python">print(1)</code_interpreter>',
                    }
                ],
            }
        ]
        output, end = state.process(
            'code_interpreter',
            [('<code_interpreter>', '</code_interpreter>')],
            output,
        )
        self.assertTrue(end)
        self.assertEqual(output[0]['type'], 'open_webui:code_interpreter')
        self.assertEqual(output[0]['code'], 'print(1)')
        self.assertEqual(output[1]['content'][0]['text'], '')


if __name__ == '__main__':
    unittest.main()
