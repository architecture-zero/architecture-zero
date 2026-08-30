<!--
Small and obvious? Delete whatever does not apply and open it. This template is
a reminder, not a gate.
-->

## What this changes, and why

<!-- The why matters more than the what - the diff already says the what. -->

## How you verified it

<!--
Not "tests pass" - which tests, and how you know they would have failed before.
If you added a test, say that you broke the thing it guards and watched that
test go red. Several tests in this repo passed for a year without being able to
fail.

If the change touches a request path, a default, or anything a deployment sees,
say whether you RAN it. The last three defects here were found by running the
product rather than reading it.
-->

## Guarantees

- [ ] This does not narrow a stated guarantee. If it does, I said which one and why.
- [ ] No new route, or the route-authz sweep knows about it at the right privilege level.
- [ ] No secret, hostname, or private-deployment term enters the tree.
