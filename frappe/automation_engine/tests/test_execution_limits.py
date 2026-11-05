# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE

import json
import time
from typing import ClassVar
from unittest.mock import patch

import frappe
from frappe.automation_engine import settings, usage
from frappe.automation_engine.actions.base import AutomationAction, get_action_registry
from frappe.automation_engine.api import trial_run
from frappe.automation_engine.runner import RUN, execute_automation
from frappe.automation_engine.tests.test_automation_runner import (
	QUEUE,
	AutomationRunnerTestCase,
	branch,
	make_automation,
	make_todo,
	set_field,
	wait,
)
from frappe.automation_engine.tests.test_control_flow import FindTodos, find, in_loop, loop


class Reads(AutomationAction):
	"""Reports `queries` queries and `records` records read, and touches nothing."""

	action_type = "Reads"
	requires_document = False

	def execute(self, doc, params, context):
		usage.report(queries=params.get("queries", 1), records_read=params.get("records", 0))
		return "read"


class Writes(AutomationAction):
	"""Creates a ToDo for real and reports it, so a rollback can be seen in the database."""

	action_type = "Writes"
	requires_document = False

	def execute(self, doc, params, context):
		frappe.get_doc({"doctype": "ToDo", "description": params.get("label", "limit-w")}).insert()
		usage.report(writes=1, records_changed=params.get("changed", 1))
		return "wrote"


class Silent(AutomationAction):
	"""Says nothing about what it used, like an action from an app that predates reporting."""

	action_type = "Silent"
	requires_document = False

	def execute(self, doc, params, context):
		return "ok"


class Webhookish(Silent):
	action_type = "Webhookish"
	transactional = False


class Slow(AutomationAction):
	action_type = "Slow"
	requires_document = False

	def execute(self, doc, params, context):
		time.sleep(0.05)
		return "slow"


def step(action_type, key, **params):
	return {"action_type": action_type, "step_key": key, "params": json.dumps(params)}


class LimitsTestCase(AutomationRunnerTestCase):
	def setUp(self):
		super().setUp()
		frappe.local.automation_actions = {
			**get_action_registry(),
			"FindTodos": FindTodos(),
			"Reads": Reads(),
			"Writes": Writes(),
			"Silent": Silent(),
			"Webhookish": Webhookish(),
			"Slow": Slow(),
		}

	def run_flow(self, actions, limits=None, **kwargs):
		trigger = make_todo(description="trigger")
		auto = make_automation(actions, **kwargs)
		with patch.dict(settings.DEFAULTS, limits or {}):
			execute_automation(self.queue_row(auto, trigger.name))
		return auto, trigger

	def run_doc(self, auto):
		return frappe.get_doc(RUN, {"automation": auto})

	def state(self, auto):
		return json.loads(self.run_doc(auto).run_state or "{}")

	def todos_labelled(self, label):
		return frappe.db.count("ToDo", {"description": label})


