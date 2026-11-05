# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE

import time
from contextlib import contextmanager

import frappe
from frappe import _
from frappe.automation_engine import settings, usage
from frappe.automation_engine.actions.base import StopAutomation, get_action_registry
from frappe.automation_engine.conditions import condition_values, evaluate_related_condition
from frappe.automation_engine.dispatch import matches_rule
from frappe.automation_engine.events import get_wait_outcome, schedule_event_wait
from frappe.automation_engine.queue import (
	QUEUE,
	WAITING_STATES,
	clear_effects,
	mark_effects_delivered,
	queue_status,
)
from frappe.automation_engine.registry import clear_automation_cache
from frappe.automation_engine.relationships import load_record, resolve_relationships
from frappe.utils import add_to_date, cint, now

RUN = "Automation Run"
LOOP_ARM = "Loop"
EXECUTION_SAVEPOINT = "auto_exec"
STEP_FIELDS = (
	"step_idx",
	"step_key",
	"iteration",
	"action_type",
	"status",
	"detail",
	"message",
	"exception",
	"traceback",
	"condition",
	"condition_values",
	"output",
	"duration_ms",
)
STEP_JSON_FIELDS = ("condition_values", "output")
WAIT_UNIT_SECONDS = {"Seconds": 1, "Minutes": 60, "Hours": 3600, "Days": 86400}


# What `context.error` holds outside a Fault path: present but empty, so a template can read it.
NO_ERROR = {"message": "", "step_key": ""}


# The limits on one execution (the stretch of a run between waits), after the Salesforce flow
# limits: what is counted, the setting that holds the limit, and what the failure says.
LIMITS = {
	"steps": ("max_steps_per_run", "Step limit reached ({0})"),
	"queries": ("max_queries_per_run", "Query limit reached ({0}): the flow ran {1} queries in one go"),
	"records_read": (
		"max_records_read_per_run",
		"Records-read limit reached ({0}): the flow read {1} records in one go",
	),
	"writes": (
		"max_writes_per_run",
		"Write limit reached ({0}): the flow made {1} write operations in one go",
	),
	"records_changed": (
		"max_records_changed_per_run",
		"Records-changed limit reached ({0}): the flow changed {1} records in one go",
	),
	"seconds": (
		"max_seconds_per_run",
		"Time limit reached ({0} seconds): the flow had been running for {1} seconds",
	),
}
COUNTED = ("steps", *usage.KEYS)


class ExecutionLimitReached(frappe.ValidationError):
	"""An execution went past one of its limits.

	Not an ordinary step failure: nothing in the flow, a Fault path included, may handle it, and
	everything the execution changed is undone.
	"""

	def __init__(self, kind, limit, used):
		self.kind, self.limit, self.used = kind, limit, used
		super().__init__(_(LIMITS[kind][1]).format(limit, used))


class StepLimitReached(ExecutionLimitReached):
	def __init__(self, limit, used):
		super().__init__("steps", limit, used)


def execute_automation(queue_name: str):
	row = frappe.get_doc(QUEUE, queue_name)
	# Settle any event wait this row is resuming before the plan reads its outcome.
	event = get_wait_outcome(row) if row.resume_run else None
	rule = frappe.get_cached_doc("Automation Flow", row.automation)
	doc = _load_target(row)
	run, steps, snapshot = _run_state(rule, row, doc)

	if doc is None and row.ref_doctype and row.ref_name:
		return _finalize(run, rule, row, "Skipped", steps, error="Target document not found")
	if rule.revalidate_on_run and doc and not matches_rule(rule, doc):
		return _finalize(run, rule, row, "Skipped", steps, error="Rule no longer matches")
	status, context = _execute_plan(rule, row, run, doc, steps, snapshot, event)
	_finalize(run, rule, row, status, steps, context=context)


def _execute_plan(rule, row, run, doc, steps, snapshot, event):
	previous_depth = frappe.flags.get("automation_depth", 0)
	previous_run = frappe.flags.get("in_automation_run")
	frappe.flags.automation_depth = cint(row.depth)
	# Actions revalidate their params before running. The flag lets an action tell that apart
	# from the flow being authored, so a save-time gate is not re-applied to the run.
	frappe.flags.in_automation_run = True
	context = None
	try:
		with _execution_identity(rule, row, doc):
			_ensure_trigger_access(doc)
			context = _context(row, run, rule, doc, event)
			usage.reset()
			frappe.db.savepoint(EXECUTION_SAVEPOINT)
			status = _run_plan(steps, rule, doc, context, snapshot, cint(row.resume_from_idx))
		return status, context
	except Exception:
		# Setting up the run (identity, trigger access, alias resolution) failed.
		_append_step(steps, {"step_key": "setup"}, len(steps), "Failed", frappe.get_traceback(), 0)
		return "Failed", context
	finally:
		frappe.flags.automation_depth = previous_depth
		frappe.flags.in_automation_run = previous_run


