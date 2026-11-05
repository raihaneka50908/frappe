# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE

import json
from typing import ClassVar
from unittest.mock import patch

import frappe
from frappe.automation_engine import settings
from frappe.automation_engine.actions.base import AutomationAction, get_action_registry
from frappe.automation_engine.api import trial_run
from frappe.automation_engine.runner import RUN, execute_automation
from frappe.automation_engine.tests.test_automation_runner import (
	QUEUE,
	AutomationRunnerTestCase,
	branch,
	if_step,
	make_automation,
	make_todo,
	set_field,
	wait,
)


class FindTodos(AutomationAction):
	action_type = "FindTodos"
	label = "Find ToDos"
	requires_document = False
	output_schema: ClassVar[dict] = {"records": {"doctype": "ToDo", "cardinality": "many"}}

	def output_doctype(self, params):
		return "ToDo"

	def execute(self, doc, params, context):
		names = frappe.get_all(
			"ToDo",
			filters={"description": ("like", f"{params['prefix']}%")},
			pluck="name",
			order_by="creation asc, name asc",
		)
		return {
			"detail": f"Found {len(names)}",
			"records": [{"doctype": "ToDo", "name": name} for name in names],
		}


class Tick(AutomationAction):
	action_type = "Tick"
	requires_document = False
	calls = 0

	def execute(self, doc, params, context):
		Tick.calls += 1
		return {"detail": "tick", "count": Tick.calls}


class Flaky(AutomationAction):
	action_type = "Flaky"
	seen: ClassVar[list] = []

	def execute(self, doc, params, context):
		Flaky.seen.append(doc.name)
		if doc.description.endswith("-2"):
			raise ValueError("boom")
		return "ok"


def find(prefix, key="found"):
	return {"action_type": "FindTodos", "step_key": key, "params": json.dumps({"prefix": prefix})}


def loop(source="found", alias="item", key="each"):
	params = {"source": source, "item_alias": alias}
	return {"step_type": "Loop", "step_key": key, "params": json.dumps(params)}


def in_loop(step, loop_idx):
	return branch(step, loop_idx, "Loop")


def on_item(action, alias="item"):
	return {**action, "target": alias}


def jump(key, **extra):
	return {"step_type": "Jump", "params": json.dumps({"target_step_key": key}), **extra}


def tick(key="tick"):
	return {"action_type": "Tick", "step_key": key}


class ControlFlowTestCase(AutomationRunnerTestCase):
	def setUp(self):
		super().setUp()
		Tick.calls = 0
		Flaky.seen = []
		frappe.local.automation_actions = {
			**get_action_registry(),
			"FindTodos": FindTodos(),
			"Tick": Tick(),
			"Flaky": Flaky(),
		}

	def make_todos(self, prefix, count):
		return [make_todo(description=f"{prefix}{n}").name for n in range(1, count + 1)]

	def run_flow(self, actions, **kwargs):
		trigger = make_todo(description="trigger")
		auto = make_automation(actions, **kwargs)
		execute_automation(self.queue_row(auto, trigger.name))
		return auto, trigger

	def priorities(self, names):
		return [frappe.db.get_value("ToDo", name, "priority") for name in names]

	def iterations(self, auto, step_key):
		return [s["iteration"] for s in self.run_result(auto)["steps"] if s["step_key"] == step_key]


