# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE

import frappe
from frappe import _
from frappe.automation_engine.registry import DOC_TRIGGER_TYPES, clear_automation_cache
from frappe.model.document import Document

STEP_TYPES = ("Action", "Wait", "WaitForEvent", "If", "Loop", "Jump")
WAIT_STEP_TYPES = ("Wait", "WaitForEvent")
BLOCK_BRANCHES = {"If": ("If", "Else"), "Loop": ("Loop",)}
WAIT_UNITS = ("Seconds", "Minutes", "Hours", "Days")


class AutomationFlow(Document):
	"""A trigger, its filters, and the steps that run when it matches.

	`relationships` names record aliases that steps can target; each alias is
	resolved through a provider an app registered with the automation engine.
	`next_run` is the scheduler's stored due time for cron flows.
	"""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.automation.doctype.automation_action.automation_action import AutomationAction
		from frappe.types import DF

		actions: DF.Table[AutomationAction]
		automation_user: DF.Link | None
		condition: DF.Code | None
		cron_expression: DF.Data | None
		custom_event: DF.Data | None
		date_direction: DF.Literal["Before", "After"]
		date_field: DF.Literal[None]
		date_offset: DF.Int
		disabled_reason: DF.SmallText | None
		document_type: DF.Link | None
		enabled: DF.Check
		filters: DF.Code | None
		from_value: DF.Data | None
		next_run: DF.Datetime | None
		revalidate_on_run: DF.Check
		relationships: DF.JSON | None
		run_as: DF.Literal["Triggering User", "Document Owner", "Automation User"]
		stop_on_error: DF.Check
		title: DF.Data
		to_value: DF.Data | None
		trigger_field: DF.Literal[None]
		trigger_type: DF.Literal[
			"Doc Created",
			"Doc Updated",
			"Field Value Changed",
			"Doc Deleted",
			"Doc Submitted",
			"Doc Cancelled",
			"Date Based",
			"Scheduled",
			"Custom Event",
			"Manual",
		]
	# end: auto-generated types

	def validate(self):
		self.validate_document_type()
		self.validate_trigger_config()
		self.validate_execution_identity()
		self.validate_actions()
		if self.enabled:
			self.validate_ready_to_enable()

	def validate_ready_to_enable(self):
		"""Only allow enabling a rule whose every part can actually run today."""
		if not self.actions:
			frappe.throw(_("Enable an Automation Flow only after adding at least one action"))

	def validate_actions(self):
		from frappe.automation_engine.actions.base import get_action

		keys = set()
		record_sources = {}
		targets = self.get_action_targets()
		for row in self.actions:
			self.set_step_key(row, keys)
			self.validate_step(row)
			self.validate_branch(row)
			self.validate_action_aliases(row, targets)
			if (row.step_type or "Action") == "Action" and any(
				r.parent_step == row.idx and r.branch == "Fault" for r in self.actions
			):
				self.validate_fault_body(row)
			if row.step_type == "Loop":
				self.validate_loop(row, targets, record_sources)
				continue
			if row.step_type == "Jump":
				self.validate_jump(row)
				continue
			if row.step_type in (*WAIT_STEP_TYPES, "If"):
				continue
			action = get_action(row.action_type)
			self.validate_action_context(action)
			self.validate_action_params(action, row, targets, record_sources)

	def validate_loop(self, row, targets, record_sources):
		params = frappe.parse_json(row.params) if row.params else {}
		source, alias = params.get("source"), params.get("item_alias")
		if source not in record_sources:
			frappe.throw(
				_(
					"Row {0}: Loop source must be the Step Key of an earlier action that returns records"
				).format(row.idx)
			)
		if not alias:
			frappe.throw(_("Row {0}: Loop needs an Item Alias").format(row.idx))
		if alias in targets:
			frappe.throw(_("Row {0}: duplicate Item Alias {1}").format(row.idx, alias))
		targets[alias] = record_sources[source]
		self.validate_loop_body(row)

	def validate_loop_body(self, row):
		body = sorted(r.idx for r in self.actions if self.is_inside(r, row.idx))
		if not body:
			frappe.throw(_("Row {0}: a Loop needs at least one step inside it").format(row.idx))
		if body != list(range(row.idx + 1, body[-1] + 1)):
			frappe.throw(_("Row {0}: the steps inside a Loop must follow it without gaps").format(row.idx))

	def is_inside(self, row, block_idx):
		parent = row.parent_step
		while parent:
			if parent == block_idx:
				return True
			parent = next((r.parent_step for r in self.actions if r.idx == parent), 0)
		return False

	def validate_jump(self, row):
		params = frappe.parse_json(row.params) if row.params else {}
		key = params.get("target_step_key")
		target = next((r for r in self.actions if r.step_key == key and r.idx < row.idx), None)
		if not target:
			frappe.throw(_("Row {0}: Jump must target the Step Key of an earlier step").format(row.idx))
		if (target.parent_step or 0, target.branch or "") not in self.jump_scope(row):
			frappe.throw(
				_("Row {0}: Jump cannot leave its Loop or enter a block from outside").format(row.idx)
			)

	def jump_scope(self, row):
		"""The (parent step, branch) pairs a Jump may land in: its own block and the ones around
		it, up to the nearest Loop, which a Jump cannot leave."""
		scope = []
		while True:
			scope.append((row.parent_step or 0, row.branch or ""))
			parent = self.block_at(row.parent_step)
			if not parent or parent.step_type == "Loop":
				return scope
			row = parent

	def validate_action_params(self, action, row, targets, record_sources=None):
		"""An action reports a bad param by raising, which reaches the user as a bare 500;
		rethrowing it names the row and reads like every other save error."""
		from frappe.automation_engine.actions.base import AutomationParamError

		params = frappe.parse_json(row.params) if row.params else {}
		try:
			action.validate(params, targets.get(row.target))
		except AutomationParamError as e:
			frappe.throw(_("Row {0}: {1}").format(row.idx, str(e)))
		targets.update(action.output_targets(params, row.output_alias))
		if record_sources is not None and "records" in (action.output_schema or {}):
			record_sources[row.step_key] = action.output_doctype(params)

	def get_action_targets(self):
		from frappe.automation_engine.relationships import get_relationship_targets

		return get_relationship_targets(self.document_type, self.relationships)

	def validate_action_aliases(self, row, targets):
		from frappe.automation_engine.conditions import validate_related_condition

		row.target = row.target or "trigger"
		if row.target not in targets:
			frappe.throw(_("Row {0}: unknown target alias {1}").format(row.idx, row.target))
		validate_related_condition(row.related_condition, targets)
		if row.output_alias and row.output_alias in targets:
			frappe.throw(_("Row {0}: duplicate output alias {1}").format(row.idx, row.output_alias))

	def set_step_key(self, row, keys):
		row.step_key = row.step_key or f"step_{row.idx}"
		if row.step_key in keys:
			frappe.throw(_("Row {0}: Step Key must be unique").format(row.idx))
		keys.add(row.step_key)

	def validate_execution_identity(self):
		self.run_as = self.run_as or "Automation User"
		if self.run_as != "Automation User":
			return
		self.automation_user = self.automation_user or "Administrator"
		if not frappe.db.get_value("User", self.automation_user, "enabled"):
			frappe.throw(_("Automation User {0} is disabled or missing").format(self.automation_user))

	def validate_branch(self, row):
		"""A step inside an If or Loop must name that block (by idx) and which arm it belongs to."""
		if not row.parent_step:
			if row.branch:
				frappe.throw(
					_("Row {0}: Branch is only meaningful inside an If or Loop step").format(row.idx)
				)
			return
		if row.parent_step >= row.idx:
			frappe.throw(_("Row {0}: Parent Step must be an earlier row").format(row.idx))
		if row.branch == "Fault":
			return self.validate_fault_branch(row)
		parent = self.block_at(row.parent_step)
		if not parent:
			frappe.throw(
				_("Row {0}: Parent Step {1} is not an If or Loop step").format(row.idx, row.parent_step)
			)
		if row.branch not in BLOCK_BRANCHES[parent.step_type]:
			frappe.throw(
				_("Row {0}: choose the {1} branch").format(
					row.idx, " or ".join(BLOCK_BRANCHES[parent.step_type])
				)
			)

	def validate_fault_branch(self, row):
		"""A Fault step belongs to the action it recovers from, and nothing else can own one."""
		parent = next((r for r in self.actions if r.idx == row.parent_step), None)
		if not parent or (parent.step_type or "Action") != "Action":
			frappe.throw(
				_("Row {0}: a Fault path belongs to an action step, not to row {1}").format(
					row.idx, row.parent_step
				)
			)

	def validate_fault_body(self, row):
		"""The steps of an action's Fault path directly follow it, so the walk reaches them in order."""
		body = sorted(r.idx for r in self.actions if self.is_inside(r, row.idx))
		if body != list(range(row.idx + 1, body[-1] + 1)):
			frappe.throw(
				_("Row {0}: the steps of a Fault path must follow their action without gaps").format(row.idx)
			)

	def if_step_at(self, idx):
		return next((r for r in self.actions if r.idx == idx and r.step_type == "If"), None)

	def block_at(self, idx):
		return next((r for r in self.actions if r.idx == idx and r.step_type in BLOCK_BRANCHES), None)

	def validate_action_context(self, action):
		if action.requires_document and not self.document_type:
			frappe.throw(_("{0} requires a Document Type").format(action.label))
		if action.supported_trigger_types and self.trigger_type not in action.supported_trigger_types:
			frappe.throw(_("{0} does not support {1} triggers").format(action.label, self.trigger_type))

	def validate_step(self, row):
		row.step_type = row.step_type or "Action"
		if row.step_type not in STEP_TYPES:
			frappe.throw(_("Row {0}: unsupported Step Type {1}").format(row.idx, row.step_type))
		if row.step_type == "Action" and not row.action_type:
			frappe.throw(_("Action Type is required for action steps"))
		if row.step_type == "If" and not row.step_condition:
			frappe.throw(_("Row {0}: an If step needs a Step Condition").format(row.idx))
		if row.step_type == "Wait":
			self.validate_wait(row)
		if row.step_type == "WaitForEvent":
			self.validate_event_wait(row)

	def validate_wait(self, row):
		params = frappe.parse_json(row.params) if row.params else {}
		if not params.get("value") or not params.get("unit"):
			frappe.throw(_("Wait steps require a duration value and unit"))
		if params["unit"] not in WAIT_UNITS:
			frappe.throw(_("Row {0}: Wait unit must be one of {1}").format(row.idx, ", ".join(WAIT_UNITS)))

	def validate_event_wait(self, row):
		from frappe.automation_engine.events import validate_wait_params

		validate_wait_params(frappe.parse_json(row.params) if row.params else {})

	def validate_document_type(self):
		if self.document_type and frappe.get_meta(self.document_type).istable:
			frappe.throw(_("Automation Flow cannot target a child table: {0}").format(self.document_type))

	def validate_trigger_config(self):
		needs_doctype = self.trigger_type in DOC_TRIGGER_TYPES or self.trigger_type == "Date Based"
		if needs_doctype and not self.document_type:
			frappe.throw(_("{0} trigger requires a Document Type").format(self.trigger_type))
		if self.trigger_type == "Field Value Changed" and not self.trigger_field:
			frappe.throw(_("Field Value Changed trigger requires a Trigger Field"))
		if self.trigger_type == "Date Based" and not (self.date_field and self.date_direction):
			frappe.throw(_("Date Based trigger requires a Date Field and a Date Direction"))
		if self.trigger_type == "Custom Event" and not self.custom_event:
			frappe.throw(_("Custom Event trigger requires an event name"))
		if self.trigger_type == "Field Value Changed":
			self.validate_field_exists("trigger_field")
		if self.trigger_type == "Date Based":
			self.validate_field_exists("date_field", fieldtypes=("Date", "Datetime"))
		if self.trigger_type == "Scheduled":
			self.validate_cron()
		self.set_next_run()

	def validate_field_exists(self, fieldname, fieldtypes=None):
		"""A field left over from another Document Type never matches, and a missing date column
		fails the hourly sweep for every Date Based flow."""
		from frappe.model import std_fields

		value = self.get(fieldname)
		df = frappe.get_meta(self.document_type).get_field(value)
		df = df or next((f for f in std_fields if f["fieldname"] == value), None)
		if not df or (fieldtypes and df.get("fieldtype") not in fieldtypes):
			frappe.throw(
				_("{0}: {1} is not a valid field on {2}").format(
					_(self.meta.get_label(fieldname)), value, self.document_type
				)
			)

	def set_next_run(self):
		from frappe.automation_engine.scheduler import next_fire

		if self.trigger_type != "Scheduled":
			self.next_run = None
		elif self.has_value_changed("cron_expression") or not self.next_run:
			self.next_run = next_fire(self.cron_expression, frappe.utils.now_datetime())

	def validate_cron(self):
		from croniter import croniter

		if not self.cron_expression:
			frappe.throw(_("Scheduled trigger requires a Cron Expression"))
		if not croniter.is_valid(self.cron_expression):
			frappe.throw(_("Invalid cron expression: {0}").format(self.cron_expression))

	def on_update(self):
		clear_automation_cache(self.document_type)
		# Retargeting a flow leaves the old doctype's cached map holding a rule that no longer
		before = self.get_doc_before_save()
		if before and before.document_type and before.document_type != self.document_type:
			clear_automation_cache(before.document_type)

	def on_trash(self):
		clear_automation_cache(self.document_type)
		queue = frappe.qb.DocType("Automation Trigger Queue")
		subscription = frappe.qb.DocType("Automation Event Subscription")
		frappe.qb.from_(subscription).delete().where(
			subscription.resume_queue.isin(
				frappe.qb.from_(queue).select(queue.name).where(queue.automation == self.name)
			)
		).run()
		frappe.db.delete("Automation Trigger Queue", {"automation": self.name})
