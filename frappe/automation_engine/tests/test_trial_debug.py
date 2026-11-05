# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE

import json
from typing import ClassVar

import frappe
from frappe.automation_engine.actions.base import AutomationAction, get_action_registry
from frappe.automation_engine.api import trial_run
from frappe.automation_engine.tests.test_automation_runner import (
	AutomationRunnerTestCase,
	make_automation,
	make_todo,
	set_field,
)

OTHER_USER = "test@example.com"


class WhoAmI(AutomationAction):
	action_type = "WhoAmI"
	requires_document = False
	seen: ClassVar[list] = []

	def execute(self, doc, params, context):
		WhoAmI.seen.append(frappe.session.user)
		return f"ran as {frappe.session.user}"


class TestTrialDebug(AutomationRunnerTestCase):
	def setUp(self):
		super().setUp()
		WhoAmI.seen = []
		frappe.local.automation_actions = {**get_action_registry(), "WhoAmI": WhoAmI()}

	def flow(self, **kwargs):
		return make_automation(
			[set_field("priority", "High"), {"action_type": "WhoAmI", "step_key": "who"}],
			condition="doc.description.startswith('match')",
			**kwargs,
		)

	def test_a_record_that_misses_the_start_conditions_is_not_run(self):
		todo = make_todo(description="other")
		result = trial_run(self.flow(), todo.name)
		self.assertEqual(result["status"], "Skipped")
		self.assertEqual(result["steps"], [])
		self.assertFalse(result["conditions_met"])
		self.assertIn("start conditions", result["error_summary"])
		self.assertEqual(WhoAmI.seen, [])

	def test_skipping_the_start_conditions_runs_it_anyway_and_says_so(self):
		todo = make_todo(description="other")
		result = trial_run(self.flow(), todo.name, skip_conditions=1)
		self.assertEqual(result["status"], "Success")
		self.assertTrue(result["conditions_skipped"])
		self.assertFalse(result["conditions_met"])
		self.assertEqual(frappe.db.get_value("ToDo", todo.name, "priority"), "Medium")

	def test_a_record_that_meets_them_just_runs(self):
		todo = make_todo(description="match this")
		result = trial_run(self.flow(), todo.name)
		self.assertEqual(result["status"], "Success")
		self.assertTrue(result["conditions_met"])
		self.assertFalse(result["conditions_skipped"])

	def test_filters_count_as_start_conditions_too(self):
		todo = make_todo(description="match", priority="Low")
		auto = make_automation(
			[set_field("priority", "High")], filters=json.dumps([["priority", "=", "High"]])
		)
		self.assertEqual(trial_run(auto, todo.name)["status"], "Skipped")

	def test_a_test_can_be_run_as_another_user(self):
		todo = make_todo(description="match")
		result = trial_run(self.flow(), todo.name, run_as=OTHER_USER)
		self.assertEqual(WhoAmI.seen, [OTHER_USER])
		self.assertEqual(result["status"], "Success")
		self.assertEqual(frappe.session.user, "Administrator")

	def test_by_default_it_runs_as_the_flows_own_user(self):
		todo = make_todo(description="match")
		trial_run(self.flow(), todo.name)
		self.assertEqual(WhoAmI.seen, ["Administrator"])

	def test_an_unknown_or_disabled_user_is_refused(self):
		todo = make_todo(description="match")
		with self.assertRaises(frappe.ValidationError):
			trial_run(self.flow(), todo.name, run_as="nobody@example.com")

	def test_only_a_system_manager_can_run_as_someone_else(self):
		todo = make_todo(description="match")
		auto = self.flow()
		plain = frappe.get_doc(
			{
				"doctype": "User",
				"email": "plain-debug@example.com",
				"first_name": "Plain",
				"send_welcome_email": 0,
			}
		).insert(ignore_permissions=True)
		frappe.set_user(plain.name)
		with self.assertRaises(frappe.PermissionError):
			trial_run(auto, todo.name, run_as="Administrator")
