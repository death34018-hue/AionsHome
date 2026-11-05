import unittest
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import config
import flowai_image_gen as flowai
import chatroom_image_retry as retry
import image_gen
from routes import settings


class FlowImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_settings_and_dispatch_all_presets(self):
        with patch.dict(image_gen.SETTINGS, {'image_gen_model': 'my-api-model'}, clear=True), \
                patch.object(settings, 'save_settings'), patch.object(settings.manager, 'broadcast', new_callable=AsyncMock), \
                patch('flowai_image_gen.generate_flowai_image', new_callable=AsyncMock) as generate:
            generate.return_value = 'album/test.jpg'
            for model in ('gpt_image2_5_flare', 'seedream_5_0_flash', 'grok_imagine_image_2_0'):
                await settings.update_settings(settings.SettingsUpdate(image_gen_provider='flowai', flowai_image_model=model))
                self.assertEqual((await settings.get_settings())['flowai_image_model'], model)
                path = await image_gen.generate_image('scene', False, 'connor')
                self.assertEqual(path, 'album/test.jpg')
                self.assertEqual(generate.await_args.kwargs['model'], model)
            await settings.update_settings(settings.SettingsUpdate(image_gen_provider='codex'))
            self.assertEqual(image_gen.SETTINGS['flowai_image_model'], 'grok_imagine_image_2_0')
            self.assertEqual(image_gen.SETTINGS['image_gen_model'], 'my-api-model')

    async def test_group_retry_resumes_only_the_selected_models_job(self):
        snapshots = []
        seen = []

        async def remember(payload):
            snapshots.append(payload)

        async def fake_generate(*args, model, flow_job_id, on_flow_job, **kwargs):
            seen.append((model, flow_job_id))
            if not flow_job_id:
                await on_flow_job(f'job-{model}')
                raise TimeoutError()
            return 'album/completed.jpg'

        with patch.dict(image_gen.SETTINGS, image_gen_provider='flowai', flowai_image_model='seedream_5_0_flash'), \
                patch('system_logs._publish', new_callable=AsyncMock), \
                patch.object(flowai, 'generate_flowai_image', fake_generate):
            payload = await retry.snapshot('scene', [], False, 'connor')
            with self.assertRaises(TimeoutError):
                await retry.generate(payload, on_snapshot=remember)
            with patch.dict(image_gen.SETTINGS, flowai_image_model='gpt_image2_5_flare'):
                with self.assertRaises(TimeoutError):
                    await retry.generate(snapshots[-1], on_snapshot=remember)
                self.assertEqual(await retry.generate(snapshots[-1]), 'album/completed.jpg')
            self.assertEqual(seen, [('seedream_5_0_flash', None), ('gpt_image2_5_flare', None),
                                    ('gpt_image2_5_flare', 'job-gpt_image2_5_flare')])

    def test_old_saved_model_upgrades_but_cannot_be_selected(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(config, 'SETTINGS_PATH', Path(tmp) / 'settings.json'):
            config.save_settings({'image_gen_provider':'flowai', 'flowai_image_model':'jimeng_imgref_v4.5'})
            self.assertEqual(config.load_settings(), {'image_gen_provider':'flowai', 'flowai_image_model':'seedream_5_0_flash'})
        with self.assertRaises(ValueError):
            settings.SettingsUpdate(flowai_image_model='jimeng_imgref_v4.5')

    async def test_model_reference_limits_apply_before_upload(self):
        for model, limit in [('seedream_5_0_flash', 10), ('grok_imagine_image_2_0', 5)]:
            with self.subTest(model=model), self.assertRaisesRegex(ValueError, str(limit)):
                await flowai.generate_flowai_image('合照', False, 'connor', model=model, references=[{}] * (limit + 1))

    async def test_flow_requests_new_models_and_only_polls_existing_retired_jobs(self):
        call = AsyncMock()
        @asynccontextmanager
        async def session():
            yield call

        submitted = {'generation_id': 'ticket'}
        done = {'status': 'completed', 'urls': ['https://www.flowwall.ai/test.jpg']}
        with patch.object(flowai, '_flow_session', session), \
                patch.object(flowai, '_download_image', new_callable=AsyncMock), \
                patch.object(flowai, 'save_generated_image', new_callable=AsyncMock):
            for model in ('seedream_5_0_flash', 'grok_imagine_image_2_0'):
                call.reset_mock()
                call.side_effect = [submitted, done]
                await flowai.generate_flowai_image('画树', False, '', model=model)
                self.assertEqual(call.await_args_list[0].args, ('generate_image', {
                    'prompt': '画树', 'model': model, 'resolution': '1K'}))
            call.reset_mock()
            call.side_effect = [done]
            await flowai.generate_flowai_image('画树', False, '', model='jimeng_imgref_v4.5', flow_job_id='ticket')
            call.assert_awaited_once_with('get_generation_status', {'generation_id': 'ticket'})
