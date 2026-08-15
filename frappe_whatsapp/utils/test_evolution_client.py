# Copyright (c) 2026, Shridhar Patil and Contributors
# See license.txt

import frappe
from frappe_whatsapp.testing import IntegrationTestCase

from frappe_whatsapp.utils.evolution_client import (
	EvolutionAPIClient,
	get_message_id,
	is_evolution_account,
	normalize_phone,
)


class TestNormalizePhone(IntegrationTestCase):
	def test_strips_leading_zero_and_prepends_country_code_kenya(self):
		# 07xx and 01xx numbers are the common short local formats in Kenya.
		self.assertEqual(normalize_phone("0725548065", "254"), "254725548065")
		self.assertEqual(normalize_phone("0112345678", "254"), "254112345678")

	def test_generic_across_country_codes_india(self):
		self.assertEqual(normalize_phone("09876543210", "91"), "919876543210")

	def test_plus_prefixed_number_untouched_by_country_code(self):
		self.assertEqual(normalize_phone("+254725548065", "254"), "254725548065")

	def test_00_prefixed_number_untouched_by_country_code(self):
		self.assertEqual(normalize_phone("00254725548065", "254"), "254725548065")

	def test_no_default_country_code_leaves_digits_as_is(self):
		self.assertEqual(normalize_phone("0725548065", None), "0725548065")

	def test_wa_jid_suffix_stripped(self):
		self.assertEqual(normalize_phone("254725548065@s.whatsapp.net"), "254725548065")


class TestGetMessageId(IntegrationTestCase):
	def test_extracts_from_key(self):
		self.assertEqual(
			get_message_id({"key": {"id": "3EB0C8", "remoteJid": "254@wa"}}),
			"3EB0C8",
		)

	def test_extracts_from_message_id(self):
		self.assertEqual(get_message_id({"messageId": "EVO-123"}), "EVO-123")

	def test_extracts_from_id(self):
		self.assertEqual(get_message_id({"id": "EVO-456"}), "EVO-456")

	def test_none_for_empty_response(self):
		self.assertIsNone(get_message_id(None))
		self.assertIsNone(get_message_id({}))


class TestIsEvolutionAccount(IntegrationTestCase):
	def test_evolution_account(self):
		account = frappe._dict(api_type="Evolution API")
		self.assertTrue(is_evolution_account(account))

	def test_meta_account_and_missing_api_type(self):
		self.assertFalse(is_evolution_account(frappe._dict(api_type="Meta Cloud API")))
		self.assertFalse(is_evolution_account(frappe._dict()))
		self.assertFalse(is_evolution_account(None))


class TestEvolutionAPIClientConstructor(IntegrationTestCase):
	def test_constructor_reads_fields_from_account(self):
		account = frappe.get_doc({
			"doctype": "WhatsApp Account",
			"account_name": "Test Evo Client Account",
			"status": "Active",
			"api_type": "Evolution API",
			"base_url": "https://evo.example.com/",
			"instance_name": "test-instance",
			"api_key": "test-api-key",
			"timeout": 15,
			"default_country_code": "254",
			"webhook_secret": "test-secret",
		})
		account.insert(ignore_permissions=True)

		client = EvolutionAPIClient(account)
		self.assertEqual(client.base_url, "https://evo.example.com")
		self.assertEqual(client.instance_name, "test-instance")
		self.assertEqual(client.api_key, "test-api-key")
		self.assertEqual(client.timeout, 15)

	def test_constructor_requires_evolution_fields(self):
		account = frappe._dict(
			api_type="Evolution API",
			base_url="",
			instance_name="",
			api_key="",
			timeout=30,
		)
		with self.assertRaises(frappe.ValidationError):
			EvolutionAPIClient(account)