def _run_state(rule, row, doc):
	"""Return (run, steps_so_far, actions_snapshot) for a fresh or resumed run."""
	if row.resume_run:
		return _resumed_run_state(row)
	snapshot = [_action_snapshot(action) for action in rule.actions]
	return _create_run(rule, row, snapshot, doc), [], snapshot


def _resumed_run_state(row):
	"""Pick a waiting run back up: same Automation Run, its original snapshot and steps."""
	run = frappe.get_doc(RUN, row.resume_run)
	return run, run_steps(run), frappe.parse_json(run.actions_snapshot) or []


def run_steps(run) -> list[dict]:
	"""The run's recorded steps as plain dicts, in the shape the runner appends them."""
	steps = []
	for row in run.steps:
		step = {field: row.get(field) for field in STEP_FIELDS}
		for field in STEP_JSON_FIELDS:
			step[field] = frappe.parse_json(step[field]) if step[field] else None
		steps.append(step)
	return steps


def _load_target(row):
	if not (row.ref_doctype and row.ref_name):
		return None
	try:
		return frappe.get_doc(row.ref_doctype, row.ref_name)
	except frappe.DoesNotExistError:
		return None


def _create_run(rule, row, snapshot, doc):
	run = frappe.new_doc(RUN)
	run.update(_run_start_values(rule, row, snapshot, doc))
	# The referenced doc may legitimately be gone (deleted, or a Doc Deleted trigger); the
	# run must record it regardless, so skip Dynamic Link existence validation.
	run.flags.ignore_links = True
	return run.insert(ignore_permissions=True)


def _run_start_values(rule, row, snapshot, doc) -> dict:
	return {
		"automation": rule.name,
		"automation_title": rule.title,
		"status": "Running",
		"reference_doctype": row.ref_doctype,
		"reference_name": row.ref_name,
		"user": _execution_user(rule, row, doc),
		"depth": cint(row.depth),
		"queue_row": row.name,
		"started_at": now(),
		"actions_snapshot": frappe.as_json(snapshot),
		"relationships": rule.relationships,
	}


def _action_snapshot(action) -> dict:
	return {
		"idx": cint(action.idx),
		"step_type": action.step_type or "Action",
		"action_type": action.action_type,
		"params": action.params,
		"step_condition": action.step_condition,
		"related_condition": action.related_condition,
		"step_key": action.step_key or f"step_{action.idx}",
		"target": action.target or "trigger",
		"output_alias": action.output_alias,
		"parent_step": cint(action.parent_step),
		"branch": action.branch or "",
	}


def _context(row, run, rule, doc, event=None) -> dict:
	"""Assemble what steps can read. A resumed run restores the aliases and outputs the first
	leg recorded on the Automation Run instead of re-resolving them."""
	state = frappe.parse_json(run.run_state) if run.run_state else {}
	return {
		"payload": frappe.parse_json(row.event_payload) if row.event_payload else {},
		"event": event or {},
		"queue_row": row,
		"run": run,
		"rule": rule,
		"trigger_doc": doc,
		"steps": state.get("step_outputs") or {},
		"records": state.get("records") or resolve_relationships(doc, frappe.parse_json(run.relationships)),
		"branches": state.get("branches") or {},
		"loops": state.get("loops") or {},
		# Counted from zero on every execution, including the one after a wait.
		"usage": dict.fromkeys(COUNTED, 0),
		"started": time.monotonic(),
		"external": [],
		# What the failed action said, while its Fault path is running; empty everywhere else.
		"error": state.get("error") or dict(NO_ERROR),
		"fault_span": state.get("fault_span"),
	}


@contextmanager
def _execution_identity(rule, row, doc):
	# frappe.set_user rebuilds local.session in place, and local.session IS session_obj.data,
	# which the request writes back to tabSessions on the way out. Restoring the user alone
	# would persist an emptied session and log the caller out; the whole session has to go back.
	previous = frappe._dict(frappe.session)
	# Switching identity is the point - the flow's run_as policy, validated on save, decides the
	# user, and every step still runs its own permission checks.
	frappe.set_user(_execution_user(rule, row, doc))  # nosemgrep
	try:
		yield
	finally:
		frappe.set_user(previous.user)  # nosemgrep
		frappe.local.session.update(previous)


