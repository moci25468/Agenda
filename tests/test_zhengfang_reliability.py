from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

import httpx

from app.providers.zhengfang import TermEmptyError, ZhengfangProvider


def response(payload) -> httpx.Response:
    return httpx.Response(
        200, json=payload,
        request=httpx.Request("POST", "https://jxgl.qut.edu.cn/jwglxt/test"))


class ZhengfangReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def provider(self) -> ZhengfangProvider:
        return ZhengfangProvider({
            "provider": {"zhengfang": {
                "base": "https://jxgl.qut.edu.cn/jwglxt",
                "year": "2026", "term": "3",
            }}
        })

    async def test_malformed_course_container_does_not_look_like_empty_term(self):
        client = AsyncMock()
        client.post.return_value = response({"kbList": {"name": "broken"}})
        with self.assertRaises(ValueError):
            await self.provider()._fetch_courses(client)

    async def test_missing_course_container_preserves_snapshot(self):
        client = AsyncMock()
        client.post.return_value = response({"unexpected": []})
        with self.assertRaises(ValueError):
            await self.provider()._fetch_courses(client)

    async def test_real_empty_course_array_still_switches_term(self):
        client = AsyncMock()
        client.post.return_value = response({"kbList": []})
        with self.assertRaises(TermEmptyError):
            await self.provider()._fetch_courses(client)

    async def test_exams_fetch_all_pages(self):
        first = [{"id": str(i), "kcmc": f"课程{i}", "kssj": "2026-12-20 09:00"}
                 for i in range(50)]
        second = [{"id": str(i), "kcmc": f"课程{i}", "kssj": "2026-12-21 09:00"}
                  for i in range(50, 55)]
        client = AsyncMock()
        client.post.side_effect = [
            response({"items": first, "total": 55}),
            response({"items": second, "total": 55}),
        ]
        exams = await self.provider()._fetch_exams(client)
        self.assertEqual(len(exams), 55)
        self.assertEqual(client.post.await_count, 2)
        self.assertEqual(
            client.post.await_args_list[1].kwargs["data"]["queryModel.currentPage"], "2")

    async def test_repeated_exam_page_is_not_silently_truncated(self):
        items = [{"id": str(i), "kcmc": f"课程{i}", "kssj": "2026-12-20 09:00"}
                 for i in range(50)]
        client = AsyncMock()
        client.post.side_effect = [
            response({"items": items, "total": 55}),
            response({"items": items, "total": 55}),
        ]
        with self.assertRaises(ValueError):
            await self.provider()._fetch_exams(client)

    async def test_malformed_score_container_preserves_snapshot(self):
        client = AsyncMock()
        client.post.return_value = response({"items": "broken"})
        with self.assertRaises(ValueError):
            await self.provider()._fetch_scores(client)

    async def test_unrecognized_score_rows_preserve_snapshot(self):
        client = AsyncMock()
        client.post.return_value = response({"items": [{"renamed": "value"}]})
        with self.assertRaises(ValueError):
            await self.provider()._fetch_scores(client)


if __name__ == "__main__":
    unittest.main()
