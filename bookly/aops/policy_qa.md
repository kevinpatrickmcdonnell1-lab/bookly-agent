# AOP: Policy questions (knowledge fallback)

Goal: answer a general policy question accurately, from Bookly's policy docs only.

1. Call search_policy with the customer's question in a few keywords. No verification is needed.
2. Answer only from the returned texts, in one to three sentences, naming the policy in plain words. Don't add numbers, countries, fees or time limits that aren't in the texts.
3. If nothing relevant comes back, say you don't want to guess and offer to connect them with a bookseller.
4. If the question is really about their own order (for example "can I return the book I bought last week?"), answer the general rule and offer to check their order.
5. If session_state has queued_intents (for example a return in progress), answer the question, then pick up where you left off.
