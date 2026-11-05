# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE

"""What a step consumed, reported by the action itself.

An action knows how many queries it ran and how many documents it wrote; the runner cannot see
that from outside. Actions call `report`, and the runner reads the totals to hold an execution to
its limits. Reporting is a no-op cost when nothing is listening, so an action can always call it.
"""

import frappe

KEYS = ("queries", "records_read", "writes", "records_changed")


def current() -> dict:
	"""The running totals of the execution in progress, created on first use."""
	usage = frappe.flags.get("automation_usage")
	if usage is None:
		usage = frappe.flags.automation_usage = dict.fromkeys(KEYS, 0)
	return usage


def report(queries: int = 0, records_read: int = 0, writes: int = 0, records_changed: int = 0):
	"""Add to the totals: queries run, records read, write operations, documents changed."""
	usage = current()
	usage["queries"] += queries
	usage["records_read"] += records_read
	usage["writes"] += writes
	usage["records_changed"] += records_changed


def reset():
	"""Start counting from zero, as the runner does at the start of an execution."""
	frappe.flags.automation_usage = dict.fromkeys(KEYS, 0)