def _execution_user(rule, row, doc):
	if rule.run_as == "Triggering User":
		return row.triggered_by or rule.owner
	if rule.run_as == "Document Owner" and doc:
		return doc.owner
	return rule.automation_user or "Administrator"


def _ensure_trigger_access(doc):
	if doc:
		doc.check_permission("read")


def _run_plan(steps, rule, doc, context, snapshot, start_idx=0) -> str:
	"""Walk the snapshot; going past a limit undoes the whole execution and fails the run."""
	try:
		return _walk_plan(steps, rule, doc, context, snapshot, start_idx)
	except ExecutionLimitReached as error:
		return _abort_execution(steps, context, error)


def _abort_execution(steps, context, error) -> str:
	"""Undo everything this execution changed, keep the log, and fail the run."""
	frappe.db.rollback(save_point=EXECUTION_SAVEPOINT)
	usage.reset()
	step, pos = context.get("at") or ({"step_key": "limit"}, len(steps))
	note = _limit_note(error)
	if context["external"]:
		note["detail"] = note["message"] = f"{note['message']}\n" + _(
			"Not undone, because it already left the database: {0}"
		).format(", ".join(context["external"]))
	_append_step(steps, step, pos, "Failed", note, 0, _iteration(context, pos))
	return "Failed"


def _walk_plan(steps, rule, doc, context, snapshot, start_idx=0) -> str:
	"""Walk the snapshot, honouring branch arms, loops and jumps, and skipping what already ran."""
	registry = get_action_registry()
	taken = context["branches"]
	context["plan"] = _plan_layout(snapshot)
	overall = "Success"
	pos = 0
	while pos < len(snapshot):
		step = snapshot[pos]
		context["at"] = (step, pos)
		if pos >= start_idx:
			start_idx = 0
		status, detail, duration = _safe_step_outcome(registry, step, doc, context, pos, start_idx, taken)
		if status is not None:
			entry = _append_step(steps, step, pos, status, detail, duration, _iteration(context, pos))
			_update_context(context, step, entry)
			if status == "Waiting":
				return "Waiting"
			if status == "Failed":
				overall = "Partially Failed"
				if not _enter_fault_path(step, detail, pos, context, taken) and rule.stop_on_error:
					return "Failed"
		following = _next_position(snapshot, context, pos, start_idx)
		_leave_fault_path(context, following)
		if following != pos + 1:
			start_idx = 0
		pos = following
	return overall


def _enter_fault_path(step, detail, pos, context, taken) -> bool:
	"""Hand a failed action to its Fault path, when it has one and the failure may be handled.

	The steps of that path sit right after the action; marking the action's arm as "Fault" makes
	them the ones the walk takes next. The limit on the run is never handled this way.
	"""
	end = context["plan"]["fault_end"].get(pos)
	if end is None:
		return False
	taken[_branch_key(step.get("idx"))] = "Fault"
	message = detail.get("message") if isinstance(detail, dict) else None
	context["error"] = {"message": message or str(detail), "step_key": step.get("step_key") or ""}
	context["fault_span"] = [pos, end]
	return True


def _leave_fault_path(context, following):
	"""Forget the error once the walk is past the Fault path (or has jumped out of it)."""
	span = context.get("fault_span")
	if span and not span[0] < following <= span[1]:
		context["error"] = dict(NO_ERROR)
		context["fault_span"] = None


def _safe_step_outcome(registry, step, doc, context, pos, start_idx, taken):
	try:
		return _step_outcome(registry, step, doc, context, pos, start_idx, taken)
	except ExecutionLimitReached:
		raise
	except Exception:
		return "Failed", frappe.get_traceback(), 0


def _step_outcome(registry, step, doc, context, pos, start_idx, taken):
	"""Resolve one plan entry. A None status means it is not on this run's path at all."""
	if not _branch_active(step, taken):
		return None, None, None
	step_type = step.get("step_type") or "Action"
	if step_type == "If":
		return _resolve_if(step, doc, context, pos, start_idx, taken)
	if pos < start_idx:
		return None, None, None  # already executed in the leg before the wait
	_count_step(context)
	if not _step_condition_matches(step, doc, context):
		return "Skipped", _skip_detail(step, doc, context), 0
	if step_type == "Wait":
		return _begin_wait(step, context, pos)
	if step_type == "WaitForEvent":
		return _begin_event_wait(step, context, pos)
	if step_type == "Loop":
		return _begin_loop(step, context, pos)
	if step_type == "Jump":
		return _jump(step, context)
	return _run_one(registry, step, doc, context, pos)


