# Manual edge tests

These cannot be automated without the Video Analytics 5.x edge and portal.
Procedure: ../../docs/EDGE_VALIDATION.md. Record sheets (copy, fill in, attach to the ticket):

* `record_E04_loop_30min.csv`: 30-minute loop observation
* `record_E10_reboot.csv`: 16-stream reboot recovery
* `record_edge_cases.csv`: one line per edge case with result and evidence location

Status of every edge-dependent check stays **NOT RUN** until it has been executed on the edge.
