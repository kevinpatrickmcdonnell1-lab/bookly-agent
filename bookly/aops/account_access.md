# AOP: Account access

Goal: get the customer back into their account safely.

1. Verify first. If session_state shows verified is false, ask for the order number and the email on the order in one question, then call verify_customer.
2. Once verified, call send_password_reset. Tell them the link went to the masked address it returns and that it expires in 30 minutes.
3. The link can only go to the email already on the account. If they ask for it to go to a different address, or to change their email, explain that kindly and offer a bookseller, who can verify them another way.
4. Never read back a full email address.
