# For reviewing code — checking correctness, style, security, and maintainability.

# Code Review

A systematic approach to reviewing code for quality, correctness, and maintainability.

## High-Level Checks First
- Does the code do what it's supposed to do?
- Is the logic correct? Walk through edge cases mentally.
- Are there any obvious bugs, off-by-one errors, or null pointer issues?

## Code Quality
- **Readability**: Is the code easy to understand? Are names clear?
- **Modularity**: Is the code well-organized? Are functions/methods focused?
- **Consistency**: Does it follow the existing style and conventions?
- **Comments**: Are they useful, or just restating the code?

## Security
- Check for common vulnerabilities: injection, XSS, CSRF, hardcoded secrets.
- Validate inputs and sanitize where needed.
- Verify authentication and authorization checks.

## Performance
- Identify obvious inefficiencies (nested loops, N+1 queries, unnecessary allocations).
- Consider scalability — will this work at higher volumes?

## Testing
- Are there tests? Do they cover edge cases?
- Is the test code itself clean and maintainable?

## Style & Conventions
- Follow the project's style guide (linters, formatters).
- Keep functions small and single-responsibility.
- Avoid overly long lines or deeply nested code.

## How to Give Feedback
- Be constructive: explain the "why" behind suggestions.
- Separate blocking issues from style preferences.
- Acknowledge what's done well.
- Use phrases like "Have you considered..." instead of "You should..."
