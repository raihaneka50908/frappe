# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE

import frappe
from frappe.automation_engine import usage
from frappe.tests import IntegrationTestCase


class TestUsage(IntegrationTestCase):
	def setUp(self):
		usage.reset()

	def tearDown(self):
		frappe.flags.automation_usage = None

	def test_totals_start_at_zero(self):
		self.assertEqual(usage.current(), dict.fromkeys(usage.KEYS, 0))

	def test_reports_add_up(self):
		usage.report(queries=1, records_read=40)
		usage.report(queries=1, writes=1, records_changed=3)
		self.assertEqual(
			usage.current(), {"queries": 2, "records_read": 40, "writes": 1, "records_changed": 3}
		)

	def test_reporting_without_a_reset_starts_a_fresh_total(self):
		frappe.flags.automation_usage = None
		usage.report(writes=1)
		self.assertEqual(usage.current()["writes"], 1)

	def test_reset_zeroes_the_totals(self):
		usage.report(queries=5)
		usage.reset()
		self.assertEqual(usage.current()["queries"], 0)
