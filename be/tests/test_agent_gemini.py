from __future__ import annotations

import json
import unittest
from datetime import date
from types import SimpleNamespace

from app.agent_layer.domain import DigestItemRecord
from app.agent_layer.gemini import GeminiAgent


class _FakeModels:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.call = None

    async def generate_content(self, **kwargs):
        self.call = kwargs
        return SimpleNamespace(text=json.dumps(self.payload))


class GeminiAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_command_uses_flash_and_structured_json_schema(self):
        models = _FakeModels(
            {
                "actions": [
                    {
                        "action": "draft",
                        "item_number": 1,
                        "customer_id": "customer-1",
                        "tone": "firm",
                        "note": None,
                        "confidence": 0.98,
                        "reason": "Exact customer match",
                    }
                ],
                "confidence": 0.98,
                "explanation": "Matched item 1",
                "ambiguous": False,
            }
        )
        agent = GeminiAgent(
            client=SimpleNamespace(aio=SimpleNamespace(models=models)),
            model_name="gemini-2.5-flash",
        )
        item = DigestItemRecord(
            id="item-1",
            digest_id="digest-1",
            item_number=1,
            customer_id="customer-1",
            customer_name="Acme",
            invoice_ids=("invoice-1",),
            amount_paise=100_000,
            oldest_due_date=date(2026, 8, 1),
            recommendation_reason="Overdue",
            source_references=(),
        )
        decision = await agent.parse_owner_command(
            owner_text="Acme ko firm bhejo", digest_items=[item]
        )
        self.assertFalse(decision.ambiguous)
        self.assertEqual(decision.actions[0].customer_id, "customer-1")
        self.assertEqual(models.call["model"], "gemini-2.5-flash")
        self.assertEqual(models.call["config"]["response_mime_type"], "application/json")
        self.assertIn("response_json_schema", models.call["config"])

    def test_non_flash_model_is_rejected(self):
        with self.assertRaises(ValueError):
            GeminiAgent(client=object(), model_name="gemini-pro")


if __name__ == "__main__":
    unittest.main()
