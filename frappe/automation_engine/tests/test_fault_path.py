# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE

import json

import frappe
from frappe.automation_engine.api import trial_run
from frappe.automation_engine.runner import RUN, execute_automation
from frappe.automation_engine.tests.test_automation_runner import (
	AutomationRunnerTestCase,
	branch,
	if_step,
	make_automation,
	make_todo,
	set_field,
)


def failing_create():
	"""A Create Document that always fails: ToDo.priority is a Select with fixed options."""
	return {
		"action_type": "CreateDocument",
		"step_key": "make",
		"params": json.dumps({"doctype": "ToDo", "values": {"description": "never", "priority": "Bogus"}}),
	}


def fault(action, parent_idx):
	return branch(action, parent_idx, "Fault")


def stop_flow():
	return {"action_type": "StopFlow", "step_key": "halt"}


class TestFaultPath(AutomationRunnerTestCase):
	def run_flow(self, actions, **kwargs):
		todo = make_todo(description="start")
		auto = make_automation(actions, **kwargs)
		execute_automation(self.queue_row(auto, todo.name))
		run = frappe.get_doc(RUN, {"automation": auto})
		return todo, run

	def test_a_failing_action_with_a_fault_path_runs_the_path_and_the_flow_goes_on(self):
		todo, run = self.run_flow(
			[
				failing_create(),
				fault(set_field("description", "failed: {{ context.error.message }}"), 1),
				set_field("priority", "High"),
			]
		)
		todo.reload()
		self.assertIn("Priority", todo.description)
		self.assertTrue(todo.description.startswith("failed: "))
		self.assertEqual(todo.priority, "High")
		self.assertEqual(run.status, "Partially Failed")

	def test_the_run_never_reports_success_when_a_step_failed(self):
		_todo, run = self.run_flow([failing_create(), fault(set_field("priority", "Low"), 1)])
		self.assertNotEqual(run.status, "Success")
		self.assertIn("make", run.error_summary or "")

	def test_earlier_steps_are_not_undone(self):
		todo, _run = self.run_flow(
			[set_field("priority", "High"), failing_create(), fault(set_field("description", "handled"), 2)]
		)
		todo.reload()
		self.assertEqual((todo.priority, todo.description), ("High", "handled"))

	def test_the_failed_step_leaves_nothing_behind(self):
		self.run_flow([failing_create(), fault(set_field("description", "handled"), 1)])
		self.assertFalse(frappe.db.exists("ToDo", {"description": "never"}))

	def test_a_fault_path_is_skipped_when_the_action_succeeds(self):
		todo = make_todo(description="start")
		ok = {
			"action_type": "CreateDocument",
			"step_key": "make",
			"params": json.dumps({"doctype": "ToDo", "values": {"description": "fine"}}),
		}
		auto = make_automation([ok, fault(set_field("description", "should not run"), 1)])
		execute_automation(self.queue_row(auto, todo.name))
		self.assertEqual(frappe.db.get_value(RUN, {"automation": auto}, "status"), "Success")
		self.assertEqual(frappe.db.get_value("ToDo", todo.name, "description"), "start")

	def test_without_a_fault_path_a_failure_behaves_as_before(self):
		todo, run = self.run_flow([failing_create(), set_field("priority", "High")])
		self.assertEqual(run.status, "Failed")
		self.assertEqual(frappe.db.get_value("ToDo", todo.name, "priority"), "Medium")

	def test_without_a_fault_path_and_not_stopping_the_flow_goes_on(self):
		todo, run = self.run_flow([failing_create(), set_field("priority", "High")], stop_on_error=0)
		self.assertEqual(run.status, "Partially Failed")
		self.assertEqual(frappe.db.get_value("ToDo", todo.name, "priority"), "High")

	def test_the_error_is_empty_outside_the_fault_path(self):
		todo, _run = self.run_flow(
			[
				failing_create(),
				fault(set_field("description", "in"), 1),
				set_field("description", "after:{{ context.error.message }}"),
			]
		)
		self.assertEqual(frappe.db.get_value("ToDo", todo.name, "description"), "after:")

	def test_the_error_names_the_step_that_failed(self):
		todo, _run = self.run_flow(
			[failing_create(), fault(set_field("description", "{{ context.error.step_key }}"), 1)]
		)
		self.assertEqual(frappe.db.get_value("ToDo", todo.name, "description"), "make")

	def test_stop_flow_ends_the_run_after_the_fault_path(self):
		todo, run = self.run_flow(
			[
				failing_create(),
				fault(set_field("description", "handled"), 1),
				fault(stop_flow(), 1),
				set_field("priority", "High"),
			]
		)
		todo.reload()
		self.assertEqual((todo.description, todo.priority), ("handled", "Medium"))
		self.assertEqual(run.status, "Partially Failed")

	def test_a_decision_inside_a_fault_path(self):
		todo, _run = self.run_flow(
			[
				failing_create(),
				fault(if_step("doc.description == 'start'"), 1),
				branch(set_field("description", "took if"), 2, "If"),
				branch(set_field("description", "took else"), 2, "Else"),
			]
		)
		self.assertEqual(frappe.db.get_value("ToDo", todo.name, "description"), "took if")

	def test_a_test_run_shows_the_fault_path_and_changes_nothing(self):
		todo = make_todo(description="start")
		auto = make_automation([failing_create(), fault(set_field("description", "handled"), 1)])
		result = trial_run(auto, todo.name)
		keys = [(step["step_key"], step["status"]) for step in result["steps"]]
		self.assertEqual(keys[0], ("make", "Failed"))
		self.assertEqual(result["status"], "Partially Failed")
		self.assertEqual(frappe.db.get_value("ToDo", todo.name, "description"), "start")


class TestFaultPathValidation(AutomationRunnerTestCase):
	def assertRejected(self, actions, message):
		with self.assertRaises(frappe.ValidationError) as error:
			make_automation(actions)
		self.assertIn(message, str(error.exception))

	def test_a_fault_path_on_an_action_saves(self):
		make_automation(
			[failing_create(), fault(set_field("priority", "Low"), 1), fault(set_field("status", "Open"), 1)]
		)

	def test_a_fault_path_on_a_decision_is_rejected(self):
		self.assertRejected(
			[if_step("doc.priority == 'Low'"), fault(set_field("priority", "High"), 1)],
			"Fault",
		)

	def test_a_fault_path_on_a_wait_is_rejected(self):
		wait = {"step_type": "Wait", "params": json.dumps({"value": 1, "unit": "Seconds"})}
		self.assertRejected([wait, fault(set_field("priority", "High"), 1)], "Fault")

	def test_the_fault_steps_must_follow_the_action_without_gaps(self):
		self.assertRejected(
			[failing_create(), set_field("priority", "Low"), fault(set_field("status", "Open"), 1)],
			"without gaps",
		)

	def test_a_jump_cannot_land_inside_a_fault_path(self):
		jump = {"step_type": "Jump", "step_key": "back", "params": json.dumps({"target_step_key": "inside"})}
		inside = {**set_field("status", "Open"), "step_key": "inside"}
		self.assertRejected([failing_create(), fault(inside, 1), jump], "Jump")

	def test_a_normal_branch_name_on_an_action_parent_is_rejected(self):
		self.assertRejected(
			[failing_create(), branch(set_field("priority", "Low"), 1, "If")], "not an If or Loop"
		)
