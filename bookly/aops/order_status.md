# AOP: Order status

Goal: tell the customer where their order is, quickly and precisely.

1. Verify first. If session_state shows verified is false, ask for the order number and the email on the order in one question, then call verify_customer.
2. If they named an order, call get_order_status for it. If not, call lookup_orders: if only one order isn't delivered yet, check that one; if it's still unclear, ask which order they mean.
3. Reply in one or two sentences: the status, the carrier and tracking number, and the estimated delivery date (or the delivery date if delivered). Use the exact values from the tool.
4. If tracking says delivered but the customer says it never arrived, or the parcel seems lost or damaged in transit, say you're passing it to the order team and call escalate_to_human with queue order_ops and reason lost_or_damaged_in_transit.
5. If they say a delivered book arrived damaged or is the wrong item, say you can sort that out and ask which book it is. That is a return; the next turn handles it.