class TestLoop(ControlFlowTestCase):
	def test_body_runs_once_per_record(self):
		items = self.make_todos("loopA-", 3)
		auto, trigger = self.run_flow(
			[
				find("loopA-"),
				loop(),
				in_loop(on_item(set_field("priority", "High")), 2),
				set_field("description", "after"),
			]
		)
		self.assertEqual(self.run_status(auto), "Success")
		self.assertEqual(self.priorities(items), ["High"] * 3)
		self.assertNotEqual(frappe.db.get_value("ToDo", trigger.name, "priority"), "High")
		self.assertEqual(frappe.db.get_value("ToDo", trigger.name, "description"), "after")

	def test_each_round_is_logged_with_its_number(self):
		self.make_todos("loopA-", 3)
		auto, _trigger = self.run_flow(
			[
				find("loopA-"),
				loop(),
				in_loop(on_item(set_field("priority", "High")), 2),
				set_field("description", "after"),
			]
		)
		self.assertEqual(self.iterations(auto, "step_3"), [1, 2, 3])
		self.assertEqual(self.iterations(auto, "step_4"), [0])

	def test_flow_without_a_loop_logs_no_round_number(self):
		auto, _trigger = self.run_flow([set_field("priority", "High")])
		self.assertEqual(self.iterations(auto, "step_1"), [0])

	def test_empty_list_skips_the_body(self):
		auto, trigger = self.run_flow(
			[
				find("none-"),
				loop(),
				in_loop(on_item(set_field("priority", "High")), 2),
				set_field("description", "after"),
			]
		)
		self.assertEqual(self.run_status(auto), "Success")
		self.assertEqual(frappe.db.get_value("ToDo", trigger.name, "description"), "after")
		positions = [step["step_idx"] for step in self.run_result(auto)["steps"]]
		self.assertEqual(positions, [0, 1, 3])

	def test_failed_round_stops_the_run_when_stop_on_error(self):
		self.make_todos("loopB-", 3)
		auto, _trigger = self.run_flow(
			[find("loopB-"), loop(), in_loop(on_item({"action_type": "Flaky"}), 2)], stop_on_error=1
		)
		self.assertEqual(self.run_status(auto), "Failed")
		self.assertEqual(len(Flaky.seen), 2)

	def test_failed_round_does_not_stop_the_loop_otherwise(self):
		self.make_todos("loopB-", 3)
		auto, _trigger = self.run_flow(
			[find("loopB-"), loop(), in_loop(on_item({"action_type": "Flaky"}), 2)], stop_on_error=0
		)
		self.assertEqual(self.run_status(auto), "Partially Failed")
		self.assertEqual(len(Flaky.seen), 3)

	def test_nested_loops_restart_the_inner_loop_each_round(self):
		self.make_todos("outer-", 2)
		self.make_todos("inner-", 3)
		auto, _trigger = self.run_flow(
			[
				find("outer-", "outer"),
				find("inner-", "inner"),
				loop("outer", "a", "each_a"),
				in_loop(loop("inner", "b", "each_b"), 3),
				in_loop(tick(), 4),
			]
		)
		self.assertEqual(self.run_status(auto), "Success")
		self.assertEqual(Tick.calls, 6)
		self.assertEqual(self.iterations(auto, "tick"), [1, 2, 3, 1, 2, 3])

	def test_trial_run_walks_the_loop_and_changes_nothing(self):
		items = self.make_todos("loopT-", 2)
		trigger = make_todo(description="trigger")
		auto = make_automation([find("loopT-"), loop(), in_loop(on_item(set_field("priority", "High")), 2)])
		result = trial_run(auto, trigger.name)
		self.assertEqual(result["status"], "Success")
		rounds = [s["iteration"] for s in result["steps"] if s["step_key"] == "step_3"]
		self.assertEqual(rounds, [1, 2])
		self.assertEqual(self.priorities(items), ["Medium"] * 2)


class TestLoopValidation(ControlFlowTestCase):
	def test_loop_source_must_be_an_earlier_step_that_returns_records(self):
		with self.assertRaises(frappe.ValidationError):
			make_automation([loop(source="ghost"), in_loop(set_field("priority", "High"), 1)])

	def test_loop_source_cannot_be_a_step_without_records(self):
		with self.assertRaises(frappe.ValidationError):
			make_automation(
				[
					{**set_field("priority", "Low"), "step_key": "found"},
					loop(),
					in_loop(set_field("priority", "High"), 2),
				]
			)

	def test_loop_needs_a_body(self):
		with self.assertRaises(frappe.ValidationError):
			make_automation([find("x-"), loop()])

	def test_loop_body_must_follow_the_loop_without_gaps(self):
		with self.assertRaises(frappe.ValidationError):
			make_automation(
				[
					find("x-"),
					loop(),
					in_loop(set_field("priority", "High"), 2),
					set_field("description", "outside"),
					in_loop(set_field("priority", "Low"), 2),
				]
			)

	def test_item_alias_cannot_repeat_an_existing_alias(self):
		with self.assertRaises(frappe.ValidationError):
			make_automation(
				[
					find("x-"),
					loop(alias="trigger"),
					in_loop(set_field("priority", "High"), 2),
				]
			)

	def test_loop_body_steps_must_use_the_loop_branch(self):
		with self.assertRaises(frappe.ValidationError):
			make_automation([find("x-"), loop(), branch(set_field("priority", "High"), 2, "If")])