def _count_step(context):
	"""Count one step execution against the execution's budget; a loop or jump cannot outlive it.

	This is also where the clock is read: between steps, so one long step is never cut short.
	"""
	context["usage"]["steps"] += 1
	if context["usage"]["steps"] > settings.get("max_steps_per_run"):
		raise StepLimitReached(settings.get("max_steps_per_run"), context["usage"]["steps"])
	elapsed = time.monotonic() - context["started"]
	if elapsed > settings.get("max_seconds_per_run"):
		raise ExecutionLimitReached("seconds", settings.get("max_seconds_per_run"), round(elapsed, 1))


def _account_usage(context, handler, status):
	"""Add what the action just consumed to the execution's totals, and hold them to the limits."""
	taken = usage.take()
	if status == "Success" and handler and handler.transactional and not any(taken.values()):
		taken["writes"] = 1  # an action that does not report is assumed to write once
	for key in usage.KEYS:
		context["usage"][key] += taken[key]
	if handler and not handler.transactional and status == "Success":
		context["external"].append(context["at"][0].get("step_key") or "")
	for kind in usage.KEYS:
		limit = settings.get(LIMITS[kind][0])
		if context["usage"][kind] > limit:
			raise ExecutionLimitReached(kind, limit, context["usage"][kind])


def _usage_report(context) -> dict:
	"""The execution's totals next to the limits they were held to, for the run and the test run."""
	limits = {kind: settings.get(LIMITS[kind][0]) for kind in COUNTED}
	limits["seconds"] = settings.get(LIMITS["seconds"][0])
	return {
		**context["usage"],
		"seconds": round(time.monotonic() - context["started"], 1),
		"limits": limits,
	}


def _limit_note(error) -> dict:
	return {
		"note": True,
		"detail": str(error),
		"message": str(error),
		"exception": f"{type(error).__module__}.{type(error).__name__}",
	}


def _plan_layout(snapshot) -> dict:
	"""Where each block starts and ends, so the walk can loop back and jump without rescanning.

	`body_end` maps a Loop's position to the position of its last body step, `loops_ending_at`
	lists the Loops that close at a position (innermost first), `enclosing` maps a position to its
	nearest Loop, `inner_loops` maps a Loop to the Loops nested inside it, and `positions` maps
	step keys to positions for Jump targets.
	"""
	by_idx = {cint(step["idx"]): pos for pos, step in enumerate(snapshot)}
	parent = {pos: by_idx.get(cint(step.get("parent_step"))) for pos, step in enumerate(snapshot)}
	loops = [pos for pos, step in enumerate(snapshot) if step.get("step_type") == "Loop"]
	layout = {
		"body_end": {},
		"loops_ending_at": {},
		"enclosing": {},
		"inner_loops": {},
		"loop_keys": {loop_pos: _branch_key(snapshot[loop_pos]["idx"]) for loop_pos in loops},
		"positions": {step["step_key"]: pos for pos, step in enumerate(snapshot)},
		"end": len(snapshot),
		"fault_end": {},
	}
	for pos in range(len(snapshot)):
		body = [other for other in range(len(snapshot)) if _inside(parent, other, pos)]
		if body and any(snapshot[other].get("branch") == "Fault" for other in body):
			layout["fault_end"][pos] = max(body)
	for loop_pos in loops:
		body = [pos for pos in range(len(snapshot)) if _inside(parent, pos, loop_pos)]
		layout["inner_loops"][loop_pos] = [other for other in loops if _inside(parent, other, loop_pos)]
		if not body:
			continue
		layout["body_end"][loop_pos] = max(body)
		layout["loops_ending_at"].setdefault(max(body), []).append(loop_pos)
		for pos in body:
			if pos not in layout["enclosing"] or layout["enclosing"][pos] < loop_pos:
				layout["enclosing"][pos] = loop_pos
	for ending in layout["loops_ending_at"].values():
		ending.sort(reverse=True)
	return layout


def _inside(parent, pos, ancestor) -> bool:
	pos = parent.get(pos)
	while pos is not None:
		if pos == ancestor:
			return True
		pos = parent.get(pos)
	return False


