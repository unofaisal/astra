# For systematic debugging — a step-by-step approach to finding and fixing bugs.

# Debugging Framework

A structured approach to diagnosing and fixing bugs efficiently.

## Step 1: Reproduce the Bug
- Find a reliable, minimal reproduction case.
- If it's flaky, add logging or use a debugger to catch it consistently.
- Note the exact inputs, environment, and expected vs. actual output.

## Step 2: Isolate the Scope
- Determine what works and what doesn't.
- Check if the issue is in input handling, processing logic, output, or dependencies.
- Try to narrow the problem to a single function, module, or component.

## Step 3: Check Recent Changes
- Review recent commits, config changes, or dependency updates.
- Use `git bisect` if appropriate to find the exact commit that introduced the bug.

## Step 4: Binary Search / Divide and Conquer
- Add strategic print statements or breakpoints at the midpoint of suspected code.
- Check whether the state is correct before and after each section.
- Eliminate half the suspect area with each test.

## Step 5: Inspect State at the Failure Point
- Examine variable values, object states, and external conditions at the moment of failure.
- Use a debugger to step through line by line when needed.

## Step 6: Form and Test Hypotheses
- Based on evidence, form a hypothesis about the root cause.
- Test it with a targeted experiment or code change.
- If wrong, refine the hypothesis and repeat.

## Step 7: Fix and Verify
- Apply the fix.
- Verify the original reproduction case now passes.
- Test related functionality to ensure no regressions.

## Step 8: Prevent Regression
- Add a test case that would have caught this bug.
- Consider if logging, monitoring, or additional validation is needed.
- Document the root cause if it was non-obvious.

## Tips
- Take breaks to get fresh eyes on stubborn bugs.
- Don't assume — verify with actual data.
- Rubber duck debugging: explain the code to someone (or a rubber duck) line by line.
- Check the logs — often the answer is already there.
