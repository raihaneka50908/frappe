# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE

from frappe.model.document import Document


class AutomationSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		allow_unregistered_events: DF.Check
		commit_every: DF.Int
		disable_automations: DF.Check
		drain_seconds: DF.Float
		event_payload_limit: DF.Int
		failure_threshold: DF.Int
		max_attempts: DF.Int
		max_depth: DF.Int
		max_queries_per_run: DF.Int
		max_records_changed_per_run: DF.Int
		max_records_read_per_run: DF.Int
		max_seconds_per_run: DF.Int
		max_steps_per_run: DF.Int
		max_writes_per_run: DF.Int
		queue_retention_days: DF.Int
		stale_running_minutes: DF.Int
		step_output_limit: DF.Int
	# end: auto-generated types