class TestJump(ControlFlowTestCase):
	def test_jump_repeats_a_step_until_its_condition_fails(self):
		auto, _trigger = self.run_flow(
			[tick(), jump("tick", step_condition="context['steps']['tick']['count'] < 3")]
		)
		self.assertEqual(self.run_status(auto), "Success")
		self.assertEqual(Tick.calls, 3)

	def test_jump_out_of_a_branch_to_an_earlier_step(self):
		auto, _trigger = self.run_flow(
			[
				tick("again"),
				if_step("context['steps']['again']['count'] < 2"),
				branch(jump("again"), 2, "If"),
			]
		)
		self.assertEqual(self.run_status(auto), "Success")
		self.assertEqual(Tick.calls, 2)

	def test_endless_jump_stops_at_the_step_limit(self):
		with patch.dict(settings.DEFAULTS, {"max_steps_per_run": 10}):
			auto, _trigger = self.run_flow([tick(), jump("tick")])
		run = frappe.get_doc(RUN, {"automation": auto})
		self.assertEqual(run.status, "Failed")
		self.assertEqual(run.error_summary, "Step limit reached (10)")
		self.assertLessEqual(Tick.calls, 10)

	def test_step_limit_fails_the_run_even_when_not_stopping_on_error(self):
		with patch.dict(settings.DEFAULTS, {"max_steps_per_run": 10}):
			auto, _trigger = self.run_flow([tick(), jump("tick")], stop_on_error=0)
		self.assertEqual(self.run_status(auto), "Failed")

	def test_step_limit_applies_to_loop_rounds(self):
		self.make_todos("loopL-", 5)
		with patch.dict(settings.DEFAULTS, {"max_steps_per_run": 6}):
			auto, _trigger = self.run_flow([find("loopL-"), loop(), in_loop(tick(), 2)])
		self.assertEqual(self.run_status(auto), "Failed")
		self.assertLess(Tick.calls, 5)

	def test_jump_must_target_an_earlier_step(self):
		with self.assertRaises(frappe.ValidationError):
			make_automation([jump("later"), tick("later")])

	def test_jump_cannot_leave_its_loop(self):
		with self.assertRaises(frappe.ValidationError):
			make_automation(
				[find("x-"), tick("outside"), loop(), in_loop(jump("outside"), 3)],
			)

	def test_jump_cannot_enter_a_branch_from_outside(self):
		with self.assertRaises(frappe.ValidationError):
			make_automation([if_step("True"), branch(tick("inner"), 1, "If"), jump("inner")])


class TestWaitInsideLoop(ControlFlowTestCase):
	def finish(self, auto):
		"""Run each resume row the way the drainer would, until nothing is left waiting."""
		for _ in range(10):
			name = frappe.db.get_value(QUEUE, {"automation": auto, "resume_run": ("is", "set")}, "name")
			if not name:
				return
			execute_automation(name)

	def test_wait_first_in_a_round_resumes_on_the_same_record(self):
		items = self.make_todos("loopW-", 3)
		auto, _trigger = self.run_flow(
			[
				find("loopW-"),
				loop(),
				in_loop(wait(5), 2),
				in_loop(on_item(set_field("priority", "High")), 2),
			]
		)
		self.assertEqual(self.run_status(auto), "Waiting")
		self.assertEqual(self.priorities(items), ["Medium"] * 3)

		self.finish(auto)

		self.assertEqual(self.run_status(auto), "Success")
		self.assertEqual(self.priorities(items), ["High"] * 3)
		self.assertEqual(self.iterations(auto, "step_4"), [1, 2, 3])

	def test_wait_last_in_a_round_still_moves_to_the_next_record(self):
		items = self.make_todos("loopW-", 3)
		auto, _trigger = self.run_flow(
			[
				find("loopW-"),
				loop(),
				in_loop(on_item(set_field("priority", "High")), 2),
				in_loop(wait(5), 2),
			]
		)
		self.assertEqual(self.run_status(auto), "Waiting")
		self.assertEqual(self.priorities(items), ["High", "Medium", "Medium"])

		self.finish(auto)

		self.assertEqual(self.run_status(auto), "Success")
		self.assertEqual(self.priorities(items), ["High"] * 3)

	def test_steps_before_the_wait_do_not_run_again_after_it(self):
		self.make_todos("loopW-", 2)
		auto, _trigger = self.run_flow(
			[
				find("loopW-"),
				tick("before"),
				loop(),
				in_loop(wait(5), 3),
			]
		)
		self.finish(auto)
		self.assertEqual(self.run_status(auto), "Success")
		self.assertEqual(Tick.calls, 1)
