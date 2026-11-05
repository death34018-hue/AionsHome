import copy
import json
import time
import unittest
from unittest.mock import patch

import post_sentinel as profile
import user_profile_policy as policy


class RecentLifePolicyTests(unittest.TestCase):
    def test_only_committed_plans_and_ongoing_matters_enter_the_shared_list(self):
        doc = {'entries': {}}
        changes = [('ideas', 'idea', '有空想做表情包'), ('preferences', 'context', '最近爱吃奶茶冻'),
                   ('state', 'context', '午饭吃了虾'), ('plans', 'task', '周六一起挑图做表情包')]
        updates = [dict(category='recent', subcategory=sub, kind=kind, content=content,
                        reason='下次聊天可提起', sources=['U1']) for sub, kind, content in changes]
        profile._apply_analysis(doc, {'updates': updates}, {'U1': 'private:one'}, {'source_ts': time.time()})
        self.assertEqual([e['content'] for e in doc['entries'].values()], ['周六一起挑图做表情包'])
        self.assertEqual(doc['_analysis_errors'], [])
        legacy = self.entry(source_ts=time.time())
        doc['entries']['care'] = legacy
        self.assertNotIn('水培', policy.render_context(doc)[0])
        legacy['sources'] = ['manual']
        self.assertIn('水培', policy.render_context(doc)[0])

    def entry(self, **extra):
        return {'key': 'care', 'category': 'recent', 'subcategory': 'ideas', 'kind': 'idea',
                'status': 'active', 'topic': '试试水培', 'content': '想试试水培，还没开始。',
                'source_ts': 100, 'updated_at': 100, 'sources': ['private:one'], **extra}

    def test_ideas_preferences_and_states_fade_after_one_week(self):
        for sub, kind in [('ideas', 'idea'), ('preferences', 'context'), ('state', 'context')]:
            item = self.entry(subcategory=sub, kind=kind)
            self.assertEqual(policy.lifecycle(item, 100+6*86400), 'active')
            self.assertEqual(policy.lifecycle(item, 100+7*86400), 'retired')

    def test_explicit_task_is_unknown_not_forgotten_or_completed(self):
        item = self.entry(subcategory='plans', kind='task', due_at=200)
        self.assertEqual(policy.lifecycle(item, 100+8*86400), 'unknown')

    def test_ended_entries_leave_context_then_clean_up_without_fact_archive(self):
        item = self.entry(status='done', retired_at=100, importance=0.99)
        doc = {'entries': {'care': item}}
        self.assertEqual(policy.render_context(doc, now=200)[0], '')
        policy.maintain(doc, now=100+8*86400)
        self.assertEqual(doc['entries'], {})

    def test_picking_up_medicine_can_continue_same_matter_as_confirmed_course(self):
        now = time.time()
        old = self.entry(kind='task', subcategory='plans', topic='本次就医', content='明天去拿药', source_ts=now-100)
        doc = {'entries': {'care': old}}
        update = {'ref': 'P1', 'category': 'recent', 'subcategory': 'projects', 'kind': 'project',
                  'content': '药已拿到，开始两周疗程。', 'next_step': '按本人确认的安排每天早晚服用。',
                  'continues': True, 'duration_days': 14, 'sources': ['U1']}
        profile._apply_analysis(doc, {'updates': [update]}, {'U1': 'private:two'}, {'source_ts': now})
        self.assertFalse(doc['_analysis_errors'])
        self.assertEqual(len(doc['entries']), 1)
        self.assertEqual(doc['entries']['care']['kind'], 'project')
        self.assertIn('早晚', doc['entries']['care']['next_step'])
        self.assertNotIn('明天去拿药', policy.render_context(doc, now=now)[0])

    def test_completion_without_user_source_cannot_remove_open_task(self):
        old = self.entry(kind='task', subcategory='plans')
        doc = {'entries': {'care': old}}
        profile._apply_analysis(doc, {'updates': [{'ref': 'P1', 'status': 'done'}]}, {},
                                {'source_ts': 200, 'consolidation': True})
        self.assertEqual(doc['entries']['care']['status'], 'active')
        self.assertTrue(doc['_analysis_errors'])

    def test_entire_active_document_is_bounded_not_silently_truncated(self):
        entries = {str(i): self.entry(key=str(i), source_ts=time.time(), kind='project', subcategory='projects',
                                     content='近期事项正在处理。'*12) for i in range(20)}
        with self.assertRaisesRegex(ValueError, '2000'):
            profile._check_budget({'entries': entries})

    def test_old_identity_and_fact_categories_are_rejected(self):
        for cat in ('profile', 'facts'):
            with self.assertRaises(ValueError):
                policy.category(cat)

    def test_no_source_reorganization_does_not_refresh_freshness(self):
        old = self.entry(kind='context', subcategory='preferences', source_ts=time.time()-6*86400)
        doc = {'entries': {'care': old}}
        original = old['source_ts']
        profile._apply_analysis(doc, {'updates': [{'ref': 'P1', 'content': '最近想吃榴莲。'}]}, {},
                                {'source_ts': time.time(), 'consolidation': True})
        self.assertEqual(doc['entries']['care']['source_ts'], original)

    def test_rementioned_temporary_idea_needs_commitment_to_revive(self):
        now = time.time()
        old = self.entry(source_ts=now-3*86400, expires_at=now-86400, status='retired')
        doc = {'entries': {'care': old}}
        profile._apply_analysis(doc, {'updates': [{'ref':'P1', 'content':'今天又想试试水培。','sources':['U1']}]},
                                {'U1':'private:two'}, {'source_ts':now})
        self.assertEqual(policy.lifecycle(doc['entries']['care'],now), 'retired')
        profile._apply_analysis(doc, {'updates': [{'ref':'P1', 'kind':'task', 'subcategory':'plans',
                                'content':'定了周六开始水培。','sources':['U1']}]},
                                {'U1':'private:two'}, {'source_ts':now})
        self.assertIn('定了周六', policy.render_context(doc, now=now)[0])

    def test_complete_final_json_survives_repeated_streamed_draft(self):
        from profile_initialize import parse_week_result
        final = {'profile_updates': [{'ref':'P1','content':'短清单'}]}
        raw = '{"profile_updates":[{"content":"unfinished' + json.dumps(final, ensure_ascii=False)
        self.assertEqual(parse_week_result(raw),final)
        with self.assertRaises(ValueError):
            parse_week_result('{"profile_updates":[')

    def test_update_alias_and_fresh_idea_preservation(self):
        now=time.time()
        doc={'entries':{'care':self.entry(source_ts=now)}}
        profile._apply_analysis(doc,{'updates':[{'ref':'P1','op':'update','content':'想试水培。'}]}, {},
                                {'source_ts':now,'consolidation':True})
        self.assertFalse(doc['_analysis_errors'])
        self.assertEqual(doc['entries']['care']['content'],'想试水培。')
        profile._apply_analysis(doc,{'updates':[{'ref':'P1','op':'remove'}]}, {},
                                {'source_ts':now,'consolidation':True})
        self.assertEqual(doc['entries']['care']['status'],'active')

    def test_prompts_only_maintain_recent_life(self):
        from profile_prompts import sentinel_prompt, digest_prompt
        doc = {'entries': {}}
        for text in (sentinel_prompt(doc, [], {}, '', 'Mira'), digest_prompt(doc, 'Sol', 'Mira')):
            self.assertIn('7天', text)
            self.assertIn('下一步', text)
            self.assertIn('长期身份', text)
            self.assertNotIn('profile 核心画像', text)

    def test_fresh_idea_cannot_be_ended_by_reorganization(self):
        now = time.time()
        for change in ({'op': 'remove'}, {'status': 'retired'}, {'status': 'done'},
                       {'status': 'cancelled'}, {'expires_at': now-1}):
            with self.subTest(change=change):
                old = self.entry(source_ts=now-100)
                doc = {'entries': {'care': copy.deepcopy(old)}}
                profile._apply_analysis(doc, {'updates': [{'ref': 'P1', **change}]}, {},
                                        {'digest': True, 'consolidation': True, 'source_ts': now})
                self.assertEqual(doc['entries']['care'], old)

    def test_sentinel_can_still_merge_and_end_with_current_evidence(self):
        now = time.time()
        doc = {'entries': {'care': self.entry(kind='project', subcategory='projects', source_ts=now-100),
                          'other': self.entry(key='other', kind='project', subcategory='projects',
                                              source_ts=now-50, sources=['private:other'])}}
        job = {'source_id': 'private:latest', 'source_ts': now}
        sources = {'U1': 'private:latest'}
        profile._apply_analysis(doc, {'updates': [{'ref': 'P1', 'merge_from': ['P2'],
            'content': '想试水培，相关想法合在一起。', 'sources': ['U1']}]}, sources, job)
        self.assertFalse(doc['_analysis_errors'])
        self.assertEqual(list(doc['entries']), ['care'])
        self.assertEqual(set(doc['entries']['care']['sources']),
                         {'private:one', 'private:other', 'private:latest'})
        profile._apply_analysis(doc, {'updates': [{'ref': 'P1', 'status': 'cancelled',
            'content': '已经明确不打算做水培。', 'sources': ['U1']}]}, sources, job)
        self.assertFalse(doc['_analysis_errors'])
        self.assertEqual(policy.render_context(doc, now=now)[0], '')

    def test_digest_merge_keeps_active_idea_and_original_confirmation(self):
        now = time.time()
        doc = {'entries': {'care': self.entry(source_ts=now-100),
                          'other': self.entry(key='other', source_ts=now-50)}}
        profile._apply_analysis(doc, {'updates': [{'ref': 'P1', 'merge_from': ['P2'],
            'content': '合并后的水培想法。'}]}, {},
            {'digest': True, 'consolidation': True, 'source_ts': now})
        self.assertFalse(doc['_analysis_errors'])
        self.assertEqual(list(doc['entries']), ['care'])
        self.assertEqual(doc['entries']['care']['source_ts'], now-50)
        self.assertEqual(policy.lifecycle(doc['entries']['care'], now), 'active')


if __name__ == '__main__':
    unittest.main()
