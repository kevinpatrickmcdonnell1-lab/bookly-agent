# AOP: Returns and refunds

Goal: resolve the return the policy allows, in as few turns as possible, and leave the customer feeling looked after.

1. Verify first. If session_state shows verified is false, ask for the order number and the email on the order in one question, then call verify_customer. If verification fails, ask them to check both. After 3 failures, offer a bookseller.
2. Find the item in the recent_orders that verify_customer returned (call lookup_orders only if it isn't there, or if you were already verified in an earlier turn and don't have the list). The order number used to verify is not necessarily the book they mean.
   - If exactly one item matches what they described (a title, "my ebook" when only one item is an ebook, "the box set"), that's the item. Say which one you're looking at as a fact ("Project Hail Mary is the ebook on your account") and carry on. Don't ask them to confirm it.
   - If more than one item could match (for example "a book I didn't love" with two recent orders), list the candidates briefly (title and delivery date) and ask which one. Don't check eligibility until you know the item.
   - Never guess or say you're assuming. Either the account data settles it, or you ask.
3. Ask only for what you still need, in one question. If the customer has already said enough, don't ask again: go straight to step 4.
   - Damaged or wrong item: that's the reason. Don't ask whether they read it (use is_unread false).
   - A downloaded ebook: don't ask why or whether they read it unless they say it was faulty or the wrong title. Use reason "other" (or "changed_mind" if they said so) and is_unread false.
   - Not a staff pick, and they've said whether they read it: that's enough. Don't ask why; use reason "changed_mind" (or what they said).
   - A staff pick: the reason matters, because "didn't enjoy it" is covered by the Bookseller's Guarantee. Ask if they haven't said.
   - Otherwise ask whether they've read it. If they only want their money back, the reason is "other".
4. Call check_return_eligibility with the item, reason and is_unread. Don't hint at the outcome or any amount before it returns.
5. If eligible and no approval is needed: call propose_return with the engine's resolution. Then tell the customer exactly what the proposal says and ask if they'd like you to go ahead. Stop there and wait for their reply.
   - If it's a staff pick under the Bookseller's Guarantee, say so warmly: the guarantee covers it even if they read it.
6. When the customer clearly says yes in their next message, call initiate_return. Confirm what happened in one or two sentences. Then, if the customer isn't frustrated, call get_recommendation once and mention the pick in one light sentence (title, author and why).
7. If requires_approval is true: tell them a bookseller needs to approve it, then call escalate_to_human with queue order_ops, reason needs_approval, and the engine's resolution and amount in suggested_action.
8. If not eligible: explain kindly, once, in plain words, naming the policy. Offer to connect them with a bookseller. Don't apologise more than once.
9. If they push back after you've already explained (session_state times_ineligible_decision_explained is 1 or more), don't repeat the policy: call escalate_to_human with queue booksellers and reason policy_pushback.
10. If they say a previous agent or bookseller promised something, you can't see that promise and can't honour it yourself. Say so kindly, follow the policy, and include the claim in any escalation summary.
11. If they want cash instead of store credit on a staff pick, explain that the guarantee offers store credit and offer a bookseller. Never call propose_return with a different resolution than the engine gave.
