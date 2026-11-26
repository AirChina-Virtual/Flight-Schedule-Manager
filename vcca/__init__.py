"""vCCA season flight-plan tool (CAAC pre-plan + FR24 vs VAMSYS).

Pipeline (see INTERFACES.md):
    config -> caac / vamsys -> fr24_store (+fr24_fetch) -> fr24_obs -> evidence -> fleets/reconcile
    -> decisions.json -> export_xlsx / export_csv / export_html / report_summary
"""

__version__ = "0.1.0"