def _iteration(context, pos) -> int | None:
	"""The current round of the Loop this position sits in, if any."""
	loop_pos = context["plan"]["enclosing"].get(pos)
	state = _loop_state(context, loop_pos) if loop_pos is not None else None
	return state["index"] + 1 if state else None


def _loop_state(context, loop_pos) -> dict | None:
	return context["loops"].get(context["plan"]["loop_keys"][loop_pos])


def _next_position(snapshot, context, pos, start_idx) -> int:
	"""Where the walk goes after `pos`: a jump target, the next round of a Loop, or the next row."""
	jump = context.pop("jump_to", None)
	if jump is not None:
		return jump
	skip = context.pop("skip_to", None)
	if skip is not None:
		return skip
	if pos < start_idx - 1:
		return pos + 1
	for loop_pos in context["plan"]["loops_ending_at"].get(pos, ()):
		if _advance_loop(snapshot, context, loop_pos):
			return loop_pos + 1
	return pos + 1


def _begin_loop(step, context, pos):
	started = time.monotonic()
	params = _step_params(step)
	records = _loop_records(params, context)
	plan = context["plan"]
	if not records:
		context["skip_to"] = plan["body_end"].get(pos, pos) + 1
		return "Success", _("Loop has no records"), _ms(started)
	key = plan["loop_keys"][pos]
	context["loops"][key] = {"records": records, "index": 0}
	context["branches"][key] = LOOP_ARM
	_bind_loop_item(step, context, records[0])
	return (
		"Success",
		{"detail": _("Looping over {0} records").format(len(records)), "count": len(records)},
		_ms(started),
	)


def _loop_records(params, context) -> list:
	source = params.get("source")
	output = context["steps"].get(source)
	records = output.get("records") if isinstance(output, dict) else None
	if not isinstance(records, list):
		raise ValueError(_("Loop source {0} did not return a list of records").format(source))
	return records


def _bind_loop_item(step, context, record):
	context["records"][_step_params(step)["item_alias"]] = record


def _advance_loop(snapshot, context, loop_pos) -> bool:
	"""Move a Loop that just ran its last body step to its next record. False once it is spent."""
	plan = context["plan"]
	key = plan["loop_keys"][loop_pos]
	state = context["loops"].get(key)
	if not state or context["branches"].get(key) != LOOP_ARM:
		return False
	for inner in plan["inner_loops"][loop_pos]:
		inner_key = plan["loop_keys"][inner]
		context["loops"].pop(inner_key, None)
		context["branches"].pop(inner_key, None)
	state["index"] += 1
	if state["index"] >= len(state["records"]):
		context["loops"].pop(key)
		context["branches"].pop(key)
		return False
	_count_step(context)
	_bind_loop_item(snapshot[loop_pos], context, state["records"][state["index"]])
	return True


def _jump(step, context):
	target = _step_params(step).get("target_step_key")
	position = context["plan"]["positions"].get(target)
	if position is None:
		raise ValueError(_("Jump target {0} is not a step of this flow").format(target))
	context["jump_to"] = position
	return "Success", _("Jumped to {0}").format(target), 0


def _branch_active(step, taken) -> bool:
	"""True when the enclosing If (if any) chose the arm this step sits in."""
	parent = cint(step.get("parent_step"))
	if not parent:
		return True
	arm = taken.get(_branch_key(parent))
	if arm is None:
		return False  # the enclosing If was itself on an arm that wasn't taken
	return (step.get("branch") or "If") == arm


def _branch_key(idx) -> str:
	# Keyed by string: this dict round-trips through the run's JSON state, which has no
	# integer keys, and a resumed leg has to look up what the first one wrote.
	return str(cint(idx))


def _resolve_if(step, doc, context, pos, start_idx, taken):
	"""Pick the arm for an If step, on the leg that first reaches it.

	The arm is run state, not a derivation: the document may have changed during the Wait.
	"""
	key = _branch_key(step.get("idx"))
	forced = _forced_arm(key)
	if pos < start_idx:
		# Already decided, unless this run parked before arms were recorded.
		if key in taken:
			return None, None, None
		taken[key] = forced or _evaluated_arm(step, doc, context)
		return None, None, None
	_count_step(context)
	taken[key] = arm = forced or _evaluated_arm(step, doc, context)
	if forced:
		return "Success", _("Forced the {0} branch").format(arm), 0
	return "Success", _("Condition took the {0} branch").format(arm), 0