class TestLimits(LimitsTestCase):
	def test_the_defaults_follow_the_salesforce_limits(self):
		d = settings.DEFAULTS
		self.assertEqual(
			(
				d.max_steps_per_run,
				d.max_queries_per_run,
				d.max_records_read_per_run,
				d.max_writes_per_run,
				d.max_records_changed_per_run,
				d.max_seconds_per_run,
			),
			(2000, 100, 50000, 150, 10000, 60),
		)

	def test_too_many_queries_fail_the_run_and_undo_the_whole_execution(self):
		auto, trigger = self.run_flow(
			[set_field("priority", "High"), *[step("Reads", f"r{n}") for n in range(5)]],
			limits={"max_queries_per_run": 3},
		)
		run = self.run_doc(auto)
		self.assertEqual(run.status, "Failed")
		self.assertIn("quer", run.error_summary.lower())
		self.assertIn("3", run.error_summary)
		self.assertEqual(frappe.db.get_value("ToDo", trigger.name, "priority"), "Medium")

	def test_the_log_survives_the_rollback(self):
		auto, _trigger = self.run_flow(
			[step("Reads", "r1"), step("Reads", "r2"), step("Reads", "r3")], limits={"max_queries_per_run": 2}
		)
		steps = [(s["step_key"], s["status"]) for s in self.state_steps(auto)]
		self.assertEqual(steps[:2], [("r1", "Success"), ("r2", "Success")])
		self.assertEqual(steps[2], ("r3", "Failed"))

	def state_steps(self, auto):
		from frappe.automation_engine.runner import run_steps

		return run_steps(self.run_doc(auto))

	def test_too_many_writes_leave_nothing_behind(self):
		auto, _trigger = self.run_flow(
			[step("Writes", f"w{n}", label="limit-wa") for n in range(5)], limits={"max_writes_per_run": 3}
		)
		self.assertEqual(self.run_doc(auto).status, "Failed")
		self.assertEqual(self.todos_labelled("limit-wa"), 0)
		self.assertIn("write", self.run_doc(auto).error_summary.lower())

	def test_too_many_records_changed(self):
		auto, _trigger = self.run_flow(
			[step("Writes", f"w{n}", label="limit-wb", changed=40) for n in range(3)],
			limits={"max_records_changed_per_run": 100},
		)
		self.assertEqual(self.run_doc(auto).status, "Failed")
		self.assertEqual(self.todos_labelled("limit-wb"), 0)

	def test_too_many_records_read(self):
		auto, _trigger = self.run_flow(
			[step("Reads", f"r{n}", queries=1, records=400) for n in range(3)],
			limits={"max_records_read_per_run": 1000},
		)
		self.assertEqual(self.run_doc(auto).status, "Failed")

	def test_too_much_time(self):
		auto, _trigger = self.run_flow(
			[step("Slow", f"s{n}") for n in range(4)], limits={"max_seconds_per_run": 0.06}
		)
		run = self.run_doc(auto)
		self.assertEqual(run.status, "Failed")
		self.assertIn("second", run.error_summary.lower())

	def test_the_step_limit_undoes_the_execution_too(self):
		auto, trigger = self.run_flow(
			[
				{**set_field("priority", "High"), "step_key": "again"},
				{"step_type": "Jump", "params": json.dumps({"target_step_key": "again"})},
			],
			limits={"max_steps_per_run": 8},
		)
		self.assertEqual(self.run_doc(auto).status, "Failed")
		self.assertEqual(self.run_doc(auto).error_summary, "Step limit reached (8)")
		self.assertEqual(frappe.db.get_value("ToDo", trigger.name, "priority"), "Medium")

	def test_an_action_that_does_not_report_counts_as_one_write(self):
		auto, _trigger = self.run_flow(
			[step("Silent", f"s{n}") for n in range(4)], limits={"max_writes_per_run": 3}
		)
		self.assertEqual(self.run_doc(auto).status, "Failed")

	def test_an_action_outside_the_database_does_not_count(self):
		auto, _trigger = self.run_flow(
			[step("Webhookish", f"h{n}") for n in range(5)], limits={"max_writes_per_run": 2}
		)
		self.assertEqual(self.run_doc(auto).status, "Success")

	def test_a_fault_path_cannot_catch_a_limit(self):
		auto, trigger = self.run_flow(
			[
				step("Writes", "w1", label="limit-wc"),
				step("Writes", "w2", label="limit-wc"),
				branch(set_field("description", "handled"), 2, "Fault"),
			],
			limits={"max_writes_per_run": 1},
		)
		self.assertEqual(self.run_doc(auto).status, "Failed")
		self.assertNotEqual(frappe.db.get_value("ToDo", trigger.name, "description"), "handled")
		self.assertEqual(self.todos_labelled("limit-wc"), 0)

	def test_not_stopping_on_error_does_not_get_past_a_limit(self):
		auto, _trigger = self.run_flow(
			[step("Reads", f"r{n}") for n in range(4)], limits={"max_queries_per_run": 2}, stop_on_error=0
		)
		self.assertEqual(self.run_doc(auto).status, "Failed")
		self.assertEqual(len([s for s in self.state_steps(auto) if s["step_key"].startswith("r")]), 3)

	def test_usage_is_recorded_on_the_run(self):
		auto, _trigger = self.run_flow(
			[step("Reads", "r1", queries=2, records=7), step("Writes", "w1", label="limit-wd")]
		)
		self.assertEqual(self.run_doc(auto).status, "Success")
		recorded = self.state(auto)["usage"]
		self.assertEqual(
			(recorded["queries"], recorded["records_read"], recorded["writes"], recorded["records_changed"]),
			(2, 7, 1, 1),
		)
		self.assertEqual(recorded["steps"], 2)
		self.assertEqual(recorded["limits"]["queries"], 100)

	def test_usage_is_recorded_when_a_limit_stops_the_run(self):
		auto, _trigger = self.run_flow(
			[step("Reads", f"r{n}") for n in range(5)], limits={"max_queries_per_run": 2}
		)
		self.assertEqual(self.state(auto)["usage"]["queries"], 3)

	def test_a_reasonable_flow_stays_within_the_defaults(self):
		items = [make_todo(description=f"limit-e{n}").name for n in range(100)]
		auto, _trigger = self.run_flow(
			[
				find("limit-e"),
				loop(),
				in_loop(
					{
						"action_type": "SetFieldValue",
						"target": "item",
						"params": json.dumps({"field": "priority", "value": "High"}),
					},
					2,
				),
			]
		)
		self.assertEqual(self.run_doc(auto).status, "Success")
		self.assertEqual([frappe.db.get_value("ToDo", n, "priority") for n in items], ["High"] * 100)
		# 100 updates, plus the test helper that finds the todos and does not report its usage.
		self.assertEqual(self.state(auto)["usage"]["writes"], 101)


