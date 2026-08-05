import unittest

from stream_reply import resolve_stream_failure


class StreamFailurePolicyTest(unittest.TestCase):
    def test_partial_model_text_is_preserved_without_transport_error(self):
        result = resolve_stream_failure(
            "已经收到的正文。",
            RuntimeError("peer closed connection"),
            "Core 回复失败",
        )

        self.assertEqual(result.visible_text, "已经收到的正文。")
        self.assertEqual(result.tts_text, "已经收到的正文。")
        self.assertEqual(result.diagnostic_error, "peer closed connection")
        self.assertTrue(result.had_partial_text)

    def test_empty_model_text_uses_friendly_failure_and_silences_tts(self):
        result = resolve_stream_failure(
            " \n",
            RuntimeError("peer closed connection"),
            "Core 回复失败",
        )

        self.assertEqual(
            result.visible_text,
            "[Core 回复失败] 暂时无法完成回复，请手动重试。",
        )
        self.assertEqual(result.tts_text, "")
        self.assertEqual(result.diagnostic_error, "peer closed connection")
        self.assertFalse(result.had_partial_text)


if __name__ == "__main__":
    unittest.main()