def _evaluated_arm(step, doc, context) -> str:
	return "If" if _step_condition_matches(step, doc, context) else "Else"


def _forced_arm(key) -> str | None:
	"""A trial can ask for the arm the record itself would not take, to test the other path."""
	if not frappe.flags.get("in_automation_trial"):
		return None
	arm = (frappe.flags.get("automation_branch_overrides") or {}).get(key)
	return arm if arm in ("If", "Else") else None


def _begin_wait(step, context, pos):
	started = time.monotonic()
	params = _step_params(step)
	if frappe.flags.get("in_automation_trial"):
		# Nothing will ever resume a trial, so parking here would hide every later step.
		return "Success", _simulated_wait(params), _ms(started)
	seconds = _wait_seconds(params)
	schedule_wait(context, seconds, pos + 1)
	return "Waiting", _("Waiting {0} seconds").format(seconds), _ms(started)


def _simulated_wait(params) -> str:
	return _("Waited {0} {1} (simulated)").format(cint(params.get("value")), params.get("unit") or "Minutes")


def _begin_event_wait(step, context, pos):
	started = time.monotonic()
	params = _step_params(step)
	if frappe.flags.get("in_automation_trial"):
		# Nothing will ever emit the event for a trial, so parking here would hide every later
		# step. Steps behind it read an empty event payload.
		return "Success", _simulated_event_wait(params), _ms(started)
	subscription = schedule_event_wait(context, params, step["step_key"], pos + 1)
	return "Waiting", _("Waiting for {0}").format(subscription.event_name), _ms(started)


def _simulated_event_wait(params) -> str:
	return _("Waited for {0} (simulated)").format(params.get("event_name") or _("event"))


def _wait_seconds(params) -> int:
	unit = params.get("unit") or "Minutes"
	return cint(params.get("value")) * WAIT_UNIT_SECONDS.get(unit, 60)


def schedule_wait(context, seconds: int, resume_from_idx: int):
	"""Queue the resume row that picks this run up once the wait elapses."""
	run_after = add_to_date(now(), seconds=seconds)
	frappe.get_doc(resume_row_values(context, run_after, resume_from_idx)).insert(ignore_permissions=True)


def resume_row_values(context, run_after, resume_from_idx) -> dict:
	"""The queue row that continues a parked run at `run_after`.

	dedup_key is NULL whenever resume_run is set, so resume rows never collide with a fresh
	trigger for the same document, or with each other.
	"""
	row = context["queue_row"]
	return {
		"doctype": QUEUE,
		"automation": row.automation,
		"ref_doctype": row.ref_doctype,
		"ref_name": row.ref_name,
		"status": queue_status(run_after),
		"triggered_at": now(),
		"run_after": run_after,
		"depth": cint(row.depth),
		"triggered_by": row.triggered_by,
		"event_payload": row.event_payload,
		"resume_run": context["run"].name,
		"resume_from_idx": resume_from_idx,
	}


def _run_one(registry, step, doc, context, idx):
	started = time.monotonic()
	savepoint = f"auto_step_{idx}"
	handler = registry.get(step.get("action_type"))
	params = _step_params(step)
	for last_attempt in (False, True):
		outcome = _try_action(handler, step, doc, context, params, savepoint, idx, started)
		if outcome[0] != "Retry":
			_account_usage(context, handler, outcome[0])
			return outcome
		if last_attempt:
			_account_usage(context, handler, "Failed")
			return "Failed", outcome[1], outcome[2]
		if doc:
			doc.reload()


def _try_action(handler, step, doc, context, params, savepoint, idx, started):
	frappe.db.savepoint(savepoint)
	messages_before = len(frappe.local.message_log)
	try:
		target = _target_doc(step, doc, context, permission_type="write")
		if not handler:
			raise ValueError(f"Unknown action type: {step.get('action_type')}")
		handler.validate(params, target.doctype if target else None)
		if not handler.transactional:
			# Before the call, not after: a webhook that answers 500, or times out, has still left
			# this process, and nothing here can tell whether it arrived. Marking a step that turns
			# out to fail early costs a retry; marking one too late repeats the request.
			mark_effects_delivered(context["queue_row"].name)
		return "Success", handler.execute(target, params, context), _ms(started)
	except StopAutomation as error:
		schedule_wait(context, error.resume_after, idx + 1)
		return "Waiting", str(error) or _("Automation paused"), _ms(started)
	except frappe.TimestampMismatchError:
		frappe.db.rollback(save_point=savepoint)
		return "Retry", frappe.get_traceback(), _ms(started)
	except Exception as error:
		frappe.db.rollback(save_point=savepoint)
		return "Failed", _failure_detail(error, messages_before), _ms(started)