class TestLimitsAcrossAWait(LimitsTestCase):
	def finish(self, auto):
		for _ in range(10):
			name = frappe.db.get_value(QUEUE, {"automation": auto, "resume_run": ("is", "set")}, "name")
			if not name:
				return
			execute_automation(name)

	def test_each_leg_of_a_run_is_counted_on_its_own(self):
		auto, _trigger = self.run_flow(
			[
				step("Reads", "a1"),
				step("Reads", "a2"),
				step("Reads", "a3"),
				wait(1),
				step("Reads", "b1"),
				step("Reads", "b2"),
				step("Reads", "b3"),
			],
			limits={"max_queries_per_run": 4},
		)
		with patch.dict(settings.DEFAULTS, {"max_queries_per_run": 4}):
			self.finish(auto)
		self.assertEqual(self.run_doc(auto).status, "Success")

	def test_only_the_leg_that_broke_a_limit_is_undone(self):
		auto, trigger = self.run_flow(
			[set_field("priority", "High"), wait(1), *[step("Reads", f"r{n}") for n in range(5)]],
			limits={"max_queries_per_run": 3},
		)
		with patch.dict(settings.DEFAULTS, {"max_queries_per_run": 3}):
			self.finish(auto)
		self.assertEqual(self.run_doc(auto).status, "Failed")
		self.assertEqual(frappe.db.get_value("ToDo", trigger.name, "priority"), "High")


class TestTrialRunUsage(LimitsTestCase):
	def test_a_test_run_returns_the_usage_and_the_limits(self):
		trigger = make_todo(description="trigger")
		auto = make_automation([step("Reads", "r1", queries=2), step("Writes", "w1", label="limit-wf")])
		result = trial_run(auto, trigger.name)
		self.assertEqual(result["status"], "Success")
		self.assertEqual(result["usage"]["queries"], 2)
		self.assertEqual(result["usage"]["writes"], 1)
		self.assertEqual(result["usage"]["limits"]["writes"], 150)
		self.assertEqual(self.todos_labelled("limit-wf"), 0)

	def test_a_test_run_is_held_to_the_same_limits(self):
		trigger = make_todo(description="trigger")
		auto = make_automation([step("Reads", f"r{n}") for n in range(4)])
		with patch.dict(settings.DEFAULTS, {"max_queries_per_run": 2}):
			result = trial_run(auto, trigger.name)
		self.assertEqual(result["status"], "Failed")
		self.assertIn("quer", result["error_summary"].lower())