def _skip_detail(step, doc, context) -> dict:
	"""Why a gated step was passed over: the condition source, and the values it read."""
	condition = step.get("step_condition") or ""
	target = _target_doc(step, doc, context, permission_type="read") if condition else None
	return {
		"note": True,
		"detail": _("Step condition did not match"),
		"condition": condition or step.get("related_condition") or "",
		"condition_values": condition_values(condition, {"doc": doc, "target": target}),
	}


def _failure_detail(error, messages_before) -> dict:
	"""What the step failed with: the traceback, plus anything the action threw as a message."""
	traceback = frappe.get_traceback()
	return {
		"note": True,
		"detail": traceback,
		"traceback": traceback,
		"message": _thrown_message(error, messages_before),
		"exception": f"{type(error).__module__}.{type(error).__name__}",
	}


def _thrown_message(error, messages_before) -> str:
	thrown = frappe.local.message_log[messages_before:]
	messages = [message.get("message") for message in thrown if message.get("message")]
	return "\n".join(messages) if messages else str(error)


def _step_params(step) -> dict:
	params = step.get("params")
	return (frappe.parse_json(params) if isinstance(params, str) else params) or {}


def _step_condition_matches(step, doc, context) -> bool:
	if not evaluate_related_condition(step.get("related_condition"), context):
		return False
	condition = step.get("step_condition")
	if not condition:
		return True
	target = _target_doc(step, doc, context, permission_type="read")
	return bool(frappe.safe_eval(condition, None, {"doc": doc, "target": target, "context": context}))


def _append_step(steps, step, idx, status, detail, duration, iteration=None):
	note = detail if isinstance(detail, dict) and detail.get("note") else {}
	detail, output = (note["detail"], None) if note else _action_result(detail)
	entry = {
		"step_idx": idx,
		"step_key": step.get("step_key") or f"step_{idx + 1}",
		"iteration": iteration,
		"action_type": step.get("action_type") or step.get("step_type") or "Action",
		"status": status,
		"detail": (detail or "")[:5000],
		"message": _trim(note.get("message")),
		"exception": note.get("exception"),
		"traceback": _trim(note.get("traceback")),
		"condition": note.get("condition"),
		"condition_values": note.get("condition_values"),
		"output": output,
		"duration_ms": duration,
	}
	steps.append(entry)
	return entry


def _trim(value):
	return value[:5000] if value else None


def _target_doc(step, trigger_doc, context, permission_type=None):
	target = step.get("target") or "trigger"
	if target == "trigger":
		if trigger_doc and permission_type:
			trigger_doc.check_permission(permission_type)
		return trigger_doc
	return load_record(context["records"].get(target), permission_type=permission_type)


def _update_context(context, step, entry):
	"""Publish a finished step's output so later steps and Jinja can read it."""
	output = entry.get("output") or {}
	if not _within_output_limit(output):
		# The action already ran, so oversized output is dropped rather than failing the run.
		entry["output"] = output = {"truncated": True}
	context["steps"][entry["step_key"]] = output
	alias = step.get("output_alias")
	if alias and output.get("destination_reference"):
		context["records"][alias] = output["destination_reference"]


def _within_output_limit(output) -> bool:
	return len(frappe.as_json(output).encode()) <= settings.get("step_output_limit")


def _action_result(result):
	if not isinstance(result, dict):
		return result, None
	detail = result.get("detail") or result.get("destination_reference") or _("Action completed")
	return str(detail), result


def _ms(started) -> int:
	return int((time.monotonic() - started) * 1000)


def _finalize(run, rule, row, status, steps, error=None, context=None):
	handled = bool(context) and "Fault" in context["branches"].values()
	error_summary = error or _error_summary(status, steps, handled)
	run.update(_run_values(status, steps, error_summary, context))
	# The target may have been deleted since the run started (notably across a Wait, or on a
	# Doc Deleted trigger); the run must still record its outcome, so skip link validation.
	run.flags.ignore_links = True
	run.save(ignore_permissions=True)
	_settle_queue_row(row, status)

	# Neither of the things below is in the transaction a trial rolls back: the breaker counter
	# lives in Redis, and a realtime event has already left.
	if frappe.flags.get("in_automation_trial"):
		clear_effects(row.name)
		return

	if status == "Failed":
		_record_failure(rule)
	elif status == "Success":
		_reset_failures(rule)

	_publish_update(run, rule, status)
	# The mark is deliberately left standing. This outcome is not durable until the drainer commits
	# it, and a commit that fails re-runs the whole group; the drainer clears the mark once the row
	# is safely recorded.


def _publish_update(run, rule, status):
	frappe.publish_realtime(
		"automation_run_update",
		{"automation": rule.name, "run": run.name, "status": status},
		doctype=run.reference_doctype,
		docname=run.reference_name,
	)


def _run_values(status, steps, error_summary, context=None) -> dict:
	values = {"status": status, "error_summary": error_summary, "steps": _step_rows(steps)}
	if context:
		values["run_state"] = frappe.as_json(
			{
				"step_outputs": context["steps"],
				"records": context["records"],
				"branches": context["branches"],
				"loops": context["loops"],
				"executed": context["usage"]["steps"],
				"usage": _usage_report(context),
				"error": context["error"],
				"fault_span": context["fault_span"],
			}
		)
	if status != "Waiting":
		# A Waiting run is finished by the resume row, on this same record.
		values["ended_at"] = now()
	return values


def _step_rows(steps) -> list[dict]:
	# Serialized here rather than by the JSON field, which cannot encode the dates and decimals
	# a condition or an action output can carry.
	return [
		{**step, **{field: frappe.as_json(step[field]) for field in STEP_JSON_FIELDS if step.get(field)}}
		for step in steps
	]


def _settle_queue_row(row, status):
	# Completed and Waiting rows are dropped; Failed and Skipped are kept for the purge sweep.
	# Plain DELETE rather than frappe.delete_doc, which would archive a Deleted Document copy of
	# every queue row this site ever ran.
	if status in ("Success", "Partially Failed", "Waiting"):
		frappe.db.delete(QUEUE, {"name": row.name})
	else:
		frappe.db.set_value(QUEUE, row.name, "status", status, update_modified=False)


def _error_summary(status, steps, handled=False) -> str | None:
	"""One line for the run list. The app's own thrown message beats a traceback tail.

	A run that recovered through a Fault path names the step that failed, since the status alone
	(Partially Failed) would not say where.
	"""
	if status not in ("Failed", "Partially Failed"):
		return None
	failed = next((step for step in steps if step["status"] == "Failed"), None)
	message = (failed or {}).get("message")
	if message:
		line = message.splitlines()[0][:140]
		return f"{failed['step_key']}: {line}" if handled and failed.get("step_key") else line
	detail = _first_error_detail(steps)
	return detail.splitlines()[-1][:140] if detail else "Failed"


def _first_error_detail(steps) -> str | None:
	for step in steps:
		if step["status"] == "Failed":
			return step["detail"] or "Failed"
	return "Failed"


def _failure_key(rule_name) -> str:
	return f"automation_failures::{frappe.local.site}::{rule_name}"


def _record_failure(rule):
	threshold = settings.get("failure_threshold")
	if frappe.cache.incr(_failure_key(rule.name)) >= threshold:
		_trip_breaker(rule, threshold)


def _reset_failures(rule):
	frappe.cache.delete(_failure_key(rule.name))


def _trip_breaker(rule, threshold):
	reason = _("Auto-disabled after {0} consecutive failures").format(threshold)
	frappe.db.set_value(
		"Automation Flow", rule.name, {"enabled": 0, "disabled_reason": reason}, update_modified=False
	)
	# Drop the orphaned backlog in one UPDATE, the rule won't run again.
	frappe.db.set_value(
		QUEUE,
		{"automation": rule.name, "status": ("in", WAITING_STATES)},
		"status",
		"Skipped",
		update_modified=False,
	)
	clear_automation_cache(rule.document_type)
	frappe.cache.delete(_failure_key(rule.name))
	_notify_owner(rule, reason)


def _notify_owner(rule, reason):
	owner = frappe.db.get_value("Automation Flow", rule.name, "owner")
	if not owner:
		return
	frappe.get_doc(
		{
			"doctype": "Notification Log",
			"for_user": owner,
			"type": "Alert",
			"subject": _("Automation Flow {0} was auto-disabled").format(rule.title),
			"email_content": reason,
			"document_type": "Automation Flow",
			"document_name": rule.name,
		}
	).insert(ignore_permissions=True)
